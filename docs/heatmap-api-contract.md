# Heatmap Data API Contract

## Overview

The backend streams composed demand-density frames to the frontend map over
**Server-Sent Events (SSE)** and accepts simulation controls over normal HTTP
endpoints.

The frontend treats each streamed frame as the complete current display state.
Frames are sparse but are **snapshots, not deltas** — a cell absent from a frame
is zero, not unchanged.

Two implementations satisfy this contract:

- `backend/server.py` — the real simulation (land use → demand → transit capacity).
- `mock/server.py` — a fully synthetic drifting-hotspot generator, no data files.

Both listen on `http://localhost:8000`. The frontend never uses an absolute API host: it
requests the same-origin path `/api/*`, which the Vite dev server (port 5173) and nginx (in
the container) each proxy to the backend. See `vite.config.ts` and `nginx.conf`.

Both are exercised by the same test suite -- `backend/tests/test_server_parity.py` runs every
endpoint-level assertion in this document against both apps, so the mock cannot drift.

---

## API Surfaces

| Method | Path | Used by frontend |
|---|---|---|
| GET | `/api/heatmap/stream` | yes — the SSE stream |
| POST | `/api/scenario` | yes — deploy a transit build-out |
| POST | `/api/playback` | yes — play / pause |
| POST | `/api/playback/seek` | yes — time dial |
| POST | `/api/people` | yes — drop a crowd |
| DELETE | `/api/people/{id}` | yes |
| DELETE | `/api/people` | yes |
| GET | `/api/playback` | no — diagnostics |
| GET | `/api/people` | no — diagnostics |
| GET | `/healthz` | no — container health check |

---

## Grid Configuration

The backend owns the grid and sends its dimensions on connect. The frontend
adapts to whatever size it receives.

```json
{
  "bounds": {
    "west": -122.4597,
    "south": 47.481,
    "east": -122.22653013600585,
    "north": 47.73252712899757
  },
  "rows": 56,
  "cols": 35
}
```

### Cell Indexing

- **Origin**: top-left, northwest corner of the bounding box.
- **Row**: increases southward. **Col**: increases eastward.
- **Cell center**:
  - `lon = west + (col + 0.5) * cell_width`
  - `lat = north - (row + 0.5) * cell_height`

No geometry is transmitted on the wire; the frontend derives coordinates from
this config. `backend/grid.py` enforces the uniformity this formula assumes by
dropping partial edge cells and normalizing row orientation.

---

## SSE Stream

### Endpoint

```
GET /api/heatmap/stream
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
```

Wire format, with a monotonically increasing integer `id`:

```
id: 0
event: config
data: {"bounds": {...}, "rows": 56, "cols": 35}

```

### Handshake order is load-bearing

On connect the server **must** emit, in this order:

1. `config` — the grid. The frontend discards every frame that arrives before it.
2. `scenario` — `{"scenario_id": "line-1"}`.
3. `playback` — the full playback state (see below).

Then `frame` events repeat, with `scenario` and `playback` re-emitted whenever
they change. The frontend's loading overlay clears only once it has seen *all
four* of config, a confirmed scenario, at least one frame, and a playback state.
Omitting the initial `playback` event leaves the app stuck on the loading screen.

### `frame`

```json
{
  "timestamp": 1714070400.0,
  "state_version": "state_v1",
  "sim_time": { "day_of_week": 0, "time_bin": 510, "minute_of_week": 510 },
  "cells": [[12, 34, 0.82], [13, 34, 0.65]]
}
```

| Field | Type | Notes |
|---|---|---|
| `timestamp` | float | Unix seconds. Not read by the frontend. |
| `state_version` | string | `state_v<N>`, **N non-decreasing**. Frames older than the newest N seen from a `POST /api/scenario` response are dropped. |
| `sim_time.day_of_week` | int | 0 = Sunday. |
| `sim_time.time_bin` | int | Minute-of-day, a multiple of `time_bin_minutes`. |
| `sim_time.minute_of_week` | int | `day_of_week * 1440 + minute_of_day`. |
| `cells` | array | `[row, col, density]`, `density` in `(0, 1]`. |

Cells at or below the display threshold are omitted.

### `playback`

```json
{
  "is_playing": true,
  "current_tick": 0,
  "sim_step_seconds": 1800,
  "sim_minutes_per_second": 30.0,
  "frame_interval_seconds": 1.0,
  "time_bin_minutes": 30,
  "sim_time": { "day_of_week": 0, "time_bin": 0, "minute_of_week": 0 }
}
```

