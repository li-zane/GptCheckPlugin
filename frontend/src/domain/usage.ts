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
  management_account_id: string | null;
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
  management_account_id: string | null;
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
export { buildDisplayedUsageEstimate, usageDetailAccountCounts, usageProblemAccountUnusedQuota } from "../usageEstimatePresentation";
export { filterUsageLimitSamples, sortUsageLimitSamples, usageSampleDatePresets, usageSampleDateRangeForPreset } from "../usageSampleSort";

