import type { Feature, FeatureCollection, LineString, Point } from 'geojson';
import { BALLARD_TRACK, LINE_1_TRACK, LINE_2_TRACK, type LonLat } from './track_geometry';

export type TransitStop = {
  id: string;
  name: string;
  coordinates: [longitude: number, latitude: number];
};

export type TransitLine = {
  id: string;
  name: string;
  color: string;
  /** Perpendicular pixel offset for parallel-line rendering. */
  offset: number;
  /** Ordered stop IDs that define the route path. */
  stopIds: string[];
  /**
   * Real-world track geometry (ordered [lon, lat] points). When set, this is
   * used as the rendered polyline instead of straight stop-to-stop segments.
   */
  path?: LonLat[];
};

export type ExpansionMode = {
  id: string;
  name: string;
  description: string;
  stops: TransitStop[];
  lines: TransitLine[];
};

// ---------------------------------------------------------------------------
// Stops
// ---------------------------------------------------------------------------

/** Existing Link 1 Line stations, ordered north → south. */
export const LINE_1_STOPS: TransitStop[] = [
  { id: 'northgate',      name: 'Northgate',                 coordinates: [-122.3272, 47.6992] },
  { id: 'roosevelt',      name: 'Roosevelt',                 coordinates: [-122.3167, 47.6768] },
  { id: 'u-district',     name: 'U District',                coordinates: [-122.3155, 47.6614] },
  { id: 'uw',             name: 'UW',                        coordinates: [-122.3037, 47.6498] },
  { id: 'capitol-hill',   name: 'Capitol Hill',              coordinates: [-122.3209, 47.6190] },
  { id: 'westlake',       name: 'Westlake',                  coordinates: [-122.3371, 47.6113] },
  { id: 'symphony',       name: 'Symphony',                  coordinates: [-122.3361, 47.6074] },
  { id: 'pioneer-square', name: 'Pioneer Square',            coordinates: [-122.3314, 47.6021] },
  { id: 'id-chinatown',   name: 'Intl District / Chinatown', coordinates: [-122.3278, 47.5983] },
  { id: 'stadium',        name: 'Stadium',                   coordinates: [-122.3275, 47.5911] },
  { id: 'sodo',           name: 'SODO',                      coordinates: [-122.3271, 47.5807] },
  { id: 'beacon-hill',    name: 'Beacon Hill',               coordinates: [-122.3115, 47.5793] },
  { id: 'mount-baker',    name: 'Mount Baker',               coordinates: [-122.2975, 47.5764] },
  { id: 'columbia-city',  name: 'Columbia City',             coordinates: [-122.2922, 47.5599] },
  { id: 'othello',        name: 'Othello',                   coordinates: [-122.2812, 47.5383] },
  { id: 'rainier-beach',  name: 'Rainier Beach',             coordinates: [-122.2688, 47.5222] },
];

/** 2 Line — branches east from ID/Chinatown, across Lake Washington. */
export const LINE_2_STOPS: TransitStop[] = [
  { id: 'judkins-park',      name: 'Judkins Park',      coordinates: [-122.3043, 47.5907] },
  { id: 'mercer-island',     name: 'Mercer Island',     coordinates: [-122.2350, 47.5871] },
  { id: 'bellevue-downtown', name: 'Bellevue Downtown', coordinates: [-122.1960, 47.6155] },
];

/** Ballard extension — unique stations not on the 1 or 2 Line. */
export const BALLARD_STOPS: TransitStop[] = [
  { id: 'midtown',          name: 'Midtown',           coordinates: [-122.3322, 47.6088] },
  { id: 'denny',            name: 'Denny',             coordinates: [-122.3405, 47.6188] },
  { id: 'south-lake-union', name: 'South Lake Union',  coordinates: [-122.3377, 47.6258] },
  { id: 'seattle-center',   name: 'Seattle Center',    coordinates: [-122.3520, 47.6243] },
  { id: 'smith-cove',       name: 'Smith Cove',        coordinates: [-122.3635, 47.6378] },
  { id: 'interbay',         name: 'Interbay',          coordinates: [-122.3765, 47.6478] },
  { id: 'ballard',          name: 'Ballard',           coordinates: [-122.3765, 47.6677] },
];

// ---------------------------------------------------------------------------
// Lines
// ---------------------------------------------------------------------------

export const LINK_1_LINE: TransitLine = {
  id: 'link-1-line',
  name: '1 Line',
  color: '#0063c6',
  offset: -4,
  stopIds: [
    'northgate', 'roosevelt', 'u-district', 'uw', 'capitol-hill',
    'westlake', 'symphony', 'pioneer-square', 'id-chinatown',
    'stadium', 'sodo', 'beacon-hill', 'mount-baker',
    'columbia-city', 'othello', 'rainier-beach',
  ],
  path: LINE_1_TRACK,
};

