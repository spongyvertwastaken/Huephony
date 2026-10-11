export const ARTWORK_SIZE = { min: 30, max: 100, fallback: 70 } as const;

export function artworkSize(value: unknown): number {
  const size = typeof value === "number" && Number.isFinite(value) ? Math.round(value) : ARTWORK_SIZE.fallback;
  return Math.min(ARTWORK_SIZE.max, Math.max(ARTWORK_SIZE.min, size));
}
