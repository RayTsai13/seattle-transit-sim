"""End-to-end contract tests for the heatmap intermediary.

Covers the wire protocol from ``docs/heatmap-api-contract.md`` and the data
shape the frontend depends on per ``docs/frontend-heatmap.md``:

- SSE handshake order: ``config`` → ``scenario`` → ``frame``
- Monotonic event ids; correct ``text/event-stream`` headers
- ``config`` payload matches the source GeoJSON (bounds / rows / cols)
- ``frame`` cells are sparse ``[row, col, density]`` tuples with valid indices
  and density in ``[0, 1]``
- Centroid formula from the contract reconstructs the original polygon
  centroids in the GeoJSON (so the frontend's lookup will land on real cells)
- ``POST /api/scenario`` accepts the documented ids and rejects unknown ones
- ``POST /api/people`` validates bounds, persists, and affects future frames
- ``DELETE /api/people/{id}`` and ``DELETE /api/people`` follow contract codes

Plain HTTP routes are exercised through ``fastapi.testclient.TestClient``.
The SSE behavior is verified by driving the route's async generator directly
with a fake ``Request`` — that's how we actually consume an open-ended stream
without the HTTP stack buffering the whole infinite body.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import server
from backend.geo import haversine_m
from backend.landuse import SEATTLE_DISTRICTS, is_probable_water
from backend.network import ServiceCapacity, default_network_for_scenario
from backend.sim_time import MINUTES_PER_DAY, MINUTES_PER_WEEK
from backend.state import DEFAULT_SCENARIO_ID, VALID_SCENARIO_IDS, State

GEOJSON_PATH = Path("seattle/data/processed/seattle_heatmap_grid.geojson")

CELL_ID_RE = re.compile(r"^r(\d+)_c(\d+)$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore a clean :class:`State` before and after every test.

    Also shrinks the frame cadence so the SSE loop wakes quickly between
    iterations and notices the fake disconnect within the test's lifetime.
    """
    monkeypatch.setattr(server, "FRAME_INTERVAL_S", 0.02)
    server.STATE = State(server.GRID, frame_interval_seconds=server.FRAME_INTERVAL_S)
    yield
    server.STATE = State(server.GRID, frame_interval_seconds=server.FRAME_INTERVAL_S)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(server.app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Fake Request + SSE-stream driver
# ---------------------------------------------------------------------------


class FakeRequest:
    """Stand-in for :class:`fastapi.Request` exposing only the API the
    streaming endpoint touches: ``await is_disconnected()``."""

    def __init__(self) -> None:
        self._disconnected = False

    def disconnect(self) -> None:
        self._disconnected = True

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _parse_sse_event(block: str) -> dict:
    """Parse one ``id:/event:/data:`` SSE record into a dict."""
    out: dict = {}
    for raw_line in block.splitlines():
        if raw_line.startswith("id:"):
            out["id"] = int(raw_line[3:].strip())
        elif raw_line.startswith("event:"):
            out["event"] = raw_line[6:].strip()
        elif raw_line.startswith("data:"):
            out["data"] = json.loads(raw_line[5:].strip())
    return out


def _assert_frame_payload(payload: dict) -> None:
    assert set(payload) == {"timestamp", "state_version", "sim_time", "cells"}
    assert isinstance(payload["timestamp"], (int, float))
    assert isinstance(payload["state_version"], str)
    assert set(payload["sim_time"]) == {"day_of_week", "time_bin", "minute_of_week"}
    assert isinstance(payload["cells"], list)

    rows, cols = server.GRID.rows, server.GRID.cols
    for cell in payload["cells"]:
        assert isinstance(cell, list) and len(cell) == 3
        row, col, density = cell
        assert isinstance(row, int) and 0 <= row < rows
        assert isinstance(col, int) and 0 <= col < cols
        assert isinstance(density, (int, float))
        assert 0.0 <= float(density) <= 1.0


async def drive_stream(
    request: FakeRequest,
    n: int,
    timeout: float = 5.0,
) -> list[dict]:
    """Pull ``n`` events from ``server._stream`` then disconnect cleanly."""

    async def _collect() -> list[dict]:
        events: list[dict] = []
        buffer = ""
        async for chunk in server._stream(request):
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                if block.strip():
                    events.append(_parse_sse_event(block))
                    if len(events) >= n:
                        request.disconnect()
                        return events
        return events

    return await asyncio.wait_for(_collect(), timeout=timeout)


# ---------------------------------------------------------------------------
# Endpoint headers (StreamingResponse object — no body iteration needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_endpoint_returns_event_stream_headers() -> None:
    response = await server.heatmap_stream(FakeRequest())
    assert response.media_type == "text/event-stream"
    assert response.headers.get("cache-control") == "no-cache"
    assert response.headers.get("x-accel-buffering") == "no"


# ---------------------------------------------------------------------------
# Connection + handshake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_initial_handshake_is_config_scenario_playback_then_frame() -> None:
    """Initial state on connect: config, scenario, playback, then a frame."""
    events = await drive_stream(FakeRequest(), n=4)
    assert [e["event"] for e in events] == ["config", "scenario", "playback", "frame"]
    assert [e["id"] for e in events] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_event_ids_are_monotonically_increasing() -> None:
    events = await drive_stream(FakeRequest(), n=4)
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# Config payload alignment with the source GeoJSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_payload_matches_loaded_geojson() -> None:
    events = await drive_stream(FakeRequest(), n=1)
    config = events[0]["data"]

    assert set(config) == {"bounds", "rows", "cols"}
    assert set(config["bounds"]) == {"west", "south", "east", "north"}
    assert config["rows"] == server.GRID.rows
    assert config["cols"] == server.GRID.cols
    assert config["bounds"] == server.GRID.bounds.to_dict()


def test_config_bounds_form_uniform_canonical_grid() -> None:
    """The contract's centroid formula assumes uniform cell sizes. Verify the
    loader's bounds + ``rows`` × ``cols`` describe such a grid (i.e. cell
    width and cell height come out exactly the same as the source GeoJSON's
    canonical (0, 0) cell)."""
    bounds = server.GRID.bounds
    raw = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    f00 = next(
        f for f in raw["features"] if f["properties"]["cell_id"] == "r000_c000"
    )
    ring = f00["geometry"]["coordinates"][0]
    src_lons = [p[0] for p in ring]
    src_lats = [p[1] for p in ring]
    src_cell_w = max(src_lons) - min(src_lons)
    src_cell_h = max(src_lats) - min(src_lats)

    derived_cell_w = (bounds.east - bounds.west) / server.GRID.cols
    derived_cell_h = (bounds.north - bounds.south) / server.GRID.rows
    assert derived_cell_w == pytest.approx(src_cell_w)
    assert derived_cell_h == pytest.approx(src_cell_h)


# ---------------------------------------------------------------------------
# Scenario event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_scenario_event_uses_default_scenario() -> None:
    events = await drive_stream(FakeRequest(), n=2)
    assert events[1]["event"] == "scenario"
    assert events[1]["data"] == {"scenario_id": DEFAULT_SCENARIO_ID}


# ---------------------------------------------------------------------------
# Frame schema (used directly by the frontend GeoJSON conversion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frame_payload_matches_documented_schema() -> None:
    events = await drive_stream(FakeRequest(), n=4)
    frame = events[3]
    assert frame["event"] == "frame"

    payload = frame["data"]
    _assert_frame_payload(payload)


@pytest.mark.asyncio
async def test_frame_cells_are_sparse_no_zero_density() -> None:
    """Contract: cells with density == 0 may be omitted; we omit them."""
    events = await drive_stream(FakeRequest(), n=4)
    cells = events[3]["data"]["cells"]
    assert all(density > 0 for _, _, density in cells)


@pytest.mark.asyncio
async def test_frame_density_stays_in_unit_range() -> None:
    """Density is an absolute ratio of unmet trips to a fixed ceiling, so it
    must never leave [0, 1] -- but it is not expected to fill the range at
    every hour. That is the point: a quiet night should look quiet."""
    events = await drive_stream(FakeRequest(), n=4)
    cells = events[3]["data"]["cells"]
    assert cells, "expected at least one nonzero cell"
    assert all(0.0 < density <= 1.0 for _, _, density in cells)


def test_weekday_peak_is_hot_and_night_is_quiet() -> None:
    """The absolute scale must separate rush hour from the small hours."""
    server.STATE.playback.set_playing(False)
    server.STATE.set_scenario("line-1")

    server.STATE.seek_playback(day_of_week=3, time_bin=8 * 60)
    peak = server.STATE.compose_frame_cells()
    server.STATE.seek_playback(day_of_week=3, time_bin=3 * 60)
    night = server.STATE.compose_frame_cells()

    assert max(density for _, _, density in peak) > 0.6
    assert max(density for _, _, density in night) < 0.15
    assert _mean_frame_density(peak) > _mean_frame_density(night) * 5


# ---------------------------------------------------------------------------
# Centroid math: contract formula must reconstruct real polygon centroids
# ---------------------------------------------------------------------------


def test_contract_centroid_formula_matches_geojson_polygon_centroids() -> None:
    """The frontend builds its centroid lookup from the contract formula
    ``lon = west + (col + 0.5) * cell_w``,
    ``lat = north - (row + 0.5) * cell_h``.

    For every full (non-partial) GeoJSON cell that the loader keeps, that
    formula must land on the polygon's geometric centroid — otherwise the
    rendered heatmap would be offset from the underlying data.

    The source file's ``row 0`` sits at the south, while the contract
    requires ``row 0`` at the north, so the loader flips rows. This test
    mirrors that flip to verify cell positions end up where the frontend
    expects them.
    """
    raw = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    bounds = server.GRID.bounds
    rows = server.GRID.rows
    cols = server.GRID.cols
    cell_w = (bounds.east - bounds.west) / cols
    cell_h = (bounds.north - bounds.south) / rows

    f00 = next(
        f for f in raw["features"] if f["properties"]["cell_id"] == "r000_c000"
    )
    src_lats_00 = [p[1] for p in f00["geometry"]["coordinates"][0]]
    f_top = next(
        f for f in raw["features"] if f["properties"]["cell_id"] == f"r{rows - 1:03d}_c000"
    )
    src_lats_top = [p[1] for p in f_top["geometry"]["coordinates"][0]]
    source_row_zero_is_north = max(src_lats_00) >= max(src_lats_top)

    checked = 0
    for feature in raw["features"]:
        match = CELL_ID_RE.match(feature["properties"]["cell_id"])
        assert match
        src_row, src_col = int(match.group(1)), int(match.group(2))
        if src_row >= rows or src_col >= cols:
            continue

        contract_row = src_row if source_row_zero_is_north else (rows - 1 - src_row)
        contract_col = src_col

        ring = feature["geometry"]["coordinates"][0]
        unique = ring[:-1] if ring[0] == ring[-1] else ring
        poly_lon = sum(p[0] for p in unique) / len(unique)
        poly_lat = sum(p[1] for p in unique) / len(unique)

        contract_lon = bounds.west + (contract_col + 0.5) * cell_w
        contract_lat = bounds.north - (contract_row + 0.5) * cell_h

        assert poly_lon == pytest.approx(contract_lon, abs=1e-9)
        assert poly_lat == pytest.approx(contract_lat, abs=1e-9)
        checked += 1

    assert checked == rows * cols, "expected every kept cell to be validated"


# ---------------------------------------------------------------------------
# POST /api/scenario
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", sorted(VALID_SCENARIO_IDS))
def test_post_scenario_accepts_documented_ids(
    client: TestClient, scenario_id: str
) -> None:
    response = client.post("/api/scenario", json={"scenario_id": scenario_id})
    assert response.status_code == 200
    payload = response.json()
    assert payload["scenario_id"] == scenario_id
    _assert_frame_payload(payload["frame"])
    assert server.STATE.scenario_id == scenario_id


def test_post_scenario_rejects_unknown_id(client: TestClient) -> None:
    response = client.post("/api/scenario", json={"scenario_id": "made-up"})
    assert response.status_code == 400


def test_post_scenario_rejects_missing_field(client: TestClient) -> None:
    response = client.post("/api/scenario", json={})
    assert response.status_code == 400


def test_post_scenario_accepts_active_network_payload(client: TestClient) -> None:
    payload = {
        "scenario_id": "line-1",
        "stops": [
            {
                "id": "custom-a",
                "name": "Custom A",
                "coordinates": [-122.36, 47.62],
            },
            {
                "id": "custom-b",
                "name": "Custom B",
                "coordinates": [-122.34, 47.62],
            },
        ],
        "lines": [
            {
                "id": "custom-line",
                "name": "Custom Line",
                "stopIds": ["custom-a", "custom-b"],
            },
        ],
    }
    response = client.post("/api/scenario", json=payload)
    assert response.status_code == 200
    response_payload = response.json()
    assert response_payload["scenario_id"] == "line-1"
    _assert_frame_payload(response_payload["frame"])
    assert [stop.id for stop in server.STATE.active_network.stops] == [
        "custom-a",
        "custom-b",
    ]


@pytest.mark.asyncio
async def test_scenario_post_emits_new_event_on_open_stream() -> None:
    """Per contract ``Scenario change ordering``: when the scenario changes
    while a stream is open, the stream must emit a ``scenario`` event with
    the new id (before any frame from the new scenario)."""
    request = FakeRequest()
    events: list[dict] = []

    async def reader() -> None:
        buffer = ""
        async for chunk in server._stream(request):
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                if block.strip():
                    events.append(_parse_sse_event(block))
                    new_seen = any(
                        e["event"] == "scenario"
                        and e["data"]["scenario_id"] == "line-1-2-ballard"
                        for e in events
                    )
                    if new_seen:
                        request.disconnect()
                        return

    async def changer() -> None:
        # Let the initial config + scenario + frame flow first.
        await asyncio.sleep(0.1)
        server.STATE.set_scenario("line-1-2-ballard")
        await server.STATE.notify_change()

    await asyncio.wait_for(asyncio.gather(reader(), changer()), timeout=5.0)

    scenario_events = [e for e in events if e["event"] == "scenario"]
    assert scenario_events[0]["data"]["scenario_id"] == DEFAULT_SCENARIO_ID
    assert any(
        e["data"]["scenario_id"] == "line-1-2-ballard" for e in scenario_events
    )


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


def test_get_playback_returns_current_sim_time(client: TestClient) -> None:
    response = client.get("/api/playback")
    assert response.status_code == 200
    payload = response.json()
    assert payload["is_playing"] is True
    assert set(payload["sim_time"]) == {"day_of_week", "time_bin", "minute_of_week"}


def test_post_playback_updates_play_state(client: TestClient) -> None:
    response = client.post("/api/playback", json={"is_playing": False})
    assert response.status_code == 200
    assert response.json()["is_playing"] is False
    assert server.STATE.playback.is_playing is False


def test_seek_playback_moves_sim_time(client: TestClient) -> None:
    response = client.post(
        "/api/playback/seek",
        json={"day_of_week": 3, "time_bin": 18 * 60},
    )
    assert response.status_code == 200
    sim_time = response.json()["sim_time"]
    assert sim_time["day_of_week"] == 3
    assert sim_time["time_bin"] == 18 * 60


# ---------------------------------------------------------------------------
# /api/people
# ---------------------------------------------------------------------------


def _seattle_inbounds_point() -> dict:
    bounds = server.GRID.bounds
    return {
        "lat": (bounds.north + bounds.south) / 2,
        "lon": (bounds.east + bounds.west) / 2,
    }


def _cell_for_point(lat: float, lon: float) -> tuple[int, int]:
    bounds = server.GRID.bounds
    cell_w = (bounds.east - bounds.west) / server.GRID.cols
    cell_h = (bounds.north - bounds.south) / server.GRID.rows
    return (
        int((bounds.north - lat) / cell_h),
        int((lon - bounds.west) / cell_w),
    )


def _cell_centers() -> list[tuple[int, int, float, float]]:
    bounds = server.GRID.bounds
    cell_w = (bounds.east - bounds.west) / server.GRID.cols
    cell_h = (bounds.north - bounds.south) / server.GRID.rows
    centers: list[tuple[int, int, float, float]] = []
    for row in range(server.GRID.rows):
        lat = bounds.north - (row + 0.5) * cell_h
        for col in range(server.GRID.cols):
            lon = bounds.west + (col + 0.5) * cell_w
            centers.append((row, col, lat, lon))
    return centers


def _mean_density_near(
    cells: list[list[int | float]],
    *,
    lat: float,
    lon: float,
    radius_m: float,
) -> float:
    by_cell = {(int(row), int(col)): float(density) for row, col, density in cells}
    values = [
        by_cell.get((row, col), 0.0)
        for row, col, cell_lat, cell_lon in _cell_centers()
        if haversine_m(lat, lon, cell_lat, cell_lon) <= radius_m
    ]
    assert values, "test location should cover at least one grid cell"
    return sum(values) / len(values)


def _mean_frame_density(cells: list[list[int | float]]) -> float:
    total = sum(float(density) for _, _, density in cells)
    return total / (server.GRID.rows * server.GRID.cols)


def _total_served_near(
    service: ServiceCapacity,
    demand: list[float],
    *,
    lat: float,
    lon: float,
    radius_m: float,
) -> float:
    """Trips/hour actually absorbed by transit within a radius."""
    served = service.allocate(demand)
    values = [
        served[idx]
        for idx, center in enumerate(service.centers)
        if haversine_m(lat, lon, center.lat, center.lon) <= radius_m
    ]
    assert values, "test location should cover at least one grid cell"
    return sum(values)


def _mean_abs_frame_delta(
    left: list[list[int | float]],
    right: list[list[int | float]],
) -> float:
    left_by_cell = {(int(row), int(col)): float(density) for row, col, density in left}
    right_by_cell = {(int(row), int(col)): float(density) for row, col, density in right}
    total = 0.0
    count = server.GRID.rows * server.GRID.cols
    for row in range(server.GRID.rows):
        for col in range(server.GRID.cols):
            total += abs(left_by_cell.get((row, col), 0.0) - right_by_cell.get((row, col), 0.0))
    return total / count


def test_post_people_returns_201_and_persists(client: TestClient) -> None:
    body = {**_seattle_inbounds_point(), "count": 25}
    response = client.post("/api/people", json=body)
    assert response.status_code == 201

    payload = response.json()
    assert set(payload) == {"id", "lat", "lon", "count"}
    assert payload["lat"] == body["lat"]
    assert payload["lon"] == body["lon"]
    assert payload["count"] == 25
    assert isinstance(payload["id"], str) and payload["id"]

    listing = client.get("/api/people").json()
    assert listing == {"people": [payload]}


def test_post_people_defaults_count_to_one(client: TestClient) -> None:
    response = client.post("/api/people", json=_seattle_inbounds_point())
    assert response.status_code == 201
    assert response.json()["count"] == 1


def test_post_people_accepts_visual_tuning_fields(client: TestClient) -> None:
    body = {
        **_seattle_inbounds_point(),
        "count": 10_000,
        "kind": "stadium",
        "duration_minutes": 240,
        "radius_m": 3200,
    }
    response = client.post("/api/people", json=body)
    assert response.status_code == 201
    payload = response.json()
    assert payload["kind"] == "stadium"
    assert payload["duration_minutes"] == 240
    assert payload["radius_m"] == 3200


def test_post_people_rejects_out_of_bounds(client: TestClient) -> None:
    response = client.post("/api/people", json={"lat": 0.0, "lon": 0.0, "count": 1})
    assert response.status_code == 400


def test_post_people_rejects_missing_coordinates(client: TestClient) -> None:
    response = client.post("/api/people", json={"count": 1})
    assert response.status_code == 400


def test_delete_single_person_returns_204(client: TestClient) -> None:
    created = client.post("/api/people", json=_seattle_inbounds_point()).json()
    response = client.delete(f"/api/people/{created['id']}")
    assert response.status_code == 204
    assert client.get("/api/people").json() == {"people": []}


def test_delete_unknown_person_returns_404(client: TestClient) -> None:
    response = client.delete("/api/people/p_does_not_exist")
    assert response.status_code == 404


def test_delete_all_people_returns_204_and_clears(client: TestClient) -> None:
    point = _seattle_inbounds_point()
    client.post("/api/people", json=point)
    client.post("/api/people", json=point)
    assert len(client.get("/api/people").json()["people"]) == 2

    response = client.delete("/api/people")
    assert response.status_code == 204
    assert client.get("/api/people").json() == {"people": []}


def test_added_person_boosts_density_in_their_cell() -> None:
    """The data path the frontend renders: a placed person must show up in
    the next composed frame at their cell's [row, col]."""
    point = _seattle_inbounds_point()
    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=8 * 60)
    baseline_cells = {(r, c): d for r, c, d in server.STATE.compose_frame_cells()}

    # The UI drops crowds in the thousands; ten people do not move a city.
    person = server.STATE.add_person(lat=point["lat"], lon=point["lon"], count=5_000)

    bounds = server.GRID.bounds
    cell_w = (bounds.east - bounds.west) / server.GRID.cols
    cell_h = (bounds.north - bounds.south) / server.GRID.rows
    expected_col = int((person.lon - bounds.west) / cell_w)
    expected_row = int((bounds.north - person.lat) / cell_h)

    after = {(r, c): d for r, c, d in server.STATE.compose_frame_cells()}
    boosted = after[(expected_row, expected_col)]
    baseline = baseline_cells.get((expected_row, expected_col), 0.0)
    assert boosted > baseline
    assert boosted <= 1.0


