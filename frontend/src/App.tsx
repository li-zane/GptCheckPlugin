import {
  Activity,
  AlertTriangle,
  ChevronDown,
  CheckCircle2,
  Clock3,
  Copy,
  Database,
  ExternalLink,
  Globe2,
  Inbox,
  KeyRound,
  Link2,
  LogOut,
  Mail,
  MailOpen,
  Moon,
  PauseCircle,
  Play,
  Plus,
  Radar,
  RefreshCcw,
  Save,
  Search,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Sun,
  TimerReset,
  Trash2,
  UserRoundX,
  UsersRound,
  X,
  type LucideIcon,
} from "lucide-react";
import { createContext, FormEvent, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { api } from "./api";
import type {
  Account,
  AccountExceptionRecord,
  AccountUsageEstimate,
  AppEvent,
  AppSettings,
  AppSettingsUpdate,
  Mailbox,
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
} from "./types";

type View = "overview" | "accounts" | "usage" | "usage-samples" | "mailboxes" | "phones" | "history" | "settings";
type Theme = "light" | "dark";
type AccountCounts = { actual: number; deduped: number; duplicates: number };
type AccountStatusFilter = "all" | "normal" | "normal-no-rate-limit" | "five-hour-rate-limited" | "seven-day-rate-limited" | "monthly-rate-limited" | "error" | "deactive";
type UsageDetailAccountFilter = "normal" | "rate-limited";
type AccountJumpTarget = { email: string | null; sub2apiAccountId: string | null; requestedAt: number };
type ProblemUnusedQuotaSummary = { accountCount: number; fiveHour: UsageWindowAggregate; sevenDay: UsageWindowAggregate };

const defaultTimeZone = "Asia/Shanghai";
const defaultSiteName = "sub2api AT 刷新机";
const defaultUsageLimitSampleThresholdPercent = 99;
const usageLimitWindowKeys = ["five_hour", "seven_day", "monthly"] as const;
const coreSubscriptionTypes = new Set(["plus", "team", "pro", "free", "k12", "unknown"]);
const defaultUsageLimitPlanRanges: UsageLimitPlanRanges = {
  five_hour: { lower: 15, upper: 25 },
  seven_day: { lower: 100, upper: 140 },
  monthly: { lower: 100, upper: 300 },
};
const defaultUsageLimitRanges = Object.fromEntries(
  [...coreSubscriptionTypes].map((subscriptionType) => [subscriptionType, cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges)]),
) as UsageLimitDefaultRanges;
const sub2ApiApiPrefix = "/api/v1";
const themeStorageKey = "sub2api-at-theme";
const TimeZoneContext = createContext(defaultTimeZone);
const NowContext = createContext(Date.now());
const refreshClockIntervalMs = 30_000;
const timeZoneOptions = [
  { value: "Asia/Shanghai", label: "中国标准时间 · Asia/Shanghai" },
  { value: "UTC", label: "UTC" },
  { value: "Asia/Tokyo", label: "日本 · Asia/Tokyo" },
  { value: "Asia/Singapore", label: "新加坡 · Asia/Singapore" },
  { value: "Europe/London", label: "伦敦 · Europe/London" },
  { value: "America/New_York", label: "纽约 · America/New_York" },
  { value: "America/Los_Angeles", label: "洛杉矶 · America/Los_Angeles" },
];

const allAccountStatusFilterOptions: Array<{ value: AccountStatusFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "normal", label: "正常" },
  { value: "normal-no-rate-limit", label: "正常(不含限流)" },
  { value: "five-hour-rate-limited", label: "5h限流" },
  { value: "seven-day-rate-limited", label: "7d限流" },
  { value: "monthly-rate-limited", label: "月限流" },
  { value: "error", label: "错误" },
  { value: "deactive", label: "封禁" },
];

const emptySummary: Summary = {
  total_accounts: 0,
  actual_accounts: 0,
  deduped_accounts: 0,
  duplicate_accounts: 0,
  error_accounts: 0,
  paused_accounts: 0,
  deactive_accounts: 0,
  refreshing_accounts: 0,
  mailbox_count: 0,
  recent_success: 0,
  recent_failed: 0,
};

const emptySettings: AppSettings = {
  sub2api_base_url: "http://localhost:8080/api/v1",
  sub2api_port: 8080,
  sub2api_base_url_source: "env",
  sub2api_x_api_key_set: false,
  sub2api_x_api_key_hint: null,
  sub2api_auto_recover_state: true,
  automation_paused: false,
  recovery_enabled: false,
  monitor_interval_seconds: 300,
  usage_refresh_enabled: false,
  usage_refresh_interval_seconds: 3600,
  usage_refresh_max_concurrency: 5,
  usage_limit_sample_five_hour_threshold_percent: 0,
  usage_limit_sample_seven_day_threshold_percent: 0,
  usage_limit_default_ranges: defaultUsageLimitRanges,
  refresh_max_concurrency: 1,
  protocol_refresh_max_concurrency: 1,
  browser_refresh_max_concurrency: 1,
  browser_min_available_memory_mb: 500,
  subscription_refresh_batch_size: 3,
  subscription_refresh_max_concurrency: 3,
  last_scan_at: null,
  last_scan_status: null,
  last_scan_message: null,
  display_timezone: defaultTimeZone,
  site_name: defaultSiteName,
};

function App() {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const [authState, setAuthState] = useState<"checking" | "in" | "out">("checking");
  const [view, setView] = useState<View>("overview");
  const [summary, setSummary] = useState<Summary>(emptySummary);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [mailboxes, setMailboxes] = useState<Mailbox[]>([]);
  const [phones, setPhones] = useState<PhoneNumber[]>([]);
  const [jobs, setJobs] = useState<RefreshJob[]>([]);
  const [events, setEvents] = useState<AppEvent[]>([]);
  const [exceptionRecords, setExceptionRecords] = useState<AccountExceptionRecord[]>([]);
  const [accountJumpTarget, setAccountJumpTarget] = useState<AccountJumpTarget | null>(null);
  const [settings, setSettings] = useState<AppSettings>(emptySettings);
  const [usageEstimate, setUsageEstimate] = useState<UsageEstimate | null>(null);
  const [usageLimitSamples, setUsageLimitSamples] = useState<UsageLimitSamples | null>(null);
  const [usageLimitSamplesLoading, setUsageLimitSamplesLoading] = useState(false);
  const [usageLimitSamplesError, setUsageLimitSamplesError] = useState("");
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState("");
  const [usageEstimateRefreshed, setUsageEstimateRefreshed] = useState(false);
  const [notice, setNotice] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const siteName = settings.site_name?.trim() || defaultSiteName;
  const now = useRefreshClock();
  const lastSyncEvent = useMemo(() => latestEventByKinds(events, ["manual_sync", "monitor_sync"]), [events]);
  const lastUsageRefreshEvent = useMemo(
    () => latestEventByKinds(events, ["usage_statistics_refresh", "usage_refresh"]),
    [events],
  );
  const syncActionTime = lastSyncEvent?.created_at ?? null;
  const usageActionTime = lastUsageRefreshEvent?.created_at ?? null;
  const toggleTheme = useCallback(() => setTheme((current) => (current === "dark" ? "light" : "dark")), []);

  const loadAll = useCallback(async ({ includePhones = true }: { includePhones?: boolean } = {}) => {
    const phonePromise = includePhones ? api.phones().catch(() => null) : Promise.resolve<PhoneNumber[] | null>(null);
    const exceptionRecordsPromise = api.exceptionRecords().catch(() => null);
    const [nextSummary, nextAccounts, nextMailboxes, nextPhones, nextJobs, nextEvents, nextExceptionRecords, nextSettings] = await Promise.all([
      api.summary(),
      api.accounts(),
      api.mailboxes(),
      phonePromise,
      api.jobs(),
      api.events(),
      exceptionRecordsPromise,
      api.settings(),
    ]);
    setSummary(nextSummary);
    setAccounts(nextAccounts);
    setMailboxes(nextMailboxes);
    if (nextPhones) {
      setPhones(nextPhones);
    }
    setJobs(nextJobs);
    setEvents(nextEvents);
    if (nextExceptionRecords) {
      setExceptionRecords(nextExceptionRecords);
    }
    setSettings((current) => (appSettingsEqual(current, nextSettings) ? current : nextSettings));
  }, []);

  const usageByEmail = useMemo(() => {
    const entries: Array<readonly [string, AccountUsageEstimate]> = [];
    const grouped = new Map<string, AccountUsageEstimate[]>();
    for (const account of usageEstimate?.accounts || []) {
      const email = account.email.toLowerCase();
      const bucket = grouped.get(email);
      if (bucket) {
        bucket.push(account);
      } else {
        grouped.set(email, [account]);
      }
    }
    for (const [email, accounts] of grouped) {
      if (accounts.length === 1) {
        entries.push([email, accounts[0]]);
      }
    }
    return new Map(entries);
  }, [usageEstimate]);

  const usageByAccountId = useMemo(() => {
    const entries = (usageEstimate?.accounts || [])
      .filter((account) => Boolean(account.sub2api_account_id))
      .map((account) => [account.sub2api_account_id || "", account] as const);
    return new Map(entries);
  }, [usageEstimate]);

  const accountCounts = useMemo(() => accountDisplayCounts(accounts), [accounts]);
  const problemUnusedQuota = useMemo(() => (usageEstimate ? usageProblemAccountUnusedQuota(usageEstimate.accounts) : null), [usageEstimate]);

  const loadUsageEstimate = useCallback(async (refresh = true) => {
    setUsageLoading(true);
    setUsageError("");
    try {
      const nextEstimate = await api.usageEstimate(refresh);
      setUsageEstimate(nextEstimate);
      setUsageEstimateRefreshed(refresh);
      return nextEstimate;
    } catch (error) {
      const message = error instanceof Error ? error.message : "额度估算读取失败";
      setUsageError(message);
      throw error;
    } finally {
      setUsageLoading(false);
    }
  }, []);

  const loadUsageLimitSamples = useCallback(async () => {
    setUsageLimitSamplesLoading(true);
    setUsageLimitSamplesError("");
    try {
      setUsageLimitSamples(await api.usageLimitSamples());
    } catch (error) {
      setUsageLimitSamplesError(error instanceof Error ? error.message : "额度样本读取失败");
    } finally {
      setUsageLimitSamplesLoading(false);
    }
  }, []);

  useEffect(() => {
    api
      .me()
      .then(async () => {
        setAuthState("in");
        try {
          await loadAll();
        } catch (error) {
          setNotice(error instanceof Error ? error.message : "数据读取失败");
        }
      })
      .catch(() => setAuthState("out"));
  }, [loadAll]);

  useEffect(() => {
    if (authState !== "in") return;
    const timer = window.setInterval(() => {
      loadAll({ includePhones: view === "phones" }).catch(() => undefined);
      if (view === "overview" || view === "accounts") {
        loadUsageEstimate(false).catch(() => undefined);
      }
    }, 12_000);
    return () => window.clearInterval(timer);
  }, [authState, loadAll, loadUsageEstimate, view]);

  useEffect(() => {
    if (authState !== "in" || usageLoading || usageError) return;
    if (view === "overview" || view === "accounts") {
      if (usageEstimate) return;
      loadUsageEstimate(false).catch(() => undefined);
      return;
    }
    if (view === "usage") {
      const needsEstimate = !(usageEstimate && usageEstimateRefreshed);
      if (needsEstimate) {
        loadUsageEstimate(true).catch(() => undefined);
      }
    }
    if (view === "usage-samples" && !usageLimitSamples && !usageLimitSamplesLoading) {
      loadUsageLimitSamples().catch(() => undefined);
    }
  }, [authState, loadUsageEstimate, loadUsageLimitSamples, usageError, usageEstimate, usageEstimateRefreshed, usageLimitSamples, usageLimitSamplesLoading, usageLoading, view]);

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(themeStorageKey, theme);
    } catch {
      // Ignore storage failures in restricted browser contexts.
    }
  }, [theme]);

  useEffect(() => {
    document.title = siteName;
  }, [siteName]);

  const runAction = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    setNotice("");
    try {
      const result = await action();
      setNotice(
        result && typeof result === "object" && "message" in result && typeof result.message === "string"
          ? result.message
          : success,
      );
      await loadAll();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const saveSettings = async (payload: AppSettingsUpdate) => {
    setBusy(true);
    setNotice("");
    try {
      const nextSettings = await api.updateSettings(payload);
      setSettings(nextSettings);
      setNotice("设置已保存");
      await loadAll().catch((error) => {
        setNotice(error instanceof Error ? `设置已保存；刷新页面数据失败：${error.message}` : "设置已保存；刷新页面数据失败");
      });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "设置保存失败");
    } finally {
      setBusy(false);
    }
  };

  const runSync = () =>
    runAction(async () => {
      const result = await api.sync();
      await loadUsageEstimate(false).catch(() => undefined);
      return result;
    }, "同步完成");

  const runUsageStatistics = () =>
    runAction(async () => {
      setUsageLoading(true);
      try {
        const result = await api.refreshUsageWindows();
        await loadUsageEstimate(false);
        setUsageEstimateRefreshed(true);
        return result;
      } finally {
        setUsageLoading(false);
      }
    }, "额度统计已刷新");

  if (authState === "checking") {
    return <BootScreen />;
  }

  if (authState === "out") {
    return (
      <LoginScreen
        siteName={siteName}
        theme={theme}
        onToggleTheme={toggleTheme}
        onLogin={async (adminKey) => {
          await api.login(adminKey);
          setAuthState("in");
          try {
            await loadAll();
          } catch (error) {
            setNotice(error instanceof Error ? error.message : "数据读取失败");
          }
        }}
      />
    );
  }

  const navItems: Array<{ id: View; label: string; icon: LucideIcon }> = [
    { id: "overview", label: "概览", icon: Activity },
    { id: "accounts", label: "账号", icon: UsersRound },
    { id: "usage", label: "额度", icon: TimerReset },
    { id: "usage-samples", label: "样本", icon: Radar },
    { id: "mailboxes", label: "邮箱", icon: Mail },
    { id: "phones", label: "手机号", icon: Smartphone },
    { id: "history", label: "历史", icon: Clock3 },
    { id: "settings", label: "设置", icon: Settings2 },
  ];

  return (
    <TimeZoneContext.Provider value={settings.display_timezone || defaultTimeZone}>
      <NowContext.Provider value={now}>
      <main className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <img alt="" src="/logo.png" />
          </div>
          <div>
            <strong>{siteName}</strong>
            <span>sub2api access token</span>
          </div>
        </div>

        <nav className="nav">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={view === item.id ? "nav-item active" : "nav-item"}
                key={item.id}
                onClick={() => setView(item.id)}
                type="button"
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <button
          className="ghost-button"
          type="button"
          onClick={async () => {
            setBusy(true);
            try {
              await api.logout();
            } finally {
              setBusy(false);
              setAuthState("out");
            }
          }}
        >
          <LogOut size={17} />
          <span>退出</span>
        </button>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">本机管理面板</p>
            <h1>{titleFor(view)}</h1>
          </div>
          <div className="topbar-actions">
            <ThemeToggle theme={theme} onToggleTheme={toggleTheme} />
            {notice ? <span className="notice">{notice}</span> : null}
            <ToolbarTimeButton
              busy={busy}
              icon={RefreshCcw}
              label="同步"
              onClick={runSync}
              time={syncActionTime}
            />
            <ToolbarTimeButton
              busy={busy || usageLoading}
              icon={Database}
              label={usageLoading ? "统计中" : "额度统计"}
              onClick={runUsageStatistics}
              time={usageActionTime}
            />
          </div>
        </header>

        {view === "overview" ? (
          <Overview
            summary={summary}
            accounts={accounts}
            accountCounts={accountCounts}
            jobs={jobs}
            events={events}
            problemUnusedQuota={problemUnusedQuota}
            usageByAccountId={usageByAccountId}
            usageByEmail={usageByEmail}
          />
        ) : null}
        {view === "accounts" ? (
          <AccountsView
            accounts={accounts}
            accountJumpTarget={accountJumpTarget}
            busy={busy}
            mailboxes={mailboxes}
            usageByAccountId={usageByAccountId}
            usageByEmail={usageByEmail}
            onDeleteDeactivated={() => runAction(api.deleteDeactivatedAccounts, "已删除封禁/重复账号")}
            onDeleteSelectedAccounts={(selectedAccounts) =>
              runAction(() => api.deleteSelectedAccounts(selectedAccounts.map(selectedAccountDeleteItem)), "已删除所选账号")
            }
            onAccountJumpHandled={() => setAccountJumpTarget(null)}
            onDeleteRemote={(account) =>
              runAction(
                async () => {
                  const result = await api.deleteRemoteAccount(account.sub2api_account_id || "");
                  setUsageEstimate(null);
                  setUsageEstimateRefreshed(false);
                  return result;
                },
                "已删除 sub2api 账号",
              )
            }
            onToggleDeleteUnlock={(account, unlocked) =>
              runAction(
                () => api.updateRemoteAccountDeleteLock(account.sub2api_account_id || "", unlocked),
                unlocked ? "已解锁删除保护" : "已恢复删除保护",
              )
            }
            onRefresh={(email) => runAction(() => api.refresh(email), "已创建检测/刷新任务")}
            onToggleUsageEstimate={(id, enabled) =>
              runAction(async () => {
                const result = await api.updateAccountUsageEstimate(id, enabled);
                await loadUsageEstimate(false);
                return result;
              }, enabled ? "已纳入额度估算" : "已排除额度估算")
            }
            onToggleRefreshLock={(account, unlocked) =>
              runAction(
                () => api.updateAccountRefreshLock(account.id, unlocked),
                unlocked ? "已解锁自动刷新" : "已恢复自动刷新锁定",
              )
            }
          />
        ) : null}
        {view === "usage" ? (
          <UsageEstimateView
            estimate={usageEstimate}
            error={usageError}
            loading={usageLoading}
          />
        ) : null}
        {view === "usage-samples" ? (
          <UsageLimitSamplesView
            data={usageLimitSamples}
            error={usageLimitSamplesError}
            loading={usageLimitSamplesLoading}
            onRefresh={loadUsageLimitSamples}
          />
        ) : null}
        {view === "mailboxes" ? (
          <MailboxView
            mailboxes={mailboxes}
            busy={busy}
            onImport={(content, provider) => runAction(() => api.importMailboxes(content, provider), "导入完成")}
            onDelete={(id) => runAction(() => api.deleteMailbox(id), "已删除")}
          />
        ) : null}
        {view === "phones" ? (
          <PhoneView
            accounts={accounts}
            busy={busy}
            phones={phones}
            onDelete={(id) => runAction(() => api.deletePhone(id), "已删除手机号")}
            onExport={async () => {
              const result = await api.exportPhones();
              downloadTextFile("phones.txt", result.message || "");
              setNotice("手机号已导出");
            }}
            onImport={(content) => runAction(() => api.importPhones(content), "导入完成")}
            onRefreshStatuses={() => runAction(api.refreshPhoneStatuses, "接码状态已刷新")}
            onUpdateBindings={(id, accountEmails) => runAction(() => api.updatePhoneBindings(id, accountEmails), "绑定已更新")}
          />
        ) : null}
        {view === "history" ? (
          <HistoryView
            busy={busy}
            exceptionRecords={exceptionRecords}
            jobs={jobs}
            events={events}
            onClear={() => runAction(api.clearHistory, "历史已清空")}
            onDeleteExceptionRecord={(id) => runAction(() => api.deleteExceptionRecord(id), "异常账号记录已删除")}
            onLocateAccount={(record) => {
              setAccountJumpTarget({
                email: record.email,
                sub2apiAccountId: record.sub2api_account_id,
                requestedAt: Date.now(),
              });
              setView("accounts");
            }}
          />
        ) : null}
        {view === "settings" ? (
          <SettingsView
            busy={busy}
            settings={settings}
            subscriptionTypes={[...new Set(accounts.map((account) => account.subscription_type).filter(Boolean))]}
            onScan={() => runAction(api.scanSub2Api, "扫描完成")}
            onSave={saveSettings}
          />
        ) : null}
      </section>
      </main>
      </NowContext.Provider>
    </TimeZoneContext.Provider>
  );
}

function LoginScreen({
  siteName,
  theme,
  onToggleTheme,
  onLogin,
}: {
  siteName: string;
  theme: Theme;
  onToggleTheme: () => void;
  onLogin: (adminKey: string) => Promise<void>;
}) {
  const [adminKey, setAdminKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onLogin(adminKey);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-panel">
        <div className="login-panel-head">
          <div className="login-emblem">
            <img alt="" src="/logo.png" />
          </div>
          <ThemeToggle compact theme={theme} onToggleTheme={onToggleTheme} />
        </div>
        <p className="eyebrow">{siteName}</p>
        <h1>控制台登录</h1>
        <form onSubmit={submit}>
          <label htmlFor="adminKey">管理密钥</label>
          <input
            autoFocus
            id="adminKey"
            name="adminKey"
            onChange={(event) => setAdminKey(event.target.value)}
            placeholder="APP_ADMIN_KEY"
            type="password"
            value={adminKey}
          />
          {error ? <p className="form-error">{error}</p> : null}
          <button className="primary-button wide" disabled={busy || !adminKey} type="submit">
            <ShieldCheck size={18} />
            <span>{busy ? "验证中" : "进入后台"}</span>
          </button>
        </form>
      </section>
      <section className="login-signal">
        <div className="signal-line" />
        <div className="signal-tile">
          <Sparkles size={18} />
          <span>Token Recovery Loop</span>
        </div>
      </section>
    </main>
  );
}

function BootScreen() {
  return (
    <main className="boot">
      <RefreshCcw className="spin" size={24} />
      <span>加载中</span>
    </main>
  );
}

