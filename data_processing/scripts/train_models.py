#!/usr/bin/env python3
"""Train baseline and city-generic heatmap models from processed features."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train heatmap models.")
    parser.add_argument("--processed-dir", default="curr_data/processed")
    parser.add_argument("--features-dir", help="Feature input directory. Defaults to <processed-dir>/features.")
    parser.add_argument(
        "--model-output-dir",
        help="Model output directory. Defaults to <processed-dir>/model_outputs.",
    )
    parser.add_argument("--skip-seattle-baseline", action="store_true")
    parser.add_argument("--skip-demand", action="store_true")
    parser.add_argument(
        "--include-timelapse",
        action="store_true",
        help="Also run the older passenger-flow timelapse scorer.",
    )
    return parser.parse_args()


def run(command: list[str]) -> None:
    print(f"\n$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT_DIR, check=True)


def py(*args: str) -> list[str]:
    return [sys.executable, *args]


def main() -> None:
    args = parse_args()
    processed_dir = args.processed_dir
    features_dir = args.features_dir or f"{processed_dir}/features"
    model_output_dir = args.model_output_dir or f"{processed_dir}/model_outputs"

    if not args.skip_seattle_baseline:
        run(
            py(
                "-m",
                "src.models.train_heatmap_model",
                "--features-csv",
                f"{features_dir}/seattle_heatmap_features.csv",
                "--out-dir",
                model_output_dir,
            )
        )

    if not args.skip_demand:
        run(
            py(
                "-m",
                "src.models.train_demand_heatmap_model",
                "--training-features",
                f"{features_dir}/delhi_heatmap_training_features.csv",
                "--candidate-features",
                f"{features_dir}/city_heatmap_candidate_features.csv",
                "--out-dir",
                model_output_dir,
            )
        )

    if args.include_timelapse:
        run(
            py(
                "-m",
                "src.models.train_heatmap_timelapse_model",
                "--training-features",
                f"{features_dir}/delhi_heatmap_training_features.csv",
                "--candidate-features",
                f"{features_dir}/city_heatmap_candidate_features.csv",
                "--out-dir",
                model_output_dir,
            )
        )


if __name__ == "__main__":
    main()
