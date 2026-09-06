"""Server-authoritative playback state for the heatmap timelapse."""

from __future__ import annotations

from dataclasses import dataclass

from src.runtime.clock import MINUTES_PER_DAY, MINUTES_PER_WEEK, SimTime, SimulationClock


@dataclass
class PlaybackController:
    clock: SimulationClock
    frame_interval_seconds: float = 1.0
    is_playing: bool = True

    @property
    def current_tick(self) -> int:
        return self.clock.current_tick

    @property
    def current_time(self) -> SimTime:
        return self.clock.current_time

    @property
    def sim_minutes_per_second(self) -> float:
        if self.frame_interval_seconds <= 0:
            return 0.0
        return (self.clock.sim_step_seconds / 60) / self.frame_interval_seconds

    def advance(self) -> SimTime:
        if self.is_playing:
            return self.clock.advance()
        return self.clock.current_time

    def set_playing(self, is_playing: bool) -> None:
        self.is_playing = is_playing

    def set_speed(self, sim_minutes_per_second: float) -> None:
        if sim_minutes_per_second <= 0:
            raise ValueError("sim_minutes_per_second must be positive.")
        self.clock.sim_step_seconds = int(round(sim_minutes_per_second * 60 * self.frame_interval_seconds))

    def seek(
        self,
        *,
        minute_of_week: int | None = None,
        day_of_week: int | None = None,
        time_bin: int | None = None,
    ) -> SimTime:
        if minute_of_week is None:
            if day_of_week is None or time_bin is None:
                raise ValueError("Provide minute_of_week or both day_of_week and time_bin.")
            minute_of_week = (int(day_of_week) % 7) * MINUTES_PER_DAY + int(time_bin)
        return self.clock.seek(minute_of_week=minute_of_week % MINUTES_PER_WEEK)

    def to_dict(self) -> dict:
        return {
            "is_playing": self.is_playing,
            "current_tick": self.current_tick,
            "sim_step_seconds": self.clock.sim_step_seconds,
            "sim_minutes_per_second": self.sim_minutes_per_second,
            "frame_interval_seconds": self.frame_interval_seconds,
            "time_bin_minutes": self.clock.time_bin_minutes,
            "sim_time": self.current_time.to_dict(),
        }
