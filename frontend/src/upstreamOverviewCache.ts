import type {
  ApiAccount,
  Upstream,
  UpstreamOverviewResponse,
  UpstreamMonitor,
  ApiAccountPauseHold,
  UpstreamGroupOption,
  UpstreamType,
  PriorityInterval,
  PriorityAllocationStrategy,
} from "./domain";

export const upstreamOverviewCacheVersion = 9;
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
  response: UpstreamOverviewResponse,
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
export function sanitizeUpstreamOverview(value: unknown): UpstreamOverviewResponse | null {
  const source = asRecord(value);
  if (!source || !Array.isArray(source.upstreams) || !Array.isArray(source.unassigned_accounts)) return null;
  return {
    management_recharge_multiplier: nullableNumber(source.management_recharge_multiplier),
    management_recharge_source: nullableString(source.management_recharge_source),
    management_recharge_status: nullableString(source.management_recharge_status),
    priority_intervals: sanitizePriorityIntervals(source.priority_intervals),
    upstreams: source.upstreams.map(sanitizeUpstream).filter(isPresent),
    unassigned_accounts: source.unassigned_accounts.map(sanitizeAccount).filter(isPresent),
  };
}

export function upstreamOverviewHasLiveMutationData(value: UpstreamOverviewResponse | null | undefined) {
  if (!value) return false;
  const accounts = [
    ...value.upstreams.flatMap((upstream) => upstream.accounts || []),
    ...value.unassigned_accounts,
  ];
  return value.upstreams.every((upstream) => typeof upstream.probe_enabled === "boolean")
    && accounts.every((account) => /^[a-f0-9]{64}$/.test(String(account.identity_fingerprint || "")));
}

