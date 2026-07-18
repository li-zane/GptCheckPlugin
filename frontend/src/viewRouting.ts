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
export type ApiKeySubview = "accounts" | "channels" | "intervals" | "rate-log";
export type AppRoute = { view: View; apiKeySubview: ApiKeySubview };

export const apiKeySubviewPaths: Record<ApiKeySubview, string> = {
  accounts: "/api-keys",
  channels: "/api-keys/channels",
  intervals: "/api-keys/priority-intervals",
  "rate-log": "/api-keys/upstream-changes",
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