# ---------------------------------------------------------------------------
# Visual simulation behavior
# ---------------------------------------------------------------------------


def test_ballard_deployment_cools_new_station_catchment() -> None:
    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=8 * 60)
    server.STATE.set_scenario("line-1")
    baseline = server.STATE.compose_frame_cells()

    server.STATE.set_scenario("line-1-2-ballard")
    expanded = server.STATE.compose_frame_cells()

    baseline_ballard = _mean_density_near(
        baseline,
        lat=47.6677,
        lon=-122.3765,
        radius_m=1000,
    )
    expanded_ballard = _mean_density_near(
        expanded,
        lat=47.6677,
        lon=-122.3765,
        radius_m=1000,
    )
    assert expanded_ballard < baseline_ballard * 0.75


def test_line_2_doubles_shared_station_throughput() -> None:
    line_1 = ServiceCapacity(server.GRID, default_network_for_scenario("line-1"))
    line_1_2 = ServiceCapacity(server.GRID, default_network_for_scenario("line-1-2"))

    assert line_1.line_count_by_stop_id["westlake"] == 1
    assert line_1_2.line_count_by_stop_id["westlake"] == 2
    assert line_1_2.line_count_by_stop_id["u-district"] == 2
    assert line_1_2.line_count_by_stop_id["judkins-park"] == 1

    # A second line calling at Westlake doubles the trips/hour it can absorb.
    assert line_1_2.capacity_by_stop_id["westlake"] == pytest.approx(
        line_1.capacity_by_stop_id["westlake"] * 2
    )

    # And that capacity is actually taken up, because downtown demand at the
    # AM peak far exceeds what one line can carry.
    server.STATE.seek_playback(day_of_week=3, time_bin=8 * 60)
    demand = server.STATE.demand_field.trips_per_hour(server.STATE.playback.current_time)

    line_1_served = _total_served_near(
        line_1, demand, lat=47.6113, lon=-122.3371, radius_m=1000
    )
    line_1_2_served = _total_served_near(
        line_1_2, demand, lat=47.6113, lon=-122.3371, radius_m=1000
    )
    assert line_1_2_served > line_1_served * 1.2


