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
  management_account_id: string | null;
  management_site_imported_at: string | null;
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
  management_site_error_code: number | null;
  management_site_error_message: string | null;
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
  notes: string;
  groups: AccountGroupRef[];
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

export type AccountGroupRef = {
  id: string;
  name: string;
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

export type AccountNotes = {
  account_id: string;
  account_name: string;
  notes: string;
  identity_fingerprint: string;
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

export type DeactivatedCleanupResult = {
  message: string;
  deleted_accounts: number;
  deleted_mailboxes: number;
  deleted_management_site_api_accounts: number;
  deleted_management_site_accounts_without_email: number;
  failed_management_site_api_accounts: string[];
};

export type SelectedAccountDeleteItem = {
  management_account_id?: string | null;
  snapshot_id?: number | null;
};
export { accountFilterFacetCandidates } from "../accountFilterFacets";
export { sortAccountsForTable } from "../accountTableSort";
export { accountCanBeLivenessTested, livenessAccountIds, MAX_LIVENESS_ACCOUNTS } from "../accountLiveness";

