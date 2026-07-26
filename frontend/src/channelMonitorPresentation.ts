import type { UpstreamChannelMonitorTimelinePoint } from "./types";

export const CHANNEL_MONITOR_TIMELINE_LIMIT = 60;

export function recentChannelMonitorTimeline(
  timeline: UpstreamChannelMonitorTimelinePoint[] | null | undefined,
) {
  return [...(timeline || [])]
    .filter((point) => Number.isFinite(channelMonitorTimestamp(point)))
    .sort((left, right) => channelMonitorTimestamp(left) - channelMonitorTimestamp(right))
    .slice(-CHANNEL_MONITOR_TIMELINE_LIMIT);
}

export function latestChannelMonitorStatus(
  primaryStatus: string | null | undefined,
  timeline: UpstreamChannelMonitorTimelinePoint[] | null | undefined,
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

function channelMonitorTimestamp(point: UpstreamChannelMonitorTimelinePoint) {
  return Date.parse(String(point.checked_at || point.time || ""));
}