function ThemeToggle({ theme, onToggleTheme, compact = false }: { theme: Theme; onToggleTheme: () => void; compact?: boolean }) {
  const isDark = theme === "dark";
  const Icon = isDark ? Sun : Moon;
  const label = isDark ? "浅色" : "暗色";
  const title = isDark ? "切换到浅色模式" : "切换到暗色模式";

  return (
    <button
      aria-label={title}
      className={compact ? "secondary-button theme-toggle compact" : "secondary-button theme-toggle"}
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

function Overview({
  summary,
  accounts,
  accountCounts,
  jobs,
  events,
  problemUnusedQuota,
  usageByAccountId,
  usageByEmail,
}: {
  summary: Summary;
  accounts: Account[];
  accountCounts: AccountCounts;
  jobs: RefreshJob[];
  events: AppEvent[];
  problemUnusedQuota: ProblemUnusedQuotaSummary | null;
  usageByAccountId: Map<string, AccountUsageEstimate>;
  usageByEmail: Map<string, AccountUsageEstimate>;
}) {
  const recentAccounts = accounts.slice(0, 6);
  const latestJob = jobs[0];
  const latestEvent = events[0];
  const actualAccounts = summary.actual_accounts ?? accountCounts.actual;
  const dedupedAccounts = summary.deduped_accounts ?? accountCounts.deduped;
  const duplicateAccounts = summary.duplicate_accounts ?? accountCounts.duplicates;
  const fiveHourRateLimitedAccounts = rateLimitedAccountsForWindow(accounts, "five_hour");
  const sevenDayRateLimitedAccounts = rateLimitedAccountsForWindow(accounts, "seven_day");
  const monthlyRateLimitedAccounts = rateLimitedAccountsForWindow(accounts, "monthly");
  const rateLimitedAccountCount = new Set(
    [...fiveHourRateLimitedAccounts, ...sevenDayRateLimitedAccounts, ...monthlyRateLimitedAccounts].map(accountRowKey),
  ).size;
  const availableAccountCount = accounts.filter(
    (account) => !account.deactive && !accountHasError(account) && !accountRateLimited(account),
  ).length;
  const problemUnusedQuotaTitle = problemUnusedQuota
    ? `错误/封停账号 ${problemUnusedQuota.accountCount} 个，可估 ${problemUnusedQuota.sevenDay.estimable_accounts} 个，5h 未用 ${formatAggregateMoney(problemUnusedQuota.fiveHour)}`
    : "等待额度估算数据";
  const stats: Array<{ label: string; value: number | string; icon: LucideIcon; tone: string; title?: string }> = [
    { label: "实际账号", value: actualAccounts, icon: UsersRound, tone: "ink" },
    { label: "去重账号", value: dedupedAccounts, icon: ShieldCheck, tone: "ok" },
    { label: "可用", value: availableAccountCount, icon: CheckCircle2, tone: "ok" },
    { label: "重复", value: duplicateAccounts, icon: Link2, tone: "warn" },
    { label: "限流", value: rateLimitedAccountCount, icon: ShieldAlert, tone: "warn" },
    { label: "错误", value: summary.error_accounts, icon: AlertTriangle, tone: "warn" },
    { label: "暂停", value: summary.paused_accounts ?? 0, icon: PauseCircle, tone: "ink" },
    { label: "恢复中", value: summary.refreshing_accounts, icon: RefreshCcw, tone: "teal" },
    { label: "封禁", value: summary.deactive_accounts, icon: UserRoundX, tone: "danger" },
    { label: "无法使用额度", value: formatProblemUnusedQuota(problemUnusedQuota), icon: TimerReset, tone: "danger", title: problemUnusedQuotaTitle },
    { label: "邮箱", value: summary.mailbox_count, icon: Mail, tone: "blue" },
    { label: "24h 成功", value: summary.recent_success, icon: CheckCircle2, tone: "ok" },
  ];

  return (
    <div className="stack">
      <section className="stat-grid">
        {stats.map((stat) => {
          const Icon = stat.icon;
          return (
            <article className={`stat-card ${stat.tone}`} key={stat.label} title={stat.title}>
              <Icon size={20} />
              <span>{stat.label}</span>
              <strong>{stat.value}</strong>
            </article>
          );
        })}
      </section>

      <section className="panel rate-limit-panel">
        <div className="panel-toolbar">
          <div>
            <PanelTitle title="限流账号" icon={ShieldAlert} />
            <p className="panel-subtitle">按 sub2api 当前窗口状态分开显示 5h、7d 与月限流账号。</p>
          </div>
          <Badge tone={rateLimitedAccountCount ? "warn" : "ok"}>
            {rateLimitedAccountCount ? `${rateLimitedAccountCount} 个账号` : "无限流"}
          </Badge>
        </div>
        <div className="rate-limit-window-grid">
          <RateLimitedAccountColumn
            title="5h"
            windowKey="five_hour"
            accounts={fiveHourRateLimitedAccounts}
            usageByAccountId={usageByAccountId}
            usageByEmail={usageByEmail}
          />
          <RateLimitedAccountColumn
            title="7d"
            windowKey="seven_day"
            accounts={sevenDayRateLimitedAccounts}
            usageByAccountId={usageByAccountId}
            usageByEmail={usageByEmail}
          />
          <RateLimitedAccountColumn
            title="月"
            windowKey="monthly"
            accounts={monthlyRateLimitedAccounts}
            usageByAccountId={usageByAccountId}
            usageByEmail={usageByEmail}
          />
        </div>
      </section>

      <section className="split-grid">
        <div className="panel">
          <PanelTitle title="最近账号" icon={UsersRound} />
          <div className="compact-list">
            {recentAccounts.length ? (
              recentAccounts.map((account) => <AccountRow account={account} key={account.email} compact />)
            ) : (
              <Empty label="暂无账号快照" />
            )}
          </div>
        </div>

        <div className="panel">
          <PanelTitle title="运行信号" icon={Activity} />
          <div className="signal-list">
            <SignalLine label="最近任务" value={latestJob ? `${latestJob.email} · ${statusLabel(latestJob.status)}` : "暂无"} />
            <SignalLine label="最近事件" value={latestEvent ? latestEvent.message : "暂无"} />
            <SignalLine label="24h 失败" value={`${summary.recent_failed}`} />
          </div>
        </div>
      </section>
    </div>
  );
}

function RateLimitedAccountColumn({
  title,
  windowKey,
  accounts,
  usageByAccountId,
  usageByEmail,
}: {
  title: string;
  windowKey: string;
  accounts: Account[];
  usageByAccountId: Map<string, AccountUsageEstimate>;
  usageByEmail: Map<string, AccountUsageEstimate>;
}) {
  const timeZone = useDisplayTimeZone();
  const now = useNow();
  return (
    <div className="rate-limit-window">
      <div className="rate-limit-window-head">
        <strong>{title}</strong>
        <span>{accounts.length} 个</span>
      </div>
      {accounts.length ? (
        <div className="rate-limit-account-list">
          {accounts.map((account) => {
            const usage = usageForAccount(account, usageByAccountId, usageByEmail);
            const detail = accountRateLimitDetails(account, usage, timeZone, now).find((item) => item.key === windowKey);
            return (
              <div className="rate-limit-account-row" key={accountRowKey(account)}>
                <div className="rate-limit-account-main">
                  <span className="mono">{account.email}</span>
                  {account.sub2api_account_id ? <span className="muted mono">{account.sub2api_account_id}</span> : null}
                </div>
                <div className="rate-limit-account-badges">
                  {account.is_duplicate ? <Badge tone="warn">重复</Badge> : null}
                  <Badge tone={accountStatusTone(account, usage)}>{accountStatusLabel(account, usage)}</Badge>
                  {detail ? (
                    <span className="rate-limit-recovery-tag aligned">
                      <RefreshCcw size={12} />
                      <span>{detail.recovery}</span>
                    </span>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <Empty label={`暂无 ${title} 限流账号`} />
      )}
    </div>
  );
}

function AccountsView({
  accounts,
  accountJumpTarget,
  busy,
  usageByAccountId,
  usageByEmail,
  mailboxes,
  onDeleteDeactivated,
  onDeleteSelectedAccounts,
  onAccountJumpHandled,
  onDeleteRemote,
  onToggleDeleteUnlock,
  onToggleRefreshLock,
  onRefresh,
  onToggleUsageEstimate,
}: {
  accounts: Account[];
  accountJumpTarget: AccountJumpTarget | null;
  busy: boolean;
  mailboxes: Mailbox[];
  usageByAccountId: Map<string, AccountUsageEstimate>;
  usageByEmail: Map<string, AccountUsageEstimate>;
  onDeleteDeactivated: () => void;
  onDeleteSelectedAccounts: (accounts: Account[]) => void;
  onAccountJumpHandled: () => void;
  onDeleteRemote: (account: Account) => void;
  onToggleDeleteUnlock: (account: Account, unlocked: boolean) => void;
  onToggleRefreshLock: (account: Account, unlocked: boolean) => void;
  onRefresh: (email: string) => void;
  onToggleUsageEstimate: (id: number, enabled: boolean) => void;
}) {
  const problemAccountCount = accounts.filter(canBulkDeleteProblemAccount).length;
  const orderedAccounts = useMemo(() => [...accounts].sort(accountCompare), [accounts]);
  const [accountSearch, setAccountSearch] = useState("");
  const [accountStatusFilter, setAccountStatusFilter] = useState<AccountStatusFilter>("all");
  const [accountSubscriptionFilter, setAccountSubscriptionFilter] = useState("");
  const [selectedAccountKeys, setSelectedAccountKeys] = useState<Record<string, boolean>>({});
  const [sessionDeleteUnlocks, setSessionDeleteUnlocks] = useState<Record<string, boolean>>({});
  const [selectedMailbox, setSelectedMailbox] = useState<Mailbox | null>(null);
  const [folder, setFolder] = useState<"inbox" | "junk">("inbox");
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [mailLoading, setMailLoading] = useState(false);
  const [mailError, setMailError] = useState("");
  const [selectedPhoneAccount, setSelectedPhoneAccount] = useState<Account | null>(null);
  const [selectedErrorAccount, setSelectedErrorAccount] = useState<Account | null>(null);
  const [highlightedAccountKey, setHighlightedAccountKey] = useState<string | null>(null);
  const handledJumpRequestRef = useRef<number | null>(null);
  const mailboxByEmail = useMemo(() => {
    const entries = mailboxes
      .filter((mailbox) => !mailbox.disabled)
      .map((mailbox) => [mailbox.gpt_email.toLowerCase(), mailbox] as const);
    return new Map(entries);
  }, [mailboxes]);
  const accountStatusFilterOptions = useMemo(
    () => availableAccountStatusFilterOptions(orderedAccounts, usageByAccountId, usageByEmail),
    [orderedAccounts, usageByAccountId, usageByEmail],
  );
  const accountSubscriptionFilterOptions = useMemo(() => availableAccountSubscriptionFilterOptions(orderedAccounts), [orderedAccounts]);
  const filteredAccounts = useMemo(
    () =>
      orderedAccounts.filter((account) => {
        const usage = usageForAccount(account, usageByAccountId, usageByEmail);
        return (
          accountMatchesStatusFilter(account, usage, accountStatusFilter) &&
          (!accountSubscriptionFilter || accountSubscriptionTypeLabel(account) === accountSubscriptionFilter) &&
          textMatchesSearch(
            [
              account.email,
              account.sub2api_account_id,
              account.sub2api_imported_at,
              account.last_seen_at,
              account.updated_at,
              account.platform,
              account.account_type,
              account.status,
              account.schedulable === null ? "未知 unknown" : account.schedulable ? "可用 schedulable" : "暂停 unschedulable",
              accountShowsRateLimit(account, usage) ? `限流 rate limited ${accountRateLimitedWindowsLabel(account, usage)}` : "",
              usage?.seven_day_token_history.total_tokens,
              usage?.seven_day_token_history.windows.map((window) => `${window.window_reset_key} ${window.tokens}`).join(" "),
              account.mailbox_bound ? "已绑定 mailbox bound" : "未绑定 mailbox unbound",
              account.deactive ? "封禁 deactive deactivated banned" : "",
              account.refreshing ? "刷新中 refreshing" : "",
              account.remote_error ? "错误 error" : "",
              account.is_duplicate ? "重复 duplicate" : "",
              account.subscription_starts_at,
              account.subscription_expires_at,
              account.subscription_renews_at,
              account.subscription_cancels_at,
              account.subscription_billing_period,
              account.subscription_plan,
              account.has_active_subscription === null ? "" : account.has_active_subscription ? "订阅有效 active subscription" : "订阅无效 inactive subscription",
              account.last_error,
              mailboxByEmail.get(account.email.toLowerCase())?.mailbox_email,
              mailboxByEmail.get(account.email.toLowerCase())?.provider,
              account.phone_number,
              account.phone_sms_url,
              account.phone_sms_cdk,
              account.phone_sms_recharge_url,
            ],
            accountSearch,
          )
        );
      }),
    [accountSearch, accountStatusFilter, accountSubscriptionFilter, mailboxByEmail, orderedAccounts, usageByAccountId, usageByEmail],
  );
  const selectedAccounts = accounts.filter((account) => selectedAccountKeys[accountRowKey(account)]);
  const selectedAccountCount = selectedAccounts.length;
  const currentNoEmailAccountCount = filteredAccounts.filter(
    (account) => !account.mailbox_bound && accountCanBeSelectedForDeletion(account),
  ).length;

  useEffect(() => {
    const validKeys = new Set(accounts.map(accountRowKey));
    setSelectedAccountKeys((current) => {
      let changed = false;
      const next: Record<string, boolean> = {};
      for (const [key, selected] of Object.entries(current)) {
        if (selected && validKeys.has(key)) {
          next[key] = true;
        } else if (selected) {
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [accounts]);

  useEffect(() => {
    if (!accountStatusFilterOptions.some((option) => option.value === accountStatusFilter)) {
      setAccountStatusFilter("all");
    }
  }, [accountStatusFilter, accountStatusFilterOptions]);

  useEffect(() => {
    if (accountSubscriptionFilter && !accountSubscriptionFilterOptions.some((option) => option.value === accountSubscriptionFilter)) {
      setAccountSubscriptionFilter("");
    }
  }, [accountSubscriptionFilter, accountSubscriptionFilterOptions]);

  useEffect(() => {
    if (!accountJumpTarget || handledJumpRequestRef.current === accountJumpTarget.requestedAt) return;
    if (!orderedAccounts.length) return;

    handledJumpRequestRef.current = accountJumpTarget.requestedAt;
    const targetAccount = findAccountJumpTarget(orderedAccounts, accountJumpTarget);
    const searchText = accountJumpSearchText(targetAccount, accountJumpTarget);

    setAccountStatusFilter("all");
    setAccountSubscriptionFilter("");

    if (!targetAccount) {
      setAccountSearch(searchText);
      setHighlightedAccountKey(null);
      onAccountJumpHandled();
      return;
    }

    const rowKey = accountRowKey(targetAccount);
    const rowId = accountRowDomId(targetAccount);
    setAccountSearch("");
    setHighlightedAccountKey(rowKey);

    window.setTimeout(() => {
      if (handledJumpRequestRef.current !== accountJumpTarget.requestedAt) return;
      document.getElementById(rowId)?.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
    }, 120);
    onAccountJumpHandled();
  }, [accountJumpTarget, onAccountJumpHandled, orderedAccounts]);

  useEffect(() => {
    if (!highlightedAccountKey) return;
    const clearTimer = window.setTimeout(() => {
      setHighlightedAccountKey((current) => (current === highlightedAccountKey ? null : current));
    }, 4_000);
    return () => window.clearTimeout(clearTimer);
  }, [highlightedAccountKey]);

  const supportsJunk = selectedMailbox ? ["outlook", "hotmail", "gmail"].includes(selectedMailbox.provider) : false;
  const openMessages = (mailbox: Mailbox) => {
    setSelectedMailbox(mailbox);
    setFolder("inbox");
    setMessages([]);
    setMailError("");
  };

  const deleteRemote = (account: Account) => {
    if (busy || !account.sub2api_account_id) return;
    const sessionDeleteUnlocked = Boolean(sessionDeleteUnlocks[accountRowKey(account)]);
    const needsSessionUnlock = account.delete_unlockable && !sessionDeleteUnlocked;
    if (needsSessionUnlock) {
      setSessionDeleteUnlocks((current) => ({ ...current, [accountRowKey(account)]: true }));
      return;
    }
    if (!account.can_delete_remote && !account.delete_unlockable) return;
    const kind = deleteAccountKindLabel(account);
    if (window.confirm(`确定删除 ${account.email} 的 ${kind}（sub2api ID: ${account.sub2api_account_id}）吗？`)) {
      setSessionDeleteUnlocks((current) => {
        const next = { ...current };
        delete next[accountRowKey(account)];
        return next;
      });
      onDeleteRemote(account);
    }
  };

  useEffect(() => {
    if (!selectedMailbox) return;
    const normalizedFolder = supportsJunk ? folder : "inbox";
    let alive = true;
    setMailLoading(true);
    setMailError("");
    api
      .mailboxMessages(selectedMailbox.id, normalizedFolder)
      .then((nextMessages) => {
        if (alive) setMessages(nextMessages);
      })
      .catch((error) => {
        if (alive) setMailError(error instanceof Error ? error.message : "读取邮件失败");
      })
      .finally(() => {
        if (alive) setMailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [folder, selectedMailbox, supportsJunk]);

  const deleteDeactivated = () => {
    if (!problemAccountCount || busy) return;
    if (window.confirm(`确定删除 ${problemAccountCount} 个封禁账号或重复账号，并在没有剩余远端账号时同步删除邮箱吗？`)) {
      onDeleteDeactivated();
    }
  };

  const toggleSelectedAccount = (account: Account, selected: boolean) => {
    if (!accountCanBeSelectedForDeletion(account)) return;
    const key = accountRowKey(account);
    setSelectedAccountKeys((current) => {
      if (selected) return { ...current, [key]: true };
      const next = { ...current };
      delete next[key];
      return next;
    });
  };

  const selectCurrentNoEmailAccounts = () => {
    setSelectedAccountKeys((current) => {
      const next = { ...current };
      for (const account of filteredAccounts) {
        if (!account.mailbox_bound && accountCanBeSelectedForDeletion(account)) {
          next[accountRowKey(account)] = true;
        }
      }
      return next;
    });
  };

  const clearSelectedAccounts = () => setSelectedAccountKeys({});

  const deleteSelectedAccounts = () => {
    if (busy || !selectedAccounts.length) return;
    const noEmailCount = selectedAccounts.filter((account) => !account.mailbox_bound).length;
    const suffix = noEmailCount ? `，其中 ${noEmailCount} 个无邮箱账号` : "";
    if (window.confirm(`确定删除 ${selectedAccounts.length} 个已选账号${suffix}吗？`)) {
      onDeleteSelectedAccounts(selectedAccounts);
      clearSelectedAccounts();
    }
  };

  const selectedPhoneAccountKey = selectedPhoneAccount ? accountRowKey(selectedPhoneAccount) : null;
  const currentSelectedPhoneAccount = selectedPhoneAccountKey
    ? accounts.find((account) => accountRowKey(account) === selectedPhoneAccountKey) || selectedPhoneAccount
    : null;

  return (
    <>
      <section className="panel">
        <div className="panel-toolbar accounts-panel-toolbar">
          <div className="accounts-toolbar-main">
            <PanelTitle title="账号状态" icon={UsersRound} />
            <div className="accounts-filter-row">
              <SearchBox
                count={filteredAccounts.length}
                onChange={setAccountSearch}
                placeholder="搜索账号 / 状态 / ID"
                total={accounts.length}
                value={accountSearch}
              />
              <AccountStatusFilterMenu
                onChange={setAccountStatusFilter}
                options={accountStatusFilterOptions}
                value={accountStatusFilter}
              />
              <AccountSubscriptionFilterMenu
                onChange={setAccountSubscriptionFilter}
                options={accountSubscriptionFilterOptions}
                value={accountSubscriptionFilter}
              />
            </div>
          </div>
          <div className="toolbar-actions">
            <button className="secondary-button" disabled={busy || !currentNoEmailAccountCount} onClick={selectCurrentNoEmailAccounts} type="button">
              勾选当前无邮箱{currentNoEmailAccountCount ? ` (${currentNoEmailAccountCount})` : ""}
            </button>
            {selectedAccountCount ? (
              <button className="secondary-button" disabled={busy} onClick={clearSelectedAccounts} type="button">
                清空选择 ({selectedAccountCount})
              </button>
            ) : null}
            <button
              className="danger-button"
              disabled={busy || !selectedAccountCount}
              onClick={deleteSelectedAccounts}
              type="button"
            >
              <Trash2 size={17} />
              <span>删除所选{selectedAccountCount ? ` (${selectedAccountCount})` : ""}</span>
            </button>
            <button className="danger-button" disabled={busy || !problemAccountCount} onClick={deleteDeactivated} type="button">
              <Trash2 size={17} />
              <span>删除封禁/重复账号</span>
            </button>
          </div>
        </div>
        <div className="table-wrap">
          <table className="accounts-table">
            <colgroup>
              <col className="accounts-col-select" />
              <col className="accounts-col-email" />
              <col className="accounts-col-id" />
              <col className="accounts-col-time-records" />
              <col className="accounts-col-mailbox" />
              <col className="accounts-col-participation" />
              <col className="accounts-col-status" />
              <col className="accounts-col-schedule" />
              <col className="accounts-col-quota" />
              <col className="accounts-col-quota" />
              <col className="accounts-col-history" />
              <col className="accounts-col-subscription" />
              <col className="accounts-col-error" />
              <col className="accounts-col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th className="accounts-select-header"><span className="sr-only">选择</span></th>
                <th>邮箱</th>
                <th>sub2api ID</th>
                <th>时间记录</th>
                <th>绑定邮箱</th>
                <th>参与额度</th>
                <th>状态</th>
                <th>调度</th>
                <th>5h 额度</th>
                <th>7d/月额度</th>
                <th>7d/月额度历史</th>
                <th>订阅</th>
                <th>错误</th>
                <th className="sticky-action-column">操作</th>
              </tr>
            </thead>
            <tbody>
              {filteredAccounts.map((account) => (
                <AccountRow
                  account={account}
                  busy={busy}
                  highlighted={highlightedAccountKey === accountRowKey(account)}
                  id={accountRowDomId(account)}
                  key={accountRowKey(account)}
                  mailbox={mailboxByEmail.get(account.email.toLowerCase())}
                  selected={Boolean(selectedAccountKeys[accountRowKey(account)])}
                  usage={usageForAccount(account, usageByAccountId, usageByEmail)}
                  onDeleteRemote={deleteRemote}
                  onToggleSelected={toggleSelectedAccount}
                  sessionDeleteUnlocked={Boolean(sessionDeleteUnlocks[accountRowKey(account)])}
                  onToggleRefreshLock={onToggleRefreshLock}
                  onOpenMailbox={openMessages}
                  onOpenError={setSelectedErrorAccount}
                  onOpenPhone={setSelectedPhoneAccount}
                  onRefresh={onRefresh}
                  onToggleUsageEstimate={onToggleUsageEstimate}
                />
              ))}
            </tbody>
          </table>
          {!accounts.length ? <Empty label="尚未同步账号" /> : null}
          {accounts.length > 0 && !filteredAccounts.length ? <Empty label="没有匹配账号" /> : null}
        </div>
      </section>
      {selectedMailbox ? (
        <MailMessageDialog
          folder={supportsJunk ? folder : "inbox"}
          loading={mailLoading}
          mailbox={selectedMailbox}
          messages={messages}
          error={mailError}
          supportsJunk={supportsJunk}
          onClose={() => setSelectedMailbox(null)}
          onFolderChange={setFolder}
        />
      ) : null}
      {currentSelectedPhoneAccount ? <AccountPhoneDialog account={currentSelectedPhoneAccount} onClose={() => setSelectedPhoneAccount(null)} /> : null}
      {selectedErrorAccount ? <AccountErrorDialog account={selectedErrorAccount} onClose={() => setSelectedErrorAccount(null)} /> : null}
    </>
  );
}

function AccountRow({
  account,
  busy,
  compact = false,
  highlighted = false,
  id,
  mailbox,
  selected = false,
  usage,
  onDeleteRemote,
  onToggleSelected,
  sessionDeleteUnlocked = false,
  onToggleRefreshLock,
  onOpenMailbox,
  onOpenError,
  onOpenPhone,
  onRefresh,
  onToggleUsageEstimate,
}: {
  account: Account;
  busy?: boolean;
  compact?: boolean;
  highlighted?: boolean;
  id?: string;
  mailbox?: Mailbox;
  selected?: boolean;
  usage?: AccountUsageEstimate;
  onDeleteRemote?: (account: Account) => void;
  onToggleSelected?: (account: Account, selected: boolean) => void;
  sessionDeleteUnlocked?: boolean;
  onToggleRefreshLock?: (account: Account, unlocked: boolean) => void;
  onOpenMailbox?: (mailbox: Mailbox) => void;
  onOpenError?: (account: Account) => void;
  onOpenPhone?: (account: Account) => void;
  onRefresh?: (email: string) => void;
  onToggleUsageEstimate?: (id: number, enabled: boolean) => void;
}) {
  const timeZone = useDisplayTimeZone();
  const now = useNow();
  const rateLimited = accountShowsRateLimit(account, usage);
  const manuallyPaused = accountIsManuallyPaused(account, usage);
  const statusTone = accountStatusTone(account, usage);
  const statusText = accountStatusLabel(account, usage);
  const rateLimitDetails = accountDisplayRateLimitDetails(account, usage, timeZone, now);
  const rateLimitStatusLabel = rateLimitDetails.length ? rateLimitDetails.map((detail) => detail.label).join("/") : "";
  const rateLimitStatusText = `${statusText} | ${rateLimitStatusLabel ? `${rateLimitStatusLabel}限流` : "限流"}`;
  const rateLimitStatusTone = rateLimitDetails[0]?.tone || "warn";
  const windowRefreshTags = accountWindowRefreshTags(usage, timeZone, now);
  const errorSummary = accountErrorSummary(account);
  const refreshLocked = accountHasError(account) && account.auto_refresh_locked;
  const ActionIcon = account.deactive ? Radar : account.mailbox_bound ? Play : Radar;
  const actionTitle = refreshLocked ? "刷新失败已锁定，点击解锁后才会再次自动刷新" : account.deactive ? "复检封禁账号" : account.mailbox_bound ? "刷新 AT" : "检测账号";
  const rowClass = [
    account.is_duplicate ? "duplicate-row" : "",
    rateLimited ? "rate-limited-row" : "",
    highlighted ? "account-jump-highlight" : "",
  ]
    .filter(Boolean)
    .join(" ");
  const canShowDelete = account.remote_error || account.is_duplicate;
  const canSelect = accountCanBeSelectedForDeletion(account);
  const deleteNeedsSessionUnlock = account.delete_unlockable && !sessionDeleteUnlocked;
  const deleteButtonLocked = deleteNeedsSessionUnlock;
  const deleteTitle = account.can_delete_remote
    ? isDeactivatedAccount(account)
      ? "删除此封禁账号"
      : account.delete_unlockable
        ? "删除此普通错误账号"
        : account.remote_error
          ? "删除此重复异常账号"
          : "删除此重复账号"
    : deleteNeedsSessionUnlock
      ? "此类错误账号需要在当前页面先点击一次解锁，再点击删除"
      : account.remote_error && !account.is_duplicate
        ? "普通错误账号可能重新授权恢复，不能直接删除"
        : account.is_duplicate
          ? "重复账号只会保留同邮箱主账号"
          : "当前账号不能单独删除";
  if (compact) {
    return (
      <div className="compact-row">
        <span className="mono">{account.email}</span>
        {account.is_duplicate ? <Badge tone="warn">重复</Badge> : null}
        <Badge tone={statusTone}>{statusText}</Badge>
      </div>
    );
  }
  return (
    <tr className={rowClass} id={id}>
      <td>
        <label className="table-check account-select-check" title={canSelect ? "选中后可批量删除" : "此账号缺少可删除标识"}>
          <input
            aria-label={`选择 ${account.email}`}
            checked={selected}
            disabled={busy || !canSelect}
            onChange={(event) => onToggleSelected?.(account, event.currentTarget.checked)}
            type="checkbox"
          />
        </label>
      </td>
      <td>
        <div className="email-cell">
          <CopyTextButton className="account-email-copy-button mono" hideIcon title="复制账号邮箱" value={account.email} />
          <div className="email-cell-meta">
            {account.is_duplicate ? (
              <span className="duplicate-marker">
                <span className="duplicate-dot" />
                <span>{account.duplicate_primary ? "主账号" : `重复 ${account.duplicate_rank + 1}/${account.duplicate_group_size}`}</span>
              </span>
            ) : null}
          </div>
        </div>
      </td>
      <td className="account-id-cell mono muted" title={account.sub2api_account_id || undefined}>{account.sub2api_account_id || "-"}</td>
      <td>
        <AccountTimeRecordsCell account={account} timeZone={timeZone} />
      </td>
      <td>
        <Badge tone={account.mailbox_bound ? "ok" : "ink"}>{account.mailbox_bound ? "已绑定" : "未绑定"}</Badge>
      </td>
      <td>
        {account.deactive || accountEstimateExcludedByError(account, usage) || rateLimited ? (
          <span className="table-check-label" title={accountUsageEstimateToggleTitle(account, usage)}>
            {accountUsageEstimateToggleLabel(account, usage)}
          </span>
        ) : (
          <label className="table-check" title={accountUsageEstimateToggleTitle(account, usage)}>
            <input
              checked={account.usage_estimate_enabled}
              disabled={busy || account.id <= 0}
              onChange={(event) => {
                if (account.id > 0) onToggleUsageEstimate?.(account.id, event.target.checked);
              }}
              type="checkbox"
            />
            <span>{accountUsageEstimateToggleLabel(account, usage)}</span>
          </label>
        )}
      </td>
      <td>
        <div className="status-stack">
          <div className="status-badge-row">
            <Badge className="status-column-badge" tone={rateLimited ? rateLimitStatusTone : statusTone}>{rateLimited ? rateLimitStatusText : statusText}</Badge>
            {!rateLimited && manuallyPaused ? <Badge className="status-column-badge" tone="ink">主动暂停</Badge> : null}
          </div>
          {windowRefreshTags.length ? (
            <div className="rate-limit-details">
              {windowRefreshTags.map((tag) => (
                <div className="rate-limit-item" key={tag.key}>
                  <span className="rate-limit-recovery-tag window-refresh-tag aligned" title={tag.title}>
                    <span className="window-refresh-tag-label">{tag.label}</span>
                    <span>{tag.time}</span>
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </td>
      <td>
        <Badge tone={accountScheduleTone(account, usage)}>{accountScheduleLabel(account, usage)}</Badge>
      </td>
      <td>
        <AccountQuotaCell hideMonthly window={usage?.five_hour} />
      </td>
      <td>
        <AccountQuotaCell window={usage?.seven_day} />
      </td>
      <td>
        <QuotaHistoryCell compact history={usage?.seven_day_token_history} />
      </td>
      <td>
        <AccountSubscriptionCell account={account} timeZone={timeZone} />
      </td>
      <td className="error-cell">
        {errorSummary ? (
          <button className="error-pill-button" onClick={() => onOpenError?.(account)} title="查看完整错误详情" type="button">
            <Badge className="error-pill-badge" tone={errorSummary.tone}>{errorSummary.label}</Badge>
          </button>
        ) : (
          "-"
        )}
      </td>
      <td className="right sticky-action-cell">
        <div className="row-actions">
          {mailbox ? (
            <button className="icon-button" disabled={busy} onClick={() => onOpenMailbox?.(mailbox)} title="查看邮件" type="button">
              <MailOpen size={16} />
            </button>
          ) : null}
          <button className="icon-button" disabled={busy} onClick={() => onOpenPhone?.(account)} title="查看手机号" type="button">
            <Smartphone size={16} />
          </button>
          <button
            className={refreshLocked ? "icon-button lock-state" : "icon-button"}
            disabled={busy || account.refreshing || !onRefresh}
            onClick={() => (refreshLocked ? onToggleRefreshLock?.(account, true) : onRefresh?.(account.email))}
            title={actionTitle}
            type="button"
          >
            <ActionIcon size={16} />
          </button>
          {canShowDelete ? (
            <div className="delete-action-stack">
              <button
                className={deleteButtonLocked ? "icon-button danger lock-state" : "icon-button danger"}
              disabled={busy || (!account.can_delete_remote && !account.delete_unlockable) || !account.sub2api_account_id || !onDeleteRemote}
              onClick={() => onDeleteRemote?.(account)}
              title={deleteTitle}
              type="button"
            >
              <Trash2 size={16} />
            </button>
          </div>
        ) : null}
        </div>
      </td>
    </tr>
  );
}

function AccountTimeRecordsCell({ account, timeZone }: { account: Account; timeZone: string }) {
  const records: Array<{ key: string; icon: LucideIcon; label: string; value: string | null | undefined }> = [
    { key: "imported", icon: Database, label: "导入 sub2api", value: account.sub2api_imported_at },
    { key: "called", icon: Activity, label: "最近调用", value: account.last_seen_at },
    { key: "updated", icon: Clock3, label: "最近更新", value: account.updated_at },
  ];

  return (
    <div className="account-time-records">
      {records.map((record) => {
        const Icon = record.icon;
        const shortTime = record.value ? formatDate(record.value, timeZone) : "-";
        const fullTime = record.value ? formatFullDate(record.value, timeZone) : "暂无记录";
        return (
          <time className={`account-time-tag ${record.key}`} dateTime={record.value || undefined} key={record.key} title={`${record.label}: ${fullTime}`}>
            <Icon size={12} />
            <span>{shortTime}</span>
          </time>
        );
      })}
    </div>
  );
}

function AccountQuotaCell({ window, loading, hideMonthly = false }: { window?: UsageWindowEstimate; loading?: boolean; hideMonthly?: boolean }) {
  if (loading) {
    return <span className="muted">查询中</span>;
  }
  if (!window) {
    return <span className="muted">-</span>;
  }
  if (window.window_kind === "none" || window.source === "not_applicable" || (hideMonthly && window.window_kind === "monthly")) {
    return (
      <div className="quota-cell quota-disabled-cell">
        <strong>{hideMonthly && window.window_kind === "monthly" ? "无 5h" : window.window_label || "无 5h"}</strong>
        <span>无独立 5h 限额</span>
      </div>
    );
  }
  const used = windowUsedAmount(window);
  const usedLabel = windowUsedLabel(window);
  const usedPercent = quotaUsedPercent(window);
  const scopeLabel = window.window_kind === "monthly" ? "月" : "";
  const progressLabel = `${scopeLabel}${usedLabel} ${formatPercent(usedPercent)}`;
  const title = window.rate_limited ? `限流 · ${progressLabel}` : progressLabel;
  const totalLabel = "总额";
  const remainingLabel = "未用";
  return (
    <div className={window.rate_limited ? "quota-cell quota-progress-cell limited" : "quota-cell quota-progress-cell"}>
      <div className="quota-progress" title={title}>
        <div style={{ width: `${usedPercent ?? 0}%` }} />
        <span>{window.rate_limited ? `${progressLabel} · 限流` : progressLabel}</span>
      </div>
      <div className="quota-labels">
        <span>{totalLabel} {formatMoney(window.estimated_limit)}</span>
        <span>{usedLabel} {formatMoney(used)}</span>
        <span>{remainingLabel} {window.remaining === null ? formatWindowRemaining(window) : formatMoney(window.remaining)}</span>
        <span>官方 {formatPercent(window.used_percent)}</span>
      </div>
    </div>
  );
}

function QuotaHistoryCell({
  history,
  loading,
  compact = false,
}: {
  history?: UsageTokenHistory;
  loading?: boolean;
  compact?: boolean;
}) {
  const timeZone = useDisplayTimeZone();
  if (loading) {
    return <span className="muted">查询中</span>;
  }
  if (!history) {
    return <span className="muted">-</span>;
  }

  const visibleWindows = compact ? history.windows.slice(0, 3) : history.windows;
  const hiddenCount = Math.max(history.windows.length - visibleWindows.length, 0);
  const title = history.windows
    .map((window) => `${formatTokenWindowLabel(window, timeZone)}: ${formatTokenWindowSummary(window)}`)
    .join("\n");

  return (
    <div className="token-history-cell" title={title || undefined}>
      <div className="history-metrics">
        <span className="history-metric-tag strong">{formatTokenHistoryTotalLabel(history)}</span>
        <span className="history-metric-tag">{formatTokenHistoryCountLabel(history)}</span>
      </div>
      {visibleWindows.length ? (
        <div className="token-window-list">
          {visibleWindows.map((window) => (
            <span key={window.window_reset_key}>
              <span>{formatTokenWindowLabel(window, timeZone)}</span>
              <strong>{formatTokenWindowSummary(window)}</strong>
            </span>
          ))}
          {hiddenCount ? <span>另 {hiddenCount} 个窗口</span> : null}
        </div>
      ) : (
        <span className="muted">暂无额度记录</span>
      )}
    </div>
  );
}

function HistoricalQuotaCell({ history }: { history: UsageTokenHistory }) {
  const timeZone = useDisplayTimeZone();
  const title = history.windows
    .map((window) => `${formatTokenWindowLabel(window, timeZone)}: ${formatTokenWindowSummary(window)}`)
    .join("\n");

  return (
    <div className="history-quota-cell" title={title || undefined}>
      <strong>{formatTokenHistoryTotalLabel(history)}</strong>
      <span>{formatTokenHistoryCountLabel(history)}</span>
    </div>
  );
}

function AccountSubscriptionCell({ account, timeZone }: { account: Account; timeZone: string }) {
  const period = account.subscription_billing_period ? periodLabel(account.subscription_billing_period) : null;
  const activeTone = account.has_active_subscription === false ? "warn" : account.has_active_subscription === true ? "ok" : "ink";
  const planLabelText = accountSubscriptionTypeLabel(account);
  if (!account.subscription_expires_at && !account.subscription_starts_at && !account.subscription_renews_at) {
    return (
      <div className="subscription-cell">
        <Badge tone={activeTone}>{planLabelText}</Badge>
        <span className="muted">暂无订阅时间</span>
      </div>
    );
  }
  return (
    <div className="subscription-cell">
      <div className="subscription-head">
        <Badge tone={activeTone}>{planLabelText}</Badge>
        {period ? <span>{period}</span> : null}
      </div>
      <span>开通 {account.subscription_starts_at ? formatDate(account.subscription_starts_at, timeZone) : "-"}</span>
      <span>到期 {account.subscription_expires_at ? formatDate(account.subscription_expires_at, timeZone) : "-"}</span>
      {account.subscription_renews_at ? <span>续费 {formatDate(account.subscription_renews_at, timeZone)}</span> : null}
      {account.subscription_cancels_at ? <span>取消 {formatDate(account.subscription_cancels_at, timeZone)}</span> : null}
    </div>
  );
}

function UsageEstimateView({
  estimate,
  loading,
  error,
}: {
  estimate: UsageEstimate | null;
  loading: boolean;
  error: string;
}) {
  const timeZone = useDisplayTimeZone();
  const now = useNow();
  const [includePausedAccounts, setIncludePausedAccounts] = useState(true);
  const [detailAccountFilter, setDetailAccountFilter] = useState<UsageDetailAccountFilter>("normal");
  const [subscriptionFilter, setSubscriptionFilter] = useState("");
  const displayedEstimate = useMemo(
    () => (estimate ? buildDisplayedUsageEstimate(estimate, includePausedAccounts) : null),
    [estimate, includePausedAccounts],
  );
  const headerStats = useMemo(
    () => (displayedEstimate ? usageEstimateHeaderStats(displayedEstimate, includePausedAccounts) : null),
    [displayedEstimate, includePausedAccounts],
  );
  const detailAccountCounts = useMemo(
    () => (displayedEstimate ? usageDetailAccountCounts(displayedEstimate.accounts) : { normal: 0, rateLimited: 0 }),
    [displayedEstimate],
  );
  const detailBaseAccounts = useMemo(
    () => displayedEstimate?.accounts.filter((account) => accountMatchesUsageDetailFilter(account, detailAccountFilter)) || [],
    [detailAccountFilter, displayedEstimate],
  );
  const subscriptionFilterOptions = useMemo(() => usageSubscriptionFilterOptions(detailBaseAccounts), [detailBaseAccounts]);
  const detailAccounts = useMemo(
    () => detailBaseAccounts.filter((account) => !subscriptionFilter || usageSubscriptionLabel(account) === subscriptionFilter),
    [detailBaseAccounts, subscriptionFilter],
  );

  useEffect(() => {
    if (subscriptionFilter && !subscriptionFilterOptions.some((option) => option.label === subscriptionFilter)) {
      setSubscriptionFilter("");
    }
  }, [subscriptionFilter, subscriptionFilterOptions]);

  return (
    <div className="stack">
      <section className="panel usage-estimate-panel">
        <div className="panel-toolbar">
          <div>
            <PanelTitle title="额度估算" icon={TimerReset} />
            {displayedEstimate ? (
              <div className="usage-estimate-meta" role="status">
                <Badge tone="ink">{`账号数 ${headerStats?.accountCount ?? 0}`}</Badge>
                <Badge tone="ok">{`可用 ${headerStats?.availableCount ?? 0}`}</Badge>
                <Badge tone="warn">{`限流 ${headerStats?.rateLimitedCount ?? 0}`}</Badge>
                <Badge tone="info">{`更新 ${formatDate(displayedEstimate.updated_at, timeZone)}`}</Badge>
              </div>
            ) : (
              <p className="panel-subtitle">等待用量数据</p>
            )}
          </div>
          <div className="toolbar-actions">
            <label className="checkbox-line usage-estimate-toggle">
              <input checked={includePausedAccounts} onChange={(event) => setIncludePausedAccounts(event.target.checked)} type="checkbox" />
              <span>统计主动暂停账号</span>
            </label>
          </div>
        </div>

        {error ? <div className="mail-error">{error}</div> : null}

        <div className="usage-summary-grid">
          <UsageSummaryCard aggregate={displayedEstimate?.overall.five_hour} title="综合 5h" />
          <UsageSummaryCard aggregate={displayedEstimate?.overall.seven_day} title="综合 7d/月" />
        </div>
      </section>

      {!estimate && loading ? <Empty label="正在读取 sub2api 用量" /> : null}

      {displayedEstimate ? (
        <>
          <section className="panel">
            <PanelTitle title="分组剩余额度" icon={UsersRound} />
            <div className="table-wrap">
              <table className="usage-group-table">
                <thead>
                  <tr>
                    <th>分组</th>
                    <th>账号</th>
                    <th>5h 剩余</th>
                    <th>5h 占比</th>
                    <th>7d/月 剩余</th>
                    <th>7d/月 占比</th>
                  </tr>
                </thead>
                <tbody>
                  {displayedEstimate.groups.map((group) => (
                    <tr key={group.group_id}>
                      <td>{group.group_name}</td>
                      <td>
                        {group.five_hour.enabled_account_count}/{group.account_count}
                      </td>
                      <td>{formatAggregateMoney(group.five_hour)}</td>
                      <td>{formatPercent(group.five_hour.remaining_percent)}</td>
                      <td>{formatAggregateMoney(group.seven_day)}</td>
                      <td>{formatPercent(group.seven_day.remaining_percent)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!displayedEstimate.groups.length ? <Empty label="暂无分组数据" /> : null}
            </div>
          </section>

          <section className="panel">
            <div className="panel-toolbar">
              <div>
                <PanelTitle title="账号额度明细" icon={Activity} />
                <p className="panel-subtitle">错误和封禁账号不参与估算，已从明细中隐藏。</p>
              </div>
              <div className="usage-detail-toolbar-actions">
                <div className="usage-detail-tabs" role="tablist" aria-label="额度明细账号类型">
                  <button
                    aria-selected={detailAccountFilter === "normal"}
                    className={detailAccountFilter === "normal" ? "usage-detail-tab active" : "usage-detail-tab"}
                    onClick={() => setDetailAccountFilter("normal")}
                    role="tab"
                    type="button"
                  >
                    <span>正常账号</span>
                    <strong>{detailAccountCounts.normal}</strong>
                  </button>
                  <button
                    aria-selected={detailAccountFilter === "rate-limited"}
                    className={detailAccountFilter === "rate-limited" ? "usage-detail-tab active" : "usage-detail-tab"}
                    onClick={() => setDetailAccountFilter("rate-limited")}
                    role="tab"
                    type="button"
                  >
                    <span>限流账号</span>
                    <strong>{detailAccountCounts.rateLimited}</strong>
                  </button>
                </div>
                {detailBaseAccounts.length ? (
                  <div className="usage-subscription-filters" role="toolbar" aria-label="按订阅类型筛选账号">
                    <button
                      aria-pressed={!subscriptionFilter}
                      className={!subscriptionFilter ? "usage-subscription-filter active" : "usage-subscription-filter"}
                      onClick={() => setSubscriptionFilter("")}
                      type="button"
                    >
                      <span>全部订阅</span>
                      <strong>{detailBaseAccounts.length}</strong>
                    </button>
                    {subscriptionFilterOptions.map((option) => (
                      <button
                        aria-pressed={subscriptionFilter === option.label}
                        className={subscriptionFilter === option.label ? "usage-subscription-filter active" : "usage-subscription-filter"}
                        key={option.label}
                        onClick={() => setSubscriptionFilter(option.label)}
                        type="button"
                      >
                        <span>{option.label}</span>
                        <strong>{option.count}</strong>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
            <div className="table-wrap">
              <table className="usage-account-table">
                <colgroup>
                  <col className="usage-account-col-email" />
                  <col className="usage-account-col-tags" />
                  <col className="usage-account-col-subscription" />
                  <col className="usage-account-col-participation" />
                  <col className="usage-account-col-quota" />
                  <col className="usage-account-col-quota" />
                  <col className="usage-account-col-reset" />
                  <col className="usage-account-col-history" />
                </colgroup>
                <thead>
                  <tr>
                    <th>邮箱</th>
                    <th>标签</th>
                    <th>订阅类型</th>
                    <th>参与</th>
                    <th>5h 额度</th>
                    <th>7d/月 额度</th>
                    <th>重置</th>
                    <th>历史额度累计</th>
                  </tr>
                </thead>
                <tbody>
                  {detailAccounts.map((account, index) => (
                    <tr key={`${account.email}:${account.sub2api_account_id || index}`}>
                      <td>
                        <CopyTextButton className="account-email-copy-button mono" hideIcon title="复制账号邮箱" value={account.email} />
                      </td>
                      <td>
                        <UsageAccountTags groups={account.groups} />
                      </td>
                      <td>
                        <UsageSubscriptionCell account={account} />
                      </td>
                      <td>
                        <Badge tone={usageEstimateParticipationTone(account, includePausedAccounts)}>
                          {usageEstimateParticipationLabel(account, includePausedAccounts)}
                        </Badge>
                      </td>
                      <td>
                        <AccountQuotaCell hideMonthly window={account.five_hour} />
                      </td>
                      <td>
                        <AccountQuotaCell window={account.seven_day} />
                      </td>
                      <td>
                        <UsageWindowResetCell fiveHour={account.five_hour} now={now} sevenDay={account.seven_day} />
                      </td>
                      <td>
                        <HistoricalQuotaCell history={account.seven_day_token_history} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!detailAccounts.length ? <Empty label={subscriptionFilter ? `暂无 ${subscriptionFilter} 账号额度` : detailAccountFilter === "normal" ? "暂无正常账号额度" : "暂无限流账号额度"} /> : null}
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

function UsageAccountTags({ groups }: { groups: UsageGroupRef[] }) {
  const visibleGroups = groups.length ? groups : [{ id: "ungrouped", name: "未分组" }];
  return (
    <div className="usage-account-tags">
      {visibleGroups.map((group) => (
        <span className="usage-account-tag" key={`${group.id}:${group.name}`} title={group.name}>
          {group.name}
        </span>
      ))}
    </div>
  );
}

function UsageSubscriptionCell({ account }: { account: AccountUsageEstimate }) {
  const period = account.subscription_billing_period ? periodLabel(account.subscription_billing_period) : null;
  const tone = account.has_active_subscription === false ? "warn" : account.has_active_subscription === true ? "ok" : "ink";
  const label = usageSubscriptionLabel(account);
  return (
    <div className="usage-subscription-cell">
      <Badge tone={tone}>{label}</Badge>
      {period ? <span>{period}</span> : null}
    </div>
  );
}

function UsageWindowResetCell({
  fiveHour,
  sevenDay,
  now,
}: {
  fiveHour: UsageWindowEstimate;
  sevenDay: UsageWindowEstimate;
  now: number;
}) {
  const timeZone = useDisplayTimeZone();
  const rows = [
    { key: "five-hour", label: "5h", window: fiveHour },
    { key: "seven-day", label: "7d", window: sevenDay },
  ];

  return (
    <div className="usage-reset-cell">
      {rows.map((row) => (
        <time
          className={`account-time-tag usage-reset-tag ${row.key}`}
          dateTime={row.window.reset_at || undefined}
          key={row.key}
          title={formatUsageWindowResetTitle(row.label, row.window, timeZone, now)}
        >
          <Clock3 size={12} />
          <span>{formatWindowResetLine(row.label, row.window, now)}</span>
        </time>
      ))}
    </div>
  );
}

function usageSubscriptionLabel(
  account: Pick<
    AccountUsageEstimate,
    "account_type" | "has_active_subscription" | "platform" | "subscription_label" | "subscription_plan" | "subscription_type"
  >,
) {
  const plan = account.subscription_type
    ? subscriptionTypeLabel(account.subscription_type)
    : account.subscription_label || planLabel(account.subscription_plan || account.account_type || account.platform || "未知");
  return account.has_active_subscription === false ? "订阅无效" : plan === "active" ? "正常" : plan;
}

function usageSubscriptionFilterOptions(accounts: AccountUsageEstimate[]) {
  const counts = new Map<string, number>();
  for (const account of accounts) {
    const label = usageSubscriptionLabel(account);
    counts.set(label, (counts.get(label) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([label, count]) => ({ label, count }))
    .sort((left, right) => usageSubscriptionSortRank(left.label) - usageSubscriptionSortRank(right.label) || left.label.localeCompare(right.label));
}

function usageSubscriptionSortRank(label: string) {
  const normalized = label.trim().toLowerCase();
  return (
    {
      plus: 10,
      team: 20,
      pro: 30,
      free: 40,
      k12: 50,
      正常: 60,
      订阅无效: 70,
      未知: 80,
    }[normalized] || 100
  );
}

function UsageLimitSamplesView({
  data,
  loading,
  error,
  onRefresh,
}: {
  data: UsageLimitSamples | null;
  loading: boolean;
  error: string;
  onRefresh: () => Promise<unknown>;
}) {
  const timeZone = useDisplayTimeZone();
  const sampleWindows = data?.windows || [];
  const [selectedWindowKey, setSelectedWindowKey] = useState<string>("");
  useEffect(() => {
    if (!sampleWindows.length) {
      if (selectedWindowKey) {
        setSelectedWindowKey("");
      }
      return;
    }
    if (!sampleWindows.some((window) => `${window.window_key}:${window.plan_cohort}` === selectedWindowKey)) {
      setSelectedWindowKey(`${sampleWindows[0].window_key}:${sampleWindows[0].plan_cohort}`);
    }
  }, [sampleWindows, selectedWindowKey]);
  const selectedWindow = useMemo(
    () => sampleWindows.find((window) => `${window.window_key}:${window.plan_cohort}` === selectedWindowKey) || sampleWindows[0] || null,
    [sampleWindows, selectedWindowKey],
  );

  return (
    <div className="stack">
      <section className="panel usage-samples-hero">
        <div className="panel-toolbar">
          <div>
            <PanelTitle title="额度样本" icon={Radar} />
            <p className="panel-subtitle">
              展示本地保存、用于推断官方窗口额度的限流样本；每个窗口最多保留中间 {data?.target_sample_count ?? 100} 条。
            </p>
          </div>
          <button className="secondary-button" disabled={loading} onClick={() => onRefresh().catch(() => undefined)} type="button">
            <RefreshCcw className={loading ? "spin" : ""} size={17} />
            <span>{loading ? "读取中" : "刷新样本"}</span>
          </button>
        </div>
        {error ? <div className="mail-error">{error}</div> : null}
        <div className="usage-samples-note">
          <span>
            触发阈值：5h ≥ {formatPercent(data?.five_hour_threshold_percent ?? data?.full_percent_threshold ?? null)} · 7d/月 ≥{" "}
            {formatPercent(data?.seven_day_threshold_percent ?? data?.full_percent_threshold ?? null)}
          </span>
          <span>plus / team / 月 team 样本分别统计</span>
          <span>样本数量 &lt; 10 条时使用默认区间</span>
          <span>样本数量 ≥ 10 条后使用 mean ± 3 sigma</span>
        </div>
      </section>

      {!data && loading ? <Empty label="正在读取额度样本" /> : null}
      {data ? (
        <>
          <div className="usage-sample-tabs" role="tablist" aria-label="额度样本视图切换">
            {sampleWindows.map((window) => {
              const windowId = `${window.window_key}:${window.plan_cohort}`;
              return (
                <button
                  key={windowId}
                  className={selectedWindowKey === windowId ? "usage-sample-tab active" : "usage-sample-tab"}
                  onClick={() => setSelectedWindowKey(windowId)}
                  role="tab"
                  aria-selected={selectedWindowKey === windowId}
                  type="button"
                >
                  <span>{`${window.label} · ${window.plan_label}`}</span>
                  <strong>{window.samples.length}</strong>
                </button>
              );
            })}
          </div>
          {selectedWindow ? (
            <section className="panel usage-sample-window" key={`${selectedWindow.window_key}:${selectedWindow.plan_cohort}`}>
              <div className="usage-sample-window-head">
                <div>
                  <PanelTitle title={`${selectedWindow.label} 样本 · ${selectedWindow.plan_label}`} icon={TimerReset} />
                  <p className="panel-subtitle">
                    {selectedWindow.calibration.source === "sigma" ? "当前使用统计区间" : "当前使用默认区间"} · {selectedWindow.samples.length}/
                    {data.target_sample_count} 条 · 更新 {formatDate(data.updated_at, timeZone)}
                  </p>
                </div>
                <div className="usage-sample-calibration">
                  <strong>
                    {formatMoney(selectedWindow.calibration.lower)} - {formatMoney(selectedWindow.calibration.upper)}
                  </strong>
                  <span>
                    均值 {formatMoney(selectedWindow.calibration.mean)} · sigma {formatMoney(selectedWindow.calibration.sigma)}
                  </span>
                </div>
              </div>
              <div className="table-wrap">
                <table className="usage-sample-table">
                  <thead>
                    <tr>
                      <th>#</th>
                      <th>套餐</th>
                      <th>邮箱</th>
                      <th>窗口总额</th>
                      <th>限流已用</th>
                      <th>官方百分比</th>
                      <th>重置</th>
                      <th>记录时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedWindow.samples.map((sample, index) => (
                      <tr key={sample.id}>
                        <td className="mono muted">{index + 1}</td>
                        <td>{sample.plan_cohort}</td>
                        <td>
                          <div className="usage-sample-account">
                            <span className="mono">{sample.email || "-"}</span>
                            <span>{sample.sub2api_account_id || sample.account_key}</span>
                          </div>
                        </td>
                        <td>{formatMoney(sample.observed_limit)}</td>
                        <td>{formatMoney(sample.raw_spent)}</td>
                        <td>{formatPercent(sample.used_percent)}</td>
                        <td>{sample.reset_at ? formatDate(sample.reset_at, timeZone) : "-"}</td>
                        <td>{formatDate(sample.updated_at || sample.created_at, timeZone)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!selectedWindow.samples.length ? <Empty label="暂无限流样本" /> : null}
              </div>
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function UsageSummaryCard({ title, aggregate }: { title: string; aggregate?: UsageWindowAggregate }) {
  const meterPercent = clampPercentValue(aggregate?.remaining_percent ?? null) ?? 0;
  const meterLabel = formatPercent(aggregate?.remaining_percent ?? null);
  const meterTone = quotaMeterTone(aggregate?.remaining_percent ?? null);
  return (
    <article className="usage-summary-card">
      <span>{title}</span>
      <strong>{aggregate ? formatAggregateMoney(aggregate) : "-"}</strong>
      <div className={`quota-meter ${meterTone}`} aria-label={`${title} 剩余占比 ${meterLabel}`} role="img">
        <div style={{ width: `${meterPercent}%` }} />
        <span>{`剩余 ${meterLabel}`}</span>
      </div>
      <div className="usage-summary-stats">
        <span>总额 {formatMoney(aggregate?.estimated_limit)}</span>
        <span>已用 {formatMoney(aggregate?.spent)}</span>
      </div>
    </article>
  );
}

function MailboxView({
  mailboxes,
  busy,
  onImport,
  onDelete,
}: {
  mailboxes: Mailbox[];
  busy: boolean;
  onImport: (content: string, provider: string) => void;
  onDelete: (id: number) => void;
}) {
  const timeZone = useDisplayTimeZone();
  const [content, setContent] = useState("");
  const [provider, setProvider] = useState("auto");
  const [selectedMailbox, setSelectedMailbox] = useState<Mailbox | null>(null);
  const [folder, setFolder] = useState<"inbox" | "junk">("inbox");
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [mailLoading, setMailLoading] = useState(false);
  const [mailError, setMailError] = useState("");
  const [mailboxSearch, setMailboxSearch] = useState("");
  const filteredMailboxes = useMemo(
    () =>
      mailboxes.filter((mailbox) =>
        textMatchesSearch(
          [
            mailbox.gpt_email,
            mailbox.mailbox_email,
            mailbox.provider,
            mailbox.disabled ? "停用 disabled" : "启用 enabled",
            mailbox.last_error,
            mailbox.last_success_at,
          ],
          mailboxSearch,
        ),
      ),
    [mailboxSearch, mailboxes],
  );

  const supportsJunk = selectedMailbox ? ["outlook", "hotmail", "gmail"].includes(selectedMailbox.provider) : false;
  const openMessages = (mailbox: Mailbox) => {
    setSelectedMailbox(mailbox);
    setFolder("inbox");
    setMessages([]);
    setMailError("");
  };

  useEffect(() => {
    if (!selectedMailbox) return;
    const normalizedFolder = supportsJunk ? folder : "inbox";
    let alive = true;
    setMailLoading(true);
    setMailError("");
    api
      .mailboxMessages(selectedMailbox.id, normalizedFolder)
      .then((nextMessages) => {
        if (alive) setMessages(nextMessages);
      })
      .catch((error) => {
        if (alive) setMailError(error instanceof Error ? error.message : "读取邮件失败");
      })
      .finally(() => {
        if (alive) setMailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [folder, selectedMailbox, supportsJunk]);

  return (
    <div className="stack">
      <section className="panel">
        <PanelTitle title="导入邮箱" icon={Mail} />
        <form
          className="import-form"
          onSubmit={(event) => {
            event.preventDefault();
            onImport(content, provider);
          }}
        >
          <div className="import-controls">
            <label>
              识别方式
              <select onChange={(event) => setProvider(event.target.value)} value={provider}>
                <option value="auto">按后缀自动</option>
                <option value="outlook">Outlook</option>
                <option value="hotmail">Hotmail</option>
                <option value="gmail">Gmail / Google Workspace</option>
                <option value="custom">Custom HTTP</option>
                <option value="manual">Manual</option>
              </select>
            </label>
            <button className="primary-button" disabled={busy || !content.trim()} type="submit">
              <Mail size={17} />
              <span>导入</span>
            </button>
          </div>
          <textarea
            onChange={(event) => setContent(event.target.value)}
            placeholder={"gpt@example.com----mail@hotmail.com----mail_password----client_id----refresh_token\ncustom@edu.rainynight.me----yourname@gmail.com----gmail_app_password\nother@example.com----other@outlook.com----mail_password----client_id----refresh_token"}
            rows={6}
            value={content}
          />
          <p className="form-hint">支持批量导入，一行一个；空行和 # 开头的行会被忽略。Gmail 转发行可用 3 列：GPT 邮箱----转发到的 Gmail ---- Gmail App Password。</p>
        </form>
      </section>

      <section className="panel">
        <div className="panel-toolbar">
          <PanelTitle title="邮箱凭据" icon={ShieldCheck} />
          <div className="toolbar-actions">
            <SearchBox
              count={filteredMailboxes.length}
              onChange={setMailboxSearch}
              placeholder="搜索 GPT / 取件邮箱"
              total={mailboxes.length}
              value={mailboxSearch}
            />
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>GPT 邮箱</th>
                <th>取件邮箱</th>
                <th>类型</th>
                <th>最近成功</th>
                <th>错误</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filteredMailboxes.map((mailbox) => (
                <tr key={mailbox.id}>
                  <td className="mono">{mailbox.gpt_email}</td>
                  <td className="mono">{mailbox.mailbox_email}</td>
                  <td>
                    <Badge tone={mailbox.disabled ? "deactive" : "ok"}>{mailbox.provider}</Badge>
                  </td>
                  <td>{mailbox.last_success_at ? formatDate(mailbox.last_success_at, timeZone) : "-"}</td>
                  <td className="truncate">{mailbox.last_error || "-"}</td>
                  <td className="right">
                    <div className="row-actions">
                      <button className="icon-button" disabled={busy} onClick={() => openMessages(mailbox)} title="查看邮件" type="button">
                        <MailOpen size={16} />
                      </button>
                      <button className="icon-button danger" disabled={busy} onClick={() => onDelete(mailbox.id)} title="删除" type="button">
                        <Trash2 size={16} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!mailboxes.length ? <Empty label="暂无邮箱凭据" /> : null}
          {mailboxes.length > 0 && !filteredMailboxes.length ? <Empty label="没有匹配邮箱" /> : null}
        </div>
      </section>
      {selectedMailbox ? (
        <MailMessageDialog
          folder={supportsJunk ? folder : "inbox"}
          loading={mailLoading}
          mailbox={selectedMailbox}
          messages={messages}
          error={mailError}
          supportsJunk={supportsJunk}
          onClose={() => setSelectedMailbox(null)}
          onFolderChange={setFolder}
        />
      ) : null}
    </div>
  );
}

function MailMessageDialog({
  mailbox,
  messages,
  folder,
  loading,
  error,
  supportsJunk,
  onClose,
  onFolderChange,
}: {
  mailbox: Mailbox;
  messages: MailMessage[];
  folder: "inbox" | "junk";
  loading: boolean;
  error: string;
  supportsJunk: boolean;
  onClose: () => void;
  onFolderChange: (folder: "inbox" | "junk") => void;
}) {
  const timeZone = useDisplayTimeZone();
  const title = useMemo(() => (folder === "junk" ? "垃圾箱" : "收件箱"), [folder]);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-modal="true" className="mail-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">{mailbox.provider}</p>
            <h2>{mailbox.mailbox_email}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>

        <div className="mail-tabs" role="tablist">
          <button className={folder === "inbox" ? "mail-tab active" : "mail-tab"} onClick={() => onFolderChange("inbox")} type="button">
            <Inbox size={16} />
            <span>收件箱</span>
          </button>
          {supportsJunk ? (
            <button className={folder === "junk" ? "mail-tab active" : "mail-tab"} onClick={() => onFolderChange("junk")} type="button">
              <ShieldAlert size={16} />
              <span>垃圾箱</span>
            </button>
          ) : null}
        </div>

        <div className="mail-list-head">
          <strong>{title}</strong>
          <span>{loading ? "读取中" : `${messages.length} 封`}</span>
        </div>

        {error ? <div className="mail-error">{error}</div> : null}
        {!error && loading ? <Empty label="正在读取邮件" /> : null}
        {!error && !loading && !messages.length ? <Empty label="暂无邮件" /> : null}
        {!error && !loading && messages.length ? (
          <div className="mail-list">
            {messages.map((message) => {
              const verificationCode = mailVerificationCode(message);
              return (
                <article className="mail-item" key={message.id}>
                  <div className="mail-item-main">
                    <strong>{message.subject || "无主题"}</strong>
                    <span>{message.body_preview || "无预览内容"}</span>
                  </div>
                  <div className="mail-item-meta">
                    {verificationCode ? (
                      <CopyTextButton
                        className="mail-code-tag mono"
                        copiedLabel="已复制"
                        title="复制验证码"
                        value={verificationCode}
                      />
                    ) : null}
                    <span>{message.sender_name || message.sender_address || "未知发件人"}</span>
                    <time>{message.received_at ? formatDate(message.received_at, timeZone) : "-"}</time>
                  </div>
                </article>
              );
            })}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function AccountPhoneDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-modal="true" className="mail-dialog phone-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">账号手机号</p>
            <h2>{account.email}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="phone-dialog-grid">
          <div className="phone-dialog-card">
            <strong>手机号</strong>
            <span className="mono">{account.phone_number || "-"}</span>
          </div>
          <div className="phone-dialog-card">
            <strong>接码信息</strong>
            <PhoneSourceDetails
              smsCdk={account.phone_sms_cdk}
              smsRechargeUrl={account.phone_sms_recharge_url}
              smsUrl={account.phone_sms_url}
            />
          </div>
        </div>
      </section>
    </div>
  );
}

function PhoneSourceDetails({
  smsUrl,
  smsCdk,
  smsRechargeUrl,
}: {
  smsUrl: string | null | undefined;
  smsCdk: string | null | undefined;
  smsRechargeUrl: string | null | undefined;
}) {
  const [copied, setCopied] = useState(false);
  const tags = phoneSourceTags(smsUrl, smsCdk, smsRechargeUrl);
  const primaryText = primaryPhoneSourceText(smsUrl, smsCdk);
  const onCopy = () => {
    if (!smsCdk) return;
    copyTextToClipboard(smsCdk).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => undefined);
  };

  if (!primaryText && !tags.length && !smsCdk && !smsRechargeUrl) return <span className="muted">未绑定</span>;

  return (
    <div className="phone-source-stack">
      {primaryText ? <span className="mono phone-source-primary">{primaryText}</span> : null}
      <div className="account-chip-list">
        {smsCdk ? (
          <button className="phone-source-tag" onClick={onCopy} type="button">
            <Copy size={14} />
            <span>{copied ? "CDK 已复制" : `CDK: ${smsCdk}`}</span>
          </button>
        ) : null}
        {smsRechargeUrl ? (
          <a className="phone-source-tag" href={smsRechargeUrl} rel="noreferrer" target="_blank">
            <ExternalLink size={14} />
            <span>接码网址</span>
          </a>
        ) : null}
        {tags.map((tag) => (
          <span className="history-metric-tag" key={tag}>{tag}</span>
        ))}
      </div>
    </div>
  );
}

function AccountErrorDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const errorSummary = accountErrorSummary(account);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-modal="true" className="mail-dialog phone-dialog error-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">账号错误详情</p>
            <h2>{account.email}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="phone-dialog-grid error-dialog-grid">
          <div className="phone-dialog-card">
            <strong>错误标签</strong>
            {errorSummary ? <Badge tone={errorSummary.tone}>{errorSummary.label}</Badge> : <span className="muted">无</span>}
          </div>
          <div className="phone-dialog-card">
            <strong>当前状态</strong>
            <span>{account.status || "-"}</span>
          </div>
        </div>
        <div className="phone-dialog-card error-dialog-body">
          <strong>完整报错</strong>
          <pre>{account.last_error || (account.remote_error ? "sub2api 账号异常" : "无错误详情")}</pre>
        </div>
      </section>
    </div>
  );
}

function PhoneView({
  phones,
  accounts,
  busy,
  onImport,
  onExport,
  onRefreshStatuses,
  onUpdateBindings,
  onDelete,
}: {
  phones: PhoneNumber[];
  accounts: Account[];
  busy: boolean;
  onImport: (content: string) => Promise<void> | void;
  onExport: () => Promise<void>;
  onRefreshStatuses: () => Promise<void> | void;
  onUpdateBindings: (id: number, accountEmails: string[]) => Promise<void> | void;
  onDelete: (id: number) => Promise<void> | void;
}) {
  const timeZone = useDisplayTimeZone();
  const [content, setContent] = useState("");
  const [phoneSearch, setPhoneSearch] = useState("");
  const [selectedPhone, setSelectedPhone] = useState<PhoneNumber | null>(null);
  const filteredPhones = useMemo(
    () =>
      phones.filter((phone) =>
        textMatchesSearch(
          [phone.phone_number, phone.sms_url, phone.sms_cdk, phone.sms_recharge_url, phone.account_emails.join(" "), phone.updated_at, phone.created_at],
          phoneSearch,
        ),
      ),
    [phoneSearch, phones],
  );

  return (
    <div className="stack">
      <section className="panel">
        <PanelTitle title="导入手机号" icon={Smartphone} />
        <form
          className="import-form"
          onSubmit={(event) => {
            event.preventDefault();
            onImport(content);
          }}
        >
          <div className="import-controls">
            <div className="form-hint">格式: `17312739425----http://...`、`+13202952260----SMSRTPBXUZK5Y5TVU33` 或 `+13202952260----SMSRTPBXUZK5Y5TVU33----https://chongpt.xyz/recharge`；CDK 会标记为手动处理，自动 OAuth 不会请求接码。</div>
            <div className="toolbar-actions">
              <button className="secondary-button" disabled={busy || !phones.length} onClick={() => onExport().catch(() => undefined)} type="button">
                <Save size={17} />
                <span>导出</span>
              </button>
              <button className="primary-button" disabled={busy || !content.trim()} type="submit">
                <Smartphone size={17} />
                <span>导入</span>
              </button>
            </div>
          </div>
          <textarea
            onChange={(event) => setContent(event.target.value)}
            placeholder={"17312739425----http://qk.sms777.top/sms/api/get_orange_sms?app_id=480&phone=17312739425\n+17312739426----https://example.com/sms?id=2\n+13202952260----SMSRTPBXUZK5Y5TVU33\n+13202952261----SMSRTPBXUZK5Y5TVU34----https://chongpt.xyz/recharge"}
            rows={6}
            value={content}
          />
        </form>
      </section>

      <section className="panel">
        <div className="panel-toolbar">
          <PanelTitle title="手机号列表" icon={ShieldCheck} />
          <div className="toolbar-actions">
            <SearchBox
              count={filteredPhones.length}
              onChange={setPhoneSearch}
              placeholder="搜索手机号 / 链接 / 账号"
              total={phones.length}
              value={phoneSearch}
            />
            <button className="secondary-button" disabled={busy || !phones.length} onClick={() => Promise.resolve(onRefreshStatuses()).catch(() => undefined)} type="button">
              <RefreshCcw size={17} />
              <span>检查接码</span>
            </button>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>手机号</th>
                <th>状态</th>
                <th>接码信息</th>
                <th>绑定账号</th>
                <th>绑定数</th>
                <th>最近更新</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filteredPhones.map((phone) => {
                const smsSummary = phoneSmsSummary(phone, timeZone);
                return (
                  <tr key={phone.id}>
                    <td className="mono">{phone.phone_number}</td>
                    <td>
                      <div className="status-stack">
                        <Badge tone={smsSummary.tone}>{smsSummary.label}</Badge>
                        <span>{smsSummary.detail}</span>
                      </div>
                    </td>
                    <td className="phone-source-cell">
                      <PhoneSourceDetails smsCdk={phone.sms_cdk} smsRechargeUrl={phone.sms_recharge_url} smsUrl={phone.sms_url} />
                    </td>
                    <td>
                      <div className="account-chip-list">
                        {phone.account_emails.length
                          ? phone.account_emails.map((email) => (
                              <span className="history-metric-tag" key={email}>
                                {email}
                              </span>
                            ))
                          : <span className="muted">未绑定</span>}
                      </div>
                    </td>
                    <td>{`${phone.bindings_count}/3`}</td>
                    <td>{formatDate(phone.updated_at || phone.created_at, timeZone)}</td>
                    <td className="right">
                      <div className="row-actions">
                        <button className="icon-button" disabled={busy} onClick={() => setSelectedPhone(phone)} title="编辑绑定" type="button">
                          <Link2 size={16} />
                        </button>
                        <button className="icon-button danger" disabled={busy} onClick={() => onDelete(phone.id)} title="删除" type="button">
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!phones.length ? <Empty label="暂无手机号" /> : null}
          {phones.length > 0 && !filteredPhones.length ? <Empty label="没有匹配手机号" /> : null}
        </div>
      </section>
      {selectedPhone ? (
        <PhoneBindingDialog
          accounts={accounts}
          busy={busy}
          phone={selectedPhone}
          onClose={() => setSelectedPhone(null)}
          onSave={(accountEmails) => onUpdateBindings(selectedPhone.id, accountEmails)}
        />
      ) : null}
    </div>
  );
}

function PhoneBindingDialog({
  phone,
  accounts,
  busy,
  onClose,
  onSave,
}: {
  phone: PhoneNumber;
  accounts: Account[];
  busy: boolean;
  onClose: () => void;
  onSave: (accountEmails: string[]) => Promise<void> | void;
}) {
  const [content, setContent] = useState(phone.account_emails.join("\n"));
  const candidates = useMemo(() => [...new Set(accounts.map((account) => account.email))].sort(), [accounts]);
  const parsedEmails = useMemo(
    () => [...new Set(content.split(/\r?\n|,/).map((item) => item.trim().toLowerCase()).filter(Boolean))],
    [content],
  );
  const invalid = parsedEmails.length > 3;

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-modal="true" className="mail-dialog phone-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">手机号绑定</p>
            <h2>{phone.phone_number}</h2>
          </div>
          <button className="icon-button" onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="phone-dialog-grid">
          <div className="phone-dialog-card">
            <strong>接码信息</strong>
            <PhoneSourceDetails smsCdk={phone.sms_cdk} smsRechargeUrl={phone.sms_recharge_url} smsUrl={phone.sms_url} />
          </div>
          <div className="phone-dialog-card">
            <strong>候选账号</strong>
            <div className="account-chip-list">
              {candidates.slice(0, 20).map((email) => (
                <span className="history-metric-tag" key={email}>{email}</span>
              ))}
            </div>
          </div>
        </div>
        <label>
          绑定账号
          <textarea onChange={(event) => setContent(event.target.value)} rows={6} value={content} />
        </label>
        <p className="form-hint">每行一个邮箱，最多 3 个。已绑定到其他手机号的账号会自动移动到当前手机号。</p>
        {invalid ? <div className="mail-error">一个手机号最多绑定 3 个账号。</div> : null}
        <div className="settings-actions">
          <div className="key-state">
            <Smartphone size={16} />
            <span>当前 {parsedEmails.length}/3 个账号</span>
          </div>
          <button
            className="primary-button"
            disabled={busy || invalid}
            onClick={() => {
              Promise.resolve(onSave(parsedEmails)).then(() => onClose()).catch(() => undefined);
            }}
            type="button"
          >
            <Save size={17} />
            <span>保存绑定</span>
          </button>
        </div>
      </section>
    </div>
  );
}

function SettingsView({
  settings,
  subscriptionTypes,
  busy,
  onSave,
  onScan,
}: {
  settings: AppSettings;
  subscriptionTypes: string[];
  busy: boolean;
  onSave: (payload: AppSettingsUpdate) => Promise<void> | void;
  onScan: () => Promise<void> | void;
}) {
  const [siteName, setSiteName] = useState(settings.site_name || defaultSiteName);
  const [instanceUrl, setInstanceUrl] = useState(toSub2ApiInstanceUrl(settings.sub2api_base_url));
  const [recoveryEnabled, setRecoveryEnabled] = useState(settings.recovery_enabled);
  const [autoRecoverState, setAutoRecoverState] = useState(settings.sub2api_auto_recover_state);
  const [automationPaused, setAutomationPaused] = useState(settings.automation_paused);
  const [interval, setInterval] = useState(String(settings.monitor_interval_seconds));
  const [usageRefreshEnabled, setUsageRefreshEnabled] = useState(settings.usage_refresh_enabled);
  const [usageRefreshInterval, setUsageRefreshInterval] = useState(String(settings.usage_refresh_interval_seconds));
  const [usageRefreshMaxConcurrency, setUsageRefreshMaxConcurrency] = useState(
    String(settings.usage_refresh_max_concurrency || 5),
  );
  const [usageLimitSampleFiveHourThreshold, setUsageLimitSampleFiveHourThreshold] = useState(
    String(settings.usage_limit_sample_five_hour_threshold_percent ?? 0),
  );
  const [usageLimitSampleSevenDayThreshold, setUsageLimitSampleSevenDayThreshold] = useState(
    String(settings.usage_limit_sample_seven_day_threshold_percent ?? 0),
  );
  const subscriptionTypesKey = subscriptionTypes.join("\u0000");
  const [usageLimitDefaultRanges, setUsageLimitDefaultRanges] = useState<UsageLimitDefaultRanges>(() =>
    mergeUsageLimitDefaultRanges(settings.usage_limit_default_ranges, subscriptionTypes),
  );
  const [newSubscriptionType, setNewSubscriptionType] = useState("");
  const [protocolRefreshMaxConcurrency, setProtocolRefreshMaxConcurrency] = useState(
    String(settings.protocol_refresh_max_concurrency || settings.refresh_max_concurrency || 1),
  );
  const [browserRefreshMaxConcurrency, setBrowserRefreshMaxConcurrency] = useState(
    String(settings.browser_refresh_max_concurrency || 1),
  );
  const [browserMinAvailableMemoryMb, setBrowserMinAvailableMemoryMb] = useState(
    String(settings.browser_min_available_memory_mb ?? 500),
  );
  const [subscriptionRefreshBatchSize, setSubscriptionRefreshBatchSize] = useState(
    String(settings.subscription_refresh_batch_size || 3),
  );
  const [subscriptionRefreshMaxConcurrency, setSubscriptionRefreshMaxConcurrency] = useState(
    String(settings.subscription_refresh_max_concurrency || 3),
  );
  const [displayTimeZone, setDisplayTimeZone] = useState(settings.display_timezone || defaultTimeZone);
  const [xApiKey, setXApiKey] = useState("");
  const [clearXApiKey, setClearXApiKey] = useState(false);

  useEffect(() => {
    setSiteName(settings.site_name || defaultSiteName);
    setInstanceUrl(toSub2ApiInstanceUrl(settings.sub2api_base_url));
    setRecoveryEnabled(settings.recovery_enabled);
    setAutoRecoverState(settings.sub2api_auto_recover_state);
    setAutomationPaused(settings.automation_paused);
    setInterval(String(settings.monitor_interval_seconds));
    setUsageRefreshEnabled(settings.usage_refresh_enabled);
    setUsageRefreshInterval(String(settings.usage_refresh_interval_seconds));
    setUsageRefreshMaxConcurrency(String(settings.usage_refresh_max_concurrency || 5));
    setUsageLimitSampleFiveHourThreshold(String(settings.usage_limit_sample_five_hour_threshold_percent ?? 0));
    setUsageLimitSampleSevenDayThreshold(String(settings.usage_limit_sample_seven_day_threshold_percent ?? 0));
    setUsageLimitDefaultRanges(mergeUsageLimitDefaultRanges(settings.usage_limit_default_ranges, subscriptionTypes));
    setNewSubscriptionType("");
    setProtocolRefreshMaxConcurrency(
      String(settings.protocol_refresh_max_concurrency || settings.refresh_max_concurrency || 1),
    );
    setBrowserRefreshMaxConcurrency(String(settings.browser_refresh_max_concurrency || 1));
    setBrowserMinAvailableMemoryMb(String(settings.browser_min_available_memory_mb ?? 500));
    setSubscriptionRefreshBatchSize(String(settings.subscription_refresh_batch_size || 3));
    setSubscriptionRefreshMaxConcurrency(String(settings.subscription_refresh_max_concurrency || 3));
    setDisplayTimeZone(settings.display_timezone || defaultTimeZone);
    setXApiKey("");
    setClearXApiKey(false);
  }, [
    settings.automation_paused,
    settings.browser_min_available_memory_mb,
    settings.browser_refresh_max_concurrency,
    settings.display_timezone,
    settings.monitor_interval_seconds,
    settings.protocol_refresh_max_concurrency,
    settings.refresh_max_concurrency,
    settings.recovery_enabled,
    settings.site_name,
    settings.subscription_refresh_batch_size,
    settings.subscription_refresh_max_concurrency,
    settings.sub2api_auto_recover_state,
    settings.sub2api_base_url,
    settings.sub2api_x_api_key_hint,
    settings.sub2api_x_api_key_set,
    settings.usage_refresh_enabled,
    settings.usage_refresh_interval_seconds,
    settings.usage_refresh_max_concurrency,
    settings.usage_limit_sample_five_hour_threshold_percent,
    settings.usage_limit_sample_seven_day_threshold_percent,
    settings.usage_limit_default_ranges,
    subscriptionTypesKey,
  ]);

  const intervalNumber = Number(interval);
  const usageRefreshIntervalNumber = Number(usageRefreshInterval);
  const usageRefreshMaxConcurrencyNumber = Number(usageRefreshMaxConcurrency);
  const usageLimitSampleFiveHourThresholdNumber = Number(usageLimitSampleFiveHourThreshold);
  const usageLimitSampleSevenDayThresholdNumber = Number(usageLimitSampleSevenDayThreshold);
  const protocolRefreshMaxConcurrencyNumber = Number(protocolRefreshMaxConcurrency);
  const browserRefreshMaxConcurrencyNumber = Number(browserRefreshMaxConcurrency);
  const browserMinAvailableMemoryMbNumber = Number(browserMinAvailableMemoryMb);
  const subscriptionRefreshBatchSizeNumber = Number(subscriptionRefreshBatchSize);
  const subscriptionRefreshMaxConcurrencyNumber = Number(subscriptionRefreshMaxConcurrency);
  const cleanSiteName = siteName.trim();
  const usageLimitDefaultRangesInvalid =
    !usageLimitDefaultRanges.unknown ||
    Object.keys(usageLimitDefaultRanges).length > 100 ||
    Object.values(usageLimitDefaultRanges).some((planRanges) =>
      usageLimitWindowKeys.some((windowKey) => {
        const range = planRanges[windowKey];
        return !Number.isFinite(range.lower) || !Number.isFinite(range.upper) || range.lower < 0 || range.upper < range.lower || range.upper > 1_000_000_000;
      }),
    );
  const invalid =
    !cleanSiteName ||
    cleanSiteName.length > 80 ||
    !isSub2ApiInstanceUrl(instanceUrl) ||
    !Number.isInteger(intervalNumber) ||
    intervalNumber < 30 ||
    !Number.isInteger(usageRefreshIntervalNumber) ||
    usageRefreshIntervalNumber < 60 ||
    usageRefreshIntervalNumber > 86_400 ||
    !Number.isInteger(usageRefreshMaxConcurrencyNumber) ||
    usageRefreshMaxConcurrencyNumber < 1 ||
    usageRefreshMaxConcurrencyNumber > 20 ||
    !Number.isFinite(usageLimitSampleFiveHourThresholdNumber) ||
    usageLimitSampleFiveHourThresholdNumber < 0 ||
    usageLimitSampleFiveHourThresholdNumber > 100 ||
    !Number.isFinite(usageLimitSampleSevenDayThresholdNumber) ||
    usageLimitSampleSevenDayThresholdNumber < 0 ||
    usageLimitSampleSevenDayThresholdNumber > 100 ||
    !Number.isInteger(protocolRefreshMaxConcurrencyNumber) ||
    protocolRefreshMaxConcurrencyNumber < 1 ||
    protocolRefreshMaxConcurrencyNumber > 50 ||
    !Number.isInteger(browserRefreshMaxConcurrencyNumber) ||
    browserRefreshMaxConcurrencyNumber < 1 ||
    browserRefreshMaxConcurrencyNumber > 50 ||
    !Number.isInteger(browserMinAvailableMemoryMbNumber) ||
    browserMinAvailableMemoryMbNumber < 0 ||
    browserMinAvailableMemoryMbNumber > 1_048_576 ||
    !Number.isInteger(subscriptionRefreshBatchSizeNumber) ||
    subscriptionRefreshBatchSizeNumber < 1 ||
    subscriptionRefreshBatchSizeNumber > 100 ||
    !Number.isInteger(subscriptionRefreshMaxConcurrencyNumber) ||
    subscriptionRefreshMaxConcurrencyNumber < 1 ||
    subscriptionRefreshMaxConcurrencyNumber > 20 ||
    usageLimitDefaultRangesInvalid;

  const addSubscriptionType = () => {
    const normalized = normalizeSubscriptionType(newSubscriptionType);
    if (!normalized || normalized === "unknown") return;
    setUsageLimitDefaultRanges((current) => ({
      ...current,
      [normalized]: cloneUsageLimitPlanRanges(current[normalized] || current.unknown || defaultUsageLimitPlanRanges),
    }));
    setNewSubscriptionType("");
  };

  const updateUsageLimitRange = (
    subscriptionType: string,
    windowKey: (typeof usageLimitWindowKeys)[number],
    bound: "lower" | "upper",
    value: string,
  ) => {
    setUsageLimitDefaultRanges((current) => ({
      ...current,
      [subscriptionType]: {
        ...current[subscriptionType],
        [windowKey]: {
          ...current[subscriptionType][windowKey],
          [bound]: Number(value),
        },
      },
    }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (invalid) return;
    const payload: AppSettingsUpdate = {
      site_name: cleanSiteName,
      sub2api_base_url: toSub2ApiInstanceUrl(instanceUrl),
      recovery_enabled: recoveryEnabled,
      sub2api_auto_recover_state: autoRecoverState,
      automation_paused: automationPaused,
      monitor_interval_seconds: intervalNumber,
      usage_refresh_enabled: usageRefreshEnabled,
      usage_refresh_interval_seconds: usageRefreshIntervalNumber,
      usage_refresh_max_concurrency: usageRefreshMaxConcurrencyNumber,
      usage_limit_sample_five_hour_threshold_percent: usageLimitSampleFiveHourThresholdNumber,
      usage_limit_sample_seven_day_threshold_percent: usageLimitSampleSevenDayThresholdNumber,
      usage_limit_default_ranges: usageLimitDefaultRanges,
      protocol_refresh_max_concurrency: protocolRefreshMaxConcurrencyNumber,
      browser_refresh_max_concurrency: browserRefreshMaxConcurrencyNumber,
      browser_min_available_memory_mb: browserMinAvailableMemoryMbNumber,
      subscription_refresh_batch_size: subscriptionRefreshBatchSizeNumber,
      subscription_refresh_max_concurrency: subscriptionRefreshMaxConcurrencyNumber,
      display_timezone: displayTimeZone,
    };
    if (xApiKey.trim()) {
      payload.sub2api_x_api_key = xApiKey.trim();
    }
    if (clearXApiKey) {
      payload.clear_sub2api_x_api_key = true;
    }
    await onSave(payload);
  };

  return (
    <div className="stack">
      <section className="panel">
        <PanelTitle title="sub2api 连接" icon={Link2} />
        <form className="settings-form" onSubmit={submit}>
          <div className="settings-grid settings-main-grid">
            <label className="site-name-label">
              站点名
              <input
                maxLength={80}
                onChange={(event) => setSiteName(event.target.value)}
                placeholder={defaultSiteName}
                value={siteName}
              />
            </label>
            <label className="instance-url-label">
              sub2api 实例 URL
              <div className="url-input-group">
                <input
                  onBlur={() => setInstanceUrl(toSub2ApiInstanceUrl(instanceUrl))}
                  onChange={(event) => setInstanceUrl(event.target.value)}
                  placeholder="https://sub2api.example.com"
                  value={instanceUrl}
                />
                <span className="url-suffix">{sub2ApiApiPrefix}</span>
              </div>
            </label>
            <label>
              巡检间隔（秒）
              <input
                min={30}
                onChange={(event) => setInterval(event.target.value)}
                type="number"
                value={interval}
              />
            </label>
            <label>
              用量查询间隔（秒）
              <input
                max={86400}
                min={60}
                onChange={(event) => setUsageRefreshInterval(event.target.value)}
                type="number"
                value={usageRefreshInterval}
              />
            </label>
            <label>
              协议最大同时数
              <input
                max={50}
                min={1}
                onChange={(event) => setProtocolRefreshMaxConcurrency(event.target.value)}
                type="number"
                value={protocolRefreshMaxConcurrency}
              />
            </label>
            <label>
              用量查询最大同时数
              <input
                max={20}
                min={1}
                onChange={(event) => setUsageRefreshMaxConcurrency(event.target.value)}
                type="number"
                value={usageRefreshMaxConcurrency}
              />
            </label>
            <label>
              默认 5h 用量阈值 (%)
              <input
                max={100}
                min={0}
                onChange={(event) => setUsageLimitSampleFiveHourThreshold(event.target.value)}
                step="0.1"
                title={`0 表示使用默认 ${defaultUsageLimitSampleThresholdPercent}%`}
                type="number"
                value={usageLimitSampleFiveHourThreshold}
              />
            </label>
            <label>
              默认 7d 用量阈值 (%)
              <input
                max={100}
                min={0}
                onChange={(event) => setUsageLimitSampleSevenDayThreshold(event.target.value)}
                step="0.1"
                title={`0 表示使用默认 ${defaultUsageLimitSampleThresholdPercent}%`}
                type="number"
                value={usageLimitSampleSevenDayThreshold}
              />
            </label>
            <label>
              浏览器最大同时登录数
              <input
                max={50}
                min={1}
                onChange={(event) => setBrowserRefreshMaxConcurrency(event.target.value)}
                type="number"
                value={browserRefreshMaxConcurrency}
              />
            </label>
            <label>
              浏览器最低可用内存（MB）
              <input
                max={1048576}
                min={0}
                onChange={(event) => setBrowserMinAvailableMemoryMb(event.target.value)}
                type="number"
                value={browserMinAvailableMemoryMb}
              />
            </label>
            <label>
              单次订阅查询数量
              <input
                max={100}
                min={1}
                onChange={(event) => setSubscriptionRefreshBatchSize(event.target.value)}
                type="number"
                value={subscriptionRefreshBatchSize}
              />
            </label>
            <label>
              订阅查询最大同时数
              <input
                max={20}
                min={1}
                onChange={(event) => setSubscriptionRefreshMaxConcurrency(event.target.value)}
                type="number"
                value={subscriptionRefreshMaxConcurrency}
              />
            </label>
          </div>

          <fieldset className="quota-range-settings">
            <legend>订阅默认额度区间</legend>
            <div className="quota-range-list">
              {Object.entries(usageLimitDefaultRanges)
                .sort(([left], [right]) => subscriptionTypeSortRank(left) - subscriptionTypeSortRank(right) || left.localeCompare(right))
                .map(([subscriptionType, planRanges]) => (
                  <div className="quota-range-row" key={subscriptionType}>
                    <div className="quota-range-type">
                      <strong>{subscriptionTypeLabel(subscriptionType)}</strong>
                      <span>{subscriptionType}</span>
                    </div>
                    {usageLimitWindowKeys.map((windowKey) => (
                      <div className="quota-range-window" key={windowKey}>
                        <span>{usageLimitWindowLabel(windowKey)}</span>
                        <label>
                          下限
                          <input
                            aria-label={`${subscriptionTypeLabel(subscriptionType)} ${usageLimitWindowLabel(windowKey)} 下限`}
                            min={0}
                            onChange={(event) => updateUsageLimitRange(subscriptionType, windowKey, "lower", event.target.value)}
                            step="0.01"
                            type="number"
                            value={planRanges[windowKey].lower}
                          />
                        </label>
                        <label>
                          上限
                          <input
                            aria-label={`${subscriptionTypeLabel(subscriptionType)} ${usageLimitWindowLabel(windowKey)} 上限`}
                            min={0}
                            onChange={(event) => updateUsageLimitRange(subscriptionType, windowKey, "upper", event.target.value)}
                            step="0.01"
                            type="number"
                            value={planRanges[windowKey].upper}
                          />
                        </label>
                      </div>
                    ))}
                    <button
                      aria-label={`删除 ${subscriptionTypeLabel(subscriptionType)} 额度配置`}
                      className="icon-button quota-range-delete"
                      disabled={coreSubscriptionTypes.has(subscriptionType)}
                      onClick={() =>
                        setUsageLimitDefaultRanges((current) =>
                          Object.fromEntries(Object.entries(current).filter(([key]) => key !== subscriptionType)),
                        )
                      }
                      title={coreSubscriptionTypes.has(subscriptionType) ? "内置订阅类型" : "删除订阅类型"}
                      type="button"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                ))}
            </div>
            <div className="quota-range-add">
              <input
                maxLength={80}
                onChange={(event) => setNewSubscriptionType(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    addSubscriptionType();
                  }
                }}
                placeholder="新增订阅类型"
                value={newSubscriptionType}
              />
              <button
                aria-label="新增订阅类型"
                className="secondary-button"
                disabled={!newSubscriptionType.trim() || normalizeSubscriptionType(newSubscriptionType) === "unknown"}
                onClick={addSubscriptionType}
                title="新增订阅类型"
                type="button"
              >
                <Plus size={17} />
                <span>新增</span>
              </button>
            </div>
          </fieldset>

          <div className="settings-toggle-list">
            <label className="checkbox-line settings-toggle">
              <input
                checked={recoveryEnabled}
                onChange={(event) => setRecoveryEnabled(event.target.checked)}
                type="checkbox"
              />
              <span>开启账号恢复任务</span>
            </label>
            <label className="checkbox-line settings-toggle">
              <input
                checked={autoRecoverState}
                onChange={(event) => setAutoRecoverState(event.target.checked)}
                type="checkbox"
              />
              <span>刷新成功后自动恢复 sub2api 调度状态</span>
            </label>
            <label className="checkbox-line settings-toggle">
              <input
                checked={automationPaused}
                onChange={(event) => setAutomationPaused(event.target.checked)}
                type="checkbox"
              />
              <span>暂停自动巡检与自动用量查询</span>
            </label>
            <label className="checkbox-line settings-toggle">
              <input
                checked={usageRefreshEnabled}
                onChange={(event) => setUsageRefreshEnabled(event.target.checked)}
                type="checkbox"
              />
              <span>自动查询账号用量窗口</span>
            </label>
          </div>

          <div className="settings-grid time-zone-grid">
            <label>
              显示时区
              <select onChange={(event) => setDisplayTimeZone(event.target.value)} value={displayTimeZone}>
                {timeZoneOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <div className="time-zone-preview">
              <Globe2 size={16} />
              <span>{formatDate(new Date().toISOString(), displayTimeZone)}</span>
            </div>
          </div>

          <div className="settings-grid secret-grid">
            <label>
              x-api 密钥
              <input
                autoComplete="new-password"
                onChange={(event) => setXApiKey(event.target.value)}
                placeholder={settings.sub2api_x_api_key_set ? "留空保持当前密钥" : "输入 sub2api x-api-key"}
                type="password"
                value={xApiKey}
              />
            </label>
            <label className="checkbox-line">
              <input
                checked={clearXApiKey}
                onChange={(event) => setClearXApiKey(event.target.checked)}
                type="checkbox"
              />
              <span>清空已保存密钥</span>
            </label>
          </div>

          <div className="settings-actions">
            <div className="key-state">
              <KeyRound size={16} />
              <span>{settings.sub2api_x_api_key_set ? `已保存 ${settings.sub2api_x_api_key_hint || ""}` : "未设置"}</span>
            </div>
            <button className="primary-button" disabled={busy || invalid} type="submit">
              <Save size={17} />
              <span>保存设置</span>
            </button>
          </div>
        </form>
      </section>

      <section className="panel">
        <PanelTitle title="端口扫描" icon={Radar} />
        <div className="settings-status">
          <SignalLine label="配置来源" value={sourceLabel(settings.sub2api_base_url_source)} />
          <SignalLine label="当前地址" value={settings.sub2api_base_url} />
          <SignalLine label="上次扫描" value={settings.last_scan_at ? formatDate(settings.last_scan_at, displayTimeZone) : "暂无"} />
          <SignalLine label="扫描结果" value={settings.last_scan_message || "暂无"} />
        </div>
        <div className="settings-actions">
          <div className="key-state">
            <TimerReset size={16} />
            <span>
              {settings.automation_paused ? "已暂停自动任务 · " : ""}巡检 {settings.monitor_interval_seconds} 秒 · 用量{" "}
              {settings.usage_refresh_enabled ? `${settings.usage_refresh_interval_seconds} 秒` : "关闭"} · 用量并发{" "}
              {settings.usage_refresh_max_concurrency} · 协议并发{" "}
              {settings.protocol_refresh_max_concurrency || settings.refresh_max_concurrency} · 浏览器并发{" "}
              {settings.browser_refresh_max_concurrency} · 样本阈值 5h{" "}
              {sampleThresholdSettingLabel(settings.usage_limit_sample_five_hour_threshold_percent)} / 7d{" "}
              {sampleThresholdSettingLabel(settings.usage_limit_sample_seven_day_threshold_percent)} · 订阅单次{" "}
              {settings.subscription_refresh_batch_size} · 订阅并发{" "}
              {settings.subscription_refresh_max_concurrency} · 浏览器内存阈值 {settings.browser_min_available_memory_mb} MB · 恢复任务{" "}
              {settings.recovery_enabled ? "开启" : "关闭"} · 状态恢复 {settings.sub2api_auto_recover_state ? "开启" : "关闭"}
            </span>
          </div>
          <button className="secondary-button" disabled={busy} onClick={onScan} type="button">
            <Radar size={17} />
            <span>扫描 sub2api</span>
          </button>
        </div>
      </section>
    </div>
  );
}

function HistoryView({
  jobs,
  events,
  exceptionRecords,
  busy,
  onClear,
  onDeleteExceptionRecord,
  onLocateAccount,
}: {
  jobs: RefreshJob[];
  events: AppEvent[];
  exceptionRecords: AccountExceptionRecord[];
  busy: boolean;
  onClear: () => void;
  onDeleteExceptionRecord: (id: number) => void;
  onLocateAccount: (record: AccountExceptionRecord) => void;
}) {
  const timeZone = useDisplayTimeZone();
  const [subview, setSubview] = useState<"exceptions" | "jobs" | "events">("exceptions");
  const hasHistory = jobs.length > 0 || events.length > 0;
  const clearHistory = () => {
    if (!hasHistory || busy) return;
    if (window.confirm("确定要清空刷新任务和事件历史吗？")) {
      onClear();
    }
  };

  return (
    <div className="stack">
      <div className="history-toolbar">
        <div className="history-counts">
          <span>
            <AlertTriangle size={14} />
            {exceptionRecords.length} 条异常
          </span>
          <span>
            <RefreshCcw size={14} />
            {jobs.length} 条任务
          </span>
          <span>
            <Activity size={14} />
            {events.length} 条事件
          </span>
        </div>
        <button className="danger-button" disabled={busy || !hasHistory} onClick={clearHistory} type="button">
          <Trash2 size={17} />
          <span>清空历史</span>
        </button>
      </div>

      <div className="history-tabs" role="tablist" aria-label="历史子界面">
        <button
          aria-selected={subview === "exceptions"}
          className={subview === "exceptions" ? "history-tab active" : "history-tab"}
          onClick={() => setSubview("exceptions")}
          role="tab"
          type="button"
        >
          <AlertTriangle size={16} />
          <span>异常账号</span>
          <strong>{exceptionRecords.length}</strong>
        </button>
        <button
          aria-selected={subview === "jobs"}
          className={subview === "jobs" ? "history-tab active" : "history-tab"}
          onClick={() => setSubview("jobs")}
          role="tab"
          type="button"
        >
          <RefreshCcw size={16} />
          <span>刷新任务</span>
          <strong>{jobs.length}</strong>
        </button>
        <button
          aria-selected={subview === "events"}
          className={subview === "events" ? "history-tab active" : "history-tab"}
          onClick={() => setSubview("events")}
          role="tab"
          type="button"
        >
          <Activity size={16} />
          <span>事件</span>
          <strong>{events.length}</strong>
        </button>
      </div>

      {subview === "exceptions" ? (
        <section className="panel">
          <PanelTitle title="异常账号记录" icon={AlertTriangle} />
          <div className="history-list">
            {exceptionRecords.map((record) => {
              const relatedJob = latestErrorRefreshJobForRecord(record, jobs);
              const displayMessage = relatedJob?.reason || record.message || "-";
              const displayTime = relatedJob?.created_at ?? record.updated_at;
              const protocolSummary = relatedJob ? refreshJobProtocolSummary(relatedJob, events) : null;
              return (
                <article className="history-item exception-record-item" key={record.id}>
                  <div className="history-item-head">
                    <div className="history-meta">
                      <Badge tone={exceptionStatusTone(record.status)}>{exceptionStatusLabel(record.status)}</Badge>
                      <Badge tone="ink">{exceptionSourceLabel(record.source)}</Badge>
                      <strong className="mono history-email">{record.email || "unknown"}</strong>
                      {record.sub2api_account_id ? <span className="memory-pill mono">{record.sub2api_account_id}</span> : null}
                    </div>
                    <div className="history-record-actions">
                      <time>{formatDate(displayTime, timeZone)}</time>
                      <button
                        aria-label="在账号界面定位此账号"
                        className="icon-button"
                        disabled={busy || (!record.email && !record.sub2api_account_id)}
                        onClick={() => onLocateAccount(record)}
                        title="在账号界面定位此账号"
                        type="button"
                      >
                        <ExternalLink size={16} />
                      </button>
                      <button
                        aria-label="删除异常账号记录"
                        className="icon-button history-dismiss-button"
                        disabled={busy}
                        onClick={() => onDeleteExceptionRecord(record.id)}
                        title="删除这条异常账号记录"
                        type="button"
                      >
                        <X size={16} />
                      </button>
                    </div>
                  </div>
                  <p className="history-message">{displayMessage}</p>
                  {protocolSummary ? <p className="history-message">{`协议链路：${protocolSummary}`}</p> : null}
                </article>
              );
            })}
            {!exceptionRecords.length ? <Empty label="暂无异常账号记录" /> : null}
          </div>
        </section>
      ) : null}

      {subview === "jobs" ? (
        <section className="panel">
          <PanelTitle title="刷新任务" icon={RefreshCcw} />
          <div className="history-list">
            {jobs.map((job) => {
              const protocolSummary = refreshJobProtocolSummary(job, events);
              return (
                <article className="history-item" key={job.id}>
                  <div className="history-item-head">
                    <div className="history-meta">
                      <Badge tone={job.status === "succeeded" ? "ok" : job.status === "running" ? "running" : "error"}>
                        {statusLabel(job.status)}
                      </Badge>
                      <strong className="mono history-email">{job.email}</strong>
                      {job.memory_peak_rss_bytes ? (
                        <span className="memory-pill">内存峰值 {formatBytes(job.memory_peak_rss_bytes)}</span>
                      ) : null}
                    </div>
                    <time>{formatDate(job.created_at, timeZone)}</time>
                  </div>
                  <p className="history-message">{job.reason || "-"}</p>
                  {protocolSummary ? <p className="history-message">{`协议链路：${protocolSummary}`}</p> : null}
                </article>
              );
            })}
            {!jobs.length ? <Empty label="暂无刷新任务" /> : null}
          </div>
        </section>
      ) : null}

      {subview === "events" ? (
        <section className="panel">
          <PanelTitle title="事件" icon={Activity} />
          <div className="history-list">
            {events.map((event) => {
              const reasonLabel = eventReasonLabel(event);
              const syncErrorCount = eventSyncErrorCount(event);
              return (
                <article className="history-item event-history-item" key={event.id}>
                  <div className="history-item-head">
                    <div className="history-meta">
                      <Badge tone={eventTone(event.kind)}>{eventKindLabel(event.kind)}</Badge>
                      {reasonLabel ? <Badge tone="ink">{reasonLabel}</Badge> : null}
                      {syncErrorCount !== null ? (
                        <Badge tone={syncErrorCount > 0 ? "error" : "ok"}>{`错误账号 ${syncErrorCount}`}</Badge>
                      ) : null}
                      <strong className="history-email">{event.email || "system"}</strong>
                    </div>
                    <time>{formatDate(event.created_at, timeZone)}</time>
                  </div>
                  <p className="history-message">{event.message}</p>
                </article>
              );
            })}
            {!events.length ? <Empty label="暂无事件" /> : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function PanelTitle({ title, icon: Icon }: { title: string; icon: LucideIcon }) {
  return (
    <div className="panel-title">
      <Icon size={18} />
      <h2>{title}</h2>
    </div>
  );
}

function Badge({ children, className, tone }: { children: string; className?: string; tone: string }) {
  return <span className={["badge", tone, className].filter(Boolean).join(" ")}>{children}</span>;
}

function CopyTextButton({
  value,
  className = "",
  title = "复制",
  copiedLabel = "已复制",
  hideIcon = false,
}: {
  value: string;
  className?: string;
  title?: string;
  copiedLabel?: string;
  hideIcon?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, []);

  const onCopy = () => {
    copyTextToClipboard(value).then(() => {
      setCopied(true);
      if (timerRef.current) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => undefined);
  };

  return (
    <button
      aria-label={`${title}: ${value}`}
      className={["copy-text-button", className].filter(Boolean).join(" ")}
      onClick={onCopy}
      title={copied ? copiedLabel : title}
      type="button"
    >
      <span>{value}</span>
      {copied ? <span className="copy-feedback">{copiedLabel}</span> : hideIcon ? null : <Copy size={13} />}
    </button>
  );
}

function SearchBox({ value, placeholder, count, total, onChange }: { value: string; placeholder: string; count: number; total: number; onChange: (value: string) => void }) {
  return (
    <label className="list-search">
      <Search size={15} />
      <input
        aria-label={placeholder}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        type="search"
        value={value}
      />
      <span>{value.trim() ? `${count}/${total}` : `${total}`}</span>
      {value ? (
        <button aria-label="清空搜索" onClick={() => onChange("")} type="button">
          <X size={14} />
        </button>
      ) : null}
    </label>
  );
}

type QuickFilterOption<T extends string> = { value: T; label: string };

function AccountStatusFilterMenu({
  value,
  options,
  onChange,
}: {
  value: AccountStatusFilter;
  options: Array<QuickFilterOption<AccountStatusFilter>>;
  onChange: (value: AccountStatusFilter) => void;
}) {
  return <QuickFilterMenu ariaLabel="状态筛选选项" label="状态筛选" onChange={onChange} options={options} value={value} />;
}

function AccountSubscriptionFilterMenu({
  value,
  options,
  onChange,
}: {
  value: string;
  options: Array<QuickFilterOption<string>>;
  onChange: (value: string) => void;
}) {
  return <QuickFilterMenu ariaLabel="订阅筛选选项" label="订阅筛选" onChange={onChange} options={options} value={value} />;
}

function QuickFilterMenu<T extends string>({
  value,
  options,
  label,
  ariaLabel,
  onChange,
}: {
  value: T;
  options: Array<QuickFilterOption<T>>;
  label: string;
  ariaLabel: string;
  onChange: (value: T) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const selectedOption = options.find((option) => option.value === value) || options[0] || { value, label: value || "全部" };

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", handlePointerDown);
    return () => window.removeEventListener("mousedown", handlePointerDown);
  }, [open]);

  return (
    <div className={open ? "quick-filter open" : "quick-filter"} onKeyDown={(event) => event.key === "Escape" && setOpen(false)} ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        className="secondary-button quick-filter-button"
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        <span className="quick-filter-label">{label}</span>
        <strong className="quick-filter-value">{selectedOption.label}</strong>
        <ChevronDown size={14} />
      </button>
      {open ? (
        <div aria-label={ariaLabel} className="quick-filter-menu" role="menu">
          {options.map((option) => (
            <button
              aria-checked={option.value === value}
              className={option.value === value ? "quick-filter-option active" : "quick-filter-option"}
              key={option.value}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
              role="menuitemradio"
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function Empty({ label }: { label: string }) {
  return <div className="empty">{label}</div>;
}

function ToolbarTimeButton({
  busy,
  icon: Icon,
  label,
  onClick,
  time,
}: {
  busy: boolean;
  icon: LucideIcon;
  label: string;
  onClick: () => void;
  time: string | null;
}) {
  const timeZone = useDisplayTimeZone();
  const shortTime = time ? formatClockTime(time, timeZone) : "--:--";
  const fullTime = time ? formatFullDate(time, timeZone) : "暂无刷新记录";
  return (
    <button className="secondary-button toolbar-time-button" disabled={busy} onClick={onClick} title={`上次刷新时间 ${fullTime}`} type="button">
      <Icon className={busy ? "spin" : ""} size={17} />
      <span>{label}</span>
      <span className="toolbar-time">{shortTime}</span>
    </button>
  );
}

function SignalLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="signal-row">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function formatMoney(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  return `$${value.toFixed(2)}`;
}

function formatTokenCount(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  return Math.max(0, Math.round(value)).toLocaleString("zh-CN");
}

function formatTokenWindowLabel(window: UsageTokenHistory["windows"][number], timeZone: string) {
  const prefix = tokenWindowKindLabel(window.window_key);
  if (window.window_start_at && window.reset_at) {
    return `${prefix} ${formatShortDate(window.window_start_at, timeZone)} - ${formatShortDate(window.reset_at, timeZone)}`;
  }
  if (window.reset_at) {
    return `${prefix} 重置 ${formatShortDate(window.reset_at, timeZone)}`;
  }
  if (window.window_start_at) {
    return `${prefix} 始于 ${formatShortDate(window.window_start_at, timeZone)}`;
  }
  return `${prefix} ${window.window_reset_key.replace(/^reset_date:/, "").replace(/^observed_week:/, "").replace(/^observed_month:/, "")}`;
}

function tokenWindowKindLabel(windowKey: string) {
  return windowKey === "monthly" ? "月" : windowKey === "seven_day" ? "7d" : windowKey;
}

function formatTokenWindowSummary(window: UsageTokenHistory["windows"][number]) {
  const limit = formatTokenWindowLimit(window.estimated_limit);
  const spent = window.spent > 0 ? `已用 ${formatMoney(window.spent)}` : "";
  const tokens = window.tokens > 0 ? `${formatTokenCount(window.tokens)} tokens` : "";
  return [limit, spent, tokens].filter(Boolean).join(" · ") || "暂无记录";
}

function formatTokenWindowLimit(value: number | null | undefined) {
  return value !== null && value !== undefined && Number.isFinite(value) && value > 0 ? `总额 ${formatMoney(value)}` : "总额待采样";
}

function formatTokenHistoryTotalLabel(history: UsageTokenHistory) {
  if (!history.window_count) return "-";
  if (history.total_estimated_limit > 0) return `总额 ${formatMoney(history.total_estimated_limit)}`;
  if (history.total_spent > 0) return `已用 ${formatMoney(history.total_spent)}`;
  if (history.total_tokens > 0) return `${formatTokenCount(history.total_tokens)} tokens`;
  return "暂无用量";
}

function formatTokenHistoryCountLabel(history: UsageTokenHistory) {
  if (!history.window_count) return "暂无额度历史";
  const windows = new Set(history.windows.map((window) => tokenWindowKindLabel(window.window_key)));
  const kindLabel = windows.size ? [...windows].join("/") : "7d/月";
  return `共 ${history.window_count} 个 ${kindLabel} 窗口`;
}

function windowUsedAmount(window: UsageWindowEstimate) {
  return window.raw_spent ?? window.spent ?? window.estimate_spent ?? null;
}

function windowUsedLabel(window: UsageWindowEstimate) {
  return "已用";
}

function clampPercentValue(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }
  return Math.max(0, Math.min(value, 100));
}

function quotaMeterTone(value: number | null | undefined) {
  const normalized = clampPercentValue(value);
  if (normalized === null) return "ink";
  if (normalized <= 20) return "error";
  if (normalized <= 40) return "warn";
  return "ok";
}

function quotaUsedPercent(window: UsageWindowEstimate) {
  if (window.used_percent !== null) {
    return clampPercentValue(window.used_percent);
  }
  const used = windowUsedAmount(window);
  if (used !== null && window.estimated_limit !== null && window.estimated_limit > 0) {
    return clampPercentValue((used / window.estimated_limit) * 100);
  }
  if (window.remaining_percent !== null) {
    return clampPercentValue(100 - window.remaining_percent);
  }
  return null;
}

function formatAggregateMoney(aggregate: UsageWindowAggregate | null | undefined) {
  return aggregate?.remaining === null || aggregate?.remaining === undefined ? "-" : formatMoney(aggregate.remaining);
}

function formatProblemUnusedQuota(summary: ProblemUnusedQuotaSummary | null) {
  if (!summary) return "-";
  if (summary.accountCount === 0) return formatMoney(0);
  return formatAggregateMoney(summary.sevenDay);
}

function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  const normalized = Math.max(0, Math.min(value, 100));
  return `${Number.isInteger(normalized) ? normalized.toFixed(0) : normalized.toFixed(1)}%`;
}

function sampleThresholdSettingLabel(value: number | null | undefined) {
  return value && value > 0 ? formatPercent(value) : `默认 ${formatPercent(defaultUsageLimitSampleThresholdPercent)}`;
}

function formatWindowUsage(window: UsageWindowEstimate) {
  return `${formatPercent(quotaUsedPercent(window))} · ${windowUsedLabel(window)} ${formatMoney(windowUsedAmount(window))}`;
}

function formatWindowRemaining(window: UsageWindowEstimate) {
  if (window.remaining !== null) {
    return `${formatMoney(window.remaining)} · ${formatPercent(window.remaining_percent)}`;
  }
  if (window.estimated_limit !== null) {
    if (window.remaining_percent !== null) {
      return `${formatMoney(window.estimated_limit)} · ${formatPercent(window.remaining_percent)}`;
    }
    return formatMoney(window.estimated_limit);
  }
  if (!window.estimate_spent || window.estimate_spent <= 0) {
    return "暂无窗口用量";
  }
  if (!window.used_percent || window.used_percent <= 0) {
    return "-";
  }
  return "无法估算";
}

function formatWindowResetLine(defaultLabel: string, window: UsageWindowEstimate, now: number) {
  if (window.window_kind === "none" || window.source === "not_applicable") {
    return `${defaultLabel} 无独立限额`;
  }
  if (defaultLabel === "5h" && window.window_kind === "monthly") {
    return "5h 无独立限额";
  }
  const label = window.window_kind === "monthly" ? "月" : window.window_label || defaultLabel;
  const remaining = formatWindowRefreshTime(window, now);
  return `${label} ${remaining === "待查询" ? "-" : remaining}`;
}

function formatUsageWindowResetTitle(defaultLabel: string, window: UsageWindowEstimate, timeZone: string, now: number) {
  if (window.window_kind === "none" || window.source === "not_applicable") {
    return `${defaultLabel} 无独立限额`;
  }
  if (defaultLabel === "5h" && window.window_kind === "monthly") {
    return "5h 无独立限额";
  }
  const label = window.window_kind === "monthly" ? "月" : window.window_label || defaultLabel;
  return formatWindowRefreshTitle(label, window, timeZone, now);
}

function accountWindowRefreshTags(usage: AccountUsageEstimate | undefined, timeZone: string, now: number) {
  if (!usage) return [];

  const monthlyWindow = usage.seven_day.window_kind === "monthly" ? usage.seven_day : usage.five_hour.window_kind === "monthly" ? usage.five_hour : null;
  const monthlyOnly = Boolean(monthlyWindow && usage.five_hour.window_kind === "monthly" && usage.seven_day.window_kind === "monthly");
  const windows: Array<{ key: string; label: string; quotaWindow: UsageWindowEstimate }> = [];

  if (monthlyOnly && monthlyWindow) {
    windows.push({ key: "monthly", label: "月", quotaWindow: monthlyWindow });
  } else {
    if (usage.five_hour.window_kind !== "none" && usage.five_hour.window_kind !== "monthly" && usage.five_hour.source !== "not_applicable") {
      windows.push({ key: "five_hour", label: "5h", quotaWindow: usage.five_hour });
    }
    if (usage.seven_day.window_kind !== "none" && usage.seven_day.window_kind !== "monthly" && usage.seven_day.source !== "not_applicable") {
      windows.push({ key: "seven_day", label: "7d", quotaWindow: usage.seven_day });
    }
    if (!windows.length && monthlyWindow) {
      windows.push({ key: "monthly", label: "月", quotaWindow: monthlyWindow });
    }
  }

  return windows.map(({ key, label, quotaWindow }) => ({
    key,
    label,
    time: formatWindowRefreshTime(quotaWindow, now),
    title: formatWindowRefreshTitle(label, quotaWindow, timeZone, now),
  }));
}

function formatWindowRefreshTime(window: UsageWindowEstimate, now: number) {
  const resetRemainingSeconds = windowResetRemainingSeconds(window, now);
  if (resetRemainingSeconds !== null) return formatRemainingDuration(resetRemainingSeconds);
  if (window.remaining_seconds !== null && window.remaining_seconds !== undefined) return formatRemainingDuration(window.remaining_seconds);
  return "待查询";
}

function formatWindowRefreshTitle(label: string, window: UsageWindowEstimate, timeZone: string, now: number) {
  const refreshTime = formatWindowRefreshTime(window, now);
  if (window.reset_at) {
    const resetLine = `刷新时间 ${formatFullDate(window.reset_at, timeZone)}`;
    return refreshTime === "已到刷新" ? `${label} 已到刷新时间，${resetLine}` : `${label} 剩余 ${refreshTime}，${resetLine}`;
  }
  if (window.remaining_seconds !== null && window.remaining_seconds !== undefined) {
    return refreshTime === "已到刷新" ? `${label} 已到刷新时间` : `${label} 约 ${refreshTime} 后刷新`;
  }
  return `${label} 刷新时间待查询`;
}

function titleFor(view: View) {
  return {
    overview: "调度恢复概览",
    accounts: "GPT 账号状态",
    usage: "额度估算",
    "usage-samples": "额度样本",
    mailboxes: "验证码邮箱",
    phones: "手机号管理",
    history: "历史记录",
    settings: "运行设置",
  }[view];
}

function downloadTextFile(fileName: string, content: string) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function copyTextToClipboard(value: string) {
  if (window.navigator.clipboard?.writeText) {
    try {
      await window.navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall back for local HTTP or browsers that block the Clipboard API.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("复制失败");
}

function sourceLabel(source: string) {
  return (
    {
      env: ".env",
      auto: "自动扫描",
      manual: "手动设置",
    }[source] || source
  );
}

function periodLabel(period: string) {
  return (
    {
      monthly: "月付",
      month: "月付",
      yearly: "年付",
      annual: "年付",
      annually: "年付",
      year: "年付",
      weekly: "周付",
      week: "周付",
    }[period.toLowerCase()] || period
  );
}

function planLabel(plan: string) {
  const normalized = plan.trim().toLowerCase();
  if (normalized === "active") return "active";
  const subscriptionType = normalizeSubscriptionType(plan);
  return subscriptionType === "unknown" ? plan : subscriptionTypeLabel(subscriptionType);
}

function appSettingsEqual(left: AppSettings, right: AppSettings) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function normalizeSubscriptionType(value: string | null | undefined) {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return "unknown";
  const compact = text.replace(/[^a-z0-9]+/g, "");
  const aliases: Record<string, string> = {
    chatgptplusplan: "plus",
    chatgptplus: "plus",
    plus: "plus",
    chatgptteamplan: "team",
    chatgptteam: "team",
    chatgptbusinessplan: "team",
    business: "team",
    team: "team",
    chatgptproplan: "pro",
    chatgptpro: "pro",
    pro: "pro",
    chatgptfreeplan: "free",
    chatgptfree: "free",
    free: "free",
    chatgptk12plan: "k12",
    chatgptk12: "k12",
    k12plan: "k12",
    k12: "k12",
  };
  if (aliases[compact]) return aliases[compact];
  if (compact.includes("k12")) return "k12";
  const candidate = text.replace(/^chatgpt[^a-z0-9]*/, "").replace(/[^a-z0-9]*plan$/, "");
  const candidateCompact = candidate.replace(/[^a-z0-9]+/g, "");
  if (aliases[candidateCompact]) return aliases[candidateCompact];
  const slug = candidate.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
  return !slug || ["active", "inactive", "none", "null", "oauth", "openai", "subscription", "plan", "unknown"].includes(slug)
    ? "unknown"
    : slug;
}

function subscriptionTypeLabel(value: string) {
  const normalized = normalizeSubscriptionType(value);
  const known: Record<string, string> = {
    plus: "Plus",
    team: "Team",
    pro: "Pro",
    free: "Free",
    k12: "K12",
    unknown: "未知",
  };
  return (
    known[normalized] ||
    normalized
      .split("-")
      .map((part) => (/\d/.test(part) ? part.toUpperCase() : `${part.charAt(0).toUpperCase()}${part.slice(1)}`))
      .join(" ")
  );
}

function subscriptionTypeSortRank(value: string) {
  return ({ plus: 10, team: 20, pro: 30, free: 40, k12: 50, unknown: 100 } as Record<string, number>)[
    normalizeSubscriptionType(value)
  ] || 80;
}

function cloneUsageLimitPlanRanges(value: UsageLimitPlanRanges): UsageLimitPlanRanges {
  return {
    five_hour: { ...value.five_hour },
    seven_day: { ...value.seven_day },
    monthly: { ...value.monthly },
  };
}

function mergeUsageLimitDefaultRanges(value: UsageLimitDefaultRanges, detectedTypes: string[]): UsageLimitDefaultRanges {
  const merged = Object.fromEntries(
    Object.entries({ ...defaultUsageLimitRanges, ...(value || {}) }).map(([subscriptionType, ranges]) => [
      normalizeSubscriptionType(subscriptionType),
      cloneUsageLimitPlanRanges(ranges),
    ]),
  ) as UsageLimitDefaultRanges;
  const fallback = merged.unknown || cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges);
  for (const rawType of detectedTypes) {
    const subscriptionType = normalizeSubscriptionType(rawType);
    if (subscriptionType !== "unknown" && !merged[subscriptionType]) {
      merged[subscriptionType] = cloneUsageLimitPlanRanges(fallback);
    }
  }
  merged.unknown = cloneUsageLimitPlanRanges(fallback);
  return merged;
}

function usageLimitWindowLabel(windowKey: (typeof usageLimitWindowKeys)[number]) {
  return windowKey === "five_hour" ? "5h" : windowKey === "seven_day" ? "7d" : "月";
}

function toSub2ApiInstanceUrl(value: string) {
  const text = value.trim().replace(/\/+$/, "");
  if (!text) return "";
  return text.toLowerCase().endsWith(sub2ApiApiPrefix) ? text.slice(0, -sub2ApiApiPrefix.length).replace(/\/+$/, "") : text;
}

function isSub2ApiInstanceUrl(value: string) {
  const instanceUrl = toSub2ApiInstanceUrl(value);
  if (!instanceUrl) return false;
  const candidate = instanceUrl.includes("://") ? instanceUrl : `http://${instanceUrl}`;
  try {
    return Boolean(new URL(candidate).hostname);
  } catch {
    return false;
  }
}

function accountDisplayCounts(accounts: Account[]): AccountCounts {
  const actual = accounts.length;
  const deduped = new Set(accounts.map((account) => account.email.toLowerCase())).size;
  return { actual, deduped, duplicates: Math.max(actual - deduped, 0) };
}

function accountCompare(left: Account, right: Account) {
  const emailCompare = left.email.localeCompare(right.email);
  if (emailCompare !== 0) return emailCompare;
  if (left.duplicate_rank !== right.duplicate_rank) return left.duplicate_rank - right.duplicate_rank;
  return (left.sub2api_account_id || "").localeCompare(right.sub2api_account_id || "");
}

function accountRowKey(account: Account) {
  return `${account.email}:${account.sub2api_account_id || account.id}:${account.duplicate_rank}`;
}

function accountRowDomId(account: Account) {
  return `account-row-${account.id}`;
}

function findAccountJumpTarget(accounts: Account[], target: AccountJumpTarget) {
  const targetAccountId = normalizeSearch(target.sub2apiAccountId);
  const targetEmail = normalizeSearch(target.email);

  if (targetAccountId) {
    const accountById = accounts.find((account) => normalizeSearch(account.sub2api_account_id) === targetAccountId);
    if (accountById) return accountById;
  }

  if (targetEmail) {
    return accounts.find((account) => normalizeSearch(account.email) === targetEmail) || null;
  }

  return null;
}

function accountJumpSearchText(account: Account | null, target: AccountJumpTarget) {
  return account?.sub2api_account_id || account?.email || target.sub2apiAccountId?.trim() || target.email?.trim() || "";
}

function usageForAccount(
  account: Pick<Account, "email" | "sub2api_account_id">,
  usageByAccountId: Map<string, AccountUsageEstimate>,
  usageByEmail: Map<string, AccountUsageEstimate>,
) {
  const accountId = account.sub2api_account_id?.trim();
  if (accountId) {
    return usageByAccountId.get(accountId);
  }
  return usageByEmail.get(account.email.toLowerCase());
}

function rateLimitedAccountsForWindow(accounts: Account[], window: string) {
  return accounts
    .filter((account) => !accountHasError(account) && accountRateLimitedWindowKeys(account).includes(window))
    .sort(accountCompare);
}

function textMatchesSearch(values: Array<string | number | boolean | null | undefined>, search: string) {
  const term = normalizeSearch(search);
  if (!term) return true;
  return values.some((value) => normalizeSearch(value).includes(term));
}

function normalizeSearch(value: string | number | boolean | null | undefined) {
  return String(value ?? "")
    .trim()
    .toLowerCase();
}

function mailVerificationCode(message: MailMessage) {
  const code = String(message.code || "").trim();
  return code || extractVerificationCode(message.subject, message.body_preview);
}

function extractVerificationCode(...texts: Array<string | null | undefined>) {
  for (const text of texts) {
    const match = text?.match(/\b(\d{6})\b/);
    if (match) return match[1];
  }
  return null;
}

function isDeactivatedAccount(account: Account) {
  if (account.deactive) return true;
  const text = `${account.status || ""} ${account.last_error || ""}`.toLowerCase();
  return text.includes("deactive") || text.includes("deactivated") || text.includes("封禁") || text.includes("账号已停用") || text.includes("账户已停用");
}

function isManualErrorProtectedAccount(account: Account) {
  return account.delete_unlockable;
}

function isManualErrorDeletionCandidate(account: Account) {
  return account.delete_unlockable;
}

function deleteAccountKindLabel(account: Account) {
  if (isDeactivatedAccount(account)) return "封禁账号";
  if (isManualErrorDeletionCandidate(account)) return "普通错误账号";
  if (account.remote_error) return "重复异常账号";
  return "重复账号";
}

function accountRateLimited(account: Account | AccountUsageEstimate, usage?: AccountUsageEstimate) {
  return Boolean(account.rate_limited || usage?.rate_limited || usage?.five_hour.rate_limited || usage?.seven_day.rate_limited);
}

function accountShowsRateLimit(account: Account, usage?: AccountUsageEstimate) {
  if (accountEstimateExcludedByError(account, usage)) return false;
  return accountRateLimited(account, usage);
}

function accountEstimateExcludedByError(account: Account, usage?: AccountUsageEstimate) {
  return Boolean(accountHasError(account) || usage?.error);
}

function accountIsManuallyPaused(
  account: Pick<Account, "deactive" | "schedulable" | "status" | "remote_error" | "rate_limited"> | Pick<AccountUsageEstimate, "deactive" | "schedulable" | "status" | "rate_limited">,
  usage?: AccountUsageEstimate,
) {
  const remoteError = "remote_error" in account ? account.remote_error : false;
  if (account.deactive || remoteError || accountRateLimited(account as Account, usage)) return false;
  const status = String(account.status || "").trim().toLowerCase();
  return account.schedulable === false && status === "active";
}

function accountStatusTone(account: Account, usage?: AccountUsageEstimate) {
  if (account.deactive) return "deactive";
  if (account.refreshing) return "running";
  if (accountHasError(account)) return "error";
  if (accountIsManuallyPaused(account, usage)) return "ink";
  return "ok";
}

function accountStatusLabel(account: Account, usage?: AccountUsageEstimate) {
  if (account.deactive) return "封禁";
  if (account.refreshing) return "刷新中";
  if (accountHasError(account)) return "错误";
  if (accountIsManuallyPaused(account, usage)) return "主动暂停";
  if ((account.status || "").trim().toLowerCase() === "active") return "正常";
  return account.status || "未知";
}

function accountMatchesStatusFilter(account: Account, usage: AccountUsageEstimate | undefined, filter: AccountStatusFilter) {
  switch (filter) {
    case "normal":
      return accountStatusLabel(account, usage) === "正常";
    case "normal-no-rate-limit":
      return accountStatusLabel(account, usage) === "正常" && !accountShowsRateLimit(account, usage);
    case "five-hour-rate-limited":
      return !account.deactive && !accountHasError(account) && accountRateLimitedWindowKeys(account, usage).includes("five_hour");
    case "seven-day-rate-limited":
      return !account.deactive && !accountHasError(account) && accountRateLimitedWindowKeys(account, usage).includes("seven_day");
    case "monthly-rate-limited":
      return !account.deactive && !accountHasError(account) && accountRateLimitedWindowKeys(account, usage).includes("monthly");
    case "error":
      return !account.deactive && accountHasError(account);
    case "deactive":
      return account.deactive;
    default:
      return true;
  }
}

function availableAccountStatusFilterOptions(
  accounts: Account[],
  usageByAccountId: Map<string, AccountUsageEstimate>,
  usageByEmail: Map<string, AccountUsageEstimate>,
): Array<QuickFilterOption<AccountStatusFilter>> {
  return allAccountStatusFilterOptions.filter((option) => {
    if (option.value === "all") return true;
    return accounts.some((account) => accountMatchesStatusFilter(account, usageForAccount(account, usageByAccountId, usageByEmail), option.value));
  });
}

function availableAccountSubscriptionFilterOptions(accounts: Account[]): Array<QuickFilterOption<string>> {
  const labels = new Set<string>();
  for (const account of accounts) {
    labels.add(accountSubscriptionTypeLabel(account));
  }
  const options = [...labels]
    .sort((left, right) => usageSubscriptionSortRank(left) - usageSubscriptionSortRank(right) || left.localeCompare(right))
    .map((label) => ({ value: label, label }));
  return [{ value: "", label: "全部订阅" }, ...options];
}

function accountSubscriptionTypeLabel(
  account: Pick<
    Account,
    "account_type" | "has_active_subscription" | "platform" | "subscription_label" | "subscription_plan" | "subscription_type"
  >,
) {
  const plan = account.subscription_type
    ? subscriptionTypeLabel(account.subscription_type)
    : account.subscription_label || planLabel(account.subscription_plan || account.account_type || account.platform || "未知");
  return account.has_active_subscription === false ? "订阅无效" : plan === "active" ? "正常" : plan;
}

function accountScheduleTone(account: Account, usage?: AccountUsageEstimate) {
  if (account.deactive) return "deactive";
  if (accountHasError(account)) return "error";
  if (accountShowsRateLimit(account, usage)) return "warn";
  if (accountIsManuallyPaused(account, usage)) return "ink";
  if (account.schedulable === true) return "ok";
  return "ink";
}

function accountScheduleLabel(account: Account, usage?: AccountUsageEstimate) {
  if (account.deactive) return "封禁";
  if (accountHasError(account)) return "错误暂停";
  if (accountShowsRateLimit(account, usage)) return "限流暂停";
  if (accountIsManuallyPaused(account, usage)) return "主动暂停";
  if (account.schedulable === true) return "可调度";
  if (account.schedulable === false) return "暂停";
  return "未知";
}

function accountHasError(account: Pick<Account, "remote_error" | "last_error">) {
  const status = "status" in account ? String(account.status || "").trim().toLowerCase() : "";
  const schedulable = "schedulable" in account ? account.schedulable : null;
  const statusLooksError =
    status.includes("error") ||
    status.includes("fail") ||
    status.includes("invalid") ||
    status.includes("expired") ||
    status.includes("disabled") ||
    (schedulable === false && status !== "active");
  return Boolean(account.remote_error || account.last_error || statusLooksError);
}

function accountErrorSummary(account: Pick<Account, "remote_error" | "last_error">) {
  const detail = String(account.last_error || "").trim();
  const text = detail.toLowerCase();
  if (!detail && account.remote_error) {
    return { label: "远端异常", tone: "error" };
  }
  if (!detail) {
    return null;
  }
  if (text.includes("too many phone verification requests") || text.includes("rate_limit_exceeded")) {
    return { label: "手机号风控", tone: "warn" };
  }
  if (text.includes("接码链接不存在") || text.includes("sms url is unavailable") || text.includes("sms url has expired") || text.includes("接码链接") && text.includes("不可用")) {
    return { label: "接码失效", tone: "error" };
  }
  if (text.includes("手机号库中找到")) {
    return { label: "号码未入库", tone: "warn" };
  }
  if (text.includes("可绑定账号上限") || text.includes("maximum number of accounts")) {
    return { label: "号码绑定超限", tone: "warn" };
  }
  if (text.includes("手动完成验证") || text.includes("manual verification") || text.includes("cdk/手动接码源")) {
    return { label: "手动验证", tone: "violet" };
  }
  if (text.includes("otp 应用验证") || text.includes("authenticator app") || text.includes("totp") || text.includes("one-time password application")) {
    return { label: "OTP 应用验证", tone: "violet" };
  }
  if (text.includes("token_revoked")) {
    return { label: "令牌撤销", tone: "error" };
  }
  if (text.includes("unauthorized") || text.includes("authentication failed (401)")) {
    return { label: "401 鉴权失败", tone: "error" };
  }
  if (text.includes("service restarted before refresh finished")) {
    return { label: "刷新中断", tone: "ink" };
  }
  if (text.includes("session endpoint did not include accesstoken")) {
    return { label: "会话缺令牌", tone: "error" };
  }
  if (text.includes("sub2api request failed: http 502") || text.includes("origin_bad")) {
    return { label: "远端 502", tone: "warn" };
  }
  if (account.remote_error) {
    return { label: "远端异常", tone: "error" };
  }
  return { label: "查看错误", tone: "ink" };
}

function phoneSmsSummary(phone: PhoneNumber, timeZone: string) {
  const status = String(phone.sms_status || "").trim().toLowerCase();
  const error = String(phone.sms_error || "").trim();
  const checkedAt = phone.sms_checked_at ? formatDate(phone.sms_checked_at, timeZone) : "未检查";
  if (isManualPhoneSource(phone.sms_url, phone.sms_cdk) || status === "manual") {
    return { label: "手动", tone: "violet", detail: phone.sms_cdk ? `CDK: ${phone.sms_cdk}` : error || checkedAt };
  }
  if (!status) {
    return { label: "未检查", tone: "ink", detail: checkedAt };
  }
  if (status === "ok") {
    return { label: "可用", tone: "ok", detail: checkedAt };
  }
  const loweredError = error.toLowerCase();
  if (error.includes("不可用") || loweredError.includes("sms url is unavailable")) {
    return { label: "不可用", tone: "error", detail: error || checkedAt };
  }
  if (error.includes("已过期") || loweredError.includes("expired")) {
    return { label: "已过期", tone: "error", detail: error || checkedAt };
  }
  if (status === "not_found") {
    return { label: "可访问", tone: "info", detail: error || checkedAt };
  }
  return { label: "检查失败", tone: "warn", detail: error || checkedAt };
}

function isPhoneUrlSource(value: string | null | undefined) {
  return /^https?:\/\//i.test(String(value || "").trim());
}

function isManualPhoneSource(value: string | null | undefined, cdk?: string | null | undefined) {
  return Boolean(String(cdk || "").trim()) || /^cdk:/i.test(String(value || "").trim());
}

function phoneSourceLabel(value: string | null | undefined) {
  const text = String(value || "").trim();
  if (!text) return "";
  if (text === "接码链接不存在") return "";
  if (isManualPhoneSource(text)) return `CDK: ${text.replace(/^cdk:/i, "").trim()}`;
  return text;
}

function primaryPhoneSourceText(smsUrl: string | null | undefined, smsCdk: string | null | undefined) {
  if (smsCdk) return "手动处理";
  return phoneSourceLabel(smsUrl);
}

function phoneSourceTags(smsUrl: string | null | undefined, smsCdk: string | null | undefined, smsRechargeUrl: string | null | undefined) {
  const tags: string[] = [];
  if (smsCdk) tags.push("手动接码");
  if (!smsCdk && !smsRechargeUrl && !isPhoneUrlSource(smsUrl) && phoneSourceLabel(smsUrl)) {
    tags.push(phoneSourceLabel(smsUrl));
  }
  return tags;
}

function accountUsageEstimateToggleLabel(account: Account, usage?: AccountUsageEstimate) {
  if (account.deactive) return "封禁排除";
  if (accountEstimateExcludedByError(account, usage)) return "错误排除";
  if (!account.usage_estimate_enabled) return "排除";
  if (accountShowsRateLimit(account, usage)) return "限流排除";
  return "参与";
}

function accountUsageEstimateToggleTitle(account: Account, usage?: AccountUsageEstimate) {
  if (account.deactive) return "封禁账号不参与总额度估算";
  if (accountEstimateExcludedByError(account, usage)) return "错误状态账号不参与总额度估算";
  if (!account.usage_estimate_enabled) return "不参与总额度估算";
  if (accountShowsRateLimit(account, usage)) return "账号当前限流，暂不参与总额度估算";
  return "参与总额度估算";
}

function usageEstimateParticipationLabel(
  account: Pick<AccountUsageEstimate, "usage_estimate_enabled" | "error" | "rate_limited" | "schedulable" | "status" | "deactive">,
  includePausedAccounts = true,
) {
  if (account.deactive) return "封禁排除";
  if (account.error) return "错误排除";
  if (!account.usage_estimate_enabled) return "排除";
  if (account.rate_limited) return "限流排除";
  if (!includePausedAccounts && accountIsManuallyPaused(account)) return "主动暂停排除";
  return "参与";
}

function usageEstimateParticipationTone(
  account: Pick<AccountUsageEstimate, "usage_estimate_enabled" | "error" | "rate_limited" | "schedulable" | "status" | "deactive">,
  includePausedAccounts = true,
) {
  if (account.deactive) return "deactive";
  if (account.error) return "error";
  if (!account.usage_estimate_enabled) return "ink";
  if (account.rate_limited) return "warn";
  if (!includePausedAccounts && accountIsManuallyPaused(account)) return "ink";
  return "ok";
}

function accountRateLimitedWindowsLabel(account: Account | AccountUsageEstimate, usage?: AccountUsageEstimate) {
  return accountDisplayRateLimitedWindowKeys(account, usage).map((window) => rateLimitedWindowLabel(window, usage)).join("/");
}

function accountRateLimitDetails(account: Account, usage: AccountUsageEstimate | undefined, timeZone: string, now: number) {
  const windows = accountRateLimitedWindowKeys(account, usage);
  return rateLimitDetailsForWindows(windows, usage, timeZone, now);
}

function accountDisplayRateLimitDetails(account: Account, usage: AccountUsageEstimate | undefined, timeZone: string, now: number) {
  const windows = accountDisplayRateLimitedWindowKeys(account, usage);
  return rateLimitDetailsForWindows(windows, usage, timeZone, now);
}

function rateLimitDetailsForWindows(windows: string[], usage: AccountUsageEstimate | undefined, timeZone: string, now: number) {
  if (!windows.length) {
    return [];
  }
  return windows.map((window) => ({
    key: window,
    label: rateLimitedWindowLabel(window, usage),
    recovery: formatRateLimitRecovery(window, usage, timeZone, now),
    tone: rateLimitedWindowTone(window),
  }));
}

function accountDisplayRateLimitedWindowKeys(account: Account | AccountUsageEstimate, usage?: AccountUsageEstimate) {
  const windows: string[] = [];
  for (const window of accountRateLimitedWindowKeys(account, usage)) {
    const displayWindow = rateLimitedWindowLabel(window, usage) === "月" ? "monthly" : window;
    if (!windows.includes(displayWindow)) {
      windows.push(displayWindow);
    }
  }
  return windows;
}

function accountRateLimitedWindowKeys(account: Account | AccountUsageEstimate, usage?: AccountUsageEstimate) {
  const windows = new Set<string>();
  (account.rate_limited_windows || []).forEach((window) => windows.add(normalizeRateLimitedWindowKey(window, usage)));
  usage?.rate_limited_windows?.forEach((window) => windows.add(normalizeRateLimitedWindowKey(window, usage)));
  if (usage?.five_hour.rate_limited) windows.add(normalizeRateLimitedWindowKey("five_hour", usage));
  if (usage?.seven_day.rate_limited) windows.add(normalizeRateLimitedWindowKey("seven_day", usage));
  return [...windows];
}

function normalizeRateLimitedWindowKey(window: string, usage?: AccountUsageEstimate) {
  if (window === "five_hour" && usage?.five_hour.window_kind === "monthly") {
    return "monthly";
  }
  if (window === "seven_day" && usage?.seven_day.window_kind === "monthly") {
    return "monthly";
  }
  return window;
}

function rateLimitedWindowLabel(window: string, usage?: AccountUsageEstimate) {
  if (window === "five_hour" && usage?.five_hour.window_kind === "monthly") {
    return "月";
  }
  if (window === "seven_day" && usage?.seven_day.window_kind === "monthly") {
    return "月";
  }
  return (
    {
      five_hour: "5h",
      seven_day: "7d",
      monthly: "月",
    }[window] || window
  );
}

function rateLimitedWindowTone(window: string) {
  if (window === "monthly") return "warn";
  return window === "five_hour" ? "info" : "violet";
}

function formatRateLimitRecovery(window: string, usage: AccountUsageEstimate | undefined, timeZone: string, now: number) {
  const quotaWindow =
    window === "five_hour"
      ? usage?.five_hour
      : window === "seven_day"
        ? usage?.seven_day
        : window === "monthly" && usage?.seven_day.window_kind === "monthly"
          ? usage.seven_day
          : window === "monthly" && usage?.five_hour.window_kind === "monthly"
            ? usage.five_hour
            : undefined;
  if (!quotaWindow) return "待查询";
  const resetRemainingSeconds = windowResetRemainingSeconds(quotaWindow, now);
  if (resetRemainingSeconds !== null) {
    return formatRemainingDuration(resetRemainingSeconds);
  }
  if (quotaWindow.remaining_seconds !== null && quotaWindow.remaining_seconds !== undefined) {
    return formatRemainingDuration(quotaWindow.remaining_seconds);
  }
  if (quotaWindow.reset_at) return formatDate(quotaWindow.reset_at, timeZone);
  return "待查询";
}

function windowResetRemainingSeconds(window: UsageWindowEstimate, now: number) {
  if (!window.reset_at) return null;
  const resetAt = parseApiDate(window.reset_at);
  const seconds = Math.ceil((resetAt.getTime() - now) / 1000);
  return Number.isFinite(seconds) ? Math.max(0, seconds) : null;
}

function formatRemainingDuration(seconds: number) {
  if (!Number.isFinite(seconds)) return "待查询";
  if (seconds <= 0) return "已到刷新";
  return formatDuration(seconds);
}

function formatDuration(seconds: number) {
  const totalMinutes = Math.max(1, Math.ceil(seconds / 60));
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) {
    return `${days}天${hours > 0 ? `${hours}小时` : ""}`;
  }
  if (hours > 0) {
    return `${hours}小时${minutes > 0 ? `${minutes}分钟` : ""}`;
  }
  return `${minutes}分钟`;
}

function buildDisplayedUsageEstimate(estimate: UsageEstimate, includePausedAccounts: boolean): UsageEstimate {
  const activeAccounts = estimate.accounts.filter(
    (account) => usageDetailAccountVisible(account) && (includePausedAccounts || !accountIsManuallyPaused(account)),
  );
  const groups = estimate.groups
    .map((group) => {
      const rows = activeAccounts.filter((account) => account.groups.some((item) => item.id === group.group_id));
      return {
        ...group,
        account_count: rows.length,
        five_hour: aggregateUsageAccountsWindow(rows, "five_hour"),
        seven_day: aggregateUsageAccountsWindow(rows, "seven_day"),
      };
    })
    .filter((group) => group.account_count > 0);

  return {
    ...estimate,
    overall: {
      ...estimate.overall,
      account_count: activeAccounts.length,
      five_hour: aggregateUsageAccountsWindow(activeAccounts, "five_hour"),
      seven_day: aggregateUsageAccountsWindow(activeAccounts, "seven_day"),
    },
    groups,
    accounts: activeAccounts,
  };
}

function usageDetailAccountVisible(account: AccountUsageEstimate) {
  return !account.deactive && !account.error && !account.usage_error;
}

function usageProblemAccount(account: AccountUsageEstimate) {
  return account.deactive || account.error;
}

function usageDetailAccountRateLimited(account: AccountUsageEstimate) {
  return Boolean(account.rate_limited || account.five_hour.rate_limited || account.seven_day.rate_limited);
}

function accountMatchesUsageDetailFilter(account: AccountUsageEstimate, filter: UsageDetailAccountFilter) {
  if (!usageDetailAccountVisible(account)) return false;
  const rateLimited = usageDetailAccountRateLimited(account);
  return filter === "rate-limited" ? rateLimited : !rateLimited;
}

function usageDetailAccountCounts(accounts: AccountUsageEstimate[]) {
  let normal = 0;
  let rateLimited = 0;

  for (const account of accounts) {
    if (!usageDetailAccountVisible(account)) continue;
    if (usageDetailAccountRateLimited(account)) {
      rateLimited += 1;
    } else {
      normal += 1;
    }
  }

  return { normal, rateLimited };
}

function usageEstimateHeaderStats(estimate: UsageEstimate, includePausedAccounts: boolean) {
  let availableCount = 0;
  let rateLimitedCount = 0;

  for (const account of estimate.accounts) {
    if (!usageDetailAccountVisible(account)) continue;
    if (usageDetailAccountRateLimited(account)) {
      rateLimitedCount += 1;
      continue;
    }
    if (!includePausedAccounts && accountIsManuallyPaused(account)) {
      continue;
    }
    availableCount += 1;
  }

  return {
    accountCount: estimate.overall.account_count,
    availableCount,
    rateLimitedCount,
  };
}

function aggregateUsageAccountsWindow(accounts: AccountUsageEstimate[], windowKey: "five_hour" | "seven_day"): UsageWindowAggregate {
  let spent = 0;
  let limit = 0;
  let remaining = 0;
  let estimatedSpent = 0;
  let enabledAccountCount = 0;
  let estimableAccounts = 0;

  for (const account of accounts) {
    if (!usageDetailAccountVisible(account)) continue;
    if (!account.usage_estimate_enabled) continue;
    if (usageDetailAccountRateLimited(account)) continue;
    const window = aggregateSourceWindow(account, windowKey);
    if (window.window_kind === "none") continue;
    enabledAccountCount += 1;
    if (window.estimate_spent !== null) {
      spent += window.estimate_spent;
    }
    if (window.estimated_limit === null || window.remaining === null) {
      continue;
    }
    estimableAccounts += 1;
    estimatedSpent += window.estimate_spent ?? 0;
    limit += window.estimated_limit;
    remaining += window.remaining;
  }

  return {
    spent,
    estimated_limit: estimableAccounts ? limit : null,
    remaining: estimableAccounts ? remaining : null,
    remaining_percent: estimableAccounts && limit > 0 ? clampPercentValue((remaining / limit) * 100) : null,
    used_percent: estimableAccounts && limit > 0 ? clampPercentValue((estimatedSpent / limit) * 100) : null,
    account_count: accounts.length,
    enabled_account_count: enabledAccountCount,
    estimable_accounts: estimableAccounts,
  };
}

function usageProblemAccountUnusedQuota(accounts: AccountUsageEstimate[]): ProblemUnusedQuotaSummary {
  const problemAccounts = accounts.filter(usageProblemAccount);
  return {
    accountCount: problemAccounts.length,
    fiveHour: aggregateProblemUsageAccountsWindow(problemAccounts, "five_hour"),
    sevenDay: aggregateProblemUsageAccountsWindow(problemAccounts, "seven_day"),
  };
}

function aggregateProblemUsageAccountsWindow(accounts: AccountUsageEstimate[], windowKey: "five_hour" | "seven_day"): UsageWindowAggregate {
  let spent = 0;
  let limit = 0;
  let remaining = 0;
  let estimatedSpent = 0;
  let windowAccountCount = 0;
  let estimableAccounts = 0;

  for (const account of accounts) {
    const window = aggregateSourceWindow(account, windowKey);
    if (window.window_kind === "none") continue;
    windowAccountCount += 1;
    if (window.estimate_spent !== null) {
      spent += window.estimate_spent;
    }
    if (window.estimated_limit === null || window.remaining === null) {
      continue;
    }
    estimableAccounts += 1;
    estimatedSpent += window.estimate_spent ?? 0;
    limit += window.estimated_limit;
    remaining += window.remaining;
  }

  return {
    spent,
    estimated_limit: estimableAccounts ? limit : null,
    remaining: estimableAccounts ? remaining : null,
    remaining_percent: estimableAccounts && limit > 0 ? clampPercentValue((remaining / limit) * 100) : null,
    used_percent: estimableAccounts && limit > 0 ? clampPercentValue((estimatedSpent / limit) * 100) : null,
    account_count: accounts.length,
    enabled_account_count: windowAccountCount,
    estimable_accounts: estimableAccounts,
  };
}

function aggregateSourceWindow(account: AccountUsageEstimate, windowKey: "five_hour" | "seven_day") {
  const window = account[windowKey];
  if (windowKey === "five_hour" && window.window_kind === "none" && account.seven_day.window_kind === "monthly") {
    return account.seven_day;
  }
  return window;
}

function canBulkDeleteProblemAccount(account: Account) {
  return isDeactivatedAccount(account) || (account.can_delete_remote && account.is_duplicate);
}

function accountCanBeSelectedForDeletion(account: Account) {
  return Boolean(account.sub2api_account_id || account.id > 0);
}

function selectedAccountDeleteItem(account: Account): SelectedAccountDeleteItem {
  return {
    sub2api_account_id: account.sub2api_account_id,
    snapshot_id: account.id > 0 ? account.id : null,
  };
}

function latestEventByKinds(events: AppEvent[], kinds: string[]) {
  return events.find((event) => kinds.includes(event.kind)) || null;
}

function useDisplayTimeZone() {
  return useContext(TimeZoneContext);
}

function useNow() {
  return useContext(NowContext);
}

function useRefreshClock() {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), refreshClockIntervalMs);
    return () => window.clearInterval(timer);
  }, []);

  return now;
}

function statusLabel(status: string) {
  return (
    {
      queued: "排队",
      running: "运行中",
      succeeded: "成功",
      skipped: "已跳过",
      failed: "失败",
      deactive: "封禁",
    }[status] || status
  );
}

function latestErrorRefreshJobForRecord(record: AccountExceptionRecord, jobs: RefreshJob[]) {
  const accountId = normalizeHistoryAccountId(record.sub2api_account_id);
  if (accountId) {
    for (const job of jobs) {
      if (!refreshJobHasErrorReason(job)) continue;
      if (normalizeHistoryAccountId(job.sub2api_account_id) === accountId) {
        return job;
      }
    }
  }

  const email = normalizeHistoryEmail(record.email);
  if (email) {
    for (const job of jobs) {
      if (!refreshJobHasErrorReason(job)) continue;
      if (normalizeHistoryEmail(job.email) === email) {
        return job;
      }
    }
  }

  return null;
}

function refreshJobHasErrorReason(job: RefreshJob) {
  return Boolean(job.reason && (job.status === "failed" || job.status === "deactive"));
}

function refreshJobProtocolSummary(job: RefreshJob, events: AppEvent[]) {
  const relatedEvents = events
    .filter((event) => parseEventJobId(event) === job.id)
    .filter((event) => Boolean(protocolEventSummary(event)))
    .reverse();

  if (!relatedEvents.length) return null;

  const summaries = relatedEvents
    .map((event) => protocolEventSummary(event))
    .filter((summary, index, list): summary is string => Boolean(summary) && list.indexOf(summary) === index);

  return summaries.length ? summaries.join(" -> ") : null;
}

function parseEventJobId(event: AppEvent) {
  const value = event.details?.job_id;
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string") {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) return Math.trunc(parsed);
  }
  return null;
}

function protocolEventSummary(event: AppEvent) {
  switch (event.kind) {
    case "sub2api_protocol_refresh_failed":
      return protocolErrorHttpSummary(event, "/refresh", "sub2api /refresh 失败");
    case "sub2api_check_status_unavailable":
      return "sub2api /check-status 无结果";
    case "runtime_access_token_missing":
      return "只有 AT 标记，缺少真实 access_token";
    case "access_token_status_check_failed":
      return protocolErrorHttpSummary(event, "access_token 状态检查", "access_token 状态检查失败");
    case "chatgpt_protocol_refresh_failed":
      return protocolErrorHttpSummary(event, "ChatGPT 协议刷新", "ChatGPT 协议刷新失败");
    default:
      return null;
  }
}

function protocolErrorHttpSummary(event: AppEvent, fallbackLabel: string, defaultLabel: string) {
  const errorText = String(event.details?.error || "");
  const match = errorText.match(/\bHTTP\s+(\d{3})\b/i);
  if (match) {
    return `${fallbackLabel} 返回 ${match[1]}`;
  }
  return defaultLabel;
}

function normalizeHistoryEmail(value: string | null | undefined) {
  const normalized = String(value || "").trim().toLowerCase();
  return normalized || null;
}

function normalizeHistoryAccountId(value: string | null | undefined) {
  const normalized = String(value || "").trim();
  return normalized || null;
}

function exceptionSourceLabel(source: string) {
  return (
    {
      sync: "当前账号",
      refresh: "刷新任务",
      usage_refresh: "额度刷新",
      subscription_refresh: "订阅刷新",
    }[source] || source
  );
}

function exceptionStatusLabel(status: string) {
  return (
    {
      failed: "失败",
      skipped: "已跳过",
      deactive: "封禁",
      error: "错误",
      missing_mailbox: "缺邮箱",
      recovery_disabled: "恢复关闭",
      auto_refresh_locked: "刷新锁定",
    }[status] || status
  );
}

function exceptionStatusTone(status: string) {
  return (
    {
      failed: "error",
      deactive: "deactive",
      error: "error",
      missing_mailbox: "warn",
      recovery_disabled: "warn",
      auto_refresh_locked: "error",
      skipped: "warn",
    }[status] || "ink"
  );
}

function eventReasonLabel(event: AppEvent) {
  const reason = event.details?.reason;
  if (reason === "scheduled") return "自动";
  if (reason === "manual") return "手动";
  return null;
}

function eventKindLabel(kind: string) {
  return (
    {
      sub2api_protocol_refresh_failed: "sub2api /refresh",
      sub2api_check_status_unavailable: "sub2api /check-status",
      runtime_access_token_missing: "运行态 AT",
      access_token_status_check_failed: "AT 状态检查",
      chatgpt_protocol_refresh_failed: "ChatGPT 协议",
      openai_oauth_refresh_token_failed: "OpenAI OAuth",
      refresh_failed: "刷新失败",
      refresh_started: "刷新开始",
      refresh_succeeded: "刷新成功",
      refresh_deactive: "刷新封禁",
      refresh_skipped_missing_mailbox: "缺少邮箱",
    }[kind] || kind
  );
}

function eventTone(kind: string) {
  if (kind.includes("failed")) return "error";
  if (kind === "sub2api_check_status_unavailable" || kind === "runtime_access_token_missing") return "warn";
  if (kind.includes("succeeded")) return "ok";
  if (kind.includes("started")) return "running";
  return "ink";
}

function eventSyncErrorCount(event: AppEvent) {
  const directCount = parseEventCount(event.details?.error_seen);
  if (directCount !== null) return directCount;
  return parseEventCount(event.details?.sync_error_seen);
}

function parseEventCount(value: unknown) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.max(0, Math.trunc(value));
  }
  if (typeof value === "string") {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) {
      return Math.max(0, Math.trunc(parsed));
    }
  }
  return null;
}

function formatDate(value: string, timeZone = defaultTimeZone) {
  const date = parseApiDate(value);
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: defaultTimeZone,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }
}

function formatFullDate(value: string, timeZone = defaultTimeZone) {
  const date = parseApiDate(value);
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: defaultTimeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }
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

function formatShortDate(value: string, timeZone = defaultTimeZone) {
  const date = parseApiDate(value);
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone,
      month: "2-digit",
      day: "2-digit",
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: defaultTimeZone,
      month: "2-digit",
      day: "2-digit",
    }).format(date);
  }
}

function parseApiDate(value: string) {
  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  return new Date(hasTimeZone ? value : `${value}Z`);
}

function formatBytes(value: number) {
  const mib = value / 1024 / 1024;
  if (mib < 1024) {
    return `${mib.toFixed(1)} MiB`;
  }
  return `${(mib / 1024).toFixed(2)} GiB`;
}

export default App;
