import type { Account, AppEvent, Mailbox, MailboxImportResult, MailMessage, RefreshJob, Summary, SyncResult } from "./types";

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
    let message = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      message = data.detail || data.message || message;
    } catch {
      // Keep the HTTP status message.
    }
    throw new Error(message);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
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
  sync: () => request<SyncResult>("/api/accounts/sync", { method: "POST" }),
  refresh: (email: string) =>
    request<RefreshJob>("/api/accounts/refresh", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  jobs: () => request<RefreshJob[]>("/api/accounts/jobs"),
  events: () => request<AppEvent[]>("/api/accounts/events"),
  mailboxes: () => request<Mailbox[]>("/api/mailboxes"),
  mailboxMessages: (id: number, folder: "inbox" | "junk") =>
    request<MailMessage[]>(`/api/mailboxes/${id}/messages?folder=${folder}&limit=10`, {}, 50_000),
  importMailboxes: (content: string, defaultProvider: string) =>
    request<MailboxImportResult>("/api/mailboxes/import", {
      method: "POST",
      body: JSON.stringify({ content, default_provider: defaultProvider }),
    }),
  deleteMailbox: (id: number) => request<{ message: string }>(`/api/mailboxes/${id}`, { method: "DELETE" }),
};
