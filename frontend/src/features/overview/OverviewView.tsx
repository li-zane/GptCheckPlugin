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
import { api, upstreamLegacyBindingCounts } from "../../shared/api";
import {
  persistOverviewBalanceAlertDismissed,
  readOverviewBalanceAlertDismissed,
} from "../../overviewBalanceAlertPreference";
import {
  accountBillingRateChange,
  groupRateChange,
  remoteSchedulableChange,
  upstreamGroupStatusChange,
  upstreamKeyStatusChange,
  upstreamRateChange,
  upstreamChangeSummary,
  type UpstreamStateChange,
} from "../../upstreamRatePresentation";
import {
  upstreamChangeReasonLabel,
  upstreamHealthStatusLabel,
  upstreamStatusTone,
  type UpstreamHealthKind,
} from "../../upstreamLabels";
import {
  accountMatchesUsageDetailFilter,
  buildDisplayedUsageEstimate,
  usageDetailAccountCounts,
  usageDetailAccountRateLimited,
  usageDetailAccountVisible,
  usageEstimateHeaderStats,
  usageProblemAccountUnusedQuota,
  type ProblemUnusedQuotaSummary,
  type UsageDetailAccountFilter,
} from "../../usageEstimatePresentation";
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
import { AccountCounts, AccountRow, Badge, CompactAccountIdentity, Empty, PanelTitle, SignalLine, accountHasError, accountRateLimited, accountRateLimitedWindowKeys, accountRowKey, accountStatusLabel, accountStatusTone, formatAggregateMoney, formatDate, formatMoney, rateLimitDetailsForWindows, usageForAccount, useDisplayTimeZone, useNow } from "../shared/LegacyDisplay";

