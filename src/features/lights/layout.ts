import type { AppSettings } from "../../lib/types";

const MAX_LIGHTS = 32;
const DEFAULT_LIGHT_COUNT = 3;

// Bulbs per stand; mirrors room_layout in backend/lights/layout.py.
export function roomSizes(settings: Pick<AppSettings, "light_count" | "light_stands" | "devices">): number[] {
  const whole = (value: unknown) => (typeof value === "number" && value > 0 ? Math.floor(value) : 0);
  const enabled = (settings.devices ?? []).filter((device) => device.enabled !== false).length;
  const count = Math.min(MAX_LIGHTS, whole(settings.light_count) || enabled || DEFAULT_LIGHT_COUNT);
  const stands = Math.max(1, Math.min(count, whole(settings.light_stands) || count));
  const base = Math.floor(count / stands);
  return Array.from({ length: stands }, (_, index) => base + (index < count % stands ? 1 : 0));
}

export function slotLabel(sizes: number[], slot: number): string {
  let start = 0;
  for (const [stand, size] of sizes.entries()) {
    if (slot < start + size) {
      const position = slot - start;
      const place =
        size === 1 ? "" : position === 0 ? " · top" : position === size - 1 ? " · bottom" : size === 3 ? " · middle" : ` · ${position + 1}`;
      return `Stand ${stand + 1}${place}`;
    }
    start += size;
  }
  return `Spot ${slot + 1}`;
}