def test_more_lines_progressively_lower_citywide_demand() -> None:
    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=8 * 60)

    mean_by_scenario: dict[str, float] = {}
    for scenario_id in ("line-1", "line-1-2", "line-1-2-ballard"):
        server.STATE.set_scenario(scenario_id)
        mean_by_scenario[scenario_id] = _mean_frame_density(
            server.STATE.compose_frame_cells()
        )

    assert mean_by_scenario["line-1-2"] < mean_by_scenario["line-1"] * 0.96
    assert (
        mean_by_scenario["line-1-2-ballard"]
        < mean_by_scenario["line-1-2"] * 0.96
    )


def test_underserved_areas_remain_hotter_than_served_catchments() -> None:
    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=8 * 60)
    server.STATE.set_scenario("line-1-2-ballard")
    cells = server.STATE.compose_frame_cells()

    served_ballard = _mean_density_near(
        cells,
        lat=47.6677,
        lon=-122.3765,
        radius_m=1000,
    )
    underserved_lake_city = _mean_density_near(
        cells,
        lat=47.7192,
        lon=-122.2950,
        radius_m=1000,
    )
    underserved_magnolia = _mean_density_near(
        cells,
        lat=47.6465,
        lon=-122.3996,
        radius_m=1000,
    )
    # Neither Lake City nor Magnolia gets a station in any scenario, so both
    # stay hot once Ballard's catchment is being served.
    assert underserved_lake_city > served_ballard * 2
    assert underserved_magnolia > served_ballard * 2

    # Rainier Beach is the opposite case: low demand against a full station's
    # capacity, so it is absorbed almost entirely.
    served_rainier_beach = _mean_density_near(
        cells,
        lat=47.5222,
        lon=-122.2688,
        radius_m=1000,
    )
    assert served_rainier_beach < 0.02


