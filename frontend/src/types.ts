export type Summary = {
  total_accounts: number;
  actual_accounts?: number;
  deduped_accounts?: number;
  duplicate_accounts?: number;
  error_accounts: number;
  paused_accounts?: number;
  deactive_accounts: number;
  refreshing_accounts: number;
  mailbox_count: number;
  recent_success: number;
  recent_failed: number;
};

export type Account = {
  id: number;
  email: string;
  account_name: string;
  sub2api_account_id: string | null;
  sub2api_imported_at: string | null;
  platform: string | null;
  account_type: string | null;
  status: string | null;
  schedulable: boolean | null;
  usage_estimate_enabled: boolean;
  mailbox_bound: boolean;
  deactive: boolean;
  refreshing: boolean;
  auto_refresh_locked: boolean;
  last_error: string | null;
  sub2api_error_code: number | null;
  sub2api_error_message: string | null;
  last_seen_at: string;
  updated_at: string;
  is_duplicate: boolean;
  duplicate_group_size: number;
  duplicate_rank: number;
  duplicate_primary: boolean;
  duplicate_primary_account_id: string | null;
  remote_error: boolean;
  can_delete_remote: boolean;
  delete_unlockable: boolean;
  delete_unlocked: boolean;
  rate_limited: boolean;
  rate_limited_windows: string[];
  subscription_starts_at: string | null;
  subscription_expires_at: string | null;
  subscription_renews_at: string | null;
  subscription_cancels_at: string | null;
  subscription_billing_period: string | null;
  subscription_plan: string | null;
  subscription_type: string;
  subscription_label: string;
  has_active_subscription: boolean | null;
  phone_number: string | null;
  phone_sms_url: string | null;
  phone_sms_cdk: string | null;
  phone_sms_recharge_url: string | null;
};

export type AccountLivenessModel = {
  id: string;
  display_name: string;
};

export type AccountLivenessModels = {
  source_account_id: string;
  models: AccountLivenessModel[];
};

export type AccountEditConfiguration = {
  concurrency: number;
  priority: number;
  rate_multiplier: number;
  status: "active" | "inactive" | "error" | null;
  schedulable: boolean;
  proxy_id: number | null;
  group_ids: number[];
  model_whitelist: string[];
  openai_ws_mode: "off" | "ctx_pool" | "passthrough" | "http_bridge" | null;
  codex_image_tool_mode: "inherit" | "enabled" | "disabled" | "block" | null;
  openai_passthrough: boolean | null;
  openai_long_context_billing: boolean | null;
  openai_compact_mode: "auto" | "force_on" | "force_off" | null;
  codex_cli_only: boolean | null;
  codex_cli_only_allow_app_server: boolean | null;
  auto_pause_5h_disabled: boolean | null;
  auto_pause_7d_disabled: boolean | null;
  auto_pause_5h_threshold_percent: number | null;
  auto_pause_7d_threshold_percent: number | null;
};

export type AccountEditPresetConfiguration = AccountEditConfiguration & {
  account_type_scope: string | null;
};

export type AccountEditCurrent = AccountEditConfiguration & {
  account_id: string;
  name: string;
  platform: string;
  account_type: string;
  identity_fingerprint: string;
};

export type AccountEditResourceOption = {
  id: number;
  name: string;
  status: string | null;
  detail: string | null;
};

export type AccountEditPreset = {
  id: number;
  name: string;
  platform: string;
  configuration: AccountEditPresetConfiguration;
  created_at: string;
  updated_at: string;
};

export type AccountEditor = {
  account: AccountEditCurrent;
  groups: AccountEditResourceOption[];
  proxies: AccountEditResourceOption[];
  model_candidates: AccountLivenessModel[];
  model_candidates_complete: boolean;
  presets: AccountEditPreset[];
  resources_checked_at: string;
};

export type AccountEditUpdate = AccountEditConfiguration & {
  name: string;
  expected_identity_fingerprint: string;
};

export type AccountEditResult = {
  message: string;
  editor: AccountEditor;
};

export type AccountLivenessTestItem = {
  account_id: string;
  email: string | null;
  account_name: string | null;
  success: boolean;
  error: string | null;
  duration_ms: number;
};

