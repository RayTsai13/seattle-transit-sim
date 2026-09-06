# Interactive Demand Timelapse Proposal

This proposal describes how the demand heatmap should evolve for a looping frontend timelapse where users can add stations, lines, frequency changes, and events, then immediately see how demand changes over time.

## Goal

The long-term product should feel like an interactive scenario sandbox:

1. The map continuously loops through 7 days x 48 half-hour bins.
2. Users add or remove stations, draw lines, change frequency, or add events.
3. The system shows demand changes as deltas from a stable baseline.
4. The frontend can toggle scenarios on/off without rerunning the full data pipeline.

## Core Design Shift

The current pipeline is script-oriented:

```text
build features -> train model -> score candidates -> write CSV
```

For an interactive timelapse, the system should become:

```text
precomputed baseline + scenario overlay engine -> frontend timelapse
```

The key difference is that baseline data is built offline, while scenarios are applied as lightweight deltas. The frontend does not need to know how those deltas were calculated. It should receive complete composed frames that are ready for MapLibre to render.

The current frontend already matches this direction: `src/heatmap/stream.ts` opens an SSE connection to `/api/heatmap/stream`, converts each streamed frame into GeoJSON points, and feeds that GeoJSON to a MapLibre heatmap layer. The long-term architecture should preserve that contract and make the backend stream smarter.

```text
baseline components + active scenario state + playback state + live overlays
  -> frame composer
  -> SSE frame
  -> MapLibre GeoJSON source
  -> MapLibre heatmap layer
```

## Runtime Playback Contract

The runtime server owns simulation time. The frontend sends playback commands and renders the `sim_time` attached to each streamed frame.

Current playback defaults:

- `frame_interval_seconds = 1.0`
- `sim_step_seconds = 1800`
- one streamed frame advances one 30-minute model bin while playback is running

Playback API:

- `GET /api/playback`: returns `is_playing`, `sim_step_seconds`, `sim_minutes_per_second`, and current `sim_time`.
- `POST /api/playback`: updates `is_playing` and optionally `sim_minutes_per_second`.
- `POST /api/playback/seek`: jumps to `minute_of_week`, or to `day_of_week` plus `time_bin`.

When paused, the SSE connection stays open and the backend keeps composing the current frame without advancing `sim_time`. This keeps scenario and overlay state live while preventing frontend/backend clock drift.

Live events and people drops are runtime overlays, not full model reruns. They are placed into the current simulation time, decay over a configurable duration, and are composed on top of:

```text
display frame = baseline frame + precomputed scenario deltas + live overlay deltas
```

This gives immediate feedback for crowd drops and event scenarios while preserving the option to run an async high-fidelity scenario job later.

## Offline Baseline Artifacts

These artifacts should be precomputed and cached before the frontend session starts.

### 1. Grid Metadata

Purpose: stable spatial layer used by every timelapse frame.

Suggested file:

```text
baseline_grid.geojson
```

Important fields:

- `cell_id`: stable grid cell identifier.
- `row`, `col`: grid index.
- `center_lat`, `center_lon`: cell center.
- `min_lat`, `min_lon`, `max_lat`, `max_lon`: cell bounds.

### 2. Baseline Components

Purpose: frame-level baseline demand and all reusable component scores.

Suggested file:

```text
baseline_components.csv
```

Required keys:

- `cell_id`
- `day_of_week`
- `time_bin`
- `hour`
- `minute`

Recommended component columns:

- `model_demand_score`
- `access_demand_score`
- `access_service_demand_score`
- `density_activity_demand_score`
- `land_use_time_demand_score`
- `connectivity_demand_score`
- `service_demand_score`
- `line_demand_score`
- `relative_demand_pressure_raw`
- `demand_score`

Recommended raw feature columns for fast scenarios. Direct station-proximity fields should be kept out of demand scoring so adding stations does not create demand solely because a cell is near a station:

- `distance_weighted_connectivity`
- `distance_weighted_residential_density`
- `distance_weighted_transfer_score`
- `distance_weighted_office_jobs`
- `office_jobs_nearby`
- `scheduled_trains`
- `daily_scheduled_trains`
- `commute_demand_score`

