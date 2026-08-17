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
import { AccountQuotaCell, Badge, CopyTextButton, Empty, PanelTitle, accountIsManuallyPaused, accountRateLimitedWindowKeys, clampPercentValue, formatAggregateMoney, formatDate, formatMoney, formatPercent, formatTokenHistoryCountLabel, formatTokenHistoryTotalLabel, formatTokenWindowLabel, formatTokenWindowSummary, formatWindowRefreshTime, formatWindowRefreshTitle, periodLabel, planLabel, rateLimitedWindowLabel, subscriptionIsInvalid, subscriptionTypeLabel, usageLimitWindowKeys, usageSubscriptionSortRank, useDisplayTimeZone, useNow } from "../shared/LegacyDisplay";

const usageDetailPageSizeOptions = [25, 50, 100] as const;

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

export function UsageEstimateView({
  estimate,
  loading,
  error,
  onLocateAccount,
}: {
  estimate: UsageEstimate | null;
  loading: boolean;
  error: string;
  onLocateAccount: (account: AccountUsageEstimate) => void;
}) {
  const timeZone = useDisplayTimeZone();
  const now = useNow();
  const [includePausedAccounts, setIncludePausedAccounts] = useState(true);
  const [detailAccountFilter, setDetailAccountFilter] = useState<UsageDetailAccountFilter>("normal");
  const [requestedDetailRateLimitWindowKey, setRequestedDetailRateLimitWindowKey] = useState("");
  const [subscriptionFilter, setSubscriptionFilter] = useState("");
  const [detailPage, setDetailPage] = useState(1);
  const [detailPageSize, setDetailPageSize] = useState<(typeof usageDetailPageSizeOptions)[number]>(25);
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
  const detailRateLimitedAccounts = useMemo(
    () => displayedEstimate?.accounts.filter((account) => usageDetailAccountVisible(account) && usageDetailAccountRateLimited(account)) || [],
    [displayedEstimate],
  );
  const detailRateLimitWindowOptions = useMemo(
    () =>
      usageLimitWindowKeys
        .map((windowKey) => ({
          windowKey,
          label: rateLimitedWindowLabel(windowKey),
          count: detailRateLimitedAccounts.filter((account) => accountRateLimitedWindowKeys(account, account).includes(windowKey)).length,
        }))
        .filter((option) => option.count > 0),
    [detailRateLimitedAccounts],
  );
  const selectedDetailRateLimitWindow =
    detailAccountFilter === "rate-limited"
      ? detailRateLimitWindowOptions.find((option) => option.windowKey === requestedDetailRateLimitWindowKey) || detailRateLimitWindowOptions[0] || null
      : null;
  const selectedDetailRateLimitWindowKey = selectedDetailRateLimitWindow?.windowKey || "";
  const detailBaseAccounts = useMemo(
    () =>
      displayedEstimate?.accounts.filter(
        (account) =>
          accountMatchesUsageDetailFilter(account, detailAccountFilter) &&
          (!selectedDetailRateLimitWindowKey || accountRateLimitedWindowKeys(account, account).includes(selectedDetailRateLimitWindowKey)),
      ) || [],
    [detailAccountFilter, displayedEstimate, selectedDetailRateLimitWindowKey],
  );
  const subscriptionFilterOptions = useMemo(() => usageSubscriptionFilterOptions(detailBaseAccounts), [detailBaseAccounts]);
  const detailAccounts = useMemo(
    () => detailBaseAccounts.filter((account) => !subscriptionFilter || usageSubscriptionLabel(account) === subscriptionFilter),
    [detailBaseAccounts, subscriptionFilter],
  );
  const detailPageCount = Math.max(1, Math.ceil(detailAccounts.length / detailPageSize));
  const activeDetailPage = Math.min(detailPage, detailPageCount);
  const pagedDetailAccounts = useMemo(() => {
    const start = (activeDetailPage - 1) * detailPageSize;
    return detailAccounts.slice(start, start + detailPageSize);
  }, [activeDetailPage, detailAccounts, detailPageSize]);

  useEffect(() => {
    if (subscriptionFilter && !subscriptionFilterOptions.some((option) => option.label === subscriptionFilter)) {
      setSubscriptionFilter("");
      setDetailPage(1);
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

      {!estimate && loading ? <Empty label="正在读取管理站点用量" /> : null}

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
                {detailAccountFilter === "rate-limited" && detailRateLimitWindowOptions.length ? (
                  <div className="rate-limit-filter-tabs usage-detail-rate-window-filters" role="tablist" aria-label="限流账号窗口筛选">
                    {detailRateLimitWindowOptions.map((option) => (
                      <button
                        aria-selected={selectedDetailRateLimitWindowKey === option.windowKey}
                        className={selectedDetailRateLimitWindowKey === option.windowKey ? "rate-limit-filter-tab active" : "rate-limit-filter-tab"}
                        key={option.windowKey}
                        onClick={() => {
                          setRequestedDetailRateLimitWindowKey(option.windowKey);
                          setSubscriptionFilter("");
                          setDetailPage(1);
                        }}
                        role="tab"
                        type="button"
                      >
                        <span>{option.label}</span>
                        <strong>{option.count}</strong>
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="usage-detail-toolbar-actions">
                <div className="usage-detail-tabs" role="tablist" aria-label="额度明细账号类型">
                  <button
                    aria-selected={detailAccountFilter === "normal"}
                    className={detailAccountFilter === "normal" ? "usage-detail-tab active" : "usage-detail-tab"}
                    onClick={() => {
                      setDetailAccountFilter("normal");
                      setSubscriptionFilter("");
                      setDetailPage(1);
                    }}
                    role="tab"
                    type="button"
                  >
                    <span>正常账号</span>
                    <strong>{detailAccountCounts.normal}</strong>
                  </button>
                  <button
                    aria-selected={detailAccountFilter === "rate-limited"}
                    className={detailAccountFilter === "rate-limited" ? "usage-detail-tab active" : "usage-detail-tab"}
                    onClick={() => {
                      setDetailAccountFilter("rate-limited");
                      setSubscriptionFilter("");
                      setDetailPage(1);
                    }}
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
                      onClick={() => {
                        setSubscriptionFilter("");
                        setDetailPage(1);
                      }}
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
                        onClick={() => {
                          setSubscriptionFilter(option.label);
                          setDetailPage(1);
                        }}
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
                  <col className="usage-account-col-locate" />
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
                    <th aria-label="定位账号" />
                    <th>账号</th>
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
                  {pagedDetailAccounts.map((account, index) => (
                    <tr key={`${account.email}:${account.management_account_id || index}`}>
                      <td>
                        <button
                          aria-label={`在账号页查看 ${account.account_name || account.email}`}
                          className="icon-button usage-account-locate"
                          onClick={() => onLocateAccount(account)}
                          title="在账号页查看"
                          type="button"
                        >
                          <ArrowRight size={15} />
                        </button>
                      </td>
                      <td>
                        <StackedAccountIdentity accountName={account.account_name} email={account.email} />
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
            {detailAccounts.length ? (
              <nav aria-label="额度明细分页" className="usage-detail-pagination">
                <span>第 {activeDetailPage} / {detailPageCount} 页 · 共 {detailAccounts.length} 个账号</span>
                <div>
                  <label className="usage-detail-page-size">
                    <span>每页</span>
                    <select
                      onChange={(event) => {
                        setDetailPageSize(Number(event.target.value) as (typeof usageDetailPageSizeOptions)[number]);
                        setDetailPage(1);
                      }}
                      value={detailPageSize}
                    >
                      {usageDetailPageSizeOptions.map((size) => <option key={size} value={size}>{size} 条</option>)}
                    </select>
                  </label>
                  <button
                    aria-label="上一页额度明细"
                    className="icon-button"
                    disabled={activeDetailPage <= 1}
                    onClick={() => setDetailPage(activeDetailPage - 1)}
                    title="上一页"
                    type="button"
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <button
                    aria-label="下一页额度明细"
                    className="icon-button"
                    disabled={activeDetailPage >= detailPageCount}
                    onClick={() => setDetailPage(activeDetailPage + 1)}
                    title="下一页"
                    type="button"
                  >
                    <ChevronRight size={16} />
                  </button>
                </div>
              </nav>
            ) : null}
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
  const tone = subscriptionIsInvalid(account) ? "warn" : account.has_active_subscription === true ? "ok" : "ink";
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
  return subscriptionIsInvalid(account) ? "订阅无效" : plan === "active" ? "正常" : plan;
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

function StackedAccountIdentity({ accountName, email }: { accountName: string | null | undefined; email: string }) {
  return (
    <div className="account-identity-cell">
      <CopyTextButton className="account-identity-copy-button account-name-copy-button" title="复制账号名称" value={accountName?.trim() || email} />
      <CopyTextButton className="account-identity-copy-button account-email-copy-button mono" title="复制账号邮箱" value={email} />
    </div>
  );
}

function quotaMeterTone(value: number | null | undefined) {
  const normalized = clampPercentValue(value);
  if (normalized === null) return "ink";
  if (normalized <= 20) return "error";
  if (normalized <= 40) return "warn";
  return "ok";
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

function usageEstimateParticipationLabel(
  account: Pick<AccountUsageEstimate, "usage_estimate_enabled" | "error" | "rate_limited" | "schedulable" | "status" | "deactive">,
  includePausedAccounts = true,
) {
  if (account.deactive) return "封禁排除";
  if (account.error) return "错误排除";
  if (!account.usage_estimate_enabled) return "排除";
  if (account.rate_limited) return "限流窗口排除";
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