export type AccountLivenessTestResult = {
  message: string;
  model_id: string;
  total: number;
  succeeded: number;
  failed: number;
  results: AccountLivenessTestItem[];
};

export type Mailbox = {
  id: number;
  gpt_email: string;
  mailbox_email: string;
  provider: string;
  disabled: boolean;
  last_error: string | null;
  last_success_at: string | null;
  created_at: string;
  updated_at: string;
};

export type MailMessage = {
  id: string;
  folder: "inbox" | "junk";
  subject: string | null;
  sender_name: string | null;
  sender_address: string | null;
  body_preview: string | null;
  code?: string | null;
  received_at: string | null;
};

export type MailboxImportResult = {
  message: string;
  imported: number;
  skipped: number;
  invalid_lines: number[];
};

export type PhoneNumber = {
  id: number;
  phone_number: string;
  sms_url: string;
  sms_cdk: string | null;
  sms_recharge_url: string | null;
  account_emails: string[];
  bindings_count: number;
  sms_status: string | null;
  sms_error: string | null;
  sms_checked_at: string | null;
  created_at: string;
  updated_at: string;
};

export type PhoneImportResult = {
  message: string;
  imported: number;
  updated: number;
  skipped: number;
  invalid_lines: number[];
};

export type BulkDeleteResult = {
  message: string;
  requested_count: number;
  deleted_count: number;
};

