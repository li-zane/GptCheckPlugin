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
  ListOrdered,
  Pencil,
  Plus,
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
import { FormEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { api, upstreamLegacyBindingCounts } from "./api";
import {
  buildUpstreamAccountUpdatePayload,
  canSetManualMultiplier,
  expectedIdentityFingerprint,
} from "./upstreamAccountForm";
import { channelCredentialBindingChanged } from "./upstreamCredentialBinding";
import {
  upstreamChangeReasonLabel,
  upstreamHealthStatusLabel,
  upstreamStatusLabel,
  upstreamStatusTone,
  type UpstreamHealthKind,
} from "./upstreamLabels";
import {
  accountBillingRateChange,
  groupRateChange,
  remoteSchedulableChange,
  upstreamGroupStatusChange,
  upstreamKeyStatusChange,
  upstreamRateChange,
  upstreamRechargeRateChange,
  type UpstreamStateChange,
} from "./upstreamRatePresentation";
import {
  apiAccountSyncMessage,
  apiAccountLegacyBindingConfirmationMessage,
  accountRateStatusLabel,
  channelDiscoveryErrorMessage,
  channelDiscoverySuccessMessage,
  upstreamDiscoveryCopy,
  upstreamMutationControlsDisabled,
} from "./upstreamSyncPresentation";
import {
  accountCompositeMultiplier,
  filterUpstreamAccountEntries,
  flattenUpstreamAccounts,
  priorityIntervalAssignmentBlocked,
  priorityIntervalAssignmentNeedsConfirmation,
  sortUpstreamAccountEntries,
  upstreamAccountPlatforms,
  upstreamAccountMatchesStatus,
  type UpstreamAccountEntry,
} from "./upstreamPriorityPresentation";
import { upstreamOverviewHasLiveMutationData } from "./upstreamOverviewCache";
import {
  formatUpstreamBalance,
  rechargeAdjustedUsage,
  shouldShowUpstreamAccountUsage,
  visibleUpstreamBalanceMessage,
} from "./upstreamUsagePresentation";
import type { ApiKeySubview } from "./viewRouting";
import type {
  PriorityInterval,
  PriorityIntervalInput,
  UpstreamAccount,
  UpstreamChannel,
  UpstreamChangeLog,
  UpstreamChannelsResponse,
  UpstreamChannelUpdate,
  UpstreamType,
} from "./types";

type StatusFilter = "all" | "pending" | "attention" | "undiscovered";
type RateLogFilters = { startDate: string; endDate: string };
type PriorityIntervalFilter = "all" | "unassigned" | string;
type PlatformFilter = "all" | "__unknown__" | string;

type ChannelForm = {
  displayName: string;
  baseUrl: string;
  managementBaseUrl: string;
  upstreamType: UpstreamType;
  probeEnabled: boolean;
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
  remoteName: string;
};

type PriorityIntervalForm = {
  name: string;
  startPriority: string;
  endPriority: string;
  step: string;
};

type AccountCollectionDialog = {
  accounts: UpstreamAccount[];
  channel: UpstreamChannel | null;
  title: string;
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
  probeEnabled: true,
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
  remoteName: "",
};

const emptyPriorityIntervalForm: PriorityIntervalForm = {
  name: "",
  startPriority: "",
  endPriority: "",
  step: "1",
};

export function ApiKeyAccountsView({
  cacheBaseUrl,
  cachedData,
  displayTimeZone,
  globallyBusy,
  onCacheChange,
  onOperationStart,
  onSubviewChange,
  rateWritesEnabled,
  refreshVersion,
  subview,
}: {
  cacheBaseUrl: string;
  cachedData: UpstreamChannelsResponse | null;
  displayTimeZone: string;
  globallyBusy: boolean;
  onCacheChange: (data: UpstreamChannelsResponse, baseUrl: string) => void;
  onOperationStart: () => () => void;
  onSubviewChange: (subview: ApiKeySubview) => void;
  rateWritesEnabled: boolean;
  refreshVersion: number;
  subview: ApiKeySubview;
}) {
  const [data, setData] = useState<UpstreamChannelsResponse>(cachedData || emptyData);
  const [loading, setLoading] = useState(!cachedData);
  const [refreshing, setRefreshing] = useState(Boolean(cachedData));
  const [bulkDiscovering, setBulkDiscovering] = useState(false);
  const [busyChannels, setBusyChannels] = useState<Record<string, string>>({});
  const [busyAccounts, setBusyAccounts] = useState<Record<string, string>>({});
  const [channelSearch, setChannelSearch] = useState("");
  const [channelStatusFilter, setChannelStatusFilter] = useState<StatusFilter>("all");
  const [accountSearch, setAccountSearch] = useState("");
  const [accountStatusFilter, setAccountStatusFilter] = useState<StatusFilter>("all");
  const [priorityIntervalFilter, setPriorityIntervalFilter] = useState<PriorityIntervalFilter>("all");
  const [platformFilter, setPlatformFilter] = useState<PlatformFilter>("all");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [rateLogs, setRateLogs] = useState<UpstreamChangeLog[]>([]);
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
  const [priorityIntervalDialogOpen, setPriorityIntervalDialogOpen] = useState(false);
  const [editingPriorityInterval, setEditingPriorityInterval] = useState<PriorityInterval | null>(null);
  const [priorityIntervalForm, setPriorityIntervalForm] = useState<PriorityIntervalForm>(emptyPriorityIntervalForm);
  const [priorityIntervalsBusy, setPriorityIntervalsBusy] = useState(false);
  const [accountCollectionDialog, setAccountCollectionDialog] = useState<AccountCollectionDialog | null>(null);
  const [accountUpstreamDialog, setAccountUpstreamDialog] = useState<UpstreamChannel | null>(null);
  const [dialogError, setDialogError] = useState("");
  const [savingDialog, setSavingDialog] = useState(false);
  const [liveDataValidated, setLiveDataValidated] = useState(false);
  const requestSequence = useRef(0);
  const rateLogsRequestSequence = useRef(0);
  const activeCacheBaseUrlRef = useRef(cacheBaseUrl);
  const hasDataRef = useRef(Boolean(cachedData));
  const refreshVersionRef = useRef(refreshVersion);
  const dialogRef = useRef<HTMLElement | null>(null);
  const lastFocusedElementRef = useRef<HTMLElement | null>(null);
  const localMutationBusy = savingDialog
    || priorityIntervalsBusy
    || bulkDiscovering
    || Object.keys(busyChannels).length > 0
    || Object.keys(busyAccounts).length > 0;

  const loadData = useCallback(async (preserveFeedback = false) => {
    const sequence = ++requestSequence.current;
    const requestBaseUrl = cacheBaseUrl;
    setLiveDataValidated(false);
    if (hasDataRef.current) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    if (!preserveFeedback) setError("");
    try {
      const response = await api.upstreamChannels();
      if (
        sequence !== requestSequence.current
        || activeCacheBaseUrlRef.current !== requestBaseUrl
      ) return;
      const normalized = {
        ...response,
        channels: Array.isArray(response.channels) ? response.channels : [],
        priority_intervals: Array.isArray(response.priority_intervals) ? response.priority_intervals : [],
        unassigned_accounts: Array.isArray(response.unassigned_accounts) ? response.unassigned_accounts : [],
      };
      hasDataRef.current = true;
      setData(normalized);
      onCacheChange(normalized, cacheBaseUrl);
      setLiveDataValidated(true);
    } catch (reason) {
       if (
         sequence === requestSequence.current
         && activeCacheBaseUrlRef.current === requestBaseUrl
       ) {
        setError(errorMessage(reason, "上游渠道读取失败"));
      }
    } finally {
      if (
        sequence === requestSequence.current
        && activeCacheBaseUrlRef.current === requestBaseUrl
      ) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [cacheBaseUrl, onCacheChange]);

  useEffect(() => {
    if (activeCacheBaseUrlRef.current === cacheBaseUrl) return;
    requestSequence.current += 1;
    activeCacheBaseUrlRef.current = cacheBaseUrl;
    hasDataRef.current = Boolean(cachedData);
    setData(cachedData || emptyData);
    setLoading(!cachedData);
    setRefreshing(Boolean(cachedData));
    setLiveDataValidated(false);
    setError("");
    setNotice("");
    rateLogsRequestSequence.current += 1;
    setRateLogs([]);
    setRateLogsLoading(false);
    setRateLogsHasMore(false);
    setRateLogsLoaded(false);
    setRateLogsError("");
  }, [cacheBaseUrl, cachedData]);

  useEffect(() => {
    if (!cachedData || hasDataRef.current) return;
    hasDataRef.current = true;
    setData(cachedData);
    setLoading(false);
  }, [cachedData]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (refreshVersionRef.current === refreshVersion) return;
    refreshVersionRef.current = refreshVersion;
    requestSequence.current += 1;
    setError("");
    setNotice("");
    rateLogsRequestSequence.current += 1;
    setRateLogs([]);
    setRateLogsLoading(false);
    setRateLogsHasMore(false);
    setRateLogsError("");
    setRateLogsLoaded(false);
    if (cachedData) {
      const hasLiveMutationData = upstreamOverviewHasLiveMutationData(cachedData);
      hasDataRef.current = true;
      setData(cachedData);
      setLoading(false);
      setRefreshing(false);
      setLiveDataValidated(hasLiveMutationData);
      if (!hasLiveMutationData) void loadData(true);
    } else {
      void loadData();
    }
  }, [cachedData, loadData, refreshVersion]);

  const loadRateLogs = useCallback(async (append = false) => {
    const sequence = ++rateLogsRequestSequence.current;
    setRateLogsLoading(true);
    setRateLogsError("");
    try {
      const beforeId = append && rateLogs.length ? rateLogs[rateLogs.length - 1].id : null;
      const next = await api.upstreamChangeLogs(50, beforeId, {
        startDate: rateLogFilters.startDate || undefined,
        endDate: rateLogFilters.endDate || undefined,
        timeZone: displayTimeZone,
      });
      if (sequence !== rateLogsRequestSequence.current) return;
      setRateLogs((current) => append ? [...current, ...next] : next);
      setRateLogsHasMore(next.length === 50);
      setRateLogsLoaded(true);
    } catch (reason) {
      if (sequence === rateLogsRequestSequence.current) {
        setRateLogsError(errorMessage(reason, "上游变化记录读取失败"));
        setRateLogsLoaded(true);
      }
    } finally {
      if (sequence === rateLogsRequestSequence.current) setRateLogsLoading(false);
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
    rateLogsRequestSequence.current += 1;
    setRateLogs([]);
    setRateLogsLoading(false);
    setRateLogsHasMore(false);
    setRateLogFilters(rateLogDraftFilters);
    setRateLogsLoaded(false);
  }, [rateLogDraftFilters]);

  const clearRateLogFilters = useCallback(() => {
    const emptyFilters = { startDate: "", endDate: "" };
    setRateLogDraftFilters(emptyFilters);
    setRateLogsError("");
    rateLogsRequestSequence.current += 1;
    setRateLogs([]);
    setRateLogsLoading(false);
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
    const restoreTarget = lastFocusedElementRef.current;
    lastFocusedElementRef.current = null;
    setEditingChannel(null);
    setEditingAccount(null);
    setPriorityIntervalDialogOpen(false);
    setEditingPriorityInterval(null);
    setPriorityIntervalForm(emptyPriorityIntervalForm);
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setChannelForm(emptyChannelForm);
    setAccountForm(emptyAccountForm);
    setDialogError("");
    setSavingDialog(false);
    window.requestAnimationFrame(() => {
      if (restoreTarget?.isConnected) restoreTarget.focus();
    });
  }, []);

  useEffect(() => {
    if (
      !editingChannel
      && !editingAccount
      && !priorityIntervalDialogOpen
      && !accountCollectionDialog
      && !accountUpstreamDialog
    ) return;
    const previousOverflow = document.body.style.overflow;
    const dialog = dialogRef.current;
    document.body.style.overflow = "hidden";
    const focusableElements = () => Array.from(
      dialog?.querySelectorAll<HTMLElement>(
        'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ) || [],
    );
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !savingDialog) {
        closeDialog();
        return;
      }
      if (event.key !== "Tab" || !dialog) return;
      const focusable = focusableElements();
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
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
  }, [
    accountCollectionDialog,
    accountUpstreamDialog,
    closeDialog,
    editingAccount,
    editingChannel,
    priorityIntervalDialogOpen,
    savingDialog,
  ]);

  const allAccountEntries = useMemo(() => flattenUpstreamAccounts(data), [data]);
  const allAccounts = useMemo(() => allAccountEntries.map(({ account }) => account), [allAccountEntries]);
  const priorityIntervals = data.priority_intervals || [];
  const platformOptions = useMemo(() => upstreamAccountPlatforms(allAccountEntries), [allAccountEntries]);
  const filteredAccountEntries = useMemo(() => {
    const filtered = filterUpstreamAccountEntries(allAccountEntries, {
      interval: priorityIntervalFilter,
      platform: platformFilter,
      query: accountSearch,
    }).filter(({ account }) => upstreamAccountMatchesStatus(account, accountStatusFilter));
    return sortUpstreamAccountEntries(filtered);
  }, [accountSearch, accountStatusFilter, allAccountEntries, platformFilter, priorityIntervalFilter]);

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
    const query = channelSearch.trim().toLowerCase();
    return data.channels
      .map((channel) => {
        const channelMatches = !query || channelSearchText(channel).includes(query);
        const accounts = (channel.accounts || []).filter(
          (account) =>
            upstreamAccountMatchesStatus(account, channelStatusFilter) &&
            (channelMatches || accountSearchText(account).includes(query)),
        );
        const channelStatusMatches = matchesChannelStatus(channel, channelStatusFilter);
        return { channel, accounts, visible: (channelMatches && channelStatusMatches) || accounts.length > 0 };
      })
      .filter((entry) => entry.visible);
  }, [channelSearch, channelStatusFilter, data.channels]);

  const filteredUnassigned = useMemo(() => {
    const query = channelSearch.trim().toLowerCase();
    if (channelStatusFilter !== "all" && channelStatusFilter !== "attention") return [];
    return data.unassigned_accounts.filter((account) => !query || accountSearchText(account).includes(query));
  }, [channelSearch, channelStatusFilter, data.unassigned_accounts]);

  const setChannelBusy = (channel: UpstreamChannel, action: string | null) => {
    const key = channelKey(channel);
    setBusyChannels((current) => updateBusyMap(current, key, action));
  };

  const setAccountBusy = (account: UpstreamAccount, action: string | null) => {
    const key = accountKey(account);
    setBusyAccounts((current) => updateBusyMap(current, key, action));
  };

  const rememberDialogTrigger = () => {
    const activeElement = document.activeElement;
    if (
      activeElement instanceof HTMLElement
      && !dialogRef.current?.contains(activeElement)
    ) {
      lastFocusedElementRef.current = activeElement;
    }
  };

  const openChannelConfig = (channel: UpstreamChannel) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setEditingChannel(channel);
    setDialogError("");
    setChannelForm({
      displayName: channel.display_name || "",
      baseUrl: channelBaseUrl(channel),
      managementBaseUrl: channel.management_base_url || "",
      upstreamType: channel.upstream_type || "auto",
      probeEnabled: channel.probe_enabled !== false,
      accessToken: "",
      clearAccessToken: false,
      refreshToken: "",
      clearRefreshToken: false,
      upstreamUserId: channel.upstream_user_id || "",
      manualRechargeMultiplier: numberInputValue(channel.manual_recharge_multiplier),
    });
  };

  const openAccountConfig = (account: UpstreamAccount, fallbackChannel?: UpstreamChannel) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setEditingAccount(account);
    setDialogError("");
    setAccountForm({
      channelId: String(account.channel_id ?? fallbackChannel?.id ?? ""),
      apiKey: "",
      manualGroupMultiplier: numberInputValue(account.manual_group_multiplier),
      remoteName: account.remote_name || "",
    });
  };

  const openChannelAccounts = (channel: UpstreamChannel) => {
    rememberDialogTrigger();
    setAccountUpstreamDialog(null);
    setAccountCollectionDialog({
      accounts: channel.accounts || [],
      channel,
      title: channelDisplayName(channel),
    });
  };

  const openUnassignedAccounts = () => {
    rememberDialogTrigger();
    setAccountUpstreamDialog(null);
    setAccountCollectionDialog({
      accounts: data.unassigned_accounts,
      channel: null,
      title: "待分配账号",
    });
  };

  const openAccountUpstream = (entry: UpstreamAccountEntry) => {
    if (!entry.channel) return;
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(entry.channel);
  };

  const openPriorityIntervalConfig = (interval?: PriorityInterval) => {
    rememberDialogTrigger();
    setEditingPriorityInterval(interval || null);
    setPriorityIntervalForm(interval ? {
      name: interval.name,
      startPriority: String(interval.start_priority),
      endPriority: String(interval.end_priority),
      step: String(interval.step),
    } : emptyPriorityIntervalForm);
    setDialogError("");
    setPriorityIntervalDialogOpen(true);
  };

  const setAccountPriorityInterval = async (
    account: UpstreamAccount,
    priorityIntervalId: number | string | null,
  ) => {
    const confirmIdentityRebind = priorityIntervalAssignmentNeedsConfirmation(account);
    if (
      confirmIdentityRebind
      && !window.confirm(
        "这是升级前尚未绑定身份的本地配置。继续会先校验并认领当前 sub2api 账号，再分配优先级区间，是否确认？",
      )
    ) return;
    const finishOperation = onOperationStart();
    setAccountBusy(account, "priority");
    setError("");
    setNotice("");
    try {
      await api.setUpstreamAccountPriorityInterval(account.sub2api_account_id, {
        priority_interval_id: priorityIntervalId,
        expected_identity_fingerprint: expectedIdentityFingerprint(account),
        confirm_identity_rebind: confirmIdentityRebind,
      });
      await loadData(true);
      setNotice(
        priorityIntervalId === null
          ? `${accountDisplayName(account)} 已取消优先级区间，当前远端优先级保持不变。`
          : `${accountDisplayName(account)} 的优先级区间已更新。`,
      );
    } catch (reason) {
      setError(errorMessage(reason, "优先级区间分配失败"));
    } finally {
      setAccountBusy(account, null);
      finishOperation();
    }
  };

  const savePriorityInterval = async (event: FormEvent) => {
    event.preventDefault();
    const finishOperation = onOperationStart();
    setSavingDialog(true);
    setDialogError("");
    try {
      const payload = priorityIntervalPayload(priorityIntervalForm);
      if (editingPriorityInterval) {
        await api.updatePriorityInterval(editingPriorityInterval.id, payload);
      } else {
        await api.createPriorityInterval(payload);
      }
      closeDialog();
      await loadData(true);
      setError("");
      setNotice(editingPriorityInterval ? "优先级区间已更新并重新计算。" : "优先级区间已创建。");
    } catch (reason) {
      setDialogError(errorMessage(reason, "优先级区间保存失败"));
    } finally {
      setSavingDialog(false);
      finishOperation();
    }
  };

  const deletePriorityInterval = async (interval: PriorityInterval) => {
    const accountCount = priorityIntervalAccountCount(interval, allAccounts);
    const detail = accountCount
      ? `\n\n${accountCount} 个账号会变为“未选定区间”，它们当前的远端优先级不会被重置。`
      : "";
    if (!window.confirm(`确认删除优先级区间「${interval.name}」？${detail}`)) return;
    const finishOperation = onOperationStart();
    setPriorityIntervalsBusy(true);
    setError("");
    setNotice("");
    try {
      await api.deletePriorityInterval(interval.id);
      await loadData(true);
      if (priorityIntervalFilter === String(interval.id)) setPriorityIntervalFilter("all");
      setNotice("优先级区间已删除。");
    } catch (reason) {
      setError(errorMessage(reason, "优先级区间删除失败"));
    } finally {
      setPriorityIntervalsBusy(false);
      finishOperation();
    }
  };

  const rebalancePriorityIntervals = async () => {
    const finishOperation = onOperationStart();
    setPriorityIntervalsBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.rebalancePriorityIntervals();
      await loadData(true);
      const changed = finiteNumber(result.updated);
      const failed = finiteNumber(result.failed);
      setNotice(
        result.message
        || `优先级重排完成${changed === null ? "" : `，更新 ${changed} 个账号`}${failed ? `，失败 ${failed} 个` : ""}。`,
      );
    } catch (reason) {
      setError(errorMessage(reason, "优先级重排失败"));
    } finally {
      setPriorityIntervalsBusy(false);
      finishOperation();
    }
  };

  const saveChannel = async (event: FormEvent) => {
    event.preventDefault();
    if (!editingChannel) return;
    const finishOperation = onOperationStart();
    setSavingDialog(true);
    setDialogError("");
    try {
      const baseUrl = channelForm.baseUrl.trim();
      if (!baseUrl) throw new Error("请填写上游渠道地址");
      assertHttpsUrl(baseUrl);
      const managementBaseUrl = channelForm.managementBaseUrl.trim();
      if (managementBaseUrl) assertHttpsUrl(managementBaseUrl);
      const credentialRebind = channelCredentialBindingChanged(
        editingChannel,
        baseUrl,
        managementBaseUrl,
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
        probe_enabled: channelForm.probeEnabled,
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
      finishOperation();
    }
  };

  const saveAccount = async (event: FormEvent) => {
    event.preventDefault();
    if (!editingAccount) return;
    const finishOperation = onOperationStart();
    setSavingDialog(true);
    setDialogError("");
    try {
      const selectedChannel = data.channels.find((channel) => String(channel.id) === accountForm.channelId);
      const payload = buildUpstreamAccountUpdatePayload({
        account: editingAccount,
        apiKey: accountForm.apiKey,
        channelId: selectedChannel?.id ?? null,
        manualGroupMultiplier: accountForm.manualGroupMultiplier,
        remoteName: accountForm.remoteName,
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
      if (editingAccount.identity_rebind_required) {
        const confirmed = window.confirm(
          editingAccount.identity_binding_status === "mismatch"
            ? "检测到 sub2api 账号 ID 对应的身份已经变化。继续会把保留的本地上游配置和凭据重新绑定到当前账号，是否确认？"
            : "这是升级前尚未绑定身份的本地配置。继续会把它认领到当前 sub2api 账号，是否确认？",
        );
        if (!confirmed) return;
        payload.confirm_identity_rebind = true;
      }
      await api.updateUpstreamAccount(editingAccount.sub2api_account_id, payload);
      closeDialog();
      await loadData(true);
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogsHasMore(false);
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
      finishOperation();
    }
  };

  const discoverChannel = async (channel: UpstreamChannel) => {
    const finishOperation = onOperationStart();
    setChannelBusy(channel, "discover");
    setError("");
    setNotice("");
    try {
      await api.discoverUpstreamChannel(channel.id);
      await loadData(true);
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogsHasMore(false);
      setRateLogsLoaded(false);
      setNotice(channelDiscoverySuccessMessage(rateWritesEnabled, channelDisplayName(channel)));
    } catch (reason) {
      setError(errorMessage(reason, channelDiscoveryErrorMessage(rateWritesEnabled, channelDisplayName(channel))));
    } finally {
      setChannelBusy(channel, null);
      finishOperation();
    }
  };

  const discoverAll = async () => {
    if (!data.channels.length) {
      setNotice(upstreamDiscoveryCopy(rateWritesEnabled).empty);
      return;
    }
    const finishOperation = onOperationStart();
    setBulkDiscovering(true);
    setError("");
    setNotice("");
    try {
      const bindingCounts = upstreamLegacyBindingCounts(data);
      const confirmationRequired = bindingCounts.unbound > 0 || bindingCounts.originRebind > 0;
      if (
        confirmationRequired
        && !window.confirm(apiAccountLegacyBindingConfirmationMessage(bindingCounts))
      ) {
        setNotice("已取消 API 账号同步。");
        return;
      }
      const result = await api.syncApiKeyAccounts(data, confirmationRequired);
      if (result.overview) {
        const normalized = {
          ...result.overview,
          channels: Array.isArray(result.overview.channels) ? result.overview.channels : [],
          priority_intervals: Array.isArray(result.overview.priority_intervals) ? result.overview.priority_intervals : [],
          unassigned_accounts: Array.isArray(result.overview.unassigned_accounts) ? result.overview.unassigned_accounts : [],
        };
        hasDataRef.current = true;
        setData(normalized);
        onCacheChange(normalized, cacheBaseUrl);
        setLiveDataValidated(true);
      } else {
        await loadData(true);
      }
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogsHasMore(false);
      setRateLogsLoaded(false);
      setNotice(apiAccountSyncMessage(result, rateWritesEnabled));
    } catch (reason) {
      setError(errorMessage(reason, upstreamDiscoveryCopy(rateWritesEnabled).allError));
    } finally {
      setBulkDiscovering(false);
      finishOperation();
    }
  };

  const toggleAccountEnabled = async (account: UpstreamAccount) => {
    const finishOperation = onOperationStart();
    const currentlyEnabled = account.remote_schedulable !== false;
    setAccountBusy(account, currentlyEnabled ? "disable" : "enable");
    setError("");
    setNotice("");
    try {
      await api.setUpstreamAccountEnabled(
        account.sub2api_account_id,
        !currentlyEnabled,
        expectedIdentityFingerprint(account),
      );
      await loadData(true);
      setNotice(accountDisplayName(account) + (currentlyEnabled ? " 已禁用。" : " 已启用。"));
    } catch (reason) {
      setError(errorMessage(reason, currentlyEnabled ? "账号禁用失败" : "账号启用失败"));
    } finally {
      setAccountBusy(account, null);
      finishOperation();
    }
  };

  const deleteRemoteAccount = async (account: UpstreamAccount) => {
    const confirmed = window.confirm(
      "确认从 sub2api 删除「" + accountDisplayName(account) + "」？\n\n账号 #" +
        account.sub2api_account_id +
        " 及其本地上游配置会一并删除，此操作无法撤销。",
    );
    if (!confirmed) return;
    const finishOperation = onOperationStart();
    setAccountBusy(account, "delete");
    setError("");
    setNotice("");
    try {
      await api.deleteRemoteUpstreamAccount(
        account.sub2api_account_id,
        expectedIdentityFingerprint(account),
      );
      await loadData(true);
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogsHasMore(false);
      setRateLogsLoaded(false);
      setNotice("已从 sub2api 删除 " + accountDisplayName(account) + "。");
    } catch (reason) {
      setError(errorMessage(reason, "sub2api 账号删除失败"));
    } finally {
      setAccountBusy(account, null);
      finishOperation();
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
          aria-selected={subview === "accounts"}
          className={subview === "accounts" ? "active" : ""}
          onClick={() => onSubviewChange("accounts")}
          role="tab"
          type="button"
        >
          <LayoutGrid size={16} />
          <span>账号管理</span>
        </button>
        <button
          aria-selected={subview === "channels"}
          className={subview === "channels" ? "active" : ""}
          onClick={() => onSubviewChange("channels")}
          role="tab"
          type="button"
        >
          <Globe2 size={16} />
          <span>上游渠道</span>
        </button>
        <button
          aria-selected={subview === "intervals"}
          className={subview === "intervals" ? "active" : ""}
          onClick={() => onSubviewChange("intervals")}
          role="tab"
          type="button"
        >
          <ListOrdered size={16} />
          <span>优先级区间</span>
        </button>
        <button
          aria-selected={subview === "rate-log"}
          className={subview === "rate-log" ? "active" : ""}
          onClick={() => onSubviewChange("rate-log")}
          role="tab"
          type="button"
        >
          <History size={16} />
          <span>上游变化</span>
        </button>
      </div>

      {subview !== "rate-log" ? <>
      <div className="api-key-summary" aria-label="上游渠道汇总">
        <SummaryItem label="上游渠道" value={summary.channels} tone="blue" />
        <SummaryItem label="API Key 账号" value={summary.accounts} tone="green" />
        <SummaryItem label="倍率不一致" value={summary.pending} tone="amber" />
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

      {subview === "accounts" ? (
        <section className="api-key-panel api-key-accounts-panel" aria-label="API Key 账号">
          <div className="api-key-panel-head">
            <div>
              <h2>API Key 账号</h2>
              <p>按综合倍率从低到高排列；综合倍率不可用的账号显示在末尾。</p>
            </div>
          </div>

          <div className="api-key-filters api-key-account-filters">
            <label className="api-key-search">
              <Search size={16} />
              <span className="api-key-sr-only">搜索 API Key 账号</span>
              <input
                onChange={(event) => setAccountSearch(event.target.value)}
                placeholder="搜索账号、渠道、ID 或分组"
                type="search"
                value={accountSearch}
              />
              <small>{filteredAccountEntries.length}/{allAccountEntries.length}</small>
            </label>
            <label className="api-key-filter-select">
              <span>优先级区间</span>
              <select
                onChange={(event) => setPriorityIntervalFilter(event.target.value as PriorityIntervalFilter)}
                value={priorityIntervalFilter}
              >
                <option value="all">全部区间</option>
                <option value="unassigned">未选定区间</option>
                {priorityIntervals.map((interval) => (
                  <option key={String(interval.id)} value={String(interval.id)}>
                    {interval.name} [{interval.start_priority}, {interval.end_priority})
                  </option>
                ))}
              </select>
            </label>
            <label className="api-key-filter-select">
              <span>平台</span>
              <select onChange={(event) => setPlatformFilter(event.target.value as PlatformFilter)} value={platformFilter}>
                <option value="all">全部平台</option>
                {platformOptions.platforms.map((option) => (
                  <option key={option.value} value={option.value}>{upstreamStatusLabel(option.label)}</option>
                ))}
                {platformOptions.hasUnknown ? <option value="__unknown__">未知平台</option> : null}
              </select>
            </label>
            <label className="api-key-filter-select">
              <span>状态</span>
              <select onChange={(event) => setAccountStatusFilter(event.target.value as StatusFilter)} value={accountStatusFilter}>
                <option value="all">全部状态</option>
                <option value="pending">倍率不一致</option>
                <option value="attention">需要处理</option>
                <option value="undiscovered">尚未探测</option>
              </select>
            </label>
          </div>

          {loading && allAccountEntries.length === 0 ? (
            <div className="api-key-empty">
              <RefreshCcw className="spin" size={18} />
              <span>正在读取 API Key 账号…</span>
            </div>
          ) : filteredAccountEntries.length ? (
            <div className="api-key-account-grid api-key-account-management-grid">
              {filteredAccountEntries.map((entry) => (
                <AccountCard
                  account={entry.account}
                  busyAction={busyAccounts[accountKey(entry.account)]}
                  channel={entry.channel}
                  displayTimeZone={displayTimeZone}
                  globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                  key={accountKey(entry.account)}
                  onConfigure={() => openAccountConfig(entry.account, entry.channel || undefined)}
                  onDelete={() => deleteRemoteAccount(entry.account)}
                  onPriorityIntervalChange={(intervalId) => void setAccountPriorityInterval(entry.account, intervalId)}
                  onShowChannel={() => openAccountUpstream(entry)}
                  onToggle={() => toggleAccountEnabled(entry.account)}
                  priorityIntervals={priorityIntervals}
                  rateWritesEnabled={rateWritesEnabled}
                />
              ))}
            </div>
          ) : (
            <div className="api-key-empty">
              <KeyRound size={18} />
              <span>{allAccountEntries.length ? "没有匹配的 API Key 账号" : "暂无可管理的 API Key 账号"}</span>
            </div>
          )}
        </section>
      ) : null}

      {subview === "channels" ? (
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
          </div>
        </div>

        <div className="api-key-filters">
          <label className="api-key-search">
            <Search size={16} />
            <span className="api-key-sr-only">搜索渠道或账号</span>
            <input
              onChange={(event) => setChannelSearch(event.target.value)}
              placeholder="搜索渠道、URL、账号、ID 或分组"
              type="search"
              value={channelSearch}
            />
            <small>{filteredChannels.length}/{data.channels.length}</small>
          </label>
          <label className="api-key-filter-select">
            <span>状态</span>
            <select onChange={(event) => setChannelStatusFilter(event.target.value as StatusFilter)} value={channelStatusFilter}>
              <option value="all">全部状态</option>
              <option value="pending">倍率不一致</option>
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
            {filteredChannels.map(({ channel }) => (
              <ChannelCard
                accountCount={channelAccountCount(channel)}
                busyAction={busyChannels[channelKey(channel)]}
                channel={channel}
                displayTimeZone={displayTimeZone}
                globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                key={channelKey(channel)}
                onConfigureChannel={() => openChannelConfig(channel)}
                onDiscover={() => void discoverChannel(channel)}
                onShowAccounts={() => openChannelAccounts(channel)}
                rateWritesEnabled={rateWritesEnabled}
              />
            ))}
            {filteredUnassigned.length ? (
              <UnassignedCard
                accountCount={data.unassigned_accounts.length}
                onShowAccounts={openUnassignedAccounts}
              />
            ) : null}
          </div>
        )}
      </section>
      ) : null}

      {subview === "intervals" ? (
        <PriorityIntervalsView
          accounts={allAccounts}
          busy={anyBusy || mutationControlsDisabled}
          intervals={priorityIntervals}
          onCreate={() => openPriorityIntervalConfig()}
          onDelete={(interval) => void deletePriorityInterval(interval)}
          onEdit={openPriorityIntervalConfig}
          onRebalance={() => void rebalancePriorityIntervals()}
          rebalancing={priorityIntervalsBusy}
        />
      ) : null}

      {accountCollectionDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow={`${accountCollectionDialog.accounts.length} 个 API Key 账号`}
          onClose={closeDialog}
          saving={false}
          title={accountCollectionDialog.title}
        >
          {accountCollectionDialog.accounts.length ? (
            <div className="api-key-account-grid api-key-dialog-account-grid">
              {sortUpstreamAccountEntries(accountCollectionDialog.accounts.map((account) => ({
                account,
                channel: accountCollectionDialog.channel,
              }))).map((entry) => (
                <AccountCard
                  account={entry.account}
                  busyAction={busyAccounts[accountKey(entry.account)]}
                  channel={entry.channel}
                  displayTimeZone={displayTimeZone}
                  globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                  key={accountKey(entry.account)}
                  onConfigure={() => openAccountConfig(entry.account, entry.channel || undefined)}
                  onDelete={() => {
                    closeDialog();
                    void deleteRemoteAccount(entry.account);
                  }}
                  onPriorityIntervalChange={(intervalId) => {
                    closeDialog();
                    void setAccountPriorityInterval(entry.account, intervalId);
                  }}
                  onShowChannel={() => openAccountUpstream(entry)}
                  onToggle={() => {
                    closeDialog();
                    void toggleAccountEnabled(entry.account);
                  }}
                  priorityIntervals={priorityIntervals}
                  rateWritesEnabled={rateWritesEnabled}
                />
              ))}
            </div>
          ) : <div className="api-key-empty"><KeyRound size={18} /><span>当前没有 API Key 账号</span></div>}
        </Modal>
      ) : null}

      {accountUpstreamDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow="账号所属上游"
          onClose={closeDialog}
          saving={false}
          title={channelDisplayName(accountUpstreamDialog)}
        >
          <div className="api-key-dialog-channel-card">
            <ChannelCard
              accountCount={channelAccountCount(accountUpstreamDialog)}
              busyAction={busyChannels[channelKey(accountUpstreamDialog)]}
              channel={accountUpstreamDialog}
              displayTimeZone={displayTimeZone}
              globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
              onConfigureChannel={() => openChannelConfig(accountUpstreamDialog)}
              onDiscover={() => {
                closeDialog();
                void discoverChannel(accountUpstreamDialog);
              }}
              onShowAccounts={() => openChannelAccounts(accountUpstreamDialog)}
              rateWritesEnabled={rateWritesEnabled}
            />
          </div>
        </Modal>
      ) : null}

      {priorityIntervalDialogOpen ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow="优先级区间"
          onClose={closeDialog}
          saving={savingDialog}
          title={editingPriorityInterval ? `编辑 · ${editingPriorityInterval.name}` : "新建优先级区间"}
        >
          <form className="api-key-config-form" onSubmit={savePriorityInterval}>
            <div className="api-key-config-fields">
              <label className="api-key-field api-key-field--wide">
                <span>区间名称</span>
                <input
                  autoFocus
                  maxLength={100}
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, name: event.target.value }))}
                  placeholder="例如：低成本优先"
                  required
                  value={priorityIntervalForm.name}
                />
              </label>
              <label className="api-key-field">
                <span>起始优先级</span>
                <input
                  inputMode="numeric"
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, startPriority: event.target.value }))}
                  required
                  step="1"
                  type="number"
                  value={priorityIntervalForm.startPriority}
                />
              </label>
              <label className="api-key-field">
                <span>结束优先级（不包含）</span>
                <input
                  inputMode="numeric"
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, endPriority: event.target.value }))}
                  required
                  step="1"
                  type="number"
                  value={priorityIntervalForm.endPriority}
                />
              </label>
              <label className="api-key-field api-key-field--wide">
                <span>优先级间隔</span>
                <input
                  inputMode="numeric"
                  min="1"
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, step: event.target.value }))}
                  required
                  step="1"
                  type="number"
                  value={priorityIntervalForm.step}
                />
                <small>账号较多时会自动缩短间隔；间隔 1 仍放不下时，多出的账号使用区间最后一个优先级。</small>
              </label>
            </div>
            <DialogError message={dialogError} />
            <DialogActions onCancel={closeDialog} saving={savingDialog} />
          </form>
        </Modal>
      ) : null}

      {editingChannel ? (
        <Modal title={"配置渠道 · " + channelDisplayName(editingChannel)} eyebrow="上游渠道" onClose={closeDialog} dialogRef={dialogRef} saving={savingDialog}>
          <form className="api-key-config-form api-key-channel-form" onSubmit={saveChannel}>
            <div className="api-key-config-fields">
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

              <label className="api-key-field api-key-field--wide api-key-clear-token">
                <input
                  checked={channelForm.probeEnabled}
                  onChange={(event) =>
                    setChannelForm((current) => ({
                      ...current,
                      probeEnabled: event.target.checked,
                    }))
                  }
                  type="checkbox"
                />
                <span>纳入自动上游探测</span>
                <small>仅在全局“同步 API Key 账号上游”开启时生效；关闭后保留上次观测结果，也不会自动修改该渠道账号的倍率、优先级或启用状态。</small>
              </label>

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
            </div>
            <DialogError message={dialogError} />
            <DialogActions onCancel={closeDialog} saving={savingDialog} />
          </form>
        </Modal>
      ) : null}

      {editingAccount ? (
        <Modal title={"配置账号 · " + accountDisplayName(editingAccount)} eyebrow={"#" + editingAccount.sub2api_account_id} onClose={closeDialog} dialogRef={dialogRef} saving={savingDialog}>
          <form className="api-key-config-form" onSubmit={saveAccount}>
            <div className="api-key-config-fields">
              <label className="api-key-field api-key-field--wide">
                <span>账号名称</span>
                <input
                  autoFocus
                  maxLength={100}
                  onChange={(event) => setAccountForm((current) => ({ ...current, remoteName: event.target.value }))}
                  required
                  value={accountForm.remoteName}
                />
              </label>

              <label className="api-key-field api-key-field--wide">
                <span>所属上游渠道</span>
                <select
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
            </div>
            <DialogError message={dialogError} />
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
  accountCount,
  displayTimeZone,
  busyAction,
  globallyDisabled,
  onConfigureChannel,
  onDiscover,
  onShowAccounts,
  rateWritesEnabled,
}: {
  channel: UpstreamChannel;
  accountCount: number;
  displayTimeZone: string;
  busyAction?: string;
  globallyDisabled: boolean;
  onConfigureChannel: () => void;
  onDiscover: () => void;
  onShowAccounts: () => void;
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
  const balanceDetails = balanceDetail(channel);
  const todayUsage = formatTodayBalanceUsed(channel, displayTimeZone);
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
        <ChannelStat
          badge={<StatusChip status={channel.balance_status || channel.status || "not_checked"} />}
          icon={<WalletCards size={16} />}
          label="上游余额"
        >
          <div className="api-key-channel-balance-line">
            <strong>{formatUpstreamBalance(channel.balance_remaining, channel.balance_unit, 2)}</strong>
            <span className={"api-key-channel-today-usage api-key-chip api-key-chip--" + todayUsage.tone}>
              {todayUsage.label} | {todayUsage.value}
            </span>
          </div>
          {balanceDetails ? <span>{balanceDetails}</span> : null}
        </ChannelStat>
        <ChannelStat icon={<BadgeDollarSign size={16} />} label="充值成本">
          <strong>{"¥" + formatCostPerUsd(channel.effective_recharge_multiplier) + " / $1"}</strong>
        </ChannelStat>
        <ChannelStat badge={<StatusChip status={status} />} icon={<Radar size={16} />} label="最近探测">
          <strong>{formatDate(channel.last_discovered_at || channel.checked_at, displayTimeZone)}</strong>
        </ChannelStat>
      </div>

      <div className="api-key-channel-credential-line">
        <span className={channel.probe_enabled === false ? "needs-attention" : "is-ready"}>
          <Radar size={13} />
          {channel.probe_enabled === false ? "自动探测已关闭" : "自动探测已开启"}
        </span>
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
        <button
          aria-label={`查看 ${channelDisplayName(channel)} 的 ${accountCount} 个 API Key 账号`}
          className="api-key-channel-account-button"
          onClick={onShowAccounts}
          type="button"
        >
          <UsersRound size={16} />
          <span>API Key 账号 {accountCount} 个</span>
          <ArrowRight size={15} />
        </button>
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
  accountCount,
  onShowAccounts,
}: {
  accountCount: number;
  onShowAccounts: () => void;
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
        <button
          aria-label={`查看 ${accountCount} 个待分配 API Key 账号`}
          className="api-key-channel-account-button"
          onClick={onShowAccounts}
          type="button"
        >
          <UsersRound size={16} />
          <span>API Key 账号 {accountCount} 个</span>
          <ArrowRight size={15} />
        </button>
      </section>
    </article>
  );
}