### 3. Station And Network Metadata

Purpose: support add/remove station and proposed-line scenarios.

Suggested files:

```text
stations.csv
network_metadata.csv
```

Important station fields:

- `station_id`
- `station_name`
- `lat`
- `lon`
- `activity_score`
- `connectivity`
- `connectivity_score`
- `is_transfer_proxy`
- `residential_density_ratio`
- `daily_scheduled_trains`

## Scenario Overlay Model

Scenarios should produce compact delta outputs rather than replacing the full baseline.

### Scenario Input

Use JSON for frontend-driven scenarios.

Example:

```json
{
  "scenario_id": "new_link_extension",
  "type": "line",
  "stations": [
    {"sequence": 1, "lat": 47.60, "lon": -122.33, "station_name": "A"},
    {"sequence": 2, "lat": 47.62, "lon": -122.31, "station_name": "B"}
  ],
  "frequency": [
    {"time_bin": 480, "scheduled_trains": 6},
    {"time_bin": 510, "scheduled_trains": 6}
  ]
}
```

### Scenario Output

Suggested file:

```text
scenario_delta.csv
```

Required fields:

- `scenario_id`
- `cell_id`
- `day_of_week`
- `time_bin`
- `baseline_demand_score`
- `scenario_demand_score`
- `demand_delta`
- `percent_change`

Optional diagnostic fields:

- `event_surplus_flow`
- `line_network_value`
- `line_service_weight`
- `changed_access_score`
- `changed_service_score`

### Scenario State

The scenario engine should treat every user edit as a transition from one immutable state to another.

```text
state_v1 = baseline only
state_v2 = baseline + added station
state_v3 = baseline + added station + event
state_v4 = baseline + added station + event + frequency change
```

Each action should record:

- `scenario_id`: stable ID for the user action.
- `scenario_type`: `station`, `line`, `frequency`, `event`, or another supported scenario.
- `created_at_real_time`: wall-clock time when the user made the edit.
- `created_at_sim_time`: current simulated time when the edit was made.
- `effective_from_sim_time`: simulated time when the edit starts affecting frames.
- `state_before`: immutable state version before the edit.
- `state_after`: immutable state version after the edit.
- `scenario_payload`: original user-provided scenario definition.

The important distinction is that `created_at_sim_time` is observational, while `effective_from_sim_time` controls behavior. If a user adds a line while the simulation is at Monday 08:15, the new line should affect Monday 08:15 onward unless the UI explicitly offers an "apply to whole loop" option.

### Delta Semantics

Scenario deltas should be computed from the altered state, not always from the original baseline.

For the first scenario:

```text
delta_v2 = score(state_v2) - score(state_v1)
```

For later scenarios:

```text
delta_v3 = score(state_v3) - score(state_v2)
delta_v4 = score(state_v4) - score(state_v3)
```

This matters because scenarios can interact. If the user adds a station and then draws a new line through that station, the line's value should be calculated against the world where the station already exists. Otherwise, the system can undercount or double count access, transfer, service, and line-network effects.

## Frontend Playback Model

The frontend should loop through the simulation timeline:

```text
7 days * 48 half-hour bins = 336 frames
```

These 336 model bins are the demand keyframes. The UI can still tick more frequently, such as every 30 seconds of simulated time. At each displayed tick, the frame composer maps the simulated time to the relevant model bin and active scenario state.

For each emitted frame:

```text
display_score(cell, t, state_version) =
  baseline_score(cell, t)
  + active_scenario_effect(cell, t, state_version)
```

The current MapLibre renderer should receive the result as a complete frame:

```json
{
  "timestamp": 1714070400.0,
  "state_version": "state_v4",
  "sim_time": {
    "day_of_week": 0,
    "time_bin": 510,
    "minute_of_week": 510
  },
  "cells": [[12, 34, 0.82], [13, 34, 0.65]]
}
```

The frontend can ignore `state_version` and `sim_time` until controls need them. The required rendering field remains `cells`.

The user should be able to:

- pause/play the loop,
- scrub through `day_of_week` and `time_bin`,
- toggle scenarios on/off,
- compare baseline vs scenario,
- inspect component contributions for a selected cell.

## Frame Composer

The frame composer is the runtime layer between the model artifacts and the frontend stream.

Inputs:

- `baseline_components.csv`
- active `scenario_state`
- cached `scenario_delta` tables
- current simulation clock time
- current `state_version`

Output:

- one complete SSE `frame` snapshot for the current display tick.

The frontend should not receive raw deltas as the primary heatmap stream. It should receive composed frame snapshots, because the current `src/heatmap/grid.ts` conversion treats every frame as the full current set of active cells. Internally, the backend can still store deltas compactly.

```text
composed_density(cell, t) =
  normalize_for_display(
    baseline_raw_pressure(cell, t)
    + state_delta_raw_pressure(cell, t)
  )
```

For performance, the composer should only emit nonzero or visually meaningful cells:

```text
cells = [
  [row, col, composed_density]
  for each cell
  if composed_density > display_threshold
]
```

This preserves the existing sparse SSE payload while keeping each payload semantically complete.

## Delta Modes

There are two useful ways to handle scenario deltas.

### Fast Layered Deltas

Fast mode adds independent scenario effects together:

```text
display = baseline + delta_station + delta_event + delta_frequency
```

Use this for quick previews and weakly interacting scenarios, especially:

- events,
- simple frequency changes,
- one-off station changes.

The risk is that it misses interactions. For example, an event near a newly added station should become more accessible, and a new line's value may change if another scenario already created a transfer point.

### Rebased State Scoring

Correct mode scores the full current state, or computes each new delta relative to the immediately previous state:

```text
new_delta = score(state_after) - score(state_before)
```

Use this for scenarios that change network structure:

- added lines,
- multiple station changes,
- transfer or junction effects,
- scenarios where line network value depends on previous edits.

The implementation should favor rebased state scoring as the authoritative result. Fast layered deltas can still be used as temporary previews while the backend computes the rebased state.

## Scenario Types

### Add Station

Changes:

- nearest station distance,
- stations within 500m/1000m,
- distance-weighted activity,
- connectivity exposure,
- service exposure if frequency is provided.

Expected effect:

- nearby cells gain access and possibly service demand,
- cells with latent residential/office demand should respond more strongly.

### Remove Station

Changes:

- subtract access/service contribution from affected cells,
- recompute station counts and proximity exposure.

Expected effect:

- demand pressure decreases near removed station,
- nearby stations may absorb some demand if still accessible.

### Add Line

Changes:

- adds proposed stations,
- adds corridor proximity,
- adds connected-demand value,
- adds junction/transfer potential,
- optionally adds frequency/service.

Expected effect:

- line matters most if it connects multiple demand nodes,
- line matters more near useful junctions,
- line to low-demand areas should have limited effect.

### Change Frequency

Changes:

- scheduled trains for selected stations and time bins,
- service score,
- access-service score,
- scenario demand pressure.

Expected effect:

- strongest where access and latent demand already exist,
- time-localized to affected bins unless frequency is repeated.

### Event

Changes:

- adds temporary destination demand,
- spreads surplus across event window and optional tail bins,
- allocates across cells using the same shared dispersion weight as other scenarios.

Expected effect:

- strongest near event location,
- stronger where station access and service are good,
- can be amplified by new nearby stations or lines.

## Shared Dispersion Formula

All scenario types should use one shared allocation principle:

```text
dispersion_weight =
  learned_demand_potential
  + station_access
  + scheduled_service
  + network_value
  + local_land_use_demand
```

In current code this corresponds to:

```text
dispersion_weight_raw =
  0.20
  + access_raw
  + 0.50 * service_raw
  + 0.50 * line_network_value
  + 0.50 * relative_demand_pressure
  + 0.20 * commute_demand_score
```

This keeps events, commute demand, and line scenarios consistent.

## Offline vs Online Responsibilities

### Offline / Slow

Run before the frontend session:

