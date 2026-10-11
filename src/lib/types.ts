import type { AnimationSpeed, LightEffect, ProgressStyle, WaveDirection } from "../features/lights/effects";
import type { GlassStyle } from "../features/player/glass";

export interface NowPlayingTrack {
  id: string;
  title: string;
  artist: string;
  album: string;
  cover_url: string;
  external_url?: string;
  progress_ms: number;
  // When progress_ms was read from Spotify (epoch ms).
  progress_at_ms?: number;
  duration_ms: number;
  volume_percent?: number | null;
}

export interface NowPlayingState {
  ok: boolean;
  authenticated: boolean;
  is_playing: boolean;
  mode: string;
  message: string;
  track: NowPlayingTrack | null;
  palette: string[];
  // One color per light slot (stand by stand, top first) for the current track.
  light_plan?: string[];
  smart_color_boost?: {
    source: "local" | "off";
    quality_score: number;
    palette: Array<{
      hex: string;
      weight: number;
      role: "base" | "accent" | "contrast" | "shadow" | "highlight";
    }>;
    // The artwork's real colors and how much of the cover each covers (0-1).
    swatches?: Array<{ hex: string; share: number }>;
    analysis: Record<string, unknown>;
  };
  lights: {
    enabled: boolean;
    connected: boolean;
    message: string;
  };
  // Artwork of the next queued track, so it can load before the change.
  up_next_cover_url?: string;
  updated_at: number;
}

export interface LightDevice {
  name: string;
  type?: string;
  brand?: string;
  sku?: string;
  adapter?: string;
  device_id?: string;
  ip?: string;
  enabled?: boolean;
  palette_index?: number;
  // Which room slot this light shows (see roomSizes); unset = by name/order.
  slot?: number;
  [key: string]: unknown;
}

export interface AppSettings {
  spotify_client_id: string;
  spotify_client_secret: string;
  spotify_redirect_uri: string;
  frontend_url: string;
  lights_enabled: boolean;
  default_light_adapter: string;
  brightness_min: number;
  brightness_max: number;
  update_speed_ms: number;
  transition_duration_ms: number;
  animation_enabled: boolean;
  // Pace of the Wave effect.
  animation_speed: AnimationSpeed;
  idle_animation_enabled: boolean;
  // How the room moves while a song plays (see effects.ts).
  light_effect: LightEffect;
  wave_direction: WaveDirection;
  progress_style: ProgressStyle;
  smart_color_boost_enabled: boolean;
  smart_color_boost_mode: "off" | "auto" | "always";
  accent_boost_strength: "low" | "medium" | "high";
  neutral_reduction: "off" | "balanced" | "strong";
  red_pink_correction_enabled: boolean;
  // 0 = automatic (one slot per enabled light, each on its own stand).
  light_count: number;
  light_stands: number;
  glass_style: GlassStyle;
  // Cover size when the player details are hidden: percent of the screen's short side.
  artwork_size: number;
  devices: LightDevice[];
  config_dir?: string;
}

export interface LightDeviceStatus {
  name: string;
  ok: boolean;
  message: string;
}

export interface LightCommandResult {
  ok: boolean;
  enabled: boolean;
  connected: boolean;
  message: string;
  devices: LightDeviceStatus[];
}

export interface PulseReading {
  // False when this computer's sound can't be read (message says why).
  available: boolean;
  // Whose sound is followed: the Spotify app's, or anything this PC plays.
  source: "spotify" | "pc" | null;
  // Music is playing through this PC right now.
  hearing: boolean;
  // Loudness relative to the music's loud parts, 0-1.
  level: number;
  message: string;
}

export interface DiscoveredLight {
  device_id: string;
  ip: string;
  sku: string;
  name: string;
  wifi_version?: string;
  ble_version?: string;
}

export interface LightDiscoveryResult {
  ok: boolean;
  error: string | null;
  message: string;
  devices: DiscoveredLight[];
  tips: string[];
}

export interface LightIdentifyResult {
  ok: boolean;
  reachable: boolean;
  message: string;
}

export interface PlayerCommandResult {
  ok: boolean;
  message: string;
}