def test_crowd_drop_spikes_then_decays() -> None:
    lat = 47.6060
    lon = -122.3330
    row, col = _cell_for_point(lat, lon)

    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=12 * 60)
    server.STATE.set_scenario("line-1")
    baseline = {(r, c): d for r, c, d in server.STATE.compose_frame_cells()}

    server.STATE.add_person(
        lat=lat,
        lon=lon,
        count=10_000,
        duration_minutes=240,
        radius_m=2800,
    )
    immediate = {(r, c): d for r, c, d in server.STATE.compose_frame_cells()}

    server.STATE.seek_playback(day_of_week=2, time_bin=14 * 60)
    later = {(r, c): d for r, c, d in server.STATE.compose_frame_cells()}

    assert immediate[(row, col)] > baseline.get((row, col), 0.0) + 0.10
    assert later[(row, col)] < immediate[(row, col)]


def test_crowd_near_a_station_is_absorbed_more_than_one_that_is_not() -> None:
    """Crowds contribute trips, so transit capacity acts on them the same way
    it acts on resident and job demand -- with no special-casing."""
    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=22 * 60)
    server.STATE.set_scenario("line-1")

    # Rainier Beach station has spare capacity late at night; Magnolia has
    # no station at all.
    served_point = (47.5222, -122.2688)
    unserved_point = (47.6465, -122.3996)

    boosts = []
    for lat, lon in (served_point, unserved_point):
        server.STATE.clear_people()
        before = _mean_density_near(
            server.STATE.compose_frame_cells(), lat=lat, lon=lon, radius_m=1200
        )
        server.STATE.add_person(
            lat=lat, lon=lon, count=6_000, duration_minutes=240, radius_m=1500
        )
        after = _mean_density_near(
            server.STATE.compose_frame_cells(), lat=lat, lon=lon, radius_m=1200
        )
        boosts.append(after - before)

    served_boost, unserved_boost = boosts
    assert unserved_boost > served_boost


