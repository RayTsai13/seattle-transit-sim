import type { FeatureCollection, Point } from 'geojson';

export type Bounds = {
  west: number;
  south: number;
  east: number;
  north: number;
};

export type GridConfig = {
  bounds: Bounds;
  rows: number;
  cols: number;
};

/** [row, col, density] tuple from the SSE frame */
export type CellTuple = [row: number, col: number, density: number];

export type Frame = {
  timestamp: number;
  state_version?: string;
  sim_time?: SimTime;
  cells: CellTuple[];
};

export type SimTime = {
  day_of_week: number;
  time_bin: number;
  minute_of_week: number;
};

/**
 * Precompute a flat array of [lon, lat] pairs for every (row, col).
 * Index formula: (row * cols + col) * 2 → lon, +1 → lat.
 */
export function buildCentroidLookup(config: GridConfig): Float64Array {
  const { bounds, rows, cols } = config;
  const cellWidth = (bounds.east - bounds.west) / cols;
  const cellHeight = (bounds.north - bounds.south) / rows;
  const lookup = new Float64Array(rows * cols * 2);

  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const idx = (r * cols + c) * 2;
      lookup[idx] = bounds.west + (c + 0.5) * cellWidth;
      lookup[idx + 1] = bounds.north - (r + 0.5) * cellHeight;
    }
  }
  return lookup;
}

export function frameToGeoJSON(
  cells: CellTuple[],
  centroids: Float64Array,
  cols: number,
): FeatureCollection<Point> {
  return {
    type: 'FeatureCollection',
    features: cells.map(([row, col, density]) => {
      const idx = (row * cols + col) * 2;
      return {
        type: 'Feature' as const,
        geometry: {
          type: 'Point' as const,
          coordinates: [centroids[idx], centroids[idx + 1]],
        },
        properties: { density },
      };
    }),
  };
}

export function emptyGrid(): FeatureCollection<Point> {
  return { type: 'FeatureCollection', features: [] };
}
