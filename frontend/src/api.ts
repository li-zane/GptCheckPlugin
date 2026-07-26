import type {
  Account,
  AccountSchedulingChangeEvent,
  AccountExceptionRecord,
  AccountLivenessModels,
  AccountLivenessTestResult,
  AppEvent,
  AppSettings,
  AppSettingsUpdate,
  ChangeLogPage,
  ChangeLogUnreadCounts,
  DeactivatedCleanupResult,
  Mailbox,
  MailboxImportResult,
  MailMessage,
  PhoneImportResult,
  PhoneNumber,
  PriorityInterval,
  PriorityIntervalAssignment,
  PriorityTieMoveInput,
  PriorityIntervalInput,
  PriorityRebalanceResult,
  RefreshJob,
  SelectedAccountDeleteItem,
  SiteLogoUpdateResult,
  Sub2ApiPortScanResult,
  SubscriptionRefreshResult,
  Summary,
  SyncResult,
  UsageEstimate,
  UsageLimitSamples,
  UsageRefreshResult,
  UpstreamAccount,
  UpstreamAccountAvailabilityTestResult,
  UpstreamAccountConnectionTestResult,
  UpstreamAccountUpdate,
  UpstreamChannel,
  UpstreamChannelChangeEvent,
  UpstreamChannelMonitorsResponse,
  UpstreamChannelDiscoverAllRequest,
  UpstreamChannelDiscoverAllResult,
  UpstreamChannelsResponse,
  UpstreamChannelUpdate,
  UpstreamChangeLog,
  UpstreamRateChangeLog,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const AUTH_EXPIRED_EVENT = "sub2api-at-auth-expired";
export const NO_FRONTEND_TIMEOUT = null;

async function request<T>(path: string, init: RequestInit = {}, timeoutMs: number | null = 30_000): Promise<T> {
  const controller = new AbortController();
  const { signal: externalSignal, headers: initHeaders, ...requestInit } = init;
  let timedOut = false;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) {
    abortFromCaller();
  } else {
    externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }
  const timeout = timeoutMs === null
    ? null
    : window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "sub2api-at-guardian",
      ...(initHeaders || {}),
    },
    ...requestInit,
    signal: controller.signal,
  }).catch((error) => {
    if (controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError")) {
      if (timedOut) {
        throw new Error("请求超时，请稍后重试或检查后端和网络状态。");
      }
      throw new DOMException("请求已取消", "AbortError");
    }
    throw error;
  }).finally(() => {
    if (timeout !== null) window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  });

  if (!response.ok) {
    let message = fallbackHttpErrorMessage(response);
    try {
      const data = await response.json();
      message = data.detail || data.message || message;
    } catch {
      // Keep the HTTP status message.
    }
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

type UpstreamChangeLogFilters = { startDate?: string; endDate?: string; timeZone?: string };

function upstreamChangeLogsQuery(
  limit = 50,
  beforeId?: number | null,
  filters?: UpstreamChangeLogFilters,
) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeId) params.set("before_id", String(beforeId));
  if (filters?.startDate) params.set("start_date", filters.startDate);
  if (filters?.endDate) params.set("end_date", filters.endDate);
  if (filters?.timeZone) params.set("time_zone", filters.timeZone);
  return params.toString();
}

export function upstreamChangeLogsPath(
  limit = 50,
  beforeId?: number | null,
  filters?: UpstreamChangeLogFilters,
) {
  return `/api/upstream-accounts/upstream-change-logs?${upstreamChangeLogsQuery(limit, beforeId, filters)}`;
}

export function upstreamRateChangeLogsPath(
  limit = 50,
  beforeId?: number | null,
  filters?: UpstreamChangeLogFilters,
) {
  return `/api/upstream-accounts/rate-change-logs?${upstreamChangeLogsQuery(limit, beforeId, filters)}`;
}

async function requestUpstreamChangeLogs(
  limit = 50,
  beforeId?: number | null,
  filters?: UpstreamChangeLogFilters,
) {
  try {
    return await request<UpstreamChangeLog[]>(upstreamChangeLogsPath(limit, beforeId, filters));
  } catch (error) {
    if (!(error instanceof ApiError) || ![404, 405].includes(error.status)) throw error;
    return request<UpstreamChangeLog[]>(upstreamRateChangeLogsPath(limit, beforeId, filters));
  }
}

