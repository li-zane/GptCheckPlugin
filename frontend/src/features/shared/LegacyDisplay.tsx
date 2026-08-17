// @ts-nocheck Legacy aggregate display module is being split incrementally.
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
import { NowContext, TimeZoneContext } from "../../shared/hooks/displayContext";
import { MiddleEllipsisText } from "../../MiddleEllipsisText";
import {
  accountEstimateHasEffectiveError,
  accountRateLimitShouldBeVisible,
} from "../../accountRateLimitPresentation";
import { isOAuthPhoneVerificationStopped } from "../../accountErrorPresentation";
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

const loadAccountEditorDialog = () => import("../../AccountEditorDialog");

type AccountCounts = { actual: number; deduped: number; duplicates: number };

const defaultTimeZone = "Asia/Shanghai";

const usageLimitWindowKeys = ["five_hour", "seven_day", "monthly"] as const;

const nonExpiringSubscriptionTypes = new Set(["free", "team", "k12", "enterprise", "enterprise-edu", "edu"]);

const sub2ApiApiPrefix = "/api/v1";

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
  onEdit,
  onToggleSelected,
  sessionDeleteUnlocked = false,
  onToggleRefreshLock,
  onOpenMailbox,
  onOpenError,
  onOpenNotes,
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
  onEdit?: (account: Account) => void;
  onToggleSelected?: (account: Account, selected: boolean) => void;
  sessionDeleteUnlocked?: boolean;
  onToggleRefreshLock?: (account: Account, unlocked: boolean) => void;
  onOpenMailbox?: (mailbox: Mailbox) => void;
  onOpenError?: (account: Account) => void;
  onOpenNotes?: (account: Account) => void;
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
  const displayedStatusTone = statusTone === "error" ? statusTone : rateLimited ? rateLimitStatusTone : statusTone;
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
        <CompactAccountIdentity accountName={account.account_name} email={account.email} />
        <div className="compact-row-badges">
          {account.is_duplicate ? <Badge tone="warn">重复</Badge> : null}
          <Badge tone={statusTone}>{statusText}</Badge>
        </div>
      </div>
    );
  }
  return (
    <tr className={rowClass} id={id}>
      <td>
        <label className="table-check account-select-check" title={canSelect ? "选中后可批量测活或删除" : "此账号缺少可选标识"}>
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
        <div className="account-identity-cell">
          <CopyTextButton className="account-identity-copy-button account-name-copy-button" title="复制账号名称" value={account.account_name} />
          <CopyTextButton className="account-identity-copy-button account-email-copy-button mono" title="复制账号邮箱" value={account.email} />
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
      <td className="account-id-cell mono muted" title={account.management_account_id || undefined}>{account.management_account_id || "-"}</td>
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
            <Badge className="status-column-badge" tone={displayedStatusTone}>{rateLimited ? rateLimitStatusText : statusText}</Badge>
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
          <button
            className="error-pill-button"
            onClick={() => onOpenError?.(account)}
            title={errorSummary.title || "查看完整错误详情"}
            type="button"
          >
            <Badge className="error-pill-badge" tone={errorSummary.tone}>{errorSummary.label}</Badge>
          </button>
        ) : (
          "-"
        )}
      </td>
      <td className="right sticky-action-cell">
        <div className="row-actions account-row-actions">
          {mailbox ? (
            <button className="icon-button" disabled={busy} onClick={() => onOpenMailbox?.(mailbox)} title="查看邮件" type="button">
              <MailOpen size={16} />
            </button>
          ) : null}
          <button className="icon-button" disabled={busy} onClick={() => onOpenPhone?.(account)} title="查看手机号" type="button">
            <Smartphone size={16} />
          </button>
          <button
            aria-label={`查看 ${account.account_name || account.email} 的备注`}
            className="icon-button"
            disabled={busy || !account.management_account_id}
            onClick={() => onOpenNotes?.(account)}
            title="查看备注"
            type="button"
          >
            <StickyNote size={16} />
          </button>
          <button
            aria-label={`编辑 ${account.account_name || account.email}`}
            className="icon-button"
            disabled={busy || !account.management_account_id || !onEdit}
            onClick={() => onEdit?.(account)}
            onFocus={() => void loadAccountEditorDialog()}
            onMouseEnter={() => void loadAccountEditorDialog()}
            title="编辑账号"
            type="button"
          >
            <Pencil size={16} />
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
              disabled={busy || (!account.can_delete_remote && !account.delete_unlockable) || !account.management_account_id || !onDeleteRemote}
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
    { key: "imported", icon: Database, label: "导入管理站点", value: account.management_site_imported_at },
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

