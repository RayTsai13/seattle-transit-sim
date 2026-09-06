import { useEffect, useRef, useState } from 'react';
import type { PlaybackState } from '../heatmap/api.ts';
import type { TransitLine } from './data.ts';
import type { LonLat } from './track_geometry.ts';
import {
  TRAIN_SERVICE_ARTIFACT,
  type TrainDayType,
  type TrainDirectionProfile,
  type TrainLineProfile,
  type TrainServiceWindow,
} from './train_service.ts';

export type TrainInstance = {
  id: string;
  lineId: string;
  directionId: 0 | 1;
  coordinates: LonLat;
  color: [number, number, number];
};

const MINUTES_PER_DAY = 24 * 60;
const MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY;
const DEFAULT_DWELL_MINUTES = TRAIN_SERVICE_ARTIFACT.metadata.defaultDwellSeconds / 60;
const TERMINAL_LOOKBACK_BUFFER_MINUTES = 2;
const TRAIN_OFFSET_METERS_PER_OFFSET_UNIT = 3;
// Fallback only, for the moments before the first playback event arrives.
// The real rate comes from `playback.sim_minutes_per_second`; hardcoding it
// here made trains crawl at 1/30 of the clock the rest of the UI displays.
const FALLBACK_VISUAL_MINUTES_PER_SECOND = 30;
const PLAYBACK_RESYNC_THRESHOLD_MINUTES = 30;

const profileIndex = new Map<string, TrainLineProfile>(
  TRAIN_SERVICE_ARTIFACT.lineProfiles.map((profile) => [
    `${profile.lineId}:${profile.dayType}`,
    profile,
  ]),
);

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function wrapMinuteOfWeek(value: number) {
  return ((value % MINUTES_PER_WEEK) + MINUTES_PER_WEEK) % MINUTES_PER_WEEK;
}

function wrappedMinuteDelta(previous: number, next: number) {
  return (next - previous + MINUTES_PER_WEEK) % MINUTES_PER_WEEK;
}

function minuteOfWeekToDayType(minuteOfWeek: number): TrainDayType {
  const dayIndex = Math.floor(wrapMinuteOfWeek(minuteOfWeek) / MINUTES_PER_DAY);
  if (dayIndex === 0) return 'sunday';
  if (dayIndex === 6) return 'saturday';
  return 'weekday';
}

function getLineProfile(lineId: string, dayType: TrainDayType) {
  return profileIndex.get(`${lineId}:${dayType}`) ?? null;
}

function getDirectionProfile(
  lineId: string,
  dayType: TrainDayType,
  directionId: 0 | 1,
) {
  const lineProfile = getLineProfile(lineId, dayType);
  if (!lineProfile) return null;
  return lineProfile.directionProfiles.find(
    (profile) => profile.directionId === directionId,
  ) ?? null;
}

const rgbCache = new Map<string, [number, number, number]>();

/** Parsed once per distinct line color, not once per train per frame. */
function hexToRgb(hex: string): [number, number, number] {
  const cached = rgbCache.get(hex);
  if (cached) return cached;
  const normalized = hex.replace('#', '');
  const rgb: [number, number, number] = [
    Number.parseInt(normalized.slice(0, 2), 16),
    Number.parseInt(normalized.slice(2, 4), 16),
    Number.parseInt(normalized.slice(4, 6), 16),
  ];
  rgbCache.set(hex, rgb);
  return rgb;
}

function totalVisibleRuntimeMinutes(directionProfile: TrainDirectionProfile) {
  return (
    directionProfile.tripRuntimeMinutes +
    DEFAULT_DWELL_MINUTES * Math.max(0, directionProfile.stopIds.length - 2)
  );
}

function distanceForElapsedMinutes(
  directionProfile: TrainDirectionProfile,
  elapsedMinutes: number,
) {
  const stopDistances = directionProfile.stopDistanceMeters;
  const segmentRuntimes = directionProfile.segmentRuntimeMinutes;
  let remaining = elapsedMinutes;

  for (let index = 0; index < segmentRuntimes.length; index += 1) {
    const travelMinutes = segmentRuntimes[index];
    if (remaining <= travelMinutes) {
      const startDistance = stopDistances[index];
      const endDistance = stopDistances[index + 1];
      const ratio = travelMinutes === 0 ? 0 : remaining / travelMinutes;
      return startDistance + (endDistance - startDistance) * ratio;
    }
    remaining -= travelMinutes;

    if (index < segmentRuntimes.length - 1) {
      if (remaining <= DEFAULT_DWELL_MINUTES) {
        return stopDistances[index + 1];
      }
      remaining -= DEFAULT_DWELL_MINUTES;
    }
  }

  return stopDistances[stopDistances.length - 1];
}

