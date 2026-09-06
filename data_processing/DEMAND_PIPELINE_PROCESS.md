# Demand Heatmap Pipeline Process

This document explains the current end-to-end demand heatmap process: what data enters each stage, what artifacts are generated, and what the important columns mean.

The main output is a **relative demand-pressure timelapse**, not calibrated ridership. Delhi data weakly supervises demand potential; Seattle inputs provide the target city's station, census, GTFS, and optional jobs context.

## Current Data Sources

### Delhi

- `curr_data/raw/delhi_metro_updated.csv`
  - Source: existing Kaggle Delhi Metro trip dataset.
  - Purpose: weak supervision for demand potential.
  - Why not official DMRC static as default: the official Delhi OTD/DMRC static feed appears to be form-gated and does not currently expose a stable direct download URL. Keep it as an optional/manual GTFS input through `--delhi-gtfs-url` or `gtfs_delhi/`.

- `curr_data/raw/delhi_metro_station_coordinates.csv`
  - Purpose: Delhi station coordinates.

- `curr_data/raw/delhi_wards.geojson`
  - Purpose: Delhi ward geometry for population assignment.

- `curr_data/raw/delhi_ward_population.csv`
  - Purpose: residential population around Delhi stations.

- `gtfs_delhi/`
  - Purpose: optional Delhi GTFS frequency.
  - If unavailable, the pipeline writes zero-frequency fallback rows.

### Seattle / Target City

- `gtfs/`
  - Raw Puget Sound GTFS.

- `gtfs_stations/`
  - Filtered station-only GTFS generated from `gtfs/`.
  - Default filter keeps Sound Transit rail/station-like modes: `route_type` 0, 1, 2 and `agency_id` 40.

- `curr_data/raw/king_county_acs_tract_population.csv`
  - Purpose: tract population for station catchments.

- `curr_data/raw/seattle_acs_place_population.csv`
  - Purpose: citywide population baseline.

- `curr_data/raw/cb_2022_53_tract_500k.zip`, `curr_data/raw/cb_2022_53_place_500k.zip`
  - Purpose: Census TIGER geometry.

- Optional LEHD/LODES workplace files
  - Purpose: office/jobs signal for commute demand.
  - Enabled with `scripts/build_features.py --include-lehd`.

## Stage 1: Raw Cache

Run:

```bash
.venv/bin/python scripts/download_raw_data.py
```

This caches raw files under `curr_data/raw/`, extracts Seattle GTFS into `gtfs/`, and optionally extracts Delhi GTFS into `gtfs_delhi/` if a stable source is provided.

Important raw Delhi trip columns:

- `TripID`: unique trip record identifier.
- `Date`: trip date.
- `From_Station`: origin station name.
- `To_Station`: destination station name.
- `Distance_km`: trip distance.
- `Fare`: fare paid.
- `Cost_per_passenger`: passenger cost field from the source dataset.
- `Passengers`: passengers per train/trip label used as weak supervision.
- `Ticket_Type`: categorical ticket type.
- `Remarks`: source trip context such as peak, off-peak, weekend, festival, or maintenance.

## Stage 2: Station Vectors

Run through the wrapper:

```bash
.venv/bin/python scripts/build_features.py --skip-download
```

Main station-vector outputs:

- `curr_data/processed/features/delhi_station_density.csv`
- `curr_data/processed/features/delhi_station_vectors.csv`
- `curr_data/processed/features/seattle_station_vectors.csv`

Important station-vector columns:

- `station_id`: stable slug used to join stations across artifacts.
- `station_name`: human-readable station name.
- `lat`, `lon`: station coordinates.
- `population_within_radius`: area-weighted population inside the station buffer.
- `population_density_within_radius`: station-buffer population density.
- `city_average_population_density`: city baseline density.
- `residential_density_ratio`: station density divided by city average density.
- `connectivity`: raw station connectivity. For Delhi this comes from OD links; for Seattle this comes from GTFS station departures and stop aggregation.
- `connectivity_score`: normalized connectivity score.
- `activity_score`: comparable proxy score from residential density and connectivity rank.
- `activity_rank_pct`: station activity percentile within the city.
- `is_transfer_proxy`: proxy flag for likely transfer/junction importance.

