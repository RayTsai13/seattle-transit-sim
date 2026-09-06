"""Process-wide simulation state for the interactive heatmap backend."""

from __future__ import annotations

import asyncio
from typing import Any

from .composer import FrameComposer
from .demand import DemandField
from .grid import Grid
from .landuse import build_land_use, cell_centers
from .network import (
    DEFAULT_SCENARIO_ID,
    VALID_SCENARIO_IDS,
    ActiveNetwork,
    ServiceCapacity,
    default_network_for_scenario,
    parse_network_payload,
)
from .overlays import CrowdOverlay, LiveOverlayManager
from .sim_time import PlaybackController


class State:
    """Single-process simulation state shared by all SSE connections."""

    def __init__(
        self,
        grid: Grid,
        *,
        frame_interval_seconds: float = 1.0,
        sim_step_seconds: int = 1800,
    ) -> None:
        self.grid = grid
        self.centers = cell_centers(grid)
        self.land_use = build_land_use(self.centers)
        self.demand_field = DemandField(self.land_use)
        self.composer = FrameComposer(cols=grid.cols)
        self.playback = PlaybackController(
            frame_interval_seconds=frame_interval_seconds,
            sim_step_seconds=sim_step_seconds,
        )
        self.scenario_id: str = DEFAULT_SCENARIO_ID
        self.active_network: ActiveNetwork = default_network_for_scenario(self.scenario_id)
        self.service = ServiceCapacity(grid, self.active_network)
        self.overlays = LiveOverlayManager(self.centers)
        self.people = self.overlays.people
        self.scenario_revision: int = 0
        self._version: int = 0
        self._state_counter: int = 0
        self._cond: asyncio.Condition | None = None

    @property
    def version(self) -> int:
        return self._version

    @property
    def state_version(self) -> str:
        return f"state_v{self._state_counter}"

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

    def set_scenario(
        self,
        scenario_id: str,
        *,
        stops: Any = None,
        lines: Any = None,
    ) -> None:
        if scenario_id not in VALID_SCENARIO_IDS:
            raise ValueError(f"Unknown scenario_id: {scenario_id!r}")
        self.scenario_id = scenario_id
        self.active_network = parse_network_payload(
            scenario_id=scenario_id,
            stops_payload=stops,
            lines_payload=lines,
        )
        self.service = ServiceCapacity(self.grid, self.active_network)
        self.scenario_revision += 1
        self._state_counter += 1

    def add_person(
        self,
        *,
        lat: float,
        lon: float,
        count: int,
        kind: str | None = None,
        duration_minutes: int | None = None,
        radius_m: float | None = None,
        decay_m: float | None = None,
    ) -> CrowdOverlay:
        bounds = self.grid.bounds
        in_bounds = (
            bounds.west <= lon <= bounds.east
            and bounds.south <= lat <= bounds.north
        )
        if not in_bounds:
            raise ValueError("lat/lon outside configured grid bounds")
        overlay = self.overlays.add(
            lat=lat,
            lon=lon,
            count=count,
            sim_time=self.playback.current_time,
            kind=kind,
            duration_minutes=duration_minutes,
            radius_m=radius_m,
            decay_m=decay_m,
        )
        self._state_counter += 1
        return overlay

    def remove_person(self, person_id: str) -> None:
        self.overlays.remove(person_id)
        self._state_counter += 1

    def clear_people(self) -> None:
        if self.people:
            self._state_counter += 1
        self.overlays.clear()

    def playback_state(self) -> dict[str, object]:
        return self.playback.to_dict()

    def set_playback(
        self,
        *,
        is_playing: bool | None = None,
        sim_minutes_per_second: float | None = None,
    ) -> None:
        if is_playing is not None:
            self.playback.set_playing(is_playing)
        if sim_minutes_per_second is not None:
            self.playback.set_speed(sim_minutes_per_second)

    def seek_playback(
        self,
        *,
        minute_of_week: int | None = None,
        day_of_week: int | None = None,
        time_bin: int | None = None,
    ) -> None:
        self.playback.seek(
            minute_of_week=minute_of_week,
            day_of_week=day_of_week,
            time_bin=time_bin,
        )

    def advance_playback(self) -> None:
        self.playback.advance()

    def compose_frame(self) -> dict[str, object]:
        return self.composer.compose(
            sim_time=self.playback.current_time,
            state_version=self.state_version,
            demand_field=self.demand_field,
            service=self.service,
            overlays=self.overlays,
        ).to_dict()

    def compose_frame_cells(self) -> list[list[int | float]]:
        return self.compose_frame()["cells"]  # type: ignore[return-value]
