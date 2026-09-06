#!/usr/bin/env python3
"""Train and score a relative transit-demand pressure heatmap."""

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
    DEMAND_HEATMAP_GRID_GEOJSON,
    DEMAND_HEATMAP_MODEL_METRICS_JSON,
    DEMAND_HEATMAP_PREDICTIONS_CSV,
    DEMAND_HEATMAP_SCENARIO_PREDICTIONS_CSV,
)
from src.common.heatmap_utils import haversine_m, min_max, write_grid_geojson
from src.models.train_heatmap_timelapse_model import BASE_FEATURE_COLUMNS, LINE_FEATURE_COLUMNS


FEATURE_COLUMNS = BASE_FEATURE_COLUMNS + LINE_FEATURE_COLUMNS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train and score a relative demand-pressure heatmap.")
    parser.add_argument(
        "--training-features",
        default=str(DEFAULT_PROCESSED_DIR / DELHI_HEATMAP_TRAINING_FEATURES_CSV),
        help="Delhi heatmap training features used as weak supervision.",
    )
    parser.add_argument(
        "--candidate-features",
        default=str(DEFAULT_PROCESSED_DIR / CITY_HEATMAP_CANDIDATE_FEATURES_CSV),
        help="Baseline candidate features to score.",
    )
    parser.add_argument("--scenario-features", help="Optional scenario candidate features to score.")
    parser.add_argument("--event-scenarios-csv", help="Optional event surplus users scenario CSV.")
    parser.add_argument("--out-dir", default=str(DEFAULT_PROCESSED_DIR), help="Output directory.")
    parser.add_argument("--time-bin-minutes", type=int, default=30, help="Event allocation interval.")
    parser.add_argument(
        "--event-tail-bins",
        type=int,
        default=4,
        help="Number of post-event time bins that receive decayed surplus demand.",
    )
    parser.add_argument(
        "--event-tail-decay",
        type=float,
        default=0.5,
        help="Per-bin temporal decay applied after the event window.",
    )
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
    target = np.log1p(pd.to_numeric(train["load_per_train"], errors="coerce").fillna(0))
    model.fit(prepare_matrix(train), target)

    predicted_load = np.expm1(model.predict(prepare_matrix(test))).clip(min=0)
    actual_load = pd.to_numeric(test["load_per_train"], errors="coerce").fillna(0)
    metrics = {
        "training_rows": int(len(usable)),
        "test_rows": int(len(test)),
        "target": "relative_demand_pressure_weakly_supervised_by_delhi_load_per_train",
        "mae_load_per_train_reference": float(mean_absolute_error(actual_load, predicted_load)),
        "rmse_load_per_train_reference": float(math.sqrt(mean_squared_error(actual_load, predicted_load))),
        "base_feature_columns": BASE_FEATURE_COLUMNS,
        "line_feature_columns": LINE_FEATURE_COLUMNS,
        "note": (
            "Outputs are relative demand-pressure scores, not calibrated ridership. "
            "Delhi passenger-per-train labels weakly tune the relationship between density, "
            "connectivity, service, land use, and demand. Direct station-proximity features are "
            "excluded so new stations do not create demand only by being nearby."
        ),
    }
    return model, metrics


def numeric(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[column], errors="coerce").fillna(0)


def access_raw(df: pd.DataFrame) -> pd.Series:
    """Access pressure without direct station-proximity terms."""
    return (
        0.45 * min_max(numeric(df, "distance_weighted_connectivity"))
        + 0.35 * min_max(numeric(df, "scheduled_trains") + numeric(df, "line_service_weight"))
        + 0.20 * min_max(numeric(df, "distance_weighted_transfer_score"))
    )


def service_raw(df: pd.DataFrame) -> pd.Series:
    return (
        np.log1p(numeric(df, "scheduled_trains") + numeric(df, "line_service_weight"))
        + 0.10 * np.log1p(numeric(df, "daily_scheduled_trains"))
        + 0.50 * numeric(df, "line_network_value")
    )


def access_service_raw(df: pd.DataFrame) -> pd.Series:
    return access_raw(df) * (0.50 + service_raw(df))


def dispersion_weight_raw(df: pd.DataFrame) -> pd.Series:
    """Shared allocation weight for event, land-use, and line-demand dispersion."""
    return (
        0.20
        + access_raw(df)
        + 0.50 * service_raw(df)
        + 0.50 * numeric(df, "line_network_value")
        + 0.50 * numeric(df, "relative_demand_pressure")
        + 0.20 * numeric(df, "commute_demand_score")
    )


