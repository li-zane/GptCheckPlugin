import type {
  UpstreamAccount,
  UpstreamChannel,
  UpstreamChannelsResponse,
  UpstreamGroupOption,
  UpstreamType,
  PriorityInterval,
} from "./types";

export const upstreamOverviewCacheVersion = 3;
const cacheKeyPrefix = "sub2api-at-upstream-overview:";

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem" | "key" | "length">;
type JsonRecord = Record<string, unknown>;

export function getUpstreamOverviewSessionStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function upstreamOverviewCacheScope(baseUrl: string) {
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

export function upstreamOverviewCacheKey(baseUrl: string) {
  return `${cacheKeyPrefix}v${upstreamOverviewCacheVersion}:${encodeURIComponent(upstreamOverviewCacheScope(baseUrl))}`;
}

export function readUpstreamOverviewCache(storage: StorageLike | null, baseUrl: string) {
  if (!storage) return null;
  try {
    const raw = storage.getItem(upstreamOverviewCacheKey(baseUrl));
    if (!raw) return null;
    const envelope = asRecord(JSON.parse(raw));
    if (!envelope || envelope.version !== upstreamOverviewCacheVersion) return null;
    if (envelope.scope !== upstreamOverviewCacheScope(baseUrl)) return null;
    return sanitizeUpstreamOverview(envelope.data);
  } catch {
    return null;
  }
}

export function writeUpstreamOverviewCache(
  storage: StorageLike | null,
  baseUrl: string,
  response: UpstreamChannelsResponse,
) {
  const safeResponse = sanitizeUpstreamOverview(response);
  if (!safeResponse) return null;
  if (!storage) return safeResponse;
  try {
    storage.setItem(upstreamOverviewCacheKey(baseUrl), JSON.stringify({
      version: upstreamOverviewCacheVersion,
      scope: upstreamOverviewCacheScope(baseUrl),
      stored_at: Date.now(),
      data: safeResponse,
    }));
  } catch {
    // The in-memory cache remains usable when storage is unavailable or full.
  }
  return safeResponse;
}

export function clearUpstreamOverviewCache(storage: StorageLike | null, baseUrl?: string) {
  if (!storage) return;
  try {
    if (baseUrl !== undefined) {
      storage.removeItem(upstreamOverviewCacheKey(baseUrl));
      return;
    }
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

/**
 * Persist only display state. Credential values and hints are intentionally
 * absent from this allowlist even if a future API response includes them.
 */
export function sanitizeUpstreamOverview(value: unknown): UpstreamChannelsResponse | null {
  const source = asRecord(value);
  if (!source || !Array.isArray(source.channels) || !Array.isArray(source.unassigned_accounts)) return null;
  return {
    local_recharge_multiplier: nullableNumber(source.local_recharge_multiplier),
    local_recharge_source: nullableString(source.local_recharge_source),
    local_recharge_status: nullableString(source.local_recharge_status),
    priority_intervals: sanitizePriorityIntervals(source.priority_intervals),
    channels: source.channels.map(sanitizeChannel).filter(isPresent),
    unassigned_accounts: source.unassigned_accounts.map(sanitizeAccount).filter(isPresent),
  };
}

export function upstreamOverviewHasLiveMutationData(value: UpstreamChannelsResponse | null | undefined) {
  if (!value) return false;
  const accounts = [
    ...value.channels.flatMap((channel) => channel.accounts || []),
    ...value.unassigned_accounts,
  ];
  return value.channels.every((channel) => typeof channel.probe_enabled === "boolean")
    && accounts.every((account) => /^[a-f0-9]{64}$/.test(String(account.identity_fingerprint || "")));
}

function sanitizeChannel(value: unknown): UpstreamChannel | null {
  const source = asRecord(value);
  const id = source ? nullableId(source.id) : undefined;
  if (!source || id === undefined || id === null) return null;
  return {
    id,
    display_name: nullableString(source.display_name),
    base_url: nullableString(source.base_url),
    canonical_base_url: nullableString(source.canonical_base_url),
    management_base_url: nullableString(source.management_base_url),
    upstream_type: upstreamType(source.upstream_type),
    probe_enabled: optionalBoolean(source.probe_enabled),
    resolved_upstream_type: resolvedUpstreamType(source.resolved_upstream_type),
    upstream_user_id: nullableString(source.upstream_user_id),
    access_token_set: optionalBoolean(source.access_token_set),
    refresh_token_set: optionalBoolean(source.refresh_token_set),
    manual_recharge_multiplier: nullableNumber(source.manual_recharge_multiplier),
    discovered_recharge_multiplier: nullableNumber(source.discovered_recharge_multiplier),
    effective_recharge_multiplier: nullableNumber(source.effective_recharge_multiplier),
    recharge_multiplier_source: nullableString(source.recharge_multiplier_source),
    recharge_multiplier_status: nullableString(source.recharge_multiplier_status),
    group_options: sanitizeGroupOptions(source.group_options),
    balance_remaining: nullableNumber(source.balance_remaining),
    balance_total: nullableNumber(source.balance_total),
    balance_used: nullableNumber(source.balance_used),
    balance_unit: nullableString(source.balance_unit),
    balance_status: nullableString(source.balance_status),
    balance_message: nullableString(source.balance_message),
    balance_checked_at: nullableString(source.balance_checked_at),
    today_balance_used: nullableNumber(source.today_balance_used),
    today_balance_unit: nullableString(source.today_balance_unit),
    today_balance_status: nullableString(source.today_balance_status),
    today_balance_checked_at: nullableString(source.today_balance_checked_at),
    yesterday_balance_used: nullableNumber(source.yesterday_balance_used),
    yesterday_balance_unit: nullableString(source.yesterday_balance_unit),
    yesterday_balance_status: nullableString(source.yesterday_balance_status),
    yesterday_balance_checked_at: nullableString(source.yesterday_balance_checked_at),
    status: nullableString(source.status),
    message: nullableString(source.message),
    checked_at: nullableString(source.checked_at),
    last_error: nullableString(source.last_error),
    last_discovered_at: nullableString(source.last_discovered_at),
    created_at: nullableString(source.created_at),
    updated_at: nullableString(source.updated_at),
    account_count: nullableNumber(source.account_count) ?? undefined,
    accounts: Array.isArray(source.accounts) ? source.accounts.map(sanitizeAccount).filter(isPresent) : [],
  };
}

function sanitizeAccount(value: unknown): UpstreamAccount | null {
  const source = asRecord(value);
  const accountId = source ? nullableId(source.sub2api_account_id) : undefined;
  if (!source || accountId === undefined || accountId === null) return null;
  return {
    sub2api_account_id: accountId,
    channel_id: nullableId(source.channel_id),
    remote_name: nullableString(source.remote_name),
    remote_platform: nullableString(source.remote_platform),
    remote_account_type: nullableString(source.remote_account_type),
    remote_status: nullableString(source.remote_status),
    remote_schedulable: optionalBoolean(source.remote_schedulable),
    priority: nullableNumber(source.priority),
    desired_priority: nullableNumber(source.desired_priority),
    priority_interval_id: nullableId(source.priority_interval_id),
    priority_interval_name: nullableString(source.priority_interval_name),
    priority_sync_status: nullableString(source.priority_sync_status),
    priority_sync_error: nullableString(source.priority_sync_error),
    composite_multiplier: nullableNumber(source.composite_multiplier),
    managed: optionalBoolean(source.managed),
    base_url: nullableString(source.base_url),
    upstream_type: upstreamType(source.upstream_type),
    resolved_upstream_type: resolvedUpstreamType(source.resolved_upstream_type),
    detected_upstream_type: resolvedUpstreamType(source.detected_upstream_type),
    upstream_user_id: nullableString(source.upstream_user_id),
    selected_group_id: nullableString(source.selected_group_id),
    selected_group_name: nullableString(source.selected_group_name),
    api_key_set: optionalBoolean(source.api_key_set),
    access_token_set: optionalBoolean(source.access_token_set),
    manual_group_multiplier: nullableNumber(source.manual_group_multiplier),
    manual_recharge_multiplier: nullableNumber(source.manual_recharge_multiplier),
    group_options: sanitizeGroupOptions(source.group_options),
    discovered_group_multiplier: nullableNumber(source.discovered_group_multiplier),
    effective_group_multiplier: nullableNumber(source.effective_group_multiplier),
    group_multiplier_source: nullableString(source.group_multiplier_source),
    group_multiplier_status: nullableString(source.group_multiplier_status),
    upstream_key_status: nullableString(source.upstream_key_status),
    upstream_group_status: nullableString(source.upstream_group_status),
    upstream_health_invalid_count: nullableNumber(source.upstream_health_invalid_count) ?? undefined,
    upstream_health_checked_at: nullableString(source.upstream_health_checked_at),
    upstream_key_checked_at: nullableString(source.upstream_key_checked_at),
    upstream_group_checked_at: nullableString(source.upstream_group_checked_at),
    auto_disabled_reason: nullableString(source.auto_disabled_reason),
    last_auto_disabled_at: nullableString(source.last_auto_disabled_at),
    discovered_recharge_multiplier: nullableNumber(source.discovered_recharge_multiplier),
    effective_recharge_multiplier: nullableNumber(source.effective_recharge_multiplier),
    recharge_multiplier_source: nullableString(source.recharge_multiplier_source),
    recharge_multiplier_status: nullableString(source.recharge_multiplier_status),
    local_recharge_multiplier: nullableNumber(source.local_recharge_multiplier),
    local_recharge_source: nullableString(source.local_recharge_source),
    local_recharge_status: nullableString(source.local_recharge_status),
    current_rate: nullableNumber(source.current_rate),
    target_rate: nullableNumber(source.target_rate),
    would_change: optionalBoolean(source.would_change),
    balance_remaining: nullableNumber(source.balance_remaining),
    balance_total: nullableNumber(source.balance_total),
    balance_used: nullableNumber(source.balance_used),
    balance_unit: nullableString(source.balance_unit),
    balance_status: nullableString(source.balance_status),
    balance_message: nullableString(source.balance_message),
    balance_checked_at: nullableString(source.balance_checked_at),
    upstream_usage_amount: nullableNumber(source.upstream_usage_amount),
    upstream_usage_unit: nullableString(source.upstream_usage_unit),
    upstream_usage_checked_at: nullableString(source.upstream_usage_checked_at),
    last_error: nullableString(source.last_error),
    last_discovered_at: nullableString(source.last_discovered_at),
    last_applied_at: nullableString(source.last_applied_at),
    created_at: nullableString(source.created_at),
    updated_at: nullableString(source.updated_at),
  };
}

function sanitizePriorityIntervals(value: unknown): PriorityInterval[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const source = asRecord(entry);
    const id = source ? nullableId(source.id) : undefined;
    const name = source ? nullableString(source.name) : undefined;
    const startPriority = source ? nullableNumber(source.start_priority) : undefined;
    const endPriority = source ? nullableNumber(source.end_priority) : undefined;
    const step = source ? nullableNumber(source.step) : undefined;
    if (
      !source
      || id === null || id === undefined
      || !name
      || startPriority === null || startPriority === undefined
      || endPriority === null || endPriority === undefined
      || step === null || step === undefined
    ) return null;
    return {
      id,
      name,
      start_priority: startPriority,
      end_priority: endPriority,
      step,
      account_count: nullableNumber(source.account_count) ?? undefined,
      effective_step: nullableNumber(source.effective_step) ?? undefined,
    };
  }).filter(isPresent);
}

function sanitizeGroupOptions(value: unknown): UpstreamGroupOption[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const source = asRecord(entry);
    const id = source && typeof source.id === "string" ? source.id : null;
    const name = source && typeof source.name === "string" ? source.name : null;
    const multiplier = source ? nullableNumber(source.multiplier) : undefined;
    if (id === null || name === null || multiplier === null || multiplier === undefined) return null;
    return { id, name, multiplier };
  }).filter(isPresent);
}

function asRecord(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : null;
}

function nullableString(value: unknown): string | null | undefined {
  if (value === null) return null;
  return typeof value === "string" ? value : undefined;
}

function nullableNumber(value: unknown): number | null | undefined {
  if (value === null) return null;
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function nullableId(value: unknown): number | string | null | undefined {
  if (value === null) return null;
  if (typeof value === "number" && Number.isSafeInteger(value) && value > 0) return value;
  return typeof value === "string" ? value : undefined;
}

function optionalBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function upstreamType(value: unknown): UpstreamType | null | undefined {
  if (value === null) return null;
  return value === "auto" || value === "newapi" || value === "sub2api" ? value : undefined;
}

function resolvedUpstreamType(value: unknown): "newapi" | "sub2api" | null | undefined {
  if (value === null) return null;
  return value === "newapi" || value === "sub2api" ? value : undefined;
}

function isPresent<T>(value: T | null): value is T {
  return value !== null;
}
