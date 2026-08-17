import { ApiKeyAccountsView } from "./ApiKeyWorkspace";
import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import type { ApiKeySubview } from "../../viewRouting";

function ApiKeySubviewView({ subview }: { subview: ApiKeySubview }) {
  const props = useDashboardPage("apiKeys");
  return <ApiKeyAccountsView {...props} subview={subview} />;
}

export function UpstreamsView() {
  return <ApiKeySubviewView subview="upstreams" />;
}

export function ApiAccountsView() {
  return <ApiKeySubviewView subview="accounts" />;
}

export function PriorityIntervalsView() {
  return <ApiKeySubviewView subview="intervals" />;
}

export function UpstreamChangesView() {
  return <ApiKeySubviewView subview="rate-log" />;
}

export function AccountRateChangesView() {
  return <ApiKeySubviewView subview="account-rate-log" />;
}

export function SchedulingChangesView() {
  return <ApiKeySubviewView subview="schedule-log" />;
}
