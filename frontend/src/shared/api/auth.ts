import { request } from "./client.ts";

export const authApi = {
  health: () => request<{ status: string }>("/api/health"),
  me: () => request<{ message: string }>("/api/auth/me"),
  login: (adminKey: string) => request<{ message: string }>("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ admin_key: adminKey }),
  }),
  logout: () => request<{ message: string }>("/api/auth/logout", { method: "POST" }),
};
