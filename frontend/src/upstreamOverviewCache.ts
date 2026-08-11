import type {
  UpstreamAccount,
  UpstreamChannel,
  UpstreamChannelsResponse,
  UpstreamChannelMonitor,
  UpstreamAccountPauseHold,
  UpstreamGroupOption,
  UpstreamType,
  PriorityInterval,
  PriorityAllocationStrategy,
} from "./types";

export const upstreamOverviewCacheVersion = 8;
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
    balance_source: nullableString(source.balance_source),
    balance_message: nullableString(source.balance_message),
    balance_checked_at: nullableString(source.balance_checked_at),
    recharge_adjusted_balance: nullableNumber(source.recharge_adjusted_balance),
    today_balance_used: nullableNumber(source.today_balance_used),
    today_balance_unit: nullableString(source.today_balance_unit),
    today_balance_status: nullableString(source.today_balance_status),
    today_balance_checked_at: nullableString(source.today_balance_checked_at),
    yesterday_balance_used: nullableNumber(source.yesterday_balance_used),
    yesterday_balance_unit: nullableString(source.yesterday_balance_unit),
    yesterday_balance_status: nullableString(source.yesterday_balance_status),
    yesterday_balance_checked_at: nullableString(source.yesterday_balance_checked_at),
    balance_guard_state: nullableString(source.balance_guard_state),
    balance_guard_basis: balanceGuardBasis(source.balance_guard_basis),
    balance_guard_value: nullableNumber(source.balance_guard_value),
    balance_guard_checked_at: nullableString(source.balance_guard_checked_at),
    balance_guard_paused_count: nullableNumber(source.balance_guard_paused_count) ?? undefined,
    channel_monitors: sanitizeChannelMonitors(source.channel_monitors),
    channel_monitor_count: nullableNumber(source.channel_monitor_count) ?? undefined,
    channel_monitor_status: nullableString(source.channel_monitor_status),
    channel_monitor_message: nullableString(source.channel_monitor_message),
    channel_monitor_checked_at: nullableString(source.channel_monitor_checked_at),
    channel_monitor_guard_state: nullableString(source.channel_monitor_guard_state),
    channel_monitor_unavailable_count: nullableNumber(source.channel_monitor_unavailable_count) ?? undefined,
    channel_monitor_recovery_count: nullableNumber(source.channel_monitor_recovery_count) ?? undefined,
    channel_monitor_guard_checked_at: nullableString(source.channel_monitor_guard_checked_at),
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
    upstream_api_key_record_id: nullableId(source.upstream_api_key_record_id),
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
    priority_tiebreak_order: nullableNumber(source.priority_tiebreak_order),
    priority_tiebreak_multiplier: nullableNumber(source.priority_tiebreak_multiplier),
    priority_assignment_when_disabled: optionalBoolean(source.priority_assignment_when_disabled),
    priority_assignment_when_disabled_effective: optionalBoolean(source.priority_assignment_when_disabled_effective),
    rate_pause_policy: source.rate_pause_policy === "inherit"
      || source.rate_pause_policy === "disabled"
      || source.rate_pause_policy === "custom"
      ? source.rate_pause_policy
      : undefined,
    rate_pause_effective_enabled: optionalBoolean(source.rate_pause_effective_enabled),
    rate_pause_effective_source: source.rate_pause_effective_source === "account"
      || source.rate_pause_effective_source === "priority_interval"
      || source.rate_pause_effective_source === "disabled"
      ? source.rate_pause_effective_source
      : undefined,
    rate_absolute_threshold: nullableNumber(source.rate_absolute_threshold),
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
    availability_check_mode: availabilityCheckMode(source.availability_check_mode),
    availability_monitor_id: nullableId(source.availability_monitor_id),
    availability_test_model: nullableString(source.availability_test_model),
    available_models: sanitizeAccountModels(source.available_models),
    available_models_status: nullableString(source.available_models_status),
    available_models_checked_at: nullableString(source.available_models_checked_at),
    availability_status: nullableString(source.availability_status),
    availability_unavailable_count: nullableNumber(source.availability_unavailable_count) ?? undefined,
    availability_recovery_count: nullableNumber(source.availability_recovery_count) ?? undefined,
    availability_checked_at: nullableString(source.availability_checked_at),
    availability_source: nullableString(source.availability_source),
    availability_message: nullableString(source.availability_message),
    auto_disabled_reason: nullableString(source.auto_disabled_reason),
    last_auto_disabled_at: nullableString(source.last_auto_disabled_at),
    active_pause_holds: sanitizePauseHolds(source.active_pause_holds),
    pause_owned_by_plugin: optionalBoolean(source.pause_owned_by_plugin),
    auto_restore_eligible: optionalBoolean(source.auto_restore_eligible),
    auto_pause_episode_id: nullableString(source.auto_pause_episode_id),
    auto_pause_channel_id: nullableId(source.auto_pause_channel_id),
    auto_paused_at: nullableString(source.auto_paused_at),
    balance_guard_restore_eligible: optionalBoolean(source.balance_guard_restore_eligible),
    balance_guard_channel_id: nullableId(source.balance_guard_channel_id),
    balance_guard_paused_at: nullableString(source.balance_guard_paused_at),
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
    balance_source: nullableString(source.balance_source),
    balance_message: nullableString(source.balance_message),
    balance_checked_at: nullableString(source.balance_checked_at),
    upstream_usage_amount: nullableNumber(source.upstream_usage_amount),
    upstream_usage_unit: nullableString(source.upstream_usage_unit),
    upstream_usage_checked_at: nullableString(source.upstream_usage_checked_at),
    today_upstream_usage_amount: nullableNumber(source.today_upstream_usage_amount),
    today_upstream_usage_unit: nullableString(source.today_upstream_usage_unit),
    today_upstream_usage_status: nullableString(source.today_upstream_usage_status),
    today_upstream_usage_source: nullableString(source.today_upstream_usage_source),
    today_upstream_usage_checked_at: nullableString(source.today_upstream_usage_checked_at),
    today_upstream_cost_cny: nullableNumber(source.today_upstream_cost_cny),
    today_sub2api_cost_cny: nullableNumber(source.today_sub2api_cost_cny),
    today_income_cny: nullableNumber(source.today_income_cny),
    today_consumption_cny: nullableNumber(source.today_consumption_cny),
    today_profit_cny: nullableNumber(source.today_profit_cny),
    today_sub2api_stats_status: nullableString(source.today_sub2api_stats_status),
    today_sub2api_stats_checked_at: nullableString(source.today_sub2api_stats_checked_at),
    last_used_at: nullableString(source.last_used_at),
    last_error: nullableString(source.last_error),
    last_discovered_at: nullableString(source.last_discovered_at),
    last_applied_at: nullableString(source.last_applied_at),
    created_at: nullableString(source.created_at),
    updated_at: nullableString(source.updated_at),
  };
}

