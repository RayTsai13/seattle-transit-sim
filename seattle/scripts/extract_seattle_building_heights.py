#!/usr/bin/env python3
"""Extract official Seattle building height attributes from the SceneServer.

This script walks the City of Seattle `Seattle_BuildingShells` I3S node tree,
decodes the published attribute bundles, and writes a flat CSV keyed by the
scene-layer object ids. It does not attempt to spatially join to the newer
2023 footprint layer; that needs an additional matching step.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import ssl
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen


SCENE_LAYER_URL = (
    "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/ArcGIS/rest/services/"
    "Seattle_BuildingShells/SceneServer/layers/0"
)
HEIGHT_FT_TO_M = 0.30480060960121924


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract official Seattle building height attributes from the SceneServer."
    )
    parser.add_argument(
        "--out-csv",
        default="../data/processed/seattle_scene_heights.csv",
        help="CSV output path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--summary-json",
        default="../data/processed/seattle_scene_heights_summary.json",
        help="Summary JSON path relative to seattle/scripts/.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Concurrent node attribute fetches.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--max-leaf-nodes",
        type=int,
        help="Optional limit for the number of leaf nodes to process (useful for testing).",
    )
    return parser.parse_args()


def build_json_url(path: str) -> str:
    return f"{path}?f=json"


def fetch_url(url: str, timeout: int) -> bytes:
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

def fetch_json(url: str, timeout: int) -> dict:
    return json.loads(fetch_url(url, timeout).decode("utf-8"))


def decode_uint32_attribute(payload: bytes) -> list[int]:
    count = struct.unpack_from("<I", payload, 0)[0]
    if count == 0:
        return []
    return list(struct.unpack_from(f"<{count}I", payload, 4))


def decode_int32_attribute(payload: bytes) -> list[int]:
    count = struct.unpack_from("<I", payload, 0)[0]
    if count == 0:
        return []
    return list(struct.unpack_from(f"<{count}i", payload, 4))


def decode_float64_attribute(payload: bytes) -> list[float]:
    count = struct.unpack_from("<I", payload, 0)[0]
    if count == 0:
        return []
    offset = 8 if len(payload) >= 8 + count * 8 else 4
    return list(struct.unpack_from(f"<{count}d", payload, offset))


def decode_string_attribute(payload: bytes) -> list[str]:
    count, _byte_count = struct.unpack_from("<II", payload, 0)
    if count == 0:
        return []

    lengths = struct.unpack_from(f"<{count}I", payload, 8)
    offset = 8 + 4 * count
    values: list[str] = []
    cursor = offset
    for length in lengths:
        raw = payload[cursor : cursor + length]
        values.append(raw.decode("utf-8", errors="replace").rstrip("\x00"))
        cursor += length
    return values


def extract_scene_centroids(node_url: str, timeout: int) -> dict[int, tuple[float, float]]:
    feature_payload = fetch_json(build_json_url(node_url + "/features/0"), timeout)
    geometry_payload = fetch_url(node_url + "/geometries/0", timeout)

    vertex_count, feature_count = struct.unpack_from("<II", geometry_payload, 0)
    positions = struct.unpack_from(f"<{vertex_count * 3}f", geometry_payload, 8)

    features = feature_payload.get("featureData") or []
    if feature_count != len(features):
        raise ValueError(
            f"{node_url} feature count mismatch: geometry={feature_count}, features={len(features)}"
        )

    centroids: dict[int, tuple[float, float]] = {}
    for feature in features:
        face_range = feature["geometries"][0]["params"]["faceRange"]
        start_face, end_face = face_range
        start_offset = start_face * 9
        end_offset = (end_face + 1) * 9
        coords = positions[start_offset:end_offset]
        if not coords:
            continue

        xs = coords[0::3]
        ys = coords[1::3]
        centroid_lon = feature["position"][0] + (sum(xs) / len(xs))
        centroid_lat = feature["position"][1] + (sum(ys) / len(ys))
        centroids[int(feature["id"])] = (centroid_lon, centroid_lat)

    return centroids


def walk_leaf_nodes(timeout: int, max_leaf_nodes: int | None = None) -> list[str]:
    leaf_urls: list[str] = []
    stack = [SCENE_LAYER_URL + "/nodes/root"]

    while stack:
        node_url = stack.pop()
        node = fetch_json(build_json_url(node_url), timeout)
        children = node.get("children") or []
        if children:
            for child in children:
                stack.append(SCENE_LAYER_URL + "/nodes/" + child["id"])
            continue
        if node.get("featureData"):
            leaf_urls.append(node_url)
            if max_leaf_nodes is not None and len(leaf_urls) >= max_leaf_nodes:
                break

    return sorted(leaf_urls)


def fetch_node_rows(node_url: str, timeout: int) -> list[dict]:
    object_ids = decode_uint32_attribute(fetch_url(node_url + "/attributes/f_0/0", timeout))
    if not object_ids:
        return []

    height_ft = decode_float64_attribute(fetch_url(node_url + "/attributes/f_1/0", timeout))
    eave_height_ft = decode_float64_attribute(fetch_url(node_url + "/attributes/f_2/0", timeout))
    building_fid = decode_string_attribute(fetch_url(node_url + "/attributes/f_4/0", timeout))
    original_fid = decode_int32_attribute(fetch_url(node_url + "/attributes/f_8/0", timeout))
    original_oid = decode_int32_attribute(fetch_url(node_url + "/attributes/f_11/0", timeout))
    centroids = extract_scene_centroids(node_url, timeout)

    expected = len(object_ids)
    columns = [
        ("BLDGHEIGHT", height_ft),
        ("EAVEHEIGHT", eave_height_ft),
        ("BuildingFID", building_fid),
        ("OriginalFID", original_fid),
        ("OriginalOID", original_oid),
    ]
    for name, values in columns:
        if len(values) != expected:
            raise ValueError(
                f"{node_url} {name} length mismatch: expected {expected}, got {len(values)}"
            )

    rows = []
    node_id = node_url.rsplit("/", 1)[-1]
    for index, object_id in enumerate(object_ids):
        height = height_ft[index]
        eave = eave_height_ft[index]
        centroid_lon, centroid_lat = centroids.get(int(object_id), ("", ""))
        rows.append(
            {
                "scene_node_id": node_id,
                "object_id": int(object_id),
                "scene_centroid_lon": centroid_lon,
                "scene_centroid_lat": centroid_lat,
                "building_height_ft": height,
                "building_height_m": height * HEIGHT_FT_TO_M if not math.isnan(height) else "",
                "eave_height_ft": eave,
                "eave_height_m": eave * HEIGHT_FT_TO_M if not math.isnan(eave) else "",
                "building_fid": building_fid[index],
                "original_fid": int(original_fid[index]),
                "original_oid": int(original_oid[index]),
            }
        )
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scene_node_id",
        "object_id",
        "scene_centroid_lon",
        "scene_centroid_lat",
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


def summarize(rows: list[dict], leaf_nodes: int) -> dict:
    heights = [
        row["building_height_ft"]
        for row in rows
        if isinstance(row["building_height_ft"], (int, float)) and not math.isnan(row["building_height_ft"])
    ]
    eaves = [
        row["eave_height_ft"]
        for row in rows
        if isinstance(row["eave_height_ft"], (int, float)) and not math.isnan(row["eave_height_ft"])
    ]
    centroid_rows = [
        row
        for row in rows
        if isinstance(row["scene_centroid_lon"], (int, float))
        and isinstance(row["scene_centroid_lat"], (int, float))
    ]
    return {
        "rows": len(rows),
        "leaf_nodes": leaf_nodes,
        "rows_with_scene_centroid": len(centroid_rows),
        "height_field": "BLDGHEIGHT",
        "eave_height_field": "EAVEHEIGHT",
        "height_units_inferred": "feet",
        "height_to_meter_factor": HEIGHT_FT_TO_M,
        "building_height_ft_min": min(heights) if heights else None,
        "building_height_ft_max": max(heights) if heights else None,
        "building_height_ft_avg": sum(heights) / len(heights) if heights else None,
        "eave_height_ft_min": min(eaves) if eaves else None,
        "eave_height_ft_max": max(eaves) if eaves else None,
        "eave_height_ft_avg": sum(eaves) / len(eaves) if eaves else None,
        "note": (
            "This file extracts official Seattle 3D shell attributes only. "
            "Joining to Building_Outlines_2023 still requires a separate matching step."
        ),
    }


def main() -> None:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    out_csv = base_dir / args.out_csv
    summary_json = base_dir / args.summary_json

    leaf_nodes = walk_leaf_nodes(args.timeout, args.max_leaf_nodes)
    if args.max_leaf_nodes is not None:
        print(json.dumps({"selected_leaf_nodes": len(leaf_nodes)}, indent=2))

    rows: list[dict] = []
    completed_nodes = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {
            executor.submit(fetch_node_rows, node_url, args.timeout): node_url
            for node_url in leaf_nodes
        }
        for future in as_completed(futures):
            rows.extend(future.result())
            completed_nodes += 1
            print(json.dumps({"completed_nodes": completed_nodes, "current_rows": len(rows)}))

    rows.sort(key=lambda row: row["object_id"])
    write_csv(rows, out_csv)

    summary = summarize(rows, len(leaf_nodes))
    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, indent=2))

    print(
        json.dumps(
            {
                "out_csv": str(out_csv),
                "summary_json": str(summary_json),
                **summary,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
