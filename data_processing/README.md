# Mobility and heatmap datasets

This repo contains three related paths:

1. **Seattle grid heatmap**: grid cells, observed bike counts, GTFS supply, optional LEHD.
2. **Cross-city station vectors**: Delhi trip features with train/test splits, and Delhi/Seattle station-level vectors with comparable density/connectivity activity proxies.
3. **City-generic demand heatmap**: train a Delhi-weak-supervised relative demand model, score a 48-bin x 7-day city grid from census/station vectors and GTFS supply, then run route/station/event scenarios.

Source code lives under `src/`:

- `src/common/`: shared I/O, station, and geospatial utilities.
- `src/pipelines/delhi/`: Delhi density, trip, and train/test feature builders.
- `src/pipelines/seattle/`: Seattle station-vector and heatmap feature builders.
- `src/models/`: model training entry points.

Run commands from the `data_processing/` directory with `python -m ...`. The modules read from `curr_data/raw` and write to `curr_data/processed` by default.

See `ARCHITECTURE.md` for the current pipeline architecture and the future Delhi-trained dispersion model plan.

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

---

## 0. Cache raw data

Keep public raw downloads in one place before running feature builders:

```bash
.venv/bin/python scripts/download_raw_data.py
```

By default this populates `curr_data/raw/`, downloads/extracts Puget Sound GTFS into `gtfs/`, and reuses any non-empty cached files. The Delhi trip CSV is downloaded from the Kaggle dataset `nikhilkumar766/delhi-metro-dataset`, extracted, renamed to `curr_data/raw/delhi_metro_updated.csv`, and validated for the columns required by `src.pipelines.delhi.transform_metro`.

If Kaggle requires authentication in the container, provide credentials through environment variables:

```bash
export KAGGLE_USERNAME=...
export KAGGLE_KEY=...
.venv/bin/python scripts/download_raw_data.py
```

Useful options:

```bash
.venv/bin/python scripts/download_raw_data.py --raw-dir curr_data/raw --gtfs-dir gtfs
.venv/bin/python scripts/download_raw_data.py --kaggle-delhi-dataset nikhilkumar766/delhi-metro-dataset
.venv/bin/python scripts/download_raw_data.py --delhi-trips-url https://example.com/delhi_metro_updated.csv
.venv/bin/python scripts/download_raw_data.py --delhi-gtfs-url https://example.com/delhi_gtfs.zip
.venv/bin/python scripts/download_raw_data.py --kaggle-delhi-gtfs-dataset owner/dataset-slug
.venv/bin/python scripts/download_raw_data.py --include-lehd
.venv/bin/python scripts/download_raw_data.py --skip-gtfs
```

Delhi GTFS is optional because there is no stable public DMRC GTFS URL baked into the repo. If you provide a GTFS zip, it is extracted to `gtfs_delhi/`; otherwise the Delhi frequency step emits zero-frequency fallback features and the model still trains on passenger-per-train labels.

The Puget Sound GTFS feed includes buses and ferries. Create a cleaned station-only GTFS subset before Seattle station or heatmap work:

```bash
.venv/bin/python scripts/filter_gtfs_station_data.py \
  --input-gtfs-dir gtfs \
  --output-gtfs-dir gtfs_stations \
  --route-types 0,1,2 \
  --agency-ids 40
```

`route_type` values `0,1,2` keep rail-style service, and `agency_id` `40` keeps Sound Transit. Together this keeps Link/Sounder station service while excluding bus (`3`), ferry (`4`), Seattle Streetcar (`23`), Seattle Center Monorail (`96`), and Amtrak (`51`). The raw `gtfs/` directory is preserved as the source cache; downstream commands should use `gtfs_stations/` by default.

`scripts/download_raw_data.sh` is a thin compatibility wrapper around the Python script.

---

## One-command pipeline scripts

Use these wrappers when you want to rebuild or validate the full station-only workflow without rerunning each module manually:

```bash
# Build raw-derived features and cleaned station-only Seattle artifacts.
.venv/bin/python scripts/build_features.py

# Train the Seattle baseline and city-generic demand model.
.venv/bin/python scripts/train_models.py

# Smoke-test event, station add/remove, and frequency-delta scenarios.
.venv/bin/python scripts/test_scenarios.py
```

Common build options:

```bash
.venv/bin/python scripts/build_features.py --skip-download
.venv/bin/python scripts/build_features.py --agency-ids 40 --route-types 0,1,2
.venv/bin/python scripts/build_features.py --include-lehd
.venv/bin/python scripts/build_features.py --skip-seattle-heatmap
.venv/bin/python scripts/train_models.py --include-timelapse
```

By default, the wrappers keep processed artifacts in stage folders:

- `curr_data/processed/features/`: station vectors, training features, candidate features, and feature grids.
- `curr_data/processed/model_outputs/`: model metrics, full prediction CSVs, and display GeoJSON.
- `curr_data/processed/scenarios/`: proposed-line weights and scenario overlay files.

---

## 1. Seattle grid heatmap dataset

### Outputs

- `curr_data/processed/features/seattle_heatmap_features.csv`: rows by grid cell, hour, and day of week.
- `curr_data/processed/features/seattle_heatmap_grid.geojson`: grid polygons with `congestion_score` for MapLibre or `react-map-gl`.
- `curr_data/processed/model_outputs/seattle_heatmap_model_metrics.json`: written by `src.models.train_heatmap_model`.
- `curr_data/processed/model_outputs/seattle_heatmap_predictions.csv`: optional model predictions.

### Build

```bash
.venv/bin/python -m src.pipelines.seattle.build_heatmap_dataset \
  --gtfs-dir gtfs_stations \
  --raw-dir curr_data/raw \
  --out-dir curr_data/processed/features \
  --fremont-limit 5000
```

Uses cleaned station-only GTFS in `gtfs_stations/`, Seattle Open Data Fremont Bridge counts, and Seattle transit accessibility data. Optional LEHD (larger download):

```bash
.venv/bin/python -m src.pipelines.seattle.build_heatmap_dataset \
  --gtfs-dir gtfs_stations \
  --raw-dir curr_data/raw \
  --out-dir curr_data/processed/features \
  --include-lehd \
  --lehd-year 2022
```

### Train (separate step)

```bash
.venv/bin/python -m src.models.train_heatmap_model \
  --features-csv curr_data/processed/features/seattle_heatmap_features.csv \
  --out-dir curr_data/processed/model_outputs
```

### Optional manual counts

CSV with `lat`, `lon`, `datetime`, `count`:

```bash
.venv/bin/python -m src.pipelines.seattle.build_heatmap_dataset \
  --gtfs-dir gtfs_stations \
  --raw-dir curr_data/raw \
  --out-dir curr_data/processed/features \
  --optional-counts-csv path/to/counts.csv
```

---

## 2. City-generic demand heatmap

This path produces a **relative transit demand-pressure heatmap**, not calibrated ridership counts. Delhi Metro `Passengers` is used as weak supervision for load-per-train behavior, then the model blends learned demand with census density, office/jobs density when available, station access, connectivity, service frequency, proposed-line weights, and event surplus. The default prediction output is 48 half-hour bins x 7 days per grid cell.

### Commands