function AccountCard({
  account,
  busyAction,
  channel,
  displayTimeZone,
  globallyDisabled,
  onConfigure,
  onDelete,
  onPriorityIntervalChange,
  onShowChannel,
  onToggle,
  priorityIntervals,
  rateWritesEnabled,
}: {
  account: UpstreamAccount;
  busyAction?: string;
  channel: UpstreamChannel | null;
  displayTimeZone: string;
  globallyDisabled: boolean;
  onConfigure: () => void;
  onDelete: () => void;
  onPriorityIntervalChange: (intervalId: number | string | null) => void;
  onShowChannel: () => void;
  onToggle: () => void;
  priorityIntervals: PriorityInterval[];
  rateWritesEnabled: boolean;
}) {
  const busy = Boolean(busyAction) || globallyDisabled;
  const identityBlocked = Boolean(account.identity_rebind_required);
  const priorityIdentityBlocked = priorityIntervalAssignmentBlocked(account);
  const current = finiteNumber(account.current_rate);
  const target = finiteNumber(account.target_rate);
  const groupMultiplier = finiteNumber(account.effective_group_multiplier);
  const normalizedMultiplier = accountCompositeMultiplier(account);
  const groupMultiplierTitle = groupMultiplier === null
    ? ""
    : normalizedMultiplier === null
      ? "上游原始分组倍率 " + formatMultiplier(groupMultiplier)
      : "上游原始分组倍率 " + formatMultiplier(groupMultiplier)
        + "；1:1 折算 " + formatMultiplier(normalizedMultiplier)
        + "（分组倍率 × 上游充值成本）";
  const enabled = account.remote_schedulable !== false;
  const effectiveStatus = enabled ? account.remote_status || "enabled" : "disabled";
  const currentRateLabel = formatMultiplier(current);
  const targetRateLabel = formatMultiplier(target);
  const combinedRateLabel = formatMultiplier(normalizedMultiplier);
  const rechargeMultiplierLabel = formatMultiplier(account.effective_recharge_multiplier);
  const combinedRateTitle = normalizedMultiplier === null
    ? "综合倍率暂不可用：缺少上游充值倍率或分组倍率"
    : `综合倍率为 ${combinedRateLabel}（上游充值倍率 ${rechargeMultiplierLabel} × 分组倍率 ${formatMultiplier(groupMultiplier)}）`;
  const currentRateTitle = `当前 sub2api 中账号计费倍率为 ${currentRateLabel}`;
  const targetRateTitle = `根据上游分组倍率和充值成本计算的目标计费倍率为 ${targetRateLabel}；${accountRateStatusLabel(target, account.would_change, rateWritesEnabled)}`;
  const priority = finiteNumber(account.priority);
  const desiredPriority = finiteNumber(account.desired_priority);
  const hasPriorityInterval = account.priority_interval_id !== null && account.priority_interval_id !== undefined;
  const multiplierUnavailable = hasPriorityInterval && (
    account.priority_sync_status === "multiplier_unavailable" || normalizedMultiplier === null
  );
  const priorityPending = desiredPriority !== null && priority !== desiredPriority;
  const priorityState = multiplierUnavailable
    ? "等待综合倍率"
    : account.priority_sync_error
      ? "优先级同步失败"
      : !hasPriorityInterval
        ? "未选定区间"
        : priorityPending
          ? "等待写入"
          : priorityStatusLabel(account.priority_sync_status);
  const usageAmount = finiteNumber(account.upstream_usage_amount);
  const usageCost = rechargeAdjustedUsage(usageAmount, account.effective_recharge_multiplier);
  const usageDetail = usageAmount === null
    ? "使用额待同步"
    : usageCost === null
      ? "充值倍率待同步"
      : `× 充值倍率 = ¥${formatMoney(usageCost)}`;
  const accountUpstreamType = channel
    ? resolvedChannelType(channel)
    : account.resolved_upstream_type || account.detected_upstream_type || account.upstream_type;
  const showUsage = shouldShowUpstreamAccountUsage(accountUpstreamType);
  const hasAccountMeta = Boolean(account.auto_disabled_reason || identityBlocked);
  return (
    <article className={"api-key-account-card" + (enabled ? "" : " api-key-account-card--disabled")}>
      <header className="api-key-account-card-head">
        <div className="api-key-account-title-line">
          <div className="api-key-account-name">
            <strong title={accountDisplayName(account)}>{accountDisplayName(account)}</strong>
            <span className="api-key-mono">#{account.sub2api_account_id}</span>
          </div>
          <StatusChip status={effectiveStatus} />
          {account.remote_platform?.trim() ? <PlatformChip platform={account.remote_platform} /> : null}
        </div>
        {hasAccountMeta ? <div className="api-key-inline-chips api-key-account-meta-chips">
          {account.auto_disabled_reason ? (
            <span
              className="api-key-chip api-key-chip--danger"
              title={
                upstreamChangeReasonLabel(account.auto_disabled_reason)
                + (account.last_auto_disabled_at
                  ? ` · ${formatDate(account.last_auto_disabled_at, displayTimeZone)}`
                  : "")
              }
            >
              上游失效自动禁用
            </span>
          ) : null}
          {identityBlocked ? (
            <span className="api-key-chip api-key-chip--warn">
              {account.identity_binding_status === "mismatch" ? "身份已变化" : "身份待认领"}
            </span>
          ) : null}
        </div> : null}
      </header>
      <div className="api-key-account-card-group">
        <div className="api-key-account-group-label">
          <span>上游分组</span>
          <AccountUpstreamHealthChip
            checkedAt={account.upstream_key_checked_at || account.upstream_health_checked_at}
            displayTimeZone={displayTimeZone}
            kind="key"
            label="Key"
            status={account.upstream_key_status}
          />
          <AccountUpstreamHealthChip
            checkedAt={account.upstream_group_checked_at || account.upstream_health_checked_at}
            displayTimeZone={displayTimeZone}
            kind="group"
            label="分组"
            status={account.upstream_group_status}
          />
        </div>
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
        {showUsage ? <div className="api-key-account-usage">
          <span>累计使用</span>
          <strong>{formatUpstreamBalance(usageAmount, account.upstream_usage_unit)}</strong>
          <small>{usageDetail}</small>
        </div> : null}
      </div>
      <div className="api-key-account-priority">
        <label>
          <span>优先级区间</span>
          <select
            aria-label={`设置 ${accountDisplayName(account)} 的优先级区间`}
            disabled={busy || priorityIdentityBlocked}
            onChange={(event) => onPriorityIntervalChange(
              event.target.value
                ? priorityIntervals.find((interval) => String(interval.id) === event.target.value)?.id ?? null
                : null,
            )}
            value={String(account.priority_interval_id ?? "")}
          >
            <option value="">未选定区间</option>
            {priorityIntervals.map((interval) => (
              <option key={String(interval.id)} value={String(interval.id)}>
                {interval.name} [{interval.start_priority}, {interval.end_priority})
              </option>
            ))}
          </select>
        </label>
        <div
          className={
            "api-key-account-priority-value"
            + (multiplierUnavailable ? " is-waiting" : "")
            + (account.priority_sync_error ? " is-error" : "")
          }
          title={account.priority_sync_error || priorityState}
        >
          <span className="api-key-account-priority-heading">
            <span>调度优先级</span>
            <small>{account.priority_sync_error || priorityState}</small>
            <span className="api-key-account-priority-number">
              <strong>{formatPriority(priority)}</strong>
              {priorityPending ? <><ArrowRight size={13} /><b>{formatPriority(desiredPriority)}</b></> : null}
            </span>
          </span>
          <div className="api-key-inline-chips api-key-account-rate-chips">
            <RateChip label="综合" value={combinedRateLabel} title={combinedRateTitle} tone="combined" />
            <RateChip label="当前" value={currentRateLabel} title={currentRateTitle} tone="current" />
            <RateChip label="目标" pending={account.would_change === true} value={targetRateLabel} title={targetRateTitle} tone="target" />
          </div>
        </div>
      </div>
      <footer className="api-key-account-card-actions">
        <button
          aria-label={channel ? `查看 ${accountDisplayName(account)} 的上游渠道` : `${accountDisplayName(account)} 未分配上游渠道`}
          className="api-key-icon-button"
          disabled={!channel}
          onClick={onShowChannel}
          title={channel ? "查看上游渠道" : "未分配上游渠道"}
          type="button"
        >
          <Globe2 size={15} />
        </button>
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
          disabled={busy || identityBlocked}
          onClick={onToggle}
          title={enabled ? "禁用账号" : "启用账号"}
          type="button"
        >
          {enabled ? <PowerOff size={15} /> : <Power size={15} />}
        </button>
        <button
          aria-label={"从 sub2api 删除 " + accountDisplayName(account)}
          className="api-key-icon-button api-key-icon-button--danger"
          disabled={busy || identityBlocked}
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

function RateChip({ label, pending = false, value, title, tone }: {
  label: string;
  pending?: boolean;
  value: string;
  title: string;
  tone: "combined" | "current" | "target";
}) {
  return (
    <span
      aria-label={title}
      className={`api-key-chip api-key-account-rate-chip api-key-account-rate-chip--${tone}${pending ? " is-pending" : ""}`}
      title={title}
    >
      <span>{label}</span><strong>{value}</strong>
    </span>
  );
}

function AccountUpstreamHealthChip({
  checkedAt,
  displayTimeZone,
  kind,
  label,
  status,
}: {
  checkedAt?: string | null;
  displayTimeZone: string;
  kind: Extract<UpstreamHealthKind, "key" | "group">;
  label: string;
  status?: string | null;
}) {
  const normalized = String(status || "").trim().toLowerCase();
  if (!normalized) return null;
  return (
    <span
      className={`api-key-chip api-key-chip--${upstreamStatusTone(normalized)}`}
      title={
        `${label}：${upstreamHealthStatusLabel(kind, normalized)}`
        + (checkedAt ? ` · ${formatDate(checkedAt, displayTimeZone)}` : "")
      }
    >
      {label} {upstreamHealthStatusLabel(kind, normalized)}
    </span>
  );
}

function PriorityIntervalsView({
  accounts,
  busy,
  intervals,
  onCreate,
  onDelete,
  onEdit,
  onRebalance,
  rebalancing,
}: {
  accounts: UpstreamAccount[];
  busy: boolean;
  intervals: PriorityInterval[];
  onCreate: () => void;
  onDelete: (interval: PriorityInterval) => void;
  onEdit: (interval: PriorityInterval) => void;
  onRebalance: () => void;
  rebalancing: boolean;
}) {
  const orderedIntervals = [...intervals].sort(
    (left, right) => left.start_priority - right.start_priority || String(left.id).localeCompare(String(right.id)),
  );
  return (
    <section className="api-key-panel api-key-priority-panel" aria-label="优先级区间">
      <div className="api-key-panel-head">
        <div>
          <h2>优先级区间</h2>
          <p>区间上界不包含；同一区间按综合倍率从低到高自动分配调度优先级。</p>
        </div>
        <div className="api-key-toolbar-actions">
          <button
            className="api-key-button api-key-button--secondary"
            disabled={busy || !intervals.length}
            onClick={onRebalance}
            type="button"
          >
            <Radar className={rebalancing ? "spin" : ""} size={16} />
            <span>{rebalancing ? "正在重排" : "重新计算"}</span>
          </button>
          <button className="api-key-button api-key-button--primary" disabled={busy} onClick={onCreate} type="button">
            <Plus size={16} />
            <span>新建区间</span>
          </button>
        </div>
      </div>

      {orderedIntervals.length ? (
        <div className="api-key-priority-grid">
          {orderedIntervals.map((interval) => {
            const assignedAccounts = accounts.filter(
              (account) => String(account.priority_interval_id ?? "") === String(interval.id),
            );
            const sortableCount = assignedAccounts.filter(
              (account) => accountCompositeMultiplier(account) !== null,
            ).length;
            const waitingCount = assignedAccounts.length - sortableCount;
            const capacity = Math.max(0, interval.end_priority - interval.start_priority);
            const sharedLastPriorityCount = sortableCount > capacity
              ? sortableCount - capacity + 1
              : 0;
            const effectiveStep = finiteNumber(interval.effective_step) ?? interval.step;
            return (
              <article className="api-key-priority-card" key={String(interval.id)}>
                <header>
                  <div>
                    <strong>{interval.name}</strong>
                    <span className="api-key-mono">[{interval.start_priority}, {interval.end_priority})</span>
                  </div>
                  <div className="api-key-row-actions">
                    <button
                      aria-label={`编辑优先级区间 ${interval.name}`}
                      className="api-key-icon-button"
                      disabled={busy}
                      onClick={() => onEdit(interval)}
                      title="编辑区间"
                      type="button"
                    >
                      <Pencil size={15} />
                    </button>
                    <button
                      aria-label={`删除优先级区间 ${interval.name}`}
                      className="api-key-icon-button api-key-icon-button--danger"
                      disabled={busy}
                      onClick={() => onDelete(interval)}
                      title="删除区间"
                      type="button"
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                </header>
                <div className="api-key-priority-card-stats">
                  <div><span>设定间隔</span><strong>{interval.step}</strong></div>
                  <div><span>实际间隔</span><strong>{effectiveStep}</strong></div>
                  <div><span>已选账号</span><strong>{assignedAccounts.length}</strong></div>
                  <div><span>参与排序</span><strong>{sortableCount}</strong></div>
                </div>
                {effectiveStep < interval.step ? (
                  <p className="api-key-priority-note">账号数量较多，实际间隔已自动缩短为 {effectiveStep}。</p>
                ) : null}
                {sharedLastPriorityCount ? (
                  <p className="api-key-priority-note is-warning">最后 {sharedLastPriorityCount} 个账号将共用优先级 {interval.end_priority - 1}。</p>
                ) : null}
                {waitingCount ? (
                  <p className="api-key-priority-note is-waiting">{waitingCount} 个账号等待综合倍率，不参与容量和优先级计算。</p>
                ) : null}
              </article>
            );
          })}
        </div>
      ) : (
        <div className="api-key-empty">
          <ListOrdered size={18} />
          <span>尚未设置优先级区间</span>
        </div>
      )}
    </section>
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
  logs: UpstreamChangeLog[];
  onApplyFilters: () => void;
  onClearFilters: () => void;
  onDraftFiltersChange: (filters: RateLogFilters) => void;
  onLoadMore: () => void;
  onRefresh: () => void;
}) {
  return (
    <section className="api-key-panel api-key-rate-log-panel" aria-label="上游变化记录">
      <div className="api-key-panel-head">
        <div>
          <h2>上游变化</h2>
          <p>记录上游 Key、分组、账号调度与倍率信息；综合倍率按分组倍率 × 上游充值倍率折算。</p>
        </div>
        <button
          aria-label="刷新上游变化记录"
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
          <span>正在读取上游变化…</span>
        </div>
      ) : logs.length === 0 ? (
        <div className="api-key-empty">
          <History size={18} />
          <span>{filtersApplied ? "当前日期范围内没有上游变化" : "暂无上游变化记录"}</span>
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
                    <span>{upstreamChangeReasonLabel(log.reason)}</span>
                  </div>
                  <div className="api-key-upstream-state-list" aria-label="上游状态变化">
                    <UpstreamStateTransition change={upstreamKeyStatusChange(log)} kind="key" label="Key" />
                    <UpstreamStateTransition change={upstreamGroupStatusChange(log)} kind="group" label="分组" />
                    <UpstreamStateTransition change={remoteSchedulableChange(log)} kind="account" label="调度" />
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

function UpstreamStateTransition({
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
  const title = change.direction === "changed"
    ? `${label}：${upstreamHealthStatusLabel(kind, change.oldValue)} -> ${upstreamHealthStatusLabel(kind, change.newValue)}`
    : `${label}：${upstreamHealthStatusLabel(kind, current)}`;
  return (
    <span className={`api-key-upstream-transition api-key-chip api-key-chip--${tone}`} title={title}>
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

function ChannelStat({
  badge,
  icon,
  label,
  children,
}: {
  badge?: React.ReactNode;
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="api-key-channel-stat">
      <div className="api-key-channel-stat-label">{icon}<span>{label}</span>{badge}</div>
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
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (!dialogRef.current?.contains(document.activeElement)) closeButtonRef.current?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [dialogRef]);

  return (
    <div
      className="api-key-modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !saving) onClose();
      }}
    >
      <section aria-labelledby={titleId} aria-modal="true" className="api-key-dialog" ref={dialogRef} role="dialog" tabIndex={-1}>
        <div className="api-key-dialog-head">
          <div><p>{eyebrow}</p><h2 id={titleId}>{title}</h2></div>
          <button aria-label="关闭弹窗" className="api-key-icon-button" disabled={saving} onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
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
    <div className="api-key-dialog-actions">
      <button className="api-key-button api-key-button--secondary" disabled={saving} onClick={onCancel} type="button">取消</button>
      <button className="api-key-button api-key-button--primary" disabled={saving} type="submit">
        <Save size={16} />
        <span>{saving ? "保存中" : "保存配置"}</span>
      </button>
    </div>
  );
}

function DialogError({ message }: { message: string }) {
  if (!message) return null;
  return (
    <div className="api-key-dialog-error">
      <div className="api-key-form-error" role="alert">{message}</div>
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
  const value = platform.trim();
  return <span className="api-key-chip api-key-chip--info" title={`平台：${value}`}>{value}</span>;
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
    account.upstream_key_status,
    account.upstream_group_status,
    account.auto_disabled_reason,
    account.priority,
    account.desired_priority,
    account.priority_interval_name,
    account.priority_sync_status,
    account.priority_sync_error,
    account.last_error,
  ].filter(Boolean).join(" ").toLowerCase();
}

function matchesChannelStatus(channel: UpstreamChannel, filter: StatusFilter) {
  if (filter === "all") return true;
  if (filter === "attention") return channelHasAttention(channel);
  if (filter === "undiscovered") return !channel.last_discovered_at;
  return (channel.accounts || []).some((account) => account.would_change === true);
}

function channelHasAttention(channel: UpstreamChannel) {
  if (channel.last_error || !channel.access_token_set) return true;
  if (resolvedChannelType(channel) === "sub2api" && !channel.refresh_token_set) return true;
  return [channel.status, channel.balance_status, channel.recharge_multiplier_status]
    .filter(Boolean)
    .some((status) => isFailureStatus(status));
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

  const message = visibleUpstreamBalanceMessage(channel.balance_message || channel.message);
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
  return /(error|fail|invalid|unavailable|unsupported|missing|not[_-]?found|disabled|expired|exhausted|unassigned|blocked|denied)/i.test(value);
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

function formatPriority(value: unknown) {
  const number = finiteNumber(value);
  return number === null ? "—" : number.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function priorityStatusLabel(status?: string | null) {
  const value = String(status || "").trim().toLowerCase();
  return ({
    applied: "已同步",
    apply_failed: "同步失败",
    failed: "同步失败",
    multiplier_unavailable: "等待综合倍率",
    pending: "等待写入",
    pending_apply: "等待写入",
    skipped: "保持不变",
    synced: "已同步",
    in_sync: "已同步",
    unchanged: "已同步",
  } as Record<string, string>)[value] || (value ? statusLabel(value) : "已计算");
}

function priorityIntervalPayload(form: PriorityIntervalForm): PriorityIntervalInput {
  const name = form.name.trim();
  if (!name) throw new Error("请填写区间名称");
  if (name.length > 100) throw new Error("区间名称不能超过 100 个字符");
  const startPriority = Number(form.startPriority);
  const endPriority = Number(form.endPriority);
  const step = Number(form.step);
  if (!Number.isSafeInteger(startPriority) || !Number.isSafeInteger(endPriority)) {
    throw new Error("优先级范围必须使用整数");
  }
  if (startPriority < 0) throw new Error("起始优先级不能小于 0");
  if (endPriority <= startPriority) throw new Error("结束优先级必须大于起始优先级");
  if (!Number.isSafeInteger(step) || step < 1) throw new Error("优先级间隔必须是大于 0 的整数");
  return {
    name,
    start_priority: startPriority,
    end_priority: endPriority,
    step,
  };
}

function priorityIntervalAccountCount(interval: PriorityInterval, accounts: UpstreamAccount[]) {
  const responseCount = finiteNumber(interval.account_count);
  if (responseCount !== null) return responseCount;
  return accounts.filter(
    (account) => String(account.priority_interval_id ?? "") === String(interval.id),
  ).length;
}

function channelAccountCount(channel: UpstreamChannel) {
  const responseCount = finiteNumber(channel.account_count);
  return responseCount === null ? (channel.accounts || []).length : Math.max(0, Math.trunc(responseCount));
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

function balanceDetail(channel: UpstreamChannel) {
  const details: string[] = [];
  if (finiteNumber(channel.balance_total) !== null) {
    details.push("总额 " + formatUpstreamBalance(channel.balance_total, channel.balance_unit, 2));
  }
  if (finiteNumber(channel.balance_used) !== null) {
    details.push("上游累计已用 " + formatUpstreamBalance(channel.balance_used, channel.balance_unit, 2));
  }
  return details.join(" · ");
}

function formatTodayBalanceUsed(channel: UpstreamChannel, timeZone: string) {
  const status = String(channel.today_balance_status || "not_checked").toLowerCase();
  if (status !== "ok" || finiteNumber(channel.today_balance_used) === null) {
    return {
      label: "今日",
      tone: statusTone(status),
      value: channel.resolved_upstream_type === "newapi" && status === "unsupported"
        ? "上游未提供"
        : isFailureStatus(status)
          ? "读取失败"
          : "待同步",
    };
  }
  const current = isToday(channel.today_balance_checked_at, timeZone);
  return {
    label: current ? "今日" : "上次",
    tone: current ? "success" : "warn",
    value: formatUpstreamBalance(
      channel.today_balance_used,
      channel.today_balance_unit || channel.balance_unit,
      2,
    ),
  };
}

function formatMoney(value: unknown) {
  const number = finiteNumber(value);
  if (number === null) return "—";
  return number.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function isToday(value: string | null | undefined, timeZone: string) {
  if (!value) return false;
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : value + "Z";
  const checkedAt = new Date(normalized);
  if (Number.isNaN(checkedAt.getTime())) return false;
  const formatter = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    month: "2-digit",
    timeZone,
    year: "numeric",
  });
  return formatter.format(checkedAt) === formatter.format(new Date());
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
