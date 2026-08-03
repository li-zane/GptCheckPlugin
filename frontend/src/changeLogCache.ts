import type {
  AccountSchedulingChangeEvent,
  UpstreamChannelChangeEvent,
} from "./types";

export const changeLogCacheVersion = 3;
const cacheKeyPrefix = "sub2api-at-change-log:";
const maxCachedItems = 200;
const categoryReadCursorMaxAgeMs = 60_000;

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;
type EnumerableStorageLike = Pick<Storage, "key" | "length" | "removeItem">;
type ChangeLogItem = UpstreamChannelChangeEvent | AccountSchedulingChangeEvent;

export type ChangeLogCacheCategory = "upstream" | "account_rate" | "scheduling";

export type ChangeLogCacheEntry<T extends ChangeLogItem> = {
  items: T[];
  hasMore: boolean;
  unreadCount: number;
  lastReadId: number;
  totalCount: number;
  page: number;
  pageSize: number;
  storedAt: number;
};

const memoryCache = new Map<string, ChangeLogCacheEntry<ChangeLogItem>>();
const categoryReadCursors = new Map<string, { cursor: number; recordedAt: number }>();

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
  page = 1,
  pageSize = 50,
) {
  const scope = normalizeBaseUrl(baseUrl);
  const filters = `${startDate.trim()}|${endDate.trim()}|${timeZone.trim() || "UTC"}|${page}|${pageSize}`;
  return `${cacheKeyPrefix}v${changeLogCacheVersion}:${encodeURIComponent(scope)}:${category}:${encodeURIComponent(filters)}`;
}

export function readChangeLogCache<T extends ChangeLogItem>(
  storage: StorageLike | null,
  key: string,
): ChangeLogCacheEntry<T> | null {
  const inMemory = memoryCache.get(key);
  if (inMemory) {
    return applyReadCursor(
      cloneEntry(inMemory),
      readCategoryCursor(key),
    ) as ChangeLogCacheEntry<T>;
  }
  if (!storage) return null;
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    if (parsed.version !== changeLogCacheVersion || !isCacheEntry(parsed.data)) return null;
    const entry = applyReadCursor(
      parsed.data as ChangeLogCacheEntry<ChangeLogItem>,
      readCategoryCursor(key),
    );
    memoryCache.set(key, cloneEntry(entry));
    return cloneEntry(entry) as ChangeLogCacheEntry<T>;
  } catch {
    return null;
  }
}

export function writeChangeLogCache<T extends ChangeLogItem>(
  storage: StorageLike | null,
  key: string,
  entry: Omit<ChangeLogCacheEntry<T>, "storedAt" | "totalCount" | "page" | "pageSize"> & {
    totalCount?: number;
    page?: number;
    pageSize?: number;
    storedAt?: number;
  },
) {
  const categoryCursor = readCategoryCursor(key);
  const lastReadId = Math.max(
    nonNegativeInteger(entry.lastReadId),
    categoryCursor,
  );
  const incomingItems = mergeChangeLogItems([], entry.items);
  const acknowledgedUnreadCount = incomingItems.filter(
    (item) => item.unread && item.id <= lastReadId,
  ).length;
  const safeEntry: ChangeLogCacheEntry<T> = {
    items: incomingItems.map((item) => (
      item.unread && item.id <= lastReadId ? { ...item, unread: false } : item
    )),
    hasMore: Boolean(entry.hasMore),
    unreadCount: Math.max(
      0,
      nonNegativeInteger(entry.unreadCount) - acknowledgedUnreadCount,
    ),
    lastReadId,
    totalCount: nonNegativeInteger(entry.totalCount ?? entry.items.length),
    page: positiveInteger(entry.page, 1),
    pageSize: positiveInteger(entry.pageSize, 50),
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

export function markChangeLogCategoryCachesRead(
  storage: (StorageLike & EnumerableStorageLike) | null,
  baseUrl: string,
  category: ChangeLogCacheCategory,
  throughId: number,
) {
  recordCategoryReadCursor(baseUrl, category, throughId);
  const prefix = categoryCacheKeyPrefix(baseUrl, category);
  const markedKeys = new Set<string>();
  for (const key of memoryCache.keys()) {
    if (key.startsWith(prefix)) {
      markChangeLogCacheRead(storage, key, throughId);
      markedKeys.add(key);
    }
  }
  if (!storage) return;
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key?.startsWith(prefix) && !markedKeys.has(key)) {
        markChangeLogCacheRead(storage, key, throughId);
      }
    }
  } catch {
    // Restricted browser contexts may reject storage enumeration.
  }
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
  categoryReadCursors.clear();
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
    && Number.isSafeInteger(entry.totalCount)
    && Number(entry.totalCount) >= 0
    && Number.isSafeInteger(entry.page)
    && Number(entry.page) >= 1
    && Number.isSafeInteger(entry.pageSize)
    && Number(entry.pageSize) >= 1
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