```bash
# Optional if gtfs_delhi/ exists; writes a zero-frequency fallback if it does not.
.venv/bin/python -m src.pipelines.delhi.build_gtfs_frequency \
  --gtfs-dir gtfs_delhi \
  --station-vectors curr_data/processed/features/delhi_station_vectors.csv \
  --out-dir curr_data/processed/features

# Build Delhi training rows from trip labels, station vectors, census density, and frequency.
.venv/bin/python -m src.pipelines.delhi.build_heatmap_training_dataset \
  --trip-features curr_data/processed/features/delhi_trip_features.csv \
  --station-vectors curr_data/processed/features/delhi_station_vectors.csv \
  --frequency-csv curr_data/processed/features/delhi_station_gtfs_frequency.csv \
  --out-dir curr_data/processed/features

# Build a Seattle 48-bin x 7 candidate grid using station proximity and GTFS frequency exposure.
.venv/bin/python -m src.pipelines.common.build_heatmap_candidates \
  --station-vectors curr_data/processed/features/seattle_station_vectors.csv \
  --gtfs-dir gtfs_stations \
  --out-dir curr_data/processed/features

# Train on Delhi and score the candidate grid as relative demand pressure.
.venv/bin/python -m src.models.train_demand_heatmap_model \
  --training-features curr_data/processed/features/delhi_heatmap_training_features.csv \
  --candidate-features curr_data/processed/features/city_heatmap_candidate_features.csv \
  --out-dir curr_data/processed/model_outputs
```

### Scenario scoring

Route/station scenarios are handled by rebuilding candidate features with optional overlays:

```bash
.venv/bin/python -m src.pipelines.common.build_heatmap_candidates \
  --station-vectors curr_data/processed/features/seattle_station_vectors.csv \
  --gtfs-dir gtfs_stations \
  --added-stations-csv path/to/added_stations.csv \
  --removed-stations-csv path/to/removed_stations.csv \
  --frequency-delta-csv path/to/frequency_delta.csv \
  --output-name city_heatmap_scenario_features.csv
```

Event surplus users are allocated across the 30-minute `time_bin` rows and, by default, dissipate into four trailing half-hour bins with `--event-tail-decay 0.5`. The total `surplus_users` is conserved across the active event window plus the tail. Within each bin, surplus is distributed by proximity to the event, existing station access, scheduled service, proposed-line network value, and baseline learned demand pressure. The included `examples/scenarios/seattle_event_scenario.csv` still works with `start_hour`/`end_hour`; use `start_minute`/`end_minute` for exact half-hour windows.

```bash
.venv/bin/python -m src.models.train_demand_heatmap_model \
  --event-scenarios-csv examples/scenarios/seattle_event_scenario.csv
```

For proposed new lines, start from an ordered station-coordinate CSV. The included `examples/scenarios/proposed_line_stations.csv` shows the minimum shape:

```bash
.venv/bin/python scripts/build_line_weights.py \
  --line-stations-csv examples/scenarios/proposed_line_stations.csv \
  --candidate-features curr_data/processed/features/city_heatmap_candidate_features.csv \
  --line-id proposed_line_demo
```

This writes:

- `curr_data/processed/scenarios/proposed_line_weights.csv`: one row per grid cell with `nearest_line_distance_m`, `line_distance_weight`, `line_station_weight`, `line_combined_weight`, and `line_service_weight`.
- `curr_data/processed/scenarios/proposed_line_candidate_features.csv`: rebuilt scenario candidate rows joined to those line-weight columns.
- `curr_data/processed/scenarios/proposed_line_added_stations.csv`: generated station overlay compatible with `--added-stations-csv`.
- `curr_data/processed/scenarios/proposed_line_frequency_delta.csv`: generated frequency overlay compatible with `--frequency-delta-csv` when the input includes `hour` and `scheduled_trains`. With half-hour bins, an hourly value is expanded to both half-hour bins unless `minute` or `time_bin` is provided.

By default, `build_line_weights.py` also rebuilds the existing station/frequency exposure fields using the generated added-station and frequency-delta overlays. That means old station-distance, station-count, activity/connectivity exposure, and scheduled-train weights are recomputed around the proposed line instead of simply appending new line columns to stale baseline rows.