def test_crowd_drop_scale_tracks_people_count() -> None:
    lat = 47.6060
    lon = -122.3330

    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=12 * 60)
    server.STATE.set_scenario("line-1")
    baseline = server.STATE.compose_frame_cells()
    baseline_near = _mean_density_near(
        baseline,
        lat=lat,
        lon=lon,
        radius_m=1400,
    )

    server.STATE.add_person(
        lat=lat,
        lon=lon,
        count=5_000 // 12,
        duration_minutes=240,
    )
    low_drop = server.STATE.compose_frame_cells()
    low_boost = (
        _mean_density_near(low_drop, lat=lat, lon=lon, radius_m=1400)
        - baseline_near
    )

    server.STATE.clear_people()
    server.STATE.add_person(
        lat=lat,
        lon=lon,
        count=30_000 // 12,
        duration_minutes=240,
    )
    high_drop = server.STATE.compose_frame_cells()
    high_boost = (
        _mean_density_near(high_drop, lat=lat, lon=lon, radius_m=1400)
        - baseline_near
    )

    # Trips scale linearly with head count, so a 6x crowd lifts the area
    # substantially more than the small one.
    assert high_boost > max(0.02, low_boost * 2.5)


def test_time_profiles_produce_distinct_hotspot_distributions() -> None:
    server.STATE.playback.set_playing(False)
    server.STATE.set_scenario("line-1")

    frames: dict[str, list[list[int | float]]] = {}
    for name, day, time_bin in [
        ("am", 2, 8 * 60),
        ("midday", 2, 12 * 60),
        ("pm", 2, 18 * 60),
        ("evening", 2, 20 * 60),
        ("weekend", 6, 13 * 60),
    ]:
        server.STATE.seek_playback(day_of_week=day, time_bin=time_bin)
        frames[name] = server.STATE.compose_frame_cells()

    assert _mean_abs_frame_delta(frames["am"], frames["midday"]) > 0.010
    assert _mean_abs_frame_delta(frames["midday"], frames["pm"]) > 0.007
    assert _mean_abs_frame_delta(frames["pm"], frames["evening"]) > 0.020
    assert _mean_abs_frame_delta(frames["midday"], frames["weekend"]) > 0.004


