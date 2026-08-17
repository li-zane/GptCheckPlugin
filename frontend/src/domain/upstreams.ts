import type { AccountLivenessModel } from "./accounts";
export type UpstreamType = "auto" | "newapi" | "sub2api";

export type UpstreamGroupOption = {
  id: string;
  name: string;
  multiplier: number;
};

export type PriorityAllocationStrategy = "cost_optimized" | "fixed_step";

export type PriorityInterval = {
  id: number | string;
  name: string;
  start_priority: number;
  /** Exclusive upper bound. */
  end_priority: number;
  step: number;
  allocation_strategy: PriorityAllocationStrategy;
  account_count?: number;
  effective_step?: number;
  rate_pause_enabled: boolean;
  rate_absolute_threshold: number;
  created_at?: string | null;
  updated_at?: string | null;
};

export type PriorityIntervalInput = {
  name: string;
  start_priority: number;
  end_priority: number;
  step: number;
  allocation_strategy: PriorityAllocationStrategy;
  rate_pause_enabled: boolean;
  rate_absolute_threshold: number;
};

export type PriorityIntervalAssignment = {
  priority_interval_id: number | string | null;
  expected_identity_fingerprint: string;
  confirm_identity_rebind?: boolean;
};

export type PriorityTieMoveInput = {
  direction: "up" | "down";
  expected_identity_fingerprint: string;
};

export type PriorityRebalanceResult = {
  considered?: number;
  message?: string;
  total?: number;
  updated?: number;
  unchanged?: number;
  failed?: number;
};

/**
 * A management-site API account enriched with its optional local upstream
 * management state. Most enrichment fields are optional so an unmanaged or
 * not-yet-discovered account remains renderable.
 */
export type ApiAccountPauseHold = {
  reason: string;
  triggered_at?: string | null;
  recovery_mode?: string | null;
  scope_upstream_id?: string | null;
  evidence?: {
    balance?: number;
    basis?: string;
    threshold?: number;
    unit?: string;
    key_status?: string;
    group_status?: string;
    monitor_status?: string;
    unavailable_count?: number;
    test_status?: string;
    test_purpose?: string;
    test_attempts?: number;
    max_test_attempts?: number;
    baseline_multiplier?: number;
    mode?: string;
    observed_multiplier?: number;
    absolute_threshold?: number;
    increase_percent?: number;
    threshold_percent?: number;
  } | null;
};

