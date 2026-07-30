import type { Account, AccountUsageEstimate } from "./types";

type CurrentAccountRateLimit = Pick<Account, "deactive" | "rate_limited" | "rate_limited_windows">;
type CachedAccountRateLimit = Pick<
  AccountUsageEstimate,
  "error" | "rate_limited" | "rate_limited_windows" | "five_hour" | "seven_day"
>;

export function currentAccountHasRateLimit(account: CurrentAccountRateLimit) {
  return account.rate_limited || account.rate_limited_windows.length > 0;
}

export function accountRateLimitShouldBeVisible(
  account: CurrentAccountRateLimit,
  usage: CachedAccountRateLimit | undefined,
  currentAccountHasError: boolean,
) {
  if (account.deactive) return false;
  if (currentAccountHasRateLimit(account)) return true;
  if (currentAccountHasError || usage?.error) return false;
  return Boolean(
    usage?.rate_limited
      || usage?.rate_limited_windows.length
      || usage?.five_hour.rate_limited
      || usage?.seven_day.rate_limited,
  );
}

export function accountEstimateHasEffectiveError(
  account: CurrentAccountRateLimit,
  usage: CachedAccountRateLimit | undefined,
  currentAccountHasError: boolean,
) {
  if (currentAccountHasError) return true;
  return Boolean(usage?.error && !currentAccountHasRateLimit(account));
}
