import { createContext, useContext, type ComponentProps, type ReactNode } from "react";

import type { ApiKeyAccountsView } from "../../features/api-accounts/ApiKeyWorkspace";
import type { HistoryView } from "../../HistoryView";
import type { AccountsView } from "../../features/accounts/AccountsView";
import type { MailboxView } from "../../features/mailboxes/MailboxView";
import type { Overview } from "../../features/overview/OverviewView";
import type { PhoneView } from "../../features/phones/PhoneView";
import type { SettingsView } from "../../features/settings/SettingsView";
import type { UsageEstimateView } from "../../features/usage/UsageEstimateView";
import type { UsageLimitSamplesView } from "../../features/usage-samples/UsageLimitSamplesView";

export type DashboardPages = {
  accounts: ComponentProps<typeof AccountsView>;
  apiKeys: ComponentProps<typeof ApiKeyAccountsView>;
  history: ComponentProps<typeof HistoryView>;
  mailboxes: ComponentProps<typeof MailboxView>;
  overview: ComponentProps<typeof Overview>;
  phones: ComponentProps<typeof PhoneView>;
  settings: ComponentProps<typeof SettingsView>;
  usage: ComponentProps<typeof UsageEstimateView>;
  usageSamples: ComponentProps<typeof UsageLimitSamplesView>;
};

const DashboardContext = createContext<DashboardPages | null>(null);

export function DashboardProvider({ children, value }: { children: ReactNode; value: DashboardPages }) {
  return <DashboardContext.Provider value={value}>{children}</DashboardContext.Provider>;
}

export function useDashboardPage<Key extends keyof DashboardPages>(key: Key): DashboardPages[Key] {
  const context = useContext(DashboardContext);
  if (!context) throw new Error("Dashboard page rendered outside DashboardProvider");
  return context[key];
}
