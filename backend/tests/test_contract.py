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
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import server
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
    server.STATE = State(server.GRID)
    yield
    server.STATE = State(server.GRID)


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
async def test_sse_initial_handshake_is_config_then_scenario_then_frame() -> None:
    """Per contract "Initial state on connect": config (once), scenario, frame."""
    events = await drive_stream(FakeRequest(), n=3)
    assert [e["event"] for e in events] == ["config", "scenario", "frame"]
    assert [e["id"] for e in events] == [0, 1, 2]


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
    events = await drive_stream(FakeRequest(), n=3)
    frame = events[2]
    assert frame["event"] == "frame"

    payload = frame["data"]
    assert set(payload) == {"timestamp", "cells"}
    assert isinstance(payload["timestamp"], (int, float))
    assert isinstance(payload["cells"], list)

    rows, cols = server.GRID.rows, server.GRID.cols
    for cell in payload["cells"]:
        assert isinstance(cell, list) and len(cell) == 3
        row, col, density = cell
        assert isinstance(row, int) and 0 <= row < rows
        assert isinstance(col, int) and 0 <= col < cols
        assert isinstance(density, (int, float))
        assert 0.0 <= float(density) <= 1.0


@pytest.mark.asyncio
async def test_frame_cells_are_sparse_no_zero_density() -> None:
    """Contract: cells with density == 0 may be omitted; we omit them."""
    events = await drive_stream(FakeRequest(), n=3)
    cells = events[2]["data"]["cells"]
    assert all(density > 0 for _, _, density in cells)


@pytest.mark.asyncio
async def test_frame_density_reaches_unit_after_normalization() -> None:
    """At least one cell should hit the top of the [0, 1] range."""
    events = await drive_stream(FakeRequest(), n=3)
    cells = events[2]["data"]["cells"]
    assert cells, "expected at least one nonzero cell from the source GeoJSON"
    assert max(density for _, _, density in cells) == pytest.approx(1.0)


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
    assert response.json() == {"scenario_id": scenario_id}
    assert server.STATE.scenario_id == scenario_id


def test_post_scenario_rejects_unknown_id(client: TestClient) -> None:
    response = client.post("/api/scenario", json={"scenario_id": "made-up"})
    assert response.status_code == 400


def test_post_scenario_rejects_missing_field(client: TestClient) -> None:
    response = client.post("/api/scenario", json={})
    assert response.status_code == 400


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
# /api/people
# ---------------------------------------------------------------------------


def _seattle_inbounds_point() -> dict:
    bounds = server.GRID.bounds
    return {
        "lat": (bounds.north + bounds.south) / 2,
        "lon": (bounds.east + bounds.west) / 2,
    }


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
    baseline_cells = {(r, c): d for r, c, d in server.STATE.compose_frame_cells()}

    person = server.STATE.add_person(lat=point["lat"], lon=point["lon"], count=10)

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
