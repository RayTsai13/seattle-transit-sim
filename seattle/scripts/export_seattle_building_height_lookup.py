#!/usr/bin/env python3
"""Export a compact footprint-id -> building height lookup for the frontend."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a compact Seattle building height lookup for the frontend."
    )
    parser.add_argument(
        "--join-csv",
        default="../data/processed/seattle_building_height_join.csv",
        help="Joined height CSV path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--out-json",
        default="../../public/seattle/seattle-building-heights.json",
        help="JSON output path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--min-height-m",
        type=float,
        default=1.5,
        help="Drop buildings shorter than this height in meters.",
    )
    parser.add_argument(
        "--max-height-m",
        type=float,
        default=350.0,
        help="Drop buildings taller than this height in meters.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    join_csv = base_dir / args.join_csv
    out_json = (base_dir / args.out_json).resolve()

    heights: dict[str, float] = {}
    kept_rows = 0
    skipped_rows = 0

    with join_csv.open() as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            object_id = row.get("footprint_object_id", "").strip()
            height_m_raw = row.get("building_height_m", "").strip()
            if not object_id or not height_m_raw:
                skipped_rows += 1
                continue

            try:
                height_m = float(height_m_raw)
            except ValueError:
                skipped_rows += 1
                continue

            if math.isnan(height_m) or height_m < args.min_height_m or height_m > args.max_height_m:
                skipped_rows += 1
                continue

            heights[object_id] = round(height_m, 3)
            kept_rows += 1

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(heights, separators=(",", ":")))

    print(
        json.dumps(
            {
                "out_json": str(out_json),
                "heights_written": len(heights),
                "rows_kept": kept_rows,
                "rows_skipped": skipped_rows,
                "min_height_m": args.min_height_m,
                "max_height_m": args.max_height_m,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
