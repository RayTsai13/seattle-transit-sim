"""In-memory backend state shared across all SSE connections.

Tracks the active scenario and any user-placed groups of people, and exposes
an asyncio condition so SSE streams can wake up immediately on changes
instead of waiting for the next frame tick.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

from .grid import Grid

VALID_SCENARIO_IDS = frozenset({"line-1", "line-1-2", "line-1-2-ballard"})
DEFAULT_SCENARIO_ID = "line-1"

# Density boost contributed per person at their cell.
# The composed cell is clamped to 1.0, so this is a per-person additive nudge,
# not a multiplier. Tune as the model is wired in.
PERSON_DENSITY_PER_COUNT = 0.02


@dataclass
class Person:
    id: str
    lat: float
    lon: float
    count: int


class State:
    """Process-wide state. Single instance per server."""

    def __init__(self, grid: Grid) -> None:
        self.grid = grid
        self.scenario_id: str = DEFAULT_SCENARIO_ID
        self.people: dict[str, Person] = {}
        self._version: int = 0
        # Created lazily on first use so the condition binds to the running
        # event loop — important on Python 3.9 where asyncio primitives latch
        # onto the loop at construction time.
        self._cond: asyncio.Condition | None = None

    @property
    def version(self) -> int:
        return self._version

    def _ensure_cond(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def notify_change(self) -> None:
        """Bump version + wake everyone waiting in :meth:`wait_for_change`."""
        self._version += 1
        cond = self._ensure_cond()
        async with cond:
            cond.notify_all()

    async def wait_for_change(self, last_version: int, timeout: float) -> int:
        """Block until ``version > last_version`` or ``timeout`` elapses.

        Returns the current version regardless of why we woke up, so the
        caller can pass it back on the next call.
        """
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
        bounds = self.grid.bounds
        in_bounds = (
            bounds.west <= lon <= bounds.east
            and bounds.south <= lat <= bounds.north
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

    def compose_frame_cells(self) -> list[list[int | float]]:
        """Return the sparse ``[[row, col, density], ...]`` payload for a frame.

        Combines the static grid density with people boosts. This is where the
        live simulation model would plug in once it exists — for now the grid
        is static and people simply add to whichever cell they land in.
        """
        grid = self.grid
        bounds = grid.bounds
        cell_w = (bounds.east - bounds.west) / grid.cols
        cell_h = (bounds.north - bounds.south) / grid.rows

        density = [row[:] for row in grid.density]

        for person in self.people.values():
            col = int((person.lon - bounds.west) / cell_w)
            row = int((bounds.north - person.lat) / cell_h)
            if 0 <= row < grid.rows and 0 <= col < grid.cols:
                boost = PERSON_DENSITY_PER_COUNT * person.count
                density[row][col] = min(1.0, density[row][col] + boost)

        cells: list[list[int | float]] = []
        for r in range(grid.rows):
            row_values = density[r]
            for c in range(grid.cols):
                value = row_values[c]
                if value > 0.0:
                    cells.append([r, c, round(value, 3)])
        return cells
