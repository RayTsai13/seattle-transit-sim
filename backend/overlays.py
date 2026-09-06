"""User-dropped crowds, expressed as extra trips.

A crowd is just more people wanting to travel: ``count`` people each generating
``CROWD_TRIPS_PER_PERSON_PER_HOUR`` trips, spread over a Gaussian blob and
fading linearly over the overlay's lifetime.

Because a crowd contributes trips in the same units as resident and job demand,
it flows through the same capacity allocation in ``network.ServiceCapacity``.
A crowd dropped next to a station is partly absorbed and cools quickly; the same
crowd dropped in Magnolia is not. That behaviour is not special-cased anywhere.
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass

from .geo import haversine_m
from .landuse import CellCenter
from .sim_time import MINUTES_PER_WEEK, SimTime


# A dropped crowd is an event surge: people who all want to move at once, so
# their rate is well above an average resident's.
CROWD_TRIPS_PER_PERSON_PER_HOUR = 0.6


# Meridional meters per degree of latitude; used only to size the bounding-box
# prefilter, never for the distance itself.
_METERS_PER_DEGREE_LAT = 111_320.0


@dataclass
class CrowdOverlay:
    id: str
    kind: str
    lat: float
    lon: float
    count: int
    created_at_real_time: float
    created_at_minute: int
    duration_minutes: int
    radius_m: float
    decay_m: float
    # (cell index, share of the crowd) pairs summing to 1.0. The blob never
    # moves or changes shape, so this is computed once at creation rather than
    # re-deriving a distance to every cell in the grid on every frame.
    footprint: tuple[tuple[int, float], ...] = ()

    def to_public_dict(self, *, include_tuning: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "lat": self.lat,
            "lon": self.lon,
            "count": self.count,
        }
        if include_tuning:
            payload.update(
                {
                    "kind": self.kind,
                    "duration_minutes": self.duration_minutes,
                    "radius_m": self.radius_m,
                    "decay_m": self.decay_m,
                }
            )
        return payload


class LiveOverlayManager:
    def __init__(self, centers: list[CellCenter]) -> None:
        self.centers = centers
        self.people: dict[str, CrowdOverlay] = {}

    def add(
        self,
        *,
        lat: float,
        lon: float,
        count: int,
        sim_time: SimTime,
        kind: str | None = None,
        duration_minutes: int | None = None,
        radius_m: float | None = None,
        decay_m: float | None = None,
    ) -> CrowdOverlay:
        count = max(1, int(count))
        resolved_duration = duration_minutes
        if resolved_duration is None:
            resolved_duration = 240 if count >= 10_000 else 180
        resolved_radius = radius_m
        if resolved_radius is None:
            resolved_radius = min(4200.0, max(1450.0, 780.0 + math.sqrt(count) * 24.0))
        resolved_decay = decay_m
        if resolved_decay is None:
            resolved_decay = max(420.0, resolved_radius / 2.6)

        overlay = CrowdOverlay(
            id=f"p_{secrets.token_hex(4)}",
            kind=str(kind or "crowd"),
            lat=float(lat),
            lon=float(lon),
            count=count,
            created_at_real_time=time.time(),
            created_at_minute=sim_time.minute_of_week,
            duration_minutes=max(30, int(resolved_duration)),
            radius_m=max(250.0, float(resolved_radius)),
            decay_m=max(100.0, float(resolved_decay)),
        )
        overlay.footprint = _normalized_footprint(self.centers, overlay)
        self.people[overlay.id] = overlay
        return overlay

    def remove(self, overlay_id: str) -> None:
        if overlay_id not in self.people:
            raise KeyError(overlay_id)
        del self.people[overlay_id]

    def clear(self) -> None:
        self.people.clear()

    def trips_per_hour(self, sim_time: SimTime) -> list[float]:
        """Extra trips/hour contributed by live crowds, expiring stale ones."""
        values = [0.0] * len(self.centers)
        expired: list[str] = []

        for overlay_id, overlay in self.people.items():
            age_minutes = overlay_age_minutes(overlay, sim_time)
            if age_minutes >= overlay.duration_minutes:
                expired.append(overlay_id)
                continue
            self._add_overlay_trips(values, overlay, age_minutes)

        for overlay_id in expired:
            self.people.pop(overlay_id, None)
        return values

    def _add_overlay_trips(
        self,
        values: list[float],
        overlay: CrowdOverlay,
        age_minutes: int,
    ) -> None:
        remaining = 1.0 - (age_minutes / overlay.duration_minutes)
        total_trips = overlay.count * CROWD_TRIPS_PER_PERSON_PER_HOUR * remaining
        if total_trips <= 0.0:
            return

        # The footprint is already normalized, so the crowd contributes exactly
        # `total_trips` no matter how the blob happens to land on the grid.
        for idx, share in overlay.footprint:
            values[idx] += total_trips * share


def _normalized_footprint(
    centers: list[CellCenter],
    overlay: CrowdOverlay,
) -> tuple[tuple[int, float], ...]:
    """Per-cell shares of a crowd's Gaussian blob, summing to 1.0.

    A degree-space bounding box rejects almost every cell before the haversine
    runs, and the result is reused for the overlay's whole lifetime.
    """
    reach_m = overlay.radius_m * 2.0
    lat_span = reach_m / _METERS_PER_DEGREE_LAT
    cos_lat = max(0.01, math.cos(math.radians(overlay.lat)))
    lon_span = reach_m / (_METERS_PER_DEGREE_LAT * cos_lat)
    min_lat, max_lat = overlay.lat - lat_span, overlay.lat + lat_span
    min_lon, max_lon = overlay.lon - lon_span, overlay.lon + lon_span

    weights: list[tuple[int, float]] = []
    total_weight = 0.0
    for idx, center in enumerate(centers):
        if not (min_lat <= center.lat <= max_lat and min_lon <= center.lon <= max_lon):
            continue
        distance_m = haversine_m(overlay.lat, overlay.lon, center.lat, center.lon)
        if distance_m > reach_m:
            continue
        weight = math.exp(-((distance_m / overlay.decay_m) ** 2))
        if weight <= 1e-6:
            continue
        weights.append((idx, weight))
        total_weight += weight

    if total_weight <= 0.0:
        return ()
    return tuple((idx, weight / total_weight) for idx, weight in weights)


def overlay_age_minutes(overlay: CrowdOverlay, sim_time: SimTime) -> int:
    return (sim_time.minute_of_week - overlay.created_at_minute) % MINUTES_PER_WEEK
