# Frontend Heatmap Implementation

How the frontend consumes the heatmap data stream and renders it on the map.

See [heatmap-api-contract.md](./heatmap-api-contract.md) for the full wire protocol.
See [seattle-map-architecture.md](./seattle-map-architecture.md) for the cached Seattle building layer that sits underneath the heatmap.

---

## SSE Connection Lifecycle

1. On app mount, open an `EventSource` to `http://localhost:8000/api/heatmap/stream`.
2. On `config` event: store the grid parameters (bounds, rows, cols). Precompute a lookup from `(row, col)` to `[lon, lat]` cell centroids using the formulas in the API contract. This lookup is static and only rebuilt if a new `config` arrives.
3. On `frame` event: convert the sparse cell array into GeoJSON and update the map source.
4. On `clear` event: set the map source to an empty FeatureCollection.
5. On component unmount: close the EventSource.

Reconnection is automatic (built-in `EventSource` behavior). A new `config` event on reconnect re-initializes the grid idempotently.

The stream frame is already the composed display state. The frontend should not add scenario deltas to baseline values for the primary heatmap layer. It should render the `cells` array exactly as the backend emits it.

---

## Grid-to-GeoJSON Conversion

Each frame arrives as metadata plus a sparse array of `[row, col, density]` tuples. The frontend converts `cells` to a GeoJSON `FeatureCollection` of **Point** features, one per active cell, positioned at the cell centroid:

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

The `[lon, lat]` for each cell is read from the precomputed centroid lookup (see above). No geometry math happens per-frame, just an array index. The current frontend can ignore `state_version` and `sim_time` until playback controls or scenario status UI need them.

---

## MapLibre Heatmap Layer

The GeoJSON point source feeds a MapLibre `heatmap` layer. This gives smooth, blurred heat visuals rather than showing raw grid cells.

Key layer properties:

| Property               | Value                                      | Purpose                                             |
|------------------------|--------------------------------------------|-----------------------------------------------------|
| `heatmap-weight`       | `["get", "density"]`                       | Each point's contribution scales with its density   |
| `heatmap-intensity`    | Interpolated by zoom                       | Keeps visual intensity consistent across zoom levels|
| `heatmap-radius`       | Interpolated by zoom                       | Blur radius grows/shrinks with zoom                 |
| `heatmap-color`        | Color ramp from transparent → yellow → red | Standard density color scale                        |
| `heatmap-opacity`      | ~0.7                                       | Semi-transparent so buildings show through           |

The heatmap layer is placed **above** the building fill/extrusion layers in the map's layer stack so density is visible over the Seattle building footprints.

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

When the user adds a station, line, event, or frequency change, the frontend should call a normal HTTP mutation endpoint such as `POST /api/scenarios`. The backend creates a new immutable `state_version`, computes the affected demand deltas from `state_before` to `state_after`, and then the SSE stream starts emitting composed frames for the new state when ready.

For the heatmap layer, the frontend continues doing the same work:

```text
receive frame -> convert cells to GeoJSON -> update MapLibre source
```

The frontend may later use `state_version` and `sim_time` to show controls, loading states, or compare baseline vs scenario, but those fields are not required for rendering the heatmap.

---

## Module Structure

| Module              | Responsibility                                                        |
|---------------------|-----------------------------------------------------------------------|
| `src/heatmap/grid.ts`   | Grid config types, centroid precomputation, frame-to-GeoJSON conversion |
| `src/heatmap/stream.ts` | EventSource connection, event parsing, lifecycle management            |
| `src/heatmap/layer.ts`  | MapLibre heatmap layer style definition                                |
| `src/App.tsx`            | Wires stream → grid → Source/Layer into the existing Seattle map      |
