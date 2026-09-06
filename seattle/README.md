# Seattle Reference

This folder is the Seattle-specific workspace for the project.

For the frontend/runtime map architecture, see [docs/seattle-map-architecture.md](../docs/seattle-map-architecture.md).

## Layout

- `scripts/` (all build-time only; the backend imports none of them)
  - `build_seattle_station_vectors.py`
    - Builds Seattle station activity vectors from GTFS and Census inputs.
  - `extract_seattle_building_heights.py`
    - Extracts official building height attributes from Seattle's `Seattle_BuildingShells` SceneServer.
  - `join_seattle_building_heights.py`
    - Joins extracted scene-layer heights onto official `Building_Outlines_2023` footprints.
  - `export_seattle_building_height_lookup.py`
    - Exports a compact footprint-height lookup for frontend use.
    - Useful for debugging or alternate export paths, but not the main current runtime path.
  - `export_seattle_building_regions.py`
    - Exports cached Seattle neighborhood GeoJSON chunks for the map.
  - `fetch_seattle_buildings.py`
    - Legacy OSM footprint fetcher for quick local extracts.

- `data/raw/`
  - Seattle-only raw inputs that were moved out of `data_processing`.

- `data/processed/`
  - Seattle-only generated artifacts:
    - `seattle_scene_heights.csv`
    - `seattle_building_height_join.csv`
    - `seattle_heatmap_features.csv`
    - `seattle_heatmap_grid.geojson` — **the only file the backend reads at
      runtime**, and only for grid bounds / rows / cols
    - related summary files

## Frontend Assets

The Seattle map assets are served from `public/seattle/`.

Current cached building region files:

- `public/seattle/seattle-buildings-downtown-core.geojson`
- `public/seattle/seattle-buildings-east-neighborhoods.geojson`
- `public/seattle/seattle-buildings-northwest-seattle.geojson`
- `public/seattle/seattle-buildings-west-seattle.geojson`
- `public/seattle/seattle-buildings-beacon-hill.geojson`

Other related frontend assets:

- `public/seattle/seattle-building-heights.json`
  - supporting/legacy height lookup; current frontend mainly uses pre-joined region GeoJSON
- `public/seattle/seattle-building-regions-summary.json`

## Typical Flow

The building pipeline is the one that still matters day to day:

1. Run `extract_seattle_building_heights.py`
2. Run `join_seattle_building_heights.py`
3. Run `export_seattle_building_regions.py`
4. The frontend loads the cached region GeoJSON files from `public/seattle/`

The heatmap dataset builder moved to `data_processing/`
(`src/pipelines/seattle/build_heatmap_dataset.py`). The model-training stage
that once followed it has been removed: the backend generates demand
analytically from the land-use model in `backend/landuse.py`.

## Current Frontend Behavior

- Startup camera is centered on downtown Seattle.
- Buildings load from local cached files only.
- Runtime load order is:
  1. `Downtown Core`
  2. `East Neighborhoods`
  3. `Northwest Seattle`
  4. `Beacon Hill`
  5. `West Seattle`
- The map UI shows per-region loading state so neighborhood fetch progress is visible.

## Notes

- Runtime map loading should use local cached Seattle assets, not live ArcGIS building queries.
- The Seattle scripts resolve their default output paths relative to `seattle/scripts/`.
