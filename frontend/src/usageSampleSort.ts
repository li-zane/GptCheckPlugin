import type { UsageLimitSample } from "./types";

export type UsageSampleSortField = "quota" | "recorded_at";
export type UsageSampleSortDirection = "asc" | "desc";

function recordedAt(sample: UsageLimitSample) {
  const value = sample.updated_at || sample.created_at;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
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
        : recordedAt(left.sample) - recordedAt(right.sample);
      if (comparison !== 0) return comparison * multiplier;
      const idComparison = left.sample.id - right.sample.id;
      return idComparison !== 0 ? idComparison * multiplier : left.index - right.index;
    })
    .map(({ sample }) => sample);
}
