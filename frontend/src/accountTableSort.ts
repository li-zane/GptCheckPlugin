import type { Account } from "./types";

export type AccountTableSortField = "account" | "imported_at";
export type AccountTableSortDirection = "asc" | "desc";

const accountCollator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });

export function sortAccountsForTable(
  accounts: readonly Account[],
  field: AccountTableSortField,
  direction: AccountTableSortDirection,
) {
  const multiplier = direction === "asc" ? 1 : -1;
  return accounts
    .map((account, index) => ({ account, index }))
    .sort((left, right) => {
      if (field === "imported_at") {
        const leftTime = parsedTime(left.account.sub2api_imported_at);
        const rightTime = parsedTime(right.account.sub2api_imported_at);
        if (leftTime === null && rightTime !== null) return 1;
        if (leftTime !== null && rightTime === null) return -1;
        if (leftTime !== null && rightTime !== null && leftTime !== rightTime) {
          return (leftTime - rightTime) * multiplier;
        }
      } else {
        const leftLabel = left.account.account_name?.trim() || left.account.email;
        const rightLabel = right.account.account_name?.trim() || right.account.email;
        const labelComparison = accountCollator.compare(leftLabel, rightLabel);
        if (labelComparison !== 0) return labelComparison * multiplier;
      }

      const emailComparison = accountCollator.compare(left.account.email, right.account.email);
      if (emailComparison !== 0) return emailComparison * multiplier;
      if (left.account.duplicate_rank !== right.account.duplicate_rank) {
        return (left.account.duplicate_rank - right.account.duplicate_rank) * multiplier;
      }
      const idComparison = accountCollator.compare(
        left.account.sub2api_account_id || "",
        right.account.sub2api_account_id || "",
      );
      return idComparison !== 0 ? idComparison * multiplier : left.index - right.index;
    })
    .map(({ account }) => account);
}

function parsedTime(value: string | null | undefined) {
  if (!value) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}
