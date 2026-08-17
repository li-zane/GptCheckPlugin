export const viewPaths = {
  overview: "/overview",
  accounts: "/accounts",
  "api-keys": "/api-keys",
  usage: "/usage",
  "usage-samples": "/usage-samples",
  mailboxes: "/mailboxes",
  phones: "/phones",
  history: "/history",
  settings: "/settings",
} as const;

export type View = keyof typeof viewPaths;
export type ApiKeySubview = "accounts" | "upstreams" | "intervals" | "rate-log" | "account-rate-log" | "schedule-log";
export type AppRoute = { view: View; apiKeySubview: ApiKeySubview };

export const apiKeySubviewPaths: Record<ApiKeySubview, string> = {
  accounts: "/api-keys/accounts",
  upstreams: "/api-keys/upstreams",
  intervals: "/api-keys/priority-intervals",
  "rate-log": "/api-keys/upstream-changes",
  "account-rate-log": "/api-keys/account-rate-changes",
  "schedule-log": "/api-keys/scheduling-changes",
};

const viewPathEntries = Object.entries(viewPaths) as Array<[View, string]>;
const apiKeySubviewPathEntries = Object.entries(apiKeySubviewPaths) as Array<[ApiKeySubview, string]>;

export function pathForView(view: View): string {
  return viewPaths[view];
}

export function pathForApiKeySubview(subview: ApiKeySubview): string {
  return apiKeySubviewPaths[subview];
}

export function pathForRoute(route: AppRoute): string {
  return route.view === "api-keys"
    ? pathForApiKeySubview(route.apiKeySubview)
    : pathForView(route.view);
}

export function routeFromPath(pathname: string): AppRoute {
  const normalizedPath = normalizePathname(pathname);
  // The API Key landing page is the upstream overview. Keep account management
  // addressable at its own path so the landing route can have a stable default.
  if (normalizedPath === "/api-keys") {
    return { view: "api-keys", apiKeySubview: "upstreams" };
  }
  const apiKeySubview = apiKeySubviewPathEntries.find(([, path]) => path === normalizedPath)?.[0];
  if (apiKeySubview) return { view: "api-keys", apiKeySubview };
  const view = viewPathEntries.find(([, path]) => path === normalizedPath)?.[0] || "overview";
  return { view, apiKeySubview: "accounts" };
}

export function viewFromPath(pathname: string): View {
  return routeFromPath(pathname).view;
}

export function normalizePathname(pathname: string): string {
  const trimmed = pathname.trim();
  if (!trimmed || trimmed === "/") return "/";
  return trimmed.replace(/\/+$/, "") || "/";
}
