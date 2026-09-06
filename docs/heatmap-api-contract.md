# Heatmap Data API Contract

## Overview

The heatmap runtime streams composed demand-density frames to the frontend map over **Server-Sent Events (SSE)** and accepts scenario/state operations over normal HTTP endpoints.

The frontend renderer should treat each streamed frame as the current display state. It should not need to know whether a cell's density came from the baseline model, an active scenario, or both. Scenario deltas and state rebasing are backend responsibilities.

Both processes run locally:

- **Demand heatmap runtime**: `http://localhost:8000`
- **Frontend dev server**: `http://localhost:5173`

The current runtime implementation lives in `data_processing/src/runtime/api.py`.

---

## API Surfaces

The frontend talks to the runtime over these surfaces:

1. **SSE stream**: `GET /api/heatmap/stream`
2. **Create scenario from precomputed delta**: `POST /api/scenarios`
3. **Inspect current state**: `GET /api/states/current`
4. **Inspect scenario status**: `GET /api/scenarios/{scenario_id}/status`
5. **Inspect state delta summary**: `GET /api/states/{state_version}/deltas`

SSE is one-way from backend to frontend, so user actions that mutate state use HTTP requests alongside the stream.

---

## Grid Configuration

The runtime owns the grid. It loads the grid bounds from the baseline demand prediction CSV and sends them to the frontend on connect.

```json
{
  "bounds": {
    "west": -122.3566585,
    "south": 47.5026095,
    "east": -122.2132615,
    "north": 47.723038
  },
  "rows": 50,
  "cols": 22
}
```

### Cell Indexing

- **Origin**: top-left, northwest corner of the bounding box.
- **Row**: increases southward.
- **Col**: increases eastward.
- **Cell center**:
  - `lon = west + (col + 0.5) * cell_width`
  - `lat = north - (row + 0.5) * cell_height`

No geometry is transmitted on the wire. The frontend computes point coordinates from this shared config.

---

## SSE Stream

### Endpoint

```text
GET http://localhost:8000/api/heatmap/stream
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
```

The frontend connects with `new EventSource(url)`.

### `config`

Sent once on connect. Confirms the grid parameters the stream will use.

```text
id: 0
event: config
data: {"bounds":{"west":-122.3566585,"south":47.5026095,"east":-122.2132615,"north":47.723038},"rows":50,"cols":22}
```

### `frame`

Sent repeatedly while the simulation runs. Each frame is sparse on the wire but is semantically a complete snapshot for the current simulation time and state.

```text
id: 42
event: frame
data: {"timestamp":1714070400.0,"state_version":"state_v1","sim_time":{"day_of_week":0,"time_bin":510,"minute_of_week":510},"cells":[[12,34,0.82],[13,34,0.65]]}
```

### Event IDs

Every event includes a monotonically increasing `id`. If the SSE connection drops, the browser may reconnect with `Last-Event-ID`. The runtime may resume from that point later, but the current safe behavior is to resend `config` and continue streaming current frames.

---

## Frame Schema

