# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The project (package name `gridlock`) is a Seattle-focused transit/demand
visualization: a React + TypeScript map frontend backed by a FastAPI service that streams a simulated foot-traffic
heatmap over Server-Sent Events (SSE).

## Commands

Frontend (Node/Vite, run from repo root):

```bash
npm install          # install deps
npm run dev          # Vite dev server (frontend)
npm run build        # tsc -b && vite build
npm run lint         # eslint .
npm run preview      # preview production build
```

Backend / heatmap SSE servers (Python, FastAPI + uvicorn):

```bash
# Real GeoJSON-backed simulation backend
pip install -r backend/requirements.txt
uvicorn backend.server:app --host 0.0.0.0 --port 8000

# Fully synthetic mock (no real data) — same SSE contract
npm run mock:server   # == uvicorn mock.server:app --host 0.0.0.0 --port 8000
```

Tests:

```bash
# Backend + mock parity. Run from the repository root.
pip install -r backend/requirements-dev.txt
pytest backend/tests
pytest data_processing/tests    # needs data_processing/requirements.txt

# Frontend
npm test          # vitest run
npm run test:watch
```

`backend/requirements-dev.txt` is required: the suite uses `pytest-asyncio` (for the SSE
stream tests) and `httpx` (for `TestClient`), neither of which is a runtime dependency.
`pyproject.toml` sets `asyncio_mode = "auto"`; without it the async tests are silently
skipped rather than run.

CI (`.github/workflows/ci.yml`) runs all of the above plus `npm run lint` and `npm run build`,
and `deploy.yml` will not deploy unless it passes.

Docker (production frontend + backend only):

```bash
docker compose up --build      # frontend on http://localhost:8080, proxies /api/* to backend
```

## Architecture

**Frontend (`src/`)** — React 19 + TypeScript + Vite. `react-map-gl/maplibre` renders the base map with a
`@deck.gl` overlay (`DeckGLOverlay.tsx`) for 3D layers.
- `src/heatmap/` — SSE client and heatmap rendering: `stream.ts` (`useHeatmap` hook / SSE consumer), `grid.ts`,
  `layer.ts`, `api.ts`.
- `src/stops/` — transit stops, lines, and animated trains: `data.ts` (stops/lines + GeoJSON helpers),
  `layers.ts` (MapLibre layer defs), `trains.ts`/`train_service.ts`/`track_geometry.ts` (train synthesis &
  interpolation along tracks).
- `src/App.tsx` — top-level map composition, wiring heatmap + stops + deck.gl layers together.
- Buildings load from cached Seattle GeoJSON region files in `public/seattle/`, starting downtown and expanding
  outward. Extrusion heights come from Seattle's `Seattle_BuildingShells` joined onto `Building_Outlines_2023`.

**Backend (`backend/`)** — FastAPI app in `server.py`. A single frame-producer task, owned by
the app's `lifespan`, advances the clock and composes one frame per tick; SSE connections are
pure readers of that shared frame. Never advance playback or compose from inside a connection
generator — doing so makes the simulation run N times too fast with N clients and breaks the
wire contract's declared-rate guarantee. The ticker holds the clock while no one is connected. A land-use demand simulation: each grid cell holds residents,
jobs, students, and visitors; per-hour trip rates turn those into trips/hour; transit stops absorb a stated capacity
in trips/hour; whatever is left over is streamed as density over SSE. Key modules: `landuse.py` (districts → per-cell
population, totals conserved), `demand.py` (trips-per-person-per-hour tables), `network.py` (stops/lines +
`ServiceCapacity` allocation), `overlays.py` (crowd drops, also in trips), `composer.py` (unmet trips → density on a
fixed absolute scale), `grid.py`, `state.py`, `sim_time.py`, `geo.py`. There is no ML at runtime — the GeoJSON supplies
grid geometry only. Notable endpoints:
`GET /api/heatmap/stream` (SSE), `GET /healthz`, `POST /api/scenario`, `GET|POST /api/playback`,
`POST /api/playback/seek`, and `GET|POST|DELETE /api/people`.