function sanitizePauseHolds(value: unknown): UpstreamAccountPauseHold[] | undefined {
  if (!Array.isArray(value)) return undefined;
  return value.map((entry) => {
    const source = asRecord(entry);
    const reason = source ? nullableString(source.reason)?.trim() : undefined;
    if (!source || !reason) return null;
    const evidence = sanitizePauseEvidence(source.evidence);
    return {
      reason,
      triggered_at: nullableString(source.triggered_at),
      recovery_mode: nullableString(source.recovery_mode),
      scope_channel_id: nullableId(source.scope_channel_id),
      ...(evidence ? { evidence } : {}),
    };
  }).filter(isPresent);
}

function sanitizePauseEvidence(value: unknown): UpstreamAccountPauseHold["evidence"] {
  const source = asRecord(value);
  if (!source) return undefined;
  return {
    balance: nullableNumber(source.balance) ?? undefined,
    basis: nullableString(source.basis) ?? undefined,
    threshold: nullableNumber(source.threshold) ?? undefined,
    unit: nullableString(source.unit) ?? undefined,
    key_status: nullableString(source.key_status) ?? undefined,
    group_status: nullableString(source.group_status) ?? undefined,
    monitor_status: nullableString(source.monitor_status) ?? undefined,
    unavailable_count: nullableNumber(source.unavailable_count) ?? undefined,
    test_status: nullableString(source.test_status) ?? undefined,
    test_purpose: nullableString(source.test_purpose) ?? undefined,
    test_attempts: nullableNumber(source.test_attempts) ?? undefined,
    max_test_attempts: nullableNumber(source.max_test_attempts) ?? undefined,
    baseline_multiplier: nullableNumber(source.baseline_multiplier) ?? undefined,
    mode: nullableString(source.mode) ?? undefined,
    observed_multiplier: nullableNumber(source.observed_multiplier) ?? undefined,
    absolute_threshold: nullableNumber(source.absolute_threshold) ?? undefined,
    increase_percent: nullableNumber(source.increase_percent) ?? undefined,
    threshold_percent: nullableNumber(source.threshold_percent) ?? undefined,
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
    const allocationStrategy: PriorityAllocationStrategy = source?.allocation_strategy === "fixed_step"
      ? "fixed_step"
      : "cost_optimized";
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
      allocation_strategy: allocationStrategy,
      rate_pause_enabled: source.rate_pause_enabled === true,
      rate_absolute_threshold: nullableNumber(source.rate_absolute_threshold) ?? 1,
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

function sanitizeChannelMonitors(value: unknown): UpstreamChannelMonitor[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const source = asRecord(entry);
    const id = source ? nullableId(source.id) : undefined;
    if (!source || id === null || id === undefined) return null;
    return {
      id,
      name: nullableString(source.name),
      provider: nullableString(source.provider),
      group_name: nullableString(source.group_name),
      primary_model: nullableString(source.primary_model),
      primary_status: nullableString(source.primary_status),
      primary_latency_ms: nullableNumber(source.primary_latency_ms),
      primary_ping_latency_ms: nullableNumber(source.primary_ping_latency_ms),
      availability_7d: nullableNumber(source.availability_7d),
      availability_window: availabilityWindow(source.availability_window),
      extra_models: sanitizeMonitorModels(source.extra_models),
      timeline: sanitizeMonitorTimeline(source.timeline),
    };
  }).filter(isPresent);
}

