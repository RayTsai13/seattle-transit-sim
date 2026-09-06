"""Heatmap intermediary FastAPI app.

Implements the HTTP + SSE contract from ``docs/heatmap-api-contract.md`` on
top of a static GeoJSON grid (loaded once at startup). The simulation model
isn't wired in yet — frames are recomposed from the grid plus any people
placed via ``POST /api/people``.

Run with::

    pip install -r backend/requirements.txt
    uvicorn backend.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

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
DENSITY_PROPERTY = os.environ.get("HEATMAP_DENSITY_PROPERTY", "congestion_score")
FRAME_INTERVAL_S = float(os.environ.get("HEATMAP_FRAME_INTERVAL", "1.0"))

app = FastAPI(title="Gridlock — heatmap intermediary")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GRID = load_grid(GEOJSON_PATH, density_property=DENSITY_PROPERTY)
STATE = State(GRID)


def _sse(event_id: int, event: str, data: dict) -> str:
    """Format a single SSE record per the contract."""
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream(request: Request):
    event_id = 0

    yield _sse(event_id, "config", STATE.grid.config())
    event_id += 1

    yield _sse(event_id, "scenario", {"scenario_id": STATE.scenario_id})
    event_id += 1
    last_scenario = STATE.scenario_id

    last_version = STATE.version
    while True:
        if await request.is_disconnected():
            break

        if STATE.scenario_id != last_scenario:
            yield _sse(event_id, "scenario", {"scenario_id": STATE.scenario_id})
            event_id += 1
            last_scenario = STATE.scenario_id

        frame = {
            "timestamp": time.time(),
            "cells": STATE.compose_frame_cells(),
        }
        yield _sse(event_id, "frame", frame)
        event_id += 1

        # Wake early on any state change; otherwise emit at the regular cadence.
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
        "grid": {
            "rows": STATE.grid.rows,
            "cols": STATE.grid.cols,
        },
        "people_count": len(STATE.people),
    }


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