export type ApiAccount = {
  management_account_id: number | string;
  /** Stable, non-secret identity used to reject stale mutations after a remote ID is reused. */
  identity_fingerprint?: string;
  identity_binding_status?: "unmanaged" | "unbound" | "bound" | "mismatch";
  identity_rebind_required?: boolean;
  api_key_origin_rebind_required?: boolean;
  upstream_identity_rebind_required?: boolean;
  remote_key_id?: number | string | null;
  upstream_api_key_id?: number | string | null;
  upstream_id?: string | null;
  upstream_name?: string | null;
  remote_name?: string | null;
  remote_platform?: string | null;
  remote_account_type?: string | null;
  remote_status?: string | null;
  remote_schedulable?: boolean | null;
  priority?: number | null;
  remote_present?: boolean;
  remote_snapshot_updated_at?: string | null;
  remote_missing_at?: string | null;
  desired_priority?: number | null;
  priority_interval_id?: number | string | null;
  priority_interval_name?: string | null;
  priority_sync_status?: string | null;
  priority_sync_error?: string | null;
  priority_tiebreak_order?: number | null;
  priority_tiebreak_multiplier?: number | null;
  priority_assignment_when_disabled?: boolean | null;
  priority_assignment_when_disabled_effective?: boolean;
  rate_pause_policy?: "inherit" | "disabled" | "custom";
  rate_pause_effective_enabled?: boolean;
  rate_pause_effective_source?: "account" | "priority_interval" | "disabled";
  rate_absolute_threshold?: number | null;
  upstream_actual_multiplier?: number | null;
  managed?: boolean;
  api_endpoint_url?: string | null;
  platform_type?: UpstreamType | null;
  /** Protocol resolved by the backend while configured mode remains `auto`. */
  resolved_platform_type?: Exclude<UpstreamType, "auto"> | null;
  detected_platform_type?: Exclude<UpstreamType, "auto"> | null;
  upstream_user_id?: string | null;
  selected_group_id?: string | null;
  selected_group_name?: string | null;
  api_key_set?: boolean;
  api_key_hint?: string | null;
  access_token_set?: boolean;
  upstream_group_multiplier_override?: number | null;
  upstream_recharge_multiplier_override?: number | null;
  group_options?: UpstreamGroupOption[] | null;
  discovered_upstream_group_multiplier?: number | null;
  upstream_group_multiplier?: number | null;
  group_multiplier_source?: string | null;
  group_multiplier_status?: string | null;
  upstream_key_status?: string | null;
  upstream_group_status?: string | null;
  upstream_health_invalid_count?: number;
  upstream_health_checked_at?: string | null;
  upstream_key_checked_at?: string | null;
  upstream_group_checked_at?: string | null;
  availability_check_mode?: "upstream_monitor" | "independent_model" | "disabled";
  availability_monitor_id?: number | string | null;
  availability_test_model?: string | null;
  available_models?: AccountLivenessModel[] | null;
  available_models_status?: string | null;
  available_models_checked_at?: string | null;
  availability_status?: string | null;
  availability_unavailable_count?: number;
  availability_recovery_count?: number;
  availability_checked_at?: string | null;
  availability_source?: string | null;
  availability_message?: string | null;
  auto_disabled_reason?: string | null;
  last_auto_disabled_at?: string | null;
  active_pause_holds?: ApiAccountPauseHold[];
  pause_owned_by_plugin?: boolean;
  auto_restore_eligible?: boolean;
  auto_pause_episode_id?: string | null;
  auto_pause_upstream_id?: string | null;
  auto_paused_at?: string | null;
  balance_guard_restore_eligible?: boolean;
  balance_guard_upstream_id?: string | null;
  balance_guard_paused_at?: string | null;
  discovered_upstream_recharge_multiplier?: number | null;
  upstream_recharge_multiplier?: number | null;
  recharge_multiplier_source?: string | null;
  recharge_multiplier_status?: string | null;
  management_recharge_multiplier?: number | null;
  management_recharge_source?: string | null;
  management_recharge_status?: string | null;
  management_billing_multiplier?: number | null;
  expected_management_billing_multiplier?: number | null;
  would_change?: boolean;
  wallet_balance_usd?: number | null;
  wallet_total_usd?: number | null;
  wallet_used_usd?: number | null;
  balance_unit?: string | null;
  balance_status?: string | null;
  balance_source?: string | null;
  balance_message?: string | null;
  balance_checked_at?: string | null;
  upstream_wallet_cost_usd?: number | null;
  upstream_usage_unit?: string | null;
  upstream_usage_checked_at?: string | null;
  today_upstream_wallet_cost_usd?: number | null;
  today_upstream_usage_unit?: string | null;
  today_upstream_usage_status?: string | null;
  today_upstream_usage_source?: string | null;
  today_upstream_usage_checked_at?: string | null;
  today_upstream_actual_cost_cny?: number | null;
  today_management_account_cost_cny?: number | null;
  today_actual_income_cny?: number | null;
  today_consumption_cny?: number | null;
  today_profit_cny?: number | null;
  today_management_site_stats_status?: string | null;
  today_management_site_stats_checked_at?: string | null;
  /** Management-site-reported last use of this key. */
  last_used_at?: string | null;
  last_error?: string | null;
  last_discovered_at?: string | null;
  last_applied_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type ApiAccountUpdate = {
  expected_identity_fingerprint: string;
  upstream_id?: string | null;
  remote_name?: string | null;
  api_endpoint_url?: string | null;
  platform_type?: UpstreamType;
  upstream_user_id?: string | null;
  selected_group_id?: string | null;
  selected_group_name?: string | null;
  api_key?: string;
  access_token?: string;
  clear_access_token?: boolean;
  confirm_credential_rebind?: boolean;
  confirm_identity_rebind?: boolean;
  confirm_upstream_identity_rebind?: boolean;
  upstream_group_multiplier_override?: number | null;
  upstream_recharge_multiplier_override?: number | null;
  priority_assignment_when_disabled?: boolean | null;
  rate_pause_policy?: "inherit" | "disabled" | "custom";
  rate_absolute_threshold?: number | null;
  availability_check_mode?: "upstream_monitor" | "independent_model" | "disabled";
  availability_monitor_id?: number | string | null;
  availability_test_model?: string | null;
};

export type ApiAccountAvailabilityTestResult = {
  account: ApiAccount;
  policy_action?: "hold" | "clear" | null;
  policy_status?: string | null;
  policy_error?: string | null;
  evidence: Record<string, unknown>;
};

export type ApiAccountConnectionTestResult = {
  account_id: number | string;
  success: boolean;
  model: string;
  error?: string | null;
  attempts: number;
};

export type UpstreamMonitorModel = {
  name?: string | null;
  model?: string | null;
  status?: string | null;
  latency_ms?: number | null;
};

export type UpstreamMonitorTimelinePoint = {
  time?: string | null;
  checked_at?: string | null;
  status?: string | null;
  latency_ms?: number | null;
  ping_latency_ms?: number | null;
};

export type UpstreamMonitor = {
  id: number | string;
  name?: string | null;
  provider?: string | null;
  group_name?: string | null;
  primary_model?: string | null;
  primary_status?: string | null;
  primary_latency_ms?: number | null;
  primary_ping_latency_ms?: number | null;
  availability_7d?: number | null;
  availability_window?: "24h" | "7d" | null;
  extra_models?: UpstreamMonitorModel[] | null;
  timeline?: UpstreamMonitorTimelinePoint[] | null;
};

export type UpstreamMonitorsResponse = {
  upstream_id: string;
  upstream_monitors: UpstreamMonitor[];
  upstream_monitor_count: number;
  upstream_monitor_status: string;
  upstream_monitor_message?: string | null;
  upstream_monitor_checked_at?: string | null;
};

export type Upstream = {
  upstream_id: string;
  background_discovery_pending?: boolean;
  display_name?: string | null;
  api_endpoint_url?: string | null;
  management_url?: string | null;
  platform_type?: UpstreamType | null;
  probe_enabled?: boolean;
  resolved_platform_type?: Exclude<UpstreamType, "auto"> | null;
  upstream_user_id?: string | null;
  access_token_set?: boolean;
  refresh_token_set?: boolean;
  login_credentials_set?: boolean;
  upstream_recharge_multiplier_override?: number | null;
  discovered_upstream_recharge_multiplier?: number | null;
  upstream_recharge_multiplier?: number | null;
  recharge_multiplier_source?: string | null;
  recharge_multiplier_status?: string | null;
  group_options?: UpstreamGroupOption[] | null;
  wallet_balance_usd?: number | null;
  wallet_total_usd?: number | null;
  wallet_used_usd?: number | null;
  balance_unit?: string | null;
  balance_status?: string | null;
  balance_source?: string | null;
  balance_message?: string | null;
  balance_checked_at?: string | null;
  actual_balance_cny?: number | null;
  today_upstream_wallet_cost_usd?: number | null;
  today_upstream_actual_cost_cny?: number | null;
  today_balance_unit?: string | null;
  today_balance_status?: string | null;
  today_balance_checked_at?: string | null;
  yesterday_upstream_wallet_cost_usd?: number | null;
  yesterday_upstream_actual_cost_cny?: number | null;
  yesterday_balance_unit?: string | null;
  yesterday_balance_status?: string | null;
  yesterday_balance_checked_at?: string | null;
  balance_guard_state?: string | null;
  balance_guard_basis?: "wallet" | "recharge_adjusted" | null;
  balance_guard_value?: number | null;
  balance_guard_checked_at?: string | null;
  balance_guard_paused_count?: number;
  upstream_monitors?: UpstreamMonitor[] | null;
  upstream_monitor_count?: number;
  upstream_monitor_status?: string | null;
  upstream_monitor_message?: string | null;
  upstream_monitor_checked_at?: string | null;
  upstream_monitor_guard_state?: string | null;
  upstream_monitor_unavailable_count?: number;
  upstream_monitor_recovery_count?: number;
  upstream_monitor_guard_checked_at?: string | null;
  status?: string | null;
  message?: string | null;
  checked_at?: string | null;
  last_error?: string | null;
  last_discovered_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  account_count?: number;
  accounts?: ApiAccount[] | null;
};

export type UpstreamOverviewResponse = {
  management_recharge_multiplier?: number | null;
  management_recharge_source?: string | null;
  management_recharge_status?: string | null;
  priority_intervals?: PriorityInterval[];
  upstreams: Upstream[];
  unassigned_accounts: ApiAccount[];
};

export type UpstreamUsageHistoryAccount = {
  management_account_id: number | string;
  api_account_id?: number | string | null;
  account_name: string | null;
  remote_key_id?: number | string | null;
  upstream_api_key_id?: number | string | null;
};

export type UpstreamUsageHistoryAccountDay = UpstreamUsageHistoryAccount & {
  upstream_wallet_cost_usd: number | null;
  upstream_usage_unit?: string | null;
  upstream_usage_source?: string | null;
  upstream_recharge_multiplier?: number | null;
  upstream_actual_cost_cny?: number | null;
  management_account_cost_usd?: number | null;
  management_account_cost_cny?: number | null;
  management_user_charge_usd?: number | null;
  management_recharge_multiplier?: number | null;
  actual_income_cny: number | null;
  income_unit?: string | null;
  profit_cny?: number | null;
  /** Profit divided by the effective cost, expressed as a percentage. */
  profit_margin?: number | null;
};

export type UpstreamUsageHistoryDay = {
  date: string;
  upstream_wallet_cost_usd: number | null;
  upstream_recharge_multiplier?: number | null;
  upstream_actual_cost_cny?: number | null;
  management_account_cost_usd?: number | null;
  management_account_cost_cny?: number | null;
  management_user_charge_usd?: number | null;
  management_recharge_multiplier?: number | null;
  actual_income_cny: number | null;
  income_unit?: string | null;
  consumption_cny?: number | null;
  profit_cny?: number | null;
  /** Profit divided by the effective cost, expressed as a percentage. */
  profit_margin?: number | null;
  finalized?: boolean;
  api_accounts: UpstreamUsageHistoryAccountDay[];
};

export type UpstreamUsageHistoryTotals = {
  upstream_wallet_cost_usd: number | null;
  upstream_actual_cost_cny?: number | null;
  management_account_cost_usd?: number | null;
  management_account_cost_cny?: number | null;
  management_user_charge_usd?: number | null;
  actual_income_cny: number | null;
  consumption_cny?: number | null;
  profit_cny?: number | null;
  /** Profit divided by the effective cost, expressed as a percentage. */
  profit_margin?: number | null;
};

export type UpstreamUsageHistory = {
  upstream_id: string;
  upstream_name: string | null;
  time_zone: string;
  start_date: string;
  end_date: string;
  management_account_id?: number | string | null;
  api_accounts: UpstreamUsageHistoryAccount[];
  days: UpstreamUsageHistoryDay[];
  totals: UpstreamUsageHistoryTotals;
  lifetime_totals: UpstreamUsageHistoryTotals;
};

export type UpstreamUsageHistoryFilters = {
  startDate?: string;
  endDate?: string;
  apiKeyAccountId?: number | string | null;
  timeZone?: string;
};

export type UpstreamDiscoverAllResult = {
  total: number;
  succeeded: number;
  failed: number;
  cached?: number;
  skipped?: number;
  probe_globally_enabled?: boolean;
  force?: boolean;
  cache_max_age_seconds?: number | null;
  duration_ms?: number | null;
  inventory_duration_ms?: number | null;
  probe_duration_ms?: number | null;
  priority_duration_ms?: number | null;
  upstreams: Upstream[];
  overview?: UpstreamOverviewResponse | null;
};

export type UpstreamLegacyIdentityBinding = {
  management_account_id: number;
  expected_identity_fingerprint: string;
};

export type ApiKeyViewOperation =
  | { kind: "blocking" }
  | { kind: "upstream-discovery"; upstreamId: string };

export type UpstreamDiscoverAllRequest = (
  | {
      confirm_legacy_bindings: true;
      account_bindings: UpstreamLegacyIdentityBinding[];
    }
  | {
      confirm_legacy_bindings?: never;
      account_bindings?: never;
    }
) & {
  skip_upstream_ids?: string[];
};

export type UpstreamUpdate = {
  display_name?: string | null;
  api_endpoint_url?: string | null;
  management_url?: string | null;
  platform_type?: UpstreamType;
  probe_enabled?: boolean;
  access_token?: string;
  clear_access_token?: boolean;
  refresh_token?: string;
  clear_refresh_token?: boolean;
  login_username?: string;
  login_password?: string;
  clear_login_credentials?: boolean;
  confirm_credential_rebind?: boolean;
  upstream_user_id?: string | null;
  upstream_recharge_multiplier_override?: number | null;
  upstream_monitor_test_models?: Record<string, string> | null;
};

export type UpstreamChangeLog = {
  id: number;
  management_account_id: number | string;
  account_name?: string | null;
  upstream_id?: string | null;
  upstream_name?: string | null;
  group_id?: string | null;
  group_name?: string | null;
  old_group_id?: string | null;
  new_group_id?: string | null;
  old_group_name?: string | null;
  new_group_name?: string | null;
  old_group_multiplier?: number | null;
  new_group_multiplier?: number | null;
  /** Group multiplier normalized to the cost of one upstream USD at a 1:1 recharge ratio. */
  old_upstream_multiplier?: number | null;
  /** Group multiplier normalized to the cost of one upstream USD at a 1:1 recharge ratio. */
  new_upstream_multiplier?: number | null;
  old_upstream_recharge_multiplier?: number | null;
  new_upstream_recharge_multiplier?: number | null;
  upstream_recharge_multiplier?: number | null;
  management_recharge_multiplier?: number | null;
  old_expected_management_billing_multiplier?: number | null;
  new_expected_management_billing_multiplier?: number | null;
  old_management_billing_multiplier?: number | null;
  new_management_billing_multiplier?: number | null;
  old_upstream_key_status?: string | null;
  new_upstream_key_status?: string | null;
  old_upstream_group_status?: string | null;
  new_upstream_group_status?: string | null;
  old_remote_schedulable?: boolean | null;
  new_remote_schedulable?: boolean | null;
  reason?: string | null;
  status: string;
  safe_error?: string | null;
  created_at: string;
};

export type UpstreamRateChangeLog = UpstreamChangeLog;

export type UpstreamChangeEvent = {
  id: number;
  upstream_id?: string | null;
  upstream_name?: string | null;
  event_type: "upstream_recharge_multiplier_changed" | "group_multiplier_changed" | "group_removed" | "group_added" | "group_name_changed" | "account_rate_changed" | "upstream_key_status_changed" | "upstream_group_status_changed";
  group_id?: string | null;
  group_name?: string | null;
  old_value?: number | null;
  new_value?: number | null;
  old_status?: string | null;
  new_status?: string | null;
  details?: Record<string, unknown> | null;
  created_at: string;
  unread: boolean;
};

export type AccountSchedulingChangeEvent = {
  id: number;
  management_account_id: number | string;
  account_name?: string | null;
  upstream_id?: string | null;
  upstream_name?: string | null;
  event_type: "paused" | "restored" | "pause_failed" | "restore_failed";
  reason?: string | null;
  active_reasons: string[];
  evidence?: Record<string, unknown> | null;
  old_schedulable?: boolean | null;
  new_schedulable?: boolean | null;
  status: string;
  safe_error?: string | null;
  created_at: string;
  unread: boolean;
};

export type ChangeLogPage<T> = {
  items: T[];
  unread_count: number;
  last_read_id: number;
  total_count: number;
  page: number;
  page_size: number;
};

export type ChangeLogUnreadCounts = {
  upstream_changes: number;
  account_rate_changes: number;
  account_scheduling_changes: number;
};
export { upstreamStatusLabel, upstreamStatusTone, upstreamHealthStatusLabel } from "../upstreamLabels";
export { normalizedUpstreamMultiplier, upstreamRechargeRateChange } from "../upstreamRatePresentation";
export { partitionUpstreams, isGenericUpstreamError, upstreamTokenInvalid } from "../upstreamPresentation";