function sanitizeAccountModels(value: unknown) {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.map((entry) => {
    const source = asRecord(entry);
    const id = source ? nullableString(source.id)?.trim().slice(0, 160) : undefined;
    if (!id || seen.has(id)) return null;
    seen.add(id);
    const displayName = nullableString(source?.display_name)?.trim().slice(0, 200) || id;
    return { id, display_name: displayName };
  }).filter(isPresent);
}

function sanitizeMonitorModels(value: unknown) {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const source = asRecord(entry);
    if (!source) return null;
    return {
      name: nullableString(source.name) ?? nullableString(source.model),
      status: nullableString(source.status),
      latency_ms: nullableNumber(source.latency_ms),
    };
  }).filter(isPresent);
}

function sanitizeMonitorTimeline(value: unknown) {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const source = asRecord(entry);
    if (!source) return null;
    return {
      time: nullableString(source.time) ?? nullableString(source.checked_at),
      status: nullableString(source.status),
      latency_ms: nullableNumber(source.latency_ms),
    };
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

function balanceGuardBasis(value: unknown): "wallet" | "recharge_adjusted" | null | undefined {
  if (value === null) return null;
  return value === "wallet" || value === "recharge_adjusted" ? value : undefined;
}

function availabilityWindow(value: unknown): "24h" | "7d" | undefined {
  return value === "24h" || value === "7d" ? value : undefined;
}

function upstreamType(value: unknown): UpstreamType | null | undefined {
  if (value === null) return null;
  return value === "auto" || value === "newapi" || value === "sub2api" ? value : undefined;
}

function resolvedUpstreamType(value: unknown): "newapi" | "sub2api" | null | undefined {
  if (value === null) return null;
  return value === "newapi" || value === "sub2api" ? value : undefined;
}

function availabilityCheckMode(value: unknown): "channel_monitor" | "independent_model" | undefined {
  return value === "channel_monitor" || value === "independent_model" ? value : undefined;
}

function isPresent<T>(value: T | null): value is T {
  return value !== null;
}
