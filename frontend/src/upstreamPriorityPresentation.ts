import type { UpstreamAccount, UpstreamChannel, UpstreamChannelsResponse } from "./types";

export type UpstreamAccountEntry = {
  account: UpstreamAccount;
  channel: UpstreamChannel | null;
};

export type UpstreamAccountFilters = {
  interval: "all" | "unassigned" | string;
  platform: "all" | "__unknown__" | string;
  query: string;
};

export type UpstreamAccountStatusFilter = "all" | "pending" | "attention" | "undiscovered";

export function accountCompositeMultiplier(account: UpstreamAccount) {
  const persisted = finiteNumber(account.composite_multiplier);
  if (persisted !== null) return persisted;
  const group = finiteNumber(account.effective_group_multiplier);
  const recharge = finiteNumber(account.effective_recharge_multiplier);
  return group === null || recharge === null ? null : group * recharge;
}

export function priorityIntervalAssignmentNeedsConfirmation(account: UpstreamAccount) {
  return account.identity_binding_status === "unbound";
}

export function priorityIntervalAssignmentBlocked(account: UpstreamAccount) {
  return Boolean(account.identity_rebind_required)
    && !priorityIntervalAssignmentNeedsConfirmation(account);
}

export function flattenUpstreamAccounts(data: UpstreamChannelsResponse): UpstreamAccountEntry[] {
  const entries = new Map<string, UpstreamAccountEntry>();
  for (const channel of data.channels) {
    for (const account of channel.accounts || []) {
      const key = String(account.sub2api_account_id);
      if (!entries.has(key)) entries.set(key, { account, channel });
    }
  }
  for (const account of data.unassigned_accounts) {
    const key = String(account.sub2api_account_id);
    if (!entries.has(key)) entries.set(key, { account, channel: null });
  }
  return [...entries.values()];
}

export function sortUpstreamAccountEntries(entries: UpstreamAccountEntry[]) {
  return [...entries].sort((left, right) => {
    const leftMultiplier = accountCompositeMultiplier(left.account);
    const rightMultiplier = accountCompositeMultiplier(right.account);
    if (leftMultiplier === null && rightMultiplier !== null) return 1;
    if (leftMultiplier !== null && rightMultiplier === null) return -1;
    if (leftMultiplier !== null && rightMultiplier !== null && leftMultiplier !== rightMultiplier) {
      return leftMultiplier - rightMultiplier;
    }
    return compareAccountIds(left.account.sub2api_account_id, right.account.sub2api_account_id);
  });
}

export function filterUpstreamAccountEntries(
  entries: UpstreamAccountEntry[],
  filters: UpstreamAccountFilters,
) {
  const query = filters.query.trim().toLowerCase();
  const platform = filters.platform.toLowerCase();
  return entries.filter(({ account, channel }) => {
    const intervalId = account.priority_interval_id;
    if (filters.interval === "unassigned") {
      if (intervalId !== null && intervalId !== undefined && intervalId !== "") return false;
    } else if (filters.interval !== "all" && String(intervalId ?? "") !== filters.interval) {
      return false;
    }

    const accountPlatform = String(account.remote_platform || "").trim().toLowerCase();
    if (platform === "__unknown__") {
      if (accountPlatform) return false;
    } else if (platform !== "all" && accountPlatform !== platform) {
      return false;
    }

    if (!query) return true;
    return [
      account.remote_name,
      account.remote_platform,
      account.remote_status,
      account.sub2api_account_id,
      account.selected_group_id,
      account.selected_group_name,
      account.priority,
      account.desired_priority,
      account.priority_interval_name,
      channel?.display_name,
      channel?.base_url,
      channel?.canonical_base_url,
    ].some((value) => String(value ?? "").toLowerCase().includes(query));
  });
}

export function upstreamAccountPlatforms(entries: UpstreamAccountEntry[]) {
  const values = new Map<string, string>();
  let hasUnknown = false;
  for (const { account } of entries) {
    const value = String(account.remote_platform || "").trim();
    if (!value) {
      hasUnknown = true;
      continue;
    }
    const key = value.toLowerCase();
    if (!values.has(key)) values.set(key, value);
  }
  return {
    hasUnknown,
    platforms: [...values.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([value, label]) => ({ value, label })),
  };
}

export function upstreamAccountMatchesStatus(
  account: UpstreamAccount,
  filter: UpstreamAccountStatusFilter,
) {
  if (filter === "all") return true;
  if (filter === "pending") return account.would_change === true;
  if (filter === "undiscovered") return !account.last_discovered_at;
  return accountNeedsAttention(account);
}

function accountNeedsAttention(account: UpstreamAccount) {
  if (
    !account.managed
    || account.identity_rebind_required
    || account.last_error
    || account.priority_sync_error
    || !account.api_key_set
  ) return true;
  return [
    account.group_multiplier_status,
    account.upstream_key_status,
    account.upstream_group_status,
    account.priority_sync_status,
  ]
    .filter(Boolean)
    .some((status) => isFailureStatus(status));
}

function isFailureStatus(status?: string | null) {
  const value = String(status || "").trim().toLowerCase();
  if (value === "default_missing") return false;
  return /(error|fail|invalid|unavailable|unsupported|missing|not[_-]?found|disabled|expired|exhausted|unassigned|blocked|denied)/i.test(value);
}

function compareAccountIds(left: number | string, right: number | string) {
  const leftNumber = finiteNumber(left);
  const rightNumber = finiteNumber(right);
  if (leftNumber !== null && rightNumber !== null && leftNumber !== rightNumber) return leftNumber - rightNumber;
  const leftText = String(left);
  const rightText = String(right);
  return leftText < rightText ? -1 : leftText > rightText ? 1 : 0;
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}
