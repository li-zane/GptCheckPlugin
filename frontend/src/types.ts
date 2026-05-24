export type Summary = {
  total_accounts: number;
  error_accounts: number;
  deactive_accounts: number;
  refreshing_accounts: number;
  mailbox_count: number;
  recent_success: number;
  recent_failed: number;
};

export type Account = {
  id: number;
  email: string;
  sub2api_account_id: string | null;
  platform: string | null;
  account_type: string | null;
  status: string | null;
  schedulable: boolean | null;
  usage_estimate_enabled: boolean;
  mailbox_bound: boolean;
  deactive: boolean;
  refreshing: boolean;
  last_error: string | null;
  last_seen_at: string;
  updated_at: string;
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
  received_at: string | null;
};

export type MailboxImportResult = {
  message: string;
  imported: number;
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

export type SyncResult = {
  total_seen: number;
  error_seen: number;
  queued: number;
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

export type UsageGroupRef = {
  id: string;
  name: string;
};

export type UsageWindowEstimate = {
  used_percent: number | null;
  spent: number | null;
  spend_source: string | null;
  estimated_limit: number | null;
  remaining: number | null;
  remaining_percent: number | null;
  reset_at: string | null;
  remaining_seconds: number | null;
  requests: number | null;
  tokens: number | null;
  estimable: boolean;
  source: string;
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

export type AccountUsageEstimate = {
  email: string;
  sub2api_account_id: string | null;
  platform: string | null;
  account_type: string | null;
  status: string | null;
  schedulable: boolean | null;
  deactive: boolean;
  usage_estimate_enabled: boolean;
  rate_multiplier: number;
  groups: UsageGroupRef[];
  usage_error: string | null;
  five_hour: UsageWindowEstimate;
  seven_day: UsageWindowEstimate;
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

export type DeactivatedCleanupResult = {
  message: string;
  deleted_accounts: number;
  deleted_mailboxes: number;
  deleted_sub2api_accounts: number;
  failed_sub2api_accounts: string[];
};

export type AppSettings = {
  sub2api_base_url: string;
  sub2api_port: number | null;
  sub2api_base_url_source: string;
  sub2api_x_api_key_set: boolean;
  sub2api_x_api_key_hint: string | null;
  monitor_interval_seconds: number;
  usage_refresh_enabled: boolean;
  usage_refresh_interval_seconds: number;
  refresh_max_concurrency: number;
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
  monitor_interval_seconds?: number;
  usage_refresh_enabled?: boolean;
  usage_refresh_interval_seconds?: number;
  refresh_max_concurrency?: number;
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
