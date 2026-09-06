#!/usr/bin/env python3
"""Train a Delhi load-per-train model and score city heatmap timelapse candidates."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import train_test_split

from src.common.artifacts import (
    CITY_HEATMAP_CANDIDATE_FEATURES_CSV,
    DEFAULT_PROCESSED_DIR,
    DELHI_HEATMAP_TRAINING_FEATURES_CSV,
    HEATMAP_TIMELAPSE_GRID_GEOJSON,
    HEATMAP_TIMELAPSE_MODEL_METRICS_JSON,
    HEATMAP_TIMELAPSE_PREDICTIONS_CSV,
    HEATMAP_TIMELAPSE_SCENARIO_PREDICTIONS_CSV,
)
from src.common.heatmap_utils import haversine_m, min_max, write_grid_geojson


BASE_FEATURE_COLUMNS = [
    "time_bin",
    "hour",
    "minute",
    "day_of_week",
    "is_weekend",
    "is_peak",
    "is_off_peak",
    "is_morning_commute",
    "is_evening_commute",
    "is_workday_midday",
    "is_festival",
    "is_maintenance",
    "distance_weighted_connectivity",
    "distance_weighted_residential_density",
    "distance_weighted_office_jobs",
    "distance_weighted_transfer_score",
    "residential_temporal_demand",
    "office_temporal_demand",
    "commute_demand_score",
    "scheduled_trains",
    "daily_scheduled_trains",
    "has_gtfs_frequency",
]

LINE_FEATURE_COLUMNS = [
    "nearest_line_distance_m",
    "nearest_line_station_distance_m",
    "line_distance_weight",
    "line_station_weight",
    "line_combined_weight",
    "line_scheduled_trains",
    "line_connected_demand",
    "line_junction_weight",
    "line_network_value",
    "line_service_weight",
]

FEATURE_COLUMNS = BASE_FEATURE_COLUMNS + LINE_FEATURE_COLUMNS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train and score a city-generic heatmap timelapse model.")
    parser.add_argument(
        "--training-features",
        default=str(DEFAULT_PROCESSED_DIR / DELHI_HEATMAP_TRAINING_FEATURES_CSV),
        help="Delhi heatmap training features.",
    )
    parser.add_argument(
        "--candidate-features",
        default=str(DEFAULT_PROCESSED_DIR / CITY_HEATMAP_CANDIDATE_FEATURES_CSV),
        help="Baseline candidate features to score.",
    )
    parser.add_argument("--scenario-features", help="Optional scenario candidate features to score.")
    parser.add_argument("--event-scenarios-csv", help="Optional event surplus users scenario CSV.")
    parser.add_argument("--out-dir", default=str(DEFAULT_PROCESSED_DIR), help="Output directory.")
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--random-state", type=int, default=42)
    return parser.parse_args()


def prepare_matrix(df: pd.DataFrame) -> pd.DataFrame:
    matrix = pd.DataFrame(index=df.index)
    for column in FEATURE_COLUMNS:
        if column in df:
            matrix[column] = pd.to_numeric(df[column], errors="coerce").fillna(0)
        else:
            matrix[column] = 0.0
    return matrix


def train_model(training: pd.DataFrame, test_size: float, random_state: int) -> tuple[HistGradientBoostingRegressor, dict]:
    if "load_per_train" not in training:
        raise ValueError("Training features must include load_per_train")
    usable = training[training["load_per_train"].notna()].copy()
    train, test = train_test_split(usable, test_size=test_size, random_state=random_state)
    model = HistGradientBoostingRegressor(random_state=random_state, loss="squared_error")
    model.fit(prepare_matrix(train), np.log1p(pd.to_numeric(train["load_per_train"], errors="coerce").fillna(0)))

    predicted_load = np.expm1(model.predict(prepare_matrix(test))).clip(min=0)
    actual_load = pd.to_numeric(test["load_per_train"], errors="coerce").fillna(0)
    metrics = {
        "training_rows": int(len(usable)),
        "test_rows": int(len(test)),
        "target": "load_per_train",
        "mae_load_per_train": float(mean_absolute_error(actual_load, predicted_load)),
        "rmse_load_per_train": float(math.sqrt(mean_squared_error(actual_load, predicted_load))),
        "base_feature_columns": BASE_FEATURE_COLUMNS,
        "line_feature_columns": LINE_FEATURE_COLUMNS,
        "note": (
            "Delhi labels are treated as passengers per train/trip. "
            "Line features are zero-filled for rows without proposed-line weights, "
            "and hourly flow uses scheduled trains plus line_service_weight."
        ),
    }
    return model, metrics


def score_candidates(model: HistGradientBoostingRegressor, candidates: pd.DataFrame) -> pd.DataFrame:
    scored = candidates.copy()
    scored["predicted_load_per_train"] = np.expm1(model.predict(prepare_matrix(scored))).clip(min=0)
    scored["scheduled_trains"] = pd.to_numeric(scored["scheduled_trains"], errors="coerce").fillna(0)
    if "line_service_weight" not in scored:
        scored["line_service_weight"] = 0.0
    scored["line_service_weight"] = pd.to_numeric(scored["line_service_weight"], errors="coerce").fillna(0)
    scored["effective_scheduled_trains"] = scored["scheduled_trains"] + scored["line_service_weight"]
    scored["predicted_hourly_flow"] = (
        scored["predicted_load_per_train"] * scored["effective_scheduled_trains"]
    )
    scored["event_surplus_flow"] = 0.0
    scored["scenario_hourly_flow"] = scored["predicted_hourly_flow"]
    scored["demand_score"] = min_max(scored["predicted_hourly_flow"])
    return scored


def apply_event_surplus(scored: pd.DataFrame, events_csv: str | None) -> pd.DataFrame:
    if not events_csv:
        return scored
    events = pd.read_csv(events_csv)
    required = {"lat", "lon", "day_of_week", "start_hour", "end_hour", "surplus_users"}
    missing = required.difference(events.columns)
    if missing:
        raise ValueError(f"Event scenario CSV missing columns: {', '.join(sorted(missing))}")

    result = scored.copy()
    for event in events.itertuples(index=False):
        day = int(event.day_of_week)
        start_hour = int(event.start_hour)
        end_hour = int(event.end_hour)
        hours = list(range(start_hour, end_hour + 1))
        if not hours:
            continue
        radius_m = float(getattr(event, "radius_m", 1500) or 1500)
        decay_m = float(getattr(event, "decay_m", 800) or 800)
        per_hour_users = float(event.surplus_users) / len(hours)
        for hour in hours:
            mask = (result["day_of_week"] == day) & (result["hour"] == hour)
            if not mask.any():
                continue
            subset = result.loc[mask]
            distances = haversine_m(
                subset["center_lat"].to_numpy(),
                subset["center_lon"].to_numpy(),
                np.array(float(event.lat)),
                np.array(float(event.lon)),
            )
            weights = np.where(distances <= radius_m, np.exp(-distances / decay_m), 0.0)
            if weights.sum() == 0:
                continue
            additions = per_hour_users * weights / weights.sum()
            result.loc[mask, "event_surplus_flow"] += additions
    result["scenario_hourly_flow"] = result["predicted_hourly_flow"] + result["event_surplus_flow"]
    result["demand_score"] = min_max(result["scenario_hourly_flow"])
    return result


def scenario_delta(baseline: pd.DataFrame, scenario: pd.DataFrame) -> pd.DataFrame:
    keys = ["cell_id", "day_of_week"]
    if "time_bin" in baseline.columns and "time_bin" in scenario.columns:
        keys.append("time_bin")
    else:
        keys.append("hour")
    left = baseline[keys + ["predicted_hourly_flow", "demand_score"]].rename(
        columns={
            "predicted_hourly_flow": "baseline_hourly_flow",
            "demand_score": "baseline_demand_score",
        }
    )
    right = scenario.rename(
        columns={
            "scenario_hourly_flow": "scenario_hourly_flow",
            "demand_score": "scenario_demand_score",
        }
    )
    output = right.merge(left, on=keys, how="left")
    output["demand_delta"] = output["scenario_hourly_flow"] - output["baseline_hourly_flow"]
    denominator = output["baseline_hourly_flow"].replace(0, np.nan)
    output["percent_change"] = (output["demand_delta"] / denominator * 100).replace([np.inf, -np.inf], np.nan).fillna(0)
    return output


def write_prediction_grid(scored: pd.DataFrame, output_path: Path) -> None:
    grid_columns = ["cell_id", "row", "col", "min_lat", "min_lon", "max_lat", "max_lon"]
    scored = scored.copy()
    if "demand_score" not in scored and "scenario_demand_score" in scored:
        scored["demand_score"] = scored["scenario_demand_score"]
    grid = scored.groupby("cell_id", as_index=False).agg(
        {
            "row": "first",
            "col": "first",
            "min_lat": "first",
            "min_lon": "first",
            "max_lat": "first",
            "max_lon": "first",
            "demand_score": "mean",
            "scenario_demand_score": "mean" if "scenario_demand_score" in scored else "first",
            "demand_delta": "mean" if "demand_delta" in scored else "first",
            "percent_change": "mean" if "percent_change" in scored else "first",
        }
    )
    write_grid_geojson(grid[grid_columns + [column for column in grid.columns if column not in grid_columns]], output_path)


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    training = pd.read_csv(args.training_features)
    baseline_candidates = pd.read_csv(args.candidate_features)
    model, metrics = train_model(training, args.test_size, args.random_state)
    baseline = score_candidates(model, baseline_candidates)

    if args.scenario_features:
        scenario_candidates = pd.read_csv(args.scenario_features)
        scenario = score_candidates(model, scenario_candidates)
    else:
        scenario = baseline.copy()
    scenario = apply_event_surplus(scenario, args.event_scenarios_csv)
    output = scenario_delta(baseline, scenario)
    output_path = out_dir / (
        HEATMAP_TIMELAPSE_SCENARIO_PREDICTIONS_CSV
        if args.scenario_features or args.event_scenarios_csv
        else HEATMAP_TIMELAPSE_PREDICTIONS_CSV
    )
    output.to_csv(output_path, index=False)
    write_prediction_grid(output, out_dir / HEATMAP_TIMELAPSE_GRID_GEOJSON)

    metrics.update(
        {
            "baseline_candidate_rows": int(len(baseline_candidates)),
            "scenario_candidate_rows": int(len(scenario)),
            "event_scenarios": bool(args.event_scenarios_csv),
            "predictions": str(output_path),
        }
    )
    metrics_path = out_dir / HEATMAP_TIMELAPSE_MODEL_METRICS_JSON
    metrics_path.write_text(json.dumps(metrics, indent=2))
    print(json.dumps({"metrics": str(metrics_path), **metrics}, indent=2))


if __name__ == "__main__":
    main()