function AccountSubscriptionCell({ account, timeZone }: { account: Account; timeZone: string }) {
  const period = account.subscription_billing_period ? periodLabel(account.subscription_billing_period) : null;
  const activeTone = subscriptionIsInvalid(account) ? "warn" : account.has_active_subscription === true ? "ok" : "ink";
  const planLabelText = accountSubscriptionTypeLabel(account);
  if (!account.subscription_expires_at && !account.subscription_starts_at) {
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
    </div>
  );
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
      {primaryText ? (
        isPhoneUrlSource(primaryText)
          ? <MiddleEllipsisText className="mono phone-source-primary" text={primaryText} />
          : <span className="mono phone-source-primary">{primaryText}</span>
      ) : null}
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
  displayValue,
  className = "",
  title = "复制",
  copiedLabel = "已复制",
  hideIcon = false,
  middleEllipsis = false,
}: {
  value: string;
  displayValue?: string;
  className?: string;
  title?: string;
  copiedLabel?: string;
  hideIcon?: boolean;
  middleEllipsis?: boolean;
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
      {middleEllipsis
        ? <MiddleEllipsisText text={displayValue ?? value} />
        : <span>{displayValue ?? value}</span>}
      {copied ? <span className="copy-feedback">{copiedLabel}</span> : hideIcon ? null : <Copy size={13} />}
    </button>
  );
}

