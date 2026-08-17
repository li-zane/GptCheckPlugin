import type { UpstreamMonitorTimelinePoint } from "./domain";

export const UPSTREAM_MONITOR_TIMELINE_LIMIT = 60;

export function recentUpstreamMonitorTimeline(
  timeline: UpstreamMonitorTimelinePoint[] | null | undefined,
) {
  return [...(timeline || [])]
    .filter((point) => Number.isFinite(upstreamMonitorTimestamp(point)))
    .sort((left, right) => upstreamMonitorTimestamp(left) - upstreamMonitorTimestamp(right))
    .slice(-UPSTREAM_MONITOR_TIMELINE_LIMIT);
}

export function latestUpstreamMonitorStatus(
  primaryStatus: string | null | undefined,
  timeline: UpstreamMonitorTimelinePoint[] | null | undefined,
) {
  let latestTimestamp = Number.NEGATIVE_INFINITY;
  let latestStatus = "";

  for (const point of timeline || []) {
    const timestamp = Date.parse(String(point.checked_at || point.time || ""));
    const status = String(point.status || "").trim().toLowerCase();
    if (!Number.isFinite(timestamp) || !status) continue;
    if (timestamp >= latestTimestamp) {
      latestTimestamp = timestamp;
      latestStatus = status;
    }
  }

  if (latestStatus) return latestStatus;
  const primary = String(primaryStatus || "").trim().toLowerCase();
  if (primary) return primary;
  for (let index = (timeline || []).length - 1; index >= 0; index -= 1) {
    const status = String(timeline?.[index]?.status || "").trim().toLowerCase();
    if (status) return status;
  }
  return "unknown";
}

function upstreamMonitorTimestamp(point: UpstreamMonitorTimelinePoint) {
  return Date.parse(String(point.checked_at || point.time || ""));
}