export type RefreshJob = {
  id: number;
  email: string;
  sub2api_account_id: string | null;
  status: string;
  reason: string | null;
  access_token_tail: string | null;
  memory_peak_rss_bytes: number | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type AppEvent = {
  id: number;
  kind: string;
  email: string | null;
  message: string;
  details: Record<string, unknown> | null;
  created_at: string;
};

export type AccountExceptionRecord = {
  id: number;
  email: string | null;
  sub2api_account_id: string | null;
  source: string;
  status: string;
  message: string;
  details: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type SyncResult = {
  message: string;
  total_seen: number;
  error_seen: number;
  queued: number;
  duplicate_accounts_ignored: number;
  deleted_accounts: number;
  deleted_mailboxes: number;
  usage_total?: number;
  usage_refreshed?: number;
  usage_skipped?: number;
  usage_failed?: number;
  usage_pending?: number;
};

export type UsageRefreshResult = {
  message: string;
  total: number;
  refreshed: number;
  skipped: number;
  failed: number;
  failures: Array<{
    email: string | null;
    account_id: string | null;
    error: string;
  }>;
};

export type SubscriptionRefreshResult = {
  message: string;
  total: number;
  refreshed: number;
  skipped: number;
  no_subscription_fields: number;
  protocol_attempts: number;
  failed: number;
  failures: Array<{
    email: string | null;
    account_id: string | null;
    error: string;
  }>;
};

export type UsageGroupRef = {
  id: string;
  name: string;
};

export type UsageWindowEstimate = {
  used_percent: number | null;
  spent: number | null;
  raw_spent: number | null;
  baseline_spent: number | null;
  estimate_spent: number | null;
  estimate_basis: string | null;
  spend_source: string | null;
  estimated_limit: number | null;
  remaining: number | null;
  remaining_percent: number | null;
  reset_at: string | null;
  remaining_seconds: number | null;
  requests: number | null;
  tokens: number | null;
  estimable: boolean;
  rate_limited: boolean;
  source: string;
  window_kind?: string;
  window_minutes?: number | null;
  window_label?: string | null;
};

export type UsageWindowAggregate = {
  spent: number;
  estimated_limit: number | null;
  remaining: number | null;
  remaining_percent: number | null;
  used_percent: number | null;
  account_count: number;
  enabled_account_count: number;
  estimable_accounts: number;
};

export type UsageTokenWindow = {
  window_key: string;
  window_reset_key: string;
  window_start_at: string | null;
  reset_at: string | null;
  spent: number;
  tokens: number;
  estimated_limit: number | null;
  first_observed_at: string;
  last_observed_at: string;
};

export type UsageTokenHistory = {
  total_spent: number;
  total_tokens: number;
  total_estimated_limit: number;
  window_count: number;
  windows: UsageTokenWindow[];
};

export type AccountUsageEstimate = {
  email: string;
  account_name: string;
  sub2api_account_id: string | null;
  platform: string | null;
  account_type: string | null;
  subscription_plan: string | null;
  subscription_type: string;
  subscription_label: string;
  subscription_billing_period: string | null;
  has_active_subscription: boolean | null;
  status: string | null;
  schedulable: boolean | null;
  deactive: boolean;
  error: boolean;
  rate_limited: boolean;
  rate_limited_windows: string[];
  usage_estimate_enabled: boolean;
  rate_multiplier: number;
  groups: UsageGroupRef[];
  usage_error: string | null;
  five_hour: UsageWindowEstimate;
  seven_day: UsageWindowEstimate;
  seven_day_token_history: UsageTokenHistory;
};

export type GroupUsageEstimate = {
  group_id: string;
  group_name: string;
  account_count: number;
  five_hour: UsageWindowAggregate;
  seven_day: UsageWindowAggregate;
};

export type UsageEstimate = {
  updated_at: string;
  refreshed_usage: boolean;
  formula: Record<string, string>;
  overall: {
    account_count: number;
    five_hour: UsageWindowAggregate;
    seven_day: UsageWindowAggregate;
  };
  groups: GroupUsageEstimate[];
  accounts: AccountUsageEstimate[];
};

export type UsageLimitCalibration = {
  source: string;
  sample_count: number;
  lower: number;
  upper: number;
  mean: number | null;
  sigma: number | null;
  default_lower: number;
  default_upper: number;
};

export type UsageLimitSample = {
  id: number;
  account_key: string;
  email: string | null;
  sub2api_account_id: string | null;
  plan_cohort: string;
  subscription_type: string;
  subscription_label: string;
  reset_key: string;
  reset_at: string | null;
  observed_limit: number;
  raw_spent: number;
  used_percent: number;
  created_at: string;
  updated_at: string;
};

export type UsageLimitWindowSamples = {
  window_key: string;
  label: string;
  plan_cohort: string;
  plan_label: string;
  subscription_type: string;
  subscription_label: string;
  calibration: UsageLimitCalibration;
  samples: UsageLimitSample[];
};

export type UsageLimitSamples = {
  updated_at: string;
  target_sample_count: number | null;
  full_percent_threshold: number;
  five_hour_threshold_percent: number;
  seven_day_threshold_percent: number;
  windows: UsageLimitWindowSamples[];
};

export type UsageLimitSampleDeleteResult = {
  message: string;
  requested_count: number;
  deleted_count: number;
};

export type UsageLimitRangeSettings = {
  lower: number;
  upper: number;
};

export type UsageLimitPlanRanges = {
  five_hour: UsageLimitRangeSettings;
  seven_day: UsageLimitRangeSettings;
  monthly: UsageLimitRangeSettings;
};

export type UsageLimitDefaultRanges = Record<string, UsageLimitPlanRanges>;

export type DeactivatedCleanupResult = {
  message: string;
  deleted_accounts: number;
  deleted_mailboxes: number;
  deleted_sub2api_accounts: number;
  deleted_no_email_sub2api_accounts: number;
  failed_sub2api_accounts: string[];
};

export type SelectedAccountDeleteItem = {
  sub2api_account_id?: string | null;
  snapshot_id?: number | null;
};

export type AppSettings = {
  sub2api_base_url: string;
  sub2api_port: number | null;
  sub2api_base_url_source: string;
  sub2api_x_api_key_set: boolean;
  sub2api_x_api_key_hint: string | null;
  sub2api_auto_recover_state: boolean;
  automation_paused: boolean;
  oauth_account_sync_enabled: boolean;
  oauth_login_mode: "protocol" | "browser";
  oauth_stop_on_phone_verification: boolean;
  recovery_enabled: boolean;
  monitor_interval_seconds: number;
  usage_refresh_enabled: boolean;
  usage_refresh_interval_seconds: number;
  usage_refresh_max_concurrency: number;
  api_key_account_sync_enabled: boolean;
  api_key_account_sync_interval_seconds: number;
  upstream_sync_enabled: boolean;
  upstream_sync_interval_seconds: number;
  upstream_sync_max_concurrency: number;
  upstream_rate_sync_enabled: boolean;
  upstream_priority_sync_enabled: boolean;
  manual_upstream_sync_rate_enabled: boolean;
  manual_upstream_sync_priority_enabled: boolean;
  manual_upstream_sync_upstream_health_enabled: boolean;
  manual_upstream_sync_channel_monitors_enabled: boolean;
  manual_upstream_sync_account_availability_enabled: boolean;
  manual_upstream_sync_balance_guard_enabled: boolean;
  manual_upstream_sync_rate_pause_enabled: boolean;
  api_key_auto_disable_on_upstream_unavailable: boolean;
  api_key_auto_pause_on_negative_balance_enabled: boolean;
  api_key_auto_pause_on_channel_monitor_unavailable_enabled: boolean;
  api_key_availability_all_tests_must_succeed: boolean;
  channel_monitor_auto_probe_enabled: boolean;
  account_model_whitelist_sync_enabled: boolean;
  account_model_whitelist_sync_interval_seconds: number;
  account_model_whitelist_sync_each_time: boolean;
  channel_monitor_fallback_without_monitor_enabled: boolean;
  channel_monitor_fallback_test_models: string[];
  channel_monitor_fallback_test_model: string;
  channel_monitor_fallback_test_attempts: number;
  channel_monitor_recovery_test_attempts: number;
  channel_monitor_test_attempt_interval_seconds: number;
  available_test_models: AccountLivenessModel[];
  upstream_negative_balance_basis: "wallet" | "recharge_adjusted";
  upstream_balance_pause_threshold: number;
  show_stale_negative_balance_alert: boolean;
  priority_assign_disabled_api_key_accounts: boolean;
  priority_share_same_composite_multiplier: boolean;
  upstream_rate_log_retention_days: number;
  change_log_page_size: number;
  change_log_page_size_options: number[];
  /** Number of days to keep per-day upstream usage detail. Lifetime totals are retained separately. */
  upstream_usage_data_retention_days?: number;
  /** Compatibility alias returned by earlier backend builds. */
  upstream_data_retention_days?: number;
  discord_bot_notifications_enabled: boolean;
  discord_bot_token_set: boolean;
  discord_bot_token_hint: string | null;
  discord_bot_channel_id: string;
  notify_oauth_account_disabled: boolean;
  notify_account_enabled: boolean;
  notify_api_key_rate_changed: boolean;
  notify_upstream_group_changed: boolean;
  notify_upstream_balance_low: boolean;
  notify_upstream_token_invalid: boolean;
  usage_limit_sample_five_hour_threshold_percent: number;
  usage_limit_sample_seven_day_threshold_percent: number;
  usage_limit_default_ranges: UsageLimitDefaultRanges;
  refresh_max_concurrency: number;
  protocol_refresh_max_concurrency: number;
  browser_refresh_max_concurrency: number;
  browser_min_available_memory_mb: number;
  subscription_refresh_batch_size: number;
  subscription_refresh_max_concurrency: number;
  account_liveness_max_concurrency: number;
  last_scan_at: string | null;
  last_scan_status: string | null;
  last_scan_message: string | null;
  display_timezone: string;
  site_name: string;
  site_logo_url: string | null;
  site_logo_updated_at: string | null;
};

export type AppSettingsUpdate = {
  sub2api_base_url?: string;
  sub2api_port?: number;
  sub2api_x_api_key?: string;
  clear_sub2api_x_api_key?: boolean;
  confirm_sub2api_credential_rebind?: boolean;
  sub2api_auto_recover_state?: boolean;
  automation_paused?: boolean;
  oauth_account_sync_enabled?: boolean;
  oauth_login_mode?: "protocol" | "browser";
  oauth_stop_on_phone_verification?: boolean;
  recovery_enabled?: boolean;
  monitor_interval_seconds?: number;
  usage_refresh_enabled?: boolean;
  usage_refresh_interval_seconds?: number;
  usage_refresh_max_concurrency?: number;
  api_key_account_sync_enabled?: boolean;
  api_key_account_sync_interval_seconds?: number;
  upstream_sync_enabled?: boolean;
  upstream_sync_interval_seconds?: number;
  upstream_sync_max_concurrency?: number;
  upstream_rate_sync_enabled?: boolean;
  upstream_priority_sync_enabled?: boolean;
  manual_upstream_sync_rate_enabled?: boolean;
  manual_upstream_sync_priority_enabled?: boolean;
  manual_upstream_sync_upstream_health_enabled?: boolean;
  manual_upstream_sync_channel_monitors_enabled?: boolean;
  manual_upstream_sync_account_availability_enabled?: boolean;
  manual_upstream_sync_balance_guard_enabled?: boolean;
  manual_upstream_sync_rate_pause_enabled?: boolean;
  api_key_auto_disable_on_upstream_unavailable?: boolean;
  api_key_auto_pause_on_negative_balance_enabled?: boolean;
  api_key_auto_pause_on_channel_monitor_unavailable_enabled?: boolean;
  api_key_availability_all_tests_must_succeed?: boolean;
  channel_monitor_auto_probe_enabled?: boolean;
  account_model_whitelist_sync_enabled?: boolean;
  account_model_whitelist_sync_interval_seconds?: number;
  account_model_whitelist_sync_each_time?: boolean;
  channel_monitor_fallback_without_monitor_enabled?: boolean;
  channel_monitor_fallback_test_models?: string[];
  channel_monitor_fallback_test_model?: string;
  channel_monitor_fallback_test_attempts?: number;
  channel_monitor_recovery_test_attempts?: number;
  channel_monitor_test_attempt_interval_seconds?: number;
  upstream_negative_balance_basis?: "wallet" | "recharge_adjusted";
  upstream_balance_pause_threshold?: number;
  show_stale_negative_balance_alert?: boolean;
  priority_assign_disabled_api_key_accounts?: boolean;
  priority_share_same_composite_multiplier?: boolean;
  upstream_rate_log_retention_days?: number;
  change_log_page_size?: number;
  change_log_page_size_options?: number[];
  upstream_usage_data_retention_days?: number;
  /** Compatibility alias accepted by earlier backend builds. */
  upstream_data_retention_days?: number;
  discord_bot_notifications_enabled?: boolean;
  discord_bot_token?: string;
  clear_discord_bot_token?: boolean;
  discord_bot_channel_id?: string;
  notify_oauth_account_disabled?: boolean;
  notify_account_enabled?: boolean;
  notify_api_key_rate_changed?: boolean;
  notify_upstream_group_changed?: boolean;
  notify_upstream_balance_low?: boolean;
  notify_upstream_token_invalid?: boolean;
  usage_limit_sample_five_hour_threshold_percent?: number;
  usage_limit_sample_seven_day_threshold_percent?: number;
  usage_limit_default_ranges?: UsageLimitDefaultRanges;
  refresh_max_concurrency?: number;
  protocol_refresh_max_concurrency?: number;
  browser_refresh_max_concurrency?: number;
  browser_min_available_memory_mb?: number;
  subscription_refresh_batch_size?: number;
  subscription_refresh_max_concurrency?: number;
  account_liveness_max_concurrency?: number;
  display_timezone?: string;
  site_name?: string;
};

export type SiteLogoUpdateResult = {
  site_logo_url: string | null;
  site_logo_updated_at: string | null;
  message?: string;
};

export type Sub2ApiPortScanResult = {
  found: boolean;
  base_url: string | null;
  port: number | null;
  status: string;
  message: string;
  checked_ports: number[];
  applied: boolean;
};

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
 * A remote sub2api API-key account enriched with its optional local upstream
 * management state. Most enrichment fields are optional so an unmanaged or
 * not-yet-discovered account remains renderable.
 */
export type UpstreamAccountPauseHold = {
  reason: string;
  triggered_at?: string | null;
  recovery_mode?: string | null;
  scope_channel_id?: number | string | null;
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

export type UpstreamAccount = {
  sub2api_account_id: number | string;
  /** Stable, non-secret identity used to reject stale mutations after a remote ID is reused. */
  identity_fingerprint?: string;
  identity_binding_status?: "unmanaged" | "unbound" | "bound" | "mismatch";
  identity_rebind_required?: boolean;
  api_key_origin_rebind_required?: boolean;
  upstream_identity_rebind_required?: boolean;
  upstream_api_key_record_id?: number | string | null;
  channel_id?: number | string | null;
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
  composite_multiplier?: number | null;
  managed?: boolean;
  base_url?: string | null;
  upstream_type?: UpstreamType | null;
  /** Protocol resolved by the backend while configured mode remains `auto`. */
  resolved_upstream_type?: Exclude<UpstreamType, "auto"> | null;
  /** Compatibility alias used by earlier preview builds. */
  detected_upstream_type?: Exclude<UpstreamType, "auto"> | null;
  upstream_user_id?: string | null;
  selected_group_id?: string | null;
  selected_group_name?: string | null;
  api_key_set?: boolean;
  api_key_hint?: string | null;
  access_token_set?: boolean;
  manual_group_multiplier?: number | null;
  manual_recharge_multiplier?: number | null;
  group_options?: UpstreamGroupOption[] | null;
  discovered_group_multiplier?: number | null;
  effective_group_multiplier?: number | null;
  group_multiplier_source?: string | null;
  group_multiplier_status?: string | null;
  upstream_key_status?: string | null;
  upstream_group_status?: string | null;
  upstream_health_invalid_count?: number;
  upstream_health_checked_at?: string | null;
  upstream_key_checked_at?: string | null;
  upstream_group_checked_at?: string | null;
  availability_check_mode?: "channel_monitor" | "independent_model" | "disabled";
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
  active_pause_holds?: UpstreamAccountPauseHold[];
  pause_owned_by_plugin?: boolean;
  auto_restore_eligible?: boolean;
  auto_pause_episode_id?: string | null;
  auto_pause_channel_id?: number | string | null;
  auto_paused_at?: string | null;
  balance_guard_restore_eligible?: boolean;
  balance_guard_channel_id?: number | string | null;
  balance_guard_paused_at?: string | null;
  discovered_recharge_multiplier?: number | null;
  effective_recharge_multiplier?: number | null;
  recharge_multiplier_source?: string | null;
  recharge_multiplier_status?: string | null;
  local_recharge_multiplier?: number | null;
  local_recharge_source?: string | null;
  local_recharge_status?: string | null;
  current_rate?: number | null;
  target_rate?: number | null;
  would_change?: boolean;
  balance_remaining?: number | null;
  balance_total?: number | null;
  balance_used?: number | null;
  balance_unit?: string | null;
  balance_status?: string | null;
  balance_source?: string | null;
  balance_message?: string | null;
  balance_checked_at?: string | null;
  upstream_usage_amount?: number | null;
  upstream_usage_unit?: string | null;
  upstream_usage_checked_at?: string | null;
  today_upstream_usage_amount?: number | null;
  today_upstream_usage_unit?: string | null;
  today_upstream_usage_status?: string | null;
  today_upstream_usage_source?: string | null;
  today_upstream_usage_checked_at?: string | null;
  last_error?: string | null;
  last_discovered_at?: string | null;
  last_applied_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type UpstreamAccountUpdate = {
  expected_identity_fingerprint: string;
  channel_id?: number | string | null;
  remote_name?: string | null;
  base_url?: string | null;
  upstream_type?: UpstreamType;
  upstream_user_id?: string | null;
  selected_group_id?: string | null;
  selected_group_name?: string | null;
  api_key?: string;
  access_token?: string;
  clear_access_token?: boolean;
  confirm_credential_rebind?: boolean;
  confirm_identity_rebind?: boolean;
  confirm_upstream_identity_rebind?: boolean;
  manual_group_multiplier?: number | null;
  manual_recharge_multiplier?: number | null;
  priority_assignment_when_disabled?: boolean | null;
  rate_pause_policy?: "inherit" | "disabled" | "custom";
  rate_absolute_threshold?: number | null;
  availability_check_mode?: "channel_monitor" | "independent_model" | "disabled";
  availability_monitor_id?: number | string | null;
  availability_test_model?: string | null;
};

export type UpstreamAccountAvailabilityTestResult = {
  account: UpstreamAccount;
  policy_action?: "hold" | "clear" | null;
  policy_status?: string | null;
  policy_error?: string | null;
  evidence: Record<string, unknown>;
};

export type UpstreamAccountConnectionTestResult = {
  account_id: number | string;
  success: boolean;
  model: string;
  error?: string | null;
  attempts: number;
};

export type UpstreamChannelMonitorModel = {
  name?: string | null;
  model?: string | null;
  status?: string | null;
  latency_ms?: number | null;
};

export type UpstreamChannelMonitorTimelinePoint = {
  time?: string | null;
  checked_at?: string | null;
  status?: string | null;
  latency_ms?: number | null;
  ping_latency_ms?: number | null;
};

export type UpstreamChannelMonitor = {
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
  extra_models?: UpstreamChannelMonitorModel[] | null;
  timeline?: UpstreamChannelMonitorTimelinePoint[] | null;
};

export type UpstreamChannelMonitorsResponse = {
  channel_id: number | string;
  channel_monitors: UpstreamChannelMonitor[];
  channel_monitor_count: number;
  channel_monitor_status: string;
  channel_monitor_message?: string | null;
  channel_monitor_checked_at?: string | null;
};

export type UpstreamChannel = {
  id: number | string;
  background_discovery_pending?: boolean;
  display_name?: string | null;
  base_url?: string | null;
  canonical_base_url?: string | null;
  management_base_url?: string | null;
  upstream_type?: UpstreamType | null;
  probe_enabled?: boolean;
  resolved_upstream_type?: Exclude<UpstreamType, "auto"> | null;
  upstream_user_id?: string | null;
  access_token_set?: boolean;
  refresh_token_set?: boolean;
  manual_recharge_multiplier?: number | null;
  discovered_recharge_multiplier?: number | null;
  effective_recharge_multiplier?: number | null;
  recharge_multiplier_source?: string | null;
  recharge_multiplier_status?: string | null;
  group_options?: UpstreamGroupOption[] | null;
  balance_remaining?: number | null;
  balance_total?: number | null;
  balance_used?: number | null;
  balance_unit?: string | null;
  balance_status?: string | null;
  balance_source?: string | null;
  balance_message?: string | null;
  balance_checked_at?: string | null;
  recharge_adjusted_balance?: number | null;
  today_balance_used?: number | null;
  today_recharge_adjusted_balance_used?: number | null;
  today_balance_unit?: string | null;
  today_balance_status?: string | null;
  today_balance_checked_at?: string | null;
  yesterday_balance_used?: number | null;
  yesterday_balance_unit?: string | null;
  yesterday_balance_status?: string | null;
  yesterday_balance_checked_at?: string | null;
  balance_guard_state?: string | null;
  balance_guard_basis?: "wallet" | "recharge_adjusted" | null;
  balance_guard_value?: number | null;
  balance_guard_checked_at?: string | null;
  balance_guard_paused_count?: number;
  channel_monitors?: UpstreamChannelMonitor[] | null;
  channel_monitor_count?: number;
  channel_monitor_status?: string | null;
  channel_monitor_message?: string | null;
  channel_monitor_checked_at?: string | null;
  channel_monitor_guard_state?: string | null;
  channel_monitor_unavailable_count?: number;
  channel_monitor_recovery_count?: number;
  channel_monitor_guard_checked_at?: string | null;
  status?: string | null;
  message?: string | null;
  checked_at?: string | null;
  last_error?: string | null;
  last_discovered_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  account_count?: number;
  accounts?: UpstreamAccount[] | null;
};

export type UpstreamChannelsResponse = {
  local_recharge_multiplier?: number | null;
  local_recharge_source?: string | null;
  local_recharge_status?: string | null;
  priority_intervals?: PriorityInterval[];
  channels: UpstreamChannel[];
  unassigned_accounts: UpstreamAccount[];
};

export type UpstreamUsageHistoryAccount = {
  sub2api_account_id: number | string;
  account_name: string | null;
  upstream_api_key_record_id?: number | string | null;
};

export type UpstreamUsageHistoryAccountDay = UpstreamUsageHistoryAccount & {
  upstream_usage: number | null;
  upstream_usage_adjusted?: number | null;
  upstream_usage_unit?: string | null;
  upstream_usage_source?: string | null;
  sub2api_actual_cost?: number | null;
  local_recharge_multiplier?: number | null;
  income: number | null;
  income_unit?: string | null;
};

export type UpstreamUsageHistoryDay = {
  date: string;
  /** Channel-level wallet consumption; it is not apportioned to an API key filter. */
  balance_used: number | null;
  balance_used_adjusted: number | null;
  balance_unit?: string | null;
  recharge_multiplier?: number | null;
  /** Linked sub2api API-key consumption, optionally filtered by account. */
  upstream_api_key_usage: number | null;
  income_actual_cost?: number | null;
  income_recharge_multiplier?: number | null;
  income: number | null;
  income_unit?: string | null;
  /** Cost is channel balance consumption without a key filter, otherwise that key's consumption. */
  cost: number | null;
  cost_adjusted: number | null;
  finalized?: boolean;
  api_key_accounts: UpstreamUsageHistoryAccountDay[];
};

export type UpstreamUsageHistoryTotals = {
  balance_used: number | null;
  balance_used_adjusted: number | null;
  upstream_api_key_usage: number | null;
  income_actual_cost?: number | null;
  income: number | null;
  cost: number | null;
  cost_adjusted: number | null;
};

export type UpstreamUsageHistory = {
  channel_id: number | string;
  channel_name: string | null;
  time_zone: string;
  start_date: string;
  end_date: string;
  api_key_account_id?: number | string | null;
  api_key_accounts: UpstreamUsageHistoryAccount[];
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

export type UpstreamChannelDiscoverAllResult = {
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
  channels: UpstreamChannel[];
  overview?: UpstreamChannelsResponse | null;
};

export type UpstreamLegacyIdentityBinding = {
  sub2api_account_id: number;
  expected_identity_fingerprint: string;
};

export type ApiKeyViewOperation =
  | { kind: "blocking" }
  | { kind: "channel-discovery"; channelId: number };

export type UpstreamChannelDiscoverAllRequest = (
  | {
      confirm_legacy_bindings: true;
      account_bindings: UpstreamLegacyIdentityBinding[];
    }
  | {
      confirm_legacy_bindings?: never;
      account_bindings?: never;
    }
) & {
  skip_channel_ids?: number[];
};

export type UpstreamChannelUpdate = {
  display_name?: string | null;
  base_url?: string | null;
  management_base_url?: string | null;
  upstream_type?: UpstreamType;
  probe_enabled?: boolean;
  access_token?: string;
  clear_access_token?: boolean;
  refresh_token?: string;
  clear_refresh_token?: boolean;
  confirm_credential_rebind?: boolean;
  upstream_user_id?: string | null;
  manual_recharge_multiplier?: number | null;
};

export type UpstreamChangeLog = {
  id: number;
  sub2api_account_id: number | string;
  account_name?: string | null;
  channel_id?: number | string | null;
  channel_name?: string | null;
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
  local_recharge_multiplier?: number | null;
  old_target_rate?: number | null;
  new_target_rate?: number | null;
  old_current_rate?: number | null;
  new_current_rate?: number | null;
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

/** Compatibility alias for integrations compiled against the earlier name. */
export type UpstreamRateChangeLog = UpstreamChangeLog;

export type UpstreamChannelChangeEvent = {
  id: number;
  channel_id?: number | string | null;
  channel_name?: string | null;
  event_type: "channel_multiplier_changed" | "group_multiplier_changed" | "group_removed" | "group_added" | "group_name_changed" | "account_rate_changed" | "upstream_key_status_changed" | "upstream_group_status_changed";
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
  sub2api_account_id: number | string;
  account_name?: string | null;
  channel_id?: number | string | null;
  channel_name?: string | null;
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
