import type { NowPlayingState, AppSettings, LightCommandResult, PulseReading, LightDiscoveryResult, LightIdentifyResult, PlayerCommandResult } from "./types";

export const API_BASE =
  import.meta.env.VITE_BACKEND_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

export function spotifyLoginUrl(desktop = false) {
  return `${API_BASE}/login${desktop ? "?desktop=1" : ""}`;
}

export async function fetchNowPlaying(signal?: AbortSignal): Promise<NowPlayingState> {
  const response = await fetch(`${API_BASE}/api/now-playing`, { cache: "no-store", signal });
  if (!response.ok) {
    throw new Error(`Backend returned ${response.status}`);
  }
  return response.json();
}

export async function fetchSettings(signal?: AbortSignal): Promise<AppSettings> {
  const response = await fetch(`${API_BASE}/api/settings`, { signal });
  if (!response.ok) {
    throw new Error(`Backend returned ${response.status}`);
  }
  const payload = await response.json();
  return payload.settings;
}

export async function saveSettings(settings: Partial<AppSettings>): Promise<AppSettings> {
  const response = await fetch(`${API_BASE}/api/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (!response.ok) {
    throw new Error(`Backend returned ${response.status}`);
  }
  const payload = await response.json();
  return payload.settings;
}


async function sendLightCommand(path: string): Promise<LightCommandResult> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    cache: "no-store",
  });
  const payload = await response.json().catch(() => ({
    ok: false,
    enabled: false,
    connected: false,
    message: `Backend returned ${response.status}`,
    devices: [],
  }));
  if (!response.ok) {
    return {
      ok: false,
      enabled: false,
      connected: false,
      message: payload.message ?? payload.detail ?? `Backend returned ${response.status}`,
      devices: [],
    };
  }
  return payload;
}

export function testLights() {
  return sendLightCommand("/api/lights/test");
}

export async function applyLightPalette(palette: string[]) {
  const response = await fetch(`${API_BASE}/api/lights/apply-palette`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ palette }),
  });
  const payload = await response.json().catch(() => ({
    ok: false,
    enabled: false,
    connected: false,
    message: `Backend returned ${response.status}`,
    devices: [],
  }));
  if (!response.ok) {
    return {
      ok: false,
      enabled: false,
      connected: false,
      message: payload.message ?? payload.detail ?? `Backend returned ${response.status}`,
      devices: [],
    };
  }
  return payload;
}

export function turnLightsOff() {
  return sendLightCommand("/api/lights/off");
}



// The Pulse effect's live loudness. Asking keeps the backend listening; it
// stops shortly after the requests do.
export async function fetchPulse(signal?: AbortSignal): Promise<PulseReading> {
  const response = await fetch(`${API_BASE}/api/lights/pulse`, { cache: "no-store", signal });
  if (!response.ok) {
    throw new Error(`Backend returned ${response.status}`);
  }
  return response.json();
}

// Signs out of Spotify and deletes the sign-in and listening data kept locally.
export async function disconnectSpotify(): Promise<PlayerCommandResult> {
  return postJson<PlayerCommandResult>("/api/spotify/disconnect", {}, (message) => ({ ok: false, message }));
}







const BACKEND_UNREACHABLE =
  "Could not reach the app's background service. Close and reopen the app, then try again.";

async function postJson<T>(path: string, body: unknown, failure: (message: string) => T): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return failure(BACKEND_UNREACHABLE);
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload) {
    return failure(payload?.message ?? payload?.detail ?? `Backend returned ${response.status}`);
  }
  return payload as T;
}

// Searches the local network for Govee bulbs (takes a few seconds). Pass ips to
// ask specific addresses instead, for networks that block the search.
export function discoverLights(ips?: string[]) {
  return postJson<LightDiscoveryResult>("/api/lights/discover", ips ? { ips } : {}, (message) => ({
    ok: false,
    error: "request_failed",
    message,
    devices: [],
    tips: [],
  }));
}

// Blinks one bulb so the user can see which physical bulb it is.
export function identifyLight(light: { ip: string; name?: string; device_id?: string }) {
  return postJson<LightIdentifyResult>("/api/lights/identify", light, (message) => ({
    ok: false,
    reachable: false,
    message,
  }));
}



async function sendPlayerCommand(
  path: string,
  options: RequestInit = {},
): Promise<PlayerCommandResult> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    cache: "no-store",
  });
  const payload = await response.json().catch(() => ({
    ok: false,
    message: `Backend returned ${response.status}`,
  }));
  if (!response.ok) {
    return {
      ok: false,
      message: payload.message ?? payload.detail ?? `Backend returned ${response.status}`,
    };
  }
  return payload;
}

export function playSpotify() {
  return sendPlayerCommand("/api/player/play", { method: "PUT" });
}

export function pauseSpotify() {
  return sendPlayerCommand("/api/player/pause", { method: "PUT" });
}

export function nextSpotifyTrack() {
  return sendPlayerCommand("/api/player/next", { method: "POST" });
}

export function previousSpotifyTrack() {
  return sendPlayerCommand("/api/player/previous", { method: "POST" });
}

export function setSpotifyVolume(volume: number) {
  const clamped = Math.max(0, Math.min(100, Math.round(volume)));
  return sendPlayerCommand(`/api/player/volume?volume_percent=${clamped}`, { method: "PUT" });
}

export function seekSpotify(positionMs: number) {
  const clamped = Math.max(0, Math.round(positionMs));
  return sendPlayerCommand(`/api/player/seek?position_ms=${clamped}`, { method: "PUT" });
}
