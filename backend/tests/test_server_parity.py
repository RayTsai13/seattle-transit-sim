"""Endpoint-level contract checks run against *both* implementations.

``docs/heatmap-api-contract.md`` declares that ``backend/server.py`` and
``mock/server.py`` both satisfy the same wire contract, but only the real
backend was ever tested. Everything in this module is parametrized over both, so
the mock cannot silently drift again.

Anything genuinely specific to the simulation (land use, capacity allocation,
crowd physics) belongs in ``test_contract.py``, not here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from backend import server as real_server
from mock import server as mock_server


SERVERS = {"backend": real_server, "mock": mock_server}


@pytest.fixture(params=sorted(SERVERS), ids=sorted(SERVERS))
def impl(request: pytest.FixtureRequest):
    """One of the two servers, reset to a clean state."""
    module = SERVERS[request.param]
    _reset(module)
    yield module
    _reset(module)


@pytest.fixture
def client(impl) -> Iterator[TestClient]:
    with TestClient(impl.app) as test_client:
        yield test_client


def _reset(module) -> None:
    if module is real_server:
        module.STATE = module.State(
            module.GRID,
            frame_interval_seconds=0.02,
        )
        module.FRAME_INTERVAL_S = 0.02
    else:
        module.STATE = module.MockState()
        module.PLAYBACK = module.MockPlaybackController()


def _bounds(impl) -> dict[str, float]:
    if impl is real_server:
        return impl.GRID.bounds.to_dict()
    return dict(impl.GRID_CONFIG["bounds"])


def _inbounds_point(impl) -> dict[str, float]:
    bounds = _bounds(impl)
    return {
        "lat": (bounds["north"] + bounds["south"]) / 2,
        "lon": (bounds["east"] + bounds["west"]) / 2,
    }


class FakeRequest:
    def __init__(self) -> None:
        self._disconnected = False

    def disconnect(self) -> None:
        self._disconnected = True

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _parse_sse_event(block: str) -> dict:
    out: dict = {}
    for raw_line in block.splitlines():
        if raw_line.startswith("id:"):
            out["id"] = int(raw_line[3:].strip())
        elif raw_line.startswith("event:"):
            out["event"] = raw_line[6:].strip()
        elif raw_line.startswith("data:"):
            out["data"] = json.loads(raw_line[5:].strip())
    return out


async def _drive(impl, request: FakeRequest, n: int, timeout: float = 5.0) -> list[dict]:
    async def _collect() -> list[dict]:
        events: list[dict] = []
        buffer = ""
        async for chunk in impl._stream(request):
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
# Handshake and frame shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handshake_is_config_scenario_playback_then_frame(impl) -> None:
    events = await _drive(impl, FakeRequest(), n=4)
    assert [e["event"] for e in events] == ["config", "scenario", "playback", "frame"]
    assert [e["id"] for e in events] == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_config_payload_shape(impl) -> None:
    config = (await _drive(impl, FakeRequest(), n=1))[0]["data"]
    assert set(config) == {"bounds", "rows", "cols"}
    assert set(config["bounds"]) == {"west", "south", "east", "north"}
    assert config["rows"] > 0 and config["cols"] > 0


@pytest.mark.asyncio
async def test_frame_payload_shape_and_bounds(impl) -> None:
    events = await _drive(impl, FakeRequest(), n=4)
    config = events[0]["data"]
    frame = events[3]["data"]

    assert set(frame) == {"timestamp", "state_version", "sim_time", "cells"}
    assert isinstance(frame["timestamp"], (int, float))
    assert frame["state_version"].startswith("state_v")
    assert set(frame["sim_time"]) == {"day_of_week", "time_bin", "minute_of_week"}

    for cell in frame["cells"]:
        row, col, density = cell
        assert 0 <= row < config["rows"]
        assert 0 <= col < config["cols"]
        assert 0.0 < float(density) <= 1.0


@pytest.mark.asyncio
async def test_streams_do_not_advance_the_clock(impl) -> None:
    """Two open tabs must not run the simulation at twice the declared rate."""
    playback = impl.STATE.playback if impl is real_server else impl.PLAYBACK
    before = playback.current_tick

    await asyncio.gather(
        _drive(impl, FakeRequest(), n=6),
        _drive(impl, FakeRequest(), n=6),
    )

    assert playback.current_tick == before


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", ["line-1", "line-1-2", "line-1-2-ballard"])
def test_post_scenario_accepts_documented_ids(client, scenario_id) -> None:
    response = client.post("/api/scenario", json={"scenario_id": scenario_id})
    assert response.status_code == 200
    payload = response.json()
    assert payload["scenario_id"] == scenario_id
    # A null frame here freezes the frontend's scenario gate.
    assert payload["frame"] is not None
    assert "cells" in payload["frame"]


def test_post_scenario_rejects_unknown_id(client) -> None:
    assert client.post("/api/scenario", json={"scenario_id": "nope"}).status_code == 400


def test_post_scenario_rejects_missing_field(client) -> None:
    assert client.post("/api/scenario", json={}).status_code == 400


def test_post_scenario_tolerates_extra_keys(client) -> None:
    """The frontend sends `color` and `offset` on stops and lines."""
    response = client.post(
        "/api/scenario",
        json={
            "scenario_id": "line-1-2",
            "stops": [
                {
                    "id": "a",
                    "name": "A",
                    "coordinates": [-122.33, 47.60],
                    "color": "#ff0000",
                }
            ],
            "lines": [
                {
                    "id": "l",
                    "name": "L",
                    "stopIds": ["a"],
                    "path": [[-122.33, 47.60], [-122.34, 47.61]],
                    "offset": 2,
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["frame"] is not None


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


PLAYBACK_KEYS = {
    "is_playing",
    "current_tick",
    "sim_step_seconds",
    "sim_minutes_per_second",
    "frame_interval_seconds",
    "time_bin_minutes",
    "sim_time",
}


def test_get_playback_returns_the_full_state(client) -> None:
    payload = client.get("/api/playback").json()
    assert set(payload) == PLAYBACK_KEYS


def test_post_playback_returns_the_full_state(client) -> None:
    payload = client.post("/api/playback", json={"is_playing": False}).json()
    assert set(payload) == PLAYBACK_KEYS
    assert payload["is_playing"] is False


def test_seek_returns_the_full_state(client) -> None:
    payload = client.post(
        "/api/playback/seek",
        json={"day_of_week": 3, "time_bin": 18 * 60},
    ).json()
    assert set(payload) == PLAYBACK_KEYS
    assert payload["sim_time"]["day_of_week"] == 3
    assert payload["sim_time"]["time_bin"] == 18 * 60


def test_seek_accepts_minute_of_week(client) -> None:
    payload = client.post("/api/playback/seek", json={"minute_of_week": 4000}).json()
    assert payload["sim_time"]["minute_of_week"] == 4000


@pytest.mark.parametrize("speed", [0.005, 0.5, 1.0, 60.0, 240.0])
def test_a_small_playback_speed_never_breaks_seek(client, speed) -> None:
    """sim_step_seconds of 0 used to make the next seek raise a 500."""
    assert client.post(
        "/api/playback",
        json={"sim_minutes_per_second": speed},
    ).status_code == 200
    assert client.post(
        "/api/playback/seek",
        json={"day_of_week": 2, "time_bin": 600},
    ).status_code == 200


def test_playback_declared_rate_matches_the_real_advance(client, impl) -> None:
    """The frontend's train interpolator hard-resyncs on a mismatch."""
    playback = impl.STATE.playback if impl is real_server else impl.PLAYBACK

    for speed in (60.0, 240.0, 0.5):
        payload = client.post(
            "/api/playback",
            json={"sim_minutes_per_second": speed},
        ).json()

        before = playback.current_time.minute_of_week
        playback.advance()
        after = playback.current_time.minute_of_week
        observed = (after - before) % (7 * 24 * 60)

        expected = payload["sim_minutes_per_second"] * payload["frame_interval_seconds"]
        assert observed == pytest.approx(expected)


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