function samplePathPosition(
  path: LonLat[],
  cumulativeMeters: number[],
  targetDistanceMeters: number,
) {
  if (path.length === 0) {
    return {
      coordinate: [0, 0] as LonLat,
      tangent: [1, 0] as [number, number],
    };
  }
  if (path.length === 1 || cumulativeMeters.length < 2) {
    // track_geometry.ts and train_service.ts are generated separately; if one
    // is regenerated without the other, degrade to a parked train rather than
    // dereferencing past the end of the shorter array.
    return {
      coordinate: path[0],
      tangent: [1, 0] as [number, number],
    };
  }

  const finalDistance = cumulativeMeters[cumulativeMeters.length - 1] ?? 0;
  const clampedDistance = clamp(targetDistanceMeters, 0, finalDistance);
  let upperIndex = cumulativeMeters.findIndex((distance) => distance >= clampedDistance);
  if (upperIndex <= 0) {
    // findIndex returns -1 when the target is past the last sample; both that
    // and a leading 0 resolve to the first real segment.
    upperIndex = 1;
  }
  upperIndex = Math.min(upperIndex, path.length - 1);

  const lowerIndex = Math.max(0, upperIndex - 1);
  const start = path[lowerIndex];
  const end = path[upperIndex];
  const startDistance = cumulativeMeters[lowerIndex];
  const endDistance = cumulativeMeters[upperIndex];
  const denominator = endDistance - startDistance;
  const ratio = denominator === 0 ? 0 : (clampedDistance - startDistance) / denominator;
  const coordinate: LonLat = [
    start[0] + (end[0] - start[0]) * ratio,
    start[1] + (end[1] - start[1]) * ratio,
  ];

  return {
    coordinate,
    tangent: [end[0] - start[0], end[1] - start[1]] as [number, number],
  };
}

function offsetCoordinate(
  coordinate: LonLat,
  tangent: [number, number],
  offsetMeters: number,
): LonLat {
  if (offsetMeters === 0) {
    return coordinate;
  }

  const latitudeRadians = (coordinate[1] * Math.PI) / 180;
  const lonScale = 111320 * Math.cos(latitudeRadians);
  const latScale = 111320;
  const tangentX = tangent[0] * lonScale;
  const tangentY = tangent[1] * latScale;
  const tangentLength = Math.hypot(tangentX, tangentY);
  if (tangentLength === 0) {
    return coordinate;
  }

  const normalX = -tangentY / tangentLength;
  const normalY = tangentX / tangentLength;
  return [
    coordinate[0] + (normalX * offsetMeters) / lonScale,
    coordinate[1] + (normalY * offsetMeters) / latScale,
  ];
}

function departuresForWindow(
  window: TrainServiceWindow,
  absoluteDayStartMinute: number,
  windowRangeStartMinute: number,
  windowRangeEndMinute: number,
) {
  if (window.headwayMinutes <= 0) return [];

  const absoluteWindowStart = absoluteDayStartMinute + window.startMinute;
  const absoluteWindowEnd = absoluteDayStartMinute + window.endMinute;
  const firstDeparture = absoluteWindowStart + Math.min(
    window.offsetMinutes,
    Math.max(0, window.headwayMinutes - 0.001),
  );
  const searchStart = Math.max(windowRangeStartMinute, absoluteWindowStart);
  const searchEnd = Math.min(windowRangeEndMinute, absoluteWindowEnd);
  if (searchEnd <= absoluteWindowStart || firstDeparture >= absoluteWindowEnd) {
    return [];
  }

  const firstIndex = Math.max(
    0,
    Math.ceil((searchStart - firstDeparture) / window.headwayMinutes),
  );
  const departures: number[] = [];
  for (
    let departure = firstDeparture + firstIndex * window.headwayMinutes;
    departure < searchEnd;
    departure += window.headwayMinutes
  ) {
    departures.push(Number(departure.toFixed(3)));
  }
  return departures;
}

function synthesizeDirectionTrains(
  line: TransitLine,
  directionProfile: TrainDirectionProfile,
  minuteOfWeek: number,
) {
  const totalRuntime = totalVisibleRuntimeMinutes(directionProfile);
  const absoluteStart = minuteOfWeek - totalRuntime - TERMINAL_LOOKBACK_BUFFER_MINUTES;
  const dayStartIndex = Math.floor(absoluteStart / MINUTES_PER_DAY);
  const dayEndIndex = Math.floor(minuteOfWeek / MINUTES_PER_DAY);
  const offsetMeters = line.offset * TRAIN_OFFSET_METERS_PER_OFFSET_UNIT;
  const trains: TrainInstance[] = [];

  for (let serviceDayIndex = dayStartIndex; serviceDayIndex <= dayEndIndex; serviceDayIndex += 1) {
    const dayType = minuteOfWeekToDayType(serviceDayIndex * MINUTES_PER_DAY);
    const serviceDirectionProfile = getDirectionProfile(
      line.id,
      dayType,
      directionProfile.directionId,
    );
    if (!serviceDirectionProfile) {
      continue;
    }

    const absoluteDayStartMinute = serviceDayIndex * MINUTES_PER_DAY;
    for (const window of serviceDirectionProfile.serviceWindows) {
      const departures = departuresForWindow(
        window,
        absoluteDayStartMinute,
        absoluteStart,
        minuteOfWeek + 0.001,
      );

      for (const departureMinute of departures) {
        const elapsedMinutes = minuteOfWeek - departureMinute;
        if (elapsedMinutes < 0 || elapsedMinutes > totalRuntime) {
          continue;
        }

        const distanceMeters = distanceForElapsedMinutes(
          serviceDirectionProfile,
          elapsedMinutes,
        );
        const { coordinate, tangent } = samplePathPosition(
          line.path ?? [],
          serviceDirectionProfile.pathCumulativeMeters,
          distanceMeters,
        );
        trains.push({
          id: `${line.id}:${serviceDirectionProfile.directionId}:${departureMinute.toFixed(3)}`,
          lineId: line.id,
          directionId: serviceDirectionProfile.directionId,
          coordinates: offsetCoordinate(coordinate, tangent, offsetMeters),
          color: hexToRgb(line.color),
        });
      }
    }
  }

  return trains;
}

