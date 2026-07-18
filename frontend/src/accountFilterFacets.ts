export type AccountFilterFacetCandidates<T> = {
  filteredAccounts: T[];
  statusOptionAccounts: T[];
  subscriptionOptionAccounts: T[];
};

export function accountFilterFacetCandidates<T, StatusFilter extends string>(
  accounts: readonly T[],
  statusFilter: StatusFilter,
  subscriptionFilter: string,
  matchesStatus: (account: T, filter: StatusFilter) => boolean,
  subscriptionLabel: (account: T) => string,
): AccountFilterFacetCandidates<T> {
  const statusOptionAccounts = accounts.filter(
    (account) => !subscriptionFilter || subscriptionLabel(account) === subscriptionFilter,
  );
  const subscriptionOptionAccounts = accounts.filter((account) => matchesStatus(account, statusFilter));
  const filteredAccounts = subscriptionOptionAccounts.filter(
    (account) => !subscriptionFilter || subscriptionLabel(account) === subscriptionFilter,
  );

  return { filteredAccounts, statusOptionAccounts, subscriptionOptionAccounts };
}
