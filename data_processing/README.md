# Mobility and heatmap datasets

> **Offline feature pipelines only.** The model-training stage was removed: the
> runtime backend (`backend/`) generates demand analytically from the land-use
> model in `backend/landuse.py` and consumes nothing from this directory.
> Sections describing training and model scoring have been removed accordingly;
> `DEMAND_PIPELINE_PROCESS.md` and `INTERACTIVE_TIMELAPSE_PROPOSAL.md` are kept
> as historical design records of that removed pipeline.

This repo contains three related paths:

1. **Seattle grid heatmap**: grid cells, observed bike counts, GTFS supply, optional LEHD.
2. **Cross-city station vectors**: Delhi trip features with train/test splits, and Delhi/Seattle station-level vectors with comparable density/connectivity activity proxies.
3. **City-generic demand heatmap**: train a Delhi-weak-supervised relative demand model, score a 48-bin x 7-day city grid from census/station vectors and GTFS supply, then run route/station/event scenarios.

Source code lives under `src/`:

- `src/common/`: shared I/O, station, and geospatial utilities.
- `src/pipelines/delhi/`: Delhi density, trip, and train/test feature builders.
- `src/pipelines/seattle/`: Seattle station-vector and heatmap feature builders.

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
```

Common build options:

```bash
.venv/bin/python scripts/build_features.py --skip-download
.venv/bin/python scripts/build_features.py --agency-ids 40 --route-types 0,1,2
.venv/bin/python scripts/build_features.py --include-lehd
.venv/bin/python scripts/build_features.py --skip-seattle-heatmap
```

By default, the wrappers keep processed artifacts in stage folders:

- `curr_data/processed/features/`: station vectors, candidate features, and feature grids.

---

## 1. Seattle grid heatmap dataset

### Outputs

- `curr_data/processed/features/seattle_heatmap_features.csv`: rows by grid cell, hour, and day of week.
- `curr_data/processed/features/seattle_heatmap_grid.geojson`: grid polygons with `congestion_score`.
  The backend reads the copy at `seattle/data/processed/` for grid bounds / rows / cols only; it
  does not use `congestion_score`.

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

Scenario overlays stop at the candidate-feature stage; the scoring step that
consumed them was part of the removed model pipeline.

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
