#!/usr/bin/env python3
"""Fetch a Seattle-area building GeoJSON extract from OpenStreetMap Overpass."""

from __future__ import annotations

import argparse
import json
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Seattle building footprints as GeoJSON."
    )
    parser.add_argument(
        "--bbox",
        default="47.6015,-122.3525,47.6195,-122.3245",
        help="Bounding box as south,west,north,east.",
    )
    parser.add_argument(
        "--out",
        default="../../public/seattle/seattle-buildings.geojson",
        help="Output GeoJSON file path.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="HTTP timeout in seconds.",
    )
    return parser.parse_args()


def overpass_query(bbox: str) -> str:
    south, west, north, east = [part.strip() for part in bbox.split(",")]
    return f"""
[out:json][timeout:60];
(
  way["building"]({south},{west},{north},{east});
);
out tags geom;
"""


def fetch_overpass_data(query: str, timeout: int) -> dict:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    request = Request(
        OVERPASS_URL,
        data=query.encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "gridlock/0.1",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout, context=ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def close_ring(coords: list[list[float]]) -> list[list[float]]:
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def to_feature(element: dict) -> dict | None:
    geometry = element.get("geometry") or []
    if len(geometry) < 3:
        return None

    coords = [[point["lon"], point["lat"]] for point in geometry]
    coords = close_ring(coords)
    if len(coords) < 4:
        return None

    tags = element.get("tags") or {}
    properties = {
        "osm_id": element.get("id"),
        "source": "OpenStreetMap",
        "building": tags.get("building"),
        "name": tags.get("name"),
        "height": tags.get("height"),
        "min_height": tags.get("min_height"),
        "building:levels": tags.get("building:levels"),
    }

    return {
        "type": "Feature",
        "properties": {key: value for key, value in properties.items() if value not in (None, "")},
        "geometry": {
            "type": "Polygon",
            "coordinates": [coords],
        },
    }


def main() -> int:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    output_path = (base_dir / args.out).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        payload = fetch_overpass_data(overpass_query(args.bbox), args.timeout)
    except HTTPError as exc:
        raise SystemExit(f"Overpass request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise SystemExit(f"Overpass request failed: {exc.reason}") from exc

    features = []
    for element in payload.get("elements", []):
        if element.get("type") != "way":
            continue
        feature = to_feature(element)
        if feature is not None:
            features.append(feature)

    feature_collection = {
        "type": "FeatureCollection",
        "features": features,
    }
    output_path.write_text(json.dumps(feature_collection))

    print(
        json.dumps(
            {
                "features": len(features),
                "bbox": args.bbox,
                "out": str(output_path),
                "source": OVERPASS_URL,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