## Stage 3: Delhi Trip Features

Output:

- `curr_data/processed/features/delhi_trip_features.csv`

This joins Delhi trip records to station vectors and converts source categories into model-ready fields.

Important columns:

- `from_station_id`, `to_station_id`: origin and destination station IDs.
- `day_of_week`, `month`, `year`: calendar features.
- `is_weekend`: weekend flag.
- `is_peak`, `is_off_peak`, `is_festival`, `is_maintenance`: context flags derived from `Remarks`.
- `origin_*`: station-vector fields for the origin station.
- `destination_*`: station-vector fields for the destination station.
- `target_passengers`: the supervised target, copied from `Passengers`.
- `ticket_*`: one-hot ticket type fields.
- `remark_*`: one-hot remark/context fields.
- `has_target`: flag indicating a usable passenger target.

## Stage 4: Delhi GTFS Frequency

Output:

- `curr_data/processed/features/delhi_station_gtfs_frequency.csv`

This builds 30-minute station frequency rows from `gtfs_delhi/` when available. If no Delhi GTFS is present, it creates zero-frequency rows so downstream schemas remain stable.

Important columns:

- `station_id`: station identifier.
- `time_bin`: minute-of-day bin start. For 30-minute bins: `0`, `30`, `60`, ..., `1410`.
- `hour`: hour of day.
- `minute`: minute within hour, usually `0` or `30`.
- `scheduled_trains`: trains scheduled in that time bin.
- `daily_scheduled_trains`: total scheduled trains for that station across the day.
- `has_gtfs_frequency`: `1` if real frequency exists, else `0`.

## Stage 5: Delhi Heatmap Training Features

Output:

- `curr_data/processed/features/delhi_heatmap_training_features.csv`

This converts station/trip labels into training rows that match the city candidate grid schema. Since the Delhi label is not time-binned, records are expanded into representative half-hour context bins.

Important columns:

- `cell_id`: grid cell identifier assigned to the station.
- `center_lat`, `center_lon`: cell center.
- `station_id`: Delhi station attached to this training row.
- `flow_role`: `origin` or `destination`.
- `time_bin`, `hour`, `minute`: half-hour temporal context.
- `day_of_week`, `is_weekend`, `is_peak`, `is_off_peak`: temporal flags.
- `is_morning_commute`, `is_evening_commute`, `is_workday_midday`: derived time-of-day flags.
- `activity_score`, `connectivity_score`, `residential_density_ratio`: station demand context.
- `scheduled_trains`, `daily_scheduled_trains`, `has_gtfs_frequency`: Delhi service context, if GTFS exists.
- `load_per_train`: training target derived from Delhi `target_passengers`.
- `sample_count`: number of source records aggregated into this row.
- `nearest_station_distance_m`: `0` for station-attached Delhi training rows.
- `stations_within_500m`, `stations_within_1000m`: station-access proxy fields.
- `distance_weighted_station_activity`: candidate-compatible activity exposure.
- `distance_weighted_connectivity`: candidate-compatible connectivity exposure.
- `distance_weighted_residential_density`: candidate-compatible residential density exposure.
- `distance_weighted_transfer_score`: candidate-compatible transfer/junction exposure.
- `distance_weighted_office_jobs`: currently `0` for Delhi unless office/jobs data is added.
- `residential_temporal_demand`: residential demand adjusted by time context.
- `office_temporal_demand`: office demand adjusted by time context.
- `commute_demand_score`: combined temporal land-use demand signal.
- `target_time_bin_flow`: `load_per_train * scheduled_trains`.

## Stage 6: Target City Candidate Features

Output:

- `curr_data/processed/features/city_heatmap_candidate_features.csv`

This creates the target city grid to score. For Seattle, the grid is built from station vectors, station-only GTFS, Census density, and optional LEHD office/jobs features.

Important columns:

