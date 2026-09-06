"""Frame composition: demand, minus what transit absorbs, is what you see."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .demand import DemandField
from .geo import clamp
from .network import ServiceCapacity
from .overlays import LiveOverlayManager
from .sim_time import SimTime


# Unmet trips/hour in a single cell that reads as full saturation on the map.
# This is the physical calibration: it fixes what "1.0" means, and it does not
# move between frames, so building transit visibly cools the city instead of
# being renormalized away.
MAX_UNMET_TRIPS_PER_CELL_HOUR = 2400.0

# Cosmetic shaping only. 1.0 is linear; lower values lift the midtones if the
# map reads too dark. Adjust this, not MAX_UNMET_TRIPS_PER_CELL_HOUR, for looks.
DENSITY_GAMMA = 0.85

# Cells at or below this display density are omitted from the frame.
DISPLAY_THRESHOLD = 0.01


@dataclass
class HeatmapFrame:
    timestamp: float
    state_version: str
    sim_time: SimTime
    cells: list[list[int | float]]

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "state_version": self.state_version,
            "sim_time": self.sim_time.to_dict(),
            "cells": self.cells,
        }


class FrameComposer:
    def __init__(
        self,
        *,
        cols: int,
        max_unmet_trips: float = MAX_UNMET_TRIPS_PER_CELL_HOUR,
        gamma: float = DENSITY_GAMMA,
        display_threshold: float = DISPLAY_THRESHOLD,
    ) -> None:
        self.cols = cols
        self.max_unmet_trips = max_unmet_trips
        self.gamma = gamma
        self.display_threshold = display_threshold

    def compose(
        self,
        *,
        sim_time: SimTime,
        state_version: str,
        demand_field: DemandField,
        service: ServiceCapacity,
        overlays: LiveOverlayManager,
    ) -> HeatmapFrame:
        base = demand_field.trips_per_hour(sim_time)
        crowds = overlays.trips_per_hour(sim_time)
        demand = [b + c for b, c in zip(base, crowds)]

        served = service.allocate(demand)
        unmet = [max(0.0, d - s) for d, s in zip(demand, served)]

        return HeatmapFrame(
            timestamp=time.time(),
            state_version=state_version,
            sim_time=sim_time,
            cells=self._to_sparse_cells(unmet),
        )

    def _to_sparse_cells(self, unmet: list[float]) -> list[list[int | float]]:
        cells: list[list[int | float]] = []
        for idx, trips in enumerate(unmet):
            density = clamp(trips / self.max_unmet_trips) ** self.gamma
            if density <= self.display_threshold:
                continue
            cells.append([idx // self.cols, idx % self.cols, round(density, 3)])
        return cells