function CompactAccountIdentity({
  accountName,
  email,
  className = "",
}: {
  accountName: string | null | undefined;
  email: string;
  className?: string;
}) {
  const name = accountName?.trim() || email;
  const showEmailSeparately = name.toLowerCase() !== email.toLowerCase();
  return (
    <div className={["compact-account-identity", className].filter(Boolean).join(" ")}>
      <CopyTextButton className="compact-identity-copy compact-identity-name" hideIcon title="复制账号名称" value={name} />
      {showEmailSeparately ? (
        <>
          <span aria-hidden="true" className="compact-identity-separator">|</span>
          <CopyTextButton className="compact-identity-copy compact-identity-email mono" hideIcon title="复制账号邮箱" value={email} />
        </>
      ) : null}
    </div>
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

function Empty({ label }: { label: string }) {
  return <div className="empty">{label}</div>;
}

function SignalLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="signal-row">
      <span>{label}</span>
      <strong>{isPhoneUrlSource(value) ? <MiddleEllipsisText text={value} /> : value}</strong>
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

function formatPercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  const normalized = Math.max(0, Math.min(value, 100));
  return `${Number.isInteger(normalized) ? normalized.toFixed(0) : normalized.toFixed(1)}%`;
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

function cloneUsageLimitPlanRanges(value: UsageLimitPlanRanges): UsageLimitPlanRanges {
  return {
    five_hour: { ...value.five_hour },
    seven_day: { ...value.seven_day },
    monthly: { ...value.monthly },
  };
}

function toSub2ApiInstanceUrl(value: string) {
  const text = value.trim().replace(/\/+$/, "");
  if (!text) return "";
  return text.toLowerCase().endsWith(sub2ApiApiPrefix) ? text.slice(0, -sub2ApiApiPrefix.length).replace(/\/+$/, "") : text;
}

function accountRowKey(account: Account) {
  return `${account.email}:${account.management_account_id || account.id}:${account.duplicate_rank}`;
}

function usageForAccount(
  account: Pick<Account, "email" | "management_account_id">,
  usageByAccountId: Map<string, AccountUsageEstimate>,
  usageByEmail: Map<string, AccountUsageEstimate>,
) {
  const accountId = account.management_account_id?.trim();
  if (accountId) {
    return usageByAccountId.get(accountId);
  }
  return usageByEmail.get(account.email.toLowerCase());
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

function accountRateLimited(account: Account | AccountUsageEstimate, usage?: AccountUsageEstimate) {
  return Boolean(account.rate_limited || usage?.rate_limited || usage?.five_hour.rate_limited || usage?.seven_day.rate_limited);
}

function accountShowsRateLimit(account: Account, usage?: AccountUsageEstimate) {
  return accountRateLimitShouldBeVisible(account, usage, accountHasError(account));
}

function accountEstimateExcludedByError(account: Account, usage?: AccountUsageEstimate) {
  return accountEstimateHasEffectiveError(account, usage, accountHasError(account));
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

function accountSubscriptionTypeLabel(
  account: Pick<
    Account,
    "account_type" | "has_active_subscription" | "platform" | "subscription_label" | "subscription_plan" | "subscription_type"
  >,
) {
  const plan = account.subscription_type
    ? subscriptionTypeLabel(account.subscription_type)
    : account.subscription_label || planLabel(account.subscription_plan || account.account_type || account.platform || "未知");
  return subscriptionIsInvalid(account) ? "订阅无效" : plan === "active" ? "正常" : plan;
}

function subscriptionIsInvalid(
  account: Pick<Account, "has_active_subscription" | "subscription_plan" | "subscription_type">,
) {
  const subscriptionType = normalizeSubscriptionType(account.subscription_type || account.subscription_plan);
  return account.has_active_subscription === false && !nonExpiringSubscriptionTypes.has(subscriptionType);
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

function accountErrorSummary(
  account: Pick<Account, "remote_error" | "last_error" | "management_site_error_code" | "management_site_error_message">,
): { label: string; tone: string; title?: string } | null {
  const remoteCode = account.management_site_error_code;
  const detail = String(account.management_site_error_message || account.last_error || "").trim();
  const text = `${account.management_site_error_message || ""} ${account.last_error || ""}`.toLowerCase();
  if (isOAuthPhoneVerificationStopped(account.management_site_error_message, account.last_error)) {
    return {
      label: "手机验证已终止",
      tone: "warn",
      title: "尝试重新 OAuth，但遇到手机验证码而终止",
    };
  }
  if (remoteCode) {
    return { label: `管理站点 ${remoteCode}`, tone: "error" };
  }
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
  if (accountShowsRateLimit(account, usage)) return "限流窗口排除";
  return "参与";
}

function accountUsageEstimateToggleTitle(account: Account, usage?: AccountUsageEstimate) {
  if (account.deactive) return "封禁账号不参与总额度估算";
  if (accountEstimateExcludedByError(account, usage)) return "错误状态账号不参与总额度估算";
  if (!account.usage_estimate_enabled) return "不参与总额度估算";
  if (accountShowsRateLimit(account, usage)) return "账号当前限流，仅限流窗口不参与对应周期总额度估算";
  return "参与总额度估算";
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

function accountCanBeSelectedForDeletion(account: Account) {
  return Boolean(account.management_account_id || account.id > 0);
}

function useDisplayTimeZone() {
  return useContext(TimeZoneContext);
}

function useNow() {
  return useContext(NowContext);
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

export { type AccountCounts, AccountQuotaCell, AccountRow, AccountSubscriptionCell, AccountTimeRecordsCell, Badge, CompactAccountIdentity, CopyTextButton, Empty, MailMessageDialog, PanelTitle, PhoneSourceDetails, QuotaHistoryCell, SearchBox, SignalLine, accountCanBeSelectedForDeletion, accountDisplayRateLimitDetails, accountDisplayRateLimitedWindowKeys, accountErrorSummary, accountEstimateExcludedByError, accountHasError, accountIsManuallyPaused, accountRateLimited, accountRateLimitedWindowKeys, accountRowKey, accountScheduleLabel, accountScheduleTone, accountShowsRateLimit, accountStatusLabel, accountStatusTone, accountSubscriptionTypeLabel, accountUsageEstimateToggleLabel, accountUsageEstimateToggleTitle, accountWindowRefreshTags, clampPercentValue, cloneUsageLimitPlanRanges, copyTextToClipboard, defaultTimeZone, downloadTextFile, extractVerificationCode, formatAggregateMoney, formatDate, formatDuration, formatFullDate, formatMoney, formatPercent, formatRateLimitRecovery, formatRemainingDuration, formatShortDate, formatTokenCount, formatTokenHistoryCountLabel, formatTokenHistoryTotalLabel, formatTokenWindowLabel, formatTokenWindowLimit, formatTokenWindowSummary, formatWindowRefreshTime, formatWindowRefreshTitle, formatWindowRemaining, isDeactivatedAccount, isManualPhoneSource, isPhoneUrlSource, loadAccountEditorDialog, mailVerificationCode, nonExpiringSubscriptionTypes, normalizeRateLimitedWindowKey, normalizeSearch, normalizeSubscriptionType, parseApiDate, periodLabel, phoneSourceLabel, phoneSourceTags, planLabel, primaryPhoneSourceText, quotaUsedPercent, rateLimitDetailsForWindows, rateLimitedWindowLabel, rateLimitedWindowTone, sub2ApiApiPrefix, subscriptionIsInvalid, subscriptionTypeLabel, textMatchesSearch, toSub2ApiInstanceUrl, tokenWindowKindLabel, usageForAccount, usageLimitWindowKeys, usageSubscriptionSortRank, useDisplayTimeZone, useNow, windowResetRemainingSeconds, windowUsedAmount, windowUsedLabel };
