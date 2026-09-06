#!/usr/bin/env python3
"""Build Seattle station vectors from GTFS connectivity and Census population density."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR / "data_processing") not in sys.path:
    sys.path.append(str(ROOT_DIR / "data_processing"))

from src.geo_utils import compute_population_density_vectors
from src.io_utils import cached_download, ensure_dir, write_csv
from src.station_utils import add_rank_and_scores, aggregate_duplicate_stations, read_gtfs_stops


SEATTLE_BBOX = (-122.4597, 47.4810, -122.2244, 47.7340)
ACS_YEAR = "2022"
TRACT_ZIP_URL = "https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_53_tract_500k.zip"
PLACE_ZIP_URL = "https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_53_place_500k.zip"
ACS_URL = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create Seattle station vectors.")
    parser.add_argument("--gtfs-dir", default="gtfs", help="Local Seattle/Puget Sound GTFS directory.")
    parser.add_argument("--raw-dir", default="../data/raw", help="Raw data cache directory.")
    parser.add_argument("--out-dir", default="../data/processed", help="Output directory.")
    parser.add_argument("--radius-m", type=int, default=1000, help="Station population radius.")
    return parser.parse_args()


def in_seattle_bbox(stations: pd.DataFrame) -> pd.Series:
    min_lon, min_lat, max_lon, max_lat = SEATTLE_BBOX
    return (
        stations["lon"].between(min_lon, max_lon)
        & stations["lat"].between(min_lat, max_lat)
    )


def read_zip_shapefile(zip_path: Path) -> gpd.GeoDataFrame:
    with zipfile.ZipFile(zip_path) as archive:
        shp_files = [name for name in archive.namelist() if name.endswith(".shp")]
    if not shp_files:
        raise ValueError(f"No shapefile found in {zip_path}")
    return gpd.read_file(f"zip://{zip_path}!{shp_files[0]}")


def fetch_acs_tract_population(raw_dir: Path) -> pd.DataFrame:
    output = raw_dir / "king_county_acs_tract_population.csv"
    if output.exists() and output.stat().st_size > 0:
        return pd.read_csv(output, dtype={"state": "string", "county": "string", "tract": "string"})

    response = requests.get(
        ACS_URL,
        params={"get": "NAME,B01003_001E", "for": "tract:*", "in": "state:53 county:033"},
        timeout=90,
    )
    response.raise_for_status()
    rows = response.json()
    population = pd.DataFrame(rows[1:], columns=rows[0])
    population["GEOID"] = population["state"] + population["county"] + population["tract"]
    population["population"] = pd.to_numeric(population["B01003_001E"], errors="coerce").fillna(0)
    write_csv(population, output)
    return population


def fetch_seattle_place_population(raw_dir: Path) -> float:
    output = raw_dir / "seattle_acs_place_population.csv"
    if output.exists() and output.stat().st_size > 0:
        cached = pd.read_csv(output)
        return float(cached["population"].iloc[0])

    response = requests.get(
        ACS_URL,
        params={"get": "NAME,B01003_001E", "for": "place:63000", "in": "state:53"},
        timeout=90,
    )
    response.raise_for_status()
    rows = response.json()
    df = pd.DataFrame(rows[1:], columns=rows[0])
    df["population"] = pd.to_numeric(df["B01003_001E"], errors="coerce").fillna(0)
    write_csv(df, output)
    return float(df["population"].iloc[0])


def load_population_geometries(raw_dir: Path) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, float]:
    tract_zip = cached_download(TRACT_ZIP_URL, raw_dir / "cb_2022_53_tract_500k.zip")
    place_zip = cached_download(PLACE_ZIP_URL, raw_dir / "cb_2022_53_place_500k.zip")

    tract_population = fetch_acs_tract_population(raw_dir)
    tracts = read_zip_shapefile(tract_zip)
    king_tracts = tracts[tracts["COUNTYFP"] == "033"].merge(
        tract_population[["GEOID", "population"]], on="GEOID", how="left"
    )
    king_tracts["population"] = pd.to_numeric(king_tracts["population"], errors="coerce").fillna(0)

    places = read_zip_shapefile(place_zip)
    seattle_boundary = places[places["PLACEFP"] == "63000"].copy()
    seattle_population = fetch_seattle_place_population(raw_dir)
    return king_tracts, seattle_boundary, seattle_population


def build_gtfs_connectivity(gtfs_dir: Path) -> pd.DataFrame:
    stops = aggregate_duplicate_stations(read_gtfs_stops(gtfs_dir))
    stops = stops[in_seattle_bbox(stops)].copy()

    raw_stops = read_gtfs_stops(gtfs_dir)[["stop_id", "station_id"]]
    stop_times = pd.read_csv(gtfs_dir / "stop_times.txt", usecols=["stop_id", "trip_id"])
    stop_times["stop_id"] = stop_times["stop_id"].astype(str)
    raw_stops["stop_id"] = raw_stops["stop_id"].astype(str)
    departures = (
        stop_times.merge(raw_stops, on="stop_id", how="inner")
        .groupby("station_id", as_index=False)
        .agg(gtfs_departures=("trip_id", "count"))
    )
    stations = stops.merge(departures, on="station_id", how="left")
    stations["gtfs_departures"] = pd.to_numeric(stations["gtfs_departures"], errors="coerce").fillna(0)
    stations["connectivity_raw"] = stations["gtfs_departures"] + stations["stop_count"]
    return stations


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    raw_dir = ensure_dir((base_dir / args.raw_dir).resolve())
    out_dir = ensure_dir((base_dir / args.out_dir).resolve())
    gtfs_dir = Path(args.gtfs_dir)
    if not gtfs_dir.is_absolute():
        gtfs_dir = (ROOT_DIR / gtfs_dir).resolve()

    stations = build_gtfs_connectivity(gtfs_dir)
    tracts, seattle_boundary, seattle_population = load_population_geometries(raw_dir)
    density = compute_population_density_vectors(
        stations=stations,
        population_polygons=tracts,
        population_col="population",
        radius_m=args.radius_m,
        city_boundary=seattle_boundary,
        city_population=seattle_population,
    )

    vectors = stations.merge(density, on="station_id", how="left")
    vectors["activity_raw"] = (
        vectors["residential_density_ratio"].fillna(0) * 0.7
        + vectors["connectivity_raw"].rank(pct=True).fillna(0) * 0.3
    )
    vectors = add_rank_and_scores(vectors, "activity_raw", "connectivity_raw")

    output_columns = [
        "station_id",
        "station_name",
        "lat",
        "lon",
        "activity_score",
        "connectivity_score",
        "activity_rank_pct",
        "is_transfer_proxy",
        "connectivity",
        "population_within_radius",
        "population_density_within_radius",
        "city_average_population_density",
        "residential_density_ratio",
    ]
    output_path = write_csv(vectors[output_columns], out_dir / "seattle_station_vectors.csv")
    summary = {
        "stations": int(len(vectors)),
        "radius_m": args.radius_m,
        "seattle_population": seattle_population,
        "output": str(output_path),
    }
    (out_dir / "seattle_station_vector_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
