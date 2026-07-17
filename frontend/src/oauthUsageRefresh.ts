const pendingUsageRefreshIntervalsMs = [250, 1_000, 2_500, 4_000] as const;

export function oauthUsageBackgroundRefreshIntervals(usagePending?: number) {
  return usagePending === 0 ? [0] : [...pendingUsageRefreshIntervalsMs];
}