export function Overview({
  summary,
  accounts,
  accountCounts,
  apiKeyRefreshVersion,
  jobs,
  events,
  problemUnusedQuota,
  showStaleNegativeBalanceAlert,
  balanceBasis,
  balanceThreshold,
  upstreamOverview,
  usageByAccountId,
  usageByEmail,
  onOpenUpstreams,
}: {
  summary: Summary;
  accounts: Account[];
  accountCounts: AccountCounts;
  apiKeyRefreshVersion: number;
  jobs: RefreshJob[];
  events: AppEvent[];
  problemUnusedQuota: ProblemUnusedQuotaSummary | null;
  showStaleNegativeBalanceAlert: boolean;
  balanceBasis: "wallet" | "recharge_adjusted";
  balanceThreshold: number;
  upstreamOverview: UpstreamOverviewResponse | null;
  usageByAccountId: Map<string, AccountUsageEstimate>;
  usageByEmail: Map<string, AccountUsageEstimate>;
  onOpenUpstreams: () => void;
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
  const rateLimitWindowGroups = [
    { windowKey: "five_hour", title: "5h", accounts: fiveHourRateLimitedAccounts },
    { windowKey: "seven_day", title: "7d", accounts: sevenDayRateLimitedAccounts },
    { windowKey: "monthly", title: "月", accounts: monthlyRateLimitedAccounts },
  ].filter((group) => group.accounts.length > 0);
  const [selectedRateLimitWindowKey, setSelectedRateLimitWindowKey] = useState("five_hour");
  const selectedRateLimitWindow =
    rateLimitWindowGroups.find((group) => group.windowKey === selectedRateLimitWindowKey) || rateLimitWindowGroups[0] || null;
  const rateLimitedAccountCount = new Set(
    [...fiveHourRateLimitedAccounts, ...sevenDayRateLimitedAccounts, ...monthlyRateLimitedAccounts].map(accountRowKey),
  ).size;
  const availableAccountCount = accounts.filter(
    (account) => !account.deactive && !accountHasError(account) && !accountRateLimited(account),
  ).length;
  const errorAccountCount = accounts.filter(
    (account) => !account.deactive && accountHasError(account),
  ).length;
  const problemUnusedQuotaTitle = problemUnusedQuota
    ? `错误/封停账号 ${problemUnusedQuota.accountCount} 个，可估 ${problemUnusedQuota.sevenDay.estimable_accounts} 个，5h 未用 ${formatAggregateMoney(problemUnusedQuota.fiveHour)}`
    : "等待额度估算数据";
  const [lowBalanceAlertDismissed, setLowBalanceAlertDismissed] = useState(
    () => readOverviewBalanceAlertDismissed(),
  );
  const lowBalanceUpstreams = (upstreamOverview?.upstreams || []).filter((upstream) =>
    upstreamHasLowBalance(
      upstream,
      showStaleNegativeBalanceAlert,
      balanceBasis,
      balanceThreshold,
    ),
  );
  const lowBalanceSignature = lowBalanceUpstreams
    .map((upstream) => `${upstream.upstream_id}:${upstream.balance_guard_state}:${upstream.balance_guard_value}:${upstream.wallet_balance_usd}:${upstream.balance_checked_at}`)
    .join("|");
  const showLowBalanceAlert = Boolean(lowBalanceSignature && !lowBalanceAlertDismissed);
  const stats: Array<{ label: string; value: number | string; icon: LucideIcon; tone: string; title?: string }> = [
    { label: "实际账号", value: actualAccounts, icon: UsersRound, tone: "ink" },
    { label: "去重账号", value: dedupedAccounts, icon: ShieldCheck, tone: "ok" },
    { label: "可用", value: availableAccountCount, icon: CheckCircle2, tone: "ok" },
    { label: "重复", value: duplicateAccounts, icon: Link2, tone: "warn" },
    { label: "限流", value: rateLimitedAccountCount, icon: ShieldAlert, tone: "warn" },
    { label: "错误", value: errorAccountCount, icon: AlertTriangle, tone: "warn" },
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

      {showLowBalanceAlert ? (
        <section className="overview-balance-alert" role="alert">
          <AlertTriangle size={19} />
          <div>
            <strong>上游余额不足</strong>
            <span>{lowBalanceUpstreams.map((upstream) => `${upstream.display_name || upstream.api_endpoint_url || `上游 ${upstream.upstream_id}`} ${formatBalanceGuardValue(upstream, balanceBasis)}${isStaleLowBalance(upstream, balanceBasis, balanceThreshold) ? "（上次结果）" : ""}`).join(" · ")}</span>
          </div>
          <button className="secondary-button" onClick={onOpenUpstreams} type="button">
            <span>查看上游</span>
            <ArrowRight size={15} />
          </button>
          <button
            aria-label="关闭上游余额不足提醒"
            className="icon-button overview-balance-alert-close"
            onClick={() => {
              setLowBalanceAlertDismissed(true);
              persistOverviewBalanceAlertDismissed();
            }}
            title="关闭提醒"
            type="button"
          >
            <X size={15} />
          </button>
        </section>
      ) : null}

      <section className="panel rate-limit-panel">
        <div className="panel-toolbar">
          <div>
            <PanelTitle title="限流账号" icon={ShieldAlert} />
            <p className="panel-subtitle">按管理站点当前窗口状态分开显示 5h、7d 与月限流账号。</p>
          </div>
          <Badge tone={rateLimitedAccountCount ? "warn" : "ok"}>
            {rateLimitedAccountCount ? `${rateLimitedAccountCount} 个账号` : "无限流"}
          </Badge>
        </div>
        {rateLimitWindowGroups.length ? (
          <div className="rate-limit-filter-tabs" role="tablist" aria-label="限流窗口筛选">
            {rateLimitWindowGroups.map((group) => (
              <button
                className={selectedRateLimitWindow?.windowKey === group.windowKey ? "rate-limit-filter-tab active" : "rate-limit-filter-tab"}
                key={group.windowKey}
                onClick={() => setSelectedRateLimitWindowKey(group.windowKey)}
                role="tab"
                aria-selected={selectedRateLimitWindow?.windowKey === group.windowKey}
                type="button"
              >
                <span>{group.title}</span>
                <strong>{group.accounts.length}</strong>
              </button>
            ))}
          </div>
        ) : null}
        {selectedRateLimitWindow ? (
          <div className="rate-limit-window-grid filtered">
            <RateLimitedAccountColumn
              title={selectedRateLimitWindow.title}
              windowKey={selectedRateLimitWindow.windowKey}
              accounts={selectedRateLimitWindow.accounts}
              usageByAccountId={usageByAccountId}
              usageByEmail={usageByEmail}
            />
          </div>
        ) : (
          <Empty label="暂无限流账号" />
        )}
      </section>

      <RecentApiKeyUpstreamChanges refreshVersion={apiKeyRefreshVersion} />

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
            <SignalLine label="最近任务" value={latestJob ? `${latestJob.email} · ${refreshJobStatusLabel(latestJob.status)}` : "暂无"} />
            <SignalLine label="最近事件" value={latestEvent ? latestEvent.message : "暂无"} />
            <SignalLine label="24h 失败" value={`${summary.recent_failed}`} />
          </div>
        </div>
      </section>
    </div>
  );
}

function RecentApiKeyUpstreamChanges({ refreshVersion }: { refreshVersion: number }) {
  const timeZone = useDisplayTimeZone();
  const [logs, setLogs] = useState<UpstreamChangeLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestSequence = useRef(0);

  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setError("");
    try {
      const next = await api.upstreamChangeLogs(6);
      if (sequence === requestSequence.current) setLogs(next);
    } catch (reason) {
      if (sequence === requestSequence.current) {
        setError(reason instanceof Error ? reason.message : "上游变化读取失败");
      }
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      requestSequence.current += 1;
    };
  }, [load, refreshVersion]);

  return (
    <section className="panel api-key-rate-log-panel" aria-label="最近 API 账号上游变化">
      <div className="panel-toolbar">
        <div>
          <PanelTitle title="最近 API 账号上游变化" icon={KeyRound} />
          <p className="panel-subtitle">按发生时间展示上游 Key、分组、上游实际倍率与账号调度状态。</p>
        </div>
        <button
          aria-label="刷新最近 API 账号上游变化"
          className="api-key-icon-button"
          disabled={loading}
          onClick={() => void load()}
          title="刷新"
          type="button"
        >
          <RefreshCcw className={loading ? "spin" : ""} size={16} />
        </button>
      </div>

      {error ? (
        <div className="api-key-rate-log-error" role="alert">
          <AlertTriangle size={15} />
          <span>{error}</span>
        </div>
      ) : loading && logs.length === 0 ? (
        <div className="api-key-empty">
          <RefreshCcw className="spin" size={18} />
          <span>正在读取上游变化…</span>
        </div>
      ) : logs.length === 0 ? (
        <div className="api-key-empty">
          <KeyRound size={18} />
          <span>暂无 API 账号上游变化</span>
        </div>
      ) : (
        <div className="api-key-rate-log-list">
          {logs.map((log) => (
            <article className="api-key-rate-log-row" key={log.id}>
              <div className="api-key-rate-log-identity">
                <strong>{log.account_name || `API 账号 #${log.management_account_id}`}</strong>
                <span>
                  {log.upstream_name || "未分配上游"}
                  {log.group_name ? ` · ${log.group_name}` : ""}
                </span>
                <div className="api-key-rate-log-meta">
                  <time dateTime={log.created_at}>{formatDate(log.created_at, timeZone)}</time>
                  <span>{upstreamChangeSummary(log) || upstreamChangeReasonLabel(log.reason)}</span>
                </div>
                <div className="api-key-upstream-state-list" aria-label="上游状态变化">
                  <OverviewUpstreamStateTransition change={upstreamKeyStatusChange(log)} kind="key" label="Key" />
                  <OverviewUpstreamStateTransition change={upstreamGroupStatusChange(log)} kind="group" label="分组" />
                  <OverviewUpstreamStateTransition change={remoteSchedulableChange(log)} kind="account" label="调度" />
                </div>
              </div>
              <OverviewRateChangeCell change={groupRateChange(log)} label="分组倍率" />
              <OverviewRateChangeCell change={upstreamRateChange(log)} emphasize label="上游实际倍率" />
              <OverviewRateChangeCell change={accountBillingRateChange(log)} label="账号计费倍率" />
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function OverviewUpstreamStateTransition({
  change,
  kind,
  label,
}: {
  change: UpstreamStateChange;
  kind: UpstreamHealthKind;
  label: string;
}) {
  const current = change.newValue ?? change.oldValue;
  const tone = upstreamStatusTone(current || "unknown");
  return (
    <span className={`api-key-upstream-transition api-key-chip api-key-chip--${tone}`}>
      <span>{label}</span>
      {change.direction === "changed" ? (
        <span className="api-key-upstream-transition-flow">
          <b>{upstreamHealthStatusLabel(kind, change.oldValue)}</b>
          <ArrowRight size={10} />
          <strong>{upstreamHealthStatusLabel(kind, change.newValue)}</strong>
        </span>
      ) : (
        <strong>{upstreamHealthStatusLabel(kind, current)}</strong>
      )}
    </span>
  );
}

function OverviewRateChangeCell({
  change,
  emphasize = false,
  label,
}: {
  change: ReturnType<typeof groupRateChange>;
  emphasize?: boolean;
  label: string;
}) {
  const changed = change.direction === "increase" || change.direction === "decrease";
  const value = change.newValue ?? change.oldValue;
  return (
    <div className={"api-key-rate-log-cell" + (emphasize ? " api-key-rate-log-cell--primary" : "")}>
      <span>{label}</span>
      {changed ? (
        <div className="api-key-rate-log-flow">
          <b>{formatOverviewMultiplier(change.oldValue)}</b>
          <ArrowRight size={13} />
          <strong>{formatOverviewMultiplier(change.newValue)}</strong>
        </div>
      ) : (
        <strong className="api-key-rate-log-static">{formatOverviewMultiplier(value)}</strong>
      )}
    </div>
  );
}

function formatOverviewMultiplier(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "--";
  return value.toFixed(4).replace(/\.?0+$/, "");
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
                <CompactAccountIdentity accountName={account.account_name} className="rate-limit-account-identity" email={account.email} />
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

function formatBalanceGuardValue(
  upstream: Upstream,
  balanceBasis: "wallet" | "recharge_adjusted",
) {
  if (String(upstream.balance_guard_state || "").toLowerCase() === "insufficient") {
    const value = Number(upstream.balance_guard_value);
    if (Number.isFinite(value)) {
      const formatted = value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      return upstream.balance_guard_basis === "recharge_adjusted" ? `¥${formatted}` : `$${formatted}`;
    }
  }
  const value = historicalBalanceValue(upstream, balanceBasis);
  if (value === null) return "待确认";
  const formatted = value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (balanceBasis === "recharge_adjusted") return `¥${formatted}`;
  const unit = String(upstream.balance_unit || "USD").trim().toUpperCase();
  return unit === "USD" ? `$${formatted}` : `${unit} ${formatted}`;
}

function upstreamHasLowBalance(
  upstream: Upstream,
  includeStale: boolean,
  balanceBasis: "wallet" | "recharge_adjusted",
  threshold: number,
) {
  const activeGuard = ["insufficient", "negative", "paused"].includes(
    String(upstream.balance_guard_state || "").toLowerCase(),
  );
  return activeGuard || (includeStale && hasKnownLowBalance(upstream, balanceBasis, threshold));
}

function isStaleLowBalance(
  upstream: Upstream,
  balanceBasis: "wallet" | "recharge_adjusted",
  threshold: number,
) {
  return hasKnownLowBalance(upstream, balanceBasis, threshold)
    && !["insufficient", "negative", "paused"].includes(
      String(upstream.balance_guard_state || "").toLowerCase(),
    );
}

function hasKnownLowBalance(
  upstream: Upstream,
  balanceBasis: "wallet" | "recharge_adjusted",
  threshold: number,
) {
  const balance = historicalBalanceValue(upstream, balanceBasis);
  return balance !== null && balance < threshold;
}

function historicalBalanceValue(
  upstream: Upstream,
  balanceBasis: "wallet" | "recharge_adjusted",
) {
  const balance = Number(upstream.wallet_balance_usd);
  if (
    upstream.balance_source !== "upstream_wallet"
    || !upstream.balance_checked_at
    || !Number.isFinite(balance)
  ) return null;
  if (balanceBasis === "wallet") return balance;
  const multiplier = Number(upstream.upstream_recharge_multiplier);
  return Number.isFinite(multiplier) && multiplier > 0 ? balance * multiplier : null;
}

function formatProblemUnusedQuota(summary: ProblemUnusedQuotaSummary | null) {
  if (!summary) return "-";
  if (summary.accountCount === 0) return formatMoney(0);
  return formatAggregateMoney(summary.sevenDay);
}

function accountCompare(left: Account, right: Account) {
  const emailCompare = left.email.localeCompare(right.email);
  if (emailCompare !== 0) return emailCompare;
  if (left.duplicate_rank !== right.duplicate_rank) return left.duplicate_rank - right.duplicate_rank;
  return (left.management_account_id || "").localeCompare(right.management_account_id || "");
}

function rateLimitedAccountsForWindow(accounts: Account[], window: string) {
  return accounts
    .filter((account) => !accountHasError(account) && accountRateLimitedWindowKeys(account).includes(window))
    .sort(accountCompare);
}

function refreshJobStatusLabel(status: string) {
  const normalized = status.trim().toLowerCase();
  if (normalized === "completed" || normalized === "success") return "完成";
  if (normalized === "failed" || normalized === "error") return "失败";
  if (normalized === "running") return "运行中";
  if (normalized === "queued" || normalized === "pending") return "等待中";
  return status || "未知";
}

function accountRateLimitDetails(account: Account, usage: AccountUsageEstimate | undefined, timeZone: string, now: number) {
  const windows = accountRateLimitedWindowKeys(account, usage);
  return rateLimitDetailsForWindows(windows, usage, timeZone, now);
}
