import type { Account } from "./types";

export const MAX_LIVENESS_ACCOUNTS = 200;

function normalizedAccountType(account: Pick<Account, "account_type">) {
  return String(account.account_type || "")
    .trim()
    .toLowerCase()
    .replaceAll("-", "_")
    .replaceAll(" ", "_");
}

function isPositiveSafeAccountId(value: string | null | undefined) {
  const normalized = String(value || "").trim();
  if (!/^\d+$/.test(normalized)) return false;
  const parsed = Number(normalized);
  return Number.isSafeInteger(parsed) && parsed > 0;
}

export function accountCanBeLivenessTested(
  account: Pick<Account, "account_type" | "platform" | "sub2api_account_id">,
) {
  if (!isPositiveSafeAccountId(account.sub2api_account_id)) return false;
  const accountType = normalizedAccountType(account);
  // /api/accounts is already restricted to GPT accounts by the backend. The
  // browser can reliably distinguish only the OAuth half of that same rule.
  return accountType.includes("oauth");
}

export function livenessAccountIds(
  accounts: Array<Pick<Account, "account_type" | "platform" | "sub2api_account_id">>,
) {
  const result: string[] = [];
  const seen = new Set<string>();
  for (const account of accounts) {
    if (!accountCanBeLivenessTested(account)) continue;
    const accountId = String(account.sub2api_account_id).trim();
    if (seen.has(accountId)) continue;
    seen.add(accountId);
    result.push(accountId);
    if (result.length === MAX_LIVENESS_ACCOUNTS) break;
  }
  return result;
}
