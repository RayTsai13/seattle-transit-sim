import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { buildCentroidLookup, emptyGrid, frameToGeoJSON } from './grid.ts';
import type { GridConfig, Frame } from './grid.ts';
import {
  deleteAllPeople,
  deletePerson,
  postPlayback,
  postPeople,
  postScenario,
  seekPlayback,
  type PlaybackState,
  type PlacedPerson,
} from './api.ts';

const STREAM_URL = '/api/heatmap/stream';

export type HeatmapApi = {
  geojson: FeatureCollection<Point>;
  /** Last scenario_id confirmed by the server via a `scenario` event. */
  scenarioId: string | null;
  playback: PlaybackState | null;
  diagnostics: HeatmapDiagnostics;
  setScenario: (id: string) => Promise<void>;
  setPlaying: (isPlaying: boolean) => Promise<void>;
  seekTo: (dayOfWeek: number, timeBin: number) => Promise<void>;
  addPeople: (lat: number, lon: number, count?: number) => Promise<PlacedPerson>;
  removePeople: (id: string) => Promise<void>;
  clearPeople: () => Promise<void>;
};

export type HeatmapDiagnostics = {
  connection: 'connecting' | 'open' | 'error';
  pendingScenarioId: string | null;
  confirmedScenarioId: string | null;
  frameCount: number;
  lastFrameCellCount: number;
  featureCount: number;
  lastFrameAt: number | null;
  config: Pick<GridConfig, 'rows' | 'cols'> | null;
  simTime: PlaybackState['sim_time'] | null;
  lastError: string | null;
};

export function useHeatmap(): HeatmapApi {
  const [geojson, setGeojson] = useState<FeatureCollection<Point>>(emptyGrid());
  const [scenarioId, setScenarioId] = useState<string | null>(null);
  const [playback, setPlaybackState] = useState<PlaybackState | null>(null);
  const [diagnostics, setDiagnostics] = useState<HeatmapDiagnostics>({
    connection: 'connecting',
    pendingScenarioId: null,
    confirmedScenarioId: null,
    frameCount: 0,
    lastFrameCellCount: 0,
    featureCount: 0,
    lastFrameAt: null,
    config: null,
    simTime: null,
    lastError: null,
  });
  const centroidsRef = useRef<Float64Array | null>(null);
  const configRef = useRef<GridConfig | null>(null);
  // Set when a scenario switch is in flight; frames are dropped until the
  // server's `scenario` event confirms the switch by matching this value.
  const pendingScenarioRef = useRef<string | null>(null);

  useEffect(() => {
    const source = new EventSource(STREAM_URL);

    source.addEventListener('open', () => {
      setDiagnostics((current) => ({
        ...current,
        connection: 'open',
        lastError: null,
      }));
    });

    source.addEventListener('config', (e: MessageEvent) => {
      const config: GridConfig = JSON.parse(e.data as string);
      configRef.current = config;
      centroidsRef.current = buildCentroidLookup(config);
      setDiagnostics((current) => ({
        ...current,
        config: { rows: config.rows, cols: config.cols },
      }));
    });

    source.addEventListener('scenario', (e: MessageEvent) => {
      const { scenario_id } = JSON.parse(e.data as string) as { scenario_id: string };
      setScenarioId(scenario_id);
      if (pendingScenarioRef.current === scenario_id) {
        pendingScenarioRef.current = null;
      }
      setDiagnostics((current) => ({
        ...current,
        confirmedScenarioId: scenario_id,
        pendingScenarioId: pendingScenarioRef.current,
      }));
    });

    source.addEventListener('playback', (e: MessageEvent) => {
      const nextPlayback: PlaybackState = JSON.parse(e.data as string);
      setPlaybackState(nextPlayback);
      setDiagnostics((current) => ({
        ...current,
        simTime: nextPlayback.sim_time,
      }));
    });

    source.addEventListener('frame', (e: MessageEvent) => {
      if (!centroidsRef.current || !configRef.current) return;
      if (pendingScenarioRef.current !== null) return;
      const frame: Frame = JSON.parse(e.data as string);
      const nextGeojson = frameToGeoJSON(frame.cells, centroidsRef.current, configRef.current.cols);
      setGeojson(
        nextGeojson,
      );
      setDiagnostics((current) => ({
        ...current,
        frameCount: current.frameCount + 1,
        lastFrameCellCount: frame.cells.length,
        featureCount: nextGeojson.features.length,
        lastFrameAt: Date.now(),
        simTime: frame.sim_time ?? current.simTime,
      }));
    });

    source.addEventListener('clear', () => {
      setGeojson(emptyGrid());
      setDiagnostics((current) => ({
        ...current,
        featureCount: 0,
        lastFrameCellCount: 0,
      }));
    });

    source.addEventListener('error', () => {
      console.warn('[heatmap] SSE connection error, will auto-reconnect');
      setDiagnostics((current) => ({
        ...current,
        connection: 'error',
        lastError: 'SSE connection error; browser will retry automatically.',
      }));
    });

    return () => {
      source.close();
    };
  }, []);

  const setScenario = useCallback(async (id: string) => {
    pendingScenarioRef.current = id;
    setDiagnostics((current) => ({
      ...current,
      pendingScenarioId: id,
    }));
    try {
      await postScenario(id);
    } catch (err) {
      pendingScenarioRef.current = null;
      setDiagnostics((current) => ({
        ...current,
        pendingScenarioId: null,
        lastError: err instanceof Error ? err.message : 'Failed to post scenario.',
      }));
      throw err;
    }
  }, []);

  const setPlaying = useCallback(async (isPlaying: boolean) => {
    const nextPlayback = await postPlayback({ is_playing: isPlaying });
    setPlaybackState(nextPlayback);
  }, []);

  const seekTo = useCallback(async (dayOfWeek: number, timeBin: number) => {
    const nextPlayback = await seekPlayback(dayOfWeek, timeBin);
    setPlaybackState(nextPlayback);
    setDiagnostics((current) => ({
      ...current,
      simTime: nextPlayback.sim_time,
    }));
  }, []);

  const addPeople = useCallback(
    (lat: number, lon: number, count = 1) => postPeople(lat, lon, count),
    [],
  );
  const removePeople = useCallback((id: string) => deletePerson(id), []);
  const clearPeople = useCallback(() => deleteAllPeople(), []);

  return {
    geojson,
    scenarioId,
    playback,
    diagnostics,
    setScenario,
    setPlaying,
    seekTo,
    addPeople,
    removePeople,
    clearPeople,
  };
}
