const BASE_URL = '';

export type PlacedPerson = {
  id: string;
  lat: number;
  lon: number;
  count: number;
};

export type SimTime = {
  day_of_week: number;
  time_bin: number;
  minute_of_week: number;
};

export type PlaybackState = {
  is_playing: boolean;
  current_tick: number;
  sim_step_seconds: number;
  sim_minutes_per_second: number;
  frame_interval_seconds: number;
  time_bin_minutes: number;
  sim_time: SimTime;
};

export async function postScenario(scenarioId: string): Promise<void> {
  const res = await fetch(`${BASE_URL}/scenario`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario_id: scenarioId }),
  });
  if (!res.ok) {
    throw new Error(`POST /api/scenario failed: ${res.status}`);
  }
}

export async function postPeople(
  lat: number,
  lon: number,
  count = 1,
): Promise<PlacedPerson> {
  const res = await fetch(`${BASE_URL}/people`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lat, lon, count }),
  });
  if (!res.ok) {
    throw new Error(`POST /api/people failed: ${res.status}`);
  }
  return res.json();
}

export async function postPlayback(
  updates: Partial<Pick<PlaybackState, 'is_playing' | 'sim_minutes_per_second'>>,
): Promise<PlaybackState> {
  const res = await fetch(`${BASE_URL}/api/playback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!res.ok) {
    throw new Error(`POST /api/playback failed: ${res.status}`);
  }
  return res.json();
}

export async function seekPlayback(dayOfWeek: number, timeBin: number): Promise<PlaybackState> {
  const res = await fetch(`${BASE_URL}/api/playback/seek`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ day_of_week: dayOfWeek, time_bin: timeBin }),
  });
  if (!res.ok) {
    throw new Error(`POST /api/playback/seek failed: ${res.status}`);
  }
  return res.json();
}

export async function deletePerson(id: string): Promise<void> {
  const res = await fetch(`${BASE_URL}/people/${id}`, { method: 'DELETE' });
  if (!res.ok) {
    throw new Error(`DELETE /api/people/${id} failed: ${res.status}`);
  }
}

export async function deleteAllPeople(): Promise<void> {
  const res = await fetch(`${BASE_URL}/people`, { method: 'DELETE' });
  if (!res.ok) {
    throw new Error(`DELETE /api/people failed: ${res.status}`);
  }
}
