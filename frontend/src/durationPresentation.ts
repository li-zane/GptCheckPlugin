const EVENT_DURATION_STAGES = [
  ["account_list_duration_ms", "获取账号清单"],
  ["inventory_duration_ms", "同步本地清单"],
  ["usage_dispatch_duration_ms", "派发用量查询"],
  ["workflow_wait_duration_ms", "等待 API Key 同步"],
  ["queue_wait_duration_ms", "等待上一批用量任务"],
  ["query_duration_ms", "查询用量窗口"],
  ["probe_duration_ms", "探测上游"],
  ["priority_duration_ms", "应用优先级"],
] as const;

export function parseDurationMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.max(0, Math.round(value));
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return Math.max(0, Math.round(parsed));
  }
  return null;
}

export function formatElapsedDuration(durationMs: number): string {
  const milliseconds = Math.max(0, Math.round(durationMs));
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(2)} 秒`;

  const totalSeconds = Math.round(milliseconds / 1_000);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours} 小时 ${String(minutes).padStart(2, "0")} 分`;
  }
  return `${minutes} 分 ${String(seconds).padStart(2, "0")} 秒`;
}

export function eventDurationMs(details: Record<string, unknown> | null): number | null {
  return parseDurationMs(details?.duration_ms);
}

export function eventDurationBreakdown(details: Record<string, unknown> | null) {
  if (!details) return [];
  return EVENT_DURATION_STAGES.flatMap(([field, label]) => {
    const durationMs = parseDurationMs(details[field]);
    return durationMs === null ? [] : [{ field, label, durationMs }];
  });
}

export function timestampDurationMs(
  startedAt: string | null,
  finishedAt: string | null,
  now = Date.now(),
): number | null {
  if (!startedAt) return null;
  const started = Date.parse(startedAt);
  const finished = finishedAt ? Date.parse(finishedAt) : now;
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return null;
  return Math.max(0, Math.round(finished - started));
}
