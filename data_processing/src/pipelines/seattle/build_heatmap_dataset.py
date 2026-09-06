#!/usr/bin/env python3
"""Build a Seattle foot-traffic heatmap feature dataset.

The pipeline writes Seattle heatmap feature and grid artifacts to the processed directory.

The output is a proxy congestion surface, not measured pedestrian volume.
Observed public foot-traffic data is sparse, so the score blends transit
frequency, bicycle counts, and optional employment/event features.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from shapely.geometry import shape

from src.common.artifacts import (
    DEFAULT_GTFS_DIR,
    DEFAULT_PROCESSED_DIR,
    DEFAULT_RAW_DIR,
    FREMONT_BRIDGE_COUNTS_CSV,
    PUGET_SOUND_GTFS_ZIP,
    SEATTLE_HEATMAP_FEATURES_CSV,
    SEATTLE_HEATMAP_GRID_GEOJSON,
    TRANSIT_ACCESSIBILITY_CSV,
    US_COUNTIES_GEOJSON,
)
from src.common.gtfs_utils import (
    DEFAULT_SEATTLE_STATION_AGENCY_IDS,
    DEFAULT_STATION_ROUTE_TYPES,
    filtered_stop_ids,
    filtered_trip_ids,
    parse_agency_ids,
    parse_route_types,
)


SEATTLE_BBOX = (-122.4597, 47.4810, -122.2244, 47.7340)
GTFS_URL = "https://gtfs.sound.obaweb.org/prod/gtfs_puget_sound_consolidated.zip"
FREMONT_CSV_URL = "https://data.seattle.gov/resource/65db-xm6k.csv"
TRANSIT_ACCESS_CSV_URL = "https://performance.seattle.gov/resource/pmj3-v6fx.csv"
LODES_BASE_URL = "https://lehd.ces.census.gov/data/lodes/LODES8/wa"
CENSUS_TRACT_GEOJSON_URL = (
    "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
)


@dataclass(frozen=True)
class GridSpec:
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    cell_size_m: int
    lat_step: float
    lon_step: float
    rows: int
    cols: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Seattle heatmap CSV and GeoJSON feature outputs."
    )
    parser.add_argument("--gtfs-dir", default=str(DEFAULT_GTFS_DIR), help="Directory containing GTFS txt files.")
    parser.add_argument(
        "--download-gtfs",
        action="store_true",
        help="Download Sound Transit consolidated GTFS if --gtfs-dir is missing.",
    )
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Raw data cache directory.")
    parser.add_argument("--out-dir", default=str(DEFAULT_PROCESSED_DIR), help="Processed output directory.")
    parser.add_argument("--cell-size-m", type=int, default=500, help="Grid cell size in meters.")
    parser.add_argument(
        "--route-types",
        default=",".join(str(route_type) for route_type in DEFAULT_STATION_ROUTE_TYPES),
        help="Comma-separated GTFS route_type values to keep. Default keeps rail/station modes: 0,1,2.",
    )
    parser.add_argument(
        "--agency-ids",
        default=",".join(DEFAULT_SEATTLE_STATION_AGENCY_IDS),
        help="Comma-separated GTFS agency_id values to keep. Default keeps Sound Transit: 40.",
    )
    parser.add_argument(
        "--fremont-limit",
        type=int,
        default=50000,
        help="Maximum Fremont Bridge hourly rows to fetch from Socrata.",
    )
    parser.add_argument(
        "--include-lehd",
        action="store_true",
        help="Download and add tract-level LEHD workplace job estimates. This is slower.",
    )
    parser.add_argument("--lehd-year", type=int, default=2022, help="LODES year to use.")
    parser.add_argument(
        "--optional-counts-csv",
        help="Optional local count CSV with lat, lon, datetime, and count columns.",
    )
    return parser.parse_args()


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def download_file(url: str, destination: Path) -> Path:
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    response = requests.get(url, timeout=90)
    response.raise_for_status()
    destination.write_bytes(response.content)
    return destination


def maybe_download_gtfs(gtfs_dir: Path, raw_dir: Path, should_download: bool) -> Path:
    required = ["stops.txt", "stop_times.txt", "trips.txt"]
    if all((gtfs_dir / name).exists() for name in required):
        return gtfs_dir
    if not should_download:
        raise FileNotFoundError(
            f"Missing GTFS files in {gtfs_dir}. Pass --download-gtfs or --gtfs-dir."
        )

    zip_path = download_file(GTFS_URL, raw_dir / PUGET_SOUND_GTFS_ZIP)
    gtfs_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(gtfs_dir)
    return gtfs_dir


def make_grid_spec(bbox: tuple[float, float, float, float], cell_size_m: int) -> GridSpec:
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = (min_lat + max_lat) / 2
    lat_step = cell_size_m / 111_320
    lon_step = cell_size_m / (111_320 * math.cos(math.radians(mid_lat)))
    rows = math.ceil((max_lat - min_lat) / lat_step)
    cols = math.ceil((max_lon - min_lon) / lon_step)
    return GridSpec(min_lon, min_lat, max_lon, max_lat, cell_size_m, lat_step, lon_step, rows, cols)


def cell_id_for(lat: float, lon: float, spec: GridSpec) -> str | None:
    if lon < spec.min_lon or lon > spec.max_lon or lat < spec.min_lat or lat > spec.max_lat:
        return None
    row = int((lat - spec.min_lat) / spec.lat_step)
    col = int((lon - spec.min_lon) / spec.lon_step)
    if row < 0 or col < 0 or row >= spec.rows or col >= spec.cols:
        return None
    return f"r{row:03d}_c{col:03d}"


def iter_grid_rows(spec: GridSpec) -> Iterable[dict]:
    for row in range(spec.rows):
        for col in range(spec.cols):
            min_lat = spec.min_lat + row * spec.lat_step
            max_lat = min(min_lat + spec.lat_step, spec.max_lat)
            min_lon = spec.min_lon + col * spec.lon_step
            max_lon = min(min_lon + spec.lon_step, spec.max_lon)
            yield {
                "cell_id": f"r{row:03d}_c{col:03d}",
                "row": row,
                "col": col,
                "center_lat": (min_lat + max_lat) / 2,
                "center_lon": (min_lon + max_lon) / 2,
                "min_lat": min_lat,
                "min_lon": min_lon,
                "max_lat": max_lat,
                "max_lon": max_lon,
            }


def build_grid(cell_size_m: int) -> tuple[pd.DataFrame, GridSpec]:
    spec = make_grid_spec(SEATTLE_BBOX, cell_size_m)
    return pd.DataFrame(iter_grid_rows(spec)), spec


def parse_gtfs_hour(value: object) -> int | None:
    if pd.isna(value):
        return None
    try:
        hour = int(str(value).split(":", 1)[0])
    except ValueError:
        return None
    return hour % 24


def load_gtfs_frequency(
    gtfs_dir: Path,
    spec: GridSpec,
    route_types: tuple[int, ...] | None,
    agency_ids: tuple[str, ...] | None,
) -> pd.DataFrame:
    stops = pd.read_csv(gtfs_dir / "stops.txt", usecols=["stop_id", "stop_lat", "stop_lon"])
    stop_ids = filtered_stop_ids(gtfs_dir, route_types, agency_ids=agency_ids)
    if stop_ids is not None:
        stops = stops[stops["stop_id"].astype("string").isin(stop_ids)]
    stops["cell_id"] = [
        cell_id_for(lat, lon, spec) for lat, lon in zip(stops["stop_lat"], stops["stop_lon"])
    ]
    stops = stops.dropna(subset=["cell_id"])
    if stops.empty:
        return pd.DataFrame(columns=["cell_id", "hour", "transit_departures", "unique_stops"])

    stop_times = pd.read_csv(
        gtfs_dir / "stop_times.txt",
        usecols=["trip_id", "departure_time", "stop_id"],
        dtype={"trip_id": "string", "stop_id": "string", "departure_time": "string"},
    )
    trip_ids = filtered_trip_ids(gtfs_dir, route_types, agency_ids=agency_ids)
    if trip_ids is not None:
        stop_times = stop_times[stop_times["trip_id"].isin(trip_ids)]
    stop_times["hour"] = stop_times["departure_time"].map(parse_gtfs_hour)
    stop_times = stop_times.dropna(subset=["hour"])
    stop_times["hour"] = stop_times["hour"].astype(int)

    stops["stop_id"] = stops["stop_id"].astype("string")
    joined = stop_times.merge(stops[["stop_id", "cell_id"]], on="stop_id", how="inner")
    grouped = (
        joined.groupby(["cell_id", "hour"])
        .agg(transit_departures=("trip_id", "count"), unique_stops=("stop_id", "nunique"))
        .reset_index()
    )
    return grouped


def fetch_socrata_csv(url: str, raw_path: Path, params: dict[str, object]) -> pd.DataFrame:
    if raw_path.exists() and raw_path.stat().st_size > 0:
        return pd.read_csv(raw_path)
    response = requests.get(url, params=params, timeout=90)
    response.raise_for_status()
    raw_path.write_text(response.text)
    return pd.read_csv(raw_path)


def load_fremont_counts(raw_dir: Path, limit: int, spec: GridSpec) -> pd.DataFrame:
    params = {
        "$limit": limit,
        "$order": "date DESC",
        "$select": "date,fremont_bridge,fremont_bridge_nb,fremont_bridge_sb",
    }
    counts = fetch_socrata_csv(FREMONT_CSV_URL, raw_dir / FREMONT_BRIDGE_COUNTS_CSV, params)
    if counts.empty:
        return pd.DataFrame(columns=["cell_id", "hour", "observed_count", "bike_count_proxy"])

    counts["datetime"] = pd.to_datetime(counts["date"], errors="coerce")
    counts["hour"] = counts["datetime"].dt.hour
    counts["day_of_week"] = counts["datetime"].dt.dayofweek
    counts["observed_count"] = pd.to_numeric(counts["fremont_bridge"], errors="coerce").fillna(0)

    fremont_cell = cell_id_for(47.6480, -122.3495, spec)
    if fremont_cell is None:
        return pd.DataFrame(columns=["cell_id", "hour", "observed_count", "bike_count_proxy"])
    counts["cell_id"] = fremont_cell
    return (
        counts.groupby(["cell_id", "hour", "day_of_week"])
        .agg(observed_count=("observed_count", "mean"), bike_count_proxy=("observed_count", "mean"))
        .reset_index()
    )


def load_transit_accessibility(raw_dir: Path) -> float:
    try:
        params = {"$limit": 5000}
        access = fetch_socrata_csv(
            TRANSIT_ACCESS_CSV_URL, raw_dir / TRANSIT_ACCESSIBILITY_CSV, params
        )
    except Exception:
        return 0.0
    if access.empty:
        return 0.0

    numeric_columns = []
    for column in access.columns:
        values = pd.to_numeric(access[column], errors="coerce")
        if values.notna().any():
            numeric_columns.append((column, values))
    if not numeric_columns:
        return 0.0

    _, values = numeric_columns[-1]
    latest_value = values.dropna().iloc[-1] if not values.dropna().empty else 0.0
    return float(latest_value)


def load_optional_counts(path: str | None, spec: GridSpec) -> pd.DataFrame:
    if not path:
        return pd.DataFrame(columns=["cell_id", "hour", "day_of_week", "observed_count"])
    counts = pd.read_csv(path)
    required = {"lat", "lon", "datetime", "count"}
    if not required.issubset(counts.columns):
        raise ValueError(f"{path} must include columns: {', '.join(sorted(required))}")
    counts["datetime"] = pd.to_datetime(counts["datetime"], errors="coerce")
    counts["hour"] = counts["datetime"].dt.hour
    counts["day_of_week"] = counts["datetime"].dt.dayofweek
    counts["observed_count"] = pd.to_numeric(counts["count"], errors="coerce").fillna(0)
    counts["cell_id"] = [cell_id_for(lat, lon, spec) for lat, lon in zip(counts["lat"], counts["lon"])]
    counts = counts.dropna(subset=["cell_id", "hour", "day_of_week"])
    return (
        counts.groupby(["cell_id", "hour", "day_of_week"])
        .agg(observed_count=("observed_count", "mean"))
        .reset_index()
    )


def load_lehd_jobs(raw_dir: Path, year: int, spec: GridSpec) -> pd.DataFrame:
    """Approximate LEHD workplace jobs by assigning King County tract centroids to grid cells."""
    wac_path = raw_dir / f"wa_wac_S000_JT00_{year}.csv.gz"
    xwalk_path = raw_dir / "wa_xwalk.csv.gz"
    download_file(f"{LODES_BASE_URL}/wac/wa_wac_S000_JT00_{year}.csv.gz", wac_path)
    download_file(f"{LODES_BASE_URL}/wa_xwalk.csv.gz", xwalk_path)

    with gzip.open(wac_path, "rt") as handle:
        wac = pd.read_csv(handle, usecols=["w_geocode", "C000"], dtype={"w_geocode": "string"})
    with gzip.open(xwalk_path, "rt") as handle:
        xwalk = pd.read_csv(handle, usecols=["tabblk2020", "cty", "trct"], dtype="string")

    king = xwalk[xwalk["cty"] == "033"].copy()
    king["tract_geoid"] = "53" + king["cty"] + king["trct"]
    jobs = wac.merge(king, left_on="w_geocode", right_on="tabblk2020", how="inner")
    tract_jobs = jobs.groupby("tract_geoid", as_index=False)["C000"].sum()

    # The GitHub file is small and stable enough for a hackathon; it provides county geometries.
    # If tract centroids are unavailable, county centroid still gives a useful regional pressure flag.
    county_geojson = download_file(CENSUS_TRACT_GEOJSON_URL, raw_dir / US_COUNTIES_GEOJSON)
    geo = json.loads(county_geojson.read_text())
    king_feature = next(
        (
            feature
            for feature in geo["features"]
            if feature.get("id") == "53033"
            or feature.get("properties", {}).get("GEO_ID", "").endswith("53033")
        ),
        None,
    )
    if king_feature is None:
        return pd.DataFrame(columns=["cell_id", "employment_jobs"])

    centroid = shape(king_feature["geometry"]).centroid
    cell_id = cell_id_for(centroid.y, centroid.x, spec)
    if cell_id is None:
        return pd.DataFrame(columns=["cell_id", "employment_jobs"])
    return pd.DataFrame(
        [{"cell_id": cell_id, "employment_jobs": float(tract_jobs["C000"].sum())}]
    )


def min_max(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").fillna(0)
    span = values.max() - values.min()
    if span == 0:
        return values * 0
    return (values - values.min()) / span


def assemble_features(
    grid: pd.DataFrame,
    gtfs_frequency: pd.DataFrame,
    fremont_counts: pd.DataFrame,
    optional_counts: pd.DataFrame,
    accessibility_score: float,
    lehd_jobs: pd.DataFrame,
) -> pd.DataFrame:
    hours = pd.DataFrame({"hour": range(24)})
    days = pd.DataFrame({"day_of_week": range(7)})
    features = grid[["cell_id", "center_lat", "center_lon", "row", "col"]].merge(hours, how="cross")
    features = features.merge(days, how="cross")

    features = features.merge(gtfs_frequency, on=["cell_id", "hour"], how="left")
    features = features.merge(fremont_counts, on=["cell_id", "hour", "day_of_week"], how="left")
    if not optional_counts.empty:
        optional_counts = optional_counts.rename(columns={"observed_count": "optional_observed_count"})
        features = features.merge(optional_counts, on=["cell_id", "hour", "day_of_week"], how="left")
    else:
        features["optional_observed_count"] = 0.0

    if not lehd_jobs.empty:
        features = features.merge(lehd_jobs, on="cell_id", how="left")
    else:
        features["employment_jobs"] = 0.0

    fill_columns = [
        "transit_departures",
        "unique_stops",
        "observed_count",
        "bike_count_proxy",
        "optional_observed_count",
        "employment_jobs",
    ]
    for column in fill_columns:
        if column not in features:
            features[column] = 0.0
        features[column] = pd.to_numeric(features[column], errors="coerce").fillna(0.0)

    features["accessibility_score"] = accessibility_score
    features["target_count"] = features[["observed_count", "optional_observed_count"]].max(axis=1)
    features["congestion_score"] = (
        0.45 * min_max(features["target_count"])
        + 0.30 * min_max(features["transit_departures"])
        + 0.15 * min_max(features["employment_jobs"])
        + 0.10 * min_max(features["accessibility_score"])
    )
    return features


def write_geojson(features: pd.DataFrame, grid: pd.DataFrame, output_path: Path) -> None:
    latest = (
        features.groupby("cell_id", as_index=False)
        .agg(
            congestion_score=("congestion_score", "mean"),
            transit_departures=("transit_departures", "mean"),
            target_count=("target_count", "mean"),
            employment_jobs=("employment_jobs", "mean"),
        )
        .merge(grid, on="cell_id", how="left")
    )
    geo_features = []
    for row in latest.itertuples(index=False):
        polygon = [
            [
                [row.min_lon, row.min_lat],
                [row.max_lon, row.min_lat],
                [row.max_lon, row.max_lat],
                [row.min_lon, row.max_lat],
                [row.min_lon, row.min_lat],
            ]
        ]
        geo_features.append(
            {
                "type": "Feature",
                "properties": {
                    "cell_id": row.cell_id,
                    "congestion_score": round(float(row.congestion_score), 6),
                    "transit_departures": round(float(row.transit_departures), 3),
                    "target_count": round(float(row.target_count), 3),
                    "employment_jobs": round(float(row.employment_jobs), 3),
                },
                "geometry": {"type": "Polygon", "coordinates": polygon},
            }
        )
    output_path.write_text(json.dumps({"type": "FeatureCollection", "features": geo_features}))


def main() -> None:
    args = parse_args()
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    ensure_dirs(raw_dir, out_dir)

    grid, spec = build_grid(args.cell_size_m)
    gtfs_dir = maybe_download_gtfs(Path(args.gtfs_dir), raw_dir, args.download_gtfs)
    route_types = parse_route_types(args.route_types)
    agency_ids = parse_agency_ids(args.agency_ids)

    gtfs_frequency = load_gtfs_frequency(gtfs_dir, spec, route_types, agency_ids)
    fremont_counts = load_fremont_counts(raw_dir, args.fremont_limit, spec)
    optional_counts = load_optional_counts(args.optional_counts_csv, spec)
    accessibility_score = load_transit_accessibility(raw_dir)
    lehd_jobs = (
        load_lehd_jobs(raw_dir, args.lehd_year, spec)
        if args.include_lehd
        else pd.DataFrame(columns=["cell_id", "employment_jobs"])
    )

    features = assemble_features(
        grid=grid,
        gtfs_frequency=gtfs_frequency,
        fremont_counts=fremont_counts,
        optional_counts=optional_counts,
        accessibility_score=accessibility_score,
        lehd_jobs=lehd_jobs,
    )

    csv_path = out_dir / SEATTLE_HEATMAP_FEATURES_CSV
    geojson_path = out_dir / SEATTLE_HEATMAP_GRID_GEOJSON
    features.to_csv(csv_path, index=False)
    write_geojson(features, grid, geojson_path)

    summary = {
        "grid_cells": int(grid["cell_id"].nunique()),
        "feature_rows": int(len(features)),
        "gtfs_frequency_rows": int(len(gtfs_frequency)),
        "route_types": list(route_types) if route_types is not None else "all",
        "agency_ids": list(agency_ids) if agency_ids is not None else "all",
        "fremont_rows": int(len(fremont_counts)),
        "csv": str(csv_path),
        "geojson": str(geojson_path),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
