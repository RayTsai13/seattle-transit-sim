"""FastAPI backend for the visual-first Seattle demand simulation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .grid import load_grid
from .state import State


GEOJSON_PATH = Path(
    os.environ.get(
        "HEATMAP_GEOJSON",
        "seattle/data/processed/seattle_heatmap_grid.geojson",
    )
)
FRAME_INTERVAL_S = float(os.environ.get("HEATMAP_FRAME_INTERVAL", "1.0"))
SIM_STEP_SECONDS = int(os.environ.get("HEATMAP_SIM_STEP_SECONDS", "1800"))

app = FastAPI(title="Gridlock - visual demand simulation")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GRID = load_grid(GEOJSON_PATH)
STATE = State(
    GRID,
    frame_interval_seconds=FRAME_INTERVAL_S,
    sim_step_seconds=SIM_STEP_SECONDS,
)


def _sse(event_id: int, event: str, data: dict[str, Any]) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream(request: Request):
    event_id = 0

    yield _sse(event_id, "config", STATE.grid.config())
    event_id += 1

    yield _sse(event_id, "scenario", {"scenario_id": STATE.scenario_id})
    event_id += 1
    last_scenario = STATE.scenario_id
    last_scenario_revision = STATE.scenario_revision

    playback_state = STATE.playback_state()
    yield _sse(event_id, "playback", playback_state)
    event_id += 1
    last_playback_state = playback_state

    last_version = STATE.version
    while True:
        if await request.is_disconnected():
            break

        if (
            STATE.scenario_id != last_scenario
            or STATE.scenario_revision != last_scenario_revision
        ):
            yield _sse(event_id, "scenario", {"scenario_id": STATE.scenario_id})
            event_id += 1
            last_scenario = STATE.scenario_id
            last_scenario_revision = STATE.scenario_revision

        playback_state = STATE.playback_state()
        if playback_state != last_playback_state:
            yield _sse(event_id, "playback", playback_state)
            event_id += 1
            last_playback_state = playback_state

        yield _sse(event_id, "frame", STATE.compose_frame())
        event_id += 1

        STATE.advance_playback()
        playback_state = STATE.playback_state()
        if playback_state != last_playback_state:
            yield _sse(event_id, "playback", playback_state)
            event_id += 1
            last_playback_state = playback_state

        last_version = await STATE.wait_for_change(
            last_version,
            timeout=FRAME_INTERVAL_S,
        )


@app.get("/api/heatmap/stream")
async def heatmap_stream(request: Request):
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "scenario_id": STATE.scenario_id,
        "state_version": STATE.state_version,
        "grid": {
            "rows": STATE.grid.rows,
            "cols": STATE.grid.cols,
        },
        "people_count": len(STATE.people),
        "playback": STATE.playback_state(),
    }


@app.post("/api/scenario")
async def post_scenario(payload: dict[str, Any]):
    scenario_id = payload.get("scenario_id")
    if not isinstance(scenario_id, str):
        raise HTTPException(status_code=400, detail="scenario_id must be a string")
    try:
        STATE.set_scenario(
            scenario_id,
            stops=payload.get("stops"),
            lines=payload.get("lines"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await STATE.notify_change()
    return {
        "scenario_id": STATE.scenario_id,
        "frame": STATE.compose_frame(),
    }


@app.get("/api/playback")
async def get_playback():
    return STATE.playback_state()


@app.post("/api/playback")
async def post_playback(payload: dict[str, Any]):
    try:
        STATE.set_playback(
            is_playing=bool(payload["is_playing"]) if "is_playing" in payload else None,
            sim_minutes_per_second=(
                float(payload["sim_minutes_per_second"])
                if "sim_minutes_per_second" in payload
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await STATE.notify_change()
    return STATE.playback_state()


@app.post("/api/playback/seek")
async def seek_playback(payload: dict[str, Any]):
    try:
        if "minute_of_week" in payload:
            STATE.seek_playback(minute_of_week=int(payload["minute_of_week"]))
        else:
            STATE.seek_playback(
                day_of_week=int(payload["day_of_week"]),
                time_bin=int(payload["time_bin"]),
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Seek payload must include minute_of_week or day_of_week and time_bin.",
        ) from exc
    await STATE.notify_change()
    return STATE.playback_state()


@app.post("/api/people", status_code=201)
async def post_people(payload: dict[str, Any]):
    try:
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="lat and lon are required and must be numeric",
        ) from exc
    try:
        count = int(payload.get("count", 1))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="count must be an integer") from exc

    optional_keys = {"duration_minutes", "radius_m", "kind", "decay_m"}
    include_tuning = any(key in payload for key in optional_keys)
    try:
        person = STATE.add_person(
            lat=lat,
            lon=lon,
            count=count,
            kind=str(payload["kind"]) if "kind" in payload else None,
            duration_minutes=(
                int(payload["duration_minutes"])
                if "duration_minutes" in payload
                else None
            ),
            radius_m=float(payload["radius_m"]) if "radius_m" in payload else None,
            decay_m=float(payload["decay_m"]) if "decay_m" in payload else None,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await STATE.notify_change()
    return person.to_public_dict(include_tuning=include_tuning)


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
            person.to_public_dict()
            for person in STATE.people.values()
        ],
    }
