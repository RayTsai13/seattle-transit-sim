"""FastAPI backend for the visual-first Seattle demand simulation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .grid import load_grid
from .state import State


logger = logging.getLogger(__name__)

# Anchored to the repository root rather than the working directory, so the
# server and the tests start correctly regardless of where they are launched.
_REPO_ROOT = Path(__file__).resolve().parent.parent

GEOJSON_PATH = Path(
    os.environ.get(
        "HEATMAP_GEOJSON",
        str(_REPO_ROOT / "seattle" / "data" / "processed" / "seattle_heatmap_grid.geojson"),
    )
)
FRAME_INTERVAL_S = float(os.environ.get("HEATMAP_FRAME_INTERVAL", "1.0"))
SIM_STEP_SECONDS = int(os.environ.get("HEATMAP_SIM_STEP_SECONDS", "1800"))


async def _frame_ticker() -> None:
    """Advance the simulation clock and compose the shared frame.

    This is the single producer. Connections never advance the clock, so the
    sim runs at the same rate whether one client is watching or twenty. When
    nobody is connected the clock holds rather than composing frames for an
    empty room.
    """
    while True:
        await asyncio.sleep(FRAME_INTERVAL_S)
        try:
            if STATE.subscriber_count == 0:
                continue
            STATE.tick()
            await STATE.notify_change()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive; keep the ticker alive
            logger.exception("Frame ticker iteration failed")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    ticker = asyncio.create_task(_frame_ticker())
    try:
        yield
    finally:
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker


app = FastAPI(title="Gridlock - visual demand simulation", lifespan=lifespan)

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
    """Emit the handshake, then relay the shared frame as it changes.

    A connection is a pure reader: it never advances the clock and never
    composes a frame of its own.
    """
    event_id = 0
    STATE.add_subscriber()
    try:
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

            yield _sse(event_id, "frame", STATE.current_frame())
            event_id += 1

            last_version = await STATE.wait_for_change(
                last_version,
                timeout=FRAME_INTERVAL_S,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        # A dead generator closes the response mid-stream with no explanation;
        # log it so the cause is recoverable from the server side.
        logger.exception("Heatmap stream terminated by an unhandled error")
        raise
    finally:
        STATE.remove_subscriber()


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
        "frame": STATE.current_frame(),
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