- `cell_id`: unique grid cell ID.
- `center_lat`, `center_lon`: grid-cell center.
- `row`, `col`: grid position.
- `min_lat`, `min_lon`, `max_lat`, `max_lon`: grid-cell bounds.
- `nearest_station_distance_m`: nearest station distance.
- `stations_within_500m`, `stations_within_1000m`: nearby station counts.
- `distance_weighted_station_activity`: nearby station activity exposure.
- `distance_weighted_connectivity`: nearby connectivity exposure.
- `distance_weighted_residential_density`: nearby residential density exposure.
- `distance_weighted_transfer_score`: nearby transfer/junction exposure.
- `distance_weighted_office_jobs`: nearby office/jobs exposure from LEHD when available.
- `office_jobs_nearby`: jobs within the local catchment.
- `time_bin`, `hour`, `minute`: half-hour timelapse fields.
- `scheduled_trains`: distance-weighted scheduled train exposure in the time bin.
- `daily_scheduled_trains`: daily service exposure.
- `has_gtfs_frequency`: whether the cell has nearby scheduled rail service.
- `day_of_week`: `0` through `6`.
- `is_weekend`, `is_peak`, `is_off_peak`: temporal flags.
- `is_morning_commute`, `is_evening_commute`, `is_workday_midday`: commute period flags.
- `residential_temporal_demand`: residential demand adjusted by weekday/weekend and time.
- `office_temporal_demand`: office demand adjusted by weekday/weekend and time.
- `commute_demand_score`: residential plus office temporal demand.

## Stage 7: Demand Model Training And Scoring

Run:

```bash
.venv/bin/python scripts/train_models.py
```

Main outputs:

- `curr_data/processed/model_outputs/demand_heatmap_predictions.csv`
- `curr_data/processed/model_outputs/demand_heatmap_model_metrics.json`
- `curr_data/processed/model_outputs/demand_heatmap_grid.geojson`

The model trains on Delhi `load_per_train`, then scores the target city candidate grid. The output is relative demand pressure, not absolute ridership.

Important prediction columns:

- `model_demand_score`: learned demand potential from the Delhi-trained model.
- `access_demand_score`: station proximity and nearby station count score.
- `access_service_demand_score`: shared access-service score used for dispersion.
- `density_activity_demand_score`: residential density and activity score.
- `land_use_time_demand_score`: residential/office temporal demand weighted by shared dispersion.
- `connectivity_demand_score`: network connectivity and transfer exposure score.
- `service_demand_score`: scheduled train and daily service score.
- `line_demand_score`: proposed-line contribution, zero for baseline rows without a scenario.
- `relative_demand_pressure_raw`: unnormalized baseline demand pressure.
- `relative_demand_pressure`: normalized baseline demand pressure.
- `event_surplus_flow`: allocated event users, usually `0` in baseline output.
- `event_demand_score`: normalized event surplus signal.
- `scenario_demand_pressure_raw`: raw demand after event/scenario changes.
- `scenario_demand_pressure`: normalized scenario pressure.
- `demand_score`: map-facing normalized demand score.
- `baseline_demand_score`: baseline normalized score used for scenario comparison.
- `baseline_demand_pressure_raw`: baseline raw pressure used for correct deltas.
- `demand_delta`: raw scenario minus raw baseline.
- `percent_change`: `demand_delta` relative to raw baseline.

## Stage 8: Scenarios

Run smoke tests:

```bash
.venv/bin/python scripts/test_scenarios.py
```

Scenario outputs are written to:

- `curr_data/processed/model_outputs/demand_heatmap_scenario_predictions.csv`
- `curr_data/processed/scenarios/` for generated proposed-line overlays when running `scripts/build_line_weights.py` directly.

### Station And Frequency Scenarios

Candidate features can be rebuilt with:

- `--added-stations-csv`
- `--removed-stations-csv`
- `--frequency-delta-csv`

Expected added-station columns:

- `station_id`: new station ID.
- `station_name`: new station name.
- `lat`, `lon`: coordinates.
- Optional station-vector fields such as `activity_score`, `connectivity`, and `residential_density_ratio`.

Expected frequency-delta columns:

- `station_id`: station to modify.
- `time_bin`: preferred half-hour bin.
- `hour`, `minute`: accepted alternative to `time_bin`.
- `scheduled_trains_delta`: service change to add or subtract.

### Proposed Line Scenarios

Run:

