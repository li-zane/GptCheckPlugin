import type {
  AccountSchedulingChangeEvent,
  ApiAccount,
  ApiAccountAvailabilityTestResult,
  ApiAccountConnectionTestResult,
  ApiAccountUpdate,
  ChangeLogPage,
  ChangeLogUnreadCounts,
  PriorityInterval,
  PriorityIntervalAssignment,
  PriorityIntervalInput,
  PriorityRebalanceResult,
  PriorityTieMoveInput,
  Upstream,
  UpstreamCredentials,
  UpstreamChangeEvent,
  UpstreamChangeLog,
  UpstreamDiscoverAllRequest,
  UpstreamDiscoverAllResult,
  UpstreamMonitorsResponse,
  UpstreamOverviewResponse,
  UpstreamUpdate,
  UpstreamUsageHistory,
  UpstreamUsageHistoryFilters,
} from "../../domain";
import { NO_FRONTEND_TIMEOUT, request } from "./client.ts";

type UpstreamChangeLogFilters = { startDate?: string; endDate?: string; timeZone?: string };

function upstreamChangeLogsQuery(limit = 50, beforeId?: number | null, filters?: UpstreamChangeLogFilters, page?: number | null) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeId) params.set("before_id", String(beforeId));
  if (page) params.set("page", String(page));
  if (filters?.startDate) params.set("start_date", filters.startDate);
  if (filters?.endDate) params.set("end_date", filters.endDate);
  if (filters?.timeZone) params.set("time_zone", filters.timeZone);
  return params.toString();
}

export function upstreamChangeLogsPath(limit = 50, beforeId?: number | null, filters?: UpstreamChangeLogFilters) {
  return `/api/api-accounts/upstream-change-logs?${upstreamChangeLogsQuery(limit, beforeId, filters)}`;
}

export function upstreamUsageHistoryPath(upstreamId: number | string, filters?: UpstreamUsageHistoryFilters) {
  const params = new URLSearchParams();
  if (filters?.startDate) params.set("start_date", filters.startDate);
  if (filters?.endDate) params.set("end_date", filters.endDate);
  if (filters?.apiKeyAccountId !== null && filters?.apiKeyAccountId !== undefined && filters.apiKeyAccountId !== "") {
    params.set("management_account_id", String(filters.apiKeyAccountId));
  }
  if (filters?.timeZone) params.set("time_zone", filters.timeZone);
  const query = params.toString();
  return `/api/upstreams/${encodeURIComponent(String(upstreamId))}/usage-history${query ? `?${query}` : ""}`;
}