/**
 * 2 Line — runs from Northgate through the shared downtown tunnel,
 * then branches east from ID/Chinatown across Lake Washington.
 * Overlaps with the 1 Line from Northgate to ID/Chinatown.
 */
export const LINK_2_LINE: TransitLine = {
  id: 'link-2-line',
  name: '2 Line',
  color: '#d63e2a',
  offset: 4,
  stopIds: [
    'northgate', 'roosevelt', 'u-district', 'uw', 'capitol-hill',
    'westlake', 'symphony', 'pioneer-square', 'id-chinatown',
    'judkins-park', 'mercer-island', 'bellevue-downtown',
  ],
  path: LINE_2_TRACK,
};

/**
 * Ballard Link Extension — runs from Ballard south through a NEW downtown
 * tunnel separate from the 1/2 Line tunnel. Trains stop at new BLE platforms
 * at Westlake, Midtown (new station), Chinatown/Intl District, and SODO;
 * Westlake, ID/Chinatown, and SODO are the only transfer points to the 1/2
 * Line. The BLE does not stop at Symphony, Pioneer Square, or Stadium.
 */
export const BALLARD_LINE: TransitLine = {
  id: 'ballard-line',
  name: 'Ballard Line',
  color: '#00875a',
  offset: 0,
  stopIds: [
    'ballard', 'interbay', 'smith-cove', 'seattle-center',
    'south-lake-union', 'denny', 'westlake', 'midtown',
    'id-chinatown', 'sodo',
  ],
  path: BALLARD_TRACK,
};

// ---------------------------------------------------------------------------
// Expansion modes (cumulative)
// ---------------------------------------------------------------------------

export const EXPANSION_MODES: ExpansionMode[] = [
  {
    id: 'line-1',
    name: '1 Line Only',
    description: "Today's Link 1 Line — Northgate to Rainier Beach",
    stops: [...LINE_1_STOPS],
    lines: [LINK_1_LINE],
  },
  {
    id: 'line-1-2',
    name: '+ 2 Line',
    description: 'Adds the 2 Line east branch — ID/Chinatown to Judkins Park and beyond',
    stops: [...LINE_1_STOPS, ...LINE_2_STOPS],
    lines: [LINK_1_LINE, LINK_2_LINE],
  },
  {
    id: 'line-1-2-ballard',
    name: '+ Ballard Extension',
    description: 'Adds the Ballard line — Westlake northwest to Ballard',
    stops: [...LINE_1_STOPS, ...LINE_2_STOPS, ...BALLARD_STOPS],
    lines: [LINK_1_LINE, LINK_2_LINE, BALLARD_LINE],
  },
];

// ---------------------------------------------------------------------------
// GeoJSON helpers
// ---------------------------------------------------------------------------

type RgbColor = [red: number, green: number, blue: number];
type HslColor = [hue: number, saturation: number, lightness: number];

function hexToRgb(hex: string): RgbColor {
  const normalized = hex.trim().replace('#', '');
  const expanded = normalized.length === 3
    ? normalized.split('').map((value) => value + value).join('')
    : normalized;

  if (!/^[0-9a-fA-F]{6}$/.test(expanded)) {
    throw new Error(`Unsupported color format: ${hex}`);
  }

  return [
    Number.parseInt(expanded.slice(0, 2), 16),
    Number.parseInt(expanded.slice(2, 4), 16),
    Number.parseInt(expanded.slice(4, 6), 16),
  ];
}