```bash
.venv/bin/python scripts/build_line_weights.py \
  --line-stations-csv examples/scenarios/proposed_line_stations.csv
```

Input columns:

- `sequence`: station order along the proposed line.
- `station_id`: optional station ID.
- `station_name`: optional station name.
- `lat`, `lon`: required station coordinates.
- `hour`, `minute`, `time_bin`: optional schedule context.
- `scheduled_trains`: optional planned service level.

Generated line columns:

- `nearest_line_distance_m`: distance to the proposed line geometry.
- `nearest_line_station_distance_m`: distance to the nearest proposed station.
- `line_distance_weight`: distance-decay weight from line geometry.
- `line_station_weight`: distance-decay weight from proposed stations.
- `line_combined_weight`: combined line/station proximity weight.
- `line_scheduled_trains`: planned service level.
- `line_connected_demand`: learned demand potential connected by the line.
- `line_junction_weight`: existing transfer/junction potential connected by the line.
- `line_network_value`: connected demand boosted by junction potential.
- `line_service_weight`: `line_network_value * line_scheduled_trains`.

### Event Scenarios

Event input columns:

- `event_id`: event identifier.
- `lat`, `lon`: event location.
- `day_of_week`: event day.
- `start_hour`, `end_hour`: event window.
- `start_minute`, `end_minute`: optional exact minute window.
- `surplus_users`: total users to allocate across event window plus dissipation tail.
- `radius_m`: spatial event influence radius.
- `decay_m`: spatial decay distance.

Event surplus uses the same shared dispersion logic as commute and line scenarios:

- learned baseline demand potential,
- station access,
- scheduled service,
- proposed-line network value,
- local commute demand.

## Calculation Reference

This section lists the math behind the calculated columns. Unless otherwise stated, missing numeric inputs are treated as `0`.

### Shared Helpers

Min-max normalization:

```text
min_max(x) = (x - min(x)) / (max(x) - min(x))
```

If all values are equal, `min_max(x)` returns `0`.

Distance decay:

```text
distance_weight = exp(-distance_m / decay_m)
```

Default `decay_m` is `800`.

Haversine distance:

```text
a = sin(dlat / 2)^2 + cos(lat1) * cos(lat2) * sin(dlon / 2)^2
distance_m = 2 * 6,371,000 * asin(sqrt(a))
```

### Station IDs And Station Vectors

`station_id`:

```text
station_id = lowercase(station_name)
station_id = replace non-alphanumeric runs with "_"
station_id = trim leading/trailing "_"
```

`residential_density_ratio`:

```text
residential_density_ratio =
  population_density_within_radius / city_average_population_density
```

`activity_raw` for comparable station vectors:

```text
connectivity_rank = percentile_rank(connectivity_raw)
activity_raw = 0.70 * residential_density_ratio + 0.30 * connectivity_rank
```

`activity_score`:

```text
activity_score = min_max(activity_raw)
```

`connectivity_score`:

```text
connectivity_score = min_max(connectivity_raw)
```

`activity_rank_pct`:

```text
activity_rank_pct = percentile_rank(activity_raw)
```

`is_transfer_proxy`:

```text
transfer_threshold = quantile(connectivity_raw, 0.75)
is_transfer_proxy = 1 if connectivity_raw >= transfer_threshold else 0
```

`connectivity`:

```text
connectivity = connectivity_raw
```

Seattle station connectivity uses GTFS station-mode departures plus stop aggregation. Delhi station connectivity uses observed origin/destination station links from the trip data.

### Grid Geometry

Latitude step:

```text
lat_step = cell_size_m / 111,320
```

Longitude step:

```text
lon_step = cell_size_m / (111,320 * cos(mid_latitude))
```

Grid cell ID:

```text
row = int((lat - min_lat) / lat_step)
col = int((lon - min_lon) / lon_step)
cell_id = "r{row:03d}_c{col:03d}"
```

Cell bounds and center:

```text
min_lat_cell = bbox_min_lat + row * lat_step
max_lat_cell = min(min_lat_cell + lat_step, bbox_max_lat)
min_lon_cell = bbox_min_lon + col * lon_step
max_lon_cell = min(min_lon_cell + lon_step, bbox_max_lon)
center_lat = (min_lat_cell + max_lat_cell) / 2
center_lon = (min_lon_cell + max_lon_cell) / 2
```

