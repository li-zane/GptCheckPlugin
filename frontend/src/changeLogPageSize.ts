export const defaultChangeLogPageSizeOptions = [20, 50, 100, 200] as const;

export function normalizeChangeLogPageSizeOptions(value: unknown): number[] {
  if (!Array.isArray(value)) return [...defaultChangeLogPageSizeOptions];
  const normalized = [...new Set(value
    .map(Number)
    .filter((item) => Number.isInteger(item) && item >= 1 && item <= 200))]
    .sort((left, right) => left - right)
    .slice(0, 20);
  return normalized.length ? normalized : [...defaultChangeLogPageSizeOptions];
}

export function parseChangeLogPageSizeOptions(value: string): number[] | null {
  const tokens = value.split(/[,，\s]+/).filter(Boolean);
  if (!tokens.length || tokens.length > 20) return null;
  const parsed = tokens.map(Number);
  if (parsed.some((item) => !Number.isInteger(item) || item < 1 || item > 200)) return null;
  return [...new Set(parsed)].sort((left, right) => left - right);
}

export function normalizeChangeLogPageSize(
  value: number,
  options: unknown,
  fallback = 50,
): number {
  const normalizedOptions = normalizeChangeLogPageSizeOptions(options);
  if (normalizedOptions.includes(value)) return value;
  if (normalizedOptions.includes(fallback)) return fallback;
  return normalizedOptions[0];
}
