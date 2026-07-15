import {
  AlertTriangle,
  ArrowRight,
  BadgeDollarSign,
  CalendarDays,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Globe2,
  History,
  KeyRound,
  LayoutGrid,
  Pencil,
  Power,
  PowerOff,
  Radar,
  RefreshCcw,
  Save,
  Search,
  Settings2,
  Trash2,
  TrendingDown,
  TrendingUp,
  UsersRound,
  WalletCards,
  X,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api";
import { buildUpstreamAccountUpdatePayload, canSetManualMultiplier } from "./upstreamAccountForm";
import { rateChangeReasonLabel, upstreamStatusLabel, upstreamStatusTone } from "./upstreamLabels";
import {
  accountBillingRateChange,
  groupRateChange,
  normalizedUpstreamMultiplier,
  upstreamRateChange,
  upstreamRechargeRateChange,
} from "./upstreamRatePresentation";
import {
  apiAccountSyncMessage,
  accountRateStatusLabel,
  channelDiscoveryErrorMessage,
  channelDiscoverySuccessMessage,
  upstreamDiscoveryCopy,
  upstreamMutationControlsDisabled,
} from "./upstreamSyncPresentation";
import type {
  UpstreamAccount,
  UpstreamChannel,
  UpstreamChannelsResponse,
  UpstreamChannelUpdate,
  UpstreamRateChangeLog,
  UpstreamType,
} from "./types";

type StatusFilter = "all" | "pending" | "attention" | "undiscovered";
type Subview = "manage" | "rate-log";
type RateLogFilters = { startDate: string; endDate: string };

type ChannelForm = {
  displayName: string;
  baseUrl: string;
  managementBaseUrl: string;
  upstreamType: UpstreamType;
  accessToken: string;
  clearAccessToken: boolean;
  refreshToken: string;
  clearRefreshToken: boolean;
  upstreamUserId: string;
  manualRechargeMultiplier: string;
};

type AccountForm = {
  channelId: string;
  apiKey: string;
  manualGroupMultiplier: string;
};

const emptyData: UpstreamChannelsResponse = {
  channels: [],
  unassigned_accounts: [],
};

const emptyChannelForm: ChannelForm = {
  displayName: "",
  baseUrl: "",
  managementBaseUrl: "",
  upstreamType: "auto",
  accessToken: "",
  clearAccessToken: false,
  refreshToken: "",
  clearRefreshToken: false,
  upstreamUserId: "",
  manualRechargeMultiplier: "",
};

const emptyAccountForm: AccountForm = {
  channelId: "",
  apiKey: "",
  manualGroupMultiplier: "",
};

export function ApiKeyAccountsView({
  cacheBaseUrl,
  cachedData,
  displayTimeZone,
  globallyBusy,
  onCacheChange,
  onBusyChange,
  rateWritesEnabled,
  refreshVersion,
}: {
  cacheBaseUrl: string;
  cachedData: UpstreamChannelsResponse | null;
  displayTimeZone: string;
  globallyBusy: boolean;
  onCacheChange: (data: UpstreamChannelsResponse, baseUrl: string) => void;
  onBusyChange: (busy: boolean) => void;
  rateWritesEnabled: boolean;
  refreshVersion: number;
}) {
  const [data, setData] = useState<UpstreamChannelsResponse>(cachedData || emptyData);
  const [loading, setLoading] = useState(!cachedData);
  const [refreshing, setRefreshing] = useState(Boolean(cachedData));
  const [bulkDiscovering, setBulkDiscovering] = useState(false);
  const [busyChannels, setBusyChannels] = useState<Record<string, string>>({});
  const [busyAccounts, setBusyAccounts] = useState<Record<string, string>>({});
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [subview, setSubview] = useState<Subview>("manage");
  const [rateLogs, setRateLogs] = useState<UpstreamRateChangeLog[]>([]);
  const [rateLogsLoaded, setRateLogsLoaded] = useState(false);
  const [rateLogsLoading, setRateLogsLoading] = useState(false);
  const [rateLogsError, setRateLogsError] = useState("");
  const [rateLogsHasMore, setRateLogsHasMore] = useState(false);
  const [rateLogDraftFilters, setRateLogDraftFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [rateLogFilters, setRateLogFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [editingChannel, setEditingChannel] = useState<UpstreamChannel | null>(null);
  const [channelForm, setChannelForm] = useState<ChannelForm>(emptyChannelForm);
  const [editingAccount, setEditingAccount] = useState<UpstreamAccount | null>(null);
  const [accountForm, setAccountForm] = useState<AccountForm>(emptyAccountForm);
  const [dialogError, setDialogError] = useState("");
  const [savingDialog, setSavingDialog] = useState(false);
  const [liveDataValidated, setLiveDataValidated] = useState(false);
  const requestSequence = useRef(0);
  const hasDataRef = useRef(Boolean(cachedData));
  const refreshVersionRef = useRef(refreshVersion);
  const dialogRef = useRef<HTMLElement | null>(null);
  const lastFocusedElementRef = useRef<HTMLElement | null>(null);
  const localMutationBusy = bulkDiscovering
    || Object.keys(busyChannels).length > 0
    || Object.keys(busyAccounts).length > 0;

  const loadData = useCallback(async (preserveFeedback = false) => {
    const sequence = ++requestSequence.current;
    setLiveDataValidated(false);
    if (hasDataRef.current) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    if (!preserveFeedback) setError("");
    try {
      const response = await api.upstreamChannels();
      if (sequence !== requestSequence.current) return;
      const normalized = {
        ...response,
        channels: Array.isArray(response.channels) ? response.channels : [],
        unassigned_accounts: Array.isArray(response.unassigned_accounts) ? response.unassigned_accounts : [],
      };
      hasDataRef.current = true;
      setData(normalized);
      onCacheChange(normalized, cacheBaseUrl);
      setLiveDataValidated(true);
    } catch (reason) {
      if (sequence === requestSequence.current) {
        setError(errorMessage(reason, "上游渠道读取失败"));
      }
    } finally {
      if (sequence === requestSequence.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [cacheBaseUrl, onCacheChange]);

  useEffect(() => {
    if (!cachedData || hasDataRef.current) return;
    hasDataRef.current = true;
    setData(cachedData);
    setLoading(false);
    setRefreshing(true);
  }, [cachedData]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (refreshVersionRef.current === refreshVersion) return;
    refreshVersionRef.current = refreshVersion;
    void loadData(true);
  }, [loadData, refreshVersion]);

  useEffect(() => {
    onBusyChange(localMutationBusy);
  }, [localMutationBusy, onBusyChange]);

  useEffect(() => () => onBusyChange(false), [onBusyChange]);

  const loadRateLogs = useCallback(async (append = false) => {
    setRateLogsLoading(true);
    setRateLogsError("");
    try {
      const beforeId = append && rateLogs.length ? rateLogs[rateLogs.length - 1].id : null;
      const next = await api.upstreamRateChangeLogs(50, beforeId, {
        startDate: rateLogFilters.startDate || undefined,
        endDate: rateLogFilters.endDate || undefined,
        timeZone: displayTimeZone,
      });
      setRateLogs((current) => append ? [...current, ...next] : next);
      setRateLogsHasMore(next.length === 50);
      setRateLogsLoaded(true);
    } catch (reason) {
      setRateLogsError(errorMessage(reason, "倍率变化记录读取失败"));
      setRateLogsLoaded(true);
    } finally {
      setRateLogsLoading(false);
    }
  }, [displayTimeZone, rateLogFilters, rateLogs]);

  const applyRateLogFilters = useCallback(() => {
    if (
      rateLogDraftFilters.startDate
      && rateLogDraftFilters.endDate
      && rateLogDraftFilters.startDate > rateLogDraftFilters.endDate
    ) {
      setRateLogsError("开始日期不能晚于结束日期");
      return;
    }
    setRateLogsError("");
    setRateLogs([]);
    setRateLogsHasMore(false);
    setRateLogFilters(rateLogDraftFilters);
    setRateLogsLoaded(false);
  }, [rateLogDraftFilters]);

  const clearRateLogFilters = useCallback(() => {
    const emptyFilters = { startDate: "", endDate: "" };
    setRateLogDraftFilters(emptyFilters);
    setRateLogsError("");
    setRateLogs([]);
    setRateLogsHasMore(false);
    setRateLogFilters(emptyFilters);
    setRateLogsLoaded(false);
  }, []);

  useEffect(() => {
    if (subview === "rate-log" && !rateLogsLoaded && !rateLogsLoading) {
      void loadRateLogs();
    }
  }, [loadRateLogs, rateLogsLoaded, rateLogsLoading, subview]);

  const closeDialog = useCallback(() => {
    setEditingChannel(null);
    setEditingAccount(null);
    setChannelForm(emptyChannelForm);
    setAccountForm(emptyAccountForm);
    setDialogError("");
    setSavingDialog(false);
    window.requestAnimationFrame(() => lastFocusedElementRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!editingChannel && !editingAccount) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !savingDialog) {
        closeDialog();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
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
    };
  }, [closeDialog, editingAccount, editingChannel, savingDialog]);

  const allAccounts = useMemo(
    () => [
      ...data.channels.flatMap((channel) => channel.accounts || []),
      ...data.unassigned_accounts,
    ],
    [data.channels, data.unassigned_accounts],
  );

  const summary = useMemo(
    () => ({
      channels: data.channels.length,
      accounts: allAccounts.length,
      pending: allAccounts.filter((account) => account.would_change === true).length,
      readableBalances: data.channels.filter((channel) => finiteNumber(channel.balance_remaining) !== null).length,
    }),
    [allAccounts, data.channels],
  );

  const filteredChannels = useMemo(() => {
    const query = search.trim().toLowerCase();
    return data.channels
      .map((channel) => {
        const channelMatches = !query || channelSearchText(channel).includes(query);
        const accounts = (channel.accounts || []).filter(
          (account) =>
            matchesAccountStatus(account, statusFilter) &&
            (channelMatches || accountSearchText(account).includes(query)),
        );
        const channelStatusMatches = matchesChannelStatus(channel, statusFilter);
        return { channel, accounts, visible: (channelMatches && channelStatusMatches) || accounts.length > 0 };
      })
      .filter((entry) => entry.visible);
  }, [data.channels, search, statusFilter]);

  const filteredUnassigned = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (statusFilter !== "all" && statusFilter !== "attention") return [];
    return data.unassigned_accounts.filter((account) => !query || accountSearchText(account).includes(query));
  }, [data.unassigned_accounts, search, statusFilter]);

  const setChannelBusy = (channel: UpstreamChannel, action: string | null) => {
    const key = channelKey(channel);
    setBusyChannels((current) => updateBusyMap(current, key, action));
  };

  const setAccountBusy = (account: UpstreamAccount, action: string | null) => {
    const key = accountKey(account);
    setBusyAccounts((current) => updateBusyMap(current, key, action));
  };

  const openChannelConfig = (channel: UpstreamChannel) => {
    lastFocusedElementRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setEditingChannel(channel);
    setDialogError("");
    setChannelForm({
      displayName: channel.display_name || "",
      baseUrl: channelBaseUrl(channel),
      managementBaseUrl: channel.management_base_url || "",
      upstreamType: channel.upstream_type || "auto",
      accessToken: "",
      clearAccessToken: false,
      refreshToken: "",
      clearRefreshToken: false,
      upstreamUserId: channel.upstream_user_id || "",
      manualRechargeMultiplier: numberInputValue(channel.manual_recharge_multiplier),
    });
  };

  const openAccountConfig = (account: UpstreamAccount, fallbackChannel?: UpstreamChannel) => {
    lastFocusedElementRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setEditingAccount(account);
    setDialogError("");
    setAccountForm({
      channelId: String(account.channel_id ?? fallbackChannel?.id ?? ""),
      apiKey: "",
      manualGroupMultiplier: numberInputValue(account.manual_group_multiplier),
    });
  };

  const saveChannel = async (event: FormEvent) => {
    event.preventDefault();
    if (!editingChannel) return;
    setSavingDialog(true);
    setDialogError("");
    try {
      const baseUrl = channelForm.baseUrl.trim();
      if (!baseUrl) throw new Error("请填写上游渠道地址");
      assertHttpsUrl(baseUrl);
      const managementBaseUrl = channelForm.managementBaseUrl.trim();
      if (managementBaseUrl) assertHttpsUrl(managementBaseUrl);
      const previousOrigin = urlOrigin(
        editingChannel.management_base_url || channelBaseUrl(editingChannel),
      );
      const nextOrigin = urlOrigin(managementBaseUrl || baseUrl);
      const credentialRebind = Boolean(
        previousOrigin && nextOrigin && previousOrigin !== nextOrigin,
      );
      if (
        credentialRebind &&
        !window.confirm(
          "上游域名已改变。继续会把该渠道及账号凭据重新绑定到新域名，是否确认？",
        )
      ) {
        return;
      }
      const payload: UpstreamChannelUpdate = {
        display_name: nullableText(channelForm.displayName),
        base_url: baseUrl,
        management_base_url: nullableText(managementBaseUrl),
        upstream_type: channelForm.upstreamType,
        upstream_user_id: nullableText(channelForm.upstreamUserId),
        clear_access_token: channelForm.clearAccessToken,
        confirm_credential_rebind: credentialRebind,
        manual_recharge_multiplier: optionalPositiveNumber(
          channelForm.manualRechargeMultiplier,
          "手动充值成本",
        ),
      };
      if (!channelForm.clearAccessToken && channelForm.accessToken.trim()) {
        payload.access_token = channelForm.accessToken.trim();
      }
      if (showChannelRefreshToken) {
        payload.clear_refresh_token = channelForm.clearRefreshToken;
        if (!channelForm.clearRefreshToken && channelForm.refreshToken.trim()) {
          payload.refresh_token = channelForm.refreshToken.trim();
        }
      }
      await api.updateUpstreamChannel(editingChannel.id, payload);
      closeDialog();
      await loadData(true);
      setError("");
      setNotice("渠道配置已保存；可立即同步余额、分组和账号倍率。");
    } catch (reason) {
      setDialogError(errorMessage(reason, "渠道配置保存失败"));
    } finally {
      setSavingDialog(false);
    }
  };

  const saveAccount = async (event: FormEvent) => {
    event.preventDefault();
    if (!editingAccount) return;
    setSavingDialog(true);
    setDialogError("");
    try {
      const selectedChannel = data.channels.find((channel) => String(channel.id) === accountForm.channelId);
      const payload = buildUpstreamAccountUpdatePayload({
        account: editingAccount,
        apiKey: accountForm.apiKey,
        channelId: selectedChannel?.id ?? null,
        manualGroupMultiplier: accountForm.manualGroupMultiplier,
      });
      const previousOrigin = urlOrigin(editingAccount.base_url);
      const nextOrigin = urlOrigin(selectedChannel ? channelBaseUrl(selectedChannel) : null);
      const credentialRebind = Boolean(
        previousOrigin && nextOrigin && previousOrigin !== nextOrigin,
      );
      if (
        credentialRebind &&
        !window.confirm(
          "账号将切换到不同的上游域名。继续会把账号 API Key 重新绑定到新域名，是否确认？",
        )
      ) {
        return;
      }
      payload.confirm_credential_rebind = credentialRebind;
      await api.updateUpstreamAccount(editingAccount.sub2api_account_id, payload);
      closeDialog();
      await loadData(true);
      setRateLogsLoaded(false);
      setError("");
      setNotice(
        rateWritesEnabled
          ? "账号配置已保存；上游分组与账号计费倍率将自动同步。"
          : "账号配置已保存；目标倍率已刷新，自动同步关闭，未修改账号计费倍率。",
      );
    } catch (reason) {
      setDialogError(errorMessage(reason, "账号配置保存失败"));
    } finally {
      setSavingDialog(false);
    }
  };

  const discoverChannel = async (channel: UpstreamChannel) => {
    setChannelBusy(channel, "discover");
    setError("");
    setNotice("");
    try {
      await api.discoverUpstreamChannel(channel.id);
      await loadData(true);
      setRateLogsLoaded(false);
      setNotice(channelDiscoverySuccessMessage(rateWritesEnabled, channelDisplayName(channel)));
    } catch (reason) {
      setError(errorMessage(reason, channelDiscoveryErrorMessage(rateWritesEnabled, channelDisplayName(channel))));
    } finally {
      setChannelBusy(channel, null);
    }
  };

  const discoverAll = async () => {
    if (!data.channels.length) {
      setNotice(upstreamDiscoveryCopy(rateWritesEnabled).empty);
      return;
    }
    setBulkDiscovering(true);
    setError("");
    setNotice("");
    try {
      const result = await api.discoverAllUpstreamChannels();
      await loadData(true);
      setRateLogsLoaded(false);
      setNotice(apiAccountSyncMessage(result, rateWritesEnabled));
    } catch (reason) {
      setError(errorMessage(reason, upstreamDiscoveryCopy(rateWritesEnabled).allError));
    } finally {
      setBulkDiscovering(false);
    }
  };

  const toggleAccountEnabled = async (account: UpstreamAccount) => {
    const currentlyEnabled = account.remote_schedulable !== false;
    setAccountBusy(account, currentlyEnabled ? "disable" : "enable");
    setError("");
    setNotice("");
    try {
      await api.setUpstreamAccountEnabled(account.sub2api_account_id, !currentlyEnabled);
      await loadData(true);
      setNotice(accountDisplayName(account) + (currentlyEnabled ? " 已禁用。" : " 已启用。"));
    } catch (reason) {
      setError(errorMessage(reason, currentlyEnabled ? "账号禁用失败" : "账号启用失败"));
    } finally {
      setAccountBusy(account, null);
    }
  };

  const deleteRemoteAccount = async (account: UpstreamAccount) => {
    const confirmed = window.confirm(
      "确认从 sub2api 删除「" + accountDisplayName(account) + "」？\n\n账号 #" +
        account.sub2api_account_id +
        " 及其本地上游配置会一并删除，此操作无法撤销。",
    );
    if (!confirmed) return;
    setAccountBusy(account, "delete");
    setError("");
    setNotice("");
    try {
      await api.deleteRemoteUpstreamAccount(account.sub2api_account_id);
      await loadData(true);
      setRateLogsLoaded(false);
      setNotice("已从 sub2api 删除 " + accountDisplayName(account) + "。");
    } catch (reason) {
      setError(errorMessage(reason, "sub2api 账号删除失败"));
    } finally {
      setAccountBusy(account, null);
    }
  };

  const editingChannelType =
    channelForm.upstreamType === "auto"
      ? editingChannel?.resolved_upstream_type || "auto"
      : channelForm.upstreamType;
  const showChannelRefreshToken = editingChannelType === "sub2api";
  const anyBusy = globallyBusy || localMutationBusy;
  const discoveryCopy = upstreamDiscoveryCopy(rateWritesEnabled);
  const mutationControlsDisabled = upstreamMutationControlsDisabled({
    liveDataValidated,
    loading,
    refreshing,
  });

  return (
    <section className="api-key-view api-key-channel-view" aria-label="API Key 账号管理">
      <div className="api-key-subview-tabs" role="tablist" aria-label="API Key 子页面">
        <button
          aria-selected={subview === "manage"}
          className={subview === "manage" ? "active" : ""}
          onClick={() => setSubview("manage")}
          role="tab"
          type="button"
        >
          <LayoutGrid size={16} />
          <span>账号管理</span>
        </button>
        <button
          aria-selected={subview === "rate-log"}
          className={subview === "rate-log" ? "active" : ""}
          onClick={() => setSubview("rate-log")}
          role="tab"
          type="button"
        >
          <History size={16} />
          <span>倍率变化</span>
        </button>
      </div>

      {subview === "manage" ? <>
      <div className="api-key-summary" aria-label="上游渠道汇总">
        <SummaryItem label="上游渠道" value={summary.channels} tone="blue" />
        <SummaryItem label="API Key 账号" value={summary.accounts} tone="green" />
        <SummaryItem label={rateWritesEnabled ? "待同步倍率" : "待应用倍率"} value={summary.pending} tone="amber" />
        <SummaryItem
          label="本地充值成本"
          value={"¥" + formatCostPerUsd(data.local_recharge_multiplier) + " / $1"}
          tone="teal"
          detail={sourceLabel(data.local_recharge_source)}
        />
      </div>

      {error ? (
        <Feedback tone="error" onClose={() => setError("")}>{error}</Feedback>
      ) : null}
      {notice ? (
        <Feedback tone="success" onClose={() => setNotice("")}>{notice}</Feedback>
      ) : null}

      <section className="api-key-panel api-key-channel-panel">
        <div className="api-key-panel-head">
          <div>
            <h2>上游渠道</h2>
            <p>同站点按规范 URL 合并。账号倍率 = 上游分组倍率 × 上游充值成本 ÷ 本地充值成本。</p>
          </div>
          <div className="api-key-toolbar-actions">
            <button
              className="api-key-button api-key-button--secondary"
              disabled={mutationControlsDisabled || anyBusy || !data.channels.length}
              onClick={() => void discoverAll()}
              type="button"
            >
              <Radar className={bulkDiscovering ? "spin" : ""} size={16} />
              <span>{bulkDiscovering ? discoveryCopy.bulkBusyLabel : discoveryCopy.bulkLabel}</span>
            </button>
            <button
              aria-label="刷新上游渠道"
              className="api-key-icon-button"
              disabled={loading || refreshing || anyBusy}
              onClick={() => void loadData()}
              title="刷新"
              type="button"
            >
              <RefreshCcw className={loading || refreshing ? "spin" : ""} size={16} />
            </button>
          </div>
        </div>

        <div className="api-key-filters">
          <label className="api-key-search">
            <Search size={16} />
            <span className="api-key-sr-only">搜索渠道或账号</span>
            <input
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索渠道、URL、账号、ID 或分组"
              type="search"
              value={search}
            />
            <small>{filteredChannels.length}/{data.channels.length}</small>
          </label>
          <label className="api-key-filter-select">
            <span>状态</span>
            <select onChange={(event) => setStatusFilter(event.target.value as StatusFilter)} value={statusFilter}>
              <option value="all">全部状态</option>
              <option value="pending">{rateWritesEnabled ? "待同步倍率" : "待应用倍率"}</option>
              <option value="attention">需要处理</option>
              <option value="undiscovered">尚未探测</option>
            </select>
          </label>
        </div>

        {loading && data.channels.length === 0 && data.unassigned_accounts.length === 0 ? (
          <div className="api-key-empty">
            <RefreshCcw className="spin" size={18} />
            <span>正在读取上游渠道…</span>
          </div>
        ) : filteredChannels.length === 0 && filteredUnassigned.length === 0 ? (
          <div className="api-key-empty">
            <Globe2 size={18} />
            <span>{summary.accounts || summary.channels ? "没有匹配的渠道或账号" : "暂无可管理的 API Key 账号"}</span>
          </div>
        ) : (
          <div className="api-key-channel-grid">
            {filteredChannels.map(({ channel, accounts }) => (
              <ChannelCard
                accounts={accounts}
                busyAction={busyChannels[channelKey(channel)]}
                channel={channel}
                displayTimeZone={displayTimeZone}
                globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                key={channelKey(channel)}
                onConfigureAccount={(account) => openAccountConfig(account, channel)}
                onConfigureChannel={() => openChannelConfig(channel)}
                onDeleteAccount={deleteRemoteAccount}
                onDiscover={() => void discoverChannel(channel)}
                onToggleAccount={toggleAccountEnabled}
                rateWritesEnabled={rateWritesEnabled}
                busyAccounts={busyAccounts}
              />
            ))}
            {filteredUnassigned.length ? (
              <UnassignedCard
                accounts={filteredUnassigned}
                busyAccounts={busyAccounts}
                globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                onConfigure={openAccountConfig}
                onDelete={deleteRemoteAccount}
                onToggle={toggleAccountEnabled}
                rateWritesEnabled={rateWritesEnabled}
              />
            ) : null}
          </div>
        )}
      </section>

      {editingChannel ? (
        <Modal title={"配置渠道 · " + channelDisplayName(editingChannel)} eyebrow="上游渠道" onClose={closeDialog} dialogRef={dialogRef} saving={savingDialog}>
          <form className="api-key-config-form api-key-channel-form" onSubmit={saveChannel}>
            <label className="api-key-field">
              <span>渠道名称</span>
              <input
                autoFocus
                onChange={(event) => setChannelForm((current) => ({ ...current, displayName: event.target.value }))}
                placeholder={displayHost(channelForm.baseUrl)}
                value={channelForm.displayName}
              />
            </label>
            <label className="api-key-field">
              <span>推理地址</span>
              <input
                onChange={(event) => setChannelForm((current) => ({ ...current, baseUrl: event.target.value }))}
                placeholder="https://example.com"
                required
                type="url"
                value={channelForm.baseUrl}
              />
              <small>末尾 /v1、/api/v1 与多余斜杠会归并到同一渠道。</small>
            </label>

            <label className="api-key-field api-key-field--wide">
              <span>管理地址（可选）</span>
              <input
                onChange={(event) => setChannelForm((current) => ({ ...current, managementBaseUrl: event.target.value }))}
                placeholder="留空表示与推理地址相同"
                type="url"
                value={channelForm.managementBaseUrl}
              />
              <small>余额、分组和 Key 列表从管理地址读取；模型请求仍使用推理地址。</small>
            </label>

            <fieldset className="api-key-field api-key-field--wide">
              <legend>上游协议</legend>
              <div className="api-key-segmented">
                {(["auto", "newapi", "sub2api"] as UpstreamType[]).map((type) => (
                  <button
                    aria-pressed={channelForm.upstreamType === type}
                    className={channelForm.upstreamType === type ? "active" : ""}
                    key={type}
                    onClick={() => setChannelForm((current) => ({ ...current, upstreamType: type }))}
                    type="button"
                  >
                    {statusLabel(type)}
                  </button>
                ))}
              </div>
            </fieldset>

            <label className="api-key-field">
              <span>Access Token</span>
              <input
                autoComplete="new-password"
                disabled={channelForm.clearAccessToken}
                onChange={(event) => setChannelForm((current) => ({ ...current, accessToken: event.target.value }))}
                placeholder={editingChannel.access_token_set ? "已保存；留空保持" : "粘贴上游 Access Token"}
                type="password"
                value={channelForm.accessToken}
              />
              <label className="api-key-clear-token">
                <input
                  checked={channelForm.clearAccessToken}
                  disabled={!editingChannel.access_token_set && !channelForm.accessToken}
                  onChange={(event) =>
                    setChannelForm((current) => ({
                      ...current,
                      accessToken: event.target.checked ? "" : current.accessToken,
                      clearAccessToken: event.target.checked,
                    }))
                  }
                  type="checkbox"
                />
                <span>清除已保存的 Access Token</span>
              </label>
            </label>

            {showChannelRefreshToken ? (
              <label className="api-key-field">
                <span>Refresh Token（自动续期）</span>
                <input
                  autoComplete="new-password"
                  disabled={channelForm.clearRefreshToken}
                  onChange={(event) =>
                    setChannelForm((current) => ({ ...current, refreshToken: event.target.value }))
                  }
                  placeholder={editingChannel.refresh_token_set ? "已保存；留空保持" : "粘贴上游 Refresh Token"}
                  type="password"
                  value={channelForm.refreshToken}
                />
                <label className="api-key-clear-token">
                  <input
                    checked={channelForm.clearRefreshToken}
                    disabled={!editingChannel.refresh_token_set && !channelForm.refreshToken}
                    onChange={(event) =>
                      setChannelForm((current) => ({
                        ...current,
                        refreshToken: event.target.checked ? "" : current.refreshToken,
                        clearRefreshToken: event.target.checked,
                      }))
                    }
                    type="checkbox"
                  />
                  <span>清除已保存的 Refresh Token</span>
                </label>
                <small>Access Token 返回 401 时用于自动续期；轮换后的 AT/RT 会由后端保存。</small>
              </label>
            ) : null}

            <label className="api-key-field">
              <span>NewAPI 用户 ID（余额探测）</span>
              <input
                onChange={(event) => setChannelForm((current) => ({ ...current, upstreamUserId: event.target.value }))}
                placeholder="填写数字用户 ID"
                value={channelForm.upstreamUserId}
              />
              <small>NewAPI 用户余额接口通常要求 New-Api-User；请填写个人设置中显示的数字用户 ID。</small>
            </label>

            <label className="api-key-field api-key-field--wide">
              <span>手动充值成本（¥ / $1）</span>
              <input
                inputMode="decimal"
                min="0"
                onChange={(event) =>
                  setChannelForm((current) => ({ ...current, manualRechargeMultiplier: event.target.value }))
                }
                placeholder="留空使用自动探测值"
                step="any"
                type="number"
                value={channelForm.manualRechargeMultiplier}
              />
              <small>人民币实付 ÷ 获得的美元额度。例如 ¥1 获得 $10，填写 0.1，表示 ¥0.1 可使用 $1 额度。</small>
            </label>

            <TokenGuide upstreamType={editingChannelType} />
            {dialogError ? <div className="api-key-form-error api-key-field--wide" role="alert">{dialogError}</div> : null}
            <DialogActions onCancel={closeDialog} saving={savingDialog} />
          </form>
        </Modal>
      ) : null}

      {editingAccount ? (
        <Modal title={"配置账号 · " + accountDisplayName(editingAccount)} eyebrow={"#" + editingAccount.sub2api_account_id} onClose={closeDialog} dialogRef={dialogRef} saving={savingDialog}>
          <form className="api-key-config-form" onSubmit={saveAccount}>
            <label className="api-key-field api-key-field--wide">
              <span>所属上游渠道</span>
              <select
                autoFocus
                onChange={(event) =>
                  setAccountForm((current) => ({
                    ...current,
                    channelId: event.target.value,
                  }))
                }
                value={accountForm.channelId}
              >
                <option value="">未分配渠道</option>
                {data.channels.map((channel) => (
                  <option key={channelKey(channel)} value={String(channel.id)}>
                    {channelDisplayName(channel)} · {displayHost(channelBaseUrl(channel))}
                  </option>
                ))}
              </select>
            </label>

            <label className="api-key-field api-key-field--wide">
              <span>账号 API Key</span>
              <input
                autoComplete="new-password"
                onChange={(event) => setAccountForm((current) => ({ ...current, apiKey: event.target.value }))}
                placeholder={credentialPlaceholder(editingAccount)}
                type="password"
                value={accountForm.apiKey}
              />
              <small>加密保存在本地数据库中；原值不会回显，留空保持已保存值。</small>
            </label>

            <div className="api-key-readonly-group api-key-field--wide">
              <span>上游识别分组</span>
              <strong>{editingAccount.selected_group_name || editingAccount.selected_group_id || "尚未识别"}</strong>
              <small>
                {finiteNumber(editingAccount.effective_group_multiplier) === null
                  ? statusLabel(editingAccount.group_multiplier_status || "not_ready")
                  : formatMultiplier(editingAccount.effective_group_multiplier) + " · " + sourceLabel(editingAccount.group_multiplier_source)}
              </small>
            </div>

            {canSetManualMultiplier(editingAccount) ? (
              <label className="api-key-field api-key-field--wide">
                <span>手动分组倍率</span>
                <input
                  inputMode="decimal"
                  min="0"
                  onChange={(event) =>
                    setAccountForm((current) => ({ ...current, manualGroupMultiplier: event.target.value }))
                  }
                  placeholder="上游未提供倍率时填写"
                  step="any"
                  type="number"
                  value={accountForm.manualGroupMultiplier}
                />
                <small>仅在上游无法解析该账号分组倍率时使用。</small>
              </label>
            ) : null}

            <div className="api-key-form-note api-key-field--wide">
              <BadgeDollarSign size={16} />
              <span>
                {rateWritesEnabled
                  ? "目标倍率 = 上游分组倍率 × 上游充值成本 ÷ 本地充值成本；保存后自动同步。"
                  : "目标倍率 = 上游分组倍率 × 上游充值成本 ÷ 本地充值成本；自动同步关闭，仅计算目标倍率。"}
              </span>
            </div>
            {dialogError ? <div className="api-key-form-error api-key-field--wide" role="alert">{dialogError}</div> : null}
            <DialogActions onCancel={closeDialog} saving={savingDialog} />
          </form>
        </Modal>
      ) : null}
      </> : (
        <RateChangeLogView
          displayTimeZone={displayTimeZone}
          draftFilters={rateLogDraftFilters}
          error={rateLogsError}
          filtersApplied={Boolean(rateLogFilters.startDate || rateLogFilters.endDate)}
          hasMore={rateLogsHasMore}
          loading={rateLogsLoading}
          logs={rateLogs}
          onApplyFilters={applyRateLogFilters}
          onClearFilters={clearRateLogFilters}
          onDraftFiltersChange={setRateLogDraftFilters}
          onLoadMore={() => void loadRateLogs(true)}
          onRefresh={() => void loadRateLogs()}
        />
      )}
    </section>
  );
}

function ChannelCard({
  channel,
  accounts,
  displayTimeZone,
  busyAction,
  busyAccounts,
  globallyDisabled,
  onConfigureChannel,
  onDiscover,
  onConfigureAccount,
  onDeleteAccount,
  onToggleAccount,
  rateWritesEnabled,
}: {
  channel: UpstreamChannel;
  accounts: UpstreamAccount[];
  displayTimeZone: string;
  busyAction?: string;
  busyAccounts: Record<string, string>;
  globallyDisabled: boolean;
  onConfigureChannel: () => void;
  onDiscover: () => void;
  onConfigureAccount: (account: UpstreamAccount) => void;
  onDeleteAccount: (account: UpstreamAccount) => void;
  onToggleAccount: (account: UpstreamAccount) => void;
  rateWritesEnabled: boolean;
}) {
  const [groupsExpanded, setGroupsExpanded] = useState(false);
  const groups = channel.group_options || [];
  const visibleGroups = groupsExpanded ? groups : groups.slice(0, 6);
  const type = resolvedChannelType(channel);
  const status = channelStatus(channel);
  const url = channelBaseUrl(channel);
  const managementUrl = channel.management_base_url?.trim() || url;
  const hasSeparateManagementUrl = displayCanonicalUrl(managementUrl) !== displayCanonicalUrl(url);
  const message = channelDisplayMessage(channel);
  const error = channelDisplayError(channel);
  const busy = Boolean(busyAction) || globallyDisabled;
  const discoveryCopy = upstreamDiscoveryCopy(rateWritesEnabled);
  return (
    <article className={"api-key-channel-card" + (channelHasAttention(channel) ? " api-key-channel-card--attention" : "")}>
      <header className="api-key-channel-head">
        <div className="api-key-channel-mark" aria-hidden="true"><Globe2 size={18} /></div>
        <div className="api-key-channel-title">
          <div>
            <h3>{channelDisplayName(channel)}</h3>
            <div className="api-key-inline-chips">
              <StatusChip status={type} />
              <StatusChip status={status} />
            </div>
          </div>
          <div className="api-key-channel-urls">
            {isHttpUrl(url) ? (
              <a href={url} rel="noreferrer" target="_blank" title={url}>
                <span>{displayCanonicalUrl(url)}</span>
                <ExternalLink size={13} />
              </a>
            ) : (
              <span className="api-key-channel-url">{displayCanonicalUrl(url)}</span>
            )}
            {hasSeparateManagementUrl ? (
              isHttpUrl(managementUrl) ? (
                <a href={managementUrl} rel="noreferrer" target="_blank" title={managementUrl}>
                  <b>管理</b>
                  <span>{displayCanonicalUrl(managementUrl)}</span>
                  <ExternalLink size={13} />
                </a>
              ) : <span className="api-key-channel-url"><b>管理</b>{displayCanonicalUrl(managementUrl)}</span>
            ) : null}
          </div>
        </div>
        <div className="api-key-channel-actions">
          <button
            aria-label={"配置渠道 " + channelDisplayName(channel)}
            className="api-key-icon-button"
            disabled={busy}
            onClick={onConfigureChannel}
            title="配置渠道"
            type="button"
          >
            <Pencil size={15} />
          </button>
          <button
            aria-label={discoveryCopy.channelAriaPrefix + " " + channelDisplayName(channel)}
            className="api-key-icon-button api-key-icon-button--discover"
            disabled={busy}
            onClick={onDiscover}
            title={discoveryCopy.channelTitle}
            type="button"
          >
            <Radar className={busyAction === "discover" ? "spin" : ""} size={15} />
          </button>
        </div>
      </header>

      <div className="api-key-channel-stats">
        <ChannelStat icon={<WalletCards size={16} />} label="上游余额">
          <strong>{formatBalance(channel.balance_remaining, channel.balance_unit)}</strong>
          <span>{balanceDetail(channel)}</span>
        </ChannelStat>
        <ChannelStat icon={<BadgeDollarSign size={16} />} label="充值成本">
          <strong>{"¥" + formatCostPerUsd(channel.effective_recharge_multiplier) + " / $1"}</strong>
          <span>{sourceLabel(channel.recharge_multiplier_source)}</span>
        </ChannelStat>
        <ChannelStat icon={<Radar size={16} />} label="最近探测">
          <strong>{formatDate(channel.last_discovered_at || channel.checked_at, displayTimeZone)}</strong>
          <span>{statusLabel(status)}</span>
        </ChannelStat>
      </div>

      <div className="api-key-channel-credential-line">
        <span className={channel.access_token_set ? "is-ready" : "needs-attention"}>
          <KeyRound size={13} />
          {channel.access_token_set ? "Access Token 已配置" : "缺少 Access Token"}
        </span>
        {type === "sub2api" ? (
          <span className={channel.refresh_token_set ? "is-ready" : "needs-attention"}>
            <RefreshCcw size={13} />
            {channel.refresh_token_set ? "Refresh Token 已配置" : "缺少 Refresh Token"}
          </span>
        ) : null}
        {channel.upstream_user_id ? <span className="api-key-mono">用户 {channel.upstream_user_id}</span> : null}
        {message ? (
          <span className="api-key-channel-message" title={message}>
            {message}
          </span>
        ) : null}
      </div>

      <section className="api-key-channel-groups" aria-label="可用分组">
        <div className="api-key-channel-section-label">
          <span>上游分组</span>
          <div className="api-key-channel-section-tools">
            <small>{groups.length}</small>
            {groups.length > 6 ? (
              <button
                aria-expanded={groupsExpanded}
                aria-label={groupsExpanded ? "折叠上游分组" : "展开上游分组"}
                className="api-key-collapse-button"
                onClick={() => setGroupsExpanded((current) => !current)}
                title={groupsExpanded ? "折叠" : "展开"}
                type="button"
              >
                {groupsExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
            ) : null}
          </div>
        </div>
        <div className="api-key-group-chips">
          {visibleGroups.length ? visibleGroups.map((group) => (
            <span className="api-key-group-chip" key={group.id} title={group.name + " " + formatMultiplier(group.multiplier)}>
              <span>{group.name}</span>
              <strong>{formatMultiplier(group.multiplier)}</strong>
            </span>
          )) : <span className="api-key-muted">同步后显示上游分组倍率</span>}
          {!groupsExpanded && groups.length > visibleGroups.length ? (
            <button
              className="api-key-group-chip api-key-group-chip--more"
              onClick={() => setGroupsExpanded(true)}
              type="button"
            >+{groups.length - visibleGroups.length}</button>
          ) : null}
        </div>
      </section>

      <section className="api-key-channel-accounts" aria-label="渠道账号">
        <div className="api-key-channel-section-label api-key-channel-section-label--accounts">
          <span>API Key 账号</span>
          <small>{accounts.length} 个</small>
        </div>
        {accounts.length ? (
          <div className="api-key-account-grid">
            {accounts.map((account) => (
              <AccountCard
                account={account}
                busyAction={busyAccounts[accountKey(account)]}
                globallyDisabled={globallyDisabled || Boolean(busyAction)}
                key={accountKey(account)}
                onConfigure={() => onConfigureAccount(account)}
                onDelete={() => onDeleteAccount(account)}
                onToggle={() => onToggleAccount(account)}
                rateWritesEnabled={rateWritesEnabled}
              />
            ))}
          </div>
        ) : <div className="api-key-channel-no-accounts">当前筛选下没有匹配账号</div>}
      </section>

      {error ? (
        <div className="api-key-channel-error" title={error}>
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      ) : null}
    </article>
  );
}

function UnassignedCard({
  accounts,
  busyAccounts,
  globallyDisabled,
  onConfigure,
  onDelete,
  onToggle,
  rateWritesEnabled,
}: {
  accounts: UpstreamAccount[];
  busyAccounts: Record<string, string>;
  globallyDisabled: boolean;
  onConfigure: (account: UpstreamAccount) => void;
  onDelete: (account: UpstreamAccount) => void;
  onToggle: (account: UpstreamAccount) => void;
  rateWritesEnabled: boolean;
}) {
  return (
    <article className="api-key-channel-card api-key-channel-card--unassigned">
      <header className="api-key-channel-head">
        <div className="api-key-channel-mark" aria-hidden="true"><UsersRound size={18} /></div>
        <div className="api-key-channel-title">
          <div>
            <h3>待分配账号</h3>
            <StatusChip status="unmanaged" />
          </div>
          <span className="api-key-channel-url">选择渠道后才能读取分组并计算目标倍率</span>
        </div>
      </header>
      <section className="api-key-channel-accounts">
        <div className="api-key-channel-section-label api-key-channel-section-label--accounts">
          <span>API Key 账号</span>
          <small>{accounts.length} 个</small>
        </div>
        <div className="api-key-account-grid">
          {accounts.map((account) => (
            <AccountCard
              account={account}
              busyAction={busyAccounts[accountKey(account)]}
              globallyDisabled={globallyDisabled}
              key={accountKey(account)}
              onConfigure={() => onConfigure(account)}
              onDelete={() => onDelete(account)}
              onToggle={() => onToggle(account)}
              rateWritesEnabled={rateWritesEnabled}
            />
          ))}
        </div>
      </section>
    </article>
  );
}

function AccountCard({
  account,
  busyAction,
  globallyDisabled,
  onConfigure,
  onDelete,
  onToggle,
  rateWritesEnabled,
}: {
  account: UpstreamAccount;
  busyAction?: string;
  globallyDisabled: boolean;
  onConfigure: () => void;
  onDelete: () => void;
  onToggle: () => void;
  rateWritesEnabled: boolean;
}) {
  const busy = Boolean(busyAction) || globallyDisabled;
  const current = finiteNumber(account.current_rate);
  const target = finiteNumber(account.target_rate);
  const groupMultiplier = finiteNumber(account.effective_group_multiplier);
  const normalizedMultiplier = normalizedUpstreamMultiplier(
    groupMultiplier,
    account.effective_recharge_multiplier,
  );
  const groupMultiplierTitle = groupMultiplier === null
    ? ""
    : normalizedMultiplier === null
      ? "上游原始分组倍率 " + formatMultiplier(groupMultiplier)
      : "上游原始分组倍率 " + formatMultiplier(groupMultiplier)
        + "；1:1 折算 " + formatMultiplier(normalizedMultiplier)
        + "（分组倍率 × 上游充值成本）";
  const enabled = account.remote_schedulable !== false;
  const effectiveStatus = enabled ? account.remote_status || "enabled" : "disabled";
  return (
    <article className={"api-key-account-card" + (enabled ? "" : " api-key-account-card--disabled")}>
      <header className="api-key-account-card-head">
        <strong title={accountDisplayName(account)}>{accountDisplayName(account)}</strong>
        <span className="api-key-mono">#{account.sub2api_account_id}</span>
        <div className="api-key-inline-chips">
          <StatusChip status={effectiveStatus} />
          {account.remote_platform?.trim() ? <PlatformChip platform={account.remote_platform} /> : null}
        </div>
      </header>
      <div className="api-key-account-card-facts">
        <div className="api-key-account-card-fact">
          <span>分组</span>
          <div className="api-key-account-group-line">
            <strong title={account.selected_group_name || account.selected_group_id || "未识别"}>
              {account.selected_group_name || account.selected_group_id || "未识别"}
            </strong>
            {groupMultiplier !== null ? (
              <span
                aria-label={groupMultiplierTitle}
                className="api-key-account-group-rate"
                title={groupMultiplierTitle}
              >
                {formatMultiplier(groupMultiplier)}
              </span>
            ) : null}
          </div>
        </div>
        <div className="api-key-account-card-fact api-key-account-card-fact--rate api-key-account-card-fact--current">
          <span>当前倍率</span>
          <strong>{formatMultiplier(current)}</strong>
          <small>sub2api 当前值</small>
        </div>
        <div
          className={
            "api-key-account-card-fact api-key-account-card-fact--rate api-key-account-card-fact--target"
            + (account.would_change === true ? " is-pending" : "")
          }
        >
          <span>目标倍率</span>
          <strong>{formatMultiplier(target)}</strong>
          <small>{accountRateStatusLabel(target, account.would_change, rateWritesEnabled)}</small>
        </div>
      </div>
      <footer className="api-key-account-card-actions">
        <button
          aria-label={"配置 " + accountDisplayName(account)}
          className="api-key-icon-button"
          disabled={busy}
          onClick={onConfigure}
          title="配置账号"
          type="button"
        >
          <Settings2 size={15} />
        </button>
        <button
          aria-label={(enabled ? "禁用 " : "启用 ") + accountDisplayName(account)}
          aria-pressed={!enabled}
          className={"api-key-icon-button " + (enabled ? "api-key-icon-button--disable" : "api-key-icon-button--enable")}
          disabled={busy}
          onClick={onToggle}
          title={enabled ? "禁用账号" : "启用账号"}
          type="button"
        >
          {enabled ? <PowerOff size={15} /> : <Power size={15} />}
        </button>
        <button
          aria-label={"从 sub2api 删除 " + accountDisplayName(account)}
          className="api-key-icon-button api-key-icon-button--danger"
          disabled={busy}
          onClick={onDelete}
          title="删除 sub2api 账号"
          type="button"
        >
          <Trash2 size={15} />
        </button>
      </footer>
    </article>
  );
}

function RateChangeLogView({
  displayTimeZone,
  draftFilters,
  error,
  filtersApplied,
  hasMore,
  loading,
  logs,
  onApplyFilters,
  onClearFilters,
  onDraftFiltersChange,
  onLoadMore,
  onRefresh,
}: {
  displayTimeZone: string;
  draftFilters: RateLogFilters;
  error: string;
  filtersApplied: boolean;
  hasMore: boolean;
  loading: boolean;
  logs: UpstreamRateChangeLog[];
  onApplyFilters: () => void;
  onClearFilters: () => void;
  onDraftFiltersChange: (filters: RateLogFilters) => void;
  onLoadMore: () => void;
  onRefresh: () => void;
}) {
  return (
    <section className="api-key-panel api-key-rate-log-panel" aria-label="倍率变化记录">
      <div className="api-key-panel-head">
        <div>
          <h2>倍率变化</h2>
          <p>仅显示真实倍率变化；综合倍率按分组倍率 × 上游充值倍率折算。</p>
        </div>
        <button
          aria-label="刷新倍率变化记录"
          className="api-key-icon-button"
          disabled={loading}
          onClick={onRefresh}
          title="刷新"
          type="button"
        >
          <RefreshCcw className={loading ? "spin" : ""} size={16} />
        </button>
      </div>

      <form
        className="api-key-rate-log-filters"
        onSubmit={(event) => {
          event.preventDefault();
          onApplyFilters();
        }}
      >
        <div className="api-key-rate-log-filter-title">
          <CalendarDays size={15} />
          <span>日期范围</span>
        </div>
        <label>
          <span>开始日期</span>
          <input
            max={draftFilters.endDate || undefined}
            onChange={(event) => onDraftFiltersChange({ ...draftFilters, startDate: event.target.value })}
            type="date"
            value={draftFilters.startDate}
          />
        </label>
        <label>
          <span>结束日期</span>
          <input
            min={draftFilters.startDate || undefined}
            onChange={(event) => onDraftFiltersChange({ ...draftFilters, endDate: event.target.value })}
            type="date"
            value={draftFilters.endDate}
          />
        </label>
        <button className="api-key-button api-key-button--primary" disabled={loading} type="submit">
          <Search size={15} />
          <span>筛选</span>
        </button>
        {filtersApplied || draftFilters.startDate || draftFilters.endDate ? (
          <button
            className="api-key-button api-key-button--secondary"
            disabled={loading}
            onClick={onClearFilters}
            type="button"
          >
            <X size={15} />
            <span>清除</span>
          </button>
        ) : null}
      </form>

      {error ? (
        <div className="api-key-rate-log-error" role="alert">
          <AlertTriangle size={15} />
          <span>{error}</span>
        </div>
      ) : null}

      {loading && logs.length === 0 ? (
        <div className="api-key-empty">
          <RefreshCcw className="spin" size={18} />
          <span>正在读取倍率变化…</span>
        </div>
      ) : logs.length === 0 ? (
        <div className="api-key-empty">
          <History size={18} />
          <span>{filtersApplied ? "当前日期范围内没有倍率变化" : "暂无倍率变化记录"}</span>
        </div>
      ) : (
        <div className="api-key-rate-log-list">
          {logs.map((log) => {
            const groupChange = groupRateChange(log);
            const upstreamChange = upstreamRateChange(log);
            const billingChange = accountBillingRateChange(log);
            const rechargeChange = upstreamRechargeRateChange(log);
            const rechargeChanged = rechargeChange.direction === "increase" || rechargeChange.direction === "decrease";
            return (
              <article
                className={"api-key-rate-log-row" + (rechargeChanged ? " api-key-rate-log-row--recharge" : "")}
                key={log.id}
              >
                <div className="api-key-rate-log-identity">
                  <strong>{log.account_name || "API Key 账号 #" + log.sub2api_account_id}</strong>
                  <span>{log.channel_name || "未分配渠道"}{log.group_name ? " · " + log.group_name : ""}</span>
                  <div className="api-key-rate-log-meta">
                    <StatusChip status={log.status} />
                    <time dateTime={log.created_at}>{formatDate(log.created_at, displayTimeZone)}</time>
                    <span>{rateChangeReasonLabel(log.reason)}</span>
                  </div>
                  {rechargeChanged ? (
                    <span className="api-key-rate-recharge-change">
                      <BadgeDollarSign size={12} />
                      <span>上游充值倍率</span>
                      <b>{formatRateLogMultiplier(rechargeChange.oldValue)}</b>
                      <ArrowRight size={11} />
                      <strong>{formatRateLogMultiplier(rechargeChange.newValue)}</strong>
                    </span>
                  ) : null}
                  {log.safe_error ? <span className="api-key-rate-log-safe-error">{log.safe_error}</span> : null}
                </div>
                <CompactRateChange change={groupChange} label="分组倍率" />
                <CompactRateChange change={upstreamChange} emphasize label="综合倍率" showDirection />
                <CompactRateChange change={billingChange} label="账号计费倍率" showDirection />
              </article>
            );
          })}
        </div>
      )}

      {hasMore ? (
        <div className="api-key-rate-log-more">
          <button className="api-key-button api-key-button--secondary" disabled={loading} onClick={onLoadMore} type="button">
            <History size={15} />
            <span>{loading ? "读取中" : "加载更多"}</span>
          </button>
        </div>
      ) : null}
    </section>
  );
}

function CompactRateChange({
  change,
  emphasize = false,
  label,
  showDirection = false,
}: {
  change: ReturnType<typeof groupRateChange>;
  emphasize?: boolean;
  label: string;
  showDirection?: boolean;
}) {
  const changed = change.direction === "increase" || change.direction === "decrease";
  const value = change.newValue ?? change.oldValue;
  return (
    <div className={"api-key-rate-log-cell" + (emphasize ? " api-key-rate-log-cell--primary" : "")}>
      <span>{label}</span>
      {changed ? (
        <div className="api-key-rate-log-flow">
          <b>{formatRateLogMultiplier(change.oldValue)}</b>
          <ArrowRight size={13} />
          <strong>{formatRateLogMultiplier(change.newValue)}</strong>
        </div>
      ) : (
        <strong className="api-key-rate-log-static">{formatRateLogMultiplier(value)}</strong>
      )}
      {showDirection && changed ? (
        <span className={"api-key-rate-delta api-key-rate-delta--" + change.direction}>
          {change.direction === "increase" ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
          <span>{change.direction === "increase" ? "上涨" : "下降"}</span>
          <strong>{formatSignedMultiplier(change.delta)}</strong>
        </span>
      ) : null}
    </div>
  );
}

function ChannelStat({ icon, label, children }: { icon: React.ReactNode; label: string; children: React.ReactNode }) {
  return (
    <div className="api-key-channel-stat">
      <div className="api-key-channel-stat-label">{icon}<span>{label}</span></div>
      <div className="api-key-channel-stat-value">{children}</div>
    </div>
  );
}

function SummaryItem({
  label,
  value,
  tone,
  detail,
}: {
  label: string;
  value: React.ReactNode;
  tone: string;
  detail?: string;
}) {
  return (
    <div className={"api-key-summary-item api-key-summary-item--" + tone}>
      <div><span>{label}</span>{detail ? <small>{detail}</small> : null}</div>
      <strong>{value}</strong>
    </div>
  );
}

function Feedback({ tone, onClose, children }: { tone: "error" | "success"; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className={"api-key-feedback api-key-feedback--" + tone} role={tone === "error" ? "alert" : "status"}>
      {tone === "error" ? <AlertTriangle size={16} /> : <CheckCircle2 size={16} />}
      <span>{children}</span>
      <button aria-label="关闭提示" onClick={onClose} title="关闭" type="button"><X size={15} /></button>
    </div>
  );
}

function Modal({
  title,
  eyebrow,
  onClose,
  dialogRef,
  saving,
  children,
}: {
  title: string;
  eyebrow: string;
  onClose: () => void;
  dialogRef: React.RefObject<HTMLElement | null>;
  saving: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className="api-key-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onClose();
      }}
    >
      <section aria-modal="true" className="api-key-dialog" ref={dialogRef} role="dialog">
        <div className="api-key-dialog-head">
          <div><p>{eyebrow}</p><h2>{title}</h2></div>
          <button aria-label="关闭配置" className="api-key-icon-button" disabled={saving} onClick={onClose} title="关闭" type="button">
            <X size={17} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

function DialogActions({ onCancel, saving }: { onCancel: () => void; saving: boolean }) {
  return (
    <div className="api-key-dialog-actions api-key-field--wide">
      <button className="api-key-button api-key-button--secondary" disabled={saving} onClick={onCancel} type="button">取消</button>
      <button className="api-key-button api-key-button--primary" disabled={saving} type="submit">
        <Save size={16} />
        <span>{saving ? "保存中" : "保存配置"}</span>
      </button>
    </div>
  );
}

function TokenGuide({ upstreamType }: { upstreamType: UpstreamType }) {
  return (
    <div className="api-key-token-guide api-key-field--wide">
      <div><KeyRound size={15} /><strong>上游登录凭据获取方式</strong></div>
      {upstreamType !== "sub2api" ? (
        <p><b>NewAPI：</b>打开上游“个人设置”生成系统访问令牌，并填写用于余额接口的数字用户 ID。不要使用模型调用 API Key。</p>
      ) : null}
      {upstreamType !== "newapi" ? (
        <p><b>Sub2API：</b>同时填写登录响应中的 Access Token（AT）和 Refresh Token（RT）。AT 返回 401 时，系统会调用 <code>/api/v1/auth/refresh</code>，保存轮换后的 AT/RT 并重试一次。不要使用模型 API Key。</p>
      ) : null}
      <p>编辑时令牌留空会保留已保存值；页面和接口只显示是否已配置，绝不会回显令牌。</p>
    </div>
  );
}

function StatusChip({ status }: { status?: string | null }) {
  const value = String(status || "unknown").trim().toLowerCase();
  return <span className={"api-key-chip api-key-chip--" + statusTone(value)}>{statusLabel(value)}</span>;
}

function PlatformChip({ platform }: { platform: string }) {
  const value = platform.trim().toLowerCase();
  return <span className="api-key-chip api-key-chip--info">{statusLabel(value)}</span>;
}

function channelKey(channel: UpstreamChannel) {
  return String(channel.id);
}

function accountKey(account: UpstreamAccount) {
  return String(account.sub2api_account_id);
}

function channelDisplayName(channel: UpstreamChannel) {
  return channel.display_name?.trim() || displayHost(channelBaseUrl(channel)) || "未命名渠道";
}

function accountDisplayName(account: UpstreamAccount) {
  return account.remote_name?.trim() || "API Key 账号 #" + account.sub2api_account_id;
}

function channelBaseUrl(channel: UpstreamChannel) {
  return channel.canonical_base_url?.trim() || channel.base_url?.trim() || "";
}

function channelSearchText(channel: UpstreamChannel) {
  return [
    channel.display_name,
    channel.base_url,
    channel.canonical_base_url,
    channel.management_base_url,
    channel.upstream_type,
    channel.resolved_upstream_type,
    channel.upstream_user_id,
    channel.recharge_multiplier_status,
    channel.balance_status,
    channel.status,
    channel.message,
    channel.last_error,
    ...(channel.group_options || []).flatMap((group) => [group.id, group.name]),
  ].filter(Boolean).join(" ").toLowerCase();
}

function accountSearchText(account: UpstreamAccount) {
  return [
    account.sub2api_account_id,
    account.remote_name,
    account.remote_platform,
    account.remote_account_type,
    account.remote_status,
    account.selected_group_id,
    account.selected_group_name,
    account.api_key_hint,
    account.group_multiplier_status,
    account.last_error,
  ].filter(Boolean).join(" ").toLowerCase();
}

function matchesChannelStatus(channel: UpstreamChannel, filter: StatusFilter) {
  if (filter === "all") return true;
  if (filter === "attention") return channelHasAttention(channel);
  if (filter === "undiscovered") return !channel.last_discovered_at;
  return (channel.accounts || []).some((account) => account.would_change === true);
}

function matchesAccountStatus(account: UpstreamAccount, filter: StatusFilter) {
  if (filter === "all") return true;
  if (filter === "pending") return account.would_change === true;
  if (filter === "undiscovered") return finiteNumber(account.target_rate) === null;
  return accountHasAttention(account);
}

function channelHasAttention(channel: UpstreamChannel) {
  if (channel.last_error || !channel.access_token_set) return true;
  if (resolvedChannelType(channel) === "sub2api" && !channel.refresh_token_set) return true;
  return [channel.status, channel.balance_status, channel.recharge_multiplier_status]
    .filter(Boolean)
    .some((status) => isFailureStatus(status));
}

function accountHasAttention(account: UpstreamAccount) {
  if (!account.managed || account.last_error || !account.api_key_set) return true;
  return [account.group_multiplier_status].filter(Boolean).some((status) => isFailureStatus(status));
}

function channelStatus(channel: UpstreamChannel) {
  if (channel.last_error) return "discovery_failed";
  return channel.status || channel.balance_status || channel.recharge_multiplier_status || "not_checked";
}

function resolvedChannelType(channel: UpstreamChannel) {
  if (channel.upstream_type && channel.upstream_type !== "auto") return channel.upstream_type;
  return channel.resolved_upstream_type || "auto";
}

function channelDisplayMessage(channel: UpstreamChannel) {
  const balanceStatus = String(channel.balance_status || "").trim().toLowerCase();
  const type = resolvedChannelType(channel);
  if (balanceStatus === "credentials_missing") {
    if (type === "newapi" && channel.access_token_set && !channel.upstream_user_id) {
      return "缺少数字 New-Api-User ID，暂时无法读取余额";
    }
    return "缺少登录 Access Token，暂时无法读取余额";
  }

  const message = channel.balance_message || channel.message || "";
  if (/rejected the balance credentials/i.test(message)) {
    return "上游拒绝余额凭据，请检查 Access Token 和用户 ID";
  }
  return message;
}

function channelDisplayError(channel: UpstreamChannel) {
  const error = channel.last_error || "";
  if (!error || error === channel.balance_message || error === channel.message) return "";
  if (/rejected the balance credentials/i.test(error)) {
    return "上游拒绝余额凭据，请检查 Access Token 和用户 ID";
  }
  return error;
}

function isFailureStatus(status?: string | null) {
  const value = String(status || "").trim().toLowerCase();
  if (value === "default_missing") return false;
  return /(error|fail|invalid|unavailable|unsupported|missing|blocked|denied)/i.test(value);
}

function updateBusyMap(current: Record<string, string>, key: string, action: string | null) {
  if (action) return { ...current, [key]: action };
  const next = { ...current };
  delete next[key];
  return next;
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function numberInputValue(value: unknown) {
  const number = finiteNumber(value);
  return number === null ? "" : String(number);
}

function formatMultiplier(value: unknown) {
  const number = finiteNumber(value);
  if (number === null) return "—";
  return "×" + number.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function formatSignedMultiplier(value: unknown) {
  const number = finiteNumber(value);
  if (number === null) return "—";
  const sign = number > 0 ? "+" : number < 0 ? "−" : "";
  return sign + Math.abs(number).toLocaleString("zh-CN", { maximumFractionDigits: 6 }) + "×";
}

function formatRateLogMultiplier(value: unknown) {
  const number = finiteNumber(value);
  if (number === null) return "—";
  return "×" + number.toLocaleString("zh-CN", { maximumFractionDigits: 6 });
}

function formatCostPerUsd(value: unknown) {
  const number = finiteNumber(value);
  if (number === null) return "—";
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function formatBalance(value: unknown, unit?: string | null) {
  const number = finiteNumber(value);
  if (number === null) return "—";
  const amount = number.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
  const normalizedUnit = String(unit || "USD").trim().toUpperCase();
  if (normalizedUnit === "USD") return "$" + amount;
  if (normalizedUnit === "USDT") return "$" + amount + " USDT";
  if (normalizedUnit === "CNY" || normalizedUnit === "RMB") return "¥" + amount;
  return amount + " " + normalizedUnit;
}

function balanceDetail(channel: UpstreamChannel) {
  const details: string[] = [];
  if (finiteNumber(channel.balance_total) !== null) details.push("总额 " + formatBalance(channel.balance_total, channel.balance_unit));
  if (finiteNumber(channel.balance_used) !== null) details.push("上游累计已用 " + formatBalance(channel.balance_used, channel.balance_unit));
  return details.join(" · ") || statusLabel(channel.balance_status || channel.status || "not_checked");
}

function formatDate(value?: string | null, timeZone?: string) {
  if (!value) return "尚未探测";
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : value + "Z";
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: timeZone || undefined,
  }).format(date);
}

function displayCanonicalUrl(value?: string | null) {
  if (!value) return "未填写渠道地址";
  const trimmed = value.trim().replace(/\/+$/, "").replace(/\/(?:api\/)?v1$/i, "");
  return trimmed || value;
}

function displayHost(value?: string | null) {
  if (!value) return "未填写地址";
  const canonical = displayCanonicalUrl(value);
  try {
    const url = new URL(canonical);
    return url.host + (url.pathname === "/" ? "" : url.pathname.replace(/\/$/, ""));
  } catch {
    return canonical;
  }
}

function isHttpUrl(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function sourceLabel(source?: string | null) {
  const value = String(source || "").trim().toLowerCase();
  return ({
    auto: "自动",
    discovered: "自动探测",
    upstream: "上游读取",
    upstream_key: "上游 Key 同步",
    manual: "手动设置",
    default: "默认值",
    settings: "本地设置",
    sub2api_settings: "本地设置",
    sub2api: "Sub2API",
    unknown: "待确认",
  } as Record<string, string>)[value] || source || "待确认";
}

function statusLabel(status: string) {
  return upstreamStatusLabel(status);
}

function statusTone(status: string) {
  return upstreamStatusTone(status);
}

function nullableText(value: string) {
  return value.trim() || null;
}

function optionalPositiveNumber(value: string, label: string) {
  if (!value.trim()) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new Error(label + "必须大于 0");
  return number;
}

function assertHttpsUrl(value: string) {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("上游渠道地址格式不正确");
  }
  if (url.protocol !== "https:") {
    throw new Error("上游渠道地址必须使用 https");
  }
}

function urlOrigin(value: string | null | undefined) {
  if (!value) return null;
  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
}

function credentialPlaceholder(account: UpstreamAccount) {
  if (!account.api_key_set) return "粘贴账号 API Key";
  return "已配置；留空保持";
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}