export function synthesizeTrainInstances(
  activeLines: TransitLine[],
  minuteOfWeek: number | null,
) {
  if (minuteOfWeek === null) {
    return [] as TrainInstance[];
  }

  const currentDayType = minuteOfWeekToDayType(minuteOfWeek);
  return activeLines.flatMap((line) => {
    const lineProfile = getLineProfile(line.id, currentDayType);
    if (!lineProfile || !line.path) {
      return [];
    }
    return lineProfile.directionProfiles.flatMap((directionProfile) =>
      synthesizeDirectionTrains(line, directionProfile, minuteOfWeek),
    );
  });
}

export function useInterpolatedMinuteOfWeek(playback: PlaybackState | null) {
  const [minuteOfWeek, setMinuteOfWeek] = useState<number | null>(
    playback?.sim_time.minute_of_week ?? null,
  );
  const previousPlaybackRef = useRef<PlaybackState | null>(null);
  // Mirrors `minuteOfWeek` for the drift check below. Written only from the
  // animation frame and the resync, never during render.
  const minuteOfWeekRef = useRef<number | null>(minuteOfWeek);

  useEffect(() => {
    const previousPlayback = previousPlaybackRef.current;
    previousPlaybackRef.current = playback;

    if (!playback) {
      const clearHandle = window.setTimeout(() => {
        minuteOfWeekRef.current = null;
        setMinuteOfWeek(null);
      }, 0);
      return () => {
        window.clearTimeout(clearHandle);
      };
    }

    const serverMinute = playback.sim_time.minute_of_week;
    // How far our local clock has drifted from the server's. The previous
    // version compared two server-derived values, which are consistent with
    // each other by construction, so the guard could never fire and the local
    // clock drifted without bound.
    const localMinute = minuteOfWeekRef.current;
    const localDrift =
      localMinute === null
        ? null
        : Math.min(
            wrappedMinuteDelta(localMinute, serverMinute),
            wrappedMinuteDelta(serverMinute, localMinute),
          );
    const shouldResync =
      previousPlayback === null ||
      playback.is_playing !== previousPlayback.is_playing ||
      !playback.is_playing ||
      localDrift === null ||
      localDrift > PLAYBACK_RESYNC_THRESHOLD_MINUTES;

    const syncHandle = shouldResync
      ? window.setTimeout(() => {
          minuteOfWeekRef.current = serverMinute;
          setMinuteOfWeek(serverMinute);
        }, 0)
      : null;

    if (!playback.is_playing) {
      return () => {
        if (syncHandle !== null) {
          window.clearTimeout(syncHandle);
        }
      };
    }

    // Match the simulation's own rate so trains arrive when the time dial says
    // they do. `sim_minutes_per_second` is authoritative and can change while
    // the user is watching.
    const visualMinutesPerSecond =
      playback.sim_minutes_per_second > 0
        ? playback.sim_minutes_per_second
        : FALLBACK_VISUAL_MINUTES_PER_SECOND;

    let frameHandle = 0;
    let previousFrameAt = performance.now();
    const updateFrame = () => {
      const currentFrameAt = performance.now();
      const elapsedSeconds = (currentFrameAt - previousFrameAt) / 1000;
      previousFrameAt = currentFrameAt;
      setMinuteOfWeek((currentMinute) => {
        const next = wrapMinuteOfWeek(
          (currentMinute ?? serverMinute) +
            elapsedSeconds * visualMinutesPerSecond,
        );
        minuteOfWeekRef.current = next;
        return next;
      });
      frameHandle = requestAnimationFrame(updateFrame);
    };

    frameHandle = requestAnimationFrame(updateFrame);
    return () => {
      if (syncHandle !== null) {
        window.clearTimeout(syncHandle);
      }
      cancelAnimationFrame(frameHandle);
    };
  }, [playback]);

  return minuteOfWeek;
}