### Station Exposure Columns

For each grid cell `c` and station `s`:

```text
d(c, s) = haversine distance from cell center to station
w(c, s) = exp(-d(c, s) / decay_m)
```

`nearest_station_distance_m`:

```text
nearest_station_distance_m = min_s d(c, s)
```

`stations_within_500m`:

```text
stations_within_500m = count_s where d(c, s) <= 500
```

`stations_within_1000m`:

```text
stations_within_1000m = count_s where d(c, s) <= 1000
```

`distance_weighted_station_activity`:

```text
distance_weighted_station_activity = sum_s w(c, s) * activity_score_s
```

`distance_weighted_connectivity`:

```text
distance_weighted_connectivity = sum_s w(c, s) * connectivity_s
```

`distance_weighted_residential_density`:

```text
distance_weighted_residential_density =
  sum_s w(c, s) * residential_density_ratio_s
```

`distance_weighted_transfer_score`:

```text
distance_weighted_transfer_score =
  sum_s w(c, s) * is_transfer_proxy_s
```

These formulas mean a cell without a station nearby does not automatically become `0`. Its values decay with distance, and residential/office demand can still be non-zero.

### Office / Jobs Exposure

For optional office/jobs grid cells `j`:

```text
d(c, j) = haversine distance from candidate cell to jobs cell
w(c, j) = exp(-d(c, j) / decay_m)
```

`distance_weighted_office_jobs`:

```text
distance_weighted_office_jobs = sum_j w(c, j) * employment_jobs_j
```

`office_jobs_nearby`:

```text
office_jobs_nearby = sum_j employment_jobs_j where d(c, j) <= 1000
```

If LEHD/office features are not available:

```text
distance_weighted_office_jobs = 0
office_jobs_nearby = 0
```

### Time And Context Flags

`time_bin`:

```text
time_bin = floor(minute_of_day / bin_minutes) * bin_minutes
```

For 30-minute bins:

```text
time_bin in {0, 30, 60, ..., 1410}
hour = floor(time_bin / 60)
minute = time_bin % 60
```

`is_weekend`:

```text
is_weekend = 1 if day_of_week in {5, 6} else 0
```

`is_peak`:

```text
is_peak = 1 if hour in {7, 8, 9, 16, 17, 18} else 0
```

`is_off_peak`:

```text
is_off_peak = 1 if hour < 6 or hour > 21 else 0
```

`is_morning_commute`:

```text
is_morning_commute = 1 if hour in {6, 7, 8, 9} else 0
```

`is_evening_commute`:

```text
is_evening_commute = 1 if hour in {16, 17, 18, 19} else 0
```

`is_workday_midday`:

```text
is_workday_midday = 1 if is_weekend == 0 and 10 <= hour <= 15 else 0
```

Delhi trip rows do not have observed half-hour timestamps. They are expanded to representative hours:

```text
if is_peak: context_hours = [8, 18]
elif is_off_peak: context_hours = [11, 14, 21]
elif is_festival: context_hours = [12, 18, 21]
elif is_weekend: context_hours = [11, 15, 19]
else: context_hours = [8, 12, 18]
```

For 30-minute bins, each representative hour expands to minute `0` and `30`.

### Temporal Land-Use Demand

Residential factor:

```text
if is_weekend: residential_factor = 1.10
elif is_morning_commute: residential_factor = 1.15
elif is_evening_commute: residential_factor = 1.20
elif is_workday_midday: residential_factor = 0.75
elif is_off_peak: residential_factor = 0.50
else: residential_factor = 0.90
```

Office factor:

```text
if is_weekend: office_factor = 0.25
elif is_morning_commute: office_factor = 1.20
elif is_evening_commute: office_factor = 1.20
elif is_workday_midday: office_factor = 1.00
elif is_off_peak: office_factor = 0.10
else: office_factor = 0.60
```

`residential_temporal_demand`:

```text
residential_temporal_demand =
  distance_weighted_residential_density * residential_factor
```

`office_temporal_demand`:

```text
office_temporal_demand =
  distance_weighted_office_jobs * office_factor
```

`commute_demand_score`:

```text
commute_demand_score =
  residential_temporal_demand + office_temporal_demand
```

### GTFS Frequency And Service Exposure

GTFS departure time is converted to minute of day:

```text
minute_of_day = (hour % 24) * 60 + minute
time_bin = floor(minute_of_day / bin_minutes) * bin_minutes
```

Station frequency:

```text
scheduled_trains_station,time_bin =
  number of unique GTFS trip_id values stopping at station in that time_bin
```

Station daily frequency:

```text
daily_scheduled_trains_station =
  sum_time_bin scheduled_trains_station,time_bin
```

Candidate cell scheduled train exposure:

```text
scheduled_trains_cell,time_bin =
  sum_s exp(-d(c, s) / decay_m) * scheduled_trains_s,time_bin
```

Candidate cell daily train exposure:

```text
daily_scheduled_trains_cell =
  sum_s exp(-d(c, s) / decay_m) * daily_scheduled_trains_s
```

`has_gtfs_frequency`:

```text
has_gtfs_frequency = 1 if daily_scheduled_trains > 0 else 0
```

Frequency deltas:

```text
scheduled_trains_after_delta =
  max(0, scheduled_trains + scheduled_trains_delta)
```

If `frequency_delta.csv` provides `hour` without `minute` or `time_bin`, the hourly value is repeated across all bins inside that hour.

### Delhi Training Targets

`load_per_train`:

```text
load_per_train = mean(target_passengers)
```

where `target_passengers` comes from Delhi `Passengers`.

`sample_count`:

```text
sample_count = count(target_passengers)
```

`target_time_bin_flow`:

```text
target_time_bin_flow = load_per_train * scheduled_trains
```

`target_hourly_flow` currently mirrors `target_time_bin_flow` for backward compatibility.

### Model Training

Training rows:

```text
usable_training_rows = rows where load_per_train is not null
```

Target transformation:

```text
y = log1p(load_per_train)
```

Model:

```text
HistGradientBoostingRegressor(loss="squared_error")
```

Prediction back-transform:

```text
predicted_load = max(0, expm1(model.predict(features)))
```

`model_demand_score`:

```text
model_demand_score = min_max(predicted_load)
```

This is learned relative demand potential, not passenger count.

### Shared Demand Component Scores

Raw access:

```text
access_raw =
  exp(-nearest_station_distance_m / 800)
  + 0.25 * stations_within_500m
  + 0.10 * stations_within_1000m
```

`access_demand_score`:

```text
access_demand_score = min_max(access_raw)
```

Raw service:

```text
service_raw =
  log1p(scheduled_trains + line_service_weight)
  + 0.10 * log1p(daily_scheduled_trains)
  + 0.50 * line_network_value
```

Raw access-service:

```text
access_service_raw = access_raw * (0.50 + service_raw)
```

`access_service_demand_score`:

```text
access_service_demand_score = min_max(access_service_raw)
```

Shared dispersion weight:

```text
dispersion_weight_raw =
  0.20
  + access_raw
  + 0.50 * service_raw
  + 0.50 * line_network_value
  + 0.50 * relative_demand_pressure
  + 0.20 * commute_demand_score
```

This is the shared allocation weight used by land-use demand, event surplus, and proposed-line catchments.

`density_activity_demand_score`:

```text
density_activity_raw =
  0.35 * distance_weighted_residential_density
  + 0.25 * distance_weighted_station_activity
  + 0.25 * residential_temporal_demand
  + 0.15 * office_temporal_demand

density_activity_demand_score = min_max(density_activity_raw)
```

`land_use_time_demand_score`:

```text
land_use_base =
  commute_demand_score
  + 0.20 * distance_weighted_office_jobs

land_use_time_demand_score =
  min_max(land_use_base * dispersion_weight_raw)
```

`connectivity_demand_score`:

```text
connectivity_demand_score =
  min_max(distance_weighted_connectivity + 100.0 * distance_weighted_transfer_score)
```

`service_demand_score`:

```text
service_demand_score =
  min_max(scheduled_trains + line_service_weight + 0.05 * daily_scheduled_trains)
```

`line_demand_score`:

```text
line_demand_score =
  min_max(
    line_combined_weight
    + line_service_weight
    + line_connected_demand
    + line_junction_weight
    + line_network_value
  )
```

### Final Baseline Demand Score

`relative_demand_pressure_raw`:

```text
relative_demand_pressure_raw =
  0.25 * model_demand_score
  + 0.20 * density_activity_demand_score
  + 0.15 * service_demand_score
  + 0.10 * access_demand_score
  + 0.05 * access_service_demand_score
  + 0.10 * connectivity_demand_score
  + 0.10 * land_use_time_demand_score
  + 0.05 * line_demand_score
```

`relative_demand_pressure`:

```text
relative_demand_pressure = min_max(relative_demand_pressure_raw)
```

Baseline `demand_score`:

```text
demand_score = relative_demand_pressure
```

A cell with no nearby station can still have non-zero demand because the density, office, learned model, and temporal land-use terms can be non-zero. Station access and service terms will be low, but not every component is forced to zero.

### Proposed Line Calculations

For each grid cell and proposed line segment:

```text
nearest_line_distance_m = min distance from cell center to any proposed line segment
nearest_line_station_distance_m = min distance from cell center to any proposed station
```

Line proximity:

```text
line_distance_weight =
  exp(-nearest_line_distance_m / decay_m)
  if nearest_line_distance_m <= radius_m
  else 0
```

Station proximity:

```text
line_station_weight =
  exp(-nearest_line_station_distance_m / decay_m)
  if nearest_line_station_distance_m <= radius_m
  else 0
```

Combined proximity:

```text
line_combined_weight = max(line_distance_weight, line_station_weight)
```

Learned demand potential for line weighting:

```text
learned_demand_potential = relative_demand_pressure
```

where `relative_demand_pressure` is produced by the Delhi-trained demand model on candidate grid cells.

Heuristic catchment:

```text
heuristic_catchment =
  0.35 * min_max(distance_weighted_residential_density)
  + 0.25 * min_max(distance_weighted_office_jobs)
  + 0.15 * min_max(distance_weighted_station_activity)
  + 0.15 * min_max(distance_weighted_connectivity)
  + 0.10 * min_max(daily_scheduled_trains)
```

Cell catchment demand:

```text
catchment_demand_score =
  0.60 * min_max(learned_demand_potential)
  + 0.25 * heuristic_catchment
  + 0.15 * min_max(dispersion_weight)
```

Junction context:

```text
junction_context_score =
  0.35 * min_max(distance_weighted_connectivity)
  + 0.25 * min_max(distance_weighted_transfer_score)
  + 0.20 * min_max(stations_within_500m)
  + 0.20 * min_max(stations_within_1000m)
```

For proposed station `p` and candidate cell `c`:

```text
w(p, c) =
  exp(-distance(p, c) / decay_m)
  if distance(p, c) <= radius_m
  else 0
```

Proposed station catchment demand:

```text
line_station_catchment_demand_p =
  sum_c w(p, c) * catchment_demand_score_c / sum_c w(p, c)
```

Inferred junction potential:

```text
inferred_junction_p =
  sum_c w(p, c) * junction_context_score_c / sum_c w(p, c)
```

Supplied junction potential from optional station fields:

```text
supplied_junction_p =
  0.60 * min_max(connectivity_p)
  + 0.40 * is_transfer_proxy_p
```

Proposed station junction potential:

```text
line_station_junction_potential_p =
  max(inferred_junction_p, supplied_junction_p)
```

Connected pair demand:

```text
connected_pair_demand =
  sqrt(top_station_demand_1 * top_station_demand_2)
```

where `top_station_demand_1` and `top_station_demand_2` are the two highest proposed-station catchment demands.

Line connected demand:

```text
line_connected_demand_base =
  0.45 * mean(line_station_catchment_demand)
  + 0.55 * connected_pair_demand
```

Line junction weight:

```text
line_junction_weight_base = max(line_station_junction_potential)
```

Line network value:

```text
line_network_value_base =
  line_connected_demand_base * (1.0 + 0.50 * line_junction_weight_base)
```

