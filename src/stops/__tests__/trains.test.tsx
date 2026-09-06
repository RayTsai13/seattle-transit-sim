/**
 * @vitest-environment jsdom
 */
import { useEffect } from 'react';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useInterpolatedMinuteOfWeek } from '../trains.ts';
import type { PlaybackState } from '../../heatmap/api.ts';

function playbackAt(
  minuteOfWeek: number,
  overrides: Partial<PlaybackState> = {},
): PlaybackState {
  return {
    is_playing: true,
    current_tick: 0,
    sim_step_seconds: 1800,
    sim_minutes_per_second: 30,
    frame_interval_seconds: 1,
    time_bin_minutes: 30,
    sim_time: {
      day_of_week: Math.floor(minuteOfWeek / 1440),
      time_bin: Math.floor((minuteOfWeek % 1440) / 30) * 30,
      minute_of_week: minuteOfWeek,
    },
    ...overrides,
  };
}

// A test probe deliberately captures what the hook returned. Publishing it
// through an effect keeps the render itself side-effect free.
const captured: { minuteOfWeek: number | null } = { minuteOfWeek: null };

function Probe({ playback }: { playback: PlaybackState | null }) {
  const minuteOfWeek = useInterpolatedMinuteOfWeek(playback);
  useEffect(() => {
    captured.minuteOfWeek = minuteOfWeek;
  });
  return null;
}

function latestMinute(): number | null {
  return captured.minuteOfWeek;
}

let now = 0;
let nextFrameHandle = 1;
// A real cancelAnimationFrame matters here: the hook cancels and re-registers
// its loop on every playback change, and a no-op stub would leave stale
// callbacks running and double-advance the clock.
let frameCallbacks = new Map<number, FrameRequestCallback>();

/** Run one animation frame after advancing the clock by `seconds`. */
function advance(seconds: number) {
  now += seconds * 1000;
  const pending = [...frameCallbacks.entries()];
  frameCallbacks = new Map();
  act(() => {
    for (const [, cb] of pending) cb(now);
  });
}

beforeEach(() => {
  captured.minuteOfWeek = null;
  now = 0;
  nextFrameHandle = 1;
  frameCallbacks = new Map();
  vi.useFakeTimers();
  vi.spyOn(performance, 'now').mockImplementation(() => now);
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    const handle = nextFrameHandle++;
    frameCallbacks.set(handle, cb);
    return handle;
  });
  vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
    frameCallbacks.delete(handle);
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useInterpolatedMinuteOfWeek', () => {
  it('advances at the rate the server declares, not a fixed constant', () => {
    // The backend default is 30 sim-minutes per real second. A hardcoded
    // 1 min/s made trains crawl at 1/30 of the clock the rest of the UI shows.
    const { rerender } = render(<Probe playback={playbackAt(600)} />);
    act(() => {
      vi.runAllTimers();
    });
    rerender(<Probe playback={playbackAt(600)} />);
    expect(latestMinute()).toBe(600);

    advance(1);
    expect(latestMinute()).toBeCloseTo(630, 3);

    advance(2);
    expect(latestMinute()).toBeCloseTo(690, 3);
  });

  it('follows a speed change', () => {
    const slow = playbackAt(600, { sim_minutes_per_second: 1, sim_step_seconds: 60 });
    const { rerender } = render(<Probe playback={slow} />);
    act(() => {
      vi.runAllTimers();
    });
    rerender(<Probe playback={slow} />);

    advance(1);
    expect(latestMinute()).toBeCloseTo(601, 3);
  });

  it('holds the clock while playback is paused', () => {
    const paused = playbackAt(600, { is_playing: false });
    const { rerender } = render(<Probe playback={paused} />);
    act(() => {
      vi.runAllTimers();
    });
    rerender(<Probe playback={paused} />);
    expect(latestMinute()).toBe(600);

    advance(5);
    expect(latestMinute()).toBe(600);
  });

  it('resyncs when the local clock has drifted from the server', () => {
    const { rerender } = render(<Probe playback={playbackAt(600)} />);
    act(() => {
      vi.runAllTimers();
    });
    rerender(<Probe playback={playbackAt(600)} />);

    // Let the local clock run far ahead of what the server last reported.
    advance(10);
    expect(latestMinute()).toBeCloseTo(900, 3);

    // A server update well behind the local clock must snap it back. The old
    // guard compared two server-derived values, so it could never fire.
    rerender(<Probe playback={playbackAt(660)} />);
    act(() => {
      vi.runAllTimers();
    });
    expect(latestMinute()).toBe(660);
  });

  it('does not resync while the local clock is tracking the server', () => {
    const { rerender } = render(<Probe playback={playbackAt(600)} />);
    act(() => {
      vi.runAllTimers();
    });
    rerender(<Probe playback={playbackAt(600)} />);

    advance(1);
    const afterOneSecond = latestMinute();
    expect(afterOneSecond).toBeCloseTo(630, 3);

    // The server reports the same 30-minute step the local clock just made,
    // so there is nothing to correct and no visible snap.
    rerender(<Probe playback={playbackAt(630)} />);
    act(() => {
      vi.runAllTimers();
    });
    expect(latestMinute()).toBeCloseTo(630, 3);
  });
});
