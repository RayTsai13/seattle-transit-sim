#!/usr/bin/env python3
"""Train a baseline model from the generated Seattle heatmap feature CSV."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import train_test_split

from src.common.artifacts import (
    DEFAULT_PROCESSED_DIR,
    SEATTLE_HEATMAP_FEATURES_CSV,
    SEATTLE_HEATMAP_MODEL_METRICS_JSON,
    SEATTLE_HEATMAP_PREDICTIONS_CSV,
)


FEATURE_COLUMNS = [
    "center_lat",
    "center_lon",
    "hour",
    "day_of_week",
    "transit_departures",
    "unique_stops",
    "employment_jobs",
    "accessibility_score",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a baseline proxy congestion model from heatmap features."
    )
    parser.add_argument(
        "--features-csv",
        default=str(DEFAULT_PROCESSED_DIR / SEATTLE_HEATMAP_FEATURES_CSV),
        help="Feature CSV produced by src.pipelines.seattle.build_heatmap_dataset.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_PROCESSED_DIR),
        help="Directory for metrics and scored prediction outputs.",
    )
    parser.add_argument(
        "--min-target-rows",
        type=int,
        default=50,
        help="Minimum rows with observed targets required for training.",
    )
    return parser.parse_args()


def train_model(features: pd.DataFrame, min_target_rows: int) -> tuple[dict, pd.DataFrame | None]:
    missing = [column for column in FEATURE_COLUMNS + ["target_count"] if column not in features]
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    observed = features[features["target_count"] > 0].copy()
    if len(observed) < min_target_rows:
        return (
            {
                "skipped": True,
                "reason": f"Fewer than {min_target_rows} observed target rows.",
                "observed_rows": int(len(observed)),
            },
            None,
        )

    train, test = train_test_split(observed, test_size=0.25, random_state=42)
    model = HistGradientBoostingRegressor(random_state=42)
    model.fit(train[FEATURE_COLUMNS], train["target_count"])
    test_predictions = model.predict(test[FEATURE_COLUMNS])

    metrics = {
        "skipped": False,
        "rows": int(len(observed)),
        "mae": float(mean_absolute_error(test["target_count"], test_predictions)),
        "rmse": float(math.sqrt(mean_squared_error(test["target_count"], test_predictions))),
        "target": "target_count",
        "note": "Baseline proxy model trained only where observed counts exist.",
    }

    scored = features[["cell_id", "center_lat", "center_lon", "hour", "day_of_week"]].copy()
    scored["predicted_target_count"] = model.predict(features[FEATURE_COLUMNS])
    return metrics, scored


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    features = pd.read_csv(args.features_csv)
    metrics, scored = train_model(features, args.min_target_rows)

    metrics_path = out_dir / SEATTLE_HEATMAP_MODEL_METRICS_JSON
    metrics_path.write_text(json.dumps(metrics, indent=2))
    if scored is not None:
        scored.to_csv(out_dir / SEATTLE_HEATMAP_PREDICTIONS_CSV, index=False)

    print(json.dumps({"metrics": str(metrics_path), **metrics}, indent=2))


if __name__ == "__main__":
    main()
