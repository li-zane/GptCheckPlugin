import type {
  AccountUsageEstimate,
  UsageEstimate,
  UsageWindowAggregate,
  UsageWindowEstimate,
} from "./domain";

export type UsageDetailAccountFilter = "normal" | "rate-limited";

export type ProblemUnusedQuotaSummary = {
  accountCount: number;
  fiveHour: UsageWindowAggregate;
  sevenDay: UsageWindowAggregate;
};

type UsageWindowKey = "five_hour" | "seven_day";

export function buildDisplayedUsageEstimate(
  estimate: UsageEstimate,
  includePausedAccounts: boolean,
): UsageEstimate {
  const activeAccounts = estimate.accounts.filter(
    (account) => (
      usageDetailAccountVisible(account)
      && (includePausedAccounts || !usageAccountIsManuallyPaused(account))
    ),
  );
  const groups = estimate.groups
    .map((group) => {
      const rows = activeAccounts.filter(
        (account) => account.groups.some((item) => item.id === group.group_id),
      );
      return {
        ...group,
        account_count: rows.length,
        five_hour: aggregateUsageAccountsWindow(rows, "five_hour"),
        seven_day: aggregateUsageAccountsWindow(rows, "seven_day"),
      };
    })
    .filter((group) => group.account_count > 0);

  return {
    ...estimate,
    overall: {
      ...estimate.overall,
      account_count: activeAccounts.length,
      five_hour: aggregateUsageAccountsWindow(activeAccounts, "five_hour"),
      seven_day: aggregateUsageAccountsWindow(activeAccounts, "seven_day"),
    },
    groups,
    accounts: activeAccounts,
  };
}

export function usageDetailAccountVisible(account: AccountUsageEstimate): boolean {
  return !account.deactive && !account.error && !account.usage_error;
}

export function usageProblemAccount(account: AccountUsageEstimate): boolean {
  return account.deactive || account.error;
}

export function usageDetailAccountRateLimited(account: AccountUsageEstimate): boolean {
  return Boolean(
    account.rate_limited
    || account.five_hour.rate_limited
    || account.seven_day.rate_limited,
  );
}

export function accountMatchesUsageDetailFilter(
  account: AccountUsageEstimate,
  filter: UsageDetailAccountFilter,
): boolean {
  if (!usageDetailAccountVisible(account)) return false;
  const rateLimited = usageDetailAccountRateLimited(account);
  return filter === "rate-limited" ? rateLimited : !rateLimited;
}

export function usageDetailAccountCounts(accounts: AccountUsageEstimate[]): {
  normal: number;
  rateLimited: number;
} {
  let normal = 0;
  let rateLimited = 0;

  for (const account of accounts) {
    if (!usageDetailAccountVisible(account)) continue;
    if (usageDetailAccountRateLimited(account)) {
      rateLimited += 1;
    } else {
      normal += 1;
    }
  }

  return { normal, rateLimited };
}

export function usageEstimateHeaderStats(
  estimate: UsageEstimate,
  includePausedAccounts: boolean,
): {
  accountCount: number;
  availableCount: number;
  rateLimitedCount: number;
} {
  let availableCount = 0;
  let rateLimitedCount = 0;

  for (const account of estimate.accounts) {
    if (!usageDetailAccountVisible(account)) continue;
    if (usageDetailAccountRateLimited(account)) {
      rateLimitedCount += 1;
      continue;
    }
    if (!includePausedAccounts && usageAccountIsManuallyPaused(account)) continue;
    availableCount += 1;
  }

  return {
    accountCount: estimate.overall.account_count,
    availableCount,
    rateLimitedCount,
  };
}

