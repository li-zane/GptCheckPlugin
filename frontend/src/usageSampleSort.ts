import type { UsageLimitSample } from "./types";

export type UsageSampleSortField = "quota" | "recorded_at";
export type UsageSampleSortDirection = "asc" | "desc";
export type UsageSampleDatePreset =
  | "last-7"
  | "last-30"
  | "last-60"
  | "before-7"
  | "before-30"
  | "before-60";

export const usageSampleDatePresets: Array<{
  id: UsageSampleDatePreset;
  label: string;
}> = [
  { id: "last-7", label: "近一周" },
  { id: "last-30", label: "近一个月" },
  { id: "last-60", label: "近两个月" },
  { id: "before-7", label: "一周前" },
  { id: "before-30", label: "一个月前" },
  { id: "before-60", label: "两个月前" },
];

const dateFormatters = new Map<string, Intl.DateTimeFormat>();

export function usageSampleRecordedAt(sample: UsageLimitSample) {
  const value = sample.updated_at || sample.created_at;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function usageSampleDateRangeForPreset(
  preset: UsageSampleDatePreset,
  timeZone: string,
  now = Date.now(),
) {
  const today = dateKey(now, timeZone);
  const days = preset.endsWith("-7") ? 7 : preset.endsWith("-30") ? 30 : 60;
  if (preset.startsWith("last-")) {
    return {
      startDate: shiftDateKey(today, -(days - 1)),
      endDate: today,
    };
  }
  return {
    startDate: "",
    endDate: shiftDateKey(today, -days),
  };
}

export function filterUsageLimitSamples(
  samples: UsageLimitSample[],
  startDate: string,
  endDate: string,
  timeZone: string,
) {
  const start = startDate.trim();
  const end = endDate.trim();
  if (!start && !end) return [...samples];
  return samples.filter((sample) => {
    const recordedDate = dateKey(usageSampleRecordedAt(sample), timeZone);
    return Boolean(recordedDate)
      && (!start || recordedDate >= start)
      && (!end || recordedDate <= end);
  });
}

export function sortUsageLimitSamples(
  samples: UsageLimitSample[],
  field: UsageSampleSortField,
  direction: UsageSampleSortDirection,
) {
  const multiplier = direction === "asc" ? 1 : -1;
  return samples
    .map((sample, index) => ({ sample, index }))
    .sort((left, right) => {
      const comparison = field === "quota"
        ? left.sample.observed_limit - right.sample.observed_limit
        : usageSampleRecordedAt(left.sample) - usageSampleRecordedAt(right.sample);
      if (comparison !== 0) return comparison * multiplier;
      const idComparison = left.sample.id - right.sample.id;
      return idComparison !== 0 ? idComparison * multiplier : left.index - right.index;
    })
    .map(({ sample }) => sample);
}

function dateKey(timestamp: number, timeZone: string) {
  if (!Number.isFinite(timestamp)) return "";
  try {
    let formatter = dateFormatters.get(timeZone);
    if (!formatter) {
      formatter = new Intl.DateTimeFormat("en-CA", {
        day: "2-digit",
        month: "2-digit",
        timeZone,
        year: "numeric",
      });
      dateFormatters.set(timeZone, formatter);
    }
    const parts = formatter.formatToParts(timestamp);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  } catch {
    return new Date(timestamp).toISOString().slice(0, 10);
  }
}

function shiftDateKey(value: string, days: number) {
  const [year, month, day] = value.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1, day + days));
  return shifted.toISOString().slice(0, 10);
}
