import type { BulkDeleteResult, PhoneImportResult, PhoneNumber } from "../../domain";
import { request } from "./client.ts";

export const phonesApi = {
  phones: () => request<PhoneNumber[]>("/api/phones"),
  exportPhones: () => request<{ message: string }>("/api/phones/export"),
  importPhones: (content: string) => request<PhoneImportResult>("/api/phones/import", {
    method: "POST",
    body: JSON.stringify({ content }),
  }),
  refreshPhoneStatuses: () =>
    request<{ message: string }>("/api/phones/status-refresh", { method: "POST" }, 180_000),
  updatePhoneBindings: (id: number, accountEmails: string[]) =>
    request<{ message: string }>(`/api/phones/${id}/bindings`, {
      method: "PUT",
      body: JSON.stringify({ account_emails: accountEmails }),
    }),
  deletePhone: (id: number) => request<{ message: string }>(`/api/phones/${id}`, { method: "DELETE" }),
  deletePhones: (ids: number[]) => request<BulkDeleteResult>("/api/phones/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ ids }),
  }),
};
