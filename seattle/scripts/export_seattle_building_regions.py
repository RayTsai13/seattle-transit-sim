#!/usr/bin/env python3
"""Export local Seattle building GeoJSON chunks with joined heights."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import ssl
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BUILDING_OUTLINES_SERVICE_URL = (
    "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/ArcGIS/rest/services/"
    "Building_Outlines_2023/FeatureServer/0/query"
)

REGIONS = {
    "downtown-core": {
        "west": -122.3700,
        "south": 47.5850,
        "east": -122.3080,
        "north": 47.6325,
    },
    "east-neighborhoods": {
        "west": -122.3200,
        "south": 47.5850,
        "east": -122.2550,
        "north": 47.6760,
    },
    "northwest-seattle": {
        "west": -122.4300,
        "south": 47.6205,
        "east": -122.3220,
        "north": 47.6900,
    },
    "west-seattle": {
        "west": -122.4320,
        "south": 47.5430,
        "east": -122.3400,
        "north": 47.6040,
    },
    "beacon-hill": {
        "west": -122.3365,
        "south": 47.5500,
        "east": -122.2840,
        "north": 47.6015,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export local Seattle building GeoJSON chunks with joined heights."
    )
    parser.add_argument(
        "--join-csv",
        default="../data/processed/seattle_building_height_join.csv",
        help="Joined height CSV path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--out-dir",
        default="../../public/seattle",
        help="Output directory relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--result-record-count",
        type=int,
        default=2000,
        help="FeatureServer page size.",
    )
    return parser.parse_args()


def fetch_url(url: str, timeout: int = 60) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "gridlock/0.1",
            "Accept-Encoding": "gzip",
        },
    )
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    with urlopen(request, timeout=timeout, context=ssl_context) as response:
        payload = response.read()
        if response.headers.get("Content-Encoding") == "gzip" or payload[:2] == b"\x1f\x8b":
            return gzip.decompress(payload)
        return payload


def fetch_json(url: str, timeout: int = 60) -> dict:
    return json.loads(fetch_url(url, timeout).decode("utf-8"))


def load_height_lookup(path: Path) -> dict[str, float]:
    lookup: dict[str, float] = {}
    with path.open() as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            object_id = row.get("footprint_object_id", "").strip()
            height_m_raw = row.get("building_height_m", "").strip()
            if not object_id or not height_m_raw:
                continue
            try:
                height_m = float(height_m_raw)
            except ValueError:
                continue
            if math.isnan(height_m) or height_m < 1.5 or height_m > 350:
                continue
            lookup[object_id] = round(height_m, 3)
    return lookup


def build_region_url(bounds: dict[str, float], result_offset: int, page_size: int) -> str:
    params = {
        "where": "1=1",
        "geometry": f"{bounds['west']},{bounds['south']},{bounds['east']},{bounds['north']}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID,AREA,PIN",
        "returnGeometry": "true",
        "outSR": "4326",
        "orderByFields": "OBJECTID ASC",
        "resultRecordCount": str(page_size),
        "resultOffset": str(result_offset),
        "f": "geojson",
    }
    return f"{BUILDING_OUTLINES_SERVICE_URL}?{urlencode(params)}"


def fetch_region_geojson(bounds: dict[str, float], page_size: int) -> dict:
    features: list[dict] = []
    result_offset = 0

    while True:
        payload = fetch_json(build_region_url(bounds, result_offset, page_size))
        page_features = payload.get("features") or []
        features.extend(page_features)

        exceeded = bool((payload.get("properties") or {}).get("exceededTransferLimit"))
        if not exceeded or not page_features:
            return {
                "type": "FeatureCollection",
                "features": features,
            }

        result_offset += len(page_features)


def apply_heights(region_geojson: dict, height_lookup: dict[str, float]) -> dict:
    features: list[dict] = []
    for feature in region_geojson["features"]:
        object_id = feature.get("properties", {}).get("OBJECTID")
        height_m = height_lookup.get(str(object_id), 0)
        features.append(
            {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    "OBJECTID": object_id,
                    "AREA": feature.get("properties", {}).get("AREA", 0),
                    "PIN": feature.get("properties", {}).get("PIN", ""),
                    "height_m": height_m,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def summarize(region_name: str, region_geojson: dict) -> dict:
    heights = [
        feature["properties"]["height_m"]
        for feature in region_geojson["features"]
        if feature["properties"]["height_m"] > 0
    ]
    return {
        "region": region_name,
        "features": len(region_geojson["features"]),
        "features_with_height": len(heights),
        "height_m_min": min(heights) if heights else None,
        "height_m_max": max(heights) if heights else None,
        "height_m_avg": (sum(heights) / len(heights)) if heights else None,
    }


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    join_csv = base_dir / args.join_csv
    out_dir = (base_dir / args.out_dir).resolve()

    height_lookup = load_height_lookup(join_csv)
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    for region_name, bounds in REGIONS.items():
        geojson = fetch_region_geojson(bounds, args.result_record_count)
        geojson_with_heights = apply_heights(geojson, height_lookup)

        out_path = out_dir / f"seattle-buildings-{region_name}.geojson"
        out_path.write_text(json.dumps(geojson_with_heights, separators=(",", ":")))

        summary = summarize(region_name, geojson_with_heights)
        summary["out_path"] = f"/seattle/{out_path.name}"
        summaries.append(summary)

    summary_path = out_dir / "seattle-building-regions-summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2))
    print(json.dumps({"summary_path": str(summary_path), "regions": summaries}, indent=2))


if __name__ == "__main__":
    main()
