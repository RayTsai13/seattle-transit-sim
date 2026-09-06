"""Compose backend heatmap frames from baseline and active scenario state."""

from __future__ import annotations

import time
from dataclasses import dataclass

from src.runtime.clock import SimulationClock, SimTime
from src.runtime.overlays import LiveOverlayManager
from src.runtime.state import ScenarioStateManager
from src.runtime.stores import DemandFrameStore, FrameValues, clamp


@dataclass
class HeatmapFrame:
    timestamp: float
    state_version: str
    sim_time: SimTime
    cells: list[list[float | int]]

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "state_version": self.state_version,
            "sim_time": self.sim_time.to_dict(),
            "cells": self.cells,
        }


class FrameComposer:
    """Produces complete SSE frame snapshots for the current simulation state."""

    def __init__(
        self,
        *,
        baseline: DemandFrameStore,
        scenario_state: ScenarioStateManager,
        clock: SimulationClock,
        live_overlays: LiveOverlayManager | None = None,
        display_threshold: float = 0.0,
        display_floor: float = 0.13,
        display_ceiling: float = 0.75,
        display_gamma: float = 0.75,
        scenario_delta_multiplier: float = 3.0,
    ) -> None:
        self.baseline = baseline
        self.scenario_state = scenario_state
        self.clock = clock
        self.live_overlays = live_overlays
        self.display_threshold = display_threshold
        self.display_floor = display_floor
        self.display_ceiling = display_ceiling
        self.display_gamma = display_gamma
        self.scenario_delta_multiplier = scenario_delta_multiplier

    def next_frame(self) -> HeatmapFrame:
        return self.compose(tick=self.clock.current_tick, sim_time=self.clock.current_time)

    def compose(self, *, tick: int, sim_time: SimTime) -> HeatmapFrame:
        values = self.baseline.frame_for(sim_time)
        for record in self.scenario_state.active_records(tick):
            values = apply_delta(
                values,
                record.delta_store.frame_for(sim_time),
                multiplier=self.scenario_delta_multiplier,
            )
        if self.live_overlays:
            values = apply_delta(values, self.live_overlays.frame_delta(sim_time))

        cells: list[list[float | int]] = []
        for (row, col), density in sorted(values.items()):
            display_density = self.display_density(density)
            if display_density > self.display_threshold:
                cells.append([row, col, round(display_density, 6)])
        return HeatmapFrame(
            timestamp=time.time(),
            state_version=self.scenario_state.current_state_version,
            sim_time=sim_time,
            cells=cells,
        )

    def display_density(self, raw_density: float) -> float:
        if self.display_ceiling <= self.display_floor:
            return clamp(raw_density)
        scaled = (raw_density - self.display_floor) / (self.display_ceiling - self.display_floor)
        return clamp(scaled) ** self.display_gamma


def apply_delta(base: FrameValues, delta: FrameValues, *, multiplier: float = 1.0) -> FrameValues:
    if not delta:
        return dict(base)

    result = dict(base)
    for cell, value in delta.items():
        result[cell] = clamp(result.get(cell, 0.0) + value * multiplier)
        if result[cell] == 0.0:
            del result[cell]
    return result
