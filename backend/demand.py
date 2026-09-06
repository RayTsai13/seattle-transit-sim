"""Trip generation: how many trips each cell wants to make, this hour.

The whole model is two lookup tables. Each entry is a rate in **trips per
person per hour** for one of the four land-use populations, indexed by hour of
day 0..23. Multiply the rate by the people in a cell and you have that cell's
demand in trips/hour.

Rates are interpolated between adjacent hours so that scrubbing the time dial
moves the map smoothly instead of stepping between regimes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .landuse import CellLandUse
from .sim_time import SimTime


POPULATIONS = ("residents", "jobs", "students", "visitors")

# Weekday. Residents peak outbound 07-09; jobs peak on the evening return and
# again at lunch; students spread across the teaching day; visitors build
# through the afternoon and evening.
WEEKDAY_TRIP_RATES: dict[str, tuple[float, ...]] = {
    #          0      1      2      3      4      5      6      7      8      9     10     11
    #         12     13     14     15     16     17     18     19     20     21     22     23
    "residents": (
        0.010, 0.005, 0.005, 0.005, 0.010, 0.040, 0.120, 0.280, 0.300, 0.180, 0.100, 0.090,
        0.100, 0.090, 0.090, 0.110, 0.140, 0.160, 0.140, 0.100, 0.070, 0.050, 0.030, 0.020,
    ),
    "jobs": (
        0.010, 0.005, 0.005, 0.005, 0.010, 0.020, 0.050, 0.120, 0.180, 0.120, 0.080, 0.080,
        0.160, 0.120, 0.080, 0.100, 0.220, 0.300, 0.220, 0.100, 0.050, 0.030, 0.020, 0.010,
    ),
    "students": (
        0.020, 0.010, 0.005, 0.005, 0.010, 0.030, 0.080, 0.200, 0.260, 0.220, 0.180, 0.160,
        0.200, 0.180, 0.180, 0.200, 0.220, 0.200, 0.140, 0.100, 0.080, 0.060, 0.040, 0.030,
    ),
    "visitors": (
        0.010, 0.005, 0.005, 0.005, 0.005, 0.010, 0.020, 0.040, 0.060, 0.100, 0.160, 0.220,
        0.260, 0.260, 0.240, 0.240, 0.260, 0.280, 0.300, 0.260, 0.200, 0.140, 0.080, 0.040,
    ),
}

# Weekend. No commute peaks; a broad midday plateau and a much stronger
# visitor curve.
WEEKEND_TRIP_RATES: dict[str, tuple[float, ...]] = {
    "residents": (
        0.030, 0.020, 0.010, 0.010, 0.010, 0.020, 0.040, 0.060, 0.090, 0.130, 0.170, 0.190,
        0.190, 0.180, 0.170, 0.160, 0.150, 0.140, 0.130, 0.120, 0.100, 0.080, 0.060, 0.040,
    ),
    "jobs": (
        0.010, 0.005, 0.005, 0.005, 0.005, 0.010, 0.020, 0.040, 0.060, 0.080, 0.100, 0.110,
        0.120, 0.110, 0.100, 0.100, 0.100, 0.090, 0.080, 0.060, 0.040, 0.030, 0.020, 0.010,
    ),
    "students": (
        0.030, 0.020, 0.010, 0.010, 0.010, 0.010, 0.020, 0.030, 0.050, 0.080, 0.110, 0.130,
        0.140, 0.140, 0.130, 0.130, 0.130, 0.120, 0.120, 0.120, 0.110, 0.090, 0.070, 0.050,
    ),
    "visitors": (
        0.020, 0.010, 0.010, 0.005, 0.005, 0.010, 0.020, 0.030, 0.050, 0.090, 0.160, 0.240,
        0.300, 0.320, 0.320, 0.300, 0.280, 0.280, 0.280, 0.260, 0.220, 0.160, 0.100, 0.050,
    ),
}


def rates_for(sim_time: SimTime) -> dict[str, float]:
    """Interpolated trips-per-person-per-hour for each population."""
    table = WEEKEND_TRIP_RATES if sim_time.is_weekend else WEEKDAY_TRIP_RATES
    position = sim_time.minute_of_day / 60.0
    lower = int(position) % 24
    upper = (lower + 1) % 24
    fraction = position - int(position)
    return {
        name: table[name][lower] * (1.0 - fraction) + table[name][upper] * fraction
        for name in POPULATIONS
    }


@dataclass
class DemandField:
    """Turns per-cell land use into per-cell trips/hour at a given time."""

    land_use: list[CellLandUse]

    def trips_per_hour(self, sim_time: SimTime) -> list[float]:
        rates = rates_for(sim_time)
        resident_rate = rates["residents"]
        job_rate = rates["jobs"]
        student_rate = rates["students"]
        visitor_rate = rates["visitors"]
        return [
            cell.residents * resident_rate
            + cell.jobs * job_rate
            + cell.students * student_rate
            + cell.visitors * visitor_rate
            for cell in self.land_use
        ]
