"""Simulation clock helpers for the looping demand timelapse."""

from __future__ import annotations

from dataclasses import dataclass


MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
DEFAULT_SIM_STEP_SECONDS = 300


@dataclass(frozen=True)
class SimTime:
    day_of_week: int
    time_bin: int
    minute_of_week: int

    def frame_key(self) -> tuple[int, int]:
        return self.day_of_week, self.time_bin

    def to_dict(self) -> dict[str, int]:
        return {
            "day_of_week": self.day_of_week,
            "time_bin": self.time_bin,
            "minute_of_week": self.minute_of_week,
        }


def sim_time_for_tick(
    tick: int,
    *,
    sim_step_seconds: int = DEFAULT_SIM_STEP_SECONDS,
    time_bin_minutes: int = 30,
) -> SimTime:
    """Map a monotonically increasing simulation tick to the repeating week."""
    minute_of_week = (tick * sim_step_seconds // 60) % MINUTES_PER_WEEK
    minute_of_day = minute_of_week % MINUTES_PER_DAY
    return SimTime(
        day_of_week=minute_of_week // MINUTES_PER_DAY,
        time_bin=(minute_of_day // time_bin_minutes) * time_bin_minutes,
        minute_of_week=minute_of_week,
    )


@dataclass
class SimulationClock:
    sim_step_seconds: int = DEFAULT_SIM_STEP_SECONDS
    time_bin_minutes: int = 30
    current_tick: int = 0

    def advance(self, steps: int = 1) -> SimTime:
        self.current_tick += max(1, steps)
        return self.current_time

    def seek(self, *, minute_of_week: int) -> SimTime:
        seconds = (minute_of_week % MINUTES_PER_WEEK) * 60
        self.current_tick = seconds // self.sim_step_seconds
        return self.current_time

    @property
    def current_time(self) -> SimTime:
        return sim_time_for_tick(
            self.current_tick,
            sim_step_seconds=self.sim_step_seconds,
            time_bin_minutes=self.time_bin_minutes,
        )
