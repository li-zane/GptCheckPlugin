import type {
  AccountSchedulingChangeEvent,
  UpstreamChannelChangeEvent,
} from "./types";

export const changeLogCacheVersion = 1;
const cacheKeyPrefix = "sub2api-at-change-log:";
const maxCachedItems = 200;

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type EnumerableStorageLike = Pick<Storage, "key" | "length" | "removeItem">;
type ChangeLogItem = UpstreamChannelChangeEvent | AccountSchedulingChangeEvent;

export type ChangeLogCacheCategory = "upstream" | "account_rate" | "scheduling";

export type ChangeLogCacheEntry<T extends ChangeLogItem> = {
  items: T[];
  hasMore: boolean;
  unreadCount: number;
  lastReadId: number;
  storedAt: number;
};

const memoryCache = new Map<string, ChangeLogCacheEntry<ChangeLogItem>>();

export function getChangeLogSessionStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function changeLogCacheKey(
  baseUrl: string,
  category: ChangeLogCacheCategory,
  startDate: string,
  endDate: string,
  timeZone: string,
) {
  const scope = normalizeBaseUrl(baseUrl);
  const filters = `${startDate.trim()}|${endDate.trim()}|${timeZone.trim() || "UTC"}`;
  return `${cacheKeyPrefix}v${changeLogCacheVersion}:${encodeURIComponent(scope)}:${category}:${encodeURIComponent(filters)}`;
}

export function readChangeLogCache<T extends ChangeLogItem>(
  storage: StorageLike | null,
  key: string,
): ChangeLogCacheEntry<T> | null {
  const inMemory = memoryCache.get(key);
  if (inMemory) return cloneEntry(inMemory) as ChangeLogCacheEntry<T>;
  if (!storage) return null;
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (parsed.version !== changeLogCacheVersion || !isCacheEntry(parsed.data)) return null;
    const entry = parsed.data as ChangeLogCacheEntry<ChangeLogItem>;
    memoryCache.set(key, cloneEntry(entry));
    return cloneEntry(entry) as ChangeLogCacheEntry<T>;
  } catch {
    return null;
  }
}

export function writeChangeLogCache<T extends ChangeLogItem>(
  storage: StorageLike | null,
  key: string,
  entry: Omit<ChangeLogCacheEntry<T>, "storedAt"> & { storedAt?: number },
) {
  const safeEntry: ChangeLogCacheEntry<T> = {
    items: mergeChangeLogItems([], entry.items),
    hasMore: Boolean(entry.hasMore),
    unreadCount: nonNegativeInteger(entry.unreadCount),
    lastReadId: nonNegativeInteger(entry.lastReadId),
    storedAt: Number.isFinite(entry.storedAt) ? Number(entry.storedAt) : Date.now(),
  };
  memoryCache.set(key, cloneEntry(safeEntry));
  if (storage) {
    try {
      storage.setItem(key, JSON.stringify({
        version: changeLogCacheVersion,
        data: safeEntry,
      }));
    } catch {
      // The memory cache remains available when storage is restricted or full.
    }
  }
  return cloneEntry(safeEntry);
}

export function markChangeLogCacheRead(
  storage: StorageLike | null,
  key: string,
  throughId: number,
) {
  const entry = readChangeLogCache<ChangeLogItem>(storage, key);
  if (!entry) return null;
  const lastReadId = Math.max(entry.lastReadId, nonNegativeInteger(throughId));
  return writeChangeLogCache(storage, key, {
    ...entry,
    lastReadId,
    unreadCount: entry.items.filter((item) => item.unread && item.id > lastReadId).length,
    items: entry.items.map((item) => (
      item.unread && item.id <= lastReadId ? { ...item, unread: false } : item
    )),
  });
}

export function mergeChangeLogItems<T extends ChangeLogItem>(
  current: ReadonlyArray<T>,
  incoming: ReadonlyArray<T>,
): T[] {
  const byId = new Map<number, T>();
  for (const item of current) {
    if (isChangeLogItem(item)) byId.set(item.id, { ...item });
  }
  for (const item of incoming) {
    if (isChangeLogItem(item)) byId.set(item.id, { ...item });
  }
  return [...byId.values()]
    .sort((left, right) => {
      const timeDifference = Date.parse(right.created_at) - Date.parse(left.created_at);
      return Number.isFinite(timeDifference) && timeDifference !== 0
        ? timeDifference
        : right.id - left.id;
    })
    .slice(0, maxCachedItems);
}

export function clearChangeLogMemoryCache() {
  memoryCache.clear();
}

export function clearChangeLogCache(storage: EnumerableStorageLike | null) {
  memoryCache.clear();
  if (!storage) return;
  try {
    const keys: string[] = [];
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key?.startsWith(cacheKeyPrefix)) keys.push(key);
    }
    keys.forEach((key) => storage.removeItem(key));
  } catch {
    // Restricted browser contexts may reject storage access.
  }
}

function normalizeBaseUrl(baseUrl: string) {
  const raw = baseUrl.trim();
  if (!raw) return "unconfigured";
  try {
    const url = new URL(raw);
    url.hash = "";
    url.search = "";
    url.pathname = url.pathname.replace(/\/{2,}/g, "/").replace(/\/$/, "") || "/";
    return url.toString().replace(/\/$/, "");
  } catch {
    return raw.replace(/\/$/, "");
  }
}

function isCacheEntry(value: unknown): value is ChangeLogCacheEntry<ChangeLogItem> {
  if (!value || typeof value !== "object") return false;
  const entry = value as Record<string, unknown>;
  return Array.isArray(entry.items)
    && entry.items.every(isChangeLogItem)
    && typeof entry.hasMore === "boolean"
    && Number.isSafeInteger(entry.unreadCount)
    && Number(entry.unreadCount) >= 0
    && Number.isSafeInteger(entry.lastReadId)
    && Number(entry.lastReadId) >= 0
    && typeof entry.storedAt === "number"
    && Number.isFinite(entry.storedAt);
}

function isChangeLogItem(value: unknown): value is ChangeLogItem {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return Number.isSafeInteger(item.id)
    && Number(item.id) > 0
    && typeof item.created_at === "string"
    && typeof item.unread === "boolean";
}

function nonNegativeInteger(value: unknown) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number >= 0 ? number : 0;
}

function cloneEntry<T extends ChangeLogItem>(entry: ChangeLogCacheEntry<T>): ChangeLogCacheEntry<T> {
  return {
    ...entry,
    items: entry.items.map((item) => ({ ...item })),
  };
}