Cell-level line columns:

```text
line_connected_demand =
  line_combined_weight * line_connected_demand_base

line_junction_weight =
  line_combined_weight * line_junction_weight_base

line_network_value =
  line_combined_weight * line_network_value_base

line_service_weight =
  line_network_value * line_scheduled_trains
```

This is why a line to nowhere receives weak demand: it needs nearby learned demand potential, useful station catchments, or junction value to produce a high `line_network_value`.

### Event Scenario Calculations

Event active bins:

```text
first_bin = floor(start_minute / bin_minutes) * bin_minutes
last_bin = floor(end_minute / bin_minutes) * bin_minutes
active_bins = first_bin, first_bin + bin_minutes, ..., last_bin
```

If only `start_hour` and `end_hour` are supplied:

```text
start_minute = start_hour * 60
end_minute = end_hour * 60 + 59
```

Tail bins:

```text
tail_weight_i = event_tail_decay ^ i
```

for `i = 1..event_tail_bins`. Defaults are:

```text
event_tail_bins = 4
event_tail_decay = 0.5
```

Total temporal weight:

```text
total_temporal_weight =
  sum(active_bin_weights) + sum(tail_bin_weights)
```

Users assigned to a bin:

```text
bin_users =
  surplus_users * temporal_weight_bin / total_temporal_weight
```

For each event bin and candidate cell:

```text
event_spatial_weight =
  exp(-distance_to_event / decay_m)
  if distance_to_event <= radius_m
  else 0

event_allocation_weight =
  event_spatial_weight * max(dispersion_weight_raw, 0.05)
```

Event surplus flow:

```text
event_surplus_flow_cell,bin =
  bin_users
  * event_allocation_weight_cell
  / sum_cells(event_allocation_weight)
```

Event demand score:

```text
event_demand_score = min_max(event_surplus_flow)
```

Scenario raw pressure after events:

```text
scenario_demand_pressure_raw =
  relative_demand_pressure_raw + 0.20 * event_demand_score
```

Scenario normalized pressure:

```text
scenario_demand_pressure = min_max(scenario_demand_pressure_raw)
demand_score = scenario_demand_pressure
```

### Scenario Delta Calculations

Scenario rows are matched to baseline rows by:

```text
cell_id + day_of_week + time_bin
```

Raw demand delta:

```text
demand_delta =
  scenario_demand_pressure_raw - baseline_demand_pressure_raw
```

Percent change:

```text
percent_change =
  100 * demand_delta / baseline_demand_pressure_raw
```

If baseline raw pressure is `0`, percent change is reported as `0`.

### GeoJSON Summary Calculations

`demand_heatmap_grid.geojson` is a static summary. It aggregates scored rows by `cell_id`:

```text
scenario_demand_score = mean(scenario_demand_score over all time rows in cell)
demand_delta = mean(demand_delta over all time rows in cell)
percent_change = mean(percent_change over all time rows in cell)
demand_score = scenario_demand_score
```

The CSV remains the canonical output for timelapse playback.

## Final Outputs For The Frontend

Use the CSV as the canonical timelapse artifact:

- `curr_data/processed/model_outputs/demand_heatmap_predictions.csv`
- `curr_data/processed/model_outputs/demand_heatmap_scenario_predictions.csv`

These preserve every:

- `cell_id`
- `day_of_week`
- `time_bin`
- `demand_score`
- `demand_delta`

Use GeoJSON only as a static summary layer:

- `curr_data/processed/model_outputs/demand_heatmap_grid.geojson`

It aggregates across time and does not contain the full timelapse.

## Recommended Streamlined Command Flow

```bash
# 1. Build reusable features.
.venv/bin/python scripts/build_features.py --skip-download

# 2. Train and score baseline demand.
.venv/bin/python scripts/train_models.py

# 3. Test scenario mechanics.
.venv/bin/python scripts/test_scenarios.py
```

Keep the main story focused on:

1. Delhi weak-supervised demand potential.
2. Target city census, station, GTFS, and optional jobs features.
3. Shared dispersion logic for commute, event, and line scenarios.
4. CSV timelapse output for the frontend.
