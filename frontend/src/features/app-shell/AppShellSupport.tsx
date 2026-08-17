import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  ArrowUpDown,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CheckCircle2,
  CircleHelp,
  Clock3,
  Copy,
  Database,
  Download,
  ExternalLink,
  Globe2,
  Inbox,
  Image as ImageIcon,
  KeyRound,
  Link2,
  LogOut,
  Mail,
  MailOpen,
  Moon,
  PauseCircle,
  Pencil,
  Play,
  Plus,
  Radar,
  RefreshCcw,
  Save,
  Search,
  Send,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Smartphone,
  Sparkles,
  StickyNote,
  Sun,
  TimerReset,
  Trash2,
  Upload,
  UserRoundX,
  UsersRound,
  X,
  ZoomIn,
  type LucideIcon,
} from "lucide-react";
import { FormEvent, lazy, Suspense, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  Account,
  AccountExceptionRecord,
  AccountLivenessModel,
  AccountLivenessTestResult,
  AccountNotes,
  AccountUsageEstimate,
  ApiKeyViewOperation,
  AppEvent,
  AppSettings,
  AppSettingsUpdate,
  Mailbox,
  MailboxCredentialDetail,
  MailMessage,
  PhoneNumber,
  RefreshJob,
  SelectedAccountDeleteItem,
  Summary,
  UsageEstimate,
  UsageGroupRef,
  UsageLimitSamples,
  UsageLimitDefaultRanges,
  UsageLimitPlanRanges,
  UsageTokenHistory,
  UsageWindowAggregate,
  UsageWindowEstimate,
  UpstreamChangeLog,
  Upstream,
  UpstreamOverviewResponse,
} from "../../domain";
import { AccountCounts, cloneUsageLimitPlanRanges, defaultTimeZone, downloadTextFile, formatDate, formatFullDate, parseApiDate, toSub2ApiInstanceUrl, useDisplayTimeZone } from "../shared/LegacyDisplay";

type Theme = "light" | "dark";

const themeStorageKey = "sub2api-at-theme";

const refreshClockIntervalMs = 30_000;

function ThemeToggle({
  theme,
  onToggleTheme,
  compact = false,
  sidebar = false,
}: {
  theme: Theme;
  onToggleTheme: () => void;
  compact?: boolean;
  sidebar?: boolean;
}) {
  const isDark = theme === "dark";
  const Icon = isDark ? Sun : Moon;
  const label = isDark ? "浅色" : "暗色";
  const title = isDark ? "切换到浅色模式" : "切换到暗色模式";
  const className = sidebar
    ? "ghost-button theme-toggle sidebar-theme-toggle"
    : compact
      ? "secondary-button theme-toggle compact"
      : "secondary-button theme-toggle";

  return (
    <button
      aria-label={title}
      className={className}
      onClick={onToggleTheme}
      title={title}
      type="button"
    >
      <Icon size={17} />
      <span>{label}</span>
    </button>
  );
}

function getInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";

  try {
    const storedTheme = window.localStorage.getItem(themeStorageKey);
    if (storedTheme === "light" || storedTheme === "dark") {
      return storedTheme;
    }
  } catch {
    // Ignore storage failures in restricted browser contexts.
  }

  const prefersDark = typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
  return prefersDark ? "dark" : "light";
}

function ToolbarTimeButton({
  disabled,
  icon: Icon,
  label,
  loading,
  onClick,
  time,
}: {
  disabled: boolean;
  icon: LucideIcon;
  label: string;
  loading: boolean;
  onClick: () => void;
  time: string | null;
}) {
  const timeZone = useDisplayTimeZone();
  const shortTime = time ? formatClockTime(time, timeZone) : "--:--";
  const fullTime = time ? formatFullDate(time, timeZone) : "暂无刷新记录";
  return (
    <button className="secondary-button toolbar-time-button" disabled={disabled} onClick={onClick} title={`上次刷新时间 ${fullTime}`} type="button">
      <Icon className={loading ? "spin" : ""} size={17} />
      <span>{label}</span>
      <span className="toolbar-time">{shortTime}</span>
    </button>
  );
}

function ensureFaviconLink() {
  const existing = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  if (existing) return existing;
  const favicon = document.createElement("link");
  favicon.rel = "icon";
  document.head.appendChild(favicon);
  return favicon;
}

function versionedSiteLogoUrl(url: string, updatedAt: string | null | undefined) {
  const value = url.trim() || "/logo.png";
  if (!updatedAt || value.startsWith("data:") || value.startsWith("blob:")) return value;
  try {
    const next = new URL(value, window.location.origin);
    next.searchParams.set("v", updatedAt);
    return value.startsWith("http://") || value.startsWith("https://")
      ? next.toString()
      : `${next.pathname}${next.search}${next.hash}`;
  } catch {
    return value;
  }
}

function appSettingsEqual(left: AppSettings, right: AppSettings) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function accountDisplayCounts(accounts: Account[]): AccountCounts {
  const actual = accounts.length;
  const deduped = new Set(accounts.map((account) => account.email.toLowerCase())).size;
  return { actual, deduped, duplicates: Math.max(actual - deduped, 0) };
}

function selectedAccountDeleteItem(account: Account): SelectedAccountDeleteItem {
  return {
    management_account_id: account.management_account_id,
    snapshot_id: account.id > 0 ? account.id : null,
  };
}

function latestEventByKinds(events: AppEvent[], kinds: string[]) {
  return events.find((event) => kinds.includes(event.kind)) || null;
}

function credentialBindingOrigin(value: string) {
  try {
    return new URL(toSub2ApiInstanceUrl(value)).origin.toLowerCase();
  } catch {
    return value.trim().toLowerCase();
  }
}

function useRefreshClock() {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), refreshClockIntervalMs);
    return () => window.clearInterval(timer);
  }, []);

  return now;
}

function formatClockTime(value: string, timeZone = defaultTimeZone) {
  const date = parseApiDate(value);
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: defaultTimeZone,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }
}

export { ThemeToggle, ToolbarTimeButton, accountDisplayCounts, appSettingsEqual, cloneUsageLimitPlanRanges, credentialBindingOrigin, downloadTextFile, ensureFaviconLink, formatDate, getInitialTheme, latestEventByKinds, selectedAccountDeleteItem, useRefreshClock, versionedSiteLogoUrl };