- raw data download,
- station vector construction,
- census joins,
- GTFS parsing,
- optional LEHD/LODES processing,
- Delhi demand model training,
- baseline grid scoring,
- baseline artifact export.

### Online / Fast

Run during user interaction:

- parse scenario JSON,
- update only affected cells/time bins where possible,
- compute scenario deltas,
- rebase deltas from `state_before` to `state_after`,
- compose complete SSE frames for the frontend.

## Recommended Script Structure

Current scripts can be refactored toward:

```text
scripts/build_baseline_features.py
scripts/train_demand.py
scripts/score_baseline.py
scripts/run_scenario.py
scripts/export_frontend_artifacts.py
```

### `build_baseline_features.py`

Builds:

- station vectors,
- grid candidates,
- census exposure,
- GTFS frequency exposure,
- optional jobs exposure.

### `train_demand.py`

Trains:

- Delhi weak-supervised demand potential model.

Writes:

- model metrics,
- optional serialized model if needed later.

### `score_baseline.py`

Scores:

- all baseline grid/time rows.

Writes:

- `baseline_components.csv`,
- `baseline_grid.geojson`.

### `run_scenario.py`

Inputs:

- `baseline_components.csv`,
- `stations.csv`,
- `scenario_config.json`.

Writes:

- `scenario_delta.csv`,
- optional scenario diagnostics.

### `export_frontend_artifacts.py`

Converts:

- CSV baseline and scenario deltas into backend/frontend-friendly artifacts.
- The backend frame composer still owns final heatmap composition for the current stream.

Possible future formats:

- compressed CSV,
- Parquet,
- Arrow,
- PMTiles,
- vector tiles.

## Recommended Runtime Artifacts

Start simple:

```text
baseline_components.csv
scenario_state.json
scenario_delta.csv
baseline_grid.geojson
```

If performance becomes an issue:

```text
baseline_components.parquet
scenario_state.json
scenario_delta.parquet
grid.pmtiles
```

The frontend should not depend on raw data, Delhi training data, or GTFS internals. For the current MapLibre implementation, the most important frontend-facing artifact is still the SSE `frame` payload, not the raw CSV files.

## Main Simplifications From Current Pipeline

1. Keep one primary demand pipeline.
2. Move Seattle bike/Fremont heatmap into a legacy/demo path.
3. Deprecate the older passenger-flow timelapse model unless calibrated ridership labels become available.
4. Precompute learned demand potential once instead of retraining inside `build_line_weights.py`.
5. Use scenario deltas instead of rewriting full predictions for every interaction.
6. Keep GeoJSON as a geometry/summary artifact, not the full timelapse data source.

## Implementation Milestones

### Milestone 1: Baseline Cleanup

- Rename current `city_heatmap_candidate_features.csv` to `baseline_candidate_features.csv`.
- Add `baseline_components.csv` with component scores and raw pressure.
- Update docs to point frontend to CSV, not GeoJSON, for timelapse.

### Milestone 2: Scenario Delta Runner

- Create `scripts/run_scenario.py`.
- Support event, station, line, and frequency scenario JSON.
- Output `scenario_delta.csv`.

### Milestone 3: Fast Line Scenarios

- Precompute learned demand potential in baseline components.
- Update line scenarios to read precomputed potential instead of retraining.
- Recompute only affected line catchment cells.

### Milestone 4: Frontend Export

- Add compact frontend export.
- Include frame index:

```text
frame_index = day_of_week * 48 + time_bin / 30
```

- Support quick lookup by `frame_index` and `cell_id`.

### Milestone 5: Calibration Upgrade

If better labels become available, calibrate against:

- station boardings/alightings,
- APC counts,
- fare-card taps,
- PSRC travel survey,
- LODES home-work OD flows,
- event attendance plus station entries.

## Summary

For a looping interactive timelapse, the system should not rerun the whole data pipeline whenever a user adds a scenario. It should precompute baseline demand components once, then generate compact scenario deltas using the same shared dispersion logic. This keeps the frontend fast, makes scenario comparison easier, and preserves a clean separation between slow data engineering and interactive simulation.