The line utility now estimates whether a line actually connects demand. By default it trains the Delhi weak-supervised demand model on `curr_data/processed/features/delhi_heatmap_training_features.csv`, scores candidate grid cells as learned demand potential, and uses that learned potential around each proposed station. Proposed stations also consider nearby residential density, office/jobs density, station activity, connectivity, existing service context, and junction potential. The line then exposes `line_connected_demand`, `line_junction_weight`, and `line_network_value`; `line_service_weight` is scaled by that network value. A line through low-demand places or with only one meaningful demand node will therefore have weaker scenario impact than a line that connects multiple demand nodes or useful transfer/junction areas.

Caveat: Delhi training rows do not yet contain observed proposed-line examples, so these line fields are zero-filled during training. The current scenario effect is still a transparent demand-pressure simulation layered on top of weakly supervised demand, not a learned causal estimate of new-line ridership.

Outputs:

- `curr_data/processed/features/delhi_station_gtfs_frequency.csv`
- `curr_data/processed/features/delhi_heatmap_training_features.csv`
- `curr_data/processed/features/city_heatmap_candidate_features.csv`
- `curr_data/processed/model_outputs/demand_heatmap_predictions.csv`
- `curr_data/processed/model_outputs/demand_heatmap_scenario_predictions.csv`
- `curr_data/processed/model_outputs/demand_heatmap_model_metrics.json`
- `curr_data/processed/model_outputs/demand_heatmap_grid.geojson`

For frontend timelapse playback, the CSV is the canonical final output because it preserves every `cell_id`, `day_of_week`, and `time_bin`. The GeoJSON is a display convenience: it aggregates prediction rows to one feature per grid cell, so it is useful for a static summary layer but not sufficient for the full timelapse by itself.

The model reports component scores such as `model_demand_score`, `access_demand_score`, `access_service_demand_score`, `density_activity_demand_score`, `land_use_time_demand_score`, `connectivity_demand_score`, `service_demand_score`, `line_demand_score`, and the final `demand_score`. Scenario outputs add `baseline_demand_score`, `scenario_demand_score`, `event_surplus_flow`, `demand_delta`, and `percent_change`.

Because the Delhi label is not time-binned, the training builder expands each labeled trip into representative half-hour context bins from the trip remarks. Candidate scoring adds residential/office temporal features: residential demand is emphasized on weekend and commute periods, while office demand is emphasized on weekday workday and commute periods. Those land-use features, event allocation, and proposed-line catchments all use the same shared dispersion weight: station access, scheduled service, proposed-line network value, learned baseline demand pressure, and commute demand. Office signal comes from `employment_jobs`, so run the Seattle heatmap build with `--include-lehd` or provide an office feature CSV to get non-zero office demand. Treat the output as relative demand pressure until calibrated with local city ridership, boardings, counters, or event data.

Better calibration data, if available, would be agency station boardings/alightings by time of day, APC or fare-card tap counts, LODES home-work flows, pedestrian counters near stations, and event attendance with nearby station entries. Without those labels, event and proposed-line effects remain access/service/network-weighted heuristics rather than learned causal effects.

---

## 3. Cross-city density dataset (Delhi + Seattle)

Goal: comparable **station vectors** with `residential_density_ratio` (people per km² around a station buffer, divided by that city’s average population density) and `activity_score` derived from reproducible density/connectivity inputs instead of city-specific station IDs.

### Order of operations

Run in this order so trip features pick up density columns.

| Step | Module | Purpose |
|------|--------|---------|
| 0 | `scripts/download_raw_data.py` | Cache public raw inputs under `curr_data/raw/` and validate the Delhi trip file |
| 1 | `src.pipelines.delhi.build_population_vectors` | Delhi station coordinates + ward population + ward polygons → per-station density |
| 2 | `src.pipelines.delhi.transform_metro` | Trip CSV → `delhi_station_vectors.csv` + `delhi_trip_features.csv` (merges density from step 1 and keeps passengers as the target) |
| 3 | `src.pipelines.delhi.prepare_train_test` | Split `delhi_trip_features.csv` into train/test |
| 4 | `src.pipelines.seattle.build_station_vectors` | Seattle GTFS stops in bbox + ACS + TIGER tracts/place → `seattle_station_vectors.csv` |

