import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, createBrowserRouter, useRouteError } from "react-router-dom";

import DashboardController from "./dashboard/DashboardController";
import { AuthGate } from "./auth/AuthGate";

const OverviewPage = lazy(() => import("../pages/overview/OverviewPage"));
const AccountsPage = lazy(() => import("../pages/accounts/AccountsPage"));
const UsagePage = lazy(() => import("../pages/usage/UsagePage"));
const UsageSamplesPage = lazy(() => import("../pages/usage-samples/UsageSamplesPage"));
const MailboxesPage = lazy(() => import("../pages/mailboxes/MailboxesPage"));
const PhonesPage = lazy(() => import("../pages/phones/PhonesPage"));
const HistoryPage = lazy(() => import("../pages/history/HistoryPage"));
const SettingsPage = lazy(() => import("../pages/settings/SettingsPage"));
const ApiKeyAccountsPage = lazy(() => import("../pages/api-keys/AccountsPage"));
const UpstreamsPage = lazy(() => import("../pages/api-keys/UpstreamsPage"));
const PriorityIntervalsPage = lazy(() => import("../pages/api-keys/PriorityIntervalsPage"));
const UpstreamChangesPage = lazy(() => import("../pages/api-keys/UpstreamChangesPage"));
const AccountRateChangesPage = lazy(() => import("../pages/api-keys/AccountRateChangesPage"));
const SchedulingChangesPage = lazy(() => import("../pages/api-keys/SchedulingChangesPage"));

export const routeMeta = {
  overview: { title: "概览" },
  accounts: { title: "OAuth 账号" },
  usage: { title: "额度" },
  "usage-samples": { title: "样本" },
  mailboxes: { title: "邮箱" },
  phones: { title: "手机号" },
  history: { title: "历史" },
  settings: { title: "设置", showSave: true },
  "api-keys/upstreams": { title: "上游" },
  "api-keys/accounts": { title: "API 账号" },
  "api-keys/priority-intervals": { title: "优先级区间" },
  "api-keys/upstream-changes": { title: "上游变化" },
  "api-keys/account-rate-changes": { title: "账号倍率变化" },
  "api-keys/scheduling-changes": { title: "调度变化" },
} as const;

function LazyPage({ children }: { children: ReactNode }) {
  return <Suspense fallback={<div className="empty-state" role="status">正在加载页面</div>}>{children}</Suspense>;
}

function RouteErrorBoundary() {
  const error = useRouteError();
  const message = error instanceof Error ? error.message : "页面加载失败";
  return <div className="empty-state" role="alert"><strong>页面加载失败</strong><span>{message}</span></div>;
}

export const appRouter = createBrowserRouter([
  {
    path: "/",
    element: <AuthGate><DashboardController /></AuthGate>,
    errorElement: <RouteErrorBoundary />,
    children: [
      { index: true, element: <Navigate replace to="/overview" /> },
      { path: "overview", element: <LazyPage><OverviewPage /></LazyPage>, handle: routeMeta.overview },
      { path: "accounts", element: <LazyPage><AccountsPage /></LazyPage>, handle: routeMeta.accounts },
      { path: "usage", element: <LazyPage><UsagePage /></LazyPage>, handle: routeMeta.usage },
      { path: "usage-samples", element: <LazyPage><UsageSamplesPage /></LazyPage>, handle: routeMeta["usage-samples"] },
      { path: "mailboxes", element: <LazyPage><MailboxesPage /></LazyPage>, handle: routeMeta.mailboxes },
      { path: "phones", element: <LazyPage><PhonesPage /></LazyPage>, handle: routeMeta.phones },
      { path: "history", element: <LazyPage><HistoryPage /></LazyPage>, handle: routeMeta.history },
      { path: "settings", element: <LazyPage><SettingsPage /></LazyPage>, handle: routeMeta.settings },
      { path: "api-keys", element: <Navigate replace to="/api-keys/upstreams" /> },
      { path: "api-keys/accounts", element: <LazyPage><ApiKeyAccountsPage /></LazyPage>, handle: routeMeta["api-keys/accounts"] },
      { path: "api-keys/upstreams", element: <LazyPage><UpstreamsPage /></LazyPage>, handle: routeMeta["api-keys/upstreams"] },
      { path: "api-keys/priority-intervals", element: <LazyPage><PriorityIntervalsPage /></LazyPage>, handle: routeMeta["api-keys/priority-intervals"] },
      { path: "api-keys/upstream-changes", element: <LazyPage><UpstreamChangesPage /></LazyPage>, handle: routeMeta["api-keys/upstream-changes"] },
      { path: "api-keys/account-rate-changes", element: <LazyPage><AccountRateChangesPage /></LazyPage>, handle: routeMeta["api-keys/account-rate-changes"] },
      { path: "api-keys/scheduling-changes", element: <LazyPage><SchedulingChangesPage /></LazyPage>, handle: routeMeta["api-keys/scheduling-changes"] },
      { path: "*", element: <Navigate replace to="/overview" /> },
    ],
  },
]);
