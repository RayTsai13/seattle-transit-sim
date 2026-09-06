import { describe, expect, it } from 'vitest';
import { frameToGeoJSON, emptyGrid } from '../grid.ts';
import type { CellTuple, GridConfig } from '../grid.ts';

const config: GridConfig = {
  bounds: { west: -122.5, south: 47.5, east: -122.4, north: 47.6 },
  rows: 10,
  cols: 10,
};

const cellWidth = (config.bounds.east - config.bounds.west) / config.cols;
const cellHeight = (config.bounds.north - config.bounds.south) / config.rows;

describe('frameToGeoJSON', () => {
  it('returns an empty collection for an empty frame', () => {
    expect(frameToGeoJSON([], config).features).toHaveLength(0);
  });

  it('places samples inside the cell they belong to', () => {
    const cells: CellTuple[] = [[5, 5, 1]];
    const { features } = frameToGeoJSON(cells, config);
    expect(features.length).toBeGreaterThan(0);

    // Every emitted point must fall within one cell of the hot cell's centre.
    const centreLon = config.bounds.west + (5 + 0.5) * cellWidth;
    const centreLat = config.bounds.north - (5 + 0.5) * cellHeight;
    for (const feature of features) {
      const [lon, lat] = feature.geometry.coordinates;
      expect(Math.abs(lon - centreLon)).toBeLessThanOrEqual(cellWidth * 1.5);
      expect(Math.abs(lat - centreLat)).toBeLessThanOrEqual(cellHeight * 1.5);
    }
  });

  it('never emits a density above the source value', () => {
    const { features } = frameToGeoJSON([[4, 4, 0.8]], config);
    for (const feature of features) {
      const density = feature.properties?.density as number;
      expect(density).toBeGreaterThan(0);
      expect(density).toBeLessThanOrEqual(0.8 + 1e-6);
    }
  });

  it('ignores cells outside the configured grid', () => {
    const cells: CellTuple[] = [
      [-1, 0, 1],
      [0, 99, 1],
      [999, 999, 1],
    ];
    expect(frameToGeoJSON(cells, config).features).toHaveLength(0);
  });

  it('produces the same output whether or not the whole grid is scanned', () => {
    // The conversion only visits cells within one step of a non-zero value.
    // That optimisation must not change the result, so compare against a
    // reference that rasterises every cell of the frame explicitly.
    const cells: CellTuple[] = [
      [2, 3, 0.9],
      [7, 8, 0.4],
      [0, 0, 0.55],
      [9, 9, 0.7],
    ];
    const sparse = frameToGeoJSON(cells, config);

    const everyCell: CellTuple[] = [];
    for (let row = 0; row < config.rows; row++) {
      for (let col = 0; col < config.cols; col++) {
        const match = cells.find(([r, c]) => r === row && c === col);
        everyCell.push([row, col, match ? match[2] : 0]);
      }
    }
    const dense = frameToGeoJSON(everyCell, config);

    const key = (f: (typeof sparse.features)[number]) =>
      `${f.geometry.coordinates[0].toFixed(9)},${f.geometry.coordinates[1].toFixed(9)},${
        (f.properties?.density as number).toFixed(6)
      }`;
    expect(new Set(sparse.features.map(key))).toEqual(
      new Set(dense.features.map(key)),
    );
  });

  it('scales work with the active area, not the grid size', () => {
    const bigConfig: GridConfig = { ...config, rows: 200, cols: 200 };
    const before = performance.now();
    const { features } = frameToGeoJSON([[100, 100, 1]], bigConfig);
    const elapsed = performance.now() - before;

    // One hot cell in a 40,000-cell grid touches at most 9 cells x 9 samples.
    expect(features.length).toBeLessThanOrEqual(81);
    expect(elapsed).toBeLessThan(50);
  });
});

describe('emptyGrid', () => {
  it('is a valid empty FeatureCollection', () => {
    expect(emptyGrid()).toEqual({ type: 'FeatureCollection', features: [] });
  });
});
