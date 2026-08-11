import type { UpstreamAccount, UpstreamChannel, UpstreamChannelsResponse } from "./types";

export type UpstreamAccountEntry = {
  account: UpstreamAccount;
  channel: UpstreamChannel | null;
};

export type UpstreamAccountTieSort = "name" | "priority";

export type UpstreamAccountFilters = {
  channel: "all" | "__unassigned__" | string;
  interval: "all" | "unassigned" | string;
  platform: "all" | "__unknown__" | string;
  query: string;
};

export type UpstreamAccountStatusFilter = "all" | "enabled" | "disabled" | "pending" | "attention" | "undiscovered";

export type PriorityTieMoveState = {
  canMoveDown: boolean;
  canMoveUp: boolean;
  peerCount: number;
};

const PRIORITY_TIE_DECIMAL_PLACES = 13;

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

export function sortUpstreamAccountEntries(
  entries: UpstreamAccountEntry[],
  tieSort: UpstreamAccountTieSort = "name",
) {
  return [...entries].sort((left, right) => {
    const leftMultiplier = accountCompositeMultiplier(left.account);
    const rightMultiplier = accountCompositeMultiplier(right.account);
    if (leftMultiplier === null && rightMultiplier !== null) return 1;
    if (leftMultiplier !== null && rightMultiplier === null) return -1;
    if (leftMultiplier !== null && rightMultiplier !== null && leftMultiplier !== rightMultiplier) {
      return leftMultiplier - rightMultiplier;
    }
    return tieSort === "priority"
      ? compareAccountSchedulingPriority(left.account, right.account)
      : compareAccountNames(left.account, right.account);
  });
}

export function sortUpstreamAccountEntriesByName(entries: UpstreamAccountEntry[]) {
  return [...entries].sort((left, right) => compareAccountNames(left.account, right.account));
}

export function priorityTieMoveOptions(accounts: UpstreamAccount[]) {
  const groups = new Map<string, UpstreamAccount[]>();
  for (const account of accounts) {
    const intervalId = account.priority_interval_id;
    const multiplier = accountCompositeMultiplier(account);
    if (
      intervalId === null
      || intervalId === undefined
      || multiplier === null
      || finiteNumber(account.desired_priority) === null
    ) continue;
    const key = `${String(intervalId)}:${priorityTieMultiplierKey(multiplier)}`;
    const group = groups.get(key) || [];
    group.push(account);
    groups.set(key, group);
  }

  const options = new Map<string, PriorityTieMoveState>();
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const ordered = [...group].sort(comparePriorityTieOrder);
    ordered.forEach((account, index) => {
      options.set(String(account.sub2api_account_id), {
        canMoveDown: index > 0,
        canMoveUp: index < ordered.length - 1,
        peerCount: ordered.length,
      });
    });
  }
  return options;
}

export function filterUpstreamAccountEntries(
  entries: UpstreamAccountEntry[],
  filters: UpstreamAccountFilters,
) {
  const query = filters.query.trim().toLowerCase();
  const platform = filters.platform.toLowerCase();
  return entries.filter(({ account, channel }) => {
    if (filters.channel === "__unassigned__") {
      if (channel !== null) return false;
    } else if (filters.channel !== "all" && String(channel?.id ?? "") !== filters.channel) {
      return false;
    }

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

export function upstreamAccountChannels(entries: UpstreamAccountEntry[]) {
  const values = new Map<string, string>();
  let hasUnassigned = false;
  for (const { channel } of entries) {
    if (channel === null) {
      hasUnassigned = true;
      continue;
    }
    const value = String(channel.id);
    if (values.has(value)) continue;
    values.set(
      value,
      String(
        channel.display_name
        || channel.base_url
        || channel.canonical_base_url
        || `上游 #${value}`,
      ).trim(),
    );
  }
  return {
    hasUnassigned,
    channels: [...values.entries()]
      .sort(([, left], [, right]) => left.localeCompare(right, "zh-CN"))
      .map(([value, label]) => ({ value, label })),
  };
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
  if (filter === "enabled") return account.remote_schedulable === true;
  if (filter === "disabled") return account.remote_schedulable === false;
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

function comparePriorityTieOrder(left: UpstreamAccount, right: UpstreamAccount) {
  const multiplier = accountCompositeMultiplier(left);
  const leftOrder = activePriorityTieOrder(left, multiplier);
  const rightOrder = activePriorityTieOrder(right, multiplier);
  if (leftOrder !== null && rightOrder !== null && leftOrder !== rightOrder) {
    return leftOrder - rightOrder;
  }
  if (leftOrder !== null && rightOrder === null) return -1;
  if (leftOrder === null && rightOrder !== null) return 1;
  return compareAccountNames(left, right);
}

function compareAccountSchedulingPriority(left: UpstreamAccount, right: UpstreamAccount) {
  const leftPriority = finiteNumber(left.priority) ?? finiteNumber(left.desired_priority);
  const rightPriority = finiteNumber(right.priority) ?? finiteNumber(right.desired_priority);
  if (leftPriority !== null && rightPriority !== null && leftPriority !== rightPriority) {
    return leftPriority - rightPriority;
  }
  if (leftPriority !== null && rightPriority === null) return -1;
  if (leftPriority === null && rightPriority !== null) return 1;
  return compareAccountNames(left, right);
}

function activePriorityTieOrder(account: UpstreamAccount, multiplier: number | null) {
  const order = finiteNumber(account.priority_tiebreak_order);
  const storedMultiplier = finiteNumber(account.priority_tiebreak_multiplier);
  if (
    order === null
    || multiplier === null
    || storedMultiplier === null
    || priorityTieMultiplierKey(storedMultiplier) !== priorityTieMultiplierKey(multiplier)
  ) return null;
  return order;
}

export function priorityTieMultiplierKey(value: number) {
  const negative = value < 0;
  const [coefficient, exponentText] = Math.abs(value).toString().toLowerCase().split("e");
  const exponent = Number(exponentText || 0);
  const [whole, fraction = ""] = coefficient.split(".");
  const digits = `${whole}${fraction}` || "0";
  const decimalIndex = whole.length + exponent;
  let integerPart: string;
  let fractionPart: string;
  if (decimalIndex <= 0) {
    integerPart = "0";
    fractionPart = "0".repeat(-decimalIndex) + digits;
  } else if (decimalIndex >= digits.length) {
    integerPart = digits + "0".repeat(decimalIndex - digits.length);
    fractionPart = "";
  } else {
    integerPart = digits.slice(0, decimalIndex);
    fractionPart = digits.slice(decimalIndex);
  }
  const normalizedInteger = integerPart.replace(/^0+(?=\d)/, "") || "0";
  const normalizedFraction = (fractionPart + "0".repeat(PRIORITY_TIE_DECIMAL_PLACES))
    .slice(0, PRIORITY_TIE_DECIMAL_PLACES);
  return `${negative ? "-" : ""}${normalizedInteger}.${normalizedFraction}`;
}

function compareAccountNames(left: UpstreamAccount, right: UpstreamAccount) {
  const leftName = String(left.remote_name || "").trim();
  const rightName = String(right.remote_name || "").trim();
  const compared = leftName.localeCompare(rightName, "zh-CN", {
    numeric: true,
    sensitivity: "base",
  });
  return compared || compareAccountIds(left.sub2api_account_id, right.sub2api_account_id);
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
