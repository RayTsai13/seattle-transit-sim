import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { emptyGrid, frameToGeoJSON } from './grid.ts';
import type { GridConfig, Frame } from './grid.ts';
import {
  deleteAllPeople,
  deletePerson,
  postPlayback,
  postPeople,
  postScenario,
  seekPlayback,
  type PeopleOptions,
  type PlaybackState,
  type PlacedPerson,
  type ScenarioLine,
  type ScenarioStop,
} from './api.ts';

const STREAM_URL = '/api/heatmap/stream';

/** Consecutive SSE errors before we stop calling it a transient blip. */
const CONNECTION_FAILURE_THRESHOLD = 3;

function stateVersionNumber(stateVersion?: string): number | null {
  if (!stateVersion) return null;
  const match = /^state_v(\d+)$/.exec(stateVersion);
  return match ? Number.parseInt(match[1], 10) : null;
}

export type HeatmapOptions = {
  /**
   * Whether anyone is actually displaying the heatmap. Converting a frame to
   * GeoJSON is the most expensive thing this hook does, so it is skipped while
   * the layer is hidden; the raw frame is retained so re-enabling is instant.
   */
  renderGeometry?: boolean;
};

export type HeatmapApi = {
  geojson: FeatureCollection<Point>;
  /** Last scenario_id confirmed by the server via a `scenario` event. */
  scenarioId: string | null;
  playback: PlaybackState | null;
  diagnostics: HeatmapDiagnostics;
  /** Force a fresh EventSource after the connection has been declared failed. */
  retryConnection: () => void;
  setScenario: (id: string, stops?: ScenarioStop[], lines?: ScenarioLine[]) => Promise<void>;
  setPlaying: (isPlaying: boolean) => Promise<void>;
  seekTo: (dayOfWeek: number, timeBin: number) => Promise<void>;
  addPeople: (
    lat: number,
    lon: number,
    count?: number,
    options?: PeopleOptions,
  ) => Promise<PlacedPerson>;
  removePeople: (id: string) => Promise<void>;
  clearPeople: () => Promise<void>;
};