`sim_minutes_per_second × frame_interval_seconds` **must** equal the real
advance in `minute_of_week` between consecutive playback events. The frontend's
train interpolator drives its animation from this rate and resyncs when its own
clock drifts, so a wrong value makes trains run at the wrong speed.

Two consequences for implementors:

- **One producer.** The clock must be advanced by a single process-wide ticker, never from
  inside a per-connection stream generator — otherwise N open tabs advance it N times per
  interval and the declared rate is a lie.
- **Whole minutes.** `minute_of_week` is an integer, so `sim_step_seconds` must be a whole
  number of minutes. A fractional step reports a rate the clock cannot actually achieve.

`POST /api/playback` with a `sim_minutes_per_second` changes how fast time passes and **must
not move `sim_time`**: the clock is stored absolutely, not derived from `current_tick ×
sim_step_seconds`. `current_tick` is a monotonic count of frames played, with no fixed
relationship to `sim_time`.

### `clear`

Optional. Resets the frontend to an empty grid. Neither server emits it.

---

## `POST /api/scenario`

```json
{
  "scenario_id": "line-1-2-ballard",
  "stops": [{ "id": "ballard", "name": "Ballard", "coordinates": [-122.3765, 47.6677] }],
  "lines": [{ "id": "ballard-line", "name": "Ballard Line", "stopIds": ["ballard", "interbay"], "path": [[-122.3765, 47.6677], [-122.3765, 47.6478]] }]
}
```

`scenario_id` must be one of `line-1`, `line-1-2`, `line-1-2-ballard`; anything
else is a 400. `stops` and `lines` are optional — omit or malform them and the
server falls back to its built-in network for that id. Extra keys on stops and
lines (the frontend sends `color` and `offset`) must be tolerated.

### Response

```json
{ "scenario_id": "line-1-2-ballard", "frame": { "...": "a full frame" } }
```

**`frame` must be non-null.** The frontend gates all SSE frames from the moment
`setScenario` is called until either this frame arrives or a matching `scenario`
event does. Returning `{"scenario_id": ...}` alone freezes the heatmap.

---

## Playback

`POST /api/playback` takes any subset of `{"is_playing": bool,
"sim_minutes_per_second": float > 0}`; absent keys are unchanged.

`POST /api/playback/seek` takes either `{"minute_of_week": int}` or
`{"day_of_week": int, "time_bin": int}`.

Both return the **full** playback state, not a partial. `GET /api/playback`
returns the same shape.

---

## People (crowd drops)

`POST /api/people` → **201**

```json
{ "lat": 47.606, "lon": -122.333, "count": 416, "kind": "crowd", "duration_minutes": 240 }
```

`lat`/`lon` are required and must fall inside the grid bounds (else 400).
`count` defaults to 1. `kind`, `duration_minutes`, `radius_m`, and `decay_m` are
optional tuning fields.

Response is `{"id", "lat", "lon", "count"}`, plus the tuning fields if any were
sent. The UI drops a crowd as **12 concurrent POSTs**, so the endpoint must
handle a burst.

`DELETE /api/people/{id}` → 204, or 404 for an unknown id.
`DELETE /api/people` → 204, clears all.
`GET /api/people` → `{"people": [{"id", "lat", "lon", "count"}, ...]}`.

---

## Backend Implementation Requirements

- CORS must allow the frontend origin (both servers use `*`).
- Send the SSE headers above and do not buffer responses.
- Stop the generator when the client disconnects.
- Compose frames server-side; the frontend applies no deltas.
- Every mutating endpoint should wake open streams so the change is visible
  immediately rather than at the next tick.

---

## Frontend Connection Example

```ts
const source = new EventSource('/api/heatmap/stream');

let config: GridConfig | null = null;
source.addEventListener('config', (e) => { config = JSON.parse(e.data); });
source.addEventListener('frame', (e) => {
  if (!config) return;                       // frames before config are dropped
  const frame = JSON.parse(e.data);
  render(frameToGeoJSON(frame.cells, config));
});
```

See `src/heatmap/stream.ts` for the authoritative client, including the pending
scenario gate and the `state_version` ordering check.

---

## Error Handling

Non-2xx responses throw in the frontend API layer. SSE errors are logged and
the browser reconnects automatically; the server must tolerate reconnects and
replay the full handshake each time.
