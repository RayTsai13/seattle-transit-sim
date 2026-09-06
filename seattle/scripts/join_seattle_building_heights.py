#!/usr/bin/env python3
"""Join official Seattle SceneServer heights onto 2023 building footprints.

This script expects the scene-height extractor CSV to already exist with
`scene_centroid_lon` / `scene_centroid_lat` columns. It fetches the official
`Building_Outlines_2023` centroids, finds the nearest footprint centroid for
each scene building within a distance threshold, and writes one joined row per
footprint.
"""

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


FOOTPRINT_QUERY_URL = (
    "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/ArcGIS/rest/services/"
    "Building_Outlines_2023/FeatureServer/0/query"
)
NO_GEOMETRY_PAGE_SIZE = 32000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join Seattle official SceneServer heights onto 2023 footprints."
    )
    parser.add_argument(
        "--scene-csv",
        default="../data/processed/seattle_scene_heights.csv",
        help="Scene-height CSV path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--out-csv",
        default="../data/processed/seattle_building_height_join.csv",
        help="Joined output CSV path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--summary-json",
        default="../data/processed/seattle_building_height_join_summary.json",
        help="Join summary JSON path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--max-distance-m",
        type=float,
        default=40.0,
        help="Maximum centroid-to-centroid match distance in meters.",
    )
    parser.add_argument(
        "--max-footprints",
        type=int,
        help="Optional cap for fetched footprints, useful for testing.",
    )
    parser.add_argument(
        "--bbox-padding-m",
        type=float,
        default=80.0,
        help="Padding around the scene centroid bbox when querying footprints.",
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


def footprint_query_url(result_offset: int, page_size: int, bounds: dict | None) -> str:
    params = {
        "where": "1=1",
        "outFields": "OBJECTID,OUTLINE_ID,PIN,AREA",
        "returnGeometry": "false",
        "returnCentroid": "true",
        "outSR": "4326",
        "orderByFields": "OBJECTID ASC",
        "resultRecordCount": str(page_size),
        "resultOffset": str(result_offset),
        "f": "json",
    }
    if bounds is not None:
        params["geometry"] = (
            f"{bounds['west']},{bounds['south']},{bounds['east']},{bounds['north']}"
        )
        params["geometryType"] = "esriGeometryEnvelope"
        params["inSR"] = "4326"
        params["spatialRel"] = "esriSpatialRelIntersects"
    return f"{FOOTPRINT_QUERY_URL}?{urlencode(params)}"


def fetch_all_footprint_centroids(
    bounds: dict | None,
    max_footprints: int | None = None,
) -> list[dict]:
    footprints: list[dict] = []
    result_offset = 0

    while True:
        payload = fetch_json(footprint_query_url(result_offset, NO_GEOMETRY_PAGE_SIZE, bounds))
        features = payload.get("features") or []
        if not features:
            break

        for feature in features:
            centroid = feature.get("centroid") or {}
            if "x" not in centroid or "y" not in centroid:
                continue
            attrs = feature["attributes"]
            footprints.append(
                {
                    "footprint_object_id": int(attrs["OBJECTID"]),
                    "footprint_outline_id": attrs.get("OUTLINE_ID", ""),
                    "footprint_pin": attrs.get("PIN", ""),
                    "footprint_area": attrs.get("AREA", ""),
                    "footprint_centroid_lon": float(centroid["x"]),
                    "footprint_centroid_lat": float(centroid["y"]),
                }
            )
            if max_footprints is not None and len(footprints) >= max_footprints:
                return footprints

        result_offset += len(features)
        if not payload.get("exceededTransferLimit"):
            break

    return footprints


def load_scene_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            lon = row.get("scene_centroid_lon", "")
            lat = row.get("scene_centroid_lat", "")
            height_ft = row.get("building_height_ft", "")
            if not lon or not lat or not height_ft:
                continue

            try:
                scene_lon = float(lon)
                scene_lat = float(lat)
                building_height_ft = float(height_ft)
            except ValueError:
                continue

            if math.isnan(scene_lon) or math.isnan(scene_lat) or math.isnan(building_height_ft):
                continue

            rows.append(
                {
                    "scene_node_id": row["scene_node_id"],
                    "scene_object_id": int(row["object_id"]),
                    "scene_centroid_lon": scene_lon,
                    "scene_centroid_lat": scene_lat,
                    "building_height_ft": building_height_ft,
                    "building_height_m": float(row["building_height_m"]) if row["building_height_m"] else "",
                    "eave_height_ft": float(row["eave_height_ft"]) if row["eave_height_ft"] else "",
                    "eave_height_m": float(row["eave_height_m"]) if row["eave_height_m"] else "",
                    "building_fid": row["building_fid"],
                    "original_fid": int(row["original_fid"]) if row["original_fid"] else "",
                    "original_oid": int(row["original_oid"]) if row["original_oid"] else "",
                }
            )
    return rows


def scene_bounds(scene_rows: list[dict], padding_m: float) -> dict | None:
    if not scene_rows:
        return None

    min_lon = min(row["scene_centroid_lon"] for row in scene_rows)
    max_lon = max(row["scene_centroid_lon"] for row in scene_rows)
    min_lat = min(row["scene_centroid_lat"] for row in scene_rows)
    max_lat = max(row["scene_centroid_lat"] for row in scene_rows)
    center_lat = (min_lat + max_lat) / 2.0

    lat_pad = padding_m / 110_540.0
    lon_pad = padding_m / (111_320.0 * max(math.cos(math.radians(center_lat)), 0.2))

    return {
        "west": min_lon - lon_pad,
        "south": min_lat - lat_pad,
        "east": max_lon + lon_pad,
        "north": max_lat + lat_pad,
    }


def grid_key(lon: float, lat: float, cell_deg: float) -> tuple[int, int]:
    return (math.floor(lon / cell_deg), math.floor(lat / cell_deg))


def distance_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    mean_lat_rad = math.radians((lat_a + lat_b) / 2.0)
    dx = (lon_a - lon_b) * 111_320.0 * math.cos(mean_lat_rad)
    dy = (lat_a - lat_b) * 110_540.0
    return math.hypot(dx, dy)


def build_grid(footprints: list[dict], cell_deg: float) -> dict[tuple[int, int], list[dict]]:
    grid: dict[tuple[int, int], list[dict]] = {}
    for footprint in footprints:
        key = grid_key(
            footprint["footprint_centroid_lon"],
            footprint["footprint_centroid_lat"],
            cell_deg,
        )
        grid.setdefault(key, []).append(footprint)
    return grid


def join_rows(
    scene_rows: list[dict],
    footprints: list[dict],
    max_distance_m: float,
) -> tuple[list[dict], set[int]]:
    cell_deg = max_distance_m / 111_320.0
    grid = build_grid(footprints, cell_deg)
    best_by_footprint: dict[int, dict] = {}
    matched_scene_object_ids: set[int] = set()

    for scene_row in scene_rows:
        key_lon = scene_row["scene_centroid_lon"]
        key_lat = scene_row["scene_centroid_lat"]
        cell_x, cell_y = grid_key(key_lon, key_lat, cell_deg)

        best_match: dict | None = None
        best_distance = max_distance_m
        for offset_x in (-1, 0, 1):
            for offset_y in (-1, 0, 1):
                candidates = grid.get((cell_x + offset_x, cell_y + offset_y), [])
                for footprint in candidates:
                    candidate_distance = distance_m(
                        key_lon,
                        key_lat,
                        footprint["footprint_centroid_lon"],
                        footprint["footprint_centroid_lat"],
                    )
                    if candidate_distance <= best_distance:
                        best_distance = candidate_distance
                        best_match = footprint

        if best_match is None:
            continue
        matched_scene_object_ids.add(scene_row["scene_object_id"])

        footprint_id = best_match["footprint_object_id"]
        joined_row = {
            **best_match,
            **scene_row,
            "match_distance_m": best_distance,
        }
        existing = best_by_footprint.get(footprint_id)
        if existing is None:
            best_by_footprint[footprint_id] = joined_row
            continue

        if joined_row["match_distance_m"] < existing["match_distance_m"]:
            best_by_footprint[footprint_id] = joined_row
        elif (
            joined_row["match_distance_m"] == existing["match_distance_m"]
            and joined_row["building_height_ft"] > existing["building_height_ft"]
        ):
            best_by_footprint[footprint_id] = joined_row

    return (
        sorted(best_by_footprint.values(), key=lambda row: row["footprint_object_id"]),
        matched_scene_object_ids,
    )


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "footprint_object_id",
        "footprint_outline_id",
        "footprint_pin",
        "footprint_area",
        "footprint_centroid_lon",
        "footprint_centroid_lat",
        "scene_node_id",
        "scene_object_id",
        "scene_centroid_lon",
        "scene_centroid_lat",
        "match_distance_m",
        "building_height_ft",
        "building_height_m",
        "eave_height_ft",
        "eave_height_m",
        "building_fid",
        "original_fid",
        "original_oid",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize(
    scene_rows: list[dict],
    footprints: list[dict],
    joined_rows: list[dict],
    matched_scene_object_ids: set[int],
    max_distance_m: float,
) -> dict:
    distances = [row["match_distance_m"] for row in joined_rows]
    return {
        "scene_rows_considered": len(scene_rows),
        "scene_rows_matched": len(matched_scene_object_ids),
        "scene_rows_unmatched": len(scene_rows) - len(matched_scene_object_ids),
        "footprints_fetched": len(footprints),
        "footprints_matched": len(joined_rows),
        "max_distance_m": max_distance_m,
        "match_distance_m_min": min(distances) if distances else None,
        "match_distance_m_max": max(distances) if distances else None,
        "match_distance_m_avg": sum(distances) / len(distances) if distances else None,
        "note": (
            "Matches are centroid-based and approximate. Review distance statistics before "
            "using the joined heights as production truth."
        ),
    }


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    scene_csv = base_dir / args.scene_csv
    out_csv = base_dir / args.out_csv
    summary_json = base_dir / args.summary_json

    scene_rows = load_scene_rows(scene_csv)
    bounds = scene_bounds(scene_rows, args.bbox_padding_m)
    footprints = fetch_all_footprint_centroids(bounds, args.max_footprints)
    joined_rows, matched_scene_object_ids = join_rows(scene_rows, footprints, args.max_distance_m)

    write_csv(joined_rows, out_csv)
    summary = summarize(
        scene_rows,
        footprints,
        joined_rows,
        matched_scene_object_ids,
        args.max_distance_m,
    )
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, indent=2))

    print(
        json.dumps(
            {
                "out_csv": str(out_csv),
                "summary_json": str(summary_json),
                "query_bounds": bounds,
                **summary,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
