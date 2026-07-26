import type { ApiKeySubview } from "./viewRouting";
import type { ChangeLogUnreadCounts } from "./types";

export type ChangeLogSubview = Extract<
  ApiKeySubview,
  "rate-log" | "account-rate-log" | "schedule-log"
>;

export const CHANGE_LOG_READ_RETRY_DELAYS_MS = [1_000, 2_000, 4_000] as const;

export function pendingReadThroughId(
  currentThroughId: number | null,
  items: ReadonlyArray<{ id: number; unread: boolean }>,
): number | null {
  let throughId = currentThroughId;
  for (const item of items) {
    if (item.unread && (throughId === null || item.id > throughId)) throughId = item.id;
  }
  return throughId;
}

export function departedChangeLogSubview(
  previousSubview: ApiKeySubview,
  currentSubview: ApiKeySubview,
): ChangeLogSubview | null {
  if (
    previousSubview !== currentSubview
    && (
      previousSubview === "rate-log"
      || previousSubview === "account-rate-log"
      || previousSubview === "schedule-log"
    )
  ) {
    return previousSubview;
  }
  return null;
}

export function visibleChangeLogUnreadCounts(
  counts: ChangeLogUnreadCounts,
  _currentSubview: ApiKeySubview,
): ChangeLogUnreadCounts {
  // Opening a ledger is not the read boundary. Keep its badge visible until
  // the user leaves and the persisted read cursor has been advanced.
  return { ...counts };
}