function sanitizeUpstream(value: unknown): Upstream | null {
  const source = asRecord(value);
  const id = source ? nullableUuid(source.upstream_id) : undefined;
  if (!source || id === undefined || id === null) return null;
  return {
    upstream_id: id,
    display_name: nullableString(source.display_name),
    api_endpoint_url: nullableString(source.api_endpoint_url),
    management_url: nullableString(source.management_url),
    platform_type: upstreamType(source.platform_type),
    probe_enabled: optionalBoolean(source.probe_enabled),
    resolved_platform_type: resolvedUpstreamType(source.resolved_platform_type),
    upstream_user_id: nullableString(source.upstream_user_id),
    access_token_set: optionalBoolean(source.access_token_set),
    refresh_token_set: optionalBoolean(source.refresh_token_set),
    login_credentials_set: optionalBoolean(source.login_credentials_set),
    upstream_recharge_multiplier_override: nullableNumber(source.upstream_recharge_multiplier_override),
    discovered_upstream_recharge_multiplier: nullableNumber(source.discovered_upstream_recharge_multiplier),
    upstream_recharge_multiplier: nullableNumber(source.upstream_recharge_multiplier),
    recharge_multiplier_source: nullableString(source.recharge_multiplier_source),
    recharge_multiplier_status: nullableString(source.recharge_multiplier_status),
    group_options: sanitizeGroupOptions(source.group_options),
    wallet_balance_usd: nullableNumber(source.wallet_balance_usd),
    wallet_total_usd: nullableNumber(source.wallet_total_usd),
    wallet_used_usd: nullableNumber(source.wallet_used_usd),
    balance_unit: nullableString(source.balance_unit),
    balance_status: nullableString(source.balance_status),
    balance_source: nullableString(source.balance_source),
    balance_message: nullableString(source.balance_message),
    balance_checked_at: nullableString(source.balance_checked_at),
    actual_balance_cny: nullableNumber(source.actual_balance_cny),
    today_upstream_wallet_cost_usd: nullableNumber(source.today_upstream_wallet_cost_usd),
    today_balance_unit: nullableString(source.today_balance_unit),
    today_balance_status: nullableString(source.today_balance_status),
    today_balance_checked_at: nullableString(source.today_balance_checked_at),
    yesterday_upstream_wallet_cost_usd: nullableNumber(source.yesterday_upstream_wallet_cost_usd),
    yesterday_balance_unit: nullableString(source.yesterday_balance_unit),
    yesterday_balance_status: nullableString(source.yesterday_balance_status),
    yesterday_balance_checked_at: nullableString(source.yesterday_balance_checked_at),
    balance_guard_state: nullableString(source.balance_guard_state),
    balance_guard_basis: balanceGuardBasis(source.balance_guard_basis),
    balance_guard_value: nullableNumber(source.balance_guard_value),
    balance_guard_checked_at: nullableString(source.balance_guard_checked_at),
    balance_guard_paused_count: nullableNumber(source.balance_guard_paused_count) ?? undefined,
    upstream_monitors: sanitizeUpstreamMonitors(source.upstream_monitors),
    upstream_monitor_count: nullableNumber(source.upstream_monitor_count) ?? undefined,
    upstream_monitor_status: nullableString(source.upstream_monitor_status),
    upstream_monitor_message: nullableString(source.upstream_monitor_message),
    upstream_monitor_checked_at: nullableString(source.upstream_monitor_checked_at),
    upstream_monitor_guard_state: nullableString(source.upstream_monitor_guard_state),
    upstream_monitor_unavailable_count: nullableNumber(source.upstream_monitor_unavailable_count) ?? undefined,
    upstream_monitor_recovery_count: nullableNumber(source.upstream_monitor_recovery_count) ?? undefined,
    upstream_monitor_guard_checked_at: nullableString(source.upstream_monitor_guard_checked_at),
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

function sanitizeAccount(value: unknown): ApiAccount | null {
  const source = asRecord(value);
  const accountId = source ? nullableId(source.management_account_id) : undefined;
  if (!source || accountId === undefined || accountId === null) return null;
  return {
    management_account_id: accountId,
    remote_key_id: nullableId(source.remote_key_id),
    upstream_api_key_id: nullableId(source.upstream_api_key_id),
    upstream_id: nullableUuid(source.upstream_id),
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
    upstream_actual_multiplier: nullableNumber(source.upstream_actual_multiplier),
    managed: optionalBoolean(source.managed),
    api_endpoint_url: nullableString(source.api_endpoint_url),
    platform_type: upstreamType(source.platform_type),
    resolved_platform_type: resolvedUpstreamType(source.resolved_platform_type),
    detected_platform_type: resolvedUpstreamType(source.detected_platform_type),
    upstream_user_id: nullableString(source.upstream_user_id),
    selected_group_id: nullableString(source.selected_group_id),
    selected_group_name: nullableString(source.selected_group_name),
    api_key_set: optionalBoolean(source.api_key_set),
    access_token_set: optionalBoolean(source.access_token_set),
    upstream_group_multiplier_override: nullableNumber(source.upstream_group_multiplier_override),
    upstream_recharge_multiplier_override: nullableNumber(source.upstream_recharge_multiplier_override),
    group_options: sanitizeGroupOptions(source.group_options),
    discovered_upstream_group_multiplier: nullableNumber(source.discovered_upstream_group_multiplier),
    upstream_group_multiplier: nullableNumber(source.upstream_group_multiplier),
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
    auto_pause_upstream_id: nullableString(source.auto_pause_upstream_id),
    auto_paused_at: nullableString(source.auto_paused_at),
    balance_guard_restore_eligible: optionalBoolean(source.balance_guard_restore_eligible),
    balance_guard_upstream_id: nullableString(source.balance_guard_upstream_id),
    balance_guard_paused_at: nullableString(source.balance_guard_paused_at),
    discovered_upstream_recharge_multiplier: nullableNumber(source.discovered_upstream_recharge_multiplier),
    upstream_recharge_multiplier: nullableNumber(source.upstream_recharge_multiplier),
    recharge_multiplier_source: nullableString(source.recharge_multiplier_source),
    recharge_multiplier_status: nullableString(source.recharge_multiplier_status),
    management_recharge_multiplier: nullableNumber(source.management_recharge_multiplier),
    management_recharge_source: nullableString(source.management_recharge_source),
    management_recharge_status: nullableString(source.management_recharge_status),
    management_billing_multiplier: nullableNumber(source.management_billing_multiplier),
    expected_management_billing_multiplier: nullableNumber(source.expected_management_billing_multiplier),
    would_change: optionalBoolean(source.would_change),
    wallet_balance_usd: nullableNumber(source.wallet_balance_usd),
    wallet_total_usd: nullableNumber(source.wallet_total_usd),
    wallet_used_usd: nullableNumber(source.wallet_used_usd),
    balance_unit: nullableString(source.balance_unit),
    balance_status: nullableString(source.balance_status),
    balance_source: nullableString(source.balance_source),
    balance_message: nullableString(source.balance_message),
    balance_checked_at: nullableString(source.balance_checked_at),
    upstream_wallet_cost_usd: nullableNumber(source.upstream_wallet_cost_usd),
    upstream_usage_unit: nullableString(source.upstream_usage_unit),
    upstream_usage_checked_at: nullableString(source.upstream_usage_checked_at),
    today_upstream_wallet_cost_usd: nullableNumber(source.today_upstream_wallet_cost_usd),
    today_upstream_usage_unit: nullableString(source.today_upstream_usage_unit),
    today_upstream_usage_status: nullableString(source.today_upstream_usage_status),
    today_upstream_usage_source: nullableString(source.today_upstream_usage_source),
    today_upstream_usage_checked_at: nullableString(source.today_upstream_usage_checked_at),
    today_upstream_actual_cost_cny: nullableNumber(source.today_upstream_actual_cost_cny),
    today_management_account_cost_cny: nullableNumber(source.today_management_account_cost_cny),
    today_actual_income_cny: nullableNumber(source.today_actual_income_cny),
    today_consumption_cny: nullableNumber(source.today_consumption_cny),
    today_profit_cny: nullableNumber(source.today_profit_cny),
    today_management_site_stats_status: nullableString(source.today_management_site_stats_status),
    today_management_site_stats_checked_at: nullableString(source.today_management_site_stats_checked_at),
    last_used_at: nullableString(source.last_used_at),
    last_error: nullableString(source.last_error),
    last_discovered_at: nullableString(source.last_discovered_at),
    last_applied_at: nullableString(source.last_applied_at),
    created_at: nullableString(source.created_at),
    updated_at: nullableString(source.updated_at),
  };
}

function sanitizePauseHolds(value: unknown): ApiAccountPauseHold[] | undefined {
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
      scope_upstream_id: nullableString(source.scope_upstream_id),
      ...(evidence ? { evidence } : {}),
    };
  }).filter(isPresent);
}

function sanitizePauseEvidence(value: unknown): ApiAccountPauseHold["evidence"] {
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

function sanitizeUpstreamMonitors(value: unknown): UpstreamMonitor[] {
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

function nullableUuid(value: unknown): string | null | undefined {
  const normalized = nullableString(value);
  if (normalized === undefined || normalized === null) return normalized;
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(normalized)
    ? normalized.toLowerCase()
    : null;
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

function availabilityCheckMode(value: unknown): "upstream_monitor" | "independent_model" | undefined {
  return value === "upstream_monitor" || value === "independent_model" ? value : undefined;
}

function isPresent<T>(value: T | null): value is T {
  return value !== null;
}
