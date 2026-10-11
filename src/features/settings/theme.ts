import { type CSSProperties } from "react";

function hexToRgb(hex: string): [number, number, number] | null {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, 9 - k(n), 1));
  return [f(0), f(8), f(4)];
}

// Album colors can be near black or washed out. Keep the hue, but bring
// lightness and saturation into a range that reads on the dark panel.
function legibleAccent(hex: string): { h: number; s: number; l: number; luminance: number } {
  const [r, g, b] = (hexToRgb(hex) ?? [95, 198, 188]).map((channel) => channel / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  const lightness = (max + min) / 2;
  const saturation = delta === 0 ? 0 : delta / (1 - Math.abs(2 * lightness - 1));
  let hue = 0;
  if (delta) {
    hue = max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
    hue = (hue * 60 + 360) % 360;
  }
  const s = saturation < 0.08 ? saturation : Math.max(saturation, 0.5);
  const l = Math.min(0.68, Math.max(0.52, lightness));
  const linear = hslToRgb(hue, s, l).map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return { h: hue, s, l, luminance: 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2] };
}

export function accentStyle([first, second]: [string, string]): CSSProperties {
  const a = legibleAccent(first);
  const b = legibleAccent(second);
  const hsl = ({ h, s, l }: typeof a, alpha = 1) =>
    `hsl(${Math.round(h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}% / ${alpha})`;
  return {
    "--st-accent": hsl(a),
    "--st-accent-2": hsl(b),
    "--st-accent-soft": hsl(a, 0.18),
    "--st-glow": hsl(a, 0.12),
    "--st-accent-ink": (a.luminance + b.luminance) / 2 > 0.4 ? "#15121a" : "#ffffff",
  } as CSSProperties;
}
