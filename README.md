# Gridlock

Seattle-focused map prototype for building and eventually visualizing foot-traffic heatmaps.

The current frontend is a `React + TypeScript + Vite` app that uses `MapLibre` for the base map, cached local Seattle building region files for structure, and an SSE heatmap overlay for simulated/model output.

## Project Areas

- `src/`
  - frontend app, MapLibre map, Seattle building rendering, SSE heatmap overlay
- `docs/`
  - frontend/API documentation
- `seattle/`
  - Seattle-specific data pipeline, cached asset export scripts, processed artifacts
- `data_processing/`
  - shared or non-Seattle data processing utilities
- `public/seattle/`
  - cached Seattle building region assets served directly by the frontend

## Current Map Architecture

- Buildings load from local cached Seattle GeoJSON region files in `public/seattle/`
- Region loading starts with downtown, then expands outward as additional areas are needed
- Extrusion heights come from Seattle's official `Seattle_BuildingShells` scene data, joined offline onto `Building_Outlines_2023` footprints
- Heatmap data is a separate SSE overlay path documented in [`docs/frontend-heatmap.md`](docs/frontend-heatmap.md)

See:
- [`docs/seattle-map-architecture.md`](docs/seattle-map-architecture.md)
- [`seattle/README.md`](seattle/README.md)

## Development

Install dependencies:

```bash
npm install
```

Run the frontend:

```bash
npm run dev
```

Build the frontend:

```bash
npm run build
```

## Docker

The containerized stack includes only the production frontend and the GeoJSON-backed backend.
It does not package development-only assets like `mock/`, docs, or data-processing scripts.

Run the app with Docker Compose:

```bash
docker compose up --build
```

Then open:

- `http://localhost:8080`

The frontend is served by `nginx` and proxies `/api/*` to the FastAPI backend inside the compose network.

On a server or droplet, run detached and verify backend health:

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1/healthz
```

The `backend` service now exposes an internal `/healthz` endpoint and Docker
marks it `healthy` only when that endpoint responds successfully.

## Heatmap Servers

Two implementations of the SSE contract live in this repo:

- `mock/server.py` — fully synthetic, drifting hotspots over a 200×170 grid.
  Useful for animating the frontend without any real data.
- `backend/server.py` — skeleton that loads `seattle_heatmap_grid.geojson`,
  infers the grid config, and streams its normalized cell densities.

Either one satisfies the contract in [`docs/heatmap-api-contract.md`](docs/heatmap-api-contract.md).

Run the mock:

```bash
pip install fastapi uvicorn
uvicorn mock.server:app --host 0.0.0.0 --port 8000
```

Or, if your Python env already has `uvicorn`, use the repo script:

```bash
npm run mock:server
```

Run the GeoJSON-backed skeleton:

```bash
pip install -r backend/requirements.txt
uvicorn backend.server:app --host 0.0.0.0 --port 8000
```

Override which file/property the skeleton reads via env vars:

- `HEATMAP_GEOJSON` — path to a `FeatureCollection` of `r{row}_c{col}` cells
- `HEATMAP_DENSITY_PROPERTY` — which numeric property to normalize as density
- `HEATMAP_FRAME_INTERVAL` — seconds between frame ticks (default `1.0`)

The frontend listens to:

- `http://localhost:8000/api/heatmap/stream`

## Seattle Data Pipeline

Seattle-specific scripts now live under `seattle/scripts/`.

Typical building-data flow:

1. Extract official 3D shell heights from the City scene layer
2. Join heights onto official 2023 building footprints
3. Export cached neighborhood GeoJSON chunks into `public/seattle/`

Typical heatmap-data flow:

1. Build Seattle grid features
2. Train or evaluate the Seattle heatmap model

See [`seattle/README.md`](seattle/README.md) for the Seattle workspace layout and commands.



