import { useCallback, useEffect, useMemo, useState, useRef } from "react";
import {
  Layer,
  Map,
  NavigationControl,
  Source,
  Marker,
} from "react-map-gl/maplibre";
import type { FeatureCollection, Geometry, Point } from "geojson";
import type { Map as MapLibreMap } from "maplibre-gl";
import type { LayerProps } from "react-map-gl/maplibre";
import type {
  MapLayerMouseEvent,
  MapRef,
  ViewStateChangeEvent,
} from "react-map-gl/maplibre";
import "maplibre-gl/dist/maplibre-gl.css";
import "./App.css";
import { useHeatmap } from "./heatmap/stream.ts";
import { heatmapLayer } from "./heatmap/layer.ts";
import { DEPLOY_STEPS, stopsToGeoJSON, linesToGeoJSON } from "./stops/data.ts";
import type { TransitStop, TransitLine } from "./stops/data.ts";
import {
  synthesizeTrainInstances,
  type TrainInstance,
  useInterpolatedMinuteOfWeek,
} from "./stops/trains.ts";
import {
  lineCasingLayer,
  lineRouteLayer,
  stopCircleLayer,
  stopDotLayer,
  stopLabelLayer,
  deployPulseRingLayer,
  deployGlowDotLayer,
} from "./stops/layers.ts";
import { DeckGLOverlay } from "./DeckGLOverlay.tsx";
import { ScenegraphLayer } from "@deck.gl/mesh-layers";
import { AmbientLight, DirectionalLight, LightingEffect } from "@deck.gl/core";
import { ScatterplotLayer } from "@deck.gl/layers";
import { Mascot } from "./Mascot.tsx";

const ambientLight = new AmbientLight({
  color: [255, 255, 255],
  intensity: 10.0, // Supernova brightness
});

const blueLight = new DirectionalLight({
  color: [255, 255, 255], // Pure white directional light
  intensity: 5.0,
  direction: [-1, -3, -1],
});

const lightingEffect = new LightingEffect({ ambientLight, blueLight });

const initialViewState = {
  longitude: -122.3337,
  latitude: 47.6074,
  zoom: 15.3,
  pitch: 55,
  bearing: -18,
};

const emptyFeatureCollection: FeatureCollection<Geometry> = {
  type: "FeatureCollection",
  features: [],
};

type Bounds = {
  west: number;
  south: number;
  east: number;
  north: number;
};

type BuildingRegion = {
  id: string;
  label: string;
  url: string;
  bounds: Bounds;
};

const BUILDING_REGIONS: BuildingRegion[] = [
  {
    id: "downtown-core",
    label: "Downtown Core",
    url: "/seattle/seattle-buildings-downtown-core.geojson",
    bounds: {
      west: -122.37,
      south: 47.585,
      east: -122.308,
      north: 47.6325,
    },
  },
  {
    id: "east-neighborhoods",
    label: "East Neighborhoods",
    url: "/seattle/seattle-buildings-east-neighborhoods.geojson",
    bounds: {
      west: -122.32,
      south: 47.585,
      east: -122.255,
      north: 47.676,
    },
  },
  {
    id: "northwest-seattle",
    label: "Northwest Seattle",
    url: "/seattle/seattle-buildings-northwest-seattle.geojson",
    bounds: {
      west: -122.43,
      south: 47.6205,
      east: -122.322,
      north: 47.69,
    },
  },
  {
    id: "west-seattle",
    label: "West Seattle",
    url: "/seattle/seattle-buildings-west-seattle.geojson",
    bounds: {
      west: -122.432,
      south: 47.543,
      east: -122.34,
      north: 47.604,
    },
  },
  {
    id: "beacon-hill",
    label: "Beacon Hill",
    url: "/seattle/seattle-buildings-beacon-hill.geojson",
    bounds: {
      west: -122.3365,
      south: 47.55,
      east: -122.284,
      north: 47.6015,
    },
  },
];

const REGION_LOAD_ORDER = [
  "downtown-core",
  "east-neighborhoods",
  "northwest-seattle",
  "beacon-hill",
  "west-seattle",
] as const;

const REGION_PRIORITY = new globalThis.Map<string, number>(
  REGION_LOAD_ORDER.map(
    (regionId, index) => [regionId, index] as [string, number],
  ),
);

