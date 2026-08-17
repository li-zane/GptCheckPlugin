import type { AppSettings, AppSettingsUpdate, ManagementSiteScanResult, SiteLogoUpdateResult } from "../../domain";
import { request } from "./client.ts";

export const settingsApi = {
  settings: () => request<AppSettings>("/api/settings"),
  updateSettings: (payload: AppSettingsUpdate) => request<AppSettings>("/api/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  }),
  updateSiteLogo: (file: File) => request<SiteLogoUpdateResult>("/api/settings/logo", {
    method: "PUT",
    body: file,
    headers: { "Content-Type": file.type },
  }, 60_000),
  resetSiteLogo: () => request<SiteLogoUpdateResult>("/api/settings/logo", { method: "DELETE" }),
  scanManagementSite: () =>
    request<ManagementSiteScanResult>("/api/settings/scan-management-site", { method: "POST" }),
  testNotification: () =>
    request<{ message: string }>("/api/settings/notifications/test", { method: "POST" }),
};
