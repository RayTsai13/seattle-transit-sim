#!/usr/bin/env python3
"""Create train/test CSVs from transformed Delhi trip features."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split

from src.common.artifacts import (
    DEFAULT_PROCESSED_DIR,
    DELHI_TEST_FEATURES_CSV,
    DELHI_TRAIN_FEATURES_CSV,
    DELHI_TRAIN_TEST_SUMMARY_JSON,
    DELHI_TRIP_FEATURES_CSV,
)
from src.common.io_utils import ensure_dir, write_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split Delhi trip features into train/test files.")
    parser.add_argument(
        "--features-csv",
        default=str(DEFAULT_PROCESSED_DIR / DELHI_TRIP_FEATURES_CSV),
        help="Input trip features from src.pipelines.delhi.transform_metro.",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_PROCESSED_DIR), help="Output directory.")
    parser.add_argument("--test-size", type=float, default=0.2, help="Test split fraction.")
    parser.add_argument("--random-state", type=int, default=42, help="Deterministic split seed.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = ensure_dir(Path(args.out_dir))
    features = pd.read_csv(args.features_csv)
    if "target_passengers" not in features.columns:
        raise ValueError("Input features must include target_passengers")

    usable = features[features["target_passengers"].notna()].copy()
    train, test = train_test_split(
        usable,
        test_size=args.test_size,
        random_state=args.random_state,
        stratify=usable["is_weekend"] if "is_weekend" in usable.columns else None,
    )

    train_path = write_csv(train, out_dir / DELHI_TRAIN_FEATURES_CSV)
    test_path = write_csv(test, out_dir / DELHI_TEST_FEATURES_CSV)
    summary = {
        "input_rows": int(len(features)),
        "usable_rows": int(len(usable)),
        "train_rows": int(len(train)),
        "test_rows": int(len(test)),
        "train": str(train_path),
        "test": str(test_path),
    }
    (out_dir / DELHI_TRAIN_TEST_SUMMARY_JSON).write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
