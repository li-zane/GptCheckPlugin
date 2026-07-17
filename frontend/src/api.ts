import type {
  Account,
  AccountExceptionRecord,
  AccountLivenessModels,
  AccountLivenessTestResult,
  AppEvent,
  AppSettings,
  AppSettingsUpdate,
  DeactivatedCleanupResult,
  Mailbox,
  MailboxImportResult,
  MailMessage,
  PhoneImportResult,
  PhoneNumber,
  PriorityInterval,
  PriorityIntervalAssignment,
  PriorityIntervalInput,
  PriorityRebalanceResult,
  RefreshJob,
  SelectedAccountDeleteItem,
  Sub2ApiPortScanResult,
  SubscriptionRefreshResult,
  Summary,
  SyncResult,
  UsageEstimate,
  UsageLimitSamples,
  UsageRefreshResult,
  UpstreamAccount,
  UpstreamAccountUpdate,
  UpstreamChannel,
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
  scanSub2Api: () => request<Sub2ApiPortScanResult>("/api/settings/scan-sub2api", { method: "POST" }),
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
  upstreamChannels: () => request<UpstreamChannelsResponse>("/api/upstream-channels"),
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
  discoverUpstreamChannel: (channelId: number | string) =>
    request<UpstreamChannel>(
      `/api/upstream-channels/${encodeURIComponent(String(channelId))}/discover`,
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
    signal?: AbortSignal,
  ) => {
    const accountBindings = confirmLegacyBindings
      ? upstreamLegacyIdentityBindings(overview)
      : [];
    const payload: UpstreamChannelDiscoverAllRequest = accountBindings.length
      ? {
          confirm_legacy_bindings: true,
          account_bindings: accountBindings,
        }
      : {};
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
