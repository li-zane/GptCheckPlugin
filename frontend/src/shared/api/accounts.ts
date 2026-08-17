import type {
  Account,
  AccountEditPreset,
  AccountEditPresetConfiguration,
  AccountEditResult,
  AccountEditor,
  AccountEditUpdate,
  AccountExceptionRecord,
  AccountLivenessModels,
  AccountLivenessTestResult,
  AccountNotes,
  AppEvent,
  DeactivatedCleanupResult,
  RefreshJob,
  SelectedAccountDeleteItem,
  SubscriptionRefreshResult,
  Summary,
  SyncResult,
  UsageEstimate,
  UsageLimitSampleDeleteResult,
  UsageLimitSamples,
  UsageRefreshResult,
} from "../../domain";
import { NO_FRONTEND_TIMEOUT, request } from "./client.ts";

export const accountsApi = {
  summary: () => request<Summary>("/api/dashboard/summary"),
  accounts: () => request<Account[]>("/api/accounts"),
  accountNotes: (accountId: string, signal?: AbortSignal) =>
    request<AccountNotes>(`/api/accounts/${encodeURIComponent(accountId)}/notes`, { signal }),
  updateAccountNotes: (accountId: string, notes: string, expectedIdentityFingerprint: string) =>
    request<AccountNotes>(`/api/accounts/${encodeURIComponent(accountId)}/notes`, {
      method: "PUT",
      body: JSON.stringify({ notes, expected_identity_fingerprint: expectedIdentityFingerprint }),
    }),
  accountEditor: (accountId: string, signal?: AbortSignal) =>
    request<AccountEditor>(`/api/accounts/editor/${encodeURIComponent(accountId)}`, { signal }),
  updateAccountEditor: (accountId: string, payload: AccountEditUpdate) =>
    request<AccountEditResult>(`/api/accounts/editor/${encodeURIComponent(accountId)}`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  accountEditPresets: (platform?: string) => request<AccountEditPreset[]>(
    `/api/accounts/edit-presets${platform ? `?platform=${encodeURIComponent(platform)}` : ""}`,
  ),
  createAccountEditPreset: (payload: { name: string; platform: string; configuration: AccountEditPresetConfiguration }) =>
    request<AccountEditPreset>("/api/accounts/edit-presets", { method: "POST", body: JSON.stringify(payload) }),
  updateAccountEditPreset: (presetId: number, payload: { name: string; configuration: AccountEditPresetConfiguration }) =>
    request<AccountEditPreset>(`/api/accounts/edit-presets/${presetId}`, { method: "PUT", body: JSON.stringify(payload) }),
  deleteAccountEditPreset: (presetId: number) =>
    request<{ message: string }>(`/api/accounts/edit-presets/${presetId}`, { method: "DELETE" }),
  applyAccountEditPreset: (presetId: number, accountId: string, expectedIdentityFingerprint: string) =>
    request<AccountEditResult>(`/api/accounts/edit-presets/${presetId}/apply/${encodeURIComponent(accountId)}`, {
      method: "POST",
      body: JSON.stringify({ expected_identity_fingerprint: expectedIdentityFingerprint }),
    }),
  accountLivenessModels: (accountIds: string[], signal?: AbortSignal) => request<AccountLivenessModels>(
    "/api/accounts/liveness-models",
    { method: "POST", body: JSON.stringify({ account_ids: accountIds }), signal },
    NO_FRONTEND_TIMEOUT,
  ),
  testAccountLiveness: (accountIds: string[], modelId: string, signal?: AbortSignal) => request<AccountLivenessTestResult>(
    "/api/accounts/liveness-test",
    { method: "POST", body: JSON.stringify({ account_ids: accountIds, model_id: modelId }), signal },
    NO_FRONTEND_TIMEOUT,
  ),
  sync: (signal?: AbortSignal) =>
    request<SyncResult>("/api/accounts/sync", { method: "POST", signal }, NO_FRONTEND_TIMEOUT),
  usageEstimate: (refresh = true) =>
    request<UsageEstimate>(`/api/accounts/usage-estimate?refresh=${refresh ? "true" : "false"}`, {}, 180_000),
  usageLimitSamples: () => request<UsageLimitSamples>("/api/accounts/usage-limit-samples"),
  deleteUsageLimitSample: (id: number) =>
    request<{ message: string }>(`/api/accounts/usage-limit-samples/${id}`, { method: "DELETE" }),
  deleteUsageLimitSamples: (ids: number[]) => request<UsageLimitSampleDeleteResult>(
    "/api/accounts/usage-limit-samples",
    { method: "DELETE", body: JSON.stringify({ sample_ids: ids }) },
  ),
  refreshUsageWindows: () =>
    request<UsageRefreshResult>("/api/accounts/usage-refresh", { method: "POST" }, 180_000),
  refreshSubscriptions: () =>
    request<SubscriptionRefreshResult>("/api/accounts/subscription-refresh", { method: "POST" }, 180_000),
  updateAccountUsageEstimate: (id: number, enabled: boolean) =>
    request<{ message: string }>(`/api/accounts/${id}/usage-estimate`, {
      method: "PATCH",
      body: JSON.stringify({ enabled }),
    }),
  refresh: (email: string) => request<RefreshJob>("/api/accounts/refresh", {
    method: "POST",
    body: JSON.stringify({ email }),
  }),
  deleteDeactivatedAccounts: () =>
    request<DeactivatedCleanupResult>("/api/accounts/deactivated", { method: "DELETE" }, 180_000),
  deleteSelectedAccounts: (accounts: SelectedAccountDeleteItem[]) => request<DeactivatedCleanupResult>(
    "/api/accounts/delete-selected",
    { method: "POST", body: JSON.stringify({ accounts }) },
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
  deleteExceptionRecord: (id: number) =>
    request<{ message: string }>(`/api/accounts/exception-records/${id}`, { method: "DELETE" }),
  clearHistory: () => request<{ message: string }>("/api/accounts/history", { method: "DELETE" }),
};