def score_components(df: pd.DataFrame, model_raw: pd.Series) -> pd.DataFrame:
    result = df.copy()
    result["model_demand_score"] = min_max(model_raw)
    result["access_demand_score"] = min_max(access_raw(result))
    result["access_service_demand_score"] = min_max(access_service_raw(result))
    result["density_activity_demand_score"] = min_max(
        0.35 * numeric(result, "distance_weighted_residential_density")
        + 0.25 * numeric(result, "distance_weighted_connectivity")
        + 0.25 * numeric(result, "residential_temporal_demand")
        + 0.15 * numeric(result, "office_temporal_demand")
    )
    land_use_base = numeric(result, "commute_demand_score") + 0.20 * numeric(
        result, "distance_weighted_office_jobs"
    )
    result["land_use_time_demand_score"] = min_max(
        land_use_base * dispersion_weight_raw(result)
    )
    result["connectivity_demand_score"] = min_max(
        numeric(result, "distance_weighted_connectivity")
        + 100.0 * numeric(result, "distance_weighted_transfer_score")
    )
    result["service_demand_score"] = min_max(
        numeric(result, "scheduled_trains")
        + numeric(result, "line_service_weight")
        + 0.05 * numeric(result, "daily_scheduled_trains")
    )
    result["line_demand_score"] = min_max(
        numeric(result, "line_combined_weight")
        + numeric(result, "line_service_weight")
        + numeric(result, "line_connected_demand")
        + numeric(result, "line_junction_weight")
        + numeric(result, "line_network_value")
    )
    return result


def combined_demand_raw(scored: pd.DataFrame) -> pd.Series:
    return (
        0.25 * numeric(scored, "model_demand_score")
        + 0.20 * numeric(scored, "density_activity_demand_score")
        + 0.15 * numeric(scored, "service_demand_score")
        + 0.10 * numeric(scored, "access_demand_score")
        + 0.05 * numeric(scored, "access_service_demand_score")
        + 0.10 * numeric(scored, "connectivity_demand_score")
        + 0.10 * numeric(scored, "land_use_time_demand_score")
        + 0.05 * numeric(scored, "line_demand_score")
    )


def score_candidates(model: HistGradientBoostingRegressor, candidates: pd.DataFrame) -> pd.DataFrame:
    scored = candidates.copy()
    model_raw = pd.Series(np.expm1(model.predict(prepare_matrix(scored))).clip(min=0), index=scored.index)
    scored = score_components(scored, model_raw)
    scored["relative_demand_pressure_raw"] = combined_demand_raw(scored)
    scored["relative_demand_pressure"] = min_max(scored["relative_demand_pressure_raw"])
    scored["event_surplus_flow"] = 0.0
    scored["event_demand_score"] = 0.0
    scored["scenario_demand_pressure_raw"] = scored["relative_demand_pressure_raw"]
    scored["scenario_demand_pressure"] = scored["relative_demand_pressure"]
    scored["demand_score"] = scored["relative_demand_pressure"]
    return scored