def test_post_people_returns_201_and_persists(client, impl) -> None:
    response = client.post("/api/people", json={**_inbounds_point(impl), "count": 25})
    assert response.status_code == 201
    person = response.json()
    assert set(person) == {"id", "lat", "lon", "count"}
    assert person["count"] == 25

    listed = client.get("/api/people").json()["people"]
    assert [p["id"] for p in listed] == [person["id"]]


def test_post_people_defaults_count_to_one(client, impl) -> None:
    assert client.post("/api/people", json=_inbounds_point(impl)).json()["count"] == 1


def test_post_people_echoes_tuning_fields_when_sent(client, impl) -> None:
    """Required by the contract; the mock used to drop these silently."""
    response = client.post(
        "/api/people",
        json={
            **_inbounds_point(impl),
            "count": 5000,
            "kind": "crowd",
            "duration_minutes": 240,
            "radius_m": 1800.0,
            "decay_m": 700.0,
        },
    )
    assert response.status_code == 201
    person = response.json()
    assert person["kind"] == "crowd"
    assert person["duration_minutes"] == 240
    assert person["radius_m"] == pytest.approx(1800.0)
    assert person["decay_m"] == pytest.approx(700.0)


def test_post_people_rejects_out_of_bounds(client, impl) -> None:
    bounds = _bounds(impl)
    response = client.post(
        "/api/people",
        json={"lat": bounds["north"] + 5, "lon": bounds["east"] + 5},
    )
    assert response.status_code == 400


def test_post_people_rejects_missing_coordinates(client) -> None:
    assert client.post("/api/people", json={"count": 3}).status_code == 400


def test_people_delete_lifecycle(client, impl) -> None:
    person = client.post("/api/people", json=_inbounds_point(impl)).json()
    assert client.delete(f"/api/people/{person['id']}").status_code == 204
    assert client.delete("/api/people/not-a-real-id").status_code == 404

    client.post("/api/people", json=_inbounds_point(impl))
    assert client.delete("/api/people").status_code == 204
    assert client.get("/api/people").json()["people"] == []


def test_people_burst_of_twelve_is_handled(client, impl) -> None:
    """The UI drops a crowd as 12 concurrent POSTs."""
    point = _inbounds_point(impl)
    ids = {
        client.post("/api/people", json={**point, "count": 416}).json()["id"]
        for _ in range(12)
    }
    assert len(ids) == 12
    assert len(client.get("/api/people").json()["people"]) == 12
