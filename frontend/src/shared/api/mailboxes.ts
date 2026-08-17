import type { BulkDeleteResult, Mailbox, MailboxCredentialDetail, MailboxExportResult, MailboxImportResult, MailMessage } from "../../domain";
import { request } from "./client.ts";

export const mailboxesApi = {
  mailboxes: () => request<Mailbox[]>("/api/mailboxes"),
  mailboxCredentials: (id: number) => request<MailboxCredentialDetail>(`/api/mailboxes/${id}/credentials`),
  exportMailboxes: (ids: number[]) => request<MailboxExportResult>("/api/mailboxes/export", {
    method: "POST",
    body: JSON.stringify({ ids }),
  }),
  mailboxMessages: (id: number, folder: "inbox" | "junk") =>
    request<MailMessage[]>(`/api/mailboxes/${id}/messages?folder=${folder}&limit=10`, {}, 50_000),
  importMailboxes: (content: string, defaultProvider: string) => request<MailboxImportResult>("/api/mailboxes/import", {
    method: "POST",
    body: JSON.stringify({ content, default_provider: defaultProvider }),
  }),
  deleteMailbox: (id: number) => request<{ message: string }>(`/api/mailboxes/${id}`, { method: "DELETE" }),
  deleteMailboxes: (ids: number[]) => request<BulkDeleteResult>("/api/mailboxes/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ ids }),
  }),
};