# ---------------------------------------------------------------------------
# Land-use model invariants
# ---------------------------------------------------------------------------


def test_land_use_conserves_district_population() -> None:
    """District totals are spread over cells with normalized weights, so the
    per-cell counts must sum back to exactly what the table declares. This is
    what makes the numbers in SEATTLE_DISTRICTS auditable against real data."""
    land_use = server.STATE.land_use

    for field in ("residents", "jobs", "students", "visitors"):
        from_cells = sum(getattr(cell, field) for cell in land_use)
        from_districts = sum(getattr(d, field) for d in SEATTLE_DISTRICTS)
        assert from_cells == pytest.approx(from_districts, rel=1e-9)


def test_water_cells_hold_nobody_and_never_render() -> None:
    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=8 * 60)
    cells = {(r, c) for r, c, _ in server.STATE.compose_frame_cells()}

    water_indices = [
        idx
        for idx, center in enumerate(server.STATE.centers)
        if is_probable_water(center.lat, center.lon)
    ]
    assert water_indices, "expected the water mask to cover part of the grid"

    for idx in water_indices:
        cell = server.STATE.land_use[idx]
        assert cell.residents == 0.0
        assert cell.jobs == 0.0
        assert cell.students == 0.0
        assert cell.visitors == 0.0
        center = server.STATE.centers[idx]
        assert (center.row, center.col) not in cells


def test_no_cell_is_served_more_trips_than_it_generates() -> None:
    """Capacity allocation must never manufacture ridership out of thin air."""
    server.STATE.playback.set_playing(False)
    for scenario_id in sorted(VALID_SCENARIO_IDS):
        server.STATE.set_scenario(scenario_id)
        for time_bin in (3 * 60, 8 * 60, 12 * 60, 17 * 60, 22 * 60):
            server.STATE.seek_playback(day_of_week=2, time_bin=time_bin)
            demand = server.STATE.demand_field.trips_per_hour(
                server.STATE.playback.current_time
            )
            served = server.STATE.service.allocate(demand)
            assert len(served) == len(demand)
            for want, got in zip(demand, served):
                assert got <= want + 1e-9
                assert got >= 0.0


def test_more_service_absorbs_strictly_more_trips() -> None:
    server.STATE.playback.set_playing(False)
    server.STATE.seek_playback(day_of_week=2, time_bin=8 * 60)
    demand = server.STATE.demand_field.trips_per_hour(
        server.STATE.playback.current_time
    )

    totals = []
    for scenario_id in ("line-1", "line-1-2", "line-1-2-ballard"):
        service = ServiceCapacity(server.GRID, default_network_for_scenario(scenario_id))
        totals.append(sum(service.allocate(demand)))

    assert totals[0] < totals[1] < totals[2]


