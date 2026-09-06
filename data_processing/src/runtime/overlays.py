"""Lightweight live demand overlays for people drops and events."""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass

from src.runtime.clock import MINUTES_PER_WEEK, SimTime
from src.runtime.stores import FrameValues, GridConfig, clamp


@dataclass
class LiveDemandOverlay:
    overlay_id: str
    kind: str
    lat: float
    lon: float
    count: int
    created_at_real_time: float
    created_at_minute: int
    duration_minutes: int
    decay_m: float
    radius_m: float

    def to_dict(self) -> dict:
        return {
            "id": self.overlay_id,
            "kind": self.kind,
            "lat": self.lat,
            "lon": self.lon,
            "count": self.count,
            "created_at_real_time": self.created_at_real_time,
            "created_at_minute": self.created_at_minute,
            "duration_minutes": self.duration_minutes,
            "decay_m": self.decay_m,
            "radius_m": self.radius_m,
        }


class LiveOverlayManager:
    def __init__(self, config: GridConfig) -> None:
        self.config = config
        self._overlays: dict[str, LiveDemandOverlay] = {}
        self._centers = list(cell_centers(config))

    def add(
        self,
        *,
        kind: str,
        lat: float,
        lon: float,
        count: int,
        sim_time: SimTime,
        duration_minutes: int = 180,
        decay_m: float = 750.0,
        radius_m: float = 2500.0,
    ) -> LiveDemandOverlay:
        overlay = LiveDemandOverlay(
            overlay_id=str(uuid.uuid4()),
            kind=kind,
            lat=float(lat),
            lon=float(lon),
            count=max(1, int(count)),
            created_at_real_time=time.time(),
            created_at_minute=sim_time.minute_of_week,
            duration_minutes=max(30, int(duration_minutes)),
            decay_m=max(1.0, float(decay_m)),
            radius_m=max(1.0, float(radius_m)),
        )
        self._overlays[overlay.overlay_id] = overlay
        return overlay

    def remove(self, overlay_id: str) -> bool:
        return self._overlays.pop(overlay_id, None) is not None

    def clear(self) -> None:
        self._overlays.clear()

    def to_list(self) -> list[dict]:
        return [overlay.to_dict() for overlay in self._overlays.values()]

    def frame_delta(self, sim_time: SimTime) -> FrameValues:
        result: FrameValues = {}
        expired: list[str] = []
        for overlay_id, overlay in self._overlays.items():
            age = overlay_age_minutes(overlay, sim_time)
            if age > overlay.duration_minutes:
                expired.append(overlay_id)
                continue
            temporal_weight = math.exp(-age / max(30.0, overlay.duration_minutes / 2))
            peak_delta = min(0.45, 0.04 + overlay.count / 70_000)
            for row, col, lat, lon in self._centers:
                distance_m = haversine_m(overlay.lat, overlay.lon, lat, lon)
                if distance_m > overlay.radius_m:
                    continue
                spatial_weight = math.exp(-distance_m / overlay.decay_m)
                value = peak_delta * temporal_weight * spatial_weight
                if value <= 0:
                    continue
                key = (row, col)
                result[key] = clamp(result.get(key, 0.0) + value)

        for overlay_id in expired:
            self._overlays.pop(overlay_id, None)
        return result


def overlay_age_minutes(overlay: LiveDemandOverlay, sim_time: SimTime) -> int:
    return (sim_time.minute_of_week - overlay.created_at_minute) % MINUTES_PER_WEEK


def cell_centers(config: GridConfig):
    cell_width = (config.east - config.west) / config.cols
    cell_height = (config.north - config.south) / config.rows
    for row in range(config.rows):
        for col in range(config.cols):
            yield (
                row,
                col,
                config.north - (row + 0.5) * cell_height,
                config.west + (col + 0.5) * cell_width,
            )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6_371_000
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius_m * math.asin(math.sqrt(a))