```json
{
  "timestamp": 1714070400.0,
  "state_version": "state_v1",
  "sim_time": {
    "day_of_week": 0,
    "time_bin": 510,
    "minute_of_week": 510
  },
  "cells": [
    [12, 34, 0.82],
    [13, 34, 0.65]
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `timestamp` | float | Unix timestamp when the frame is emitted |
| `state_version` | string | Current immutable simulation state ID |
| `sim_time` | object | Simulated time within the repeating week |
| `day_of_week` | int | 0-indexed day in the repeating week |
| `time_bin` | int | Minute-of-day model bin, e.g. `510` for 08:30 |
| `minute_of_week` | int | `day_of_week * 1440 + minute_of_day` |
| `cells` | array | Sparse list of `[row, col, density]` tuples |
| `row` | int | 0-indexed grid row |
| `col` | int | 0-indexed grid column |
| `density` | float | Normalized display intensity, range `[0.0, 1.0]` |

### Frame Semantics

- **Sparse payload**: cells with zero or below-threshold density may be omitted.
- **Complete snapshot**: each `frame` replaces the previous frame entirely. It is not a frontend-applied delta.
- **Composed state**: `density` already includes baseline demand plus all active scenario effects for `state_version` and `sim_time`.
- **Normalization**: the backend is responsible for clamping/normalizing density to `[0.0, 1.0]`.

---

## Scenario State Semantics

The runtime tracks immutable state versions.

```text
state_baseline = baseline only
state_v1 = baseline + first scenario delta
state_v2 = baseline + first scenario delta + second scenario delta
```

Each scenario record contains:

- `scenario_id`
- `scenario_type`
- `state_before`
- `state_after`
- `created_at_real_time`
- `created_at_sim_time`
- `effective_from_tick`
- `effective_from_sim_time`
- `delta_source`
- `delta_frame_count`
- `delta_changed_cells`

The delta registered for a scenario should already represent:

```text
score(state_after) - score(state_before)
```

That is what lets the runtime handle scenarios introduced after previous user edits without always comparing to the original baseline.

---

## `POST /api/scenarios`

Register a scenario from a precomputed scenario-delta CSV. The runtime currently accepts `type: "precomputed_delta"`.

### Request

```http
POST /api/scenarios
Content-Type: application/json
```

```json
{
  "type": "precomputed_delta",
  "scenario_id": "event_downtown_game",
  "delta_csv": "curr_data/processed/model_outputs/demand_heatmap_scenario_predictions.csv",
  "effective_from_tick": 120
}
```

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `type` | string | No | Must be `precomputed_delta`; defaults to `precomputed_delta` |
| `scenario_id` | string | No | Stable scenario ID; generated if omitted |
| `delta_csv` | string | Yes | Path to a scenario output CSV containing `demand_delta` or scenario/baseline score columns |
| `effective_from_tick` | int | No | Simulation tick when this scenario becomes active; defaults to current tick |
| `state_after` | string | No | Explicit next state version; generated if omitted |

### Response

```json
{
  "scenario_id": "event_downtown_game",
  "scenario_type": "precomputed_delta",
  "state_before": "state_baseline",
  "state_after": "state_v1",
  "created_at_real_time": 1714070400.0,
  "created_at_sim_time": {
    "day_of_week": 0,
    "time_bin": 510,
    "minute_of_week": 510
  },
  "effective_from_tick": 120,
  "effective_from_sim_time": {
    "day_of_week": 0,
    "time_bin": 510,
    "minute_of_week": 510
  },
  "status": "ready",
  "delta_source": "curr_data/processed/model_outputs/demand_heatmap_scenario_predictions.csv",
  "delta_frame_count": 15,
  "delta_changed_cells": 852
}
```

### Errors

- `400 Bad Request`: unsupported scenario type, missing `delta_csv`, unreadable CSV, or invalid CSV schema.
- `409 Conflict`: reserved for duplicate `scenario_id` handling if exposed by the state manager.

---

## `GET /api/states/current`

Returns the current runtime state and registered scenarios.

```json
{
  "state_version": "state_v1",
  "current_tick": 120,
  "sim_time": {
    "day_of_week": 0,
    "time_bin": 510,
    "minute_of_week": 510
  },
  "scenarios": []
}
```

---

## `GET /api/scenarios/{scenario_id}/status`

Returns the scenario record for a registered scenario.

### Errors

- `404 Not Found`: unknown `scenario_id`.

---

## `GET /api/states/{state_version}/deltas`

Returns the scenario-delta summary associated with a state version.

This endpoint does not currently return every changed cell. It returns metadata such as source file, frame count, and changed-cell count.

### Errors

- `404 Not Found`: unknown `state_version`.

---

## Backend Implementation Requirements

1. **CORS**: allow requests from `http://localhost:5173` or `*`.
2. **SSE response headers**:
   - `Content-Type: text/event-stream`
   - `Cache-Control: no-cache`
   - `X-Accel-Buffering: no`
3. **SSE event format**: `id: <int>\nevent: <type>\ndata: <json>\n\n`
4. **JSON request bodies**: `Content-Type: application/json` on `POST /api/scenarios`.
5. **On client disconnect**: stop generating frames for that connection.
6. **Backend-composed frames**: frontend should not apply scenario deltas to the heatmap stream.

---

## Frontend Connection Example

```typescript
const source = new EventSource("http://localhost:8000/api/heatmap/stream");

source.addEventListener("config", (e) => {
  const config = JSON.parse(e.data);
  // initialize grid
});

source.addEventListener("frame", (e) => {
  const frame = JSON.parse(e.data);
  // convert frame.cells to GeoJSON and update MapLibre source
});

async function registerScenario(deltaCsv: string) {
  const response = await fetch("http://localhost:8000/api/scenarios", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      type: "precomputed_delta",
      delta_csv: deltaCsv,
    }),
  });
  return response.json();
}
```

The heatmap stream is independent from the Seattle building layer. The frontend map loads buildings from local cached files in `public/seattle/` and overlays the SSE heatmap on top.

---

## Error Handling

- **SSE drops**: `EventSource` auto-reconnects. The runtime resends `config` and continues streaming frames.
- **Repeated `config` events**: frontend should handle them idempotently.
- **Scenario registration failures**: surface to the user instead of retrying silently.