function categoryCacheKeyPrefix(baseUrl: string, category: ChangeLogCacheCategory) {
  return `${cacheKeyPrefix}v${changeLogCacheVersion}:${encodeURIComponent(normalizeBaseUrl(baseUrl))}:${category}:`;
}

function categoryReadCursorKey(baseUrl: string, category: ChangeLogCacheCategory) {
  return `${cacheKeyPrefix}v${changeLogCacheVersion}:read:${encodeURIComponent(normalizeBaseUrl(baseUrl))}:${category}`;
}

function categoryReadCursorKeyFromCacheKey(key: string) {
  const prefix = `${cacheKeyPrefix}v${changeLogCacheVersion}:`;
  if (!key.startsWith(prefix)) return null;
  const [encodedBaseUrl, category] = key.slice(prefix.length).split(":", 2);
  if (
    !encodedBaseUrl
    || !category
    || !(["upstream", "account_rate", "scheduling"] as string[]).includes(category)
  ) return null;
  return `${prefix}read:${encodedBaseUrl}:${category}`;
}

function readCategoryCursor(cacheKey: string) {
  const cursorKey = categoryReadCursorKeyFromCacheKey(cacheKey);
  if (!cursorKey) return 0;
  const marker = categoryReadCursors.get(cursorKey);
  if (!marker) return 0;
  if (Date.now() - marker.recordedAt <= categoryReadCursorMaxAgeMs) {
    return marker.cursor;
  }
  categoryReadCursors.delete(cursorKey);
  return 0;
}

function recordCategoryReadCursor(
  baseUrl: string,
  category: ChangeLogCacheCategory,
  throughId: number,
) {
  const cursorKey = categoryReadCursorKey(baseUrl, category);
  const current = categoryReadCursors.get(cursorKey)?.cursor ?? 0;
  categoryReadCursors.set(cursorKey, {
    cursor: Math.max(current, nonNegativeInteger(throughId)),
    recordedAt: Date.now(),
  });
}

function applyReadCursor<T extends ChangeLogItem>(
  entry: ChangeLogCacheEntry<T>,
  cursor: number,
): ChangeLogCacheEntry<T> {
  if (cursor <= entry.lastReadId) return cloneEntry(entry);
  const acknowledgedUnreadCount = entry.items.filter(
    (item) => item.unread && item.id <= cursor,
  ).length;
  return {
    ...cloneEntry(entry),
    items: entry.items.map((item) => (
      item.unread && item.id <= cursor ? { ...item, unread: false } : { ...item }
    )),
    unreadCount: Math.max(0, entry.unreadCount - acknowledgedUnreadCount),
    lastReadId: cursor,
  };
}

function positiveInteger(value: unknown, fallback: number) {
  return Number.isSafeInteger(value) && Number(value) >= 1 ? Number(value) : fallback;
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
