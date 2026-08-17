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
  accountCanBeLivenessTested,
  livenessAccountIds,
  MAX_LIVENESS_ACCOUNTS,
} from "../../accountLiveness";
import { accountFilterFacetCandidates } from "../../accountFilterFacets";
import { sortAccountsForTable } from "../../accountTableSort";
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
import { AccountRow, Badge, CopyTextButton, Empty, MailMessageDialog, PanelTitle, PhoneSourceDetails, SearchBox, accountCanBeSelectedForDeletion, accountDisplayRateLimitedWindowKeys, accountErrorSummary, accountHasError, accountRateLimitedWindowKeys, accountRowKey, accountShowsRateLimit, accountStatusLabel, accountSubscriptionTypeLabel, isDeactivatedAccount, loadAccountEditorDialog, normalizeSearch, rateLimitedWindowLabel, textMatchesSearch, usageForAccount, usageSubscriptionSortRank } from "../shared/LegacyDisplay";

const AccountEditorDialog = lazy(async () => ({
  default: (await loadAccountEditorDialog()).AccountEditorDialog,
}));

type AccountStatusFilter = "all" | "normal" | "normal-no-rate-limit" | "five-hour-rate-limited" | "seven-day-rate-limited" | "monthly-rate-limited" | "error" | "deactive";

type AccountSortField = "account" | "imported_at";

type SortDirection = "asc" | "desc";

type AccountJumpTarget = { email: string | null; managementAccountId: string | null; requestedAt: number };

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