const buildingFillLayer: LayerProps = {
  id: "official-seattle-buildings-fill",
  type: "fill-extrusion",
  paint: {
    "fill-extrusion-color": [
      "interpolate",
      ["linear"],
      ["get", "height_m"],
      3,
      "#cfe6ff",
      15,
      "#b7d8fb",
      40,
      "#97c5f6",
      120,
      "#79afe6",
    ],
    "fill-extrusion-height": ["coalesce", ["get", "height_m"], 0],
    "fill-extrusion-base": 0,
    "fill-extrusion-opacity": 0.85,
    "fill-extrusion-vertical-gradient": true,
  },
};

const BASEMAP_WATER_COLOR = "#a9d8f3";
const BASEMAP_WATER_SHADOW_COLOR = "#8fc5e4";
const BASEMAP_WATERWAY_COLOR = "#87c7ea";
const BASEMAP_GREENERY_COLOR = "#cfe7bf";

type BasemapLayer = {
  id: string;
  type: string;
  "source-layer"?: string;
};

function applyBasemapPalette(map: MapLibreMap) {
  const style = map.getStyle();
  const layers = (style?.layers ?? []) as BasemapLayer[];

  for (const layer of layers) {
    if (layer.type === "fill") {
      if (layer.id === "water") {
        map.setPaintProperty(layer.id, "fill-color", BASEMAP_WATER_COLOR);
        continue;
      }

      if (layer.id === "water_shadow") {
        map.setPaintProperty(
          layer.id,
          "fill-color",
          BASEMAP_WATER_SHADOW_COLOR,
        );
        continue;
      }

      if (layer.id === "landcover" || layer["source-layer"] === "park") {
        map.setPaintProperty(layer.id, "fill-color", BASEMAP_GREENERY_COLOR);
      }

      continue;
    }

    if (layer.type === "line" && layer["source-layer"] === "waterway") {
      map.setPaintProperty(layer.id, "line-color", BASEMAP_WATERWAY_COLOR);
    }
  }
}

async function fetchRegionBuildings(
  region: BuildingRegion,
  signal: AbortSignal,
) {
  const response = await fetch(region.url, { signal });
  if (!response.ok) {
    throw new Error(`${region.label} failed with ${response.status}`);
  }
  return response.json() as Promise<FeatureCollection<Geometry>>;
}

function expandBounds(bounds: Bounds) {
  const lonPad = (bounds.east - bounds.west) * 0.2;
  const latPad = (bounds.north - bounds.south) * 0.2;

  return {
    west: bounds.west - lonPad,
    south: bounds.south - latPad,
    east: bounds.east + lonPad,
    north: bounds.north + latPad,
  };
}

function boundsFromMap(map: MapRef) {
  const bounds = map.getBounds();
  return expandBounds({
    west: bounds.getWest(),
    south: bounds.getSouth(),
    east: bounds.getEast(),
    north: bounds.getNorth(),
  });
}

function containsBounds(outer: Bounds, inner: Bounds) {
  return (
    inner.west >= outer.west &&
    inner.south >= outer.south &&
    inner.east <= outer.east &&
    inner.north <= outer.north
  );
}

function intersectsBounds(a: Bounds, b: Bounds) {
  return !(
    a.east < b.west ||
    a.west > b.east ||
    a.north < b.south ||
    a.south > b.north
  );
}

function mergeFeatureCollections(collections: FeatureCollection<Geometry>[]) {
  return {
    type: "FeatureCollection",
    features: collections.flatMap((collection) => collection.features),
  } satisfies FeatureCollection<Geometry>;
}

function sortRegionIds(regionIds: string[]) {
  return [...new Set(regionIds)].sort(
    (left, right) =>
      (REGION_PRIORITY.get(left) ?? Number.MAX_SAFE_INTEGER) -
      (REGION_PRIORITY.get(right) ?? Number.MAX_SAFE_INTEGER),
  );
}

const initialBounds = {
  west: -122.3525,
  south: 47.6015,
  east: -122.3245,
  north: 47.6195,
};

