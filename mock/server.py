"""
Mock heatmap intermediary for frontend development.

Implements the same HTTP + SSE contract as ``backend/server.py`` while keeping
the heatmap frames fully synthetic. This lets the frontend talk to a fake
server without any code changes.

Usage:
    pip install fastapi uvicorn
    uvicorn mock.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import secrets
import time
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="Gridlock — mock heatmap server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GRID_CONFIG = {
    "bounds": {"west": -122.4597, "south": 47.481, "east": -122.2244, "north": 47.734},
    "rows": 57,
    "cols": 36,
}
FRAME_INTERVAL_S = 1.0
VALID_SCENARIO_IDS = ("line-1", "line-1-2", "line-1-2-ballard")
DEFAULT_SCENARIO_ID = VALID_SCENARIO_IDS[0]
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
TIME_BIN_MINUTES = 30
DEFAULT_SIM_STEP_SECONDS = 300

# Hotspots are cumulative by scenario so the deploy-step UI visibly changes the
# synthetic map as each transit expansion is enabled.
SCENARIO_HOTSPOTS = {
    "line-1": [
        (28, 18, 1.00),  # downtown core
        (30, 17, 0.72),  # pioneer square / stadium district
        (28, 20, 0.58),  # first hill
        (31, 22, 0.48),  # mount baker corridor
        (35, 23, 0.52),  # beacon hill
        (15, 17, 0.62),  # university district / north link
    ],
    "line-1-2": [
        (26, 20, 0.82),  # capitol hill
        (23, 25, 0.54),  # montlake / 520 approach
        (25, 24, 0.47),  # leschi / madrona
        (21, 26, 0.42),  # laurelhurst edge
        (24, 27, 0.35),  # washington park
    ],
    "line-1-2-ballard": [
        (26, 17, 0.78),  # south lake union / belltown
        (19, 14, 0.68),  # queen anne
        (16, 12, 0.61),  # fremont
        (14, 10, 0.56),  # ballard
        (16, 15, 0.45),  # wallingford
        (36, 11, 0.48),  # west seattle junction
    ],
}

_PUGET_COAST = [
    (47.74, -122.42),
    (47.69, -122.41),
    (47.67, -122.40),
    (47.645, -122.41),
    (47.635, -122.39),
    (47.625, -122.37),
    (47.615, -122.355),
    (47.605, -122.347),
    (47.595, -122.347),
    (47.58, -122.36),
    (47.565, -122.375),
    (47.55, -122.39),
    (47.50, -122.40),
]

_LAKE_WA_COAST = [
    (47.70, -122.255),
    (47.68, -122.260),
    (47.66, -122.262),
    (47.645, -122.270),
    (47.635, -122.275),
    (47.62, -122.272),
    (47.60, -122.270),
    (47.58, -122.268),
    (47.56, -122.262),
    (47.50, -122.255),
]

_BASE_GAUSS_SPREAD = 25
_BASE_CUTOFF_DIST = 12
_PERSON_RADIUS = 2
_PERSON_SPREAD = 1.5


def _interp_lon(lat: float, waypoints: list[tuple[float, float]]) -> float:
    if lat >= waypoints[0][0]:
        return waypoints[0][1]
    if lat <= waypoints[-1][0]:
        return waypoints[-1][1]
    for i in range(len(waypoints) - 1):
        lat_a, lon_a = waypoints[i]
        lat_b, lon_b = waypoints[i + 1]
        if lat_b <= lat <= lat_a:
            t = (lat - lat_b) / (lat_a - lat_b)
            return lon_b + t * (lon_a - lon_b)
    return waypoints[-1][1]


def _build_water_mask() -> list[list[bool]]:
    bounds = GRID_CONFIG["bounds"]
    rows = GRID_CONFIG["rows"]
    cols = GRID_CONFIG["cols"]
    cell_w = (bounds["east"] - bounds["west"]) / cols
    cell_h = (bounds["north"] - bounds["south"]) / rows

    mask: list[list[bool]] = [[False] * cols for _ in range(rows)]
    for r in range(rows):
        lat = bounds["north"] - (r + 0.5) * cell_h
        for c in range(cols):
            lon = bounds["west"] + (c + 0.5) * cell_w

            if lon < _interp_lon(lat, _PUGET_COAST):
                mask[r][c] = True
                continue
            if 47.628 < lat < 47.646 and -122.344 < lon < -122.328:
                mask[r][c] = True
                continue
            if 47.52 < lat < 47.70 and lon > _interp_lon(lat, _LAKE_WA_COAST):
                mask[r][c] = True

    return mask


WATER_MASK = _build_water_mask()


@dataclass
class Person:
    id: str
    lat: float
    lon: float
    count: int


@dataclass(frozen=True)
class SimTime:
    day_of_week: int
    time_bin: int
    minute_of_week: int

    def to_dict(self) -> dict[str, int]:
        return {
            "day_of_week": self.day_of_week,
            "time_bin": self.time_bin,
            "minute_of_week": self.minute_of_week,
        }


@dataclass
class MockPlaybackController:
    frame_interval_seconds: float = FRAME_INTERVAL_S
    sim_step_seconds: int = DEFAULT_SIM_STEP_SECONDS
    time_bin_minutes: int = TIME_BIN_MINUTES
    current_tick: int = 0
    is_playing: bool = True

    @property
    def sim_minutes_per_second(self) -> float:
        if self.frame_interval_seconds <= 0:
            return 0.0
        return (self.sim_step_seconds / 60) / self.frame_interval_seconds

    @property
    def current_time(self) -> SimTime:
        minute_of_week = (self.current_tick * self.sim_step_seconds // 60) % MINUTES_PER_WEEK
        minute_of_day = minute_of_week % MINUTES_PER_DAY
        return SimTime(
            day_of_week=minute_of_week // MINUTES_PER_DAY,
            time_bin=(minute_of_day // self.time_bin_minutes) * self.time_bin_minutes,
            minute_of_week=minute_of_week,
        )

    def advance(self) -> SimTime:
        if self.is_playing:
            self.current_tick += 1
        return self.current_time

    def set_playing(self, is_playing: bool) -> None:
        self.is_playing = is_playing

    def set_speed(self, sim_minutes_per_second: float) -> None:
        if sim_minutes_per_second <= 0:
            raise ValueError("sim_minutes_per_second must be positive.")
        self.sim_step_seconds = int(round(sim_minutes_per_second * 60 * self.frame_interval_seconds))

    def seek(
        self,
        *,
        minute_of_week: int | None = None,
        day_of_week: int | None = None,
        time_bin: int | None = None,
    ) -> SimTime:
        if minute_of_week is None:
            if day_of_week is None or time_bin is None:
                raise ValueError("Provide minute_of_week or both day_of_week and time_bin.")
            minute_of_week = (int(day_of_week) % 7) * MINUTES_PER_DAY + int(time_bin)
        seconds = (minute_of_week % MINUTES_PER_WEEK) * 60
        self.current_tick = seconds // self.sim_step_seconds
        return self.current_time

    def to_dict(self) -> dict[str, object]:
        return {
            "is_playing": self.is_playing,
            "current_tick": self.current_tick,
            "sim_step_seconds": self.sim_step_seconds,
            "sim_minutes_per_second": self.sim_minutes_per_second,
            "frame_interval_seconds": self.frame_interval_seconds,
            "time_bin_minutes": self.time_bin_minutes,
            "sim_time": self.current_time.to_dict(),
        }


class MockState:
    def __init__(self) -> None:
        self.scenario_id = DEFAULT_SCENARIO_ID
        self.people: dict[str, Person] = {}
        self._version = 0
        self._cond: asyncio.Condition | None = None

    @property
    def version(self) -> int:
        return self._version

    def _ensure_cond(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def notify_change(self) -> None:
        self._version += 1
        cond = self._ensure_cond()
        async with cond:
            cond.notify_all()

    async def wait_for_change(self, last_version: int, timeout: float) -> int:
        cond = self._ensure_cond()
        async with cond:
            try:
                await asyncio.wait_for(
                    cond.wait_for(lambda: self._version > last_version),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                pass
        return self._version

    def set_scenario(self, scenario_id: str) -> None:
        if scenario_id not in VALID_SCENARIO_IDS:
            raise ValueError(f"Unknown scenario_id: {scenario_id!r}")
        self.scenario_id = scenario_id

    def add_person(self, lat: float, lon: float, count: int) -> Person:
        bounds = GRID_CONFIG["bounds"]
        in_bounds = (
            bounds["west"] <= lon <= bounds["east"]
            and bounds["south"] <= lat <= bounds["north"]
        )
        if not in_bounds:
            raise ValueError("lat/lon outside configured grid bounds")
        person = Person(
            id=f"p_{secrets.token_hex(4)}",
            lat=lat,
            lon=lon,
            count=max(1, int(count)),
        )
        self.people[person.id] = person
        return person

    def remove_person(self, person_id: str) -> None:
        if person_id not in self.people:
            raise KeyError(person_id)
        del self.people[person_id]

    def clear_people(self) -> None:
        self.people.clear()


STATE = MockState()
PLAYBACK = MockPlaybackController()

_SHUTDOWN: asyncio.Event | None = None


def _shutdown_event() -> asyncio.Event:
    global _SHUTDOWN
    if _SHUTDOWN is None:
        _SHUTDOWN = asyncio.Event()
    return _SHUTDOWN


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    _shutdown_event().set()


def _sse(event_id: int, event: str, data: dict) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n"


def _iter_hotspots_for_scenario(scenario_id: str) -> list[tuple[int, int, float]]:
    hotspots: list[tuple[int, int, float]] = []
    for candidate in VALID_SCENARIO_IDS:
        hotspots.extend(SCENARIO_HOTSPOTS[candidate])
        if candidate == scenario_id:
            break
    return hotspots


def _point_to_cell(lat: float, lon: float) -> tuple[int, int] | None:
    bounds = GRID_CONFIG["bounds"]
    rows = GRID_CONFIG["rows"]
    cols = GRID_CONFIG["cols"]
    cell_w = (bounds["east"] - bounds["west"]) / cols
    cell_h = (bounds["north"] - bounds["south"]) / rows
    col = int((lon - bounds["west"]) / cell_w)
    row = int((bounds["north"] - lat) / cell_h)
    if 0 <= row < rows and 0 <= col < cols:
        return row, col
    return None


def _person_strength(count: int) -> float:
    # Keep larger crowds visible without saturating huge chunks of the map.
    return min(0.95, 0.16 + 0.12 * math.log1p(max(1, count)))


def generate_frame(
    t: float,
    scenario_id: str,
    people: list[Person],
) -> dict[str, float | list[list[int | float]]]:
    cells: list[list[int | float]] = []
    rows = GRID_CONFIG["rows"]
    cols = GRID_CONFIG["cols"]
    hotspots = _iter_hotspots_for_scenario(scenario_id)

    density_map: list[list[float]] = [[0.0] * cols for _ in range(rows)]
    for r in range(rows):
        row_mask = WATER_MASK[r]
        density_row = density_map[r]
        for c in range(cols):
            if row_mask[c]:
                continue

            density = 0.0
            for hr, hc, strength in hotspots:
                dr = hr + 5 * math.sin(t * 0.3 + hr)
                dc = hc + 5 * math.cos(t * 0.2 + hc)
                dx = r - dr
                dy = c - dc
                if abs(dx) > _BASE_CUTOFF_DIST or abs(dy) > _BASE_CUTOFF_DIST:
                    continue
                dist_sq = dx * dx + dy * dy
                if dist_sq > _BASE_CUTOFF_DIST * _BASE_CUTOFF_DIST:
                    continue
                density += strength * math.exp(-dist_sq / _BASE_GAUSS_SPREAD)

            density += random.gauss(0.0, 0.02)
            density_row[c] = max(0.0, min(1.0, density))

    for person in people:
        cell = _point_to_cell(person.lat, person.lon)
        if cell is None:
            continue
        center_row, center_col = cell
        strength = _person_strength(person.count)
        row_min = max(0, center_row - _PERSON_RADIUS)
        row_max = min(rows - 1, center_row + _PERSON_RADIUS)
        col_min = max(0, center_col - _PERSON_RADIUS)
        col_max = min(cols - 1, center_col + _PERSON_RADIUS)

        for row in range(row_min, row_max + 1):
            row_mask = WATER_MASK[row]
            density_row = density_map[row]
            for col in range(col_min, col_max + 1):
                if row_mask[col]:
                    continue
                dist_sq = (row - center_row) ** 2 + (col - center_col) ** 2
                if dist_sq > _PERSON_RADIUS * _PERSON_RADIUS:
                    continue
                boost = strength * math.exp(-dist_sq / _PERSON_SPREAD)
                density_row[col] = min(1.0, density_row[col] + boost)

    for r in range(rows):
        row_mask = WATER_MASK[r]
        density_row = density_map[r]
        for c in range(cols):
            if row_mask[c]:
                continue
            density = density_row[c]
            if density > 0.05:
                cells.append([r, c, round(density, 3)])

    return {"timestamp": time.time(), "cells": cells}


async def _stream(request: Request):
    event_id = 0
    t = 0.0
    shutdown = _shutdown_event()

    yield _sse(event_id, "config", GRID_CONFIG)
    event_id += 1

    yield _sse(event_id, "scenario", {"scenario_id": STATE.scenario_id})
    event_id += 1
    yield _sse(event_id, "playback", PLAYBACK.to_dict())
    event_id += 1
    last_scenario = STATE.scenario_id
    last_version = STATE.version
    last_playback_state = PLAYBACK.to_dict()

    try:
        while not shutdown.is_set():
            if await request.is_disconnected():
                break

            if STATE.scenario_id != last_scenario:
                yield _sse(event_id, "scenario", {"scenario_id": STATE.scenario_id})
                event_id += 1
                last_scenario = STATE.scenario_id

            frame = generate_frame(t, STATE.scenario_id, list(STATE.people.values()))
            frame["sim_time"] = PLAYBACK.current_time.to_dict()
            yield _sse(event_id, "frame", frame)
            event_id += 1

            PLAYBACK.advance()
            playback_state = PLAYBACK.to_dict()
            if playback_state != last_playback_state:
                yield _sse(event_id, "playback", playback_state)
                event_id += 1
                last_playback_state = playback_state

            t += 0.5

            wait_task = asyncio.create_task(
                STATE.wait_for_change(last_version, timeout=FRAME_INTERVAL_S),
            )
            shutdown_task = asyncio.create_task(shutdown.wait())
            done, pending = await asyncio.wait(
                {wait_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if shutdown_task in done:
                break
            last_version = wait_task.result()
    except asyncio.CancelledError:
        pass


@app.get("/api/heatmap/stream")
async def stream(request: Request):
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/scenario")
async def post_scenario(payload: dict):
    scenario_id = payload.get("scenario_id")
    if not isinstance(scenario_id, str):
        raise HTTPException(status_code=400, detail="scenario_id must be a string")
    try:
        STATE.set_scenario(scenario_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await STATE.notify_change()
    return {"scenario_id": STATE.scenario_id}


@app.get("/api/playback")
async def get_playback():
    return PLAYBACK.to_dict()


@app.post("/api/playback")
async def update_playback(payload: dict):
    if "is_playing" in payload:
        PLAYBACK.set_playing(bool(payload["is_playing"]))
    if "sim_minutes_per_second" in payload:
        try:
            PLAYBACK.set_speed(float(payload["sim_minutes_per_second"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await STATE.notify_change()
    return PLAYBACK.to_dict()


@app.post("/api/playback/seek")
async def seek_playback(payload: dict):
    try:
        if "minute_of_week" in payload:
            PLAYBACK.seek(minute_of_week=int(payload["minute_of_week"]))
        else:
            PLAYBACK.seek(
                day_of_week=int(payload["day_of_week"]),
                time_bin=int(payload["time_bin"]),
            )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Seek payload must include minute_of_week or day_of_week and time_bin.",
        ) from exc
    await STATE.notify_change()
    return PLAYBACK.to_dict()


@app.post("/api/people", status_code=201)
async def post_people(payload: dict):
    try:
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="lat and lon are required and must be numeric",
        ) from exc
    count_raw = payload.get("count", 1)
    try:
        count = int(count_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="count must be an integer") from exc

    try:
        person = STATE.add_person(lat=lat, lon=lon, count=count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await STATE.notify_change()
    return {
        "id": person.id,
        "lat": person.lat,
        "lon": person.lon,
        "count": person.count,
    }


@app.delete("/api/people/{person_id}")
async def delete_person(person_id: str):
    try:
        STATE.remove_person(person_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown id") from exc
    await STATE.notify_change()
    return Response(status_code=204)


@app.delete("/api/people")
async def delete_all_people():
    STATE.clear_people()
    await STATE.notify_change()
    return Response(status_code=204)


@app.get("/api/people")
async def get_people():
    return {
        "people": [
            {"id": p.id, "lat": p.lat, "lon": p.lon, "count": p.count}
            for p in STATE.people.values()
        ],
    }