export function aggregateUsageAccountsWindow(
  accounts: AccountUsageEstimate[],
  windowKey: UsageWindowKey,
): UsageWindowAggregate {
  let spent = 0;
  let limit = 0;
  let remaining = 0;
  let estimatedSpent = 0;
  let enabledAccountCount = 0;
  let estimableAccounts = 0;

  for (const account of accounts) {
    if (!usageDetailAccountVisible(account)) continue;
    if (!account.usage_estimate_enabled) continue;
    if (windowKey === "five_hour" && usageDetailAccountRateLimited(account)) continue;
    const window = aggregateSourceWindow(account, windowKey);
    if (window.rate_limited || window.window_kind === "none") continue;
    enabledAccountCount += 1;
    if (window.estimate_spent !== null) spent += window.estimate_spent;
    if (window.estimated_limit === null || window.remaining === null) continue;
    estimableAccounts += 1;
    estimatedSpent += window.estimate_spent ?? 0;
    limit += window.estimated_limit;
    remaining += window.remaining;
  }

  return {
    spent,
    estimated_limit: estimableAccounts ? limit : null,
    remaining: estimableAccounts ? remaining : enabledAccountCount === 0 ? 0 : null,
    remaining_percent: estimableAccounts && limit > 0
      ? clampPercentValue((remaining / limit) * 100)
      : null,
    used_percent: estimableAccounts && limit > 0
      ? clampPercentValue((estimatedSpent / limit) * 100)
      : null,
    account_count: accounts.length,
    enabled_account_count: enabledAccountCount,
    estimable_accounts: estimableAccounts,
  };
}

export function usageProblemAccountUnusedQuota(
  accounts: AccountUsageEstimate[],
): ProblemUnusedQuotaSummary {
  const problemAccounts = accounts.filter(usageProblemAccount);
  return {
    accountCount: problemAccounts.length,
    fiveHour: aggregateProblemUsageAccountsWindow(problemAccounts, "five_hour"),
    sevenDay: aggregateProblemUsageAccountsWindow(problemAccounts, "seven_day"),
  };
}

export function aggregateProblemUsageAccountsWindow(
  accounts: AccountUsageEstimate[],
  windowKey: UsageWindowKey,
): UsageWindowAggregate {
  let spent = 0;
  let limit = 0;
  let remaining = 0;
  let estimatedSpent = 0;
  let windowAccountCount = 0;
  let estimableAccounts = 0;

  for (const account of accounts) {
    const window = aggregateSourceWindow(account, windowKey);
    if (window.window_kind === "none") continue;
    windowAccountCount += 1;
    if (window.estimate_spent !== null) spent += window.estimate_spent;
    if (window.estimated_limit === null || window.remaining === null) continue;
    estimableAccounts += 1;
    estimatedSpent += window.estimate_spent ?? 0;
    limit += window.estimated_limit;
    remaining += window.remaining;
  }

  return {
    spent,
    estimated_limit: estimableAccounts ? limit : null,
    remaining: estimableAccounts ? remaining : null,
    remaining_percent: estimableAccounts && limit > 0
      ? clampPercentValue((remaining / limit) * 100)
      : null,
    used_percent: estimableAccounts && limit > 0
      ? clampPercentValue((estimatedSpent / limit) * 100)
      : null,
    account_count: accounts.length,
    enabled_account_count: windowAccountCount,
    estimable_accounts: estimableAccounts,
  };
}

function usageAccountIsManuallyPaused(account: AccountUsageEstimate): boolean {
  if (account.deactive || usageDetailAccountRateLimited(account)) return false;
  return account.schedulable === false
    && String(account.status || "").trim().toLowerCase() === "active";
}

function aggregateSourceWindow(
  account: AccountUsageEstimate,
  windowKey: UsageWindowKey,
): UsageWindowEstimate {
  const window = account[windowKey];
  if (windowKey === "five_hour" && window.window_kind === "none") {
    const longerWindowKind = account.seven_day.window_kind;
    if (longerWindowKind === "seven_day" || longerWindowKind === "monthly") {
      return account.seven_day;
    }
  }
  return window;
}

function clampPercentValue(value: number | null | undefined): number | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return Math.min(100, Math.max(0, value));
}