function App() {
  const {
    geojson: heatmapData,
    setScenario,
    playback,
    setPlaying,
    seekTo,
    addPeople,
    diagnostics: heatmapDiagnostics,
  } = useHeatmap();
  const showHeatmapDebug = useMemo(
    () => new URLSearchParams(window.location.search).has("debugHeatmap"),
    [],
  );

  // Deploy state: index of the highest deployed step (0 = Line 1 only)
  const [deployedIndex, setDeployedIndex] = useState(0);

  // Building state
  const [regionCollections, setRegionCollections] = useState<
    Record<string, FeatureCollection<Geometry>>
  >({});
  const [buildings, setBuildings] = useState<FeatureCollection<Geometry>>(
    emptyFeatureCollection,
  );
  const [queryBounds, setQueryBounds] = useState(initialBounds);
  const [queuedRegionIds, setQueuedRegionIds] = useState<string[]>(
    REGION_LOAD_ORDER.slice(1) as unknown as string[],
  );
  const [activeRegionId, setActiveRegionId] = useState<string | null>(
    REGION_LOAD_ORDER[0],
  );
  const [regionErrors, setRegionErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    setScenario(DEPLOY_STEPS[deployedIndex].id).catch((err) => {
      console.warn("[heatmap] failed to set scenario", err);
    });
  }, [deployedIndex, setScenario]);

  // Time controls
  const [timeOfDay, setTimeOfDay] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [dayOfWeek, setDayOfWeek] = useState(0);
  const [hoveredDay, setHoveredDay] = useState<number | null>(null);
  const hoverTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dialRef = useRef<HTMLDivElement>(null);
  const selectedTimeRef = useRef(timeOfDay);
  const selectedDayRef = useRef(dayOfWeek);
  const [isDraggingDial, setIsDraggingDial] = useState(false);
  const mapRef = useRef<MapRef | null>(null);
  const [offscreenArrow, setOffscreenArrow] = useState<{
    x: number;
    y: number;
    angle: number;
  } | null>(null);

  // View mode controls
  const [viewMode, setViewMode] = useState<"top-down" | "angled">("angled");
  const [isPitchLocked, setIsPitchLocked] = useState(false);
  const isAnimatingView = useRef(false);

  // Crowd drop controls
  const [crowdSize, setCrowdSize] = useState<number>(5000);
  const [isDraggingCrowd, setIsDraggingCrowd] = useState(false);
  const [crowdDragPos, setCrowdDragPos] = useState<{
    x: number;
    y: number;
  } | null>(null);

  const backendIsPlaying = playback?.is_playing ?? isPlaying;
  const interpolatedMinuteOfWeek = useInterpolatedMinuteOfWeek(playback);

  useEffect(() => {
    if (!playback?.sim_time || isDraggingDial) return;
    setDayOfWeek(playback.sim_time.day_of_week);
    setTimeOfDay(playback.sim_time.time_bin);
  }, [playback?.sim_time, isDraggingDial]);

  useEffect(() => {
    if (playback) {
      setIsPlaying(playback.is_playing);
    }
  }, [playback]);

  useEffect(() => {
    selectedTimeRef.current = timeOfDay;
  }, [timeOfDay]);

  useEffect(() => {
    selectedDayRef.current = dayOfWeek;
  }, [dayOfWeek]);

  // ── Compute active stops/lines from deployed steps ──
  const { activeStops, activeLines } = useMemo(() => {
    const stops: TransitStop[] = [];
    const lines: TransitLine[] = [];
    const seenStopIds = new Set<string>();

    for (let i = 0; i <= deployedIndex; i++) {
      const step = DEPLOY_STEPS[i];
      lines.push(step.line);
      for (const stop of step.newStops) {
        if (!seenStopIds.has(stop.id)) {
          seenStopIds.add(stop.id);
          stops.push(stop);
        }
      }
    }
    return { activeStops: stops, activeLines: lines };
  }, [deployedIndex]);

  const stopsGeoJSON = useMemo(
    () => stopsToGeoJSON(activeStops, activeLines),
    [activeStops, activeLines],
  );
  const linesGeoJSON = useMemo(
    () => linesToGeoJSON(activeLines, activeStops),
    [activeLines, activeStops],
  );
  const trainInstances = useMemo(
    () => synthesizeTrainInstances(activeLines, interpolatedMinuteOfWeek),
    [activeLines, interpolatedMinuteOfWeek],
  );
  const deckLayers = useMemo(
    () => [
      new ScenegraphLayer({
        id: "space-needle-3d-v5",
        data: [{ position: [-122.3493, 47.6205] }],
        scenegraph: "/seattle/SPACE NEEDLE.glb",
        getPosition: (d: { position: [number, number] }) => d.position,
        getOrientation: [0, 0, 90],
        getScale: [1, 1, 1],
        sizeScale: 1.2,
        opacity: 0.6,
        _lighting: "pbr",
        parameters: {
          depthTest: true,
        },
      }),
      new ScatterplotLayer({
        id: "train-glow-layer",
        beforeId: "transit-stops-labels",
        data: trainInstances,
        pickable: false,
        radiusUnits: "pixels",
        radiusMinPixels: 10,
        radiusMaxPixels: 10,
        stroked: false,
        filled: true,
        getPosition: (train: TrainInstance) => train.coordinates,
        getFillColor: (train: TrainInstance) =>
          [train.color[0], train.color[1], train.color[2], 70] as [
            number,
            number,
            number,
            number,
          ],
        parameters: {
          depthTest: false,
        },
      }),
      new ScatterplotLayer({
        id: "train-head-layer",
        beforeId: "transit-stops-labels",
        data: trainInstances,
        pickable: false,
        radiusUnits: "pixels",
        radiusMinPixels: 5,
        radiusMaxPixels: 5,
        stroked: true,
        lineWidthUnits: "pixels",
        lineWidthMinPixels: 2,
        filled: true,
        getPosition: (train: TrainInstance) => train.coordinates,
        getFillColor: (train: TrainInstance) => train.color,
        getLineColor: [245, 250, 255, 220],
        parameters: {
          depthTest: false,
        },
      }),
    ],
    [trainInstances],
  );

  // ── Building loading effects ──
  useEffect(() => {
    setBuildings(mergeFeatureCollections(Object.values(regionCollections)));
  }, [regionCollections]);

  useEffect(() => {
    const loadedRegionIds = Object.keys(regionCollections);
    const inViewRegionIds = BUILDING_REGIONS.filter((region) =>
      intersectsBounds(region.bounds, queryBounds),
    ).map((region) => region.id);
    const pendingRegionIds = sortRegionIds([
      ...inViewRegionIds,
      ...REGION_LOAD_ORDER,
    ]).filter(
      (regionId) =>
        !loadedRegionIds.includes(regionId) &&
        regionId !== activeRegionId &&
        !regionErrors[regionId],
    );

    setQueuedRegionIds((current) =>
      sortRegionIds([
        ...current.filter(
          (regionId) =>
            !loadedRegionIds.includes(regionId) &&
            regionId !== activeRegionId &&
            !regionErrors[regionId],
        ),
        ...pendingRegionIds,
      ]),
    );
  }, [activeRegionId, queryBounds, regionCollections, regionErrors]);

  useEffect(() => {
    if (activeRegionId !== null || queuedRegionIds.length === 0) {
      return;
    }

    setActiveRegionId(queuedRegionIds[0]);
    setQueuedRegionIds((current) => current.slice(1));
  }, [activeRegionId, queuedRegionIds]);

  useEffect(() => {
    if (activeRegionId === null) {
      return undefined;
    }

    const region = BUILDING_REGIONS.find(
      (candidate) => candidate.id === activeRegionId,
    );
    if (!region) {
      setActiveRegionId(null);
      return undefined;
    }

    const controller = new AbortController();
    setRegionErrors((current) => {
      const next = { ...current };
      delete next[activeRegionId];
      return next;
    });

    void fetchRegionBuildings(region, controller.signal)
      .then((featureCollection) => {
        setRegionCollections((current) => ({
          ...current,
          [region.id]: featureCollection,
        }));
        setRegionErrors((current) => {
          const next = { ...current };
          delete next[region.id];
          return next;
        });
      })
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) {
          console.error(error);
          setRegionErrors((current) => ({
            ...current,
            [region.id]: "Error",
          }));
        }
      })
      .finally(() => {
        setActiveRegionId((current) =>
          current === region.id ? null : current,
        );
      });

    return () => {
      controller.abort();
    };
  }, [activeRegionId, queryBounds]);

  function updateBuildingsForViewport(map: MapRef) {
    const nextBounds = boundsFromMap(map);
    setQueryBounds((currentBounds) =>
      containsBounds(currentBounds, nextBounds) ? currentBounds : nextBounds,
    );
  }

  const nextStep =
    deployedIndex < DEPLOY_STEPS.length - 1
      ? DEPLOY_STEPS[deployedIndex + 1]
      : null;

  // Resolve trigger coordinates: use custom coords if provided, else fall back to station
  const triggerCoords = useMemo<[number, number] | null>(() => {
    if (!nextStep) return null;
    if (nextStep.triggerCoordinates) return nextStep.triggerCoordinates;
    if (nextStep.triggerStopId) {
      const stop = activeStops.find((s) => s.id === nextStep.triggerStopId);
      return stop?.coordinates ?? null;
    }
    return null;
  }, [nextStep, activeStops]);

  // Trigger GeoJSON for the pulsing indicator
  const triggerGeoJSON = useMemo<FeatureCollection<Point>>(() => {
    if (!triggerCoords) return { type: "FeatureCollection", features: [] };
    return {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          geometry: { type: "Point", coordinates: triggerCoords },
          properties: {
            id: nextStep?.triggerStopId ?? "deploy",
            name: nextStep?.label ?? "",
          },
        },
      ],
    };
  }, [triggerCoords, nextStep]);

  // ── Off-screen arrow: track whether the deploy node is visible ──
  const updateOffscreenArrow = useCallback(() => {
    if (!mapRef.current || !triggerCoords) {
      setOffscreenArrow(null);
      return;
    }
    const map = mapRef.current;
    const projected = map.project(triggerCoords as [number, number]);
    const container = map.getContainer();
    const w = container.clientWidth;
    const h = container.clientHeight;
    const MARGIN = 60;

    const isWithinBounds = map
      .getBounds()
      .contains(triggerCoords as [number, number]);

    // If on screen mathematically and geographically, no arrow needed
    if (
      isWithinBounds &&
      projected.x >= MARGIN &&
      projected.x <= w - MARGIN &&
      projected.y >= MARGIN &&
      projected.y <= h - MARGIN
    ) {
      setOffscreenArrow(null);
      return;
    }

    const cx = w / 2;
    const cy = h / 2;

    // Calculate geographic bearing to prevent projection inversion behind camera
    const center = map.getCenter();
    const dLon = triggerCoords[0] - center.lng;
    const dLat = triggerCoords[1] - center.lat;
    const dxGeo = dLon * Math.cos((center.lat * Math.PI) / 180);
    const dyGeo = dLat;

    // Geographical angle where North is 0, East is 90
    const geoBearing = (Math.atan2(dxGeo, dyGeo) * 180) / Math.PI;

    // Screen angle: map bearing offsets geographical bearing.
    // Screen X/Y: 0 degrees is right, 90 is down.
    const screenAngleDeg = geoBearing - map.getBearing() - 90;
    const angle = (screenAngleDeg * Math.PI) / 180;

    const PAD = 48;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);

    // Intersect ray from center with padded screen edges
    const tRight = cos > 0 ? (w / 2 - PAD) / cos : Infinity;
    const tLeft = cos < 0 ? -(w / 2 - PAD) / cos : Infinity;
    const tBottom = sin > 0 ? (h / 2 - PAD) / sin : Infinity;
    const tTop = sin < 0 ? -(h / 2 - PAD) / sin : Infinity;

    const t = Math.min(tRight, tLeft, tBottom, tTop);

    const edgeX = cx + t * cos;
    const edgeY = cy + t * sin;

    setOffscreenArrow({ x: edgeX, y: edgeY, angle: screenAngleDeg });
  }, [triggerCoords]);

  useEffect(() => {
    updateOffscreenArrow();
  }, [updateOffscreenArrow]);

  const handleMapMove = useCallback(
    (e: ViewStateChangeEvent) => {
      updateOffscreenArrow();
      const p = e.viewState.pitch;

      if (viewMode === "angled" && p < 5 && !isAnimatingView.current) {
        setViewMode("top-down");
        mapRef.current?.easeTo({ pitch: 0, duration: 800 });
      } else if (viewMode === "top-down" && p < 0.5 && !isPitchLocked) {
        setIsPitchLocked(true);
      }
    },
    [updateOffscreenArrow, viewMode, isPitchLocked],
  );

  const handleToggleView = () => {
    if (viewMode === "angled") {
      setViewMode("top-down");
      mapRef.current?.easeTo({ pitch: 0, duration: 1000 });
    } else {
      setIsPitchLocked(false);
      setViewMode("angled");
      isAnimatingView.current = true;
      setTimeout(() => {
        mapRef.current?.easeTo({ pitch: 55, duration: 1000 });
        setTimeout(() => {
          isAnimatingView.current = false;
        }, 1100);
      }, 50);
    }
  };

  // ── Map click handler — deploy on trigger click ──
  const handleMapClick = useCallback(
    (e: MapLayerMouseEvent) => {
      if (!nextStep) return;
      const features = e.features;
      if (features && features.length > 0) {
        setDeployedIndex((prev) => prev + 1);
      }
    },
    [nextStep],
  );

  // ── Time controls ──
  const updateTimeFromPointer = (clientX: number, clientY: number) => {
    if (!dialRef.current) return timeOfDay;
    const rect = dialRef.current.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const angle = Math.atan2(clientY - cy, clientX - cx);
    let shiftedAngle = angle + Math.PI / 2;
    if (shiftedAngle < 0) shiftedAngle += 2 * Math.PI;
    let newTime = Math.round((shiftedAngle / (2 * Math.PI)) * 1440);
    newTime = Math.round(newTime / 30) * 30;
    if (newTime >= 1440) newTime = 0;
    selectedTimeRef.current = newTime;
    setTimeOfDay(newTime);
    return newTime;
  };

  useEffect(() => {
    const handlePointerMove = (e: PointerEvent) => {
      if (!isDraggingDial) return;
      updateTimeFromPointer(e.clientX, e.clientY);
    };

    const handlePointerUp = () => {
      setIsDraggingDial(false);
      seekTo(selectedDayRef.current, selectedTimeRef.current).catch((err) => {
        console.warn("[heatmap] failed to seek playback", err);
      });
    };

    if (isDraggingDial) {
      window.addEventListener("pointermove", handlePointerMove);
      window.addEventListener("pointerup", handlePointerUp);
    }
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [isDraggingDial]);

  // ── Crowd Drop Pointer Events ──
  useEffect(() => {
    if (!isDraggingCrowd) return;

    const handlePointerMove = (e: PointerEvent) => {
      setCrowdDragPos({ x: e.clientX, y: e.clientY });
    };

    const handlePointerUp = (e: PointerEvent) => {
      setIsDraggingCrowd(false);
      setCrowdDragPos(null);

      if (mapRef.current) {
        const dropPoint = [e.clientX, e.clientY] as [number, number];
        const lngLat = mapRef.current.unproject(dropPoint);

        // Scatter the crowd into ~12 distinct clusters within a ~150m radius
        // 0.0015 degrees lat/lon is roughly 150m.
        const drops = 12;
        const peoplePerDrop = Math.floor(crowdSize / drops);

        for (let i = 0; i < drops; i++) {
          const r = Math.random() * 0.0015;
          const theta = Math.random() * 2 * Math.PI;
          const lat = lngLat.lat + r * Math.cos(theta);
          const lon = lngLat.lng + r * Math.sin(theta);

          addPeople(lat, lon, peoplePerDrop).catch(console.error);
        }
      }
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);

    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
    };
  }, [isDraggingCrowd, crowdSize, addPeople]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.code === "Space") {
        e.preventDefault();
        setPlaying(!backendIsPlaying).catch((err) => {
          console.warn("[heatmap] failed to update playback", err);
        });
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [backendIsPlaying, setPlaying]);

  const formatTime = (minutes: number) => {
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    const period = hours >= 12 ? "PM" : "AM";
    const displayHours = hours % 12 || 12;
    return `${displayHours}:${mins.toString().padStart(2, "0")} ${period}`;
  };

  const dialRadius = 60;
  const thumbAngle = (timeOfDay / 1440) * 2 * Math.PI - Math.PI / 2;
  const thumbX = dialRadius * Math.cos(thumbAngle);
  const thumbY = dialRadius * Math.sin(thumbAngle);

  // Dynamic angle calculation for weekday orbit nodes
  const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];
  const focusIndex = hoveredDay !== null ? hoveredDay : dayOfWeek;
  const ACTIVE_WIDTH = 4.5;
  const INACTIVE_WIDTH = 1;
  const TOTAL_SPAN = 130;
  const START_ANGLE = 160;

  const nodeWidths = WEEKDAYS.map((_, i) =>
    i === focusIndex ? ACTIVE_WIDTH : INACTIVE_WIDTH,
  );
  const gaps = [];
  for (let i = 0; i < 6; i++) {
    gaps.push((nodeWidths[i] + nodeWidths[i + 1]) / 2);
  }
  const totalGap = gaps.reduce((a, b) => a + b, 0);

  const nodeAngles = [START_ANGLE];
  let currentGapSum = 0;
  for (let i = 0; i < 6; i++) {
    currentGapSum += gaps[i];
    nodeAngles.push(START_ANGLE + (currentGapSum / totalGap) * TOTAL_SPAN);
  }

  // Interactive layer IDs for click detection
  const interactiveLayerIds = useMemo(
    () => [
      "transit-stops-circle",
      "transit-stops-dot",
      "deploy-pulse-ring",
      "deploy-glow-dot",
    ],
    [],
  );

  const handleMapLoad = useCallback(
    (event: { target: MapLibreMap }) => {
      applyBasemapPalette(event.target);

      if (mapRef.current) {
        updateBuildingsForViewport(mapRef.current);
      }
    },
    [],
  );

  return (
    <div className="map-shell">
      <Map
        ref={mapRef}
        initialViewState={initialViewState}
        mapStyle="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
        style={{ width: "100vw", height: "100vh" }}
        onClick={handleMapClick}
        onMove={handleMapMove}
        onLoad={handleMapLoad}
        onMoveEnd={(event) =>
          updateBuildingsForViewport(event.target as unknown as MapRef)
        }
        interactiveLayerIds={interactiveLayerIds}
        cursor={nextStep ? "pointer" : undefined}
        maxPitch={isPitchLocked ? 0 : 85}
      >
        <NavigationControl position="top-right" />

        <button
          className="view-mode-button"
          onClick={handleToggleView}
          title={
            viewMode === "angled"
              ? "Switch to Top-Down View"
              : "Switch to Angled View"
          }
        >
          {viewMode === "angled" ? "2D" : "3D"}
        </button>

        <div className="crowd-drop-control">
          <button
            className="crowd-draggable-btn"
            onPointerDown={(e) => {
              setIsDraggingCrowd(true);
              setCrowdDragPos({ x: e.clientX, y: e.clientY });
            }}
            title="Drag to spawn crowd"
          >
            <Mascot isDragging={isDraggingCrowd} className="crowd-icon" />
          </button>
          <div className="crowd-slider-container">
            <input
              type="range"
              min="1000"
              max="30000"
              step="1000"
              value={crowdSize}
              onChange={(e) => setCrowdSize(parseInt(e.target.value))}
              className="crowd-slider"
            />
            <span className="crowd-size-label">{crowdSize / 1000}k</span>
          </div>
        </div>

        <Source id="official-seattle-buildings" type="geojson" data={buildings}>
          <Layer beforeId="watername_ocean" {...buildingFillLayer} />
        </Source>

        <DeckGLOverlay
          interleaved={true}
          effects={[lightingEffect]}
          layers={deckLayers}
        />

        <Source id="heatmap-source" type="geojson" data={heatmapData}>
          <Layer {...heatmapLayer} />
        </Source>

        <Source id="transit-lines" type="geojson" data={linesGeoJSON}>
          <Layer {...lineCasingLayer} />
          <Layer {...lineRouteLayer} />
        </Source>

        <Source id="transit-stops" type="geojson" data={stopsGeoJSON}>
          <Layer {...stopCircleLayer} />
          <Layer {...stopDotLayer} />
          <Layer {...stopLabelLayer} />
        </Source>

        {/* Deploy indicator — pulsing ring on the next trigger station */}
        <Source id="deploy-trigger" type="geojson" data={triggerGeoJSON}>
          <Layer {...deployPulseRingLayer} />
          <Layer {...deployGlowDotLayer} />
        </Source>

        {/* Tooltip marker above the trigger node */}
        {triggerCoords && nextStep?.label && (
          <Marker
            longitude={triggerCoords[0]}
            latitude={triggerCoords[1]}
            anchor="bottom"
            offset={[0, -30]}
          >
            <div className="deploy-tooltip">
              <span className="deploy-tooltip-icon">⚡</span>
              <span>{nextStep.label}</span>
            </div>
          </Marker>
        )}
      </Map>

      {/* Off-screen arrow pointing toward the deploy node */}
      {offscreenArrow && nextStep?.label && (
        <div
          className="offscreen-arrow"
          style={{
            left: `${offscreenArrow.x}px`,
            top: `${offscreenArrow.y}px`,
            transform: `translate(-50%, -50%) rotate(${offscreenArrow.angle}deg)`,
          }}
        >
          <span className="offscreen-arrow-label">{nextStep.label}</span>
          <span className="offscreen-arrow-chevron">›</span>
        </div>
      )}

      {/* Dragging Reticle */}
      {isDraggingCrowd && crowdDragPos && (
        <div
          className="crowd-drop-reticle"
          style={{
            left: `${crowdDragPos.x}px`,
            top: `${crowdDragPos.y}px`,
          }}
        >
          <div
            style={{
              width: 80,
              height: 80,
              marginBottom: 8,
              pointerEvents: "none",
            }}
          >
            <Mascot isDragging={true} />
          </div>
          <div className="reticle-label">Drop {crowdSize / 1000}k</div>
        </div>
      )}

      {showHeatmapDebug && (
        <div className="heatmap-debug-panel">
          <strong>Heatmap Debug</strong>
          <span>connection: {heatmapDiagnostics.connection}</span>
          <span>pending: {heatmapDiagnostics.pendingScenarioId ?? "none"}</span>
          <span>
            confirmed: {heatmapDiagnostics.confirmedScenarioId ?? "none"}
          </span>
          <span>frames: {heatmapDiagnostics.frameCount}</span>
          <span>playing: {backendIsPlaying ? "yes" : "no"}</span>
          <span>
            sim:{" "}
            {heatmapDiagnostics.simTime
              ? `d${heatmapDiagnostics.simTime.day_of_week} ${formatTime(heatmapDiagnostics.simTime.time_bin)}`
              : "none"}
          </span>
          <span>features: {heatmapDiagnostics.featureCount}</span>
          <span>cells: {heatmapDiagnostics.lastFrameCellCount}</span>
          <span>
            grid:{" "}
            {heatmapDiagnostics.config
              ? `${heatmapDiagnostics.config.rows}x${heatmapDiagnostics.config.cols}`
              : "none"}
          </span>
          {heatmapDiagnostics.lastError && (
            <span>{heatmapDiagnostics.lastError}</span>
          )}
        </div>
      )}

      <div className="time-controls-wrapper">
        {WEEKDAYS.map((dayLabel, index) => {
          const angleDeg = nodeAngles[index];
          const angleRad = (angleDeg * Math.PI) / 180;
          const isFocused = focusIndex === index;
          const R = isFocused ? 122 : 110;
          const x = Math.cos(angleRad) * R;
          const y = Math.sin(angleRad) * R;

          return (
            <button
              key={index}
              className={`orbit-node ${dayOfWeek === index ? "is-selected" : ""} ${focusIndex === index ? "is-focused" : ""}`}
              style={
                { "--x": `${x}px`, "--y": `${y}px` } as React.CSSProperties
              }
              onClick={() => {
                selectedDayRef.current = index;
                setDayOfWeek(index);
                seekTo(index, selectedTimeRef.current).catch((err) => {
                  console.warn("[heatmap] failed to seek playback day", err);
                });
              }}
              onMouseEnter={() => {
                if (hoverTimeoutRef.current)
                  clearTimeout(hoverTimeoutRef.current);
                setHoveredDay(index);
              }}
              onMouseLeave={() => {
                hoverTimeoutRef.current = setTimeout(() => {
                  setHoveredDay(null);
                }, 150);
              }}
              aria-label={`Select day ${index + 1}`}
            >
              {dayLabel}
            </button>
          );
        })}

        <div className="radial-dial-container">
          <div
            className="radial-dial"
            ref={dialRef}
            onPointerDown={(e) => {
              setIsDraggingDial(true);
              updateTimeFromPointer(e.clientX, e.clientY);
            }}
            style={{ touchAction: "none" }}
          >
            <svg viewBox="-75 -75 150 150" className="radial-dial-svg">
              <circle cx="0" cy="0" r={dialRadius} className="dial-track" />
              <line
                x1="0"
                y1={-dialRadius - 6}
                x2="0"
                y2={-dialRadius + 6}
                stroke="rgba(43, 77, 112, 0.4)"
                strokeWidth="2"
              />
              <line
                x1="0"
                y1={dialRadius - 6}
                x2="0"
                y2={dialRadius + 6}
                stroke="rgba(43, 77, 112, 0.4)"
                strokeWidth="2"
              />
              <line
                x1={-dialRadius - 6}
                y1="0"
                x2={-dialRadius + 6}
                y2="0"
                stroke="rgba(43, 77, 112, 0.4)"
                strokeWidth="2"
              />
              <line
                x1={dialRadius - 6}
                y1="0"
                x2={dialRadius + 6}
                y2="0"
                stroke="rgba(43, 77, 112, 0.4)"
                strokeWidth="2"
              />

              <circle cx={thumbX} cy={thumbY} r="8" className="dial-thumb" />
            </svg>
            <div className="dial-center-content">
              <span className="dial-time">{formatTime(timeOfDay)}</span>
              <button
                className="dial-play-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  setPlaying(!backendIsPlaying).catch((err) => {
                    console.warn("[heatmap] failed to update playback", err);
                  });
                }}
                aria-label={backendIsPlaying ? "Pause" : "Play"}
              >
                {backendIsPlaying ? "PAUSE" : "PLAY"}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