export type HeatmapDiagnostics = {
  connection: 'connecting' | 'open' | 'error' | 'failed';
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

export function useHeatmap(options: HeatmapOptions = {}): HeatmapApi {
  const { renderGeometry = true } = options;

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
  const configRef = useRef<GridConfig | null>(null);
  const minimumFrameStateVersionRef = useRef<number | null>(null);
  // Set when a scenario switch is in flight; frames are dropped until the
  // server's `scenario` event confirms the switch by matching this value.
  const pendingScenarioRef = useRef<string | null>(null);
  const consecutiveErrorsRef = useRef(0);
  // The most recent frame as it arrived, so the heatmap can be re-shown
  // without waiting for the next tick.
  const latestFrameRef = useRef<Frame | null>(null);
  const renderGeometryRef = useRef(renderGeometry);
  const [retryToken, setRetryToken] = useState(0);

  // The SSE listeners are registered once and must see the current value.
  useEffect(() => {
    renderGeometryRef.current = renderGeometry;
  }, [renderGeometry]);

  useEffect(() => {
    const source = new EventSource(STREAM_URL);

    /** Parse an SSE payload without letting a bad frame kill the listener. */
    const parse = <T,>(event: MessageEvent, label: string): T | null => {
      try {
        return JSON.parse(event.data as string) as T;
      } catch (err) {
        console.warn(`[heatmap] discarded a malformed \`${label}\` event`, err);
        setDiagnostics((current) => ({
          ...current,
          lastError: `Malformed \`${label}\` event from the server.`,
        }));
        return null;
      }
    };

    source.addEventListener('open', () => {
      consecutiveErrorsRef.current = 0;
      setDiagnostics((current) => ({
        ...current,
        connection: 'open',
        lastError: null,
      }));
    });

    source.addEventListener('config', (e: MessageEvent) => {
      const config = parse<GridConfig>(e, 'config');
      if (!config) return;
      configRef.current = config;
      // `config` is re-emitted on every (re)connection, which makes it the one
      // reliable signal that the server may be a *different* process than the
      // one these gates were calibrated against. A restarted backend counts
      // state_version from zero again, so without this reset every subsequent
      // frame sits below the floor and is dropped forever -- the map freezes
      // while the connection still reports itself as open.
      minimumFrameStateVersionRef.current = null;
      pendingScenarioRef.current = null;
      consecutiveErrorsRef.current = 0;
      setDiagnostics((current) => ({
        ...current,
        config: { rows: config.rows, cols: config.cols },
        pendingScenarioId: null,
      }));
    });

    source.addEventListener('scenario', (e: MessageEvent) => {
      const payload = parse<{ scenario_id: string }>(e, 'scenario');
      if (!payload) return;
      setScenarioId(payload.scenario_id);
      if (pendingScenarioRef.current === payload.scenario_id) {
        pendingScenarioRef.current = null;
      }
      setDiagnostics((current) => ({
        ...current,
        confirmedScenarioId: payload.scenario_id,
        pendingScenarioId: pendingScenarioRef.current,
      }));
    });

    source.addEventListener('playback', (e: MessageEvent) => {
      const nextPlayback = parse<PlaybackState>(e, 'playback');
      if (!nextPlayback) return;
      setPlaybackState(nextPlayback);
      setDiagnostics((current) => ({
        ...current,
        simTime: nextPlayback.sim_time,
      }));
    });

    source.addEventListener('frame', (e: MessageEvent) => {
      if (!configRef.current) return;
      if (pendingScenarioRef.current !== null) return;
      const frame = parse<Frame>(e, 'frame');
      if (!frame) return;
      const frameVersion = stateVersionNumber(frame.state_version);
      const minimumFrameStateVersion = minimumFrameStateVersionRef.current;
      if (
        frameVersion !== null &&
        minimumFrameStateVersion !== null &&
        frameVersion < minimumFrameStateVersion
      ) {
        return;
      }
      latestFrameRef.current = frame;

      let featureCount = 0;
      if (renderGeometryRef.current) {
        const nextGeojson = frameToGeoJSON(frame.cells, configRef.current);
        featureCount = nextGeojson.features.length;
        setGeojson(nextGeojson);
      }
      setDiagnostics((current) => ({
        ...current,
        frameCount: current.frameCount + 1,
        lastFrameCellCount: frame.cells.length,
        featureCount: renderGeometryRef.current ? featureCount : current.featureCount,
        lastFrameAt: Date.now(),
        simTime: frame.sim_time ?? current.simTime,
      }));
    });

    source.addEventListener('clear', () => {
      latestFrameRef.current = null;
      setGeojson(emptyGrid());
      setDiagnostics((current) => ({
        ...current,
        featureCount: 0,
        lastFrameCellCount: 0,
      }));
    });

    source.addEventListener('error', () => {
      consecutiveErrorsRef.current += 1;
      const failed = consecutiveErrorsRef.current >= CONNECTION_FAILURE_THRESHOLD;
      if (failed) {
        console.warn('[heatmap] SSE connection is not recovering');
      }
      setDiagnostics((current) => ({
        ...current,
        connection: failed ? 'failed' : 'error',
        lastError: failed
          ? 'Cannot reach the simulation server.'
          : 'SSE connection error; browser will retry automatically.',
      }));
    });

    return () => {
      source.close();
    };
  }, [retryToken]);

  // Re-showing the heatmap converts the frame we already have, so the layer
  // does not stay empty until the next tick arrives.
  useEffect(() => {
    if (!renderGeometry) return;
    const frame = latestFrameRef.current;
    const config = configRef.current;
    if (!frame || !config) return;
    const nextGeojson = frameToGeoJSON(frame.cells, config);
    setGeojson(nextGeojson);
    setDiagnostics((current) => ({
      ...current,
      featureCount: nextGeojson.features.length,
    }));
  }, [renderGeometry]);

  const retryConnection = useCallback(() => {
    consecutiveErrorsRef.current = 0;
    setDiagnostics((current) => ({
      ...current,
      connection: 'connecting',
      lastError: null,
    }));
    setRetryToken((token) => token + 1);
  }, []);

  const setScenario = useCallback(async (
    id: string,
    stops: ScenarioStop[] = [],
    lines: ScenarioLine[] = [],
  ) => {
    pendingScenarioRef.current = id;
    setDiagnostics((current) => ({
      ...current,
      pendingScenarioId: id,
    }));
    try {
      const response = await postScenario(id, stops, lines);
      if (response.frame && configRef.current) {
        const frameVersion = stateVersionNumber(response.frame.state_version);
        if (frameVersion !== null) {
          minimumFrameStateVersionRef.current = Math.max(
            minimumFrameStateVersionRef.current ?? frameVersion,
            frameVersion,
          );
        }
        latestFrameRef.current = response.frame;
        const nextGeojson = renderGeometryRef.current
          ? frameToGeoJSON(response.frame.cells, configRef.current)
          : null;
        setScenarioId(response.scenario_id);
        if (nextGeojson) setGeojson(nextGeojson);
        setDiagnostics((current) => ({
          ...current,
          confirmedScenarioId: response.scenario_id,
          frameCount: current.frameCount + 1,
          lastFrameCellCount: response.frame?.cells.length ?? 0,
          featureCount: nextGeojson ? nextGeojson.features.length : current.featureCount,
          lastFrameAt: Date.now(),
          simTime: response.frame?.sim_time ?? current.simTime,
        }));
      }
    } catch (err) {
      setDiagnostics((current) => ({
        ...current,
        lastError: err instanceof Error ? err.message : 'Failed to post scenario.',
      }));
      throw err;
    } finally {
      // Always lift the gate. Leaving it raised -- which happened whenever the
      // response carried no frame, or config had not arrived yet -- drops every
      // subsequent frame and freezes the map.
      pendingScenarioRef.current = null;
      setDiagnostics((current) => ({ ...current, pendingScenarioId: null }));
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
    (lat: number, lon: number, count = 1, options: PeopleOptions = {}) =>
      postPeople(lat, lon, count, options),
    [],
  );
  const removePeople = useCallback((id: string) => deletePerson(id), []);
  const clearPeople = useCallback(() => deleteAllPeople(), []);

  return {
    geojson,
    scenarioId,
    playback,
    diagnostics,
    retryConnection,
    setScenario,
    setPlaying,
    seekTo,
    addPeople,
    removePeople,
    clearPeople,
  };
}