function fallbackHttpErrorMessage(response: Response) {
  const statusText = response.statusText.trim();
  const status = statusText ? `${response.status} ${statusText}` : `${response.status}`;
  if (response.status >= 500) {
    return `后端服务异常 (${status})，请查看后端日志。`;
  }
  return `请求失败 (${status})`;
}

export const api = {
  health: () => request<{ status: string }>("/api/health"),
  me: () => request<{ message: string }>("/api/auth/me"),
  login: (adminKey: string) =>
    request<{ message: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ admin_key: adminKey }),
    }),
  logout: () => request<{ message: string }>("/api/auth/logout", { method: "POST" }),
  summary: () => request<Summary>("/api/dashboard/summary"),
  accounts: () => request<Account[]>("/api/accounts"),
  accountLivenessModels: (accountIds: string[], signal?: AbortSignal) =>
    request<AccountLivenessModels>(
      "/api/accounts/liveness-models",
      {
        method: "POST",
        body: JSON.stringify({ account_ids: accountIds }),
        signal,
      },
      NO_FRONTEND_TIMEOUT,
    ),
  testAccountLiveness: (accountIds: string[], modelId: string, signal?: AbortSignal) =>
    request<AccountLivenessTestResult>(
      "/api/accounts/liveness-test",
      {
        method: "POST",
        body: JSON.stringify({ account_ids: accountIds, model_id: modelId }),
        signal,
      },
      NO_FRONTEND_TIMEOUT,
    ),
  sync: (signal?: AbortSignal) =>
    request<SyncResult>("/api/accounts/sync", { method: "POST", signal }, NO_FRONTEND_TIMEOUT),
  usageEstimate: (refresh = true) =>
    request<UsageEstimate>(`/api/accounts/usage-estimate?refresh=${refresh ? "true" : "false"}`, {}, 180_000),
  usageLimitSamples: () => request<UsageLimitSamples>("/api/accounts/usage-limit-samples"),
  deleteUsageLimitSample: (id: number) =>
    request<{ message: string }>(`/api/accounts/usage-limit-samples/${id}`, { method: "DELETE" }),
  refreshUsageWindows: () =>
    request<UsageRefreshResult>("/api/accounts/usage-refresh", { method: "POST" }, 180_000),
  refreshSubscriptions: () =>
    request<SubscriptionRefreshResult>("/api/accounts/subscription-refresh", { method: "POST" }, 180_000),
  updateAccountUsageEstimate: (id: number, enabled: boolean) =>
    request<{ message: string }>(`/api/accounts/${id}/usage-estimate`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  refresh: (email: string) =>
    request<RefreshJob>("/api/accounts/refresh", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  deleteDeactivatedAccounts: () =>
    request<DeactivatedCleanupResult>("/api/accounts/deactivated", { method: "DELETE" }, 180_000),
  deleteSelectedAccounts: (accounts: SelectedAccountDeleteItem[]) =>
    request<DeactivatedCleanupResult>(
      "/api/accounts/delete-selected",
      {
        method: "POST",
        body: JSON.stringify({ accounts }),
      },
      180_000,
    ),
  deleteRemoteAccount: (accountId: string) =>
    request<{ message: string }>(`/api/accounts/remote/${encodeURIComponent(accountId)}`, { method: "DELETE" }),
  updateRemoteAccountDeleteLock: (accountId: string, unlocked: boolean) =>
    request<{ message: string }>(`/api/accounts/remote/${encodeURIComponent(accountId)}/delete-lock`, {
      method: "PUT",
      body: JSON.stringify({ unlocked }),
    }),
  updateAccountRefreshLock: (accountId: number, unlocked: boolean) =>
    request<{ message: string }>(`/api/accounts/${accountId}/refresh-lock`, {
      method: "PUT",
      body: JSON.stringify({ unlocked }),
    }),
  jobs: () => request<RefreshJob[]>("/api/accounts/jobs"),
  events: () => request<AppEvent[]>("/api/accounts/events"),
  exceptionRecords: () => request<AccountExceptionRecord[]>("/api/accounts/exception-records"),
  deleteExceptionRecord: (id: number) => request<{ message: string }>(`/api/accounts/exception-records/${id}`, { method: "DELETE" }),
  clearHistory: () => request<{ message: string }>("/api/accounts/history", { method: "DELETE" }),
  settings: () => request<AppSettings>("/api/settings"),
  updateSettings: (payload: AppSettingsUpdate) =>
    request<AppSettings>("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  updateSiteLogo: (file: File) => request<SiteLogoUpdateResult>("/api/settings/logo", {
    method: "PUT",
    body: file,
    headers: { "Content-Type": file.type },
  }, 60_000),
  resetSiteLogo: () => request<SiteLogoUpdateResult>("/api/settings/logo", { method: "DELETE" }),
  scanSub2Api: () => request<Sub2ApiPortScanResult>("/api/settings/scan-sub2api", { method: "POST" }),
  testNotification: () => request<{ message: string }>("/api/settings/notifications/test", { method: "POST" }),
  mailboxes: () => request<Mailbox[]>("/api/mailboxes"),
  mailboxMessages: (id: number, folder: "inbox" | "junk") =>
    request<MailMessage[]>(`/api/mailboxes/${id}/messages?folder=${folder}&limit=10`, {}, 50_000),
  importMailboxes: (content: string, defaultProvider: string) =>
    request<MailboxImportResult>("/api/mailboxes/import", {
      method: "POST",
      body: JSON.stringify({ content, default_provider: defaultProvider }),
    }),
  deleteMailbox: (id: number) => request<{ message: string }>(`/api/mailboxes/${id}`, { method: "DELETE" }),
  phones: () => request<PhoneNumber[]>("/api/phones"),
  exportPhones: () => request<{ message: string }>("/api/phones/export"),
  importPhones: (content: string) =>
    request<PhoneImportResult>("/api/phones/import", {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  refreshPhoneStatuses: () => request<{ message: string }>("/api/phones/status-refresh", { method: "POST" }, 180_000),
  updatePhoneBindings: (id: number, accountEmails: string[]) =>
    request<{ message: string }>(`/api/phones/${id}/bindings`, {
      method: "PUT",
      body: JSON.stringify({ account_emails: accountEmails }),
    }),
  deletePhone: (id: number) => request<{ message: string }>(`/api/phones/${id}`, { method: "DELETE" }),
  upstreamAccounts: () => request<UpstreamAccount[]>("/api/upstream-accounts"),
  priorityIntervals: () => request<PriorityInterval[]>("/api/upstream-accounts/priority-intervals"),
  createPriorityInterval: (payload: PriorityIntervalInput) =>
    request<PriorityInterval>("/api/upstream-accounts/priority-intervals", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updatePriorityInterval: (intervalId: number | string, payload: PriorityIntervalInput) =>
    request<PriorityInterval>(`/api/upstream-accounts/priority-intervals/${encodeURIComponent(String(intervalId))}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }, NO_FRONTEND_TIMEOUT),
  deletePriorityInterval: (intervalId: number | string) =>
    request<{ message: string }>(`/api/upstream-accounts/priority-intervals/${encodeURIComponent(String(intervalId))}`, {
      method: "DELETE",
    }),
  setUpstreamAccountPriorityInterval: (
    accountId: number | string,
    payload: PriorityIntervalAssignment,
  ) => request<UpstreamAccount>(
    `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/priority-interval`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
    NO_FRONTEND_TIMEOUT,
  ),
  moveUpstreamAccountPriority: (
    accountId: number | string,
    payload: PriorityTieMoveInput,
  ) => request<PriorityRebalanceResult>(
    `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/priority-order`,
    {
      method: "PUT",
      body: JSON.stringify(payload),
    },
    NO_FRONTEND_TIMEOUT,
  ),
  rebalancePriorityIntervals: () => request<PriorityRebalanceResult>(
    "/api/upstream-accounts/priority-intervals/rebalance",
    { method: "POST" },
    NO_FRONTEND_TIMEOUT,
  ),
  updateUpstreamAccount: (accountId: number | string, payload: UpstreamAccountUpdate) =>
    request<UpstreamAccount>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  setUpstreamAccountEnabled: (accountId: number | string, enabled: boolean, expectedIdentityFingerprint: string) =>
    request<UpstreamAccount>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}/enabled`, {
      method: "PATCH",
      body: JSON.stringify({ enabled, expected_identity_fingerprint: expectedIdentityFingerprint }),
    }),
  testUpstreamAccountAvailability: (
    accountId: number | string,
    expectedIdentityFingerprint: string,
  ) => request<UpstreamAccountAvailabilityTestResult>(
    `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/availability-test`,
    {
      method: "POST",
      body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }),
    },
    NO_FRONTEND_TIMEOUT,
  ),
  testUpstreamAccountConnection: (
    accountId: number | string,
    expectedIdentityFingerprint: string,
  ) => request<UpstreamAccountConnectionTestResult>(
    `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/connection-test`,
    {
      method: "POST",
      body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }),
    },
    NO_FRONTEND_TIMEOUT,
  ),
  deleteRemoteUpstreamAccount: (accountId: number | string, expectedIdentityFingerprint: string) =>
    request<{ message: string }>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}/remote`, {
      method: "DELETE",
      body: JSON.stringify({
        confirmed_account_id: accountId,
        expected_identity_fingerprint: expectedIdentityFingerprint,
      }),
    }),
  upstreamChangeLogs: (
    limit = 50,
    beforeId?: number | null,
    filters?: UpstreamChangeLogFilters,
  ) => requestUpstreamChangeLogs(limit, beforeId, filters),
  upstreamRateChangeLogs: (
    limit = 50,
    beforeId?: number | null,
    filters?: UpstreamChangeLogFilters,
  ) => request<UpstreamRateChangeLog[]>(upstreamRateChangeLogsPath(limit, beforeId, filters)),
  upstreamChannelChangeEvents: (
    limit = 50,
    beforeId?: number | null,
    filters?: UpstreamChangeLogFilters,
    category: "all" | "upstream" | "account_rate" = "all",
  ) => request<ChangeLogPage<UpstreamChannelChangeEvent>>(
    `/api/upstream-accounts/channel-change-events?${upstreamChangeLogsQuery(limit, beforeId, filters)}${category === "all" ? "" : `&category=${category}`}`,
  ),
  accountSchedulingChangeEvents: (
    limit = 50,
    beforeId?: number | null,
    filters?: UpstreamChangeLogFilters,
  ) => request<ChangeLogPage<AccountSchedulingChangeEvent>>(
    `/api/upstream-accounts/scheduling-change-events?${upstreamChangeLogsQuery(limit, beforeId, filters)}`,
  ),
  changeLogUnreadCounts: () => request<ChangeLogUnreadCounts>(
    "/api/upstream-accounts/change-log-unread-counts",
  ),
  markUpstreamChannelChangesRead: (
    throughId: number,
    category: "all" | "upstream" | "account_rate" = "all",
  ) => request<{ message: string }>(
    `/api/upstream-accounts/channel-change-events/mark-read?category=${category}`,
    { method: "POST", body: JSON.stringify({ through_id: throughId }) },
  ),
  markAccountSchedulingChangesRead: (throughId: number) => request<{ message: string }>(
    "/api/upstream-accounts/scheduling-change-events/mark-read",
    { method: "POST", body: JSON.stringify({ through_id: throughId }) },
  ),
  deleteUpstreamAccount: (accountId: number | string, expectedIdentityFingerprint: string) =>
    request<{ message: string }>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}`, {
      method: "DELETE",
      body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }),
    }),
  discoverUpstreamAccount: (accountId: number | string, expectedIdentityFingerprint: string) =>
    request<UpstreamAccount>(
      `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/discover`,
      {
        method: "POST",
        body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }),
      },
      90_000,
    ),
  applyUpstreamAccountRate: (
    accountId: number | string,
    confirmedTargetRate: number,
    expectedIdentityFingerprint: string,
  ) =>
    request<UpstreamAccount>(
      `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/apply`,
      {
        method: "POST",
        body: JSON.stringify({
          confirmed_target_rate: confirmedTargetRate,
          expected_identity_fingerprint: expectedIdentityFingerprint,
        }),
      },
      90_000,
    ),
  upstreamChannels: (refresh = false) =>
    request<UpstreamChannelsResponse>(`/api/upstream-channels?refresh=${refresh ? "true" : "false"}`),
  syncApiKeyInventory: () =>
    request<UpstreamChannelsResponse>(
      "/api/upstream-channels/sync-inventory",
      { method: "POST" },
      90_000,
    ),
  updateUpstreamChannel: (channelId: number | string, payload: UpstreamChannelUpdate) =>
    request<UpstreamChannel>(`/api/upstream-channels/${encodeURIComponent(String(channelId))}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  deleteUpstreamChannel: (channelId: number | string) =>
    request<{ message: string }>(`/api/upstream-channels/${encodeURIComponent(String(channelId))}`, {
      method: "DELETE",
    }),
  discoverUpstreamChannel: (channelId: number | string) =>
    request<UpstreamChannel>(
      `/api/upstream-channels/${encodeURIComponent(String(channelId))}/discover`,
      { method: "POST" },
      90_000,
    ),
  refreshUpstreamChannelMonitors: (channelId: number | string) =>
    request<UpstreamChannelMonitorsResponse>(
      `/api/upstream-channels/${encodeURIComponent(String(channelId))}/channel-monitors/refresh`,
      { method: "POST" },
      90_000,
    ),
  discoverAllUpstreamChannels: (signal?: AbortSignal) =>
    request<UpstreamChannelDiscoverAllResult>(
      "/api/upstream-channels/discover-all",
      { method: "POST", signal },
      NO_FRONTEND_TIMEOUT,
    ),
  syncApiKeyAccounts: (
    overview: UpstreamChannelsResponse,
    confirmLegacyBindings: boolean,
    skipChannelIds: number[] = [],
    signal?: AbortSignal,
  ) => {
    const accountBindings = confirmLegacyBindings
      ? upstreamLegacyIdentityBindings(overview)
      : [];
    const payload: UpstreamChannelDiscoverAllRequest = accountBindings.length
      ? {
          confirm_legacy_bindings: true,
          account_bindings: accountBindings,
          ...(skipChannelIds.length ? { skip_channel_ids: skipChannelIds } : {}),
        }
      : (skipChannelIds.length ? { skip_channel_ids: skipChannelIds } : {});
    return request<UpstreamChannelDiscoverAllResult>(
      "/api/upstream-channels/discover-all",
      {
        method: "POST",
        body: JSON.stringify(payload),
        signal,
      },
      NO_FRONTEND_TIMEOUT,
    );
  },
};

export function upstreamLegacyIdentityBindings(overview: UpstreamChannelsResponse) {
  const accounts = [
    ...overview.channels.flatMap((channel) => channel.accounts || []),
    ...overview.unassigned_accounts,
  ];
  const bindings = new Map<number, string>();
  for (const account of accounts) {
    if (
      account.identity_binding_status !== "unbound"
      && account.api_key_origin_rebind_required !== true
    ) {
      continue;
    }
    const accountId = Number(account.sub2api_account_id);
    const fingerprint = String(account.identity_fingerprint || "").trim();
    if (!Number.isSafeInteger(accountId) || accountId <= 0 || !/^[a-f0-9]{64}$/.test(fingerprint)) {
      throw new Error("API Key 账号身份信息无效，请刷新后重试。");
    }
    if (bindings.has(accountId)) {
      throw new Error("API Key 账号列表包含重复 ID，请刷新后重试。");
    }
    bindings.set(accountId, fingerprint);
  }
  return [...bindings]
    .sort(([left], [right]) => left - right)
    .map(([sub2api_account_id, expected_identity_fingerprint]) => ({
      sub2api_account_id,
      expected_identity_fingerprint,
    }));
}

export function upstreamLegacyBindingCounts(overview: UpstreamChannelsResponse) {
  const accounts = [
    ...overview.channels.flatMap((channel) => channel.accounts || []),
    ...overview.unassigned_accounts,
  ];
  return {
    unbound: accounts.filter((account) => account.identity_binding_status === "unbound").length,
    originRebind: accounts.filter((account) => account.api_key_origin_rebind_required === true).length,
  };
}
