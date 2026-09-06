# Frontend Heatmap Implementation

How the frontend consumes the heatmap data stream and renders it on the map.

See [heatmap-api-contract.md](./heatmap-api-contract.md) for the full wire protocol.
See [seattle-map-architecture.md](./seattle-map-architecture.md) for the cached Seattle building layer that sits underneath the heatmap.

---

## SSE Connection Lifecycle

1. On app mount, open an `EventSource` to the same-origin path `/api/heatmap/stream`
   (`src/heatmap/stream.ts`). In dev, Vite proxies `/api` to `http://localhost:8000`;
   in the container, nginx proxies it to `http://backend:8000`. No absolute API host is
   ever baked into the bundle.
2. On `config` event: store the grid parameters (bounds, rows, cols). Cell centroids are derived from these using the formulas in the API contract. Frames arriving before a `config` event are discarded.
3. On `frame` event: convert the sparse cell array into GeoJSON and update the map source.
4. On `clear` event: set the map source to an empty FeatureCollection.
5. On component unmount: close the EventSource.

Reconnection is automatic (built-in `EventSource` behavior). A new `config` event on
reconnect re-initializes the grid idempotently **and resets the frame gates**: the
`state_version` floor and any in-flight scenario gate are cleared. This matters because a
restarted backend counts `state_version` from zero again -- without the reset every later
frame would sit below the floor and be dropped forever, freezing the map while the
connection still reported itself as open.

After three consecutive SSE errors the hook reports `connection: 'failed'`, and the
loading screen offers a retry rather than spinning indefinitely.

The stream frame is already the composed display state. The frontend should not add scenario deltas to baseline values for the primary heatmap layer. It should render the `cells` array exactly as the backend emits it.

---

## Grid-to-GeoJSON Conversion

Each frame arrives as metadata plus a sparse array of `[row, col, density]` tuples. `frameToGeoJSON` rasterizes them into a `rows x cols` buffer, then **bilinearly upsamples 3x3 per cell** and emits a GeoJSON `FeatureCollection` of **Point** features — roughly 9x the number of active cells.

Only cells within one step of a non-zero value are visited, since a sample reads its
8-neighbourhood: cost is proportional to the **active area**, not to the grid size. The
conversion is also skipped entirely while the heatmap layer is hidden (`renderGeometry`),
and the last raw frame is kept so re-enabling is instant:

```
Frame input:
  {
    "timestamp": ...,
    "state_version": "state_v4",
    "sim_time": { "day_of_week": 0, "time_bin": 510, "minute_of_week": 510 },
    "cells": [[12, 34, 0.82], [13, 34, 0.65]]
  }

GeoJSON output:
  {
    "type": "FeatureCollection",
    "features": [
      {
        "type": "Feature",
        "geometry": { "type": "Point", "coordinates": [<lon>, <lat>] },
        "properties": { "density": 0.82 }
      },
      {
        "type": "Feature",
        "geometry": { "type": "Point", "coordinates": [<lon>, <lat>] },
        "properties": { "density": 0.65 }
      }
    ]
  }
```

Both `state_version` and `sim_time` are load-bearing. `state_version` drives a frame-dropping gate that suppresses stale frames after a scenario switch, and `sim_time` feeds the time dial and the train interpolator.

---

## MapLibre Density Layer

The GeoJSON point source feeds a blurred MapLibre `circle` layer. Each point's color is mapped directly from its raw `density` property, so the same density value keeps the same color at every zoom level.

Key layer properties:

| Property          | Value                                      | Purpose                                             |
|-------------------|--------------------------------------------|-----------------------------------------------------|
| `circle-color`    | Color ramp from raw `density`              | Keeps color stable across zoom levels               |
| `circle-radius`   | Interpolated by zoom                       | Blur footprint grows/shrinks with zoom              |
| `circle-blur`     | Constant blur                              | Keeps a soft heatmap-like visual                    |
| `circle-opacity`  | Constant opacity                           | Semi-transparent so buildings show through          |
| `circle-sort-key` | `["get", "density"]`                       | Draws hotter samples above cooler samples           |

The density layer is placed **above** the building fill/extrusion layers in the map's layer stack so density is visible over the Seattle building footprints.

---

## Data Flow Summary

```
Baseline components + scenario state
    │
    │  Backend frame composer
    ▼
Python SSE server
    │
    │  SSE frame: { timestamp, state_version, sim_time, cells: [[row, col, density], ...] }
    ▼
EventSource listener
    │
    │  Sparse cells + precomputed centroid lookup
    ▼
Grid-to-GeoJSON conversion
    │
    │  FeatureCollection of Points with density property
    ▼
MapLibre GeoJSON source (react-map-gl <Source>)
    │
    ▼
MapLibre heatmap layer (react-map-gl <Layer>)
```

---

## Scenario State Boundary

Scenario state belongs behind the stream, not inside the MapLibre renderer.

When the user deploys a build-out, the frontend calls `POST /api/scenario` with the scenario id plus the active stops and lines. The backend rebuilds its transit capacity, bumps `state_version`, and returns a freshly composed frame; the SSE stream then continues from the new state. The frontend suppresses stream frames until that response (or a matching `scenario` event) confirms the switch.

For the heatmap layer, the frontend continues doing the same work:

```text
receive frame -> convert cells to GeoJSON -> update MapLibre source
```

`state_version` and `sim_time` are required: see the note on the frame gate above.

---

## Module Structure

| Module              | Responsibility                                                        |
|---------------------|-----------------------------------------------------------------------|
| `src/heatmap/grid.ts`   | Grid config types, centroid math, 3x3 upsampling, frame-to-GeoJSON conversion |
| `src/heatmap/stream.ts` | EventSource connection, event parsing, frame gating, lifecycle management |
| `src/heatmap/layer.ts`  | MapLibre heatmap layer style definition                                |
| `src/App.tsx`            | Wires stream → grid → Source/Layer into the existing Seattle map      |
