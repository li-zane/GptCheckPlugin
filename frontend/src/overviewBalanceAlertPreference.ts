type StorageLike = Pick<Storage, "getItem" | "setItem">;

export const overviewBalanceAlertDismissedStorageKey =
  "sub2api-at-overview-balance-alert-dismissed:v1";

export function getOverviewBalanceAlertStorage(): StorageLike | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readOverviewBalanceAlertDismissed(
  storage: StorageLike | null = getOverviewBalanceAlertStorage(),
) {
  try {
    return storage?.getItem(overviewBalanceAlertDismissedStorageKey) === "1";
  } catch {
    return false;
  }
}

export function persistOverviewBalanceAlertDismissed(
  storage: StorageLike | null = getOverviewBalanceAlertStorage(),
) {
  try {
    storage?.setItem(overviewBalanceAlertDismissedStorageKey, "1");
  } catch {
    // Keep the in-memory dismissal when browser storage is unavailable.
  }
}