export const upstreamsApi = {
  apiAccounts: () => request<ApiAccount[]>("/api/api-accounts"),
  priorityIntervals: () => request<PriorityInterval[]>("/api/api-accounts/priority-intervals"),
  createPriorityInterval: (payload: PriorityIntervalInput) => request<PriorityInterval>(
    "/api/api-accounts/priority-intervals",
    { method: "POST", body: JSON.stringify(payload) },
  ),
  updatePriorityInterval: (intervalId: number | string, payload: PriorityIntervalInput) => request<PriorityInterval>(
    `/api/api-accounts/priority-intervals/${encodeURIComponent(String(intervalId))}`,
    { method: "PUT", body: JSON.stringify(payload) },
    NO_FRONTEND_TIMEOUT,
  ),
  deletePriorityInterval: (intervalId: number | string) => request<{ message: string }>(
    `/api/api-accounts/priority-intervals/${encodeURIComponent(String(intervalId))}`,
    { method: "DELETE" },
  ),
  setApiAccountPriorityInterval: (accountId: number | string, payload: PriorityIntervalAssignment) => request<ApiAccount>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/priority-interval`,
    { method: "PUT", body: JSON.stringify(payload) },
    NO_FRONTEND_TIMEOUT,
  ),
  moveApiAccountPriority: (accountId: number | string, payload: PriorityTieMoveInput) => request<PriorityRebalanceResult>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/priority-order`,
    { method: "PUT", body: JSON.stringify(payload) },
    NO_FRONTEND_TIMEOUT,
  ),
  rebalancePriorityIntervals: () => request<PriorityRebalanceResult>(
    "/api/api-accounts/priority-intervals/rebalance",
    { method: "POST" },
    NO_FRONTEND_TIMEOUT,
  ),
  updateApiAccount: (accountId: number | string, payload: ApiAccountUpdate) => request<ApiAccount>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}`,
    { method: "PUT", body: JSON.stringify(payload) },
  ),
  setApiAccountEnabled: (accountId: number | string, enabled: boolean, expectedIdentityFingerprint: string) => request<ApiAccount>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/enabled`,
    { method: "PATCH", body: JSON.stringify({ enabled, expected_identity_fingerprint: expectedIdentityFingerprint }) },
  ),
  testApiAccountAvailability: (accountId: number | string, expectedIdentityFingerprint: string) => request<ApiAccountAvailabilityTestResult>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/availability-test`,
    { method: "POST", body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }) },
    NO_FRONTEND_TIMEOUT,
  ),
  testApiAccountConnection: (accountId: number | string, expectedIdentityFingerprint: string) => request<ApiAccountConnectionTestResult>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/connection-test`,
    { method: "POST", body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }) },
    NO_FRONTEND_TIMEOUT,
  ),
  deleteRemoteApiAccount: (accountId: number | string, expectedIdentityFingerprint: string) => request<{ message: string }>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/remote`,
    { method: "DELETE", body: JSON.stringify({ confirmed_account_id: accountId, expected_identity_fingerprint: expectedIdentityFingerprint }) },
  ),
  upstreamChangeLogs: (limit = 50, beforeId?: number | null, filters?: UpstreamChangeLogFilters) =>
    request<UpstreamChangeLog[]>(upstreamChangeLogsPath(limit, beforeId, filters)),
  upstreamChangeEvents: (
    limit = 50,
    beforeId?: number | null,
    filters?: UpstreamChangeLogFilters,
    category: "all" | "upstream" | "account_rate" = "all",
    page = 1,
  ) => request<ChangeLogPage<UpstreamChangeEvent>>(
    `/api/api-accounts/upstream-change-events?${upstreamChangeLogsQuery(limit, beforeId, filters, page)}${category === "all" ? "" : `&category=${category}`}`,
  ),
  accountSchedulingChangeEvents: (limit = 50, beforeId?: number | null, filters?: UpstreamChangeLogFilters, page = 1) =>
    request<ChangeLogPage<AccountSchedulingChangeEvent>>(
      `/api/api-accounts/scheduling-change-events?${upstreamChangeLogsQuery(limit, beforeId, filters, page)}`,
    ),
  changeLogUnreadCounts: () => request<ChangeLogUnreadCounts>("/api/api-accounts/change-log-unread-counts"),
  markUpstreamChangesRead: (throughId: number, category: "all" | "upstream" | "account_rate" = "all") =>
    request<{ message: string }>(`/api/api-accounts/upstream-change-events/mark-read?category=${category}`, {
      method: "POST",
      body: JSON.stringify({ through_id: throughId }),
    }),
  markAccountSchedulingChangesRead: (throughId: number) =>
    request<{ message: string }>("/api/api-accounts/scheduling-change-events/mark-read", {
      method: "POST",
      body: JSON.stringify({ through_id: throughId }),
    }),
  deleteApiAccount: (accountId: number | string, expectedIdentityFingerprint: string) => request<{ message: string }>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}`,
    { method: "DELETE", body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }) },
  ),
  discoverApiAccount: (accountId: number | string, expectedIdentityFingerprint: string) => request<ApiAccount>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/discover`,
    { method: "POST", body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }) },
    90_000,
  ),
  applyApiAccountRate: (accountId: number | string, confirmedTargetRate: number, expectedIdentityFingerprint: string) => request<ApiAccount>(
    `/api/api-accounts/${encodeURIComponent(String(accountId))}/apply`,
    { method: "POST", body: JSON.stringify({ confirmed_target_rate: confirmedTargetRate, expected_identity_fingerprint: expectedIdentityFingerprint }) },
    90_000,
  ),
  upstreams: (refresh = false) => request<UpstreamOverviewResponse>(`/api/upstreams?refresh=${refresh ? "true" : "false"}`),
  upstreamCredentials: (upstreamId: number | string) => request<UpstreamCredentials>(
    `/api/upstreams/${encodeURIComponent(String(upstreamId))}/credentials`,
    { cache: "no-store" },
  ),
  upstreamUsageHistory: (upstreamId: number | string, filters?: UpstreamUsageHistoryFilters) =>
    request<UpstreamUsageHistory>(upstreamUsageHistoryPath(upstreamId, filters), {}, 90_000),
  syncApiKeyInventory: () => request<UpstreamOverviewResponse>("/api/upstreams/sync-inventory", { method: "POST" }, 90_000),
  updateUpstream: (upstreamId: number | string, payload: UpstreamUpdate) => request<Upstream>(
    `/api/upstreams/${encodeURIComponent(String(upstreamId))}`,
    { method: "PUT", body: JSON.stringify(payload) },
  ),
  deleteUpstream: (upstreamId: number | string) =>
    request<{ message: string }>(`/api/upstreams/${encodeURIComponent(String(upstreamId))}`, { method: "DELETE" }),
  discoverUpstream: (upstreamId: number | string) =>
    request<Upstream>(`/api/upstreams/${encodeURIComponent(String(upstreamId))}/discover`, { method: "POST" }, 90_000),
  refreshUpstreamMonitors: (upstreamId: number | string) => request<UpstreamMonitorsResponse>(
    `/api/upstreams/${encodeURIComponent(String(upstreamId))}/upstream-monitors/refresh`,
    { method: "POST" },
    90_000,
  ),
  discoverAllUpstreams: (signal?: AbortSignal) =>
    request<UpstreamDiscoverAllResult>("/api/upstreams/discover-all", { method: "POST", signal }, NO_FRONTEND_TIMEOUT),
  syncApiKeyAccounts: (
    overview: UpstreamOverviewResponse,
    confirmLegacyBindings: boolean,
    skipUpstreamIds: string[] = [],
    signal?: AbortSignal,
  ) => {
    const accountBindings = confirmLegacyBindings ? upstreamLegacyIdentityBindings(overview) : [];
    const payload: UpstreamDiscoverAllRequest = accountBindings.length
      ? { confirm_legacy_bindings: true, account_bindings: accountBindings, ...(skipUpstreamIds.length ? { skip_upstream_ids: skipUpstreamIds } : {}) }
      : (skipUpstreamIds.length ? { skip_upstream_ids: skipUpstreamIds } : {});
    return request<UpstreamDiscoverAllResult>(
      "/api/upstreams/discover-all",
      { method: "POST", body: JSON.stringify(payload), signal },
      NO_FRONTEND_TIMEOUT,
    );
  },
};

export function upstreamLegacyIdentityBindings(overview: UpstreamOverviewResponse) {
  const accounts = [...overview.upstreams.flatMap((upstream) => upstream.accounts || []), ...overview.unassigned_accounts];
  const bindings = new Map<number, string>();
  for (const account of accounts) {
    if (account.identity_binding_status !== "unbound" && account.api_key_origin_rebind_required !== true) continue;
    const accountId = Number(account.management_account_id);
    const fingerprint = String(account.identity_fingerprint || "").trim();
    if (!Number.isSafeInteger(accountId) || accountId <= 0 || !/^[a-f0-9]{64}$/.test(fingerprint)) {
      throw new Error("API 账号身份信息无效，请刷新后重试。");
    }
    if (bindings.has(accountId)) throw new Error("API 账号列表包含重复 ID，请刷新后重试。");
    bindings.set(accountId, fingerprint);
  }
  return [...bindings].sort(([left], [right]) => left - right).map(([management_account_id, expected_identity_fingerprint]) => ({
    management_account_id,
    expected_identity_fingerprint,
  }));
}

export function upstreamLegacyBindingCounts(overview: UpstreamOverviewResponse) {
  const accounts = [...overview.upstreams.flatMap((upstream) => upstream.accounts || []), ...overview.unassigned_accounts];
  return {
    unbound: accounts.filter((account) => account.identity_binding_status === "unbound").length,
    originRebind: accounts.filter((account) => account.api_key_origin_rebind_required === true).length,
  };
}
