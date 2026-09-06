"""Simulation clock and playback state for the visual heatmap."""

from __future__ import annotations

from dataclasses import dataclass


MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
SECONDS_PER_WEEK = MINUTES_PER_WEEK * 60
DEFAULT_TIME_BIN_MINUTES = 30
DEFAULT_SIM_STEP_SECONDS = 1800


@dataclass(frozen=True)
class SimTime:
    day_of_week: int
    time_bin: int
    minute_of_week: int

    @property
    def minute_of_day(self) -> int:
        return self.minute_of_week % MINUTES_PER_DAY

    @property
    def is_weekend(self) -> bool:
        return self.day_of_week in {0, 6}

    def to_dict(self) -> dict[str, int]:
        return {
            "day_of_week": self.day_of_week,
            "time_bin": self.time_bin,
            "minute_of_week": self.minute_of_week,
        }


def sim_time_for_second(
    second_of_week: int,
    *,
    time_bin_minutes: int = DEFAULT_TIME_BIN_MINUTES,
) -> SimTime:
    minute_of_week = (second_of_week // 60) % MINUTES_PER_WEEK
    minute_of_day = minute_of_week % MINUTES_PER_DAY
    return SimTime(
        day_of_week=minute_of_week // MINUTES_PER_DAY,
        time_bin=(minute_of_day // time_bin_minutes) * time_bin_minutes,
        minute_of_week=minute_of_week,
    )


def sim_time_for_tick(
    tick: int,
    *,
    sim_step_seconds: int = DEFAULT_SIM_STEP_SECONDS,
    time_bin_minutes: int = DEFAULT_TIME_BIN_MINUTES,
) -> SimTime:
    return sim_time_for_second(
        tick * sim_step_seconds,
        time_bin_minutes=time_bin_minutes,
    )


@dataclass
class PlaybackController:
    """The simulation clock.

    The clock is stored as an absolute ``current_second_of_week`` rather than
    derived from ``current_tick * sim_step_seconds``. That separation matters:
    it means changing the playback speed re-scales how fast time passes without
    moving where the clock currently points, and it keeps the clock free of the
    quantization a coarse step size would otherwise impose.

    ``current_tick`` remains on the wire as a monotonic count of frames played.
    """

    frame_interval_seconds: float
    sim_step_seconds: int = DEFAULT_SIM_STEP_SECONDS
    time_bin_minutes: int = DEFAULT_TIME_BIN_MINUTES
    current_tick: int = 0
    is_playing: bool = True
    current_second_of_week: int = 0

    @property
    def current_time(self) -> SimTime:
        return sim_time_for_second(
            self.current_second_of_week,
            time_bin_minutes=self.time_bin_minutes,
        )

    @property
    def sim_minutes_per_second(self) -> float:
        if self.frame_interval_seconds <= 0:
            return 0.0
        return (self.sim_step_seconds / 60.0) / self.frame_interval_seconds

    def advance(self) -> SimTime:
        if self.is_playing:
            self.current_tick += 1
            self.current_second_of_week = (
                self.current_second_of_week + self.sim_step_seconds
            ) % SECONDS_PER_WEEK
        return self.current_time

    def set_playing(self, is_playing: bool) -> None:
        self.is_playing = bool(is_playing)

    def set_speed(self, sim_minutes_per_second: float) -> None:
        """Change how fast time passes, leaving the current time untouched.

        The step is quantized to whole simulation minutes. ``minute_of_week`` is
        an integer on the wire, and the contract requires
        ``sim_minutes_per_second x frame_interval_seconds`` to equal the real
        advance between playback events -- a fractional step would report a rate
        the clock never actually achieves, which the frontend's train
        interpolator reads as drift and corrects with a visible snap.
        """
        if sim_minutes_per_second <= 0:
            raise ValueError("sim_minutes_per_second must be positive")
        minutes_per_frame = sim_minutes_per_second * self.frame_interval_seconds
        self.sim_step_seconds = max(60, int(round(minutes_per_frame)) * 60)

    def seek(
        self,
        *,
        minute_of_week: int | None = None,
        day_of_week: int | None = None,
        time_bin: int | None = None,
    ) -> SimTime:
        if minute_of_week is None:
            if day_of_week is None or time_bin is None:
                raise ValueError("Provide minute_of_week or both day_of_week and time_bin")
            minute_of_week = (int(day_of_week) % 7) * MINUTES_PER_DAY + int(time_bin)
        self.current_second_of_week = (int(minute_of_week) % MINUTES_PER_WEEK) * 60
        return self.current_time

    def to_dict(self) -> dict[str, object]:
        return {
            "is_playing": self.is_playing,
            "current_tick": self.current_tick,
            "sim_step_seconds": self.sim_step_seconds,
            "sim_minutes_per_second": self.sim_minutes_per_second,
            "frame_interval_seconds": self.frame_interval_seconds,
            "time_bin_minutes": self.time_bin_minutes,
            "sim_time": self.current_time.to_dict(),
        }