export function AccountsView({
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
  onAccountEdited,
  onNotice,
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
  onAccountEdited: (message: string) => void;
  onNotice: (message: string) => void;
  onToggleDeleteUnlock: (account: Account, unlocked: boolean) => void;
  onToggleRefreshLock: (account: Account, unlocked: boolean) => void;
  onRefresh: (email: string) => void;
  onToggleUsageEstimate: (id: number, enabled: boolean) => void;
}) {
  const problemAccountCount = accounts.filter(canBulkDeleteProblemAccount).length;
  const [accountSortField, setAccountSortField] = useState<AccountSortField>("account");
  const [accountSortDirection, setAccountSortDirection] = useState<SortDirection>("asc");
  const orderedAccounts = useMemo(
    () => sortAccountsForTable(accounts, accountSortField, accountSortDirection),
    [accountSortDirection, accountSortField, accounts],
  );
  const [accountSearch, setAccountSearch] = useState("");
  const [accountStatusFilter, setAccountStatusFilter] = useState<AccountStatusFilter>("all");
  const [accountSubscriptionFilter, setAccountSubscriptionFilter] = useState("");
  const [accountGroupFilter, setAccountGroupFilter] = useState("");
  const [selectedAccountKeys, setSelectedAccountKeys] = useState<Record<string, boolean>>({});
  const [livenessAccounts, setLivenessAccounts] = useState<Account[] | null>(null);
  const [sessionDeleteUnlocks, setSessionDeleteUnlocks] = useState<Record<string, boolean>>({});
  const [selectedMailbox, setSelectedMailbox] = useState<Mailbox | null>(null);
  const [folder, setFolder] = useState<"inbox" | "junk">("inbox");
  const [messages, setMessages] = useState<MailMessage[]>([]);
  const [mailLoading, setMailLoading] = useState(false);
  const [mailError, setMailError] = useState("");
  const [selectedPhoneAccount, setSelectedPhoneAccount] = useState<Account | null>(null);
  const [selectedErrorAccount, setSelectedErrorAccount] = useState<Account | null>(null);
  const [editingAccounts, setEditingAccounts] = useState<Account[] | null>(null);
  const [notesAccount, setNotesAccount] = useState<Account | null>(null);
  const [highlightedAccountKey, setHighlightedAccountKey] = useState<string | null>(null);
  const handledJumpRequestRef = useRef<number | null>(null);
  const livenessTriggerRef = useRef<HTMLButtonElement | null>(null);
  const toggleAccountSort = (field: AccountSortField) => {
    if (accountSortField === field) {
      setAccountSortDirection((current) => current === "asc" ? "desc" : "asc");
      return;
    }
    setAccountSortField(field);
    setAccountSortDirection("asc");
  };
  const mailboxByEmail = useMemo(() => {
    const entries = mailboxes
      .filter((mailbox) => !mailbox.disabled)
      .map((mailbox) => [mailbox.gpt_email.toLowerCase(), mailbox] as const);
    return new Map(entries);
  }, [mailboxes]);
  const accountFilterCandidates = useMemo(
    () =>
      accountFilterFacetCandidates(
        orderedAccounts,
        accountStatusFilter,
        accountSubscriptionFilter,
        (account, filter) => accountMatchesStatusFilter(account, usageForAccount(account, usageByAccountId, usageByEmail), filter),
        accountSubscriptionTypeLabel,
      ),
    [accountStatusFilter, accountSubscriptionFilter, orderedAccounts, usageByAccountId, usageByEmail],
  );
  const accountStatusFilterOptions = useMemo(
    () => availableAccountStatusFilterOptions(accountFilterCandidates.statusOptionAccounts, usageByAccountId, usageByEmail),
    [accountFilterCandidates.statusOptionAccounts, usageByAccountId, usageByEmail],
  );
  const accountSubscriptionFilterOptions = useMemo(
    () => availableAccountSubscriptionFilterOptions(accountFilterCandidates.subscriptionOptionAccounts),
    [accountFilterCandidates.subscriptionOptionAccounts],
  );
  const accountGroupFilterOptions = useMemo(
    () => availableAccountGroupFilterOptions(orderedAccounts),
    [orderedAccounts],
  );
  const filteredAccounts = useMemo(
    () =>
      accountFilterCandidates.filteredAccounts.filter((account) => accountMatchesGroupFilter(account, accountGroupFilter)).filter((account) => {
        const usage = usageForAccount(account, usageByAccountId, usageByEmail);
        return textMatchesSearch(
          [
            account.account_name,
            account.email,
            account.management_account_id,
            account.management_site_imported_at,
            account.last_seen_at,
            account.updated_at,
            account.platform,
            account.account_type,
            account.status,
            account.management_site_error_code,
            account.management_site_error_message,
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
            (account.groups || []).map((group) => `${group.name} ${group.id}`).join(" "),
            account.notes,
            account.last_error,
            mailboxByEmail.get(account.email.toLowerCase())?.mailbox_email,
            mailboxByEmail.get(account.email.toLowerCase())?.provider,
            account.phone_number,
            account.phone_sms_url,
            account.phone_sms_cdk,
            account.phone_sms_recharge_url,
          ],
          accountSearch,
        );
      }),
    [accountFilterCandidates.filteredAccounts, accountGroupFilter, accountSearch, mailboxByEmail, usageByAccountId, usageByEmail],
  );
  const selectedAccounts = accounts.filter((account) => selectedAccountKeys[accountRowKey(account)]);
  const selectedAccountCount = selectedAccounts.length;
  const selectedLivenessAccounts = selectedAccounts.filter(accountCanBeLivenessTested);
  const selectedLivenessAccountCount = selectedLivenessAccounts.length;
  const oauthAccountTotal = accounts.filter(accountCanBeLivenessTested).length;
  const currentLivenessAccounts = filteredAccounts.filter(accountCanBeLivenessTested);
  const selectableCurrentLivenessAccounts = currentLivenessAccounts.slice(0, MAX_LIVENESS_ACCOUNTS);
  const allCurrentLivenessAccountsSelected = Boolean(
    selectableCurrentLivenessAccounts.length
    && selectableCurrentLivenessAccounts.every((account) => selectedAccountKeys[accountRowKey(account)]),
  );
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
    if (accountSubscriptionFilter && !accountSubscriptionFilterOptions.some((option) => option.value === accountSubscriptionFilter)) {
      setAccountSubscriptionFilter("");
      return;
    }
    if (!accountStatusFilterOptions.some((option) => option.value === accountStatusFilter)) {
      setAccountStatusFilter("all");
    }
  }, [accountStatusFilter, accountStatusFilterOptions, accountSubscriptionFilter, accountSubscriptionFilterOptions]);

  useEffect(() => {
    if (!accountJumpTarget || handledJumpRequestRef.current === accountJumpTarget.requestedAt) return;
    if (!orderedAccounts.length) return;

    handledJumpRequestRef.current = accountJumpTarget.requestedAt;
    const targetAccount = findAccountJumpTarget(orderedAccounts, accountJumpTarget);
    const searchText = accountJumpSearchText(targetAccount, accountJumpTarget);

    setAccountStatusFilter("all");
    setAccountSubscriptionFilter("");
    setAccountGroupFilter("");

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
    if (busy || !account.management_account_id) return;
    const sessionDeleteUnlocked = Boolean(sessionDeleteUnlocks[accountRowKey(account)]);
    const needsSessionUnlock = account.delete_unlockable && !sessionDeleteUnlocked;
    if (needsSessionUnlock) {
      setSessionDeleteUnlocks((current) => ({ ...current, [accountRowKey(account)]: true }));
      return;
    }
    if (!account.can_delete_remote && !account.delete_unlockable) return;
    const kind = deleteAccountKindLabel(account);
    if (window.confirm(`确定删除 ${account.email} 的 ${kind}（管理站点账号 ID: ${account.management_account_id}）吗？`)) {
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

  const toggleCurrentLivenessAccounts = (selected: boolean) => {
    setSelectedAccountKeys((current) => {
      const next = { ...current };
      let selectedEligibleCount = accounts.filter(
        (account) => accountCanBeLivenessTested(account) && current[accountRowKey(account)],
      ).length;
      for (const account of currentLivenessAccounts) {
        const key = accountRowKey(account);
        if (!selected) {
          delete next[key];
        } else if (!next[key] && selectedEligibleCount < MAX_LIVENESS_ACCOUNTS) {
          next[key] = true;
          selectedEligibleCount += 1;
        }
      }
      return next;
    });
  };

  const closeLivenessDialog = useCallback(() => {
    setLivenessAccounts(null);
    window.requestAnimationFrame(() => livenessTriggerRef.current?.focus());
  }, []);

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
            <PanelTitle title="OAuth 账号状态" icon={UsersRound} />
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
              <AccountGroupFilterMenu
                onChange={setAccountGroupFilter}
                options={accountGroupFilterOptions}
                value={accountGroupFilter}
              />
            </div>
          </div>
          <div className="toolbar-actions">
            <button
              className="primary-button"
              disabled={
                busy
                || !selectedLivenessAccountCount
                || selectedLivenessAccountCount > MAX_LIVENESS_ACCOUNTS
              }
              onClick={(event) => {
                livenessTriggerRef.current = event.currentTarget;
                setLivenessAccounts(selectedLivenessAccounts);
              }}
              title={
                selectedLivenessAccountCount > MAX_LIVENESS_ACCOUNTS
                  ? `单次最多测试 ${MAX_LIVENESS_ACCOUNTS} 个账号，请减少选择`
                  : selectedLivenessAccountCount
                    ? `测试 ${selectedLivenessAccountCount} 个所选 OAuth GPT 账号`
                    : "请先勾选 OAuth GPT 账号"
              }
              type="button"
            >
              <Activity size={17} />
              <span>测活 ({selectedLivenessAccountCount}/{MAX_LIVENESS_ACCOUNTS})</span>
            </button>
            <button
              className="secondary-button"
              disabled={busy || selectedLivenessAccountCount < 2}
              onClick={() => setEditingAccounts(selectedLivenessAccounts)}
              onFocus={() => void loadAccountEditorDialog()}
              onMouseEnter={() => void loadAccountEditorDialog()}
              title={selectedLivenessAccountCount < 2 ? "请至少勾选 2 个 OAuth GPT 账号" : `批量编辑 ${selectedLivenessAccountCount} 个 OAuth GPT 账号`}
              type="button"
            >
              <Pencil size={17} />
              <span>批量编辑{selectedLivenessAccountCount ? ` (${selectedLivenessAccountCount})` : ""}</span>
            </button>
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
                <th className="accounts-select-header">
                  <label
                    className="table-check account-select-check"
                    title={
                      currentLivenessAccounts.length > MAX_LIVENESS_ACCOUNTS
                        ? `选择当前筛选结果中的前 ${MAX_LIVENESS_ACCOUNTS} 个 OAuth GPT 账号`
                        : "选择当前筛选结果中的 OAuth GPT 账号"
                    }
                  >
                    <input
                      aria-label={`选择当前筛选结果中的 OAuth GPT 账号，单次最多 ${MAX_LIVENESS_ACCOUNTS} 个`}
                      checked={allCurrentLivenessAccountsSelected}
                      disabled={busy || !currentLivenessAccounts.length}
                      onChange={(event) => toggleCurrentLivenessAccounts(event.currentTarget.checked)}
                      type="checkbox"
                    />
                  </label>
                </th>
                <th aria-sort={accountSortField === "account" ? (accountSortDirection === "asc" ? "ascending" : "descending") : "none"}>
                  <div className="account-header-with-selection">
                    <AccountTableSortButton
                      active={accountSortField === "account"}
                      direction={accountSortDirection}
                      label="账号"
                      onClick={() => toggleAccountSort("account")}
                    />
                    <span className="account-selection-summary" role="status">已选 {selectedLivenessAccountCount}/{oauthAccountTotal}</span>
                  </div>
                </th>
                <th>管理站点账号 ID</th>
                <th aria-sort={accountSortField === "imported_at" ? (accountSortDirection === "asc" ? "ascending" : "descending") : "none"}>
                  <AccountTableSortButton
                    active={accountSortField === "imported_at"}
                    direction={accountSortDirection}
                    label="时间记录"
                    onClick={() => toggleAccountSort("imported_at")}
                  />
                </th>
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
                  onEdit={(target) => setEditingAccounts([target])}
                  onOpenNotes={setNotesAccount}
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
      {livenessAccounts ? <AccountLivenessDialog accounts={livenessAccounts} onClose={closeLivenessDialog} /> : null}
      {notesAccount ? <AccountNotesDialog account={notesAccount} onClose={() => setNotesAccount(null)} onUpdated={onAccountEdited} /> : null}
      {editingAccounts ? (
        <Suspense fallback={null}>
          <AccountEditorDialog
            accounts={editingAccounts}
            onClose={() => setEditingAccounts(null)}
            onNotice={onNotice}
            onUpdated={onAccountEdited}
          />
        </Suspense>
      ) : null}
    </>
  );
}

function AccountTableSortButton({
  active,
  direction,
  label,
  onClick,
}: {
  active: boolean;
  direction: SortDirection;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={`按${label}${active && direction === "asc" ? "降序" : "升序"}排列`}
      className={active ? "account-table-sort active" : "account-table-sort"}
      onClick={onClick}
      title={`切换${label}排序`}
      type="button"
    >
      <span>{label}</span>
      {!active ? <ArrowUpDown size={14} /> : direction === "asc" ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
    </button>
  );
}

function AccountLivenessDialog({ accounts, onClose }: { accounts: Account[]; onClose: () => void }) {
  const accountIds = useMemo(() => livenessAccountIds(accounts), [accounts]);
  const dialogRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const modelsAbortRef = useRef<AbortController | null>(null);
  const testAbortRef = useRef<AbortController | null>(null);
  const [models, setModels] = useState<AccountLivenessModel[]>([]);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [sourceAccountId, setSourceAccountId] = useState("");
  const [modelsLoading, setModelsLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<AccountLivenessTestResult | null>(null);
  const titleId = "account-liveness-dialog-title";
  const sourceAccount = accounts.find(
    (account) => String(account.management_account_id || "") === sourceAccountId,
  );

  const abortRequests = useCallback(() => {
    const modelsController = modelsAbortRef.current;
    const testController = testAbortRef.current;
    modelsAbortRef.current = null;
    testAbortRef.current = null;
    modelsController?.abort();
    testController?.abort();
  }, []);

  const closeDialog = useCallback(() => {
    abortRequests();
    onClose();
  }, [abortRequests, onClose]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDialog();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
      abortRequests();
    };
  }, [abortRequests, closeDialog]);

  useEffect(() => {
    const controller = new AbortController();
    modelsAbortRef.current?.abort();
    modelsAbortRef.current = controller;
    setModelsLoading(true);
    setError("");
    setSourceAccountId("");
    api.accountLivenessModels(accountIds, controller.signal)
      .then((response) => {
        if (modelsAbortRef.current !== controller || controller.signal.aborted) return;
        setModels(response.models);
        setSourceAccountId(response.source_account_id);
        setSelectedModelId((current) => response.models.some((model) => model.id === current) ? current : response.models[0]?.id || "");
      })
      .catch((reason) => {
        if (modelsAbortRef.current === controller && !isAbortError(reason)) {
          setError(reason instanceof Error ? reason.message : "模型列表读取失败");
        }
      })
      .finally(() => {
        if (modelsAbortRef.current === controller) {
          modelsAbortRef.current = null;
          setModelsLoading(false);
        }
      });
    return () => {
      controller.abort();
      if (modelsAbortRef.current === controller) modelsAbortRef.current = null;
    };
  }, [accountIds]);

  const startTest = async () => {
    if (testing || !selectedModelId || !accountIds.length) return;
    const controller = new AbortController();
    testAbortRef.current?.abort();
    testAbortRef.current = controller;
    setTesting(true);
    setError("");
    setResult(null);
    try {
      const nextResult = await api.testAccountLiveness(accountIds, selectedModelId, controller.signal);
      if (testAbortRef.current === controller && !controller.signal.aborted) setResult(nextResult);
    } catch (reason) {
      if (testAbortRef.current === controller && !isAbortError(reason)) {
        setError(reason instanceof Error ? reason.message : "账号测活失败");
      }
    } finally {
      if (testAbortRef.current === controller) {
        testAbortRef.current = null;
        setTesting(false);
      }
    }
  };

  const liveMessage = error
    || (testing ? `正在测试 ${accountIds.length} 个账号` : "")
    || (result ? `测活完成，成功 ${result.succeeded} 个，失败 ${result.failed} 个` : "");

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) closeDialog();
      }}
      role="presentation"
    >
      <section
        aria-busy={testing}
        aria-labelledby={titleId}
        aria-modal="true"
        className="mail-dialog liveness-dialog"
        ref={dialogRef}
        role="dialog"
      >
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">{accountIds.length} 个 OAuth GPT 账号</p>
            <h2 id={titleId}>批量账号测活</h2>
          </div>
          <button
            aria-label="关闭账号测活"
            className="icon-button"
            onClick={closeDialog}
            ref={closeButtonRef}
            title="关闭"
            type="button"
          >
            <X size={17} />
          </button>
        </header>

        <span aria-atomic="true" aria-live="polite" className="sr-only">{liveMessage}</span>

        <div className="liveness-controls">
          <label className="liveness-model-field">
            <span>测试模型</span>
            <select
              disabled={modelsLoading || testing || !models.length}
              onChange={(event) => {
                setSelectedModelId(event.currentTarget.value);
                setResult(null);
              }}
              value={selectedModelId}
            >
              {modelsLoading ? <option value="">读取中...</option> : null}
              {!modelsLoading && !models.length ? <option value="">无可用模型</option> : null}
              {models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.display_name === model.id ? model.id : `${model.display_name} · ${model.id}`}
                </option>
              ))}
            </select>
            {sourceAccountId ? (
              <small className="liveness-model-source">
                模型来源：{sourceAccount?.account_name || sourceAccount?.email || "OAuth GPT 账号"} · #{sourceAccountId}
              </small>
            ) : null}
          </label>
          <button className="primary-button liveness-start-button" disabled={testing || modelsLoading || !selectedModelId} onClick={startTest} type="button">
            {testing ? <RefreshCcw className="spin" size={17} /> : <Activity size={17} />}
            <span>{testing ? `正在测试 ${accountIds.length} 个账号` : result ? "重新测试" : "开始测试"}</span>
          </button>
        </div>

        {error ? <div className="mail-error" role="alert">{error}</div> : null}
        {testing ? (
          <div className="liveness-running" role="status">
            <RefreshCcw className="spin" size={20} />
            <span>管理站点正在逐批测试连接...</span>
          </div>
        ) : null}

        {result ? (
          <>
            <div className="liveness-summary" aria-label="测活结果汇总" aria-live="polite">
              <div><span>总数</span><strong>{result.total}</strong></div>
              <div><span>成功</span><strong className="liveness-success-text">{result.succeeded}</strong></div>
              <div><span>失败</span><strong className="liveness-failure-text">{result.failed}</strong></div>
              <div><span>模型</span><strong className="mono">{result.model_id}</strong></div>
            </div>
            <div className="table-wrap liveness-results">
              <table>
                <thead>
                  <tr>
                    <th>账号</th>
                    <th>管理站点账号 ID</th>
                    <th>结果</th>
                    <th>耗时</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.map((item) => {
                    const email = item.email?.trim() || "";
                    const accountName = item.account_name?.trim() || email || `账号 #${item.account_id}`;
                    const showEmailSeparately = Boolean(email && email.toLowerCase() !== accountName.toLowerCase());
                    return (
                      <tr key={item.account_id}>
                        <td>
                          <div className="account-identity-cell liveness-account-identity">
                            <CopyTextButton
                              className="account-identity-copy-button account-name-copy-button"
                              title={item.account_name?.trim() ? "复制账号名称" : email ? "复制账号邮箱" : "复制账号"}
                              value={accountName}
                            />
                            {showEmailSeparately ? (
                              <CopyTextButton
                                className="account-identity-copy-button account-email-copy-button mono"
                                title="复制账号邮箱"
                                value={email}
                              />
                            ) : null}
                          </div>
                        </td>
                        <td className="mono muted">{item.account_id}</td>
                        <td><Badge tone={item.success ? "ok" : "deactive"}>{item.success ? "可用" : "失败"}</Badge></td>
                        <td className="mono muted">{item.duration_ms ? `${(item.duration_ms / 1000).toFixed(1)}s` : "-"}</td>
                        <td className={item.success ? "muted" : "liveness-error-text"}>{item.success ? "连接成功" : item.error || "测试失败"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        ) : null}
      </section>
    </div>
  );
}

function AccountNotesDialog({
  account,
  onClose,
  onUpdated,
}: {
  account: Account;
  onClose: () => void;
  onUpdated: (message: string) => Promise<void> | void;
}) {
  const accountId = account.management_account_id || "";
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const [detail, setDetail] = useState<AccountNotes | null>(null);
  const [notes, setNotes] = useState(account.notes || "");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api.accountNotes(accountId, controller.signal)
      .then((result) => {
        setDetail(result);
        setNotes(result.notes);
      })
      .catch((reason) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) {
          setError(reason instanceof Error ? reason.message : "备注读取失败");
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [accountId]);

  useEffect(() => {
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose, saving]);

  const saveNotes = (event: FormEvent) => {
    event.preventDefault();
    if (!detail || saving) return;
    setSaving(true);
    setError("");
    api.updateAccountNotes(accountId, notes, detail.identity_fingerprint)
      .then(async (result) => {
        setDetail(result);
        setNotes(result.notes);
        await onUpdated("备注已写回管理站点，将在后续同步中保持一致。");
        onClose();
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "备注写回失败"))
      .finally(() => setSaving(false));
  };

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onClose();
      }}
      role="presentation"
    >
      <section aria-busy={loading || saving} aria-labelledby="account-notes-title" aria-modal="true" className="mail-dialog account-notes-dialog" role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">管理站点 #{accountId}</p>
            <h2 id="account-notes-title">OAuth 账号备注</h2>
            <p className="account-notes-account">{detail?.account_name || account.account_name || account.email}</p>
          </div>
          <button aria-label="关闭备注" className="icon-button" disabled={saving} onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        {error ? <div className="mail-error" role="alert">{error}</div> : null}
        {loading ? (
          <div className="account-notes-loading"><RefreshCcw className="spin" size={18} />正在读取管理站点备注...</div>
        ) : (
          <form className="account-notes-form" onSubmit={saveNotes}>
            <label>
              <span>备注内容</span>
              <textarea
                autoFocus
                maxLength={10_000}
                onChange={(event) => setNotes(event.currentTarget.value)}
                placeholder="暂无备注"
                rows={10}
                value={notes}
              />
            </label>
            <footer className="account-notes-footer">
              <span>{notes.length}/10000</span>
              <button className="primary-button" disabled={!detail || saving} type="submit">
                {saving ? <RefreshCcw className="spin" size={17} /> : <Save size={17} />}
                <span>{saving ? "写回中" : "保存备注"}</span>
              </button>
            </footer>
          </form>
        )}
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

function AccountErrorDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const errorSummary = accountErrorSummary(account);
  const remoteMessage = String(account.management_site_error_message || "").trim();
  const localMessage = String(account.last_error || "").trim();
  const distinctLocalMessage = localMessage && localMessage !== remoteMessage ? localMessage : "";
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
            {errorSummary ? (
              <span title={errorSummary.title}>
                <Badge tone={errorSummary.tone}>{errorSummary.label}</Badge>
              </span>
            ) : <span className="muted">无</span>}
          </div>
          <div className="phone-dialog-card">
            <strong>当前状态</strong>
            <span>{account.status || "-"}</span>
          </div>
          <div className="phone-dialog-card">
            <strong>管理站点状态码</strong>
            <span>{account.management_site_error_code || "-"}</span>
          </div>
        </div>
        <div className="phone-dialog-card error-dialog-body">
          <strong>{remoteMessage ? "管理站点报错" : "完整报错"}</strong>
          <pre>{remoteMessage || localMessage || (account.remote_error ? "管理站点 API 账号异常" : "无错误详情")}</pre>
        </div>
        {distinctLocalMessage ? (
          <div className="phone-dialog-card error-dialog-body">
            <strong>插件报错</strong>
            <pre>{distinctLocalMessage}</pre>
          </div>
        ) : null}
      </section>
    </div>
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

function AccountGroupFilterMenu({
  value,
  options,
  onChange,
}: {
  value: string;
  options: Array<QuickFilterOption<string>>;
  onChange: (value: string) => void;
}) {
  return <QuickFilterMenu ariaLabel="分组筛选选项" label="分组筛选" onChange={onChange} options={options} value={value} />;
}

function accountRowDomId(account: Account) {
  return `account-row-${account.id}`;
}

function findAccountJumpTarget(accounts: Account[], target: AccountJumpTarget) {
  const targetAccountId = normalizeSearch(target.managementAccountId);
  const targetEmail = normalizeSearch(target.email);

  if (targetAccountId) {
    const accountById = accounts.find((account) => normalizeSearch(account.management_account_id) === targetAccountId);
    if (accountById) return accountById;
  }

  if (targetEmail) {
    return accounts.find((account) => normalizeSearch(account.email) === targetEmail) || null;
  }

  return null;
}

function accountJumpSearchText(account: Account | null, target: AccountJumpTarget) {
  return account?.management_account_id || account?.email || target.managementAccountId?.trim() || target.email?.trim() || "";
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

function availableAccountGroupFilterOptions(accounts: Account[]): Array<QuickFilterOption<string>> {
  const options: Array<QuickFilterOption<string>> = [{ value: "", label: "全部分组" }];
  const seen = new Set<string>();
  let hasUngrouped = false;
  for (const account of accounts) {
    const groups = account.groups || [];
    if (!groups.length) {
      hasUngrouped = true;
      continue;
    }
    for (const group of groups) {
      if (seen.has(group.id)) continue;
      seen.add(group.id);
      options.push({ value: group.id, label: group.name });
    }
  }
  if (hasUngrouped) options.push({ value: "__ungrouped__", label: "未分组" });
  return options;
}

function accountMatchesGroupFilter(account: Account, filter: string) {
  const groups = account.groups || [];
  if (!filter) return true;
  if (filter === "__ungrouped__") return groups.length === 0;
  return groups.some((group) => group.id === filter);
}

function accountRateLimitedWindowsLabel(account: Account | AccountUsageEstimate, usage?: AccountUsageEstimate) {
  return accountDisplayRateLimitedWindowKeys(account, usage).map((window) => rateLimitedWindowLabel(window, usage)).join("/");
}

function canBulkDeleteProblemAccount(account: Account) {
  return isDeactivatedAccount(account) || (account.can_delete_remote && account.is_duplicate);
}

function isAbortError(reason: unknown) {
  return reason instanceof DOMException && reason.name === "AbortError";
}
