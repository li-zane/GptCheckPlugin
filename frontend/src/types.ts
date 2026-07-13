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
  target_sample_count: number;
  full_percent_threshold: number;
  five_hour_threshold_percent: number;
  seven_day_threshold_percent: number;
  windows: UsageLimitWindowSamples[];
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
  recovery_enabled: boolean;
  monitor_interval_seconds: number;
  usage_refresh_enabled: boolean;
  usage_refresh_interval_seconds: number;
  usage_refresh_max_concurrency: number;
  usage_limit_sample_five_hour_threshold_percent: number;
  usage_limit_sample_seven_day_threshold_percent: number;
  usage_limit_default_ranges: UsageLimitDefaultRanges;
  refresh_max_concurrency: number;
  protocol_refresh_max_concurrency: number;
  browser_refresh_max_concurrency: number;
  browser_min_available_memory_mb: number;
  subscription_refresh_batch_size: number;
  subscription_refresh_max_concurrency: number;
  last_scan_at: string | null;
  last_scan_status: string | null;
  last_scan_message: string | null;
  display_timezone: string;
  site_name: string;
};

export type AppSettingsUpdate = {
  sub2api_base_url?: string;
  sub2api_port?: number;
  sub2api_x_api_key?: string;
  clear_sub2api_x_api_key?: boolean;
  sub2api_auto_recover_state?: boolean;
  automation_paused?: boolean;
  recovery_enabled?: boolean;
  monitor_interval_seconds?: number;
  usage_refresh_enabled?: boolean;
  usage_refresh_interval_seconds?: number;
  usage_refresh_max_concurrency?: number;
  usage_limit_sample_five_hour_threshold_percent?: number;
  usage_limit_sample_seven_day_threshold_percent?: number;
  usage_limit_default_ranges?: UsageLimitDefaultRanges;
  refresh_max_concurrency?: number;
  protocol_refresh_max_concurrency?: number;
  browser_refresh_max_concurrency?: number;
  browser_min_available_memory_mb?: number;
  subscription_refresh_batch_size?: number;
  subscription_refresh_max_concurrency?: number;
  display_timezone?: string;
  site_name?: string;
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
