"""Shared artifact names and default data locations."""

from __future__ import annotations

from pathlib import Path


DEFAULT_RAW_DIR = Path("curr_data/raw")
DEFAULT_PROCESSED_DIR = Path("curr_data/processed")
DEFAULT_GTFS_DIR = Path("gtfs")

PROCESSED_FEATURES_DIR = "features"
PROCESSED_MODEL_OUTPUTS_DIR = "model_outputs"
PROCESSED_SCENARIOS_DIR = "scenarios"

DELHI_TRIPS_CSV = "delhi_metro_updated.csv"
DELHI_STATION_COORDINATES_CSV = "delhi_metro_station_coordinates.csv"
DELHI_WARDS_GEOJSON = "delhi_wards.geojson"
DELHI_WARD_POPULATION_CSV = "delhi_ward_population.csv"
DELHI_KAGGLE_ARCHIVE_ZIP = "delhi_metro_dataset_kaggle.zip"
DELHI_GTFS_ARCHIVE_ZIP = "delhi_gtfs.zip"
DELHI_GTFS_DIR = "gtfs_delhi"

KING_COUNTY_ACS_TRACT_POPULATION_CSV = "king_county_acs_tract_population.csv"
SEATTLE_ACS_PLACE_POPULATION_CSV = "seattle_acs_place_population.csv"
WASHINGTON_TRACT_SHAPEFILE_ZIP = "cb_2022_53_tract_500k.zip"
WASHINGTON_PLACE_SHAPEFILE_ZIP = "cb_2022_53_place_500k.zip"
PUGET_SOUND_GTFS_ZIP = "gtfs_puget_sound_consolidated.zip"
FREMONT_BRIDGE_COUNTS_CSV = "fremont_bridge_counts.csv"
TRANSIT_ACCESSIBILITY_CSV = "transit_accessibility.csv"
US_COUNTIES_GEOJSON = "us_counties.geojson"

DELHI_STATION_DENSITY_CSV = "delhi_station_density.csv"
DELHI_POPULATION_VECTOR_SUMMARY_JSON = "delhi_population_vector_summary.json"
DELHI_STATION_VECTORS_CSV = "delhi_station_vectors.csv"
DELHI_TRIP_FEATURES_CSV = "delhi_trip_features.csv"
DELHI_TRAIN_FEATURES_CSV = "delhi_train_features.csv"
DELHI_TEST_FEATURES_CSV = "delhi_test_features.csv"
DELHI_TRAIN_TEST_SUMMARY_JSON = "delhi_train_test_summary.json"
DELHI_STATION_GTFS_FREQUENCY_CSV = "delhi_station_gtfs_frequency.csv"
DELHI_HEATMAP_TRAINING_FEATURES_CSV = "delhi_heatmap_training_features.csv"

SEATTLE_STATION_VECTORS_CSV = "seattle_station_vectors.csv"
SEATTLE_STATION_VECTOR_SUMMARY_JSON = "seattle_station_vector_summary.json"
SEATTLE_HEATMAP_FEATURES_CSV = "seattle_heatmap_features.csv"
SEATTLE_HEATMAP_GRID_GEOJSON = "seattle_heatmap_grid.geojson"
SEATTLE_HEATMAP_MODEL_METRICS_JSON = "seattle_heatmap_model_metrics.json"
SEATTLE_HEATMAP_PREDICTIONS_CSV = "seattle_heatmap_predictions.csv"

CITY_HEATMAP_CANDIDATE_FEATURES_CSV = "city_heatmap_candidate_features.csv"
HEATMAP_TIMELAPSE_MODEL_METRICS_JSON = "heatmap_timelapse_model_metrics.json"
HEATMAP_TIMELAPSE_PREDICTIONS_CSV = "heatmap_timelapse_predictions.csv"
HEATMAP_TIMELAPSE_SCENARIO_PREDICTIONS_CSV = "heatmap_timelapse_scenario_predictions.csv"
HEATMAP_TIMELAPSE_GRID_GEOJSON = "heatmap_timelapse_grid.geojson"

DEMAND_HEATMAP_MODEL_METRICS_JSON = "demand_heatmap_model_metrics.json"
DEMAND_HEATMAP_PREDICTIONS_CSV = "demand_heatmap_predictions.csv"
DEMAND_HEATMAP_SCENARIO_PREDICTIONS_CSV = "demand_heatmap_scenario_predictions.csv"
DEMAND_HEATMAP_GRID_GEOJSON = "demand_heatmap_grid.geojson"