def event_bins_with_weights(
    event: object,
    bin_minutes: int,
    tail_bins: int,
    tail_decay: float,
) -> list[tuple[int, int, float]]:
    if (
        hasattr(event, "start_minute")
        and hasattr(event, "end_minute")
        and not pd.isna(getattr(event, "start_minute"))
        and not pd.isna(getattr(event, "end_minute"))
    ):
        start = int(getattr(event, "start_minute"))
        end = int(getattr(event, "end_minute"))
    else:
        start = int(event.start_hour) * 60
        end = int(event.end_hour) * 60 + 59
    first = start // bin_minutes * bin_minutes
    last = end // bin_minutes * bin_minutes
    day = int(event.day_of_week)

    bins: list[tuple[int, int, float]] = []
    for absolute_bin in range(first, last + 1, bin_minutes):
        bins.append(((day + absolute_bin // 1440) % 7, absolute_bin % 1440, 1.0))

    tail_decay = max(0.0, min(float(tail_decay), 1.0))
    for index in range(max(0, tail_bins)):
        absolute_bin = last + bin_minutes * (index + 1)
        bins.append(((day + absolute_bin // 1440) % 7, absolute_bin % 1440, tail_decay ** (index + 1)))
    return [(day_value, time_bin, weight) for day_value, time_bin, weight in bins if weight > 0]


def apply_event_surplus(
    scored: pd.DataFrame,
    events_csv: str | None,
    bin_minutes: int,
    tail_bins: int,
    tail_decay: float,
) -> pd.DataFrame:
    if not events_csv:
        return scored
    events = pd.read_csv(events_csv)
    required = {"lat", "lon", "day_of_week", "surplus_users"}
    missing = required.difference(events.columns)
    if missing:
        raise ValueError(f"Event scenario CSV missing columns: {', '.join(sorted(missing))}")
    has_hour_window = {"start_hour", "end_hour"}.issubset(events.columns)
    has_minute_window = {"start_minute", "end_minute"}.issubset(events.columns)
    if not has_hour_window and not has_minute_window:
        raise ValueError("Event scenario CSV must include start_hour/end_hour or start_minute/end_minute")

    result = scored.copy()
    if "time_bin" not in result:
        result["time_bin"] = result["hour"] * 60
    for event in events.itertuples(index=False):
        bins = event_bins_with_weights(event, bin_minutes, tail_bins, tail_decay)
        if not bins:
            continue
        radius_m = float(getattr(event, "radius_m", 1500) or 1500)
        decay_m = float(getattr(event, "decay_m", 800) or 800)
        total_weight = sum(weight for _, _, weight in bins)
        if total_weight <= 0:
            continue
        for day, time_bin, temporal_weight in bins:
            mask = (result["day_of_week"] == day) & (result["time_bin"] == time_bin)
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
            allocation_weights = weights * np.clip(dispersion_weight_raw(subset).to_numpy(), 0.05, None)
            if allocation_weights.sum() == 0:
                continue
            bin_users = float(event.surplus_users) * temporal_weight / total_weight
            result.loc[mask, "event_surplus_flow"] += bin_users * allocation_weights / allocation_weights.sum()

    result["event_demand_score"] = min_max(result["event_surplus_flow"])
    result["scenario_demand_pressure_raw"] = (
        result["relative_demand_pressure_raw"] + 0.20 * result["event_demand_score"]
    )
    result["scenario_demand_pressure"] = min_max(
        result["scenario_demand_pressure_raw"]
    )
    result["demand_score"] = result["scenario_demand_pressure"]
    return result


def scenario_delta(baseline: pd.DataFrame, scenario: pd.DataFrame) -> pd.DataFrame:
    keys = ["cell_id", "day_of_week"]
    if "time_bin" in baseline.columns and "time_bin" in scenario.columns:
        keys.append("time_bin")
    else:
        keys.append("hour")
    left = baseline[keys + ["demand_score", "relative_demand_pressure_raw"]].rename(
        columns={
            "demand_score": "baseline_demand_score",
            "relative_demand_pressure_raw": "baseline_demand_pressure_raw",
        }
    )
    right = scenario.rename(columns={"demand_score": "scenario_demand_score"})
    output = right.merge(left, on=keys, how="left")
    if "scenario_demand_pressure_raw" not in output:
        output["scenario_demand_pressure_raw"] = output["relative_demand_pressure_raw"]
    output["demand_delta"] = (
        output["scenario_demand_pressure_raw"] - output["baseline_demand_pressure_raw"]
    )
    denominator = output["baseline_demand_pressure_raw"].replace(0, np.nan)
    output["percent_change"] = (output["demand_delta"] / denominator * 100).replace(
        [np.inf, -np.inf], np.nan
    ).fillna(0)
    return output


def write_prediction_grid(scored: pd.DataFrame, output_path: Path) -> None:
    grid_columns = ["cell_id", "row", "col", "min_lat", "min_lon", "max_lat", "max_lon"]
    grid = scored.groupby("cell_id", as_index=False).agg(
        {
            "row": "first",
            "col": "first",
            "min_lat": "first",
            "min_lon": "first",
            "max_lat": "first",
            "max_lon": "first",
            "scenario_demand_score": "mean",
            "demand_delta": "mean",
            "percent_change": "mean",
        }
    )
    grid["demand_score"] = grid["scenario_demand_score"]
    write_grid_geojson(
        grid[grid_columns + [column for column in grid.columns if column not in grid_columns]],
        output_path,
    )


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
    scenario = apply_event_surplus(
        scenario,
        args.event_scenarios_csv,
        args.time_bin_minutes,
        args.event_tail_bins,
        args.event_tail_decay,
    )
    output = scenario_delta(baseline, scenario)

    output_path = out_dir / (
        DEMAND_HEATMAP_SCENARIO_PREDICTIONS_CSV
        if args.scenario_features or args.event_scenarios_csv
        else DEMAND_HEATMAP_PREDICTIONS_CSV
    )
    output.to_csv(output_path, index=False)
    write_prediction_grid(output, out_dir / DEMAND_HEATMAP_GRID_GEOJSON)

    metrics.update(
        {
            "baseline_candidate_rows": int(len(baseline_candidates)),
            "scenario_candidate_rows": int(len(scenario)),
            "event_scenarios": bool(args.event_scenarios_csv),
            "event_tail_bins": args.event_tail_bins if args.event_scenarios_csv else 0,
            "event_tail_decay": args.event_tail_decay if args.event_scenarios_csv else 0,
            "predictions": str(output_path),
        }
    )
    metrics_path = out_dir / DEMAND_HEATMAP_MODEL_METRICS_JSON
    metrics_path.write_text(json.dumps(metrics, indent=2))
    print(json.dumps({"metrics": str(metrics_path), **metrics}, indent=2))


if __name__ == "__main__":
    main()
