import type {
  Account,
  AccountExceptionRecord,
  AppEvent,
  AppSettings,
  AppSettingsUpdate,
  DeactivatedCleanupResult,
  Mailbox,
  MailboxImportResult,
  MailMessage,
  PhoneImportResult,
  PhoneNumber,
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
  UpstreamChannelDiscoverAllResult,
  UpstreamChannelsResponse,
  UpstreamChannelUpdate,
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

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const response = await fetch(path, {
    credentials: "include",
    signal: controller.signal,
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "sub2api-at-guardian",
      ...(init.headers || {}),
    },
    ...init,
  }).catch((error) => {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请稍后重试或检查邮箱令牌/网络。");
    }
    throw error;
  }).finally(() => window.clearTimeout(timeout));

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

export function upstreamRateChangeLogsPath(
  limit = 50,
  beforeId?: number | null,
  filters?: { startDate?: string; endDate?: string; timeZone?: string },
) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeId) params.set("before_id", String(beforeId));
  if (filters?.startDate) params.set("start_date", filters.startDate);
  if (filters?.endDate) params.set("end_date", filters.endDate);
  if (filters?.timeZone) params.set("time_zone", filters.timeZone);
  return `/api/upstream-accounts/rate-change-logs?${params.toString()}`;
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
  sync: () => request<SyncResult>("/api/accounts/sync", { method: "POST" }, 180_000),
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
  updateUpstreamAccount: (accountId: number | string, payload: UpstreamAccountUpdate) =>
    request<UpstreamAccount>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  setUpstreamAccountEnabled: (accountId: number | string, enabled: boolean) =>
    request<UpstreamAccount>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}/enabled`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  deleteRemoteUpstreamAccount: (accountId: number | string) =>
    request<{ message: string }>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}/remote`, {
      method: "DELETE",
      body: JSON.stringify({ confirmed_account_id: accountId }),
    }),
  upstreamRateChangeLogs: (
    limit = 50,
    beforeId?: number | null,
    filters?: { startDate?: string; endDate?: string; timeZone?: string },
  ) => request<UpstreamRateChangeLog[]>(upstreamRateChangeLogsPath(limit, beforeId, filters)),
  deleteUpstreamAccount: (accountId: number | string) =>
    request<{ message: string }>(`/api/upstream-accounts/${encodeURIComponent(String(accountId))}`, { method: "DELETE" }),
  discoverUpstreamAccount: (accountId: number | string) =>
    request<UpstreamAccount>(
      `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/discover`,
      { method: "POST" },
      90_000,
    ),
  applyUpstreamAccountRate: (accountId: number | string, confirmedTargetRate: number) =>
    request<UpstreamAccount>(
      `/api/upstream-accounts/${encodeURIComponent(String(accountId))}/apply`,
      {
        method: "POST",
        body: JSON.stringify({ confirmed_target_rate: confirmedTargetRate }),
      },
      90_000,
    ),
  upstreamChannels: () => request<UpstreamChannelsResponse>("/api/upstream-channels"),
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
  discoverAllUpstreamChannels: () =>
    request<UpstreamChannelDiscoverAllResult>("/api/upstream-channels/discover-all", { method: "POST" }, 180_000),
};
