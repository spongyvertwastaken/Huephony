// Light effects for the Settings room preview; mirrors backend/lights/effects.py.
// Each function returns one [color, level] per bulb, level 0 (lowest
// brightness) to 1 (highest).

export type LightEffect = "still" | "wave" | "pulse" | "progress";
export type WaveDirection = "across" | "down";
export type ProgressStyle = "fill" | "blend";
export type AnimationSpeed = "slow" | "medium" | "fast";

export const LIGHT_EFFECTS: LightEffect[] = ["still", "wave", "pulse", "progress"];

export function lightEffect(value: unknown): LightEffect {
  return LIGHT_EFFECTS.includes(value as LightEffect) ? (value as LightEffect) : "still";
}

export type Bulb = [color: string, level: number];

// [seconds between sweeps, seconds a sweep takes to cross the room]
const WAVE_TIMING: Record<AnimationSpeed, [number, number]> = {
  slow: [9, 3],
  medium: [6, 2],
  fast: [3.5, 1.2],
};
const WAVE_FADE_SHARE = 0.6;
const PROGRESS_BLEND_STEPS = 200;

export function colorsByUse(plan: string[]): string[] {
  const counts = new Map<string, number>();
  for (const color of plan) counts.set(color, (counts.get(color) ?? 0) + 1);
  // Array.sort is stable, so ties keep plan order.
  return [...counts.keys()].sort((a, b) => counts.get(b)! - counts.get(a)!);
}

function rgb(hex: string): [number, number, number] {
  const value = parseInt(hex.replace("#", ""), 16) || 0;
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function hsv([r, g, b]: [number, number, number]): [number, number, number] {
  const max = Math.max(r, g, b) / 255;
  const min = Math.min(r, g, b) / 255;
  const delta = max - min;
  let hue = 0;
  if (delta) {
    const [rn, gn, bn] = [r / 255, g / 255, b / 255];
    hue = max === rn ? ((gn - bn) / delta) % 6 : max === gn ? (bn - rn) / delta + 2 : (rn - gn) / delta + 4;
    hue = ((hue / 6) % 1 + 1) % 1;
  }
  return [hue, max ? delta / max : 0, max];
}

function hex(channels: number[]): string {
  return `#${channels.map((c) => Math.round(Math.min(255, Math.max(0, c))).toString(16).padStart(2, "0")).join("")}`;
}

// Same blend as the bulbs (base.lerp_hex): shortest hue arc for real colors,
// straight RGB when either end is close to neutral.
export function mix(start: string, end: string, fraction: number): string {
  if (fraction <= 0) return start;
  if (fraction >= 1) return end;
  const a = rgb(start);
  const b = rgb(end);
  const [h1, s1, v1] = hsv(a);
  const [h2, s2, v2] = hsv(b);
  if (s1 < 0.15 || s2 < 0.15) return hex(a.map((c, i) => c + (b[i] - c) * fraction));
  let delta = h2 - h1;
  if (delta > 0.5) delta -= 1;
  else if (delta < -0.5) delta += 1;
  const h = (((h1 + delta * fraction) % 1) + 1) % 1;
  const s = s1 + (s2 - s1) * fraction;
  const v = v1 + (v2 - v1) * fraction;
  const k = (n: number) => (n + h * 6) % 6;
  const f = (n: number) => v - v * s * Math.max(0, Math.min(k(n), 4 - k(n), 1));
  return hex([f(5) * 255, f(3) * 255, f(1) * 255]);
}

function isWhite(color: string): boolean {
  const [, saturation, value] = hsv(rgb(color));
  return saturation < 0.12 && value > 0.6;
}

export function mainAndAccent(plan: string[]): [string, string | null] {
  const ordered = colorsByUse(plan);
  const main = ordered[0] ?? "#f2eee8";
  const others = ordered.slice(1);
  return [main, others.find((color) => !isWhite(color)) ?? others[0] ?? null];
}

// --- wave ---------------------------------------------------------------------

export function waveDirections(sizes: number[]): WaveDirection[] {
  return [...(sizes.length > 1 ? ["across" as const] : []), ...(Math.max(0, ...sizes) > 1 ? ["down" as const] : [])];
}

export function waveAxis(direction: WaveDirection, sizes: number[]): WaveDirection {
  const available = waveDirections(sizes);
  return available.includes(direction) ? direction : (available[0] ?? "across");
}

function waveOffset(stand: number, position: number, sizes: number[], axis: WaveDirection): number {
  if (axis === "across") return sizes.length > 1 ? stand / (sizes.length - 1) : 0;
  const size = sizes[stand] ?? 1;
  return size > 1 ? position / (size - 1) : 0.5;
}

export function waveFrame(
  places: Array<[number, number]>,
  sizes: number[],
  plan: string[],
  elapsed: number,
  direction: WaveDirection,
  speed: AnimationSpeed,
): Bulb[] {
  const colors = colorsByUse(plan);
  const sequence: Bulb[] =
    colors.length > 1 ? colors.map((color) => [color, 1]) : [[colors[0] ?? "#f2eee8", 1], [colors[0] ?? "#f2eee8", 0]];
  const [hold, sweep] = WAVE_TIMING[speed] ?? WAVE_TIMING.slow;
  const fade = sweep * WAVE_FADE_SHARE;
  const axis = waveAxis(direction, sizes);
  return places.map(([stand, position]) => {
    const local = elapsed - waveOffset(stand, position, sizes, axis) * sweep;
    if (local < 0) return sequence[0];
    const sweeps = Math.floor(local / hold);
    const fraction = Math.min(1, (local - sweeps * hold) / fade);
    const [toColor, toLevel] = sequence[sweeps % sequence.length];
    const [fromColor, fromLevel] = sweeps > 0 ? sequence[(sweeps - 1) % sequence.length] : sequence[0];
    return [mix(fromColor, toColor, fraction), fromLevel + (toLevel - fromLevel) * fraction];
  });
}

// --- progress -------------------------------------------------------------------

export function fillLevel(stand: number, position: number, sizes: number[], progress: number): number {
  const [units, unit] =
    sizes.length > 1 ? [sizes.length, stand] : [sizes[0] ?? 1, (sizes[0] ?? 1) - 1 - position];
  return Math.max(0, Math.min(1, progress * units - unit));
}

export function progressFrame(
  places: Array<[number, number]>,
  sizes: number[],
  plan: string[],
  progress: number,
  style: ProgressStyle,
): Bulb[] {
  const clamped = Math.max(0, Math.min(1, progress));
  const [main, accent] = mainAndAccent(plan);
  if (style === "blend" && accent) {
    const color = mix(main, accent, Math.round(clamped * PROGRESS_BLEND_STEPS) / PROGRESS_BLEND_STEPS);
    return places.map(() => [color, 1]);
  }
  return places.map(([stand, position], slot) => [plan[slot] ?? main, fillLevel(stand, position, sizes, clamped)]);
}