function rgbToHex([red, green, blue]: RgbColor): string {
  return `#${[red, green, blue]
    .map((value) => Math.max(0, Math.min(255, Math.round(value))).toString(16).padStart(2, '0'))
    .join('')}`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function rgbToHsl([red, green, blue]: RgbColor): HslColor {
  const r = red / 255;
  const g = green / 255;
  const b = blue / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const lightness = (max + min) / 2;
  const delta = max - min;

  if (delta === 0) {
    return [0, 0, lightness];
  }

  const saturation = lightness > 0.5
    ? delta / (2 - max - min)
    : delta / (max + min);

  let hue: number;
  switch (max) {
    case r:
      hue = ((g - b) / delta + (g < b ? 6 : 0)) * 60;
      break;
    case g:
      hue = ((b - r) / delta + 2) * 60;
      break;
    default:
      hue = ((r - g) / delta + 4) * 60;
      break;
  }

  return [hue, saturation, lightness];
}

function hslToRgb([hue, saturation, lightness]: HslColor): RgbColor {
  const normalizedHue = ((hue % 360) + 360) % 360;
  const chroma = (1 - Math.abs(2 * lightness - 1)) * saturation;
  const huePrime = normalizedHue / 60;
  const x = chroma * (1 - Math.abs((huePrime % 2) - 1));

  let rgb: [number, number, number];
  if (huePrime < 1) {
    rgb = [chroma, x, 0];
  } else if (huePrime < 2) {
    rgb = [x, chroma, 0];
  } else if (huePrime < 3) {
    rgb = [0, chroma, x];
  } else if (huePrime < 4) {
    rgb = [0, x, chroma];
  } else if (huePrime < 5) {
    rgb = [x, 0, chroma];
  } else {
    rgb = [chroma, 0, x];
  }

  const match = lightness - chroma / 2;
  return [
    (rgb[0] + match) * 255,
    (rgb[1] + match) * 255,
    (rgb[2] + match) * 255,
  ];
}

function blendHexColors(colors: string[]): string {
  if (colors.length === 0) {
    return '#1a73e8';
  }

  if (colors.length === 1) {
    return colors[0];
  }

  const hslColors = colors.map((color) => rgbToHsl(hexToRgb(color)));
  const [hueX, hueY, saturationTotal, lightnessTotal] = hslColors.reduce(
    (accumulator, [hue, saturation, lightness]) => [
      accumulator[0] + Math.cos((hue * Math.PI) / 180),
      accumulator[1] + Math.sin((hue * Math.PI) / 180),
      accumulator[2] + saturation,
      accumulator[3] + lightness,
    ],
    [0, 0, 0, 0],
  );

  const averageHue = hueX === 0 && hueY === 0
    ? hslColors[0][0]
    : ((Math.atan2(hueY, hueX) * 180) / Math.PI + 360) % 360;
  const averageSaturation = saturationTotal / colors.length;
  const averageLightness = lightnessTotal / colors.length;
  const tunedSaturation = clamp(
    Math.max(averageSaturation, 0.62) + (colors.length === 2 ? 0.18 : 0.12),
    0,
    1,
  );
  const targetLightness = colors.length === 2 ? 0.5 : 0.56;
  const tunedLightness = clamp((averageLightness + targetLightness) / 2, 0, 1);

  return rgbToHex(hslToRgb([averageHue, tunedSaturation, tunedLightness]));
}

function buildStopServiceMap(lines: TransitLine[]): Map<string, TransitLine[]> {
  const servicesByStopId = new Map<string, TransitLine[]>();

  for (const line of lines) {
    for (const stopId of line.stopIds) {
      const servedLines = servicesByStopId.get(stopId);
      if (servedLines) {
        servedLines.push(line);
      } else {
        servicesByStopId.set(stopId, [line]);
      }
    }
  }

  return servicesByStopId;
}

export function stopsToGeoJSON(
  stops: TransitStop[],
  lines: TransitLine[],
): FeatureCollection<Point> {
  const servicesByStopId = buildStopServiceMap(lines);

  return {
    type: 'FeatureCollection',
    features: stops.map((stop) => {
      const servedLines = servicesByStopId.get(stop.id) ?? [];
      const lineColors = servedLines.map((line) => line.color);

      return {
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: stop.coordinates },
        properties: {
          id: stop.id,
          name: stop.name,
          lineCount: servedLines.length,
          lineIds: servedLines.map((line) => line.id).join(','),
          markerColor: blendHexColors(lineColors),
        },
      };
    }),
  };
}

export function linesToGeoJSON(
  lines: TransitLine[],
  stops: TransitStop[],
): FeatureCollection<LineString> {
  const stopMap = new Map(stops.map((s) => [s.id, s]));

  const features: Feature<LineString>[] = lines.map((line) => {
    const coordinates: [number, number][] = line.path
      ? line.path
      : line.stopIds
          .map((sid) => stopMap.get(sid)?.coordinates)
          .filter((c): c is [number, number] => c !== undefined);

    return {
      type: 'Feature' as const,
      geometry: { type: 'LineString' as const, coordinates },
      properties: { id: line.id, name: line.name, color: line.color, offset: line.offset },
    };
  });

  return { type: 'FeatureCollection', features };
}

// ---------------------------------------------------------------------------
// Deploy steps — progressive unlock sequence
// ---------------------------------------------------------------------------

export type DeployStep = {
  id: string;
  /** The stop ID the user must click to deploy this line. null = auto-deployed on load. */
  triggerStopId: string | null;
  /** Custom coordinates for the deploy node (overrides triggerStopId position). */
  triggerCoordinates?: [longitude: number, latitude: number];
  line: TransitLine;
  newStops: TransitStop[];
  /** Label shown on the deploy tooltip. */
  label?: string;
};

export const DEPLOY_STEPS: DeployStep[] = [
  { id: 'line-1', triggerStopId: null, line: LINK_1_LINE, newStops: LINE_1_STOPS },
  { id: 'line-1-2', triggerStopId: 'id-chinatown', triggerCoordinates: [-122.285, 47.589], line: LINK_2_LINE, newStops: LINE_2_STOPS, label: 'Deploy 2 Line' },
  { id: 'line-1-2-ballard', triggerStopId: 'westlake', triggerCoordinates: [-122.3765, 47.6700], line: BALLARD_LINE, newStops: BALLARD_STOPS, label: 'Deploy Ballard Extension' },
];
