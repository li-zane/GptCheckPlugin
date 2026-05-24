import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Globe2,
  Inbox,
  KeyRound,
  Link2,
  LogOut,
  Mail,
  MailOpen,
  Play,
  Radar,
  RefreshCcw,
  Save,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  TimerReset,
  Trash2,
  UserRoundX,
  UsersRound,
  X,
  type LucideIcon,
} from "lucide-react";
import { createContext, FormEvent, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { api } from "./api";
import type {
  Account,
  AccountUsageEstimate,
  AppEvent,
  AppSettings,
  AppSettingsUpdate,
  Mailbox,
  MailMessage,
  RefreshJob,
  Summary,
  UsageEstimate,
  UsageWindowAggregate,
  UsageWindowEstimate,
} from "./types";

type View = "overview" | "accounts" | "usage" | "mailboxes" | "history" | "settings";

const defaultTimeZone = "Asia/Shanghai";
const defaultSiteName = "sub2api AT 刷新机";
const sub2ApiApiPrefix = "/api/v1";
const TimeZoneContext = createContext(defaultTimeZone);
const timeZoneOptions = [
  { value: "Asia/Shanghai", label: "中国标准时间 · Asia/Shanghai" },
  { value: "UTC", label: "UTC" },
  { value: "Asia/Tokyo", label: "日本 · Asia/Tokyo" },
  { value: "Asia/Singapore", label: "新加坡 · Asia/Singapore" },
  { value: "Europe/London", label: "伦敦 · Europe/London" },
  { value: "America/New_York", label: "纽约 · America/New_York" },
  { value: "America/Los_Angeles", label: "洛杉矶 · America/Los_Angeles" },
];

const emptySummary: Summary = {
  total_accounts: 0,
  error_accounts: 0,
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
  monitor_interval_seconds: 300,
  usage_refresh_enabled: false,
  usage_refresh_interval_seconds: 3600,
  refresh_max_concurrency: 1,
  last_scan_at: null,
  last_scan_status: null,
  last_scan_message: null,
  display_timezone: defaultTimeZone,
  site_name: defaultSiteName,
};

function App() {
  const [authState, setAuthState] = useState<"checking" | "in" | "out">("checking");
  const [view, setView] = useState<View>("overview");
  const [summary, setSummary] = useState<Summary>(emptySummary);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [mailboxes, setMailboxes] = useState<Mailbox[]>([]);
  const [jobs, setJobs] = useState<RefreshJob[]>([]);
  const [events, setEvents] = useState<AppEvent[]>([]);
  const [settings, setSettings] = useState<AppSettings>(emptySettings);
  const [usageEstimate, setUsageEstimate] = useState<UsageEstimate | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState("");
  const [usageEstimateRefreshed, setUsageEstimateRefreshed] = useState(false);
  const [notice, setNotice] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const siteName = settings.site_name?.trim() || defaultSiteName;

  const loadAll = useCallback(async () => {
    const [nextSummary, nextAccounts, nextMailboxes, nextJobs, nextEvents, nextSettings] = await Promise.all([
      api.summary(),
      api.accounts(),
      api.mailboxes(),
      api.jobs(),
      api.events(),
      api.settings(),
    ]);
    setSummary(nextSummary);
    setAccounts(nextAccounts);
    setMailboxes(nextMailboxes);
    setJobs(nextJobs);
    setEvents(nextEvents);
    setSettings(nextSettings);
  }, []);

  const usageByEmail = useMemo(() => {
    const entries = usageEstimate?.accounts.map((account) => [account.email.toLowerCase(), account] as const) || [];
    return new Map(entries);
  }, [usageEstimate]);

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

  useEffect(() => {
    api
      .me()
      .then(() => {
        setAuthState("in");
        return loadAll();
      })
      .catch(() => setAuthState("out"));
  }, [loadAll]);

  useEffect(() => {
    if (authState !== "in") return;
    const timer = window.setInterval(() => {
      loadAll().catch(() => undefined);
    }, 12_000);
    return () => window.clearInterval(timer);
  }, [authState, loadAll]);

  useEffect(() => {
    if (authState !== "in" || usageLoading || usageError) return;
    if (view === "accounts") {
      if (usageEstimate) return;
      loadUsageEstimate(false).catch(() => undefined);
      return;
    }
    if (view === "usage") {
      if (usageEstimate && usageEstimateRefreshed) return;
      loadUsageEstimate(true).catch(() => undefined);
    }
  }, [authState, loadUsageEstimate, usageError, usageEstimate, usageEstimateRefreshed, usageLoading, view]);

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

  if (authState === "checking") {
    return <BootScreen />;
  }

  if (authState === "out") {
    return (
      <LoginScreen
        siteName={siteName}
        onLogin={async (adminKey) => {
          await api.login(adminKey);
          setAuthState("in");
          await loadAll();
        }}
      />
    );
  }

  const navItems: Array<{ id: View; label: string; icon: LucideIcon }> = [
    { id: "overview", label: "概览", icon: Activity },
    { id: "accounts", label: "账号", icon: UsersRound },
    { id: "usage", label: "额度", icon: TimerReset },
    { id: "mailboxes", label: "邮箱", icon: Mail },
    { id: "history", label: "历史", icon: Clock3 },
    { id: "settings", label: "设置", icon: Settings2 },
  ];

  return (
    <TimeZoneContext.Provider value={settings.display_timezone || defaultTimeZone}>
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
            {notice ? <span className="notice">{notice}</span> : null}
            <button className="secondary-button" disabled={busy} type="button" onClick={() => runAction(api.sync, "同步完成")}>
              <RefreshCcw size={17} />
              <span>同步</span>
            </button>
          </div>
        </header>

        {view === "overview" ? (
          <Overview summary={summary} accounts={accounts} jobs={jobs} events={events} />
        ) : null}
        {view === "accounts" ? (
          <AccountsView
            accounts={accounts}
            busy={busy}
            usageByEmail={usageByEmail}
            usageLoading={usageLoading}
            onDeleteDeactivated={() => runAction(api.deleteDeactivatedAccounts, "已删除停用账号")}
            onRefresh={(email) => runAction(() => api.refresh(email), "已创建检测/刷新任务")}
            onToggleUsageEstimate={(id, enabled) =>
              runAction(async () => {
                const result = await api.updateAccountUsageEstimate(id, enabled);
                await loadUsageEstimate(false);
                return result;
              }, enabled ? "已纳入额度估算" : "已排除额度估算")
            }
            onRefreshUsage={() =>
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
              }, "用量窗口已刷新")
            }
          />
        ) : null}
        {view === "usage" ? (
          <UsageEstimateView
            estimate={usageEstimate}
            error={usageError}
            loading={usageLoading}
            onRefresh={() => loadUsageEstimate(true)}
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
        {view === "history" ? (
          <HistoryView
            busy={busy}
            jobs={jobs}
            events={events}
            onClear={() => runAction(api.clearHistory, "历史已清空")}
          />
        ) : null}
        {view === "settings" ? (
          <SettingsView
            busy={busy}
            settings={settings}
            onScan={() => runAction(api.scanSub2Api, "扫描完成")}
            onSave={(payload) => runAction(() => api.updateSettings(payload), "设置已保存")}
          />
        ) : null}
      </section>
      </main>
    </TimeZoneContext.Provider>
  );
}

function LoginScreen({ siteName, onLogin }: { siteName: string; onLogin: (adminKey: string) => Promise<void> }) {
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
        <div className="login-emblem">
          <img alt="" src="/logo.png" />
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

function Overview({
  summary,
  accounts,
  jobs,
  events,
}: {
  summary: Summary;
  accounts: Account[];
  jobs: RefreshJob[];
  events: AppEvent[];
}) {
  const recentAccounts = accounts.slice(0, 6);
  const latestJob = jobs[0];
  const latestEvent = events[0];
  const stats = [
    { label: "账号", value: summary.total_accounts, icon: UsersRound, tone: "ink" },
    { label: "错误", value: summary.error_accounts, icon: AlertTriangle, tone: "warn" },
    { label: "恢复中", value: summary.refreshing_accounts, icon: RefreshCcw, tone: "teal" },
    { label: "停用", value: summary.deactive_accounts, icon: UserRoundX, tone: "danger" },
    { label: "邮箱", value: summary.mailbox_count, icon: Mail, tone: "blue" },
    { label: "24h 成功", value: summary.recent_success, icon: CheckCircle2, tone: "ok" },
  ];

  return (
    <div className="stack">
      <section className="stat-grid">
        {stats.map((stat) => {
          const Icon = stat.icon;
          return (
            <article className={`stat-card ${stat.tone}`} key={stat.label}>
              <Icon size={20} />
              <span>{stat.label}</span>
              <strong>{stat.value}</strong>
            </article>
          );
        })}
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

function AccountsView({
  accounts,
  busy,
  usageByEmail,
  usageLoading,
  onDeleteDeactivated,
  onRefresh,
  onRefreshUsage,
  onToggleUsageEstimate,
}: {
  accounts: Account[];
  busy: boolean;
  usageByEmail: Map<string, AccountUsageEstimate>;
  usageLoading: boolean;
  onDeleteDeactivated: () => void;
  onRefresh: (email: string) => void;
  onRefreshUsage: () => void;
  onToggleUsageEstimate: (id: number, enabled: boolean) => void;
}) {
  const deactivatedCount = accounts.filter((account) => account.deactive).length;
  const deleteDeactivated = () => {
    if (!deactivatedCount || busy) return;
    if (window.confirm(`确定删除 ${deactivatedCount} 个停用账号，并同步删除邮箱和 sub2api 账号吗？`)) {
      onDeleteDeactivated();
    }
  };

  return (
    <section className="panel">
      <div className="panel-toolbar">
        <PanelTitle title="账号状态" icon={UsersRound} />
        <div className="toolbar-actions">
          <button className="secondary-button" disabled={busy || usageLoading || !accounts.length} onClick={onRefreshUsage} type="button">
            <Radar size={17} />
            <span>{usageLoading ? "查询中" : "查询用量窗口"}</span>
          </button>
          <button className="danger-button" disabled={busy || !deactivatedCount} onClick={deleteDeactivated} type="button">
            <Trash2 size={17} />
            <span>删除停用账号</span>
          </button>
        </div>
      </div>
      <div className="table-wrap">
        <table className="accounts-table">
          <thead>
            <tr>
              <th>邮箱</th>
              <th>绑定邮箱</th>
              <th>参与额度</th>
              <th>状态</th>
              <th>调度</th>
              <th>sub2api ID</th>
              <th>5h 额度</th>
              <th>7d 额度</th>
              <th>最近更新</th>
              <th>错误</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {accounts.map((account) => (
              <AccountRow
                account={account}
                busy={busy}
                key={account.email}
                usage={usageByEmail.get(account.email.toLowerCase())}
                usageLoading={usageLoading}
                onRefresh={onRefresh}
                onToggleUsageEstimate={onToggleUsageEstimate}
              />
            ))}
          </tbody>
        </table>
        {!accounts.length ? <Empty label="尚未同步账号" /> : null}
      </div>
    </section>
  );
}

function AccountRow({
  account,
  busy,
  compact = false,
  usage,
  usageLoading = false,
  onRefresh,
  onToggleUsageEstimate,
}: {
  account: Account;
  busy?: boolean;
  compact?: boolean;
  usage?: AccountUsageEstimate;
  usageLoading?: boolean;
  onRefresh?: (email: string) => void;
  onToggleUsageEstimate?: (id: number, enabled: boolean) => void;
}) {
  const timeZone = useDisplayTimeZone();
  const tone = account.deactive ? "deactive" : account.refreshing ? "running" : account.last_error ? "error" : "ok";
  const ActionIcon = account.mailbox_bound ? Play : Radar;
  const actionTitle = account.mailbox_bound ? "刷新 AT" : "检测账号";
  if (compact) {
    return (
      <div className="compact-row">
        <span className="mono">{account.email}</span>
        <Badge tone={tone}>{account.deactive ? "停用" : account.refreshing ? "刷新中" : account.status || "未知"}</Badge>
      </div>
    );
  }
  return (
    <tr>
      <td className="mono">{account.email}</td>
      <td>
        <Badge tone={account.mailbox_bound ? "ok" : "ink"}>{account.mailbox_bound ? "已绑定" : "未绑定"}</Badge>
      </td>
      <td>
        <label className="table-check" title={account.usage_estimate_enabled ? "参与总额度估算" : "不参与总额度估算"}>
          <input
            checked={account.usage_estimate_enabled}
            disabled={busy}
            onChange={(event) => onToggleUsageEstimate?.(account.id, event.target.checked)}
            type="checkbox"
          />
          <span>{account.usage_estimate_enabled ? "参与" : "排除"}</span>
        </label>
      </td>
      <td>
        <Badge tone={tone}>{account.deactive ? "停用" : account.refreshing ? "刷新中" : account.status || "未知"}</Badge>
      </td>
      <td>{account.schedulable === null ? "未知" : account.schedulable ? "可用" : "暂停"}</td>
      <td className="mono muted">{account.sub2api_account_id || "-"}</td>
      <td>
        <AccountQuotaCell loading={usageLoading && !usage} window={usage?.five_hour} />
      </td>
      <td>
        <AccountQuotaCell loading={usageLoading && !usage} window={usage?.seven_day} />
      </td>
      <td>{formatDate(account.updated_at, timeZone)}</td>
      <td className="truncate">{account.last_error || "-"}</td>
      <td className="right">
        <button
          className="icon-button"
          disabled={busy || account.refreshing || account.deactive}
          onClick={() => onRefresh?.(account.email)}
          title={actionTitle}
          type="button"
        >
          <ActionIcon size={16} />
        </button>
      </td>
    </tr>
  );
}

function AccountQuotaCell({ window, loading }: { window?: UsageWindowEstimate; loading?: boolean }) {
  if (loading) {
    return <span className="muted">查询中</span>;
  }
  if (!window) {
    return <span className="muted">-</span>;
  }
  return (
    <div className="quota-cell">
      <strong>{window.remaining === null ? "无法估算" : formatMoney(window.remaining)}</strong>
      <span>
        {window.remaining_percent === null ? "占比 -" : `可用 ${formatPercent(window.remaining_percent)}`} · 已用{" "}
        {formatPercent(window.used_percent)}
      </span>
    </div>
  );
}

function UsageEstimateView({
  estimate,
  loading,
  error,
  onRefresh,
}: {
  estimate: UsageEstimate | null;
  loading: boolean;
  error: string;
  onRefresh: () => Promise<unknown>;
}) {
  const timeZone = useDisplayTimeZone();
  const formulaItems = estimate
    ? [
        ["单账号额度", estimate.formula.account_limit],
        ["单账号剩余", estimate.formula.account_remaining],
        ["综合剩余额度", estimate.formula.aggregate_remaining],
        ["综合剩余占比", estimate.formula.aggregate_remaining_percent],
      ]
    : [];

  return (
    <div className="stack">
      <section className="panel usage-estimate-panel">
        <div className="panel-toolbar">
          <div>
            <PanelTitle title="额度估算" icon={TimerReset} />
            <p className="panel-subtitle">
              {estimate ? `更新于 ${formatDate(estimate.updated_at, timeZone)} · ${estimate.overall.account_count} 个账号` : "等待用量数据"}
            </p>
          </div>
          <button className="secondary-button" disabled={loading} onClick={() => onRefresh().catch(() => undefined)} type="button">
            <RefreshCcw className={loading ? "spin" : ""} size={17} />
            <span>{loading ? "估算中" : "刷新额度"}</span>
          </button>
        </div>

        {error ? <div className="mail-error">{error}</div> : null}

        <div className="usage-summary-grid">
          <UsageSummaryCard aggregate={estimate?.overall.five_hour} title="综合 5h" />
          <UsageSummaryCard aggregate={estimate?.overall.seven_day} title="综合 7d" />
        </div>

        {formulaItems.length ? (
          <div className="formula-grid">
            {formulaItems.map(([label, formula]) => (
              <div className="formula-item" key={label}>
                <span>{label}</span>
                <code>{formula}</code>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      {!estimate && loading ? <Empty label="正在读取 sub2api 用量" /> : null}

      {estimate ? (
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
                    <th>7d 剩余</th>
                    <th>7d 占比</th>
                  </tr>
                </thead>
                <tbody>
                  {estimate.groups.map((group) => (
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
              {!estimate.groups.length ? <Empty label="暂无分组数据" /> : null}
            </div>
          </section>

          <section className="panel">
            <PanelTitle title="账号额度明细" icon={Activity} />
            <div className="table-wrap">
              <table className="usage-account-table">
                <thead>
                  <tr>
                    <th>邮箱</th>
                    <th>分组</th>
                    <th>参与</th>
                    <th>5h 用量</th>
                    <th>5h 剩余</th>
                    <th>7d 用量</th>
                    <th>7d 剩余</th>
                    <th>重置</th>
                  </tr>
                </thead>
                <tbody>
                  {estimate.accounts.map((account) => (
                    <tr key={account.email}>
                      <td className="mono">{account.email}</td>
                      <td>{account.groups.map((group) => group.name).join(", ")}</td>
                      <td>
                        <Badge tone={account.usage_estimate_enabled ? "ok" : "ink"}>
                          {account.usage_estimate_enabled ? "参与" : "排除"}
                        </Badge>
                      </td>
                      <td>{formatWindowUsage(account.five_hour)}</td>
                      <td>{formatWindowRemaining(account.five_hour)}</td>
                      <td>{formatWindowUsage(account.seven_day)}</td>
                      <td>{formatWindowRemaining(account.seven_day)}</td>
                      <td>
                        <div className="quota-cell">
                          <span>5h {account.five_hour.reset_at ? formatDate(account.five_hour.reset_at, timeZone) : "-"}</span>
                          <span>7d {account.seven_day.reset_at ? formatDate(account.seven_day.reset_at, timeZone) : "-"}</span>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!estimate.accounts.length ? <Empty label="暂无账号用量" /> : null}
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

function UsageSummaryCard({ title, aggregate }: { title: string; aggregate?: UsageWindowAggregate }) {
  return (
    <article className="usage-summary-card">
      <span>{title}</span>
      <strong>{aggregate ? formatAggregateMoney(aggregate) : "-"}</strong>
      <div className="quota-meter" aria-hidden="true">
        <div style={{ width: `${aggregate?.remaining_percent ?? 0}%` }} />
      </div>
      <p>
        剩余 {formatPercent(aggregate?.remaining_percent ?? null)} · 参与 {aggregate?.enabled_account_count ?? 0}/
        {aggregate?.account_count ?? 0} · 可估算 {aggregate?.estimable_accounts ?? 0}
      </p>
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

  const supportsJunk = selectedMailbox ? ["outlook", "hotmail"].includes(selectedMailbox.provider) : false;
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
            placeholder={"gpt@example.com----mail@hotmail.com----mail_password----client_id----refresh_token\nother@example.com----other@outlook.com----mail_password----client_id----refresh_token"}
            rows={6}
            value={content}
          />
          <p className="form-hint">支持批量导入，一行一个；空行和 # 开头的行会被忽略。</p>
        </form>
      </section>

      <section className="panel">
        <PanelTitle title="邮箱凭据" icon={ShieldCheck} />
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
              {mailboxes.map((mailbox) => (
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
    <div className="modal-backdrop" role="presentation">
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
            {messages.map((message) => (
              <article className="mail-item" key={message.id}>
                <div className="mail-item-main">
                  <strong>{message.subject || "无主题"}</strong>
                  <span>{message.body_preview || "无预览内容"}</span>
                </div>
                <div className="mail-item-meta">
                  <span>{message.sender_name || message.sender_address || "未知发件人"}</span>
                  <time>{message.received_at ? formatDate(message.received_at, timeZone) : "-"}</time>
                </div>
              </article>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function SettingsView({
  settings,
  busy,
  onSave,
  onScan,
}: {
  settings: AppSettings;
  busy: boolean;
  onSave: (payload: AppSettingsUpdate) => Promise<void> | void;
  onScan: () => Promise<void> | void;
}) {
  const [siteName, setSiteName] = useState(settings.site_name || defaultSiteName);
  const [instanceUrl, setInstanceUrl] = useState(toSub2ApiInstanceUrl(settings.sub2api_base_url));
  const [interval, setInterval] = useState(String(settings.monitor_interval_seconds));
  const [usageRefreshEnabled, setUsageRefreshEnabled] = useState(settings.usage_refresh_enabled);
  const [usageRefreshInterval, setUsageRefreshInterval] = useState(String(settings.usage_refresh_interval_seconds));
  const [refreshMaxConcurrency, setRefreshMaxConcurrency] = useState(String(settings.refresh_max_concurrency || 1));
  const [displayTimeZone, setDisplayTimeZone] = useState(settings.display_timezone || defaultTimeZone);
  const [xApiKey, setXApiKey] = useState("");
  const [clearXApiKey, setClearXApiKey] = useState(false);

  useEffect(() => {
    setSiteName(settings.site_name || defaultSiteName);
    setInstanceUrl(toSub2ApiInstanceUrl(settings.sub2api_base_url));
    setInterval(String(settings.monitor_interval_seconds));
    setUsageRefreshEnabled(settings.usage_refresh_enabled);
    setUsageRefreshInterval(String(settings.usage_refresh_interval_seconds));
    setRefreshMaxConcurrency(String(settings.refresh_max_concurrency || 1));
    setDisplayTimeZone(settings.display_timezone || defaultTimeZone);
    setXApiKey("");
    setClearXApiKey(false);
  }, [settings]);

  const intervalNumber = Number(interval);
  const usageRefreshIntervalNumber = Number(usageRefreshInterval);
  const refreshMaxConcurrencyNumber = Number(refreshMaxConcurrency);
  const cleanSiteName = siteName.trim();
  const invalid =
    !cleanSiteName ||
    cleanSiteName.length > 80 ||
    !isSub2ApiInstanceUrl(instanceUrl) ||
    !Number.isInteger(intervalNumber) ||
    intervalNumber < 30 ||
    !Number.isInteger(usageRefreshIntervalNumber) ||
    usageRefreshIntervalNumber < 60 ||
    usageRefreshIntervalNumber > 86_400 ||
    !Number.isInteger(refreshMaxConcurrencyNumber) ||
    refreshMaxConcurrencyNumber < 1 ||
    refreshMaxConcurrencyNumber > 50;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (invalid) return;
    const payload: AppSettingsUpdate = {
      site_name: cleanSiteName,
      sub2api_base_url: toSub2ApiInstanceUrl(instanceUrl),
      monitor_interval_seconds: intervalNumber,
      usage_refresh_enabled: usageRefreshEnabled,
      usage_refresh_interval_seconds: usageRefreshIntervalNumber,
      refresh_max_concurrency: refreshMaxConcurrencyNumber,
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
              最大同时刷新数
              <input
                max={50}
                min={1}
                onChange={(event) => setRefreshMaxConcurrency(event.target.value)}
                type="number"
                value={refreshMaxConcurrency}
              />
            </label>
          </div>

          <label className="checkbox-line usage-refresh-toggle">
            <input
              checked={usageRefreshEnabled}
              onChange={(event) => setUsageRefreshEnabled(event.target.checked)}
              type="checkbox"
            />
            <span>自动查询账号用量窗口</span>
          </label>

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
              巡检 {settings.monitor_interval_seconds} 秒 · 用量{" "}
              {settings.usage_refresh_enabled ? `${settings.usage_refresh_interval_seconds} 秒` : "关闭"} · 并发{" "}
              {settings.refresh_max_concurrency}
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
  busy,
  onClear,
}: {
  jobs: RefreshJob[];
  events: AppEvent[];
  busy: boolean;
  onClear: () => void;
}) {
  const timeZone = useDisplayTimeZone();
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

      <section className="split-grid history-grid">
        <div className="panel">
          <PanelTitle title="刷新任务" icon={RefreshCcw} />
          <div className="history-list">
            {jobs.map((job) => (
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
              </article>
            ))}
            {!jobs.length ? <Empty label="暂无刷新任务" /> : null}
          </div>
        </div>

        <div className="panel">
          <PanelTitle title="事件" icon={Activity} />
          <div className="history-list">
            {events.map((event) => (
              <article className="history-item event-history-item" key={event.id}>
                <div className="history-item-head">
                  <div className="history-meta">
                    <Badge tone={event.kind.includes("failed") ? "error" : "ink"}>{event.kind}</Badge>
                    <strong className="history-email">{event.email || "system"}</strong>
                  </div>
                  <time>{formatDate(event.created_at, timeZone)}</time>
                </div>
                <p className="history-message">{event.message}</p>
              </article>
            ))}
            {!events.length ? <Empty label="暂无事件" /> : null}
          </div>
        </div>
      </section>
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

function Badge({ children, tone }: { children: string; tone: string }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

function Empty({ label }: { label: string }) {
  return <div className="empty">{label}</div>;
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

function formatAggregateMoney(aggregate: UsageWindowAggregate | null | undefined) {
  return aggregate?.remaining === null || aggregate?.remaining === undefined ? "-" : formatMoney(aggregate.remaining);
}

function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  const normalized = Math.max(0, Math.min(value, 100));
  return `${Number.isInteger(normalized) ? normalized.toFixed(0) : normalized.toFixed(1)}%`;
}

function formatWindowUsage(window: UsageWindowEstimate) {
  return `${formatPercent(window.used_percent)} · ${formatMoney(window.spent)}`;
}

function formatWindowRemaining(window: UsageWindowEstimate) {
  return window.remaining === null ? "无法估算" : `${formatMoney(window.remaining)} · ${formatPercent(window.remaining_percent)}`;
}

function titleFor(view: View) {
  return {
    overview: "调度恢复概览",
    accounts: "GPT 账号状态",
    usage: "额度估算",
    mailboxes: "验证码邮箱",
    history: "刷新历史",
    settings: "运行设置",
  }[view];
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

function useDisplayTimeZone() {
  return useContext(TimeZoneContext);
}

function statusLabel(status: string) {
  return (
    {
      queued: "排队",
      running: "运行中",
      succeeded: "成功",
      failed: "失败",
      deactive: "停用",
    }[status] || status
  );
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
