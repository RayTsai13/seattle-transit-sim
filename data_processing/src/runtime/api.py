"""FastAPI runtime for the interactive demand heatmap."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from src.common.artifacts import (
    DEFAULT_PROCESSED_DIR,
    DEMAND_HEATMAP_PREDICTIONS_CSV,
    PROCESSED_MODEL_OUTPUTS_DIR,
    PROCESSED_SCENARIOS_DIR,
)
from src.runtime.clock import DEFAULT_SIM_STEP_SECONDS, SimulationClock
from src.runtime.composer import FrameComposer
from src.runtime.overlays import LiveOverlayManager
from src.runtime.playback import PlaybackController
from src.runtime.state import ScenarioStateManager
from src.runtime.stores import DemandFrameStore, GridConfig


DATA_PROCESSING_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE_CSV = (
    DATA_PROCESSING_ROOT
    / DEFAULT_PROCESSED_DIR
    / PROCESSED_MODEL_OUTPUTS_DIR
    / DEMAND_HEATMAP_PREDICTIONS_CSV
)
KNOWN_DISPLAY_SCENARIO_DELTAS = {
    "line-1-2": Path(DEFAULT_PROCESSED_DIR)
    / PROCESSED_SCENARIOS_DIR
    / "line-1-2"
    / "demand_heatmap_scenario_predictions.csv",
    "line-1-2-ballard": Path(DEFAULT_PROCESSED_DIR)
    / PROCESSED_SCENARIOS_DIR
    / "line-1-2-ballard"
    / "demand_heatmap_scenario_predictions.csv",
}
DISPLAY_SCENARIO_SEQUENCE = {
    "line-1": [],
    "line-1-2": ["line-1-2"],
    "line-1-2-ballard": ["line-1-2", "line-1-2-ballard"],
}


@dataclass
class HeatmapRuntime:
    baseline: DemandFrameStore
    clock: SimulationClock
    playback: PlaybackController
    scenario_state: ScenarioStateManager
    live_overlays: LiveOverlayManager
    composer: FrameComposer
    frame_interval_seconds: float = 1.0
    active_display_scenario_id: str = "state_baseline"

    @classmethod
    def create(
        cls,
        *,
        baseline_csv: Path = DEFAULT_BASELINE_CSV,
        sim_step_seconds: int = DEFAULT_SIM_STEP_SECONDS,
        time_bin_minutes: int = 30,
        frame_interval_seconds: float = 1.0,
        display_threshold: float = 0.0,
        display_floor: float = 0.13,
        display_ceiling: float = 0.75,
        display_gamma: float = 0.75,
        scenario_delta_multiplier: float = 3.0,
    ) -> "HeatmapRuntime":
        baseline = DemandFrameStore.from_predictions_csv(baseline_csv)
        clock = SimulationClock(
            sim_step_seconds=sim_step_seconds,
            time_bin_minutes=time_bin_minutes,
        )
        playback = PlaybackController(clock=clock, frame_interval_seconds=frame_interval_seconds)
        scenario_state = ScenarioStateManager()
        live_overlays = LiveOverlayManager(baseline.config)
        composer = FrameComposer(
            baseline=baseline,
            scenario_state=scenario_state,
            clock=clock,
            live_overlays=live_overlays,
            display_threshold=display_threshold,
            display_floor=display_floor,
            display_ceiling=display_ceiling,
            display_gamma=display_gamma,
            scenario_delta_multiplier=scenario_delta_multiplier,
        )
        return cls(
            baseline=baseline,
            clock=clock,
            playback=playback,
            scenario_state=scenario_state,
            live_overlays=live_overlays,
            composer=composer,
            frame_interval_seconds=frame_interval_seconds,
        )


def create_app(
    *,
    baseline_csv: Path = DEFAULT_BASELINE_CSV,
    sim_step_seconds: int = DEFAULT_SIM_STEP_SECONDS,
    time_bin_minutes: int = 30,
    frame_interval_seconds: float = 1.0,
    display_threshold: float = 0.0,
    display_floor: float = 0.13,
    display_ceiling: float = 0.75,
    display_gamma: float = 0.75,
    scenario_delta_multiplier: float = 3.0,
) -> FastAPI:
    runtime = HeatmapRuntime.create(
        baseline_csv=baseline_csv,
        sim_step_seconds=sim_step_seconds,
        time_bin_minutes=time_bin_minutes,
        frame_interval_seconds=frame_interval_seconds,
        display_threshold=display_threshold,
        display_floor=display_floor,
        display_ceiling=display_ceiling,
        display_gamma=display_gamma,
        scenario_delta_multiplier=scenario_delta_multiplier,
    )
    app = FastAPI(title="Demand Heatmap Runtime")
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/heatmap/stream")
    async def stream(request: Request):
        return StreamingResponse(
            stream_frames(request, runtime),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/states/current")
    async def current_state():
        state = runtime.scenario_state.to_dict(
            current_tick=runtime.playback.current_tick,
            current_sim_time=runtime.playback.current_time,
        )
        state["active_display_scenario_id"] = runtime.active_display_scenario_id
        state["playback"] = runtime.playback.to_dict()
        state["live_overlays"] = runtime.live_overlays.to_list()
        return state

    @app.get("/api/playback")
    async def get_playback():
        return runtime.playback.to_dict()

    @app.post("/api/playback")
    async def update_playback(payload: dict[str, Any]):
        if "is_playing" in payload:
            runtime.playback.set_playing(bool(payload["is_playing"]))
        if "sim_minutes_per_second" in payload:
            try:
                runtime.playback.set_speed(float(payload["sim_minutes_per_second"]))
            except ValueError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
        return runtime.playback.to_dict()

    @app.post("/api/playback/seek")
    async def seek_playback(payload: dict[str, Any]):
        try:
            if "minute_of_week" in payload:
                runtime.playback.seek(minute_of_week=int(payload["minute_of_week"]))
            else:
                runtime.playback.seek(
                    day_of_week=int(payload["day_of_week"]),
                    time_bin=int(payload["time_bin"]),
                )
        except (KeyError, ValueError) as error:
            raise HTTPException(
                status_code=400,
                detail="Seek payload must include minute_of_week or day_of_week and time_bin.",
            ) from error
        return runtime.playback.to_dict()

    @app.get("/api/debug/frame")
    async def debug_frame():
        frame = runtime.composer.compose(
            tick=runtime.playback.current_tick,
            sim_time=runtime.playback.current_time,
        )
        densities = [float(cell[2]) for cell in frame.cells]
        return {
            "frame": frame.to_dict(),
            "geojson": frame_to_geojson(frame.cells, runtime.baseline.config),
            "summary": {
                "cell_count": len(frame.cells),
                "min_density": min(densities) if densities else 0.0,
                "max_density": max(densities) if densities else 0.0,
                "avg_density": sum(densities) / len(densities) if densities else 0.0,
                "grid": runtime.baseline.config.to_sse_config(),
                "value_column": runtime.baseline.value_column,
                "display_floor": runtime.composer.display_floor,
                "display_ceiling": runtime.composer.display_ceiling,
                "display_gamma": runtime.composer.display_gamma,
                "scenario_delta_multiplier": runtime.composer.scenario_delta_multiplier,
                "playback": runtime.playback.to_dict(),
                "live_overlay_count": len(runtime.live_overlays.to_list()),
            },
        }

    @app.post("/api/scenario")
    async def set_display_scenario(payload: dict[str, Any]):
        """Compatibility endpoint for the current frontend deployment controls."""
        scenario_id = payload.get("scenario_id")
        if not scenario_id:
            raise HTTPException(status_code=400, detail="Scenario payload missing scenario_id.")
        runtime.active_display_scenario_id = str(scenario_id)
        applied = apply_known_display_scenario(runtime, runtime.active_display_scenario_id)
        return {
            "scenario_id": runtime.active_display_scenario_id,
            "applied_delta": applied,
            "state_version": runtime.scenario_state.current_state_version,
            "registered_scenarios": [
                record.scenario_id for record in runtime.scenario_state.records
            ],
        }

    @app.post("/api/people")
    async def create_people_overlay(payload: dict[str, Any]):
        try:
            overlay = runtime.live_overlays.add(
                kind="people",
                lat=float(payload["lat"]),
                lon=float(payload["lon"]),
                count=int(payload.get("count", 1)),
                sim_time=runtime.playback.current_time,
                duration_minutes=int(payload.get("duration_minutes", 180)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="People payload requires lat, lon, and count.") from error
        return {
            "id": overlay.overlay_id,
            "lat": overlay.lat,
            "lon": overlay.lon,
            "count": overlay.count,
            "duration_minutes": overlay.duration_minutes,
        }

    @app.post("/api/events")
    async def create_event_overlay(payload: dict[str, Any]):
        try:
            overlay = runtime.live_overlays.add(
                kind="event",
                lat=float(payload["lat"]),
                lon=float(payload["lon"]),
                count=int(payload.get("attendance", payload.get("count", 1))),
                sim_time=runtime.playback.current_time,
                duration_minutes=int(payload.get("duration_minutes", 240)),
                decay_m=float(payload.get("decay_m", 1000.0)),
                radius_m=float(payload.get("radius_m", 3500.0)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail="Event payload requires lat and lon.") from error
        return overlay.to_dict()

    @app.get("/api/people")
    async def list_people_overlays():
        return {"overlays": runtime.live_overlays.to_list()}

    @app.delete("/api/people/{overlay_id}")
    async def delete_people_overlay(overlay_id: str):
        if not runtime.live_overlays.remove(overlay_id):
            raise HTTPException(status_code=404, detail="People overlay not found.")
        return {"deleted": overlay_id}

    @app.delete("/api/people")
    async def clear_people_overlays():
        runtime.live_overlays.clear()
        return {"deleted": "all"}

    @app.post("/api/scenarios")
    async def create_scenario(payload: dict[str, Any]):
        if payload.get("type", "precomputed_delta") != "precomputed_delta":
            raise HTTPException(
                status_code=400,
                detail="Runtime API currently accepts precomputed_delta scenarios.",
            )
        if "delta_csv" not in payload:
            raise HTTPException(status_code=400, detail="Scenario payload missing delta_csv.")

        try:
            delta_csv = resolve_path(payload["delta_csv"])
            validate_delta_grid_bounds(delta_csv, runtime.baseline.config)
            effective_from_tick = payload.get("effective_from_tick")
            record = runtime.scenario_state.register_precomputed_delta(
                payload=payload,
                delta_csv=delta_csv,
                current_tick=runtime.playback.current_tick,
                current_sim_time=runtime.playback.current_time,
                effective_from_tick=None if effective_from_tick is None else int(effective_from_tick),
                effective_from_sim_time=runtime.playback.current_time,
            )
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return record.to_dict()

    @app.get("/api/scenarios/{scenario_id}/status")
    async def scenario_status(scenario_id: str):
        record = runtime.scenario_state.get_scenario(scenario_id)
        if not record:
            raise HTTPException(status_code=404, detail="Scenario not found.")
        return record.to_dict()

    @app.get("/api/states/{state_version}/deltas")
    async def state_delta_summary(state_version: str):
        record = runtime.scenario_state.get_state_delta(state_version)
        if not record:
            raise HTTPException(status_code=404, detail="State version not found.")
        return record.to_dict()

    return app


async def stream_frames(request: Request, runtime: HeatmapRuntime):
    event_id = 0
    yield sse_event(event_id, "config", runtime.baseline.config.to_sse_config())
    event_id += 1
    sent_display_scenario_id: str | None = None
    sent_playback_state: dict[str, Any] | None = None

    try:
        while True:
            if await request.is_disconnected():
                break
            playback_state = runtime.playback.to_dict()
            if playback_state != sent_playback_state:
                sent_playback_state = playback_state
                yield sse_event(event_id, "playback", playback_state)
                event_id += 1
            if sent_display_scenario_id != runtime.active_display_scenario_id:
                sent_display_scenario_id = runtime.active_display_scenario_id
                yield sse_event(event_id, "scenario", {"scenario_id": sent_display_scenario_id})
                event_id += 1
            sim_time = runtime.playback.current_time
            frame = runtime.composer.compose(tick=runtime.playback.current_tick, sim_time=sim_time)
            yield sse_event(event_id, "frame", frame.to_dict())
            event_id += 1
            await asyncio.sleep(runtime.frame_interval_seconds)
            runtime.playback.advance()
    except asyncio.CancelledError:
        return


def sse_event(event_id: int, event: str, payload: dict) -> str:
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(payload)}\n\n"


def frame_to_geojson(cells: list[list[float | int]], config) -> dict:
    cell_width = (config.east - config.west) / config.cols
    cell_height = (config.north - config.south) / config.rows
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        config.west + (int(col) + 0.5) * cell_width,
                        config.north - (int(row) + 0.5) * cell_height,
                    ],
                },
                "properties": {"density": float(density), "row": int(row), "col": int(col)},
            }
            for row, col, density in cells
        ],
    }


def apply_known_display_scenario(runtime: HeatmapRuntime, scenario_id: str) -> bool:
    if scenario_id not in DISPLAY_SCENARIO_SEQUENCE:
        return False

    runtime.scenario_state.reset()
    applied = False
    for delta_id in DISPLAY_SCENARIO_SEQUENCE[scenario_id]:
        delta_csv = resolve_path(str(KNOWN_DISPLAY_SCENARIO_DELTAS[delta_id]))
        validate_delta_grid_bounds(delta_csv, runtime.baseline.config)
        runtime.scenario_state.register_precomputed_delta(
            payload={
                "scenario_id": delta_id,
                "type": "precomputed_delta",
                "delta_csv": str(delta_csv),
            },
            delta_csv=delta_csv,
            current_tick=runtime.playback.current_tick,
            current_sim_time=runtime.playback.current_time,
            effective_from_tick=runtime.playback.current_tick,
            effective_from_sim_time=runtime.playback.current_time,
        )
        applied = True
    return applied


def validate_delta_grid_bounds(delta_csv: Path, baseline_config: GridConfig) -> None:
    header = pd.read_csv(delta_csv, nrows=0)
    bounds_columns = {"min_lon", "min_lat", "max_lon", "max_lat"}
    if not bounds_columns.issubset(header.columns):
        return

    bounds = pd.read_csv(delta_csv, usecols=sorted(bounds_columns))
    delta_west = float(bounds["min_lon"].min())
    delta_south = float(bounds["min_lat"].min())
    delta_east = float(bounds["max_lon"].max())
    delta_north = float(bounds["max_lat"].max())
    tolerance = 1e-6
    mismatches = [
        ("west", delta_west, baseline_config.west),
        ("south", delta_south, baseline_config.south),
        ("east", delta_east, baseline_config.east),
        ("north", delta_north, baseline_config.north),
    ]
    if any(abs(delta - baseline) > tolerance for _, delta, baseline in mismatches):
        formatted = ", ".join(
            f"{name}: delta={delta:.6f}, baseline={baseline:.6f}"
            for name, delta, baseline in mismatches
        )
        raise ValueError(
            "Scenario delta grid bounds do not match the loaded baseline grid. "
            f"Rebuild the scenario delta against the current full-city candidate grid. {formatted}"
        )


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path