def test_trip_rates_interpolate_smoothly_across_the_hour() -> None:
    """Rates are interpolated between adjacent hours, so scrubbing the dial
    must not step. Consecutive 30-minute bins should differ only gradually."""
    server.STATE.playback.set_playing(False)
    server.STATE.set_scenario("line-1")

    means = []
    for minute in range(6 * 60, 11 * 60, 30):
        server.STATE.seek_playback(day_of_week=2, time_bin=minute)
        means.append(_mean_frame_density(server.STATE.compose_frame_cells()))

    steps = [abs(b - a) for a, b in zip(means, means[1:])]
    assert max(steps) < 0.035, f"demand jumps between adjacent bins: {steps}"


# ---------------------------------------------------------------------------
# The simulation clock has exactly one producer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_streams_never_advance_the_simulation_clock() -> None:
    """A connection is a reader.

    The clock used to be advanced from inside the per-connection generator, so
    two open tabs ran the simulation at twice the declared rate -- which also
    breaks the contract's requirement that ``sim_minutes_per_second x
    frame_interval_seconds`` equal the real advance in ``minute_of_week``.
    """
    server.STATE.playback.seek(day_of_week=2, time_bin=9 * 60)
    minute_before = server.STATE.playback.current_time.minute_of_week
    tick_before = server.STATE.playback.current_tick

    await asyncio.gather(
        drive_stream(FakeRequest(), n=6),
        drive_stream(FakeRequest(), n=6),
    )

    assert server.STATE.playback.current_tick == tick_before
    assert server.STATE.playback.current_time.minute_of_week == minute_before


@pytest.mark.asyncio
async def test_frame_ticker_holds_the_clock_while_nobody_is_connected() -> None:
    tick_before = server.STATE.playback.current_tick
    ticker = asyncio.create_task(server._frame_ticker())
    try:
        await asyncio.sleep(server.FRAME_INTERVAL_S * 4)
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker

    assert server.STATE.subscriber_count == 0
    assert server.STATE.playback.current_tick == tick_before


@pytest.mark.asyncio
async def test_frame_ticker_advances_the_clock_for_connected_readers() -> None:
    server.STATE.add_subscriber()
    tick_before = server.STATE.playback.current_tick
    ticker = asyncio.create_task(server._frame_ticker())
    try:
        await asyncio.sleep(server.FRAME_INTERVAL_S * 4)
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        server.STATE.remove_subscriber()

    assert server.STATE.playback.current_tick > tick_before


@pytest.mark.asyncio
async def test_all_readers_share_one_composed_frame() -> None:
    """Frames are composed once per tick, not once per client."""
    frame_a = server.STATE.current_frame()
    frame_b = server.STATE.current_frame()
    assert frame_a is frame_b

    server.STATE.tick()
    assert server.STATE.current_frame() is not frame_a


# ---------------------------------------------------------------------------
# Playback speed
# ---------------------------------------------------------------------------


def test_playback_speed_change_preserves_the_clock(client: TestClient) -> None:
    """Changing speed must re-scale how fast time passes, not where it points."""
    client.post("/api/playback/seek", json={"day_of_week": 3, "time_bin": 18 * 60})
    baseline = client.get("/api/playback").json()["sim_time"]["minute_of_week"]

    for speed in (60.0, 5.0, 240.0, 0.5):
        response = client.post(
            "/api/playback",
            json={"sim_minutes_per_second": speed},
        )
        assert response.status_code == 200
        assert response.json()["sim_time"]["minute_of_week"] == baseline


def test_playback_declared_rate_matches_the_real_advance(client: TestClient) -> None:
    """The contract's train interpolator resyncs -- visibly -- on a mismatch."""
    client.post("/api/playback/seek", json={"day_of_week": 1, "time_bin": 8 * 60})

    for speed in (60.0, 240.0, 0.5):
        payload = client.post(
            "/api/playback",
            json={"sim_minutes_per_second": speed},
        ).json()

        before = server.STATE.playback.current_time.minute_of_week
        server.STATE.playback.advance()
        after = server.STATE.playback.current_time.minute_of_week
        observed = (after - before) % MINUTES_PER_WEEK

        expected = payload["sim_minutes_per_second"] * payload["frame_interval_seconds"]
        assert observed == pytest.approx(expected)


def test_post_playback_rejects_a_non_positive_speed(client: TestClient) -> None:
    assert client.post("/api/playback", json={"sim_minutes_per_second": 0}).status_code == 400
    assert client.post("/api/playback", json={"sim_minutes_per_second": -5}).status_code == 400


# ---------------------------------------------------------------------------
# Malformed scenario payloads (the contract requires tolerating these)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stops, lines",
    [
        (None, None),
        ("not-a-list", "not-a-list"),
        ([], []),
        ([{"id": "a", "coordinates": [-122.33, 47.60]}], []),
        ([{"no_id": True}], [{"id": "l", "stopIds": ["a"]}]),
        ([{"id": "a", "coordinates": [-122.33]}], [{"id": "l", "stopIds": ["a"]}]),
        (
            [{"id": "a", "coordinates": [-122.33, 47.60]}],
            [{"id": "l", "stopIds": ["a"], "path": [[-122.33, 47.60]]}],
        ),
    ],
)
def test_parse_network_payload_falls_back_on_malformed_input(stops, lines) -> None:
    from backend.network import parse_network_payload

    network = parse_network_payload(
        scenario_id="line-1",
        stops_payload=stops,
        lines_payload=lines,
    )
    assert network == default_network_for_scenario("line-1")


