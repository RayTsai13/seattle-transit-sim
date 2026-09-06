"""Active transit network and the capacity it absorbs.

A stop serves a stated number of trips per hour, per line that calls at it.
That capacity is handed out to the cells within walking distance, in proportion
to how much demand each still has unserved. Whatever a stop cannot absorb stays
on the map as unmet demand.

The consequence, which is the point of the model: the same station cools a quiet
neighborhood completely and barely dents downtown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .geo import haversine_m
from .grid import Grid
from .landuse import CellCenter, cell_centers


VALID_SCENARIO_IDS = frozenset({"line-1", "line-1-2", "line-1-2-ballard"})
DEFAULT_SCENARIO_ID = "line-1"

# Trips per hour a single stop can absorb, per line calling at it. Light rail
# at ~8 minute headways with 4-car trains is roughly 4,500 boardings/hour per
# direction; this is deliberately below that, since not every trip in a
# walkshed is a transit trip.
STOP_CAPACITY_TRIPS_PER_HOUR = 3000.0

# How far people will walk to reach a stop.
WALK_RADIUS_M = 900.0


@dataclass(frozen=True)
class TransitStop:
    id: str
    name: str
    lon: float
    lat: float


@dataclass(frozen=True)
class TransitLine:
    id: str
    name: str
    stop_ids: tuple[str, ...]
    path: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ActiveNetwork:
    stops: tuple[TransitStop, ...]
    lines: tuple[TransitLine, ...]

    @property
    def stop_by_id(self) -> dict[str, TransitStop]:
        return {stop.id: stop for stop in self.stops}


LINE_1_STOPS: tuple[TransitStop, ...] = (
    TransitStop("northgate", "Northgate", -122.3272, 47.6992),
    TransitStop("roosevelt", "Roosevelt", -122.3167, 47.6768),
    TransitStop("u-district", "U District", -122.3155, 47.6614),
    TransitStop("uw", "UW", -122.3037, 47.6498),
    TransitStop("capitol-hill", "Capitol Hill", -122.3209, 47.6190),
    TransitStop("westlake", "Westlake", -122.3371, 47.6113),
    TransitStop("symphony", "Symphony", -122.3361, 47.6074),
    TransitStop("pioneer-square", "Pioneer Square", -122.3314, 47.6021),
    TransitStop("id-chinatown", "Intl District / Chinatown", -122.3278, 47.5983),
    TransitStop("stadium", "Stadium", -122.3275, 47.5911),
    TransitStop("sodo", "SODO", -122.3271, 47.5807),
    TransitStop("beacon-hill", "Beacon Hill", -122.3115, 47.5793),
    TransitStop("mount-baker", "Mount Baker", -122.2975, 47.5764),
    TransitStop("columbia-city", "Columbia City", -122.2922, 47.5599),
    TransitStop("othello", "Othello", -122.2812, 47.5383),
    TransitStop("rainier-beach", "Rainier Beach", -122.2688, 47.5222),
)

LINE_2_STOPS: tuple[TransitStop, ...] = (
    TransitStop("judkins-park", "Judkins Park", -122.3043, 47.5907),
    TransitStop("mercer-island", "Mercer Island", -122.2350, 47.5871),
    TransitStop("bellevue-downtown", "Bellevue Downtown", -122.1960, 47.6155),
)

BALLARD_STOPS: tuple[TransitStop, ...] = (
    TransitStop("midtown", "Midtown", -122.3322, 47.6088),
    TransitStop("denny", "Denny", -122.3405, 47.6188),
    TransitStop("south-lake-union", "South Lake Union", -122.3377, 47.6258),
    TransitStop("seattle-center", "Seattle Center", -122.3520, 47.6243),
    TransitStop("smith-cove", "Smith Cove", -122.3635, 47.6378),
    TransitStop("interbay", "Interbay", -122.3765, 47.6478),
    TransitStop("ballard", "Ballard", -122.3765, 47.6677),
)


def default_network_for_scenario(scenario_id: str) -> ActiveNetwork:
    if scenario_id not in VALID_SCENARIO_IDS:
        raise ValueError(f"Unknown scenario_id: {scenario_id!r}")

    stops = list(LINE_1_STOPS)
    lines: list[TransitLine] = [
        _line_from_stop_ids(
            "link-1-line",
            "1 Line",
            [
                "northgate",
                "roosevelt",
                "u-district",
                "uw",
                "capitol-hill",
                "westlake",
                "symphony",
                "pioneer-square",
                "id-chinatown",
                "stadium",
                "sodo",
                "beacon-hill",
                "mount-baker",
                "columbia-city",
                "othello",
                "rainier-beach",
            ],
            stops,
        )
    ]
    if scenario_id in {"line-1-2", "line-1-2-ballard"}:
        stops.extend(LINE_2_STOPS)
        lines.append(
            _line_from_stop_ids(
                "link-2-line",
                "2 Line",
                [
                    "northgate",
                    "roosevelt",
                    "u-district",
                    "uw",
                    "capitol-hill",
                    "westlake",
                    "symphony",
                    "pioneer-square",
                    "id-chinatown",
                    "judkins-park",
                    "mercer-island",
                    "bellevue-downtown",
                ],
                stops,
            )
        )
    if scenario_id == "line-1-2-ballard":
        stops.extend(BALLARD_STOPS)
        lines.append(
            _line_from_stop_ids(
                "ballard-line",
                "Ballard Line",
                [
                    "ballard",
                    "interbay",
                    "smith-cove",
                    "seattle-center",
                    "south-lake-union",
                    "denny",
                    "westlake",
                    "midtown",
                    "id-chinatown",
                    "sodo",
                ],
                stops,
            )
        )

    deduped: dict[str, TransitStop] = {}
    for stop in stops:
        deduped[stop.id] = stop
    return ActiveNetwork(stops=tuple(deduped.values()), lines=tuple(lines))


def parse_network_payload(
    *,
    scenario_id: str,
    stops_payload: Any = None,
    lines_payload: Any = None,
) -> ActiveNetwork:
    if not isinstance(stops_payload, list) or not isinstance(lines_payload, list):
        return default_network_for_scenario(scenario_id)

    stops: list[TransitStop] = []
    for raw_stop in stops_payload:
        if not isinstance(raw_stop, dict):
            continue
        coordinates = raw_stop.get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 2:
            continue
        try:
            stop = TransitStop(
                id=str(raw_stop["id"]),
                name=str(raw_stop.get("name") or raw_stop["id"]),
                lon=float(coordinates[0]),
                lat=float(coordinates[1]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        stops.append(stop)

    stop_by_id = {stop.id: stop for stop in stops}
    lines: list[TransitLine] = []
    for raw_line in lines_payload:
        if not isinstance(raw_line, dict):
            continue
        stop_ids = tuple(str(stop_id) for stop_id in raw_line.get("stopIds", ()))
        path = _path_from_payload(raw_line.get("path"))
        if len(path) < 2:
            path = tuple(
                (stop_by_id[stop_id].lon, stop_by_id[stop_id].lat)
                for stop_id in stop_ids
                if stop_id in stop_by_id
            )
        if len(path) < 2:
            continue
        lines.append(
            TransitLine(
                id=str(raw_line.get("id") or f"line-{len(lines) + 1}"),
                name=str(raw_line.get("name") or raw_line.get("id") or "Line"),
                stop_ids=stop_ids,
                path=path,
            )
        )

    if not stops or not lines:
        return default_network_for_scenario(scenario_id)
    return ActiveNetwork(stops=tuple(stops), lines=tuple(lines))


class ServiceCapacity:
    """Precomputed walksheds and per-stop capacity for the active network."""

    def __init__(self, grid: Grid, network: ActiveNetwork) -> None:
        self.grid = grid
        self.network = network
        self.centers = cell_centers(grid)
        self._line_count_by_stop_id = _line_count_by_stop_id(network)

        # Per stop: how many trips/hour it absorbs, and which cells it reaches.
        self.capacity_by_stop_id: dict[str, float] = {}
        self.walkshed_by_stop_id: dict[str, tuple[int, ...]] = {}
        for stop in network.stops:
            lines_serving = max(1, self._line_count_by_stop_id.get(stop.id, 1))
            self.capacity_by_stop_id[stop.id] = (
                STOP_CAPACITY_TRIPS_PER_HOUR * lines_serving
            )
            self.walkshed_by_stop_id[stop.id] = tuple(
                idx
                for idx, center in enumerate(self.centers)
                if haversine_m(center.lat, center.lon, stop.lat, stop.lon)
                <= WALK_RADIUS_M
            )

    @property
    def line_count_by_stop_id(self) -> dict[str, int]:
        return dict(self._line_count_by_stop_id)

    @property
    def total_capacity(self) -> float:
        return sum(self.capacity_by_stop_id.values())

    def allocate(self, demand: list[float]) -> list[float]:
        """Trips/hour served in each cell.

        Each stop distributes its capacity across its walkshed in proportion to
        the demand still unserved there, so a cell is never served more trips
        than it generates, and a cell reached by two stops draws on both.
        """
        served = [0.0] * len(demand)
        for stop in self.network.stops:
            capacity = self.capacity_by_stop_id.get(stop.id, 0.0)
            walkshed = self.walkshed_by_stop_id.get(stop.id, ())
            if capacity <= 0.0 or not walkshed:
                continue

            remaining = [max(0.0, demand[idx] - served[idx]) for idx in walkshed]
            total_remaining = sum(remaining)
            if total_remaining <= 0.0:
                continue

            share = 1.0 if capacity >= total_remaining else capacity / total_remaining
            for idx, unserved in zip(walkshed, remaining):
                served[idx] += unserved * share
        return served

    def nearest_station_distance(
        self,
        lat: float,
        lon: float,
    ) -> tuple[float, TransitStop | None]:
        nearest_distance = float("inf")
        nearest_stop: TransitStop | None = None
        for stop in self.network.stops:
            distance = haversine_m(lat, lon, stop.lat, stop.lon)
            if distance < nearest_distance:
                nearest_distance = distance
                nearest_stop = stop
        return nearest_distance, nearest_stop


def _line_from_stop_ids(
    line_id: str,
    name: str,
    stop_ids: list[str],
    stops: list[TransitStop],
) -> TransitLine:
    stop_by_id = {stop.id: stop for stop in stops}
    path = tuple(
        (stop_by_id[stop_id].lon, stop_by_id[stop_id].lat)
        for stop_id in stop_ids
        if stop_id in stop_by_id
    )
    return TransitLine(id=line_id, name=name, stop_ids=tuple(stop_ids), path=path)


def _path_from_payload(raw_path: Any) -> tuple[tuple[float, float], ...]:
    if not isinstance(raw_path, list):
        return ()
    path: list[tuple[float, float]] = []
    for raw_point in raw_path:
        if not isinstance(raw_point, (list, tuple)) or len(raw_point) != 2:
            continue
        try:
            path.append((float(raw_point[0]), float(raw_point[1])))
        except (TypeError, ValueError):
            continue
    return tuple(path)


def _line_count_by_stop_id(network: ActiveNetwork) -> dict[str, int]:
    line_count_by_stop: dict[str, int] = {}
    for line in network.lines:
        for stop_id in line.stop_ids:
            line_count_by_stop[stop_id] = line_count_by_stop.get(stop_id, 0) + 1
    return line_count_by_stop
