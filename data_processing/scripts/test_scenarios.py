#!/usr/bin/env python3
"""Run scenario smoke tests for event, station, and frequency changes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test heatmap scenario scoring.")
    parser.add_argument("--processed-dir", default="curr_data/processed")
    parser.add_argument("--features-dir", help="Feature input directory. Defaults to <processed-dir>/features.")
    parser.add_argument(
        "--model-output-dir",
        help="Model output directory. Defaults to <processed-dir>/model_outputs.",
    )
    parser.add_argument("--station-gtfs-dir", default="gtfs_stations")
    parser.add_argument("--route-types", default="0,1,2")
    parser.add_argument("--agency-ids", default="40")
    parser.add_argument(
        "--event-scenarios-csv",
        default="examples/scenarios/seattle_event_scenario.csv",
        help="Event scenario CSV to test.",
    )
    parser.add_argument(
        "--line-stations-csv",
        default="examples/scenarios/proposed_line_stations.csv",
        help="Proposed line station coordinates to test.",
    )
    parser.add_argument(
        "--scenario-bbox",
        default="-122.35,47.58,-122.30,47.62",
        help="Small bbox for station/frequency scenario smoke tests.",
    )
    parser.add_argument("--cell-size-m", type=int, default=1000)
    parser.add_argument("--time-bin-minutes", type=int, default=30)
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def run(command: list[str]) -> None:
    print(f"\n$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT_DIR, check=True)


def py(*args: str) -> list[str]:
    return [sys.executable, *args]


def write_scenario_inputs(features_dir: str, temp_dir: Path) -> None:
    stations = pd.read_csv(ROOT_DIR / features_dir / "seattle_station_vectors.csv")
    if stations.empty:
        raise ValueError("seattle_station_vectors.csv is empty")

    first = stations.iloc[0]
    pd.DataFrame([{"station_id": first["station_id"]}]).to_csv(temp_dir / "removed_stations.csv", index=False)
    pd.DataFrame(
        [
            {
                "station_id": "scenario_test_station",
                "station_name": "Scenario Test Station",
                "lat": first["lat"],
                "lon": first["lon"],
                "activity_score": first["activity_score"],
                "connectivity_score": first["connectivity_score"],
                "activity_rank_pct": first["activity_rank_pct"],
                "is_transfer_proxy": 1,
                "connectivity": first["connectivity"],
                "residential_density_ratio": first["residential_density_ratio"],
            }
        ]
    ).to_csv(temp_dir / "added_stations.csv", index=False)
    pd.DataFrame(
        [{"station_id": "scenario_test_station", "hour": 8, "scheduled_trains_delta": 10}]
    ).to_csv(temp_dir / "frequency_delta.csv", index=False)


def summarize_event_output(model_output_dir: str) -> dict:
    path = ROOT_DIR / model_output_dir / "demand_heatmap_scenario_predictions.csv"
    scenario = pd.read_csv(path, usecols=["event_surplus_flow", "demand_delta"])
    return {
        "scenario_prediction_rows": int(len(scenario)),
        "event_surplus_sum": float(scenario["event_surplus_flow"].sum()),
        "demand_delta_sum": float(scenario["demand_delta"].sum()),
    }


@contextmanager
def scenario_temp_dir(keep_temp: bool) -> Iterator[Path]:
    if keep_temp:
        temp_dir = Path(tempfile.mkdtemp(prefix="heatmap_scenario_"))
        try:
            yield temp_dir
        finally:
            print(f"Temporary scenario files kept at: {temp_dir}")
    else:
        with tempfile.TemporaryDirectory(prefix="heatmap_scenario_") as temp_name:
            yield Path(temp_name)


def run_station_frequency_scenario(args: argparse.Namespace, temp_dir: Path) -> int:
    features_dir = args.features_dir_resolved
    write_scenario_inputs(features_dir, temp_dir)
    run(
        py(
            "-m",
            "src.pipelines.common.build_heatmap_candidates",
            "--station-vectors",
            f"{features_dir}/seattle_station_vectors.csv",
            "--gtfs-dir",
            args.station_gtfs_dir,
            "--out-dir",
            str(temp_dir),
            "--bbox",
            args.scenario_bbox,
            "--cell-size-m",
            str(args.cell_size_m),
            "--time-bin-minutes",
            str(args.time_bin_minutes),
            "--added-stations-csv",
            str(temp_dir / "added_stations.csv"),
            "--removed-stations-csv",
            str(temp_dir / "removed_stations.csv"),
            "--frequency-delta-csv",
            str(temp_dir / "frequency_delta.csv"),
            "--output-name",
            "scenario_candidate_features.csv",
            "--route-types",
            args.route_types,
            "--agency-ids",
            args.agency_ids,
        )
    )
    run(
        py(
            "-m",
            "src.models.train_demand_heatmap_model",
            "--training-features",
            f"{features_dir}/delhi_heatmap_training_features.csv",
            "--candidate-features",
            f"{features_dir}/city_heatmap_candidate_features.csv",
            "--scenario-features",
            str(temp_dir / "scenario_candidate_features.csv"),
            "--out-dir",
            str(temp_dir),
        )
    )
    return len(pd.read_csv(temp_dir / "demand_heatmap_scenario_predictions.csv"))


def run_line_weight_scenario(args: argparse.Namespace, temp_dir: Path) -> dict:
    features_dir = args.features_dir_resolved
    run(
        py(
            "scripts/build_line_weights.py",
            "--line-stations-csv",
            args.line_stations_csv,
            "--candidate-features",
            f"{features_dir}/city_heatmap_candidate_features.csv",
            "--out-dir",
            str(temp_dir),
            "--line-id",
            "scenario_test_line",
            "--station-vectors",
            f"{features_dir}/seattle_station_vectors.csv",
            "--gtfs-dir",
            args.station_gtfs_dir,
            "--route-types",
            args.route_types,
            "--agency-ids",
            args.agency_ids,
            "--time-bin-minutes",
            str(args.time_bin_minutes),
        )
    )
    run(
        py(
            "-m",
            "src.models.train_demand_heatmap_model",
            "--training-features",
            f"{features_dir}/delhi_heatmap_training_features.csv",
            "--candidate-features",
            f"{features_dir}/city_heatmap_candidate_features.csv",
            "--scenario-features",
            str(temp_dir / "proposed_line_candidate_features.csv"),
            "--out-dir",
            str(temp_dir),
        )
    )
    scenario = pd.read_csv(
        temp_dir / "demand_heatmap_scenario_predictions.csv",
        usecols=["line_service_weight", "demand_delta"],
    )
    return {
        "rows": int(len(scenario)),
        "line_weighted_rows": int((scenario["line_service_weight"] > 0).sum()),
        "demand_delta_sum": float(scenario["demand_delta"].sum()),
    }


def main() -> None:
    args = parse_args()
    processed_dir = args.processed_dir
    args.features_dir_resolved = args.features_dir or f"{processed_dir}/features"
    model_output_dir = args.model_output_dir or f"{processed_dir}/model_outputs"

    run(
        py(
            "-m",
            "src.models.train_demand_heatmap_model",
            "--training-features",
            f"{args.features_dir_resolved}/delhi_heatmap_training_features.csv",
            "--candidate-features",
            f"{args.features_dir_resolved}/city_heatmap_candidate_features.csv",
            "--event-scenarios-csv",
            args.event_scenarios_csv,
            "--out-dir",
            model_output_dir,
        )
    )
    event_summary = summarize_event_output(model_output_dir)

    with scenario_temp_dir(args.keep_temp) as temp_dir:
        scenario_rows = run_station_frequency_scenario(args, temp_dir)
        line_summary = run_line_weight_scenario(args, temp_dir)

    print(
        json.dumps(
            {
                "event_scenario": event_summary,
                "station_frequency_scenario_rows": int(scenario_rows),
                "line_weight_scenario": line_summary,
                "status": "ok",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