def test_parse_network_payload_derives_a_path_from_stop_ids() -> None:
    from backend.network import parse_network_payload

    network = parse_network_payload(
        scenario_id="line-1",
        stops_payload=[
            {"id": "a", "coordinates": [-122.33, 47.60], "color": "#fff"},
            {"id": "b", "coordinates": [-122.34, 47.61], "offset": 2},
        ],
        lines_payload=[{"id": "l", "stopIds": ["a", "b"]}],
    )
    assert [stop.id for stop in network.stops] == ["a", "b"]
    assert network.lines[0].path == ((-122.33, 47.60), (-122.34, 47.61))


def test_post_scenario_tolerates_malformed_stops_and_lines(client: TestClient) -> None:
    response = client.post(
        "/api/scenario",
        json={"scenario_id": "line-1-2", "stops": "nope", "lines": 17},
    )
    assert response.status_code == 200
    assert response.json()["frame"] is not None


# ---------------------------------------------------------------------------
# Overlay ageing across the week boundary
# ---------------------------------------------------------------------------


def test_overlay_age_wraps_across_the_end_of_the_week() -> None:
    from backend.overlays import CrowdOverlay, overlay_age_minutes
    from backend.sim_time import sim_time_for_second

    # Dropped Saturday 23:00, read Sunday 01:00 -- two hours old, not a week.
    created = 6 * MINUTES_PER_DAY + 23 * 60
    overlay = CrowdOverlay(
        id="p_test",
        kind="crowd",
        lat=47.6,
        lon=-122.33,
        count=100,
        created_at_real_time=0.0,
        created_at_minute=created,
        duration_minutes=240,
        radius_m=1000.0,
        decay_m=400.0,
    )
    later = sim_time_for_second(60 * 60 * 1)  # Sunday 01:00 of the next week
    assert overlay_age_minutes(overlay, later) == 120


# ---------------------------------------------------------------------------
# Grid loader edge cases (synthetic sources, not the shipped file)
# ---------------------------------------------------------------------------


def _square_cell(west: float, south: float, size: float) -> dict:
    east, north = west + size, south + size
    return {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }


def _grid_geojson(
    rows: int,
    cols: int,
    *,
    row_zero_is_north: bool,
    size: float = 0.01,
    west: float = -122.5,
    south: float = 47.5,
    partial_last_col: bool = False,
) -> dict:
    features = []
    for row in range(rows):
        for col in range(cols):
            offset = (rows - 1 - row) if row_zero_is_north else row
            cell_size = size
            if partial_last_col and col == cols - 1:
                cell_size = size / 2
            features.append(
                {
                    "type": "Feature",
                    "properties": {"cell_id": f"r{row}_c{col}"},
                    "geometry": _square_cell(
                        west + col * size,
                        south + offset * size,
                        cell_size,
                    ),
                }
            )
    return {"type": "FeatureCollection", "features": features}


def _write_grid(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "grid.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("row_zero_is_north", [True, False])
def test_load_grid_normalizes_row_orientation(
    tmp_path: Path,
    row_zero_is_north: bool,
) -> None:
    from backend.grid import load_grid

    path = _write_grid(
        tmp_path,
        _grid_geojson(3, 2, row_zero_is_north=row_zero_is_north),
    )
    grid = load_grid(path)

    assert (grid.rows, grid.cols) == (3, 2)
    # Row 0 is the northernmost strip regardless of how the source indexed it.
    assert grid.bounds.north == pytest.approx(47.53)
    assert grid.bounds.south == pytest.approx(47.50)
    assert grid.bounds.west == pytest.approx(-122.50)
    assert grid.bounds.east == pytest.approx(-122.48)


def test_load_grid_drops_partial_edge_columns(tmp_path: Path) -> None:
    from backend.grid import load_grid

    path = _write_grid(
        tmp_path,
        _grid_geojson(3, 3, row_zero_is_north=True, partial_last_col=True),
    )
    grid = load_grid(path)
    assert (grid.rows, grid.cols) == (3, 2)


def test_load_grid_rejects_an_empty_feature_collection(tmp_path: Path) -> None:
    from backend.grid import load_grid

    path = _write_grid(tmp_path, {"type": "FeatureCollection", "features": []})
    with pytest.raises(ValueError, match="No features"):
        load_grid(path)


def test_load_grid_rejects_a_source_missing_cell_zero(tmp_path: Path) -> None:
    from backend.grid import load_grid

    payload = _grid_geojson(3, 2, row_zero_is_north=True)
    payload["features"] = [
        f for f in payload["features"] if f["properties"]["cell_id"] != "r0_c0"
    ]
    path = _write_grid(tmp_path, payload)
    with pytest.raises(ValueError, match="missing cell"):
        load_grid(path)


def test_load_grid_rejects_a_malformed_cell_id(tmp_path: Path) -> None:
    from backend.grid import load_grid

    payload = _grid_geojson(2, 2, row_zero_is_north=True)
    payload["features"][0]["properties"]["cell_id"] = "not-a-cell"
    path = _write_grid(tmp_path, payload)
    with pytest.raises(ValueError, match="Unexpected cell_id"):
        load_grid(path)
