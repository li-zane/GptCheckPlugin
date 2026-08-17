export const queryKeys = {
  auth: ["auth"] as const,
  settings: ["settings"] as const,
  summary: ["summary"] as const,
  accounts: ["accounts"] as const,
  mailboxes: ["mailboxes"] as const,
  phones: ["phones"] as const,
  history: ["history"] as const,
  historyJobs: ["history", "jobs"] as const,
  historyEvents: ["history", "events"] as const,
  historyExceptions: ["history", "exceptions"] as const,
  usage: ["usage"] as const,
  usageEstimate: (refresh = false) => ["usage", "estimate", refresh ? "refresh" : "read"] as const,
  usageSamples: ["usage", "samples"] as const,
  upstreams: (managementSiteScope: string) => ["upstreams", managementSiteScope] as const,
  priorityIntervals: ["priorityIntervals"] as const,
  changeLogs: (category?: string) => ["changeLogs", category ?? "all"] as const,
  changeLogUnread: ["changeLogs", "unread"] as const,
  pageRefresh: (view: string) => ["pageRefresh", view] as const,
};

export type MutationDomain = "account" | "upstream" | "settings";

export const mutationInvalidationMatrix = {
  account: [queryKeys.accounts, queryKeys.summary, queryKeys.usage, queryKeys.history],
  upstream: [["upstreams"], ["changeLogs"]],
  settings: [queryKeys.settings],
} as const satisfies Record<MutationDomain, readonly (readonly unknown[])[]>;
