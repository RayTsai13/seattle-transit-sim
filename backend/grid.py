"""Load a heatmap grid from a GeoJSON file.

The GeoJSON is expected to be a ``FeatureCollection`` of polygon cells where
each feature carries:

- ``properties.cell_id``: string of form ``r{row}_c{col}`` (zero-padded ints)
- one or more numeric ``properties.<name>`` fields used as density signals

The grid bounds, ``rows``, and ``cols`` are inferred from the cell ids and the
union of polygon coordinates, so the loader works with any rectangular grid
without hard-coded geometry.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CELL_ID_RE = re.compile(r"^r(\d+)_c(\d+)$")


@dataclass(frozen=True)
class Bounds:
    west: float
    south: float
    east: float
    north: float

    def to_dict(self) -> dict[str, float]:
        return {
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
        }


@dataclass
class Grid:
    bounds: Bounds
    rows: int
    cols: int
    # Dense rows x cols matrix of floats in [0, 1].
    density: list[list[float]]

    def config(self) -> dict:
        """Return the payload sent on the SSE ``config`` event."""
        return {
            "bounds": self.bounds.to_dict(),
            "rows": self.rows,
            "cols": self.cols,
        }


def _parse_cell_id(cell_id: str) -> tuple[int, int]:
    match = CELL_ID_RE.match(cell_id)
    if not match:
        raise ValueError(f"Unexpected cell_id format: {cell_id!r}")
    return int(match.group(1)), int(match.group(2))


def _polygon_bbox(coordinates) -> tuple[float, float, float, float]:
    lons: list[float] = []
    lats: list[float] = []
    for ring in coordinates:
        for lon, lat in ring:
            lons.append(lon)
            lats.append(lat)
    return min(lons), min(lats), max(lons), max(lats)


def _polygon_size(coordinates) -> tuple[float, float]:
    w, s, e, n = _polygon_bbox(coordinates)
    return e - w, n - s


def load_grid(
    path: str | Path,
    density_property: str = "congestion_score",
) -> Grid:
    """Load a grid from a GeoJSON file and normalize density to ``[0, 1]``.

    The contract assumes a uniform grid with ``row 0`` at the north edge.
    Real source files (like ``seattle_heatmap_grid.geojson``) sometimes:

    - clip the easternmost column / northernmost row to the bounding box,
      producing partial cells whose centroids would not match the contract's
      ``west + (col + 0.5) * cell_w`` formula
    - index ``row 0`` from the south instead of the north

    To honor the contract, this loader:

    1. Detects the canonical cell size from the cell ``(0, 0)``.
    2. Drops trailing rows / cols whose polygon dimensions don't match the
       canonical size (i.e. partial edge cells).
    3. Detects the source's row orientation and flips it if needed so that
       row ``0`` is the northernmost strip in the returned grid.

    Cells missing the requested property contribute ``0``.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    features = data.get("features") or []
    if not features:
        raise ValueError(f"No features in {path}")

    by_cell: dict[tuple[int, int], dict] = {}
    raw_max_row = raw_max_col = 0
    for feature in features:
        row, col = _parse_cell_id(feature["properties"]["cell_id"])
        by_cell[(row, col)] = feature
        if row > raw_max_row:
            raw_max_row = row
        if col > raw_max_col:
            raw_max_col = col

    f00 = by_cell.get((0, 0))
    if f00 is None:
        raise ValueError("Source GeoJSON is missing cell (0, 0); cannot infer cell size")
    cell_w, cell_h = _polygon_size(f00["geometry"]["coordinates"])

    def _matches_canonical(row: int, col: int) -> bool:
        feature = by_cell.get((row, col))
        if feature is None:
            return False
        w, h = _polygon_size(feature["geometry"]["coordinates"])
        return abs(w - cell_w) < 1e-9 and abs(h - cell_h) < 1e-9

    full_rows = raw_max_row + 1
    while full_rows > 1 and not _matches_canonical(full_rows - 1, 0):
        full_rows -= 1
    full_cols = raw_max_col + 1
    while full_cols > 1 and not _matches_canonical(0, full_cols - 1):
        full_cols -= 1

    src_west, src_south, _, src_north = _polygon_bbox(f00["geometry"]["coordinates"])

    f_top_left = by_cell.get((full_rows - 1, 0))
    if f_top_left is None:
        raise ValueError("Could not locate northernmost full cell")
    _, _, _, src_north_top = _polygon_bbox(f_top_left["geometry"]["coordinates"])
    source_row_zero_is_north = src_north >= src_north_top

    if source_row_zero_is_north:
        north = src_north
        south = north - full_rows * cell_h
    else:
        south = src_south
        north = south + full_rows * cell_h

    west = src_west
    east = west + full_cols * cell_w

    density: list[list[float]] = [[0.0] * full_cols for _ in range(full_rows)]
    raw_max = 0.0
    for (src_row, src_col), feature in by_cell.items():
        if src_row >= full_rows or src_col >= full_cols:
            continue
        contract_row = src_row if source_row_zero_is_north else (full_rows - 1 - src_row)
        value = float(feature["properties"].get(density_property) or 0.0)
        density[contract_row][src_col] = value
        if value > raw_max:
            raw_max = value

    if raw_max > 0:
        for row_values in density:
            for col_idx in range(full_cols):
                row_values[col_idx] = row_values[col_idx] / raw_max

    return Grid(
        bounds=Bounds(west=west, south=south, east=east, north=north),
        rows=full_rows,
        cols=full_cols,
        density=density,
    )