### Commands

```bash
# Delhi: density only (uses the cached raw files)
.venv/bin/python -m src.pipelines.delhi.build_population_vectors \
  --raw-dir curr_data/raw \
  --out-dir curr_data/processed/features \
  --radius-m 1000

# Delhi: full trip features (expects curr_data/processed/features/delhi_station_density.csv from step 1)
.venv/bin/python -m src.pipelines.delhi.transform_metro \
  --input curr_data/raw/delhi_metro_updated.csv \
  --out-dir curr_data/processed/features \
  --density-vectors curr_data/processed/features/delhi_station_density.csv

# Delhi: train / test split
.venv/bin/python -m src.pipelines.delhi.prepare_train_test \
  --features-csv curr_data/processed/features/delhi_trip_features.csv \
  --out-dir curr_data/processed/features

# Seattle: station vectors with density
.venv/bin/python -m src.pipelines.seattle.build_station_vectors \
  --gtfs-dir gtfs_stations \
  --raw-dir curr_data/raw \
  --out-dir curr_data/processed/features \
  --radius-m 1000
```

### Cross-city outputs

| File | Description |
|------|-------------|
| `curr_data/processed/features/delhi_station_density.csv` | All coordinate-matched Delhi stations with population/density fields |
| `curr_data/processed/features/delhi_station_vectors.csv` | Stations appearing in trip data: proxy activity + connectivity + density where matched |
| `curr_data/processed/features/delhi_trip_features.csv` | One row per trip with origin/destination vector columns and `target_passengers` |
| `curr_data/processed/features/delhi_train_features.csv` / `delhi_test_features.csv` | Stratified split (rows with non-null targets) |
| `curr_data/processed/features/seattle_station_vectors.csv` | Seattle-area rail/station GTFS stops in the Seattle bbox with density and proxy `activity_score` |

Summary JSON files: `delhi_population_vector_summary.json`, `seattle_station_vector_summary.json` (where generated).

### Data sources (cross-city)

- **Raw cache script**: `scripts/download_raw_data.py`
- **Delhi trips**: Kaggle dataset `nikhilkumar766/delhi-metro-dataset`, cached as `curr_data/raw/delhi_metro_updated.csv`
- **Delhi station lat/lon**: public coordinate CSV (default URL in `src.pipelines.delhi.build_population_vectors`)
- **Delhi population**: OpenCity ward population CSV; **geometry**: DataMeet `Delhi_Wards.geojson` (ward numbers joined to population rows)
- **Seattle**: local raw `gtfs/`, cleaned station-only `gtfs_stations/`; **ACS** `B01003_001E` (tract + Seattle place); **Census TIGER** cartographic boundary tract and place shapefiles (cached in `curr_data/raw/`)

`activity_score` is computed from the same proxy recipe in both station-vector builders: 70% `residential_density_ratio` plus 30% within-city connectivity percentile, then min-max normalized. Delhi connectivity currently comes from distinct origin/destination links in the trip file; Seattle connectivity comes from GTFS departures plus stop count. Delhi `Passengers` remains only as `target_passengers` in the trip feature table.

If a Delhi trip station name has no match in the coordinate file, `lat`/`lon` and density stay empty for that station.

---

## Data layout

- `curr_data/raw/`: cached downloads (GTFS zips, Census shapefiles, ward CSV, etc.)
- `curr_data/processed/`: generated staged CSV/GeoJSON/JSON artifacts for the current workspace
- `gtfs/`: extracted Puget Sound GTFS text files, including bus and ferry source rows
- `gtfs_stations/`: cleaned Puget Sound GTFS subset for Sound Transit station work (`route_type` 0, 1, 2 and `agency_id` 40)
- `gtfs_delhi/`: optional extracted Delhi GTFS text files