**`mock/server.py`** — synthetic drifting-hotspot heatmap over a 200×170 grid; satisfies the same SSE contract as
the real backend. Use it to animate the frontend without real data.

**Data pipelines:**
- `seattle/` — Seattle-specific data pipeline (building shell heights, footprint joins, neighborhood GeoJSON export
  to `public/seattle/`, heatmap grid + model). See `seattle/README.md`.
- `data_processing/` — offline feature-building only, not used at runtime: shared / non-Seattle data processing
  utilities (GTFS in `data_processing/gtfs/`, scripts, src, tests). Nothing here is imported by `backend/`, and it is
  excluded from the Docker build. The model-training stage was removed; there is no ML anywhere in this repo.
  `data_processing/ARCHITECTURE.md`, `DEMAND_PIPELINE_PROCESS.md`, and `INTERACTIVE_TIMELAPSE_PROPOSAL.md` are
  historical design records of that removed pipeline and are banner-marked as such.

## Key contracts & docs

- SSE heatmap API contract: `docs/heatmap-api-contract.md` (both `backend/` and `mock/` implement it).
- Frontend heatmap integration: `docs/frontend-heatmap.md`. Frontend listens on
  `http://localhost:8000/api/heatmap/stream`.
- Map architecture: `docs/seattle-map-architecture.md`.

## Backend env overrides

- `HEATMAP_GEOJSON` — path to the `FeatureCollection` of `r{row}_c{col}` cells, read for grid bounds/rows/cols only
  (default `seattle/data/processed/seattle_heatmap_grid.geojson`).
- `HEATMAP_FRAME_INTERVAL` — seconds between frame ticks (default `1.0`).
- `HEATMAP_SIM_STEP_SECONDS` — sim-clock advance per frame (default `1800`).

Simulation tuning lives in code, not env vars: `STOP_CAPACITY_TRIPS_PER_HOUR` / `WALK_RADIUS_M` (`network.py`),
`MAX_UNMET_TRIPS_PER_CELL_HOUR` / `DENSITY_GAMMA` (`composer.py`), the district table (`landuse.py`), and the trip
rate tables (`demand.py`).

`HEATMAP_GEOJSON` defaults to a path resolved against the repository root, so the server no
longer depends on the working directory.

## Known constraints

- **Run the backend with a single worker.** `STATE` is process-global, so `uvicorn --workers N`
  yields N independent, diverging simulations.
- **~380 MB of generated artifacts are tracked in git** (`.git` is ~204 MB). `.gitignore` lists
  `seattle/data/processed/`, `public/seattle/seattle-buildings*.geojson`, `data_processing/gtfs/`
  and `data_processing/curr_data/raw/`, but every one of those files was committed before the
  rule existed, so the entries are inert — the files stay tracked and `git add` on a regenerated
  copy still works. Only one of them is a genuine runtime dependency:
  `backend.Dockerfile` copies `seattle/data/processed/seattle_heatmap_grid.geojson` (840 KB).
  The rest are regenerable via the `seattle/` pipeline. Removing them needs a history rewrite
  plus a manual re-clone on the droplet, so it has been left alone deliberately.
- **`src/App.tsx` is a ~1,400-line component.** It mixes the building-region loading scheduler,
  camera policy, basemap restyling, the time-dial pointer maths, crowd drag/drop, deck.gl layer
  construction and all markup. `demandMetricsFromDensity` also puts demand-model logic in the
  view layer. The natural split is `useBuildingRegions` / `useTimeDial` / `useCrowdDrop`.
- **`src/stops/train_service.ts` is ~19k lines of generated data as TypeScript source**, parsed
  as JavaScript in the main bundle at startup; it belongs in `public/` as JSON. It also carries
  `sourceFeedEndDate: "20260828"`, which nothing checks.
- `docker-compose.yml` still has `yourdockerhubuser/...` image placeholders, and the `Caddyfile`
  has no site address, so Caddy's automatic HTTPS never activates despite `443:443` being
  published.

## Conventions

- Frontend is ESM TypeScript; imports use explicit `.ts`/`.tsx` extensions (see `src/App.tsx`). Match this style.
- Keep the frontend and both heatmap servers agreeing on the SSE contract when changing frame shape or grid config.
