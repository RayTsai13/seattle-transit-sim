#!/usr/bin/env python3
"""Create grid weights for a proposed line from ordered station coordinates."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from src.models.train_demand_heatmap_model import dispersion_weight_raw, score_candidates, train_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build distance-decay corridor weights from proposed line station coordinates."
    )
    parser.add_argument(
        "--line-stations-csv",
        required=True,
        help="CSV with ordered line stations. Requires lat, lon; accepts sequence, station_id, station_name, hour, scheduled_trains.",
    )
    parser.add_argument(
        "--candidate-features",
        default="curr_data/processed/features/city_heatmap_candidate_features.csv",
        help="Candidate features containing grid cell geometry.",
    )
    parser.add_argument(
        "--training-features",
        default="curr_data/processed/features/delhi_heatmap_training_features.csv",
        help="Delhi heatmap training features used to learn demand potential for line weights.",
    )
    parser.add_argument(
        "--disable-learned-demand-potential",
        action="store_true",
        help="Fall back to transparent census/connectivity catchment weights for proposed lines.",
    )
    parser.add_argument("--out-dir", default="curr_data/processed/scenarios")
    parser.add_argument("--output-name", default="proposed_line_weights.csv")
    parser.add_argument("--candidate-output-name", default="proposed_line_candidate_features.csv")
    parser.add_argument("--added-stations-output-name", default="proposed_line_added_stations.csv")
    parser.add_argument("--frequency-delta-output-name", default="proposed_line_frequency_delta.csv")
    parser.add_argument(
        "--station-vectors",
        default="curr_data/processed/features/seattle_station_vectors.csv",
        help="Existing station vectors used when rebuilding scenario candidate features.",
    )
    parser.add_argument("--gtfs-dir", default="gtfs_stations")
    parser.add_argument("--route-types", default="0,1,2")
    parser.add_argument("--agency-ids", default="40")
    parser.add_argument("--cell-size-m", type=int, default=500)
    parser.add_argument("--time-bin-minutes", type=int, default=30)
    parser.add_argument(
        "--no-rebuild-candidate-features",
        action="store_true",
        help="Only join line weights to existing candidate features. By default, station/frequency exposure is rebuilt too.",
    )
    parser.add_argument("--line-id", default="proposed_line")
    parser.add_argument("--decay-m", type=float, default=800.0)
    parser.add_argument("--radius-m", type=float, default=2500.0)
    parser.add_argument(
        "--default-scheduled-trains",
        type=float,
        default=6.0,
        help="Used when the input has no scheduled_trains column.",
    )
    return parser.parse_args()


def require_columns(df: pd.DataFrame, columns: set[str], name: str) -> None:
    missing = columns.difference(df.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {', '.join(sorted(missing))}")


def ordered_line_stations(path: str, line_id: str) -> pd.DataFrame:
    stations = pd.read_csv(path)
    require_columns(stations, {"lat", "lon"}, "--line-stations-csv")
    stations = stations.copy()
    if "sequence" not in stations:
        stations["sequence"] = range(1, len(stations) + 1)
    if "station_id" not in stations:
        stations["station_id"] = [
            f"{line_id}_station_{index:03d}" for index in range(1, len(stations) + 1)
        ]
    if "station_name" not in stations:
        stations["station_name"] = stations["station_id"]
    stations["lat"] = pd.to_numeric(stations["lat"], errors="coerce")
    stations["lon"] = pd.to_numeric(stations["lon"], errors="coerce")
    stations = stations.dropna(subset=["lat", "lon"]).sort_values("sequence")
    if len(stations) < 2:
        raise ValueError("--line-stations-csv must include at least two valid coordinate rows")
    return stations


def unique_grid(candidates: pd.DataFrame) -> pd.DataFrame:
    required = {"cell_id", "center_lat", "center_lon", "row", "col", "min_lat", "min_lon", "max_lat", "max_lon"}
    require_columns(candidates, required, "--candidate-features")
    return candidates[list(required)].drop_duplicates("cell_id").copy()


def min_max(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").fillna(0)
    min_value = numeric.min()
    max_value = numeric.max()
    if max_value == min_value:
        return pd.Series(0.0, index=numeric.index)
    return (numeric - min_value) / (max_value - min_value)


def numeric_column(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[column], errors="coerce").fillna(0)


def static_grid_context(candidates: pd.DataFrame) -> pd.DataFrame:
    static_columns = [
        "cell_id",
        "center_lat",
        "center_lon",
        "distance_weighted_residential_density",
        "distance_weighted_office_jobs",
        "distance_weighted_connectivity",
        "distance_weighted_transfer_score",
        "daily_scheduled_trains",
        "learned_demand_potential",
        "dispersion_weight",
    ]
    available = [column for column in static_columns if column in candidates.columns]
    context = candidates[available].drop_duplicates("cell_id").copy()
    for column in static_columns:
        if column not in context:
            context[column] = 0.0
    heuristic_catchment = (
        0.35 * min_max(context["distance_weighted_residential_density"])
        + 0.25 * min_max(context["distance_weighted_office_jobs"])
        + 0.25 * min_max(context["distance_weighted_connectivity"])
        + 0.15 * min_max(context["daily_scheduled_trains"])
    )
    context["catchment_demand_score"] = (
        0.60 * min_max(context["learned_demand_potential"])
        + 0.25 * heuristic_catchment
        + 0.15 * min_max(context["dispersion_weight"])
    )
    context["junction_context_score"] = (
        0.60 * min_max(context["distance_weighted_connectivity"])
        + 0.40 * min_max(context["distance_weighted_transfer_score"])
    )
    return context


def add_learned_demand_potential(
    candidates: pd.DataFrame,
    training_features: str | None,
    disabled: bool,
) -> pd.DataFrame:
    result = candidates.copy()
    if disabled or not training_features:
        result["learned_demand_potential"] = 0.0
        result["dispersion_weight"] = dispersion_weight_raw(result)
        return result

    path = Path(training_features)
    if not path.exists() or path.stat().st_size == 0:
        result["learned_demand_potential"] = 0.0
        result["dispersion_weight"] = dispersion_weight_raw(result)
        return result

    training = pd.read_csv(path)
    model, _ = train_model(training, test_size=0.25, random_state=42)
    scored = score_candidates(model, result)
    result["learned_demand_potential"] = scored["relative_demand_pressure"]
    result["dispersion_weight"] = dispersion_weight_raw(scored)
    return result


def proposed_station_context(
    stations: pd.DataFrame,
    candidates: pd.DataFrame,
    decay_m: float,
    radius_m: float,
) -> pd.DataFrame:
    context = static_grid_context(candidates)
    station_lat = stations["lat"].to_numpy()[:, None]
    station_lon = stations["lon"].to_numpy()[:, None]
    grid_lat = context["center_lat"].to_numpy()[None, :]
    grid_lon = context["center_lon"].to_numpy()[None, :]

    meters_per_degree_lat = 111_320
    origin_lat = float(pd.concat([stations["lat"], context["center_lat"]]).mean())
    meters_per_degree_lon = 111_320 * math.cos(math.radians(origin_lat))
    distances = np.sqrt(
        ((station_lat - grid_lat) * meters_per_degree_lat) ** 2
        + ((station_lon - grid_lon) * meters_per_degree_lon) ** 2
    )
    weights = np.where(distances <= radius_m, np.exp(-distances / decay_m), 0.0)
    weight_sums = weights.sum(axis=1)
    demand_values = context["catchment_demand_score"].to_numpy()
    junction_values = context["junction_context_score"].to_numpy()

    result = stations.copy()
    result["line_station_catchment_demand"] = np.divide(
        weights @ demand_values,
        weight_sums,
        out=np.zeros(len(stations)),
        where=weight_sums > 0,
    )
    inferred_junction = np.divide(
        weights @ junction_values,
        weight_sums,
        out=np.zeros(len(stations)),
        where=weight_sums > 0,
    )
    supplied_junction = 0.6 * min_max(numeric_column(result, "connectivity")) + 0.4 * numeric_column(
        result, "is_transfer_proxy"
    )
    result["line_station_junction_potential"] = np.maximum(inferred_junction, supplied_junction)
    return result


def line_network_metrics(stations: pd.DataFrame) -> dict[str, float]:
    station_demand = pd.to_numeric(stations["line_station_catchment_demand"], errors="coerce").fillna(0)
    junction = pd.to_numeric(stations["line_station_junction_potential"], errors="coerce").fillna(0)
    top_station_demands = station_demand.sort_values(ascending=False).tolist()
    connected_pair_demand = (
        math.sqrt(top_station_demands[0] * top_station_demands[1])
        if len(top_station_demands) >= 2
        else 0.0
    )
    line_connected_demand = 0.45 * float(station_demand.mean()) + 0.55 * connected_pair_demand
    line_junction_weight = float(junction.max()) if not junction.empty else 0.0
    line_network_value = line_connected_demand * (1.0 + 0.5 * line_junction_weight)
    return {
        "line_connected_demand": line_connected_demand,
        "line_junction_weight": line_junction_weight,
        "line_network_value": line_network_value,
    }


def bbox_from_grid(grid: pd.DataFrame) -> str:
    return ",".join(
        str(value)
        for value in [
            grid["min_lon"].min(),
            grid["min_lat"].min(),
            grid["max_lon"].max(),
            grid["max_lat"].max(),
        ]
    )


def project(lat: np.ndarray, lon: np.ndarray, origin_lat: float, origin_lon: float) -> tuple[np.ndarray, np.ndarray]:
    meters_per_degree_lat = 111_320
    meters_per_degree_lon = 111_320 * math.cos(math.radians(origin_lat))
    x = (lon - origin_lon) * meters_per_degree_lon
    y = (lat - origin_lat) * meters_per_degree_lat
    return x, y


def point_segment_distance_m(
    px: np.ndarray,
    py: np.ndarray,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> np.ndarray:
    abx = bx - ax
    aby = by - ay
    length_sq = abx * abx + aby * aby
    if length_sq == 0:
        return np.sqrt((px - ax) ** 2 + (py - ay) ** 2)
    t = ((px - ax) * abx + (py - ay) * aby) / length_sq
    t = np.clip(t, 0, 1)
    nearest_x = ax + t * abx
    nearest_y = ay + t * aby
    return np.sqrt((px - nearest_x) ** 2 + (py - nearest_y) ** 2)


def line_distances(grid: pd.DataFrame, stations: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    origin_lat = float(pd.concat([grid["center_lat"], stations["lat"]]).mean())
    origin_lon = float(pd.concat([grid["center_lon"], stations["lon"]]).mean())
    grid_x, grid_y = project(
        grid["center_lat"].to_numpy(),
        grid["center_lon"].to_numpy(),
        origin_lat,
        origin_lon,
    )
    station_x, station_y = project(
        stations["lat"].to_numpy(),
        stations["lon"].to_numpy(),
        origin_lat,
        origin_lon,
    )

    segment_distances = []
    for index in range(len(stations) - 1):
        segment_distances.append(
            point_segment_distance_m(
                grid_x,
                grid_y,
                float(station_x[index]),
                float(station_y[index]),
                float(station_x[index + 1]),
                float(station_y[index + 1]),
            )
        )
    nearest_line = np.vstack(segment_distances).min(axis=0)

    station_distances = []
    for x, y in zip(station_x, station_y):
        station_distances.append(np.sqrt((grid_x - x) ** 2 + (grid_y - y) ** 2))
    nearest_station = np.vstack(station_distances).min(axis=0)
    return nearest_line, nearest_station


def scheduled_train_value(stations: pd.DataFrame, default_scheduled_trains: float) -> float:
    if "scheduled_trains" not in stations:
        return default_scheduled_trains
    values = pd.to_numeric(stations["scheduled_trains"], errors="coerce").dropna()
    if values.empty:
        return default_scheduled_trains
    return float(values.mean())


def build_weights(
    grid: pd.DataFrame,
    stations: pd.DataFrame,
    line_id: str,
    decay_m: float,
    radius_m: float,
    default_scheduled_trains: float,
) -> pd.DataFrame:
    nearest_line, nearest_station = line_distances(grid, stations)
    line_weight = np.where(nearest_line <= radius_m, np.exp(-nearest_line / decay_m), 0.0)
    station_weight = np.where(nearest_station <= radius_m, np.exp(-nearest_station / decay_m), 0.0)
    scheduled_trains = scheduled_train_value(stations, default_scheduled_trains)
    network = line_network_metrics(stations)

    result = grid[["cell_id", "center_lat", "center_lon", "row", "col", "min_lat", "min_lon", "max_lat", "max_lon"]].copy()
    result["line_id"] = line_id
    result["nearest_line_distance_m"] = nearest_line
    result["nearest_line_station_distance_m"] = nearest_station
    result["line_distance_weight"] = line_weight
    result["line_station_weight"] = station_weight
    result["line_combined_weight"] = np.maximum(line_weight, station_weight)
    result["line_scheduled_trains"] = scheduled_trains
    result["line_connected_demand"] = result["line_combined_weight"] * network["line_connected_demand"]
    result["line_junction_weight"] = result["line_combined_weight"] * network["line_junction_weight"]
    result["line_network_value"] = result["line_combined_weight"] * network["line_network_value"]
    result["line_service_weight"] = result["line_network_value"] * scheduled_trains
    return result


def added_stations(stations: pd.DataFrame) -> pd.DataFrame:
    output = stations[["station_id", "station_name", "lat", "lon"]].copy()
    for column in [
        "activity_score",
        "connectivity_score",
        "activity_rank_pct",
        "is_transfer_proxy",
        "connectivity",
        "residential_density_ratio",
    ]:
        if column not in stations:
            output[column] = 0
        else:
            output[column] = stations[column]
    return output


def frequency_delta(stations: pd.DataFrame, bin_minutes: int) -> pd.DataFrame:
    if "hour" not in stations or "scheduled_trains" not in stations:
        return pd.DataFrame(columns=["station_id", "time_bin", "hour", "minute", "scheduled_trains_delta"])
    output = stations[["station_id", "hour", "scheduled_trains"]].copy()
    if "minute" in stations:
        output["minute"] = stations["minute"]
    if "time_bin" in stations:
        output["time_bin"] = pd.to_numeric(stations["time_bin"], errors="coerce")
    else:
        output["hour"] = pd.to_numeric(output["hour"], errors="coerce").fillna(0).astype(int)
        if "minute" in output:
            minute = pd.to_numeric(output["minute"], errors="coerce").fillna(0).astype(int)
            output["time_bin"] = output["hour"] * 60 + minute
        else:
            rows = []
            for row in output.itertuples(index=False):
                for offset in range(0, 60, bin_minutes):
                    values = row._asdict()
                    values["time_bin"] = int(row.hour) * 60 + offset
                    rows.append(values)
            output = pd.DataFrame(rows)
    output["hour"] = pd.to_numeric(output["hour"], errors="coerce")
    output["scheduled_trains_delta"] = pd.to_numeric(output["scheduled_trains"], errors="coerce")
    output = output.dropna(subset=["time_bin", "hour", "scheduled_trains_delta"])
    output["time_bin"] = output["time_bin"].astype(int)
    output["hour"] = output["hour"].astype(int)
    output["minute"] = output["time_bin"] % 60
    return output[["station_id", "time_bin", "hour", "minute", "scheduled_trains_delta"]]


def run(command: list[str]) -> None:
    print(f"\n$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT_DIR, check=True)


def candidate_weight_columns(weights: pd.DataFrame) -> pd.DataFrame:
    geometry_columns = [
        "center_lat",
        "center_lon",
        "row",
        "col",
        "min_lat",
        "min_lon",
        "max_lat",
        "max_lon",
    ]
    return weights.drop(columns=geometry_columns)


def rebuild_candidate_features(args: argparse.Namespace, grid: pd.DataFrame, added_path: Path, frequency_path: Path) -> pd.DataFrame:
    with tempfile.TemporaryDirectory(prefix="line_scenario_candidates_") as temp_name:
        temp_dir = Path(temp_name)
        output_name = "rebuilt_line_candidate_features.csv"
        command = [
            sys.executable,
            "-m",
            "src.pipelines.common.build_heatmap_candidates",
            "--station-vectors",
            args.station_vectors,
            "--gtfs-dir",
            args.gtfs_dir,
            "--out-dir",
            str(temp_dir),
            "--bbox",
            bbox_from_grid(grid),
            "--cell-size-m",
            str(args.cell_size_m),
            "--time-bin-minutes",
            str(args.time_bin_minutes),
            "--added-stations-csv",
            str(added_path),
            "--frequency-delta-csv",
            str(frequency_path),
            "--output-name",
            output_name,
            "--route-types",
            args.route_types,
            "--agency-ids",
            args.agency_ids,
        ]
        run(command)
        return pd.read_csv(temp_dir / output_name)


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = pd.read_csv(args.candidate_features)
    candidates = add_learned_demand_potential(
        candidates,
        args.training_features,
        args.disable_learned_demand_potential,
    )
    stations = ordered_line_stations(args.line_stations_csv, args.line_id)
    stations = proposed_station_context(stations, candidates, args.decay_m, args.radius_m)
    grid = unique_grid(candidates)
    weights = build_weights(
        grid,
        stations,
        args.line_id,
        args.decay_m,
        args.radius_m,
        args.default_scheduled_trains,
    )
    weights_path = out_dir / args.output_name
    weights.to_csv(weights_path, index=False)

    added_path = out_dir / args.added_stations_output_name
    added_stations(stations).to_csv(added_path, index=False)
    frequency_path = out_dir / args.frequency_delta_output_name
    frequency_delta(stations, args.time_bin_minutes).to_csv(frequency_path, index=False)

    if args.no_rebuild_candidate_features:
        candidates = pd.read_csv(args.candidate_features)
    else:
        candidates = rebuild_candidate_features(args, grid, added_path, frequency_path)
    candidate_output = candidates.merge(candidate_weight_columns(weights), on="cell_id", how="left")
    candidate_path = out_dir / args.candidate_output_name
    candidate_output.to_csv(candidate_path, index=False)

    print(
        json.dumps(
            {
                "line_id": args.line_id,
                "line_stations": int(len(stations)),
                "grid_cells": int(len(grid)),
                "weighted_cells": int((weights["line_combined_weight"] > 0).sum()),
                "weights": str(weights_path),
                "candidate_features": str(candidate_path),
                "added_stations": str(added_path),
                "frequency_delta": str(frequency_path),
                "rebuilt_candidate_features": not args.no_rebuild_candidate_features,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
