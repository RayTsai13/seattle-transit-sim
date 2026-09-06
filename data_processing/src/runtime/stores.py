"""Frame stores for baseline demand and scenario deltas."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.runtime.clock import SimTime


CellKey = tuple[int, int]
FrameKey = tuple[int, int]
FrameValues = dict[CellKey, float]

DISPLAY_DENSITY_COLUMNS = (
    "demand_score",
    "scenario_demand_score",
    "relative_demand_pressure",
    "baseline_demand_score",
)
DELTA_COLUMNS = (
    "demand_delta",
    "scenario_demand_pressure_raw",
    "scenario_demand_score",
    "scenario_demand_pressure",
)
BASELINE_DELTA_COLUMNS = (
    "baseline_demand_pressure_raw",
    "baseline_demand_score",
    "relative_demand_pressure",
)


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


@dataclass(frozen=True)
class GridConfig:
    west: float
    south: float
    east: float
    north: float
    rows: int
    cols: int

    def to_sse_config(self) -> dict:
        return {
            "bounds": {
                "west": self.west,
                "south": self.south,
                "east": self.east,
                "north": self.north,
            },
            "rows": self.rows,
            "cols": self.cols,
        }


@dataclass
class DemandFrameStore:
    """Sparse time-indexed demand frames keyed by `(day_of_week, time_bin)`."""

    config: GridConfig
    frames: dict[FrameKey, FrameValues]
    loaded_from: Path
    value_column: str

    @classmethod
    def from_predictions_csv(
        cls,
        path: Path,
        *,
        value_column: str | None = None,
        threshold: float = 0.0,
        chunksize: int = 100_000,
    ) -> "DemandFrameStore":
        if not path.exists():
            raise FileNotFoundError(f"Baseline predictions not found: {path}")

        header = pd.read_csv(path, nrows=0)
        selected_value_column = value_column or first_present(
            header.columns,
            DISPLAY_DENSITY_COLUMNS,
        )
        required = {"row", "col", "day_of_week", "time_bin", selected_value_column}
        missing = required.difference(header.columns)
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")

        bounds_columns = ["min_lon", "min_lat", "max_lon", "max_lat"]
        usecols = sorted(required.union(set(bounds_columns).intersection(header.columns)))
        frames: dict[FrameKey, FrameValues] = {}
        rows_seen: set[int] = set()
        cols_seen: set[int] = set()
        west = float("inf")
        south = float("inf")
        east = float("-inf")
        north = float("-inf")

        for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize):
            chunk = chunk.copy()
            chunk["row"] = pd.to_numeric(chunk["row"], errors="coerce").fillna(-1).astype(int)
            chunk["col"] = pd.to_numeric(chunk["col"], errors="coerce").fillna(-1).astype(int)
            chunk["day_of_week"] = pd.to_numeric(chunk["day_of_week"], errors="coerce").fillna(0).astype(int)
            chunk["time_bin"] = pd.to_numeric(chunk["time_bin"], errors="coerce").fillna(0).astype(int)
            chunk[selected_value_column] = pd.to_numeric(chunk[selected_value_column], errors="coerce").fillna(0.0)

            if set(bounds_columns).issubset(chunk.columns):
                west = min(west, float(chunk["min_lon"].min()))
                south = min(south, float(chunk["min_lat"].min()))
                east = max(east, float(chunk["max_lon"].max()))
                north = max(north, float(chunk["max_lat"].max()))

            for row in chunk.itertuples(index=False):
                grid_row = int(getattr(row, "row"))
                grid_col = int(getattr(row, "col"))
                density = clamp(float(getattr(row, selected_value_column)))
                rows_seen.add(grid_row)
                cols_seen.add(grid_col)
                if density <= threshold:
                    continue
                frame_key = (int(getattr(row, "day_of_week")), int(getattr(row, "time_bin")))
                frames.setdefault(frame_key, {})[(grid_row, grid_col)] = density

        if not rows_seen or not cols_seen:
            raise ValueError(f"No grid rows found in {path}")
        if west == float("inf"):
            raise ValueError(f"{path} must include grid bound columns for SSE config")

        return cls(
            config=GridConfig(
                west=west,
                south=south,
                east=east,
                north=north,
                rows=max(rows_seen) + 1,
                cols=max(cols_seen) + 1,
            ),
            frames=frames,
            loaded_from=path,
            value_column=selected_value_column,
        )

    def frame_for(self, sim_time: SimTime) -> FrameValues:
        return dict(self.frames.get(sim_time.frame_key(), {}))


@dataclass
class ScenarioDeltaStore:
    frames: dict[FrameKey, FrameValues]
    loaded_from: Path
    delta_column: str

    @classmethod
    def from_csv(
        cls,
        path: Path,
        *,
        threshold: float = 0.0,
        chunksize: int = 100_000,
    ) -> "ScenarioDeltaStore":
        if not path.exists():
            raise FileNotFoundError(f"Scenario delta CSV not found: {path}")

        header = pd.read_csv(path, nrows=0)
        required = {"row", "col", "day_of_week", "time_bin"}
        missing = required.difference(header.columns)
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")

        delta_column, baseline_column = delta_columns_for(header.columns)
        usecols = sorted(required.union({delta_column}, {baseline_column} if baseline_column else set()))
        frames: dict[FrameKey, FrameValues] = {}

        for chunk in pd.read_csv(path, usecols=usecols, chunksize=chunksize):
            chunk = chunk.copy()
            chunk["row"] = pd.to_numeric(chunk["row"], errors="coerce").fillna(-1).astype(int)
            chunk["col"] = pd.to_numeric(chunk["col"], errors="coerce").fillna(-1).astype(int)
            chunk["day_of_week"] = pd.to_numeric(chunk["day_of_week"], errors="coerce").fillna(0).astype(int)
            chunk["time_bin"] = pd.to_numeric(chunk["time_bin"], errors="coerce").fillna(0).astype(int)
            delta_values = pd.to_numeric(chunk[delta_column], errors="coerce").fillna(0.0)
            if baseline_column:
                baseline_values = pd.to_numeric(chunk[baseline_column], errors="coerce").fillna(0.0)
                delta_values = delta_values - baseline_values

            for row, delta in zip(chunk.itertuples(index=False), delta_values, strict=True):
                delta = float(delta)
                if abs(delta) <= threshold:
                    continue
                frame_key = (int(getattr(row, "day_of_week")), int(getattr(row, "time_bin")))
                cell_key = (int(getattr(row, "row")), int(getattr(row, "col")))
                frames.setdefault(frame_key, {})[cell_key] = delta

        return cls(frames=frames, loaded_from=path, delta_column=delta_column)

    def frame_for(self, sim_time: SimTime) -> FrameValues:
        return self.frames.get(sim_time.frame_key(), {})

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def changed_cells(self) -> int:
        return sum(len(frame) for frame in self.frames.values())


def first_present(columns: Iterable[str], candidates: Iterable[str]) -> str:
    column_set = set(columns)
    for candidate in candidates:
        if candidate in column_set:
            return candidate
    raise ValueError(f"None of these columns are present: {', '.join(candidates)}")


def delta_columns_for(columns: Iterable[str]) -> tuple[str, str | None]:
    column_set = set(columns)
    if "demand_delta" in column_set:
        return "demand_delta", None
    if {"scenario_demand_pressure_raw", "baseline_demand_pressure_raw"}.issubset(column_set):
        return "scenario_demand_pressure_raw", "baseline_demand_pressure_raw"
    if {"scenario_demand_score", "baseline_demand_score"}.issubset(column_set):
        return "scenario_demand_score", "baseline_demand_score"
    if {"scenario_demand_pressure", "relative_demand_pressure"}.issubset(column_set):
        return "scenario_demand_pressure", "relative_demand_pressure"
    raise ValueError(
        "Scenario CSV must contain demand_delta or scenario/baseline score columns."
    )
