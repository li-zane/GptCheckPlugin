import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  BadgeDollarSign,
  CalendarDays,
  ChartNoAxesCombined,
  CheckCircle2,
  CircleOff,
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
  PlugZap,
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
import {
  FormEvent,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api, upstreamLegacyBindingCounts } from "./api";
import { HelpPopover } from "./HelpPopover";
import { MiddleEllipsisText } from "./MiddleEllipsisText";
import {
  latestChannelMonitorStatus,
  recentChannelMonitorTimeline,
} from "./channelMonitorPresentation";
import {
  CHANGE_LOG_READ_RETRY_DELAYS_MS,
  departedChangeLogSubview,
  pendingReadThroughId,
  visibleChangeLogUnreadCounts,
} from "./changeLogReadState";
import {
  changeLogCacheKey,
  getChangeLogSessionStorage,
  markChangeLogCacheRead,
  mergeChangeLogItems,
  readChangeLogCache,
  writeChangeLogCache,
} from "./changeLogCache";
import {
  isGenericUpstreamChannelError,
  partitionUpstreamChannels,
  upstreamChannelTokenInvalid,
} from "./upstreamChannelPresentation";
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
  upstreamChangeSummary,
  upstreamGroupRatePresentation,
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
  priorityTieMultiplierKey,
  priorityTieMoveOptions,
  sortUpstreamAccountEntries,
  sortUpstreamAccountEntriesByName,
  upstreamAccountChannels,
  upstreamAccountPlatforms,
  upstreamAccountMatchesStatus,
  type UpstreamAccountEntry,
  type PriorityTieMoveState,
} from "./upstreamPriorityPresentation";
import { upstreamOverviewHasLiveMutationData } from "./upstreamOverviewCache";
import {
  formatUpstreamBalance,
  shouldShowUpstreamAccountUsage,
  visibleUpstreamBalanceMessage,
} from "./upstreamUsagePresentation";
import { routeFromPath, type ApiKeySubview } from "./viewRouting";
import type {
  ApiKeyViewOperation,
  PriorityInterval,
  PriorityAllocationStrategy,
  PriorityIntervalInput,
  AccountSchedulingChangeEvent,
  ChangeLogUnreadCounts,
  UpstreamAccount,
  UpstreamAccountPauseHold,
  UpstreamChannel,
  UpstreamChannelMonitor,
  UpstreamChannelChangeEvent,
  UpstreamChannelsResponse,
  UpstreamChannelUpdate,
  UpstreamType,
  UpstreamUsageHistory,
  UpstreamUsageHistoryFilters,
} from "./types";

type ChannelStatusFilter = "all" | "pending" | "attention" | "undiscovered";
type AccountStatusFilter = ChannelStatusFilter | "enabled" | "disabled";
type ChannelOccupancyFilter = "occupied" | "no_enabled" | "empty";
type RateLogFilters = { startDate: string; endDate: string };
type PriorityIntervalFilter = "all" | "unassigned" | string;
type PlatformFilter = "all" | "__unknown__" | string;
type AccountUpstreamFilter = "all" | "__unassigned__" | string;

const changeLogUnreadRefreshIntervalMs = 12_000;

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
  priorityAssignmentWhenDisabled: "inherit" | "enabled" | "disabled";
  ratePausePolicy: "inherit" | "disabled" | "custom";
  rateAbsoluteThreshold: string;
  availabilityCheckMode: "channel_monitor" | "independent_model" | "disabled";
  availabilityMonitorId: string;
  availabilityTestModel: string;
};

type PriorityIntervalForm = {
  name: string;
  startPriority: string;
  endPriority: string;
  step: string;
  allocationStrategy: PriorityAllocationStrategy;
  ratePauseEnabled: boolean;
  rateAbsoluteThreshold: string;
};

type AccountCollectionDialog = {
  accounts: UpstreamAccount[];
  channel: UpstreamChannel | null;
  title: string;
};

type UsageHistoryFilters = {
  startDate: string;
  endDate: string;
  apiKeyAccountId: string;
};

type UsageHistoryDatePreset = "today" | "seven_days" | "thirty_days" | "ninety_days" | "this_month";

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
  priorityAssignmentWhenDisabled: "inherit",
  ratePausePolicy: "inherit",
  rateAbsoluteThreshold: "1",
  availabilityCheckMode: "channel_monitor",
  availabilityMonitorId: "",
  availabilityTestModel: "",
};

const emptyPriorityIntervalForm: PriorityIntervalForm = {
  name: "",
  startPriority: "",
  endPriority: "",
  step: "1",
  allocationStrategy: "cost_optimized",
  ratePauseEnabled: false,
  rateAbsoluteThreshold: "1",
};

export function ApiKeyAccountsView({
  cacheBaseUrl,
  cachedData,
  channelMonitorFallbackTestModels,
  displayTimeZone,
  globallyBusy,
  onCacheChange,
  onNotice,
  onOperationStart,
  onSubviewChange,
  rateWritesEnabled,
  refreshVersion,
  shareSameCompositePriority,
  subview,
}: {
  cacheBaseUrl: string;
  cachedData: UpstreamChannelsResponse | null;
  channelMonitorFallbackTestModels: string[];
  displayTimeZone: string;
  globallyBusy: boolean;
  onCacheChange: (data: UpstreamChannelsResponse, baseUrl: string) => void;
  onNotice: (message: string) => void;
  onOperationStart: (operation?: ApiKeyViewOperation) => () => void;
  onSubviewChange: (subview: ApiKeySubview) => void;
  rateWritesEnabled: boolean;
  refreshVersion: number;
  shareSameCompositePriority: boolean;
  subview: ApiKeySubview;
}) {
  const [data, setData] = useState<UpstreamChannelsResponse>(cachedData || emptyData);
  const [loading, setLoading] = useState(!cachedData);
  const [refreshing, setRefreshing] = useState(Boolean(cachedData));
  const [bulkDiscovering, setBulkDiscovering] = useState(false);
  const [busyChannels, setBusyChannels] = useState<Record<string, string>>({});
  const [busyAccounts, setBusyAccounts] = useState<Record<string, string>>({});
  const [channelSearch, setChannelSearch] = useState("");
  const [channelStatusFilter, setChannelStatusFilter] = useState<ChannelStatusFilter>("all");
  const [channelOccupancyFilter, setChannelOccupancyFilter] = useState<ChannelOccupancyFilter>("occupied");
  const [accountSearch, setAccountSearch] = useState("");
  const [accountStatusFilter, setAccountStatusFilter] = useState<AccountStatusFilter>("all");
  const [accountUpstreamFilter, setAccountUpstreamFilter] = useState<AccountUpstreamFilter>("all");
  const [priorityIntervalFilter, setPriorityIntervalFilter] = useState<PriorityIntervalFilter>("all");
  const [platformFilter, setPlatformFilter] = useState<PlatformFilter>("all");
  const [error, setError] = useState("");
  const [rateLogs, setRateLogs] = useState<UpstreamChannelChangeEvent[]>([]);
  const [rateLogsLoaded, setRateLogsLoaded] = useState(false);
  const [rateLogsLoading, setRateLogsLoading] = useState(false);
  const [rateLogsError, setRateLogsError] = useState("");
  const [rateLogsHasMore, setRateLogsHasMore] = useState(false);
  const [rateLogDraftFilters, setRateLogDraftFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [rateLogFilters, setRateLogFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [scheduleLogs, setScheduleLogs] = useState<AccountSchedulingChangeEvent[]>([]);
  const [scheduleLogsLoaded, setScheduleLogsLoaded] = useState(false);
  const [scheduleLogsLoading, setScheduleLogsLoading] = useState(false);
  const [scheduleLogsError, setScheduleLogsError] = useState("");
  const [scheduleLogsHasMore, setScheduleLogsHasMore] = useState(false);
  const [scheduleLogDraftFilters, setScheduleLogDraftFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [scheduleLogFilters, setScheduleLogFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [changeLogUnreadCounts, setChangeLogUnreadCounts] = useState<ChangeLogUnreadCounts>({
    upstream_changes: 0,
    account_rate_changes: 0,
    account_scheduling_changes: 0,
  });
  const visibleUnreadCounts = visibleChangeLogUnreadCounts(changeLogUnreadCounts, subview);
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
  const [channelGroupDialog, setChannelGroupDialog] = useState<UpstreamChannel | null>(null);
  const [channelMonitorDialog, setChannelMonitorDialog] = useState<UpstreamChannel | null>(null);
  const [channelUsageHistoryDialog, setChannelUsageHistoryDialog] = useState<UpstreamChannel | null>(null);
  const [channelUsageHistory, setChannelUsageHistory] = useState<UpstreamUsageHistory | null>(null);
  const [channelUsageHistoryLoading, setChannelUsageHistoryLoading] = useState(false);
  const [channelUsageHistoryError, setChannelUsageHistoryError] = useState("");
  const [channelUsageHistoryDraftFilters, setChannelUsageHistoryDraftFilters] = useState<UsageHistoryFilters>(() => (
    usageHistoryDefaultFilters(displayTimeZone)
  ));
  const [channelUsageHistoryFilters, setChannelUsageHistoryFilters] = useState<UsageHistoryFilters>(() => (
    usageHistoryDefaultFilters(displayTimeZone)
  ));
  const [channelMonitorLoading, setChannelMonitorLoading] = useState(false);
  const [channelMonitorError, setChannelMonitorError] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [savingDialog, setSavingDialog] = useState(false);
  const [accountSaveWaitingForTest, setAccountSaveWaitingForTest] = useState(false);
  const [liveDataValidated, setLiveDataValidated] = useState(false);
  const setNotice = onNotice;
  const requestSequence = useRef(0);
  const backgroundRefreshTimers = useRef<number[]>([]);
  const backgroundRefreshGeneration = useRef(0);
  const rateLogsRequestSequence = useRef(0);
  const scheduleLogsRequestSequence = useRef(0);
  const channelUsageHistoryRequestSequence = useRef(0);
  const unreadCountsRequestSequence = useRef(0);
  const rateLogsRef = useRef<UpstreamChannelChangeEvent[]>([]);
  const scheduleLogsRef = useRef<AccountSchedulingChangeEvent[]>([]);
  const rateLogCacheKeysRef = useRef<Record<"upstream" | "account_rate", string>>({
    upstream: "",
    account_rate: "",
  });
  const scheduleLogCacheKeyRef = useRef("");
  const lastRateLogBackgroundRefreshRef = useRef("");
  const lastScheduleLogBackgroundRefreshRef = useRef("");
  const warmedChangeLogCacheScopeRef = useRef("");
  const pendingRateLogReadThroughIdRef = useRef<number | null>(null);
  const pendingAccountRateLogReadThroughIdRef = useRef<number | null>(null);
  const pendingScheduleLogReadThroughIdRef = useRef<number | null>(null);
  const previousSubviewRef = useRef(subview);
  const componentMountedRef = useRef(true);
  const activeCacheBaseUrlRef = useRef(cacheBaseUrl);
  const hasDataRef = useRef(Boolean(cachedData));
  const dataRef = useRef<UpstreamChannelsResponse>(cachedData || emptyData);
  const availabilityTestPromisesRef = useRef<Map<string, Promise<UpstreamAccount | null>>>(new Map());
  const connectionTestPromisesRef = useRef<Map<string, Promise<void>>>(new Map());
  const refreshVersionRef = useRef(refreshVersion);
  const dialogRef = useRef<HTMLElement | null>(null);
  const lastFocusedElementRef = useRef<HTMLElement | null>(null);
  const commitData = useCallback((nextData: UpstreamChannelsResponse) => {
    dataRef.current = nextData;
    setData(nextData);
    setAccountCollectionDialog((current) => {
      if (!current) return current;
      const channelId = current.channel?.id;
      if (channelId == null) {
        return { ...current, accounts: nextData.unassigned_accounts };
      }
      const refreshedChannel = nextData.channels.find(
        (channel) => String(channel.id) === String(channelId),
      );
      return refreshedChannel
        ? {
            ...current,
            accounts: refreshedChannel.accounts || [],
            channel: refreshedChannel,
            title: channelDisplayName(refreshedChannel),
          }
        : current;
    });
    const refreshDialogChannel = (
      current: UpstreamChannel | null,
    ): UpstreamChannel | null => {
      if (!current) return current;
      return nextData.channels.find(
        (channel) => String(channel.id) === String(current.id),
      ) || current;
    };
    setAccountUpstreamDialog(refreshDialogChannel);
    setChannelGroupDialog(refreshDialogChannel);
    setChannelMonitorDialog(refreshDialogChannel);
    setChannelUsageHistoryDialog(refreshDialogChannel);
  }, []);
  const refreshChangeLogUnreadCounts = useCallback(async () => {
    const sequence = ++unreadCountsRequestSequence.current;
    try {
      const counts = await api.changeLogUnreadCounts();
      if (sequence === unreadCountsRequestSequence.current) setChangeLogUnreadCounts(counts);
    } catch {
      // Keep the last known badges; a later foreground or interval refresh can recover.
    }
  }, []);
  const rateLogCacheKey = useCallback((category: "upstream" | "account_rate") => (
    changeLogCacheKey(
      cacheBaseUrl,
      category,
      rateLogFilters.startDate,
      rateLogFilters.endDate,
      displayTimeZone,
    )
  ), [cacheBaseUrl, displayTimeZone, rateLogFilters.endDate, rateLogFilters.startDate]);
  const schedulingLogCacheKey = useCallback(() => (
    changeLogCacheKey(
      cacheBaseUrl,
      "scheduling",
      scheduleLogFilters.startDate,
      scheduleLogFilters.endDate,
      displayTimeZone,
    )
  ), [cacheBaseUrl, displayTimeZone, scheduleLogFilters.endDate, scheduleLogFilters.startDate]);
  const localMutationBusy = savingDialog
    || priorityIntervalsBusy
    || bulkDiscovering
    || Object.keys(busyChannels).length > 0
    || Object.keys(busyAccounts).length > 0;

  const loadData = useCallback(async (
    preserveFeedback = false,
    preserveLiveValidation = false,
  ) => {
    const sequence = ++requestSequence.current;
    const requestBaseUrl = cacheBaseUrl;
    if (!preserveLiveValidation) setLiveDataValidated(false);
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
      ) return null;
      const normalized = {
        ...response,
        channels: Array.isArray(response.channels) ? response.channels : [],
        priority_intervals: Array.isArray(response.priority_intervals) ? response.priority_intervals : [],
        unassigned_accounts: Array.isArray(response.unassigned_accounts) ? response.unassigned_accounts : [],
      };
      hasDataRef.current = true;
      commitData(normalized);
      onCacheChange(normalized, cacheBaseUrl);
      setLiveDataValidated(true);
      return normalized;
    } catch (reason) {
       if (
         sequence === requestSequence.current
         && activeCacheBaseUrlRef.current === requestBaseUrl
       ) {
        setError(errorMessage(reason, "上游渠道读取失败"));
      }
      return null;
    } finally {
      if (
        sequence === requestSequence.current
        && activeCacheBaseUrlRef.current === requestBaseUrl
      ) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [cacheBaseUrl, commitData, onCacheChange]);

  const scheduleBackgroundAccountRefresh = useCallback(() => {
    const generation = ++backgroundRefreshGeneration.current;
    for (const timer of backgroundRefreshTimers.current) window.clearTimeout(timer);
    const poll = async (consecutiveFailures: number) => {
      if (generation !== backgroundRefreshGeneration.current) return;
      const response = await loadData(true, true);
      if (generation !== backgroundRefreshGeneration.current) return;
      if (response === null) {
        if (consecutiveFailures >= 9) {
          backgroundRefreshTimers.current = [];
          return;
        }
        const retryDelay = Math.min(30_000, 2_000 * (2 ** consecutiveFailures));
        backgroundRefreshTimers.current = [
          window.setTimeout(() => void poll(consecutiveFailures + 1), retryDelay),
        ];
        return;
      }
      const stillPending = response.channels.some(
        (channel) => channel.background_discovery_pending === true,
      );
      if (stillPending) {
        backgroundRefreshTimers.current = [
          window.setTimeout(() => void poll(0), 2_000),
        ];
      } else {
        backgroundRefreshTimers.current = [];
      }
    };
    backgroundRefreshTimers.current = [
      window.setTimeout(() => void poll(0), 1_000),
    ];
  }, [loadData]);

  useEffect(() => () => {
    backgroundRefreshGeneration.current += 1;
    for (const timer of backgroundRefreshTimers.current) window.clearTimeout(timer);
    backgroundRefreshTimers.current = [];
  }, [loadData]);

  useEffect(() => {
    if (activeCacheBaseUrlRef.current === cacheBaseUrl) return;
    requestSequence.current += 1;
    activeCacheBaseUrlRef.current = cacheBaseUrl;
    hasDataRef.current = Boolean(cachedData);
    commitData(cachedData || emptyData);
    setLoading(!cachedData);
    setRefreshing(Boolean(cachedData));
    setLiveDataValidated(false);
    setError("");
    setNotice("");
    rateLogsRequestSequence.current += 1;
    setRateLogsLoading(false);
    setRateLogsError("");
    scheduleLogsRequestSequence.current += 1;
    setScheduleLogsLoading(false);
    setScheduleLogsError("");
  }, [cacheBaseUrl, cachedData, commitData]);

  useEffect(() => {
    if (!cachedData || hasDataRef.current) return;
    hasDataRef.current = true;
    commitData(cachedData);
    setLoading(false);
  }, [cachedData, commitData]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (refreshVersionRef.current === refreshVersion) return;
    refreshVersionRef.current = refreshVersion;
    requestSequence.current += 1;
    setError("");
    if (cachedData) {
      const hasLiveMutationData = upstreamOverviewHasLiveMutationData(cachedData);
      hasDataRef.current = true;
      commitData(cachedData);
      setLoading(false);
      setRefreshing(false);
      setLiveDataValidated(hasLiveMutationData);
      if (!hasLiveMutationData) void loadData(true);
    } else {
      void loadData();
    }
  }, [cachedData, commitData, loadData, refreshVersion]);

  const loadRateLogs = useCallback(async (append = false) => {
    const logSubview = subview === "account-rate-log" ? "account-rate-log" : "rate-log";
    const category = logSubview === "account-rate-log" ? "account_rate" : "upstream";
    const cacheKey = rateLogCacheKey(category);
    rateLogCacheKeysRef.current[category] = cacheKey;
    const sequence = ++rateLogsRequestSequence.current;
    setRateLogsLoading(true);
    setRateLogsError("");
    try {
      const beforeId = append && rateLogsRef.current.length
        ? rateLogsRef.current[rateLogsRef.current.length - 1].id
        : null;
      const page = await api.upstreamChannelChangeEvents(50, beforeId, {
        startDate: rateLogFilters.startDate || undefined,
        endDate: rateLogFilters.endDate || undefined,
        timeZone: displayTimeZone,
      }, category);
      if (
        sequence !== rateLogsRequestSequence.current
        || !componentMountedRef.current
        || previousSubviewRef.current !== logSubview
        || (logSubview === "rate-log" && previousSubviewRef.current !== "rate-log")
      ) return;
      const next = page.items;
      const merged = mergeChangeLogItems(rateLogsRef.current, next);
      const hasMore = next.length === 50;
      rateLogsRef.current = merged;
      setRateLogs(merged);
      setRateLogsHasMore(hasMore);
      writeChangeLogCache(getChangeLogSessionStorage(), cacheKey, {
        items: merged,
        hasMore,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
      });
      const pendingRef = category === "account_rate"
        ? pendingAccountRateLogReadThroughIdRef
        : pendingRateLogReadThroughIdRef;
      pendingRef.current = pendingReadThroughId(
        pendingRef.current,
        next,
      );
      if (!append) {
        setChangeLogUnreadCounts((current) => ({
          ...current,
          [category === "account_rate" ? "account_rate_changes" : "upstream_changes"]: page.unread_count,
        }));
      }
      setRateLogsLoaded(true);
    } catch (reason) {
      if (sequence === rateLogsRequestSequence.current) {
        setRateLogsError(errorMessage(
          reason,
          category === "account_rate" ? "API Key 倍率变化记录读取失败" : "上游分组变化记录读取失败",
        ));
        setRateLogsLoaded(true);
      }
    } finally {
      if (sequence === rateLogsRequestSequence.current) setRateLogsLoading(false);
    }
  }, [displayTimeZone, rateLogCacheKey, rateLogFilters, subview]);

  const loadScheduleLogs = useCallback(async (append = false) => {
    const cacheKey = schedulingLogCacheKey();
    scheduleLogCacheKeyRef.current = cacheKey;
    const sequence = ++scheduleLogsRequestSequence.current;
    setScheduleLogsLoading(true);
    setScheduleLogsError("");
    try {
      const beforeId = append && scheduleLogsRef.current.length
        ? scheduleLogsRef.current[scheduleLogsRef.current.length - 1].id
        : null;
      const page = await api.accountSchedulingChangeEvents(50, beforeId, {
        startDate: scheduleLogFilters.startDate || undefined,
        endDate: scheduleLogFilters.endDate || undefined,
        timeZone: displayTimeZone,
      });
      if (
        sequence !== scheduleLogsRequestSequence.current
        || !componentMountedRef.current
        || previousSubviewRef.current !== "schedule-log"
      ) return;
      const next = page.items;
      const merged = mergeChangeLogItems(scheduleLogsRef.current, next);
      const hasMore = next.length === 50;
      scheduleLogsRef.current = merged;
      setScheduleLogs(merged);
      setScheduleLogsHasMore(hasMore);
      writeChangeLogCache(getChangeLogSessionStorage(), cacheKey, {
        items: merged,
        hasMore,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
      });
      pendingScheduleLogReadThroughIdRef.current = pendingReadThroughId(
        pendingScheduleLogReadThroughIdRef.current,
        next,
      );
      if (!append) {
        setChangeLogUnreadCounts((current) => ({
          ...current,
          account_scheduling_changes: page.unread_count,
        }));
      }
      setScheduleLogsLoaded(true);
    } catch (reason) {
      if (sequence === scheduleLogsRequestSequence.current) {
        setScheduleLogsError(errorMessage(reason, "账号调度变化读取失败"));
        setScheduleLogsLoaded(true);
      }
    } finally {
      if (sequence === scheduleLogsRequestSequence.current) setScheduleLogsLoading(false);
    }
  }, [displayTimeZone, scheduleLogFilters, schedulingLogCacheKey]);

  const warmDefaultChangeLogCaches = useCallback(async () => {
    const storage = getChangeLogSessionStorage();
    const filters = { timeZone: displayTimeZone };
    const upstreamCacheKey = changeLogCacheKey(cacheBaseUrl, "upstream", "", "", displayTimeZone);
    const accountRateCacheKey = changeLogCacheKey(cacheBaseUrl, "account_rate", "", "", displayTimeZone);
    const schedulingCacheKey = changeLogCacheKey(cacheBaseUrl, "scheduling", "", "", displayTimeZone);
    const warmRateCache = async (category: "upstream" | "account_rate", cacheKey: string) => {
      const page = await api.upstreamChannelChangeEvents(50, null, filters, category);
      const cached = readChangeLogCache<UpstreamChannelChangeEvent>(storage, cacheKey);
      writeChangeLogCache(storage, cacheKey, {
        items: mergeChangeLogItems(cached?.items || [], page.items),
        hasMore: page.items.length === 50,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
      });
    };
    const warmSchedulingCache = async () => {
      const page = await api.accountSchedulingChangeEvents(50, null, filters);
      const cached = readChangeLogCache<AccountSchedulingChangeEvent>(storage, schedulingCacheKey);
      writeChangeLogCache(storage, schedulingCacheKey, {
        items: mergeChangeLogItems(cached?.items || [], page.items),
        hasMore: page.items.length === 50,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
      });
    };
    const tasks: Array<Promise<void>> = [];
    if (subview !== "rate-log") tasks.push(warmRateCache("upstream", upstreamCacheKey));
    if (subview !== "account-rate-log") tasks.push(warmRateCache("account_rate", accountRateCacheKey));
    if (subview !== "schedule-log") tasks.push(warmSchedulingCache());
    await Promise.allSettled(tasks);
  }, [cacheBaseUrl, displayTimeZone, subview]);

  const markRateLogsReadOnLeave = useCallback(async (
    category: "upstream" | "account_rate",
    updateLocalState = true,
  ) => {
    const logSubview = category === "account_rate" ? "account-rate-log" : "rate-log";
    const pendingRef = category === "account_rate"
      ? pendingAccountRateLogReadThroughIdRef
      : pendingRateLogReadThroughIdRef;
    for (let retryAttempt = 0; ; retryAttempt += 1) {
      if (
        retryAttempt > 0
        && componentMountedRef.current
        && previousSubviewRef.current === logSubview
      ) return;
      const throughId = pendingRef.current;
      if (throughId === null) return;
      const cacheKey = rateLogCacheKeysRef.current[category];
      pendingRef.current = null;
      try {
        await api.markUpstreamChannelChangesRead(throughId, category);
        if (cacheKey) {
          markChangeLogCacheRead(getChangeLogSessionStorage(), cacheKey, throughId);
        }
        await refreshChangeLogUnreadCounts();
        if (
          !updateLocalState
          || !componentMountedRef.current
          || rateLogCacheKeysRef.current[category] !== cacheKey
          || (
            previousSubviewRef.current === "account-rate-log"
              ? category !== "account_rate"
              : previousSubviewRef.current === "rate-log"
                ? category !== "upstream"
                : true
          )
        ) return;
        const updated = rateLogsRef.current.map((item) => (
          item.unread && item.id <= throughId ? { ...item, unread: false } : item
        ));
        rateLogsRef.current = updated;
        setRateLogs(updated);
        return;
      } catch {
        pendingRef.current = Math.max(
          pendingRef.current ?? 0,
          throughId,
        );
        const retryDelay = CHANGE_LOG_READ_RETRY_DELAYS_MS[retryAttempt];
        if (retryDelay === undefined) return;
        await new Promise<void>((resolve) => window.setTimeout(resolve, retryDelay));
      }
    }
  }, [refreshChangeLogUnreadCounts]);

  const markScheduleLogsReadOnLeave = useCallback(async (updateLocalState = true) => {
    for (let retryAttempt = 0; ; retryAttempt += 1) {
      if (
        retryAttempt > 0
        && componentMountedRef.current
        && previousSubviewRef.current === "schedule-log"
      ) return;
      const throughId = pendingScheduleLogReadThroughIdRef.current;
      if (throughId === null) return;
      const cacheKey = scheduleLogCacheKeyRef.current;
      pendingScheduleLogReadThroughIdRef.current = null;
      try {
        await api.markAccountSchedulingChangesRead(throughId);
        if (cacheKey) {
          markChangeLogCacheRead(getChangeLogSessionStorage(), cacheKey, throughId);
        }
        await refreshChangeLogUnreadCounts();
        if (
          !updateLocalState
          || !componentMountedRef.current
          || scheduleLogCacheKeyRef.current !== cacheKey
          || previousSubviewRef.current !== "schedule-log"
        ) return;
        const updated = scheduleLogsRef.current.map((item) => (
          item.unread && item.id <= throughId ? { ...item, unread: false } : item
        ));
        scheduleLogsRef.current = updated;
        setScheduleLogs(updated);
        return;
      } catch {
        pendingScheduleLogReadThroughIdRef.current = Math.max(
          pendingScheduleLogReadThroughIdRef.current ?? 0,
          throughId,
        );
        const retryDelay = CHANGE_LOG_READ_RETRY_DELAYS_MS[retryAttempt];
        if (retryDelay === undefined) return;
        await new Promise<void>((resolve) => window.setTimeout(resolve, retryDelay));
      }
    }
  }, [refreshChangeLogUnreadCounts]);

  useLayoutEffect(() => {
    const previousSubview = previousSubviewRef.current;
    previousSubviewRef.current = subview;
    const departedSubview = departedChangeLogSubview(previousSubview, subview);
    if (departedSubview === "rate-log") {
      rateLogsRequestSequence.current += 1;
      setRateLogsLoading(false);
      void markRateLogsReadOnLeave("upstream");
    }
    if (departedSubview === "account-rate-log") {
      rateLogsRequestSequence.current += 1;
      setRateLogsLoading(false);
      void markRateLogsReadOnLeave("account_rate");
    }
    if (departedSubview === "schedule-log") {
      scheduleLogsRequestSequence.current += 1;
      setScheduleLogsLoading(false);
      void markScheduleLogsReadOnLeave();
    }
  }, [markRateLogsReadOnLeave, markScheduleLogsReadOnLeave, subview]);

  useLayoutEffect(() => {
    if (subview !== "rate-log" && subview !== "account-rate-log") return;
    const category = subview === "account-rate-log" ? "account_rate" : "upstream";
    const cacheKey = rateLogCacheKey(category);
    rateLogCacheKeysRef.current[category] = cacheKey;
    rateLogsRequestSequence.current += 1;
    const cached = readChangeLogCache<UpstreamChannelChangeEvent>(
      getChangeLogSessionStorage(),
      cacheKey,
    );
    const items = cached?.items || [];
    rateLogsRef.current = items;
    setRateLogs(items);
    setRateLogsHasMore(cached?.hasMore || false);
    setRateLogsLoaded(Boolean(cached));
    setRateLogsLoading(false);
    setRateLogsError("");
    const pendingRef = category === "account_rate"
      ? pendingAccountRateLogReadThroughIdRef
      : pendingRateLogReadThroughIdRef;
    pendingRef.current = pendingReadThroughId(pendingRef.current, items);
    if (cached) {
      setChangeLogUnreadCounts((current) => ({
        ...current,
        [category === "account_rate" ? "account_rate_changes" : "upstream_changes"]: cached.unreadCount,
      }));
    }
  }, [rateLogCacheKey, subview]);

  useLayoutEffect(() => {
    if (subview !== "schedule-log") return;
    const cacheKey = schedulingLogCacheKey();
    scheduleLogCacheKeyRef.current = cacheKey;
    scheduleLogsRequestSequence.current += 1;
    const cached = readChangeLogCache<AccountSchedulingChangeEvent>(
      getChangeLogSessionStorage(),
      cacheKey,
    );
    const items = cached?.items || [];
    scheduleLogsRef.current = items;
    setScheduleLogs(items);
    setScheduleLogsHasMore(cached?.hasMore || false);
    setScheduleLogsLoaded(Boolean(cached));
    setScheduleLogsLoading(false);
    setScheduleLogsError("");
    pendingScheduleLogReadThroughIdRef.current = pendingReadThroughId(
      pendingScheduleLogReadThroughIdRef.current,
      items,
    );
    if (cached) {
      setChangeLogUnreadCounts((current) => ({
        ...current,
        account_scheduling_changes: cached.unreadCount,
      }));
    }
  }, [schedulingLogCacheKey, subview]);

  useLayoutEffect(() => {
    componentMountedRef.current = true;
    return () => {
      componentMountedRef.current = false;
      const activeSubview = previousSubviewRef.current;
      const nextRoute = routeFromPath(window.location.pathname);
      const stillViewingSameLog = nextRoute.view === "api-keys"
        && nextRoute.apiKeySubview === activeSubview;
      if (stillViewingSameLog) return;
      rateLogsRequestSequence.current += 1;
      scheduleLogsRequestSequence.current += 1;
      if (activeSubview === "rate-log") void markRateLogsReadOnLeave("upstream", false);
      if (activeSubview === "account-rate-log") void markRateLogsReadOnLeave("account_rate", false);
      if (activeSubview === "schedule-log") void markScheduleLogsReadOnLeave(false);
    };
  }, [markRateLogsReadOnLeave, markScheduleLogsReadOnLeave]);

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

  const applyScheduleLogFilters = useCallback(() => {
    if (
      scheduleLogDraftFilters.startDate
      && scheduleLogDraftFilters.endDate
      && scheduleLogDraftFilters.startDate > scheduleLogDraftFilters.endDate
    ) {
      setScheduleLogsError("开始日期不能晚于结束日期");
      return;
    }
    setScheduleLogsError("");
    scheduleLogsRequestSequence.current += 1;
    setScheduleLogs([]);
    setScheduleLogsLoading(false);
    setScheduleLogsHasMore(false);
    setScheduleLogFilters(scheduleLogDraftFilters);
    setScheduleLogsLoaded(false);
  }, [scheduleLogDraftFilters]);

  const clearScheduleLogFilters = useCallback(() => {
    const emptyFilters = { startDate: "", endDate: "" };
    setScheduleLogDraftFilters(emptyFilters);
    setScheduleLogsError("");
    scheduleLogsRequestSequence.current += 1;
    setScheduleLogs([]);
    setScheduleLogsLoading(false);
    setScheduleLogsHasMore(false);
    setScheduleLogFilters(emptyFilters);
    setScheduleLogsLoaded(false);
  }, []);

  useEffect(() => {
    void refreshChangeLogUnreadCounts();
  }, [refreshChangeLogUnreadCounts, refreshVersion]);

  useEffect(() => {
    const scope = `${cacheBaseUrl}|${displayTimeZone}`;
    if (warmedChangeLogCacheScopeRef.current === scope) return;
    warmedChangeLogCacheScopeRef.current = scope;
    void warmDefaultChangeLogCaches();
  }, [cacheBaseUrl, displayTimeZone, warmDefaultChangeLogCaches]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshChangeLogUnreadCounts();
    };
    const timer = window.setInterval(refreshWhenVisible, changeLogUnreadRefreshIntervalMs);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [refreshChangeLogUnreadCounts]);

  useEffect(() => {
    if (subview !== "rate-log" && subview !== "account-rate-log") {
      lastRateLogBackgroundRefreshRef.current = "";
      return;
    }
    const category = subview === "account-rate-log" ? "account_rate" : "upstream";
    const refreshKey = `${rateLogCacheKey(category)}:${refreshVersion}`;
    if (lastRateLogBackgroundRefreshRef.current === refreshKey) return;
    lastRateLogBackgroundRefreshRef.current = refreshKey;
    void loadRateLogs();
  }, [loadRateLogs, rateLogCacheKey, refreshVersion, subview]);

  useEffect(() => {
    if (subview !== "schedule-log") {
      lastScheduleLogBackgroundRefreshRef.current = "";
      return;
    }
    const refreshKey = `${schedulingLogCacheKey()}:${refreshVersion}`;
    if (lastScheduleLogBackgroundRefreshRef.current === refreshKey) return;
    lastScheduleLogBackgroundRefreshRef.current = refreshKey;
    void loadScheduleLogs();
  }, [loadScheduleLogs, refreshVersion, schedulingLogCacheKey, subview]);

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
    setChannelGroupDialog(null);
    setChannelMonitorDialog(null);
    channelUsageHistoryRequestSequence.current += 1;
    setChannelUsageHistoryDialog(null);
    setChannelUsageHistory(null);
    setChannelUsageHistoryLoading(false);
    setChannelUsageHistoryError("");
    setChannelMonitorLoading(false);
    setChannelMonitorError("");
    setChannelForm(emptyChannelForm);
    setAccountForm(emptyAccountForm);
    setDialogError("");
    setSavingDialog(false);
    setAccountSaveWaitingForTest(false);
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
      && !channelGroupDialog
      && !channelMonitorDialog
      && !channelUsageHistoryDialog
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
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
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
    channelGroupDialog,
    channelMonitorDialog,
    channelUsageHistoryDialog,
    closeDialog,
    editingAccount,
    editingChannel,
    priorityIntervalDialogOpen,
    savingDialog,
  ]);

  const allAccountEntries = useMemo(() => flattenUpstreamAccounts(data), [data]);
  const allAccounts = useMemo(() => allAccountEntries.map(({ account }) => account), [allAccountEntries]);
  const priorityTieMoves = useMemo(
    () => shareSameCompositePriority ? new Map() : priorityTieMoveOptions(allAccounts),
    [allAccounts, shareSameCompositePriority],
  );
  const channelPartitions = useMemo(() => partitionUpstreamChannels(data.channels), [data.channels]);
  const assignedChannels = channelPartitions.assigned;
  const occupiedChannels = channelPartitions.enabled;
  const noEnabledChannels = channelPartitions.noEnabled;
  const emptyChannels = channelPartitions.empty;
  const occupancyChannels = channelOccupancyFilter === "occupied"
    ? occupiedChannels
    : channelOccupancyFilter === "no_enabled"
      ? noEnabledChannels
      : emptyChannels;
  const priorityIntervals = data.priority_intervals || [];
  const upstreamOptions = useMemo(() => upstreamAccountChannels(allAccountEntries), [allAccountEntries]);
  const platformOptions = useMemo(() => upstreamAccountPlatforms(allAccountEntries), [allAccountEntries]);
  const filteredAccountEntries = useMemo(() => {
    const filtered = filterUpstreamAccountEntries(allAccountEntries, {
      channel: accountUpstreamFilter,
      interval: priorityIntervalFilter,
      platform: platformFilter,
      query: accountSearch,
    }).filter(({ account }) => upstreamAccountMatchesStatus(account, accountStatusFilter));
    return sortUpstreamAccountEntries(filtered);
  }, [accountSearch, accountStatusFilter, accountUpstreamFilter, allAccountEntries, platformFilter, priorityIntervalFilter]);
  const viewPriorityIntervalAccounts = useCallback((interval: PriorityInterval) => {
    setAccountSearch("");
    setAccountStatusFilter("all");
    setAccountUpstreamFilter("all");
    setPlatformFilter("all");
    setPriorityIntervalFilter(String(interval.id));
    onSubviewChange("accounts");
  }, [onSubviewChange]);

  const summary = useMemo(
    () => ({
      channels: assignedChannels.length,
      accounts: allAccounts.length,
      pending: allAccounts.filter((account) => account.would_change === true).length,
      readableBalances: assignedChannels.filter(hasCurrentPlatformBalance).length,
    }),
    [allAccounts, assignedChannels],
  );

  const filteredChannels = useMemo(() => {
    const query = channelSearch.trim().toLowerCase();
    return occupancyChannels
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
  }, [channelSearch, channelStatusFilter, occupancyChannels]);

  const filteredUnassigned = useMemo(() => {
    const query = channelSearch.trim().toLowerCase();
    if (channelOccupancyFilter !== "occupied") return [];
    if (channelStatusFilter !== "all" && channelStatusFilter !== "attention") return [];
    return data.unassigned_accounts.filter((account) => !query || accountSearchText(account).includes(query));
  }, [channelOccupancyFilter, channelSearch, channelStatusFilter, data.unassigned_accounts]);

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
    setChannelMonitorDialog(null);
    setEditingChannel(channel);
    setDialogError("");
    setChannelForm({
      displayName: channel.display_name || "",
      baseUrl: channelBaseUrl(channel),
      managementBaseUrl: channel.management_base_url || "",
      upstreamType: channel.resolved_upstream_type || channel.upstream_type || "auto",
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
    setChannelMonitorDialog(null);
    setEditingAccount(account);
    setDialogError("");
    setAccountForm({
      channelId: String(account.channel_id ?? fallbackChannel?.id ?? ""),
      apiKey: "",
      manualGroupMultiplier: numberInputValue(account.manual_group_multiplier),
      remoteName: account.remote_name || "",
      priorityAssignmentWhenDisabled: account.priority_assignment_when_disabled === true
        ? "enabled"
        : account.priority_assignment_when_disabled === false
          ? "disabled"
          : "inherit",
      ratePausePolicy: account.rate_pause_policy || "inherit",
      rateAbsoluteThreshold: numberInputValue(account.rate_absolute_threshold) || "1",
      availabilityCheckMode: account.availability_check_mode || "channel_monitor",
      availabilityMonitorId: account.availability_monitor_id == null
        ? ""
        : String(account.availability_monitor_id),
      availabilityTestModel: account.availability_test_model || "",
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

  const openChannelMonitors = (channel: UpstreamChannel) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setChannelMonitorError("");
    setChannelMonitorDialog(channel);
  };

  const refreshChannelMonitors = async (channel: UpstreamChannel) => {
    const finishOperation = onOperationStart();
    setChannelBusy(channel, "monitors");
    setChannelMonitorLoading(true);
    setChannelMonitorError("");
    try {
      const response = await api.refreshUpstreamChannelMonitors(channel.id);
      const currentData = dataRef.current;
      const nextData = {
        ...currentData,
        channels: currentData.channels.map((item) => String(item.id) === String(response.channel_id)
          ? {
              ...item,
              channel_monitors: response.channel_monitors,
              channel_monitor_count: response.channel_monitor_count,
              channel_monitor_status: response.channel_monitor_status,
              channel_monitor_message: response.channel_monitor_message,
              channel_monitor_checked_at: response.channel_monitor_checked_at,
            }
          : item),
      };
      commitData(nextData);
      onCacheChange(nextData, cacheBaseUrl);
      setChannelMonitorDialog((current) => current && String(current.id) === String(response.channel_id)
        ? {
            ...current,
            channel_monitors: response.channel_monitors,
            channel_monitor_count: response.channel_monitor_count,
            channel_monitor_status: response.channel_monitor_status,
            channel_monitor_message: response.channel_monitor_message,
            channel_monitor_checked_at: response.channel_monitor_checked_at,
          }
        : current);
      setNotice("渠道状态已更新。");
    } catch (reason) {
      setChannelMonitorError(errorMessage(reason, "渠道状态读取失败"));
    } finally {
      setChannelMonitorLoading(false);
      setChannelBusy(channel, null);
      finishOperation();
    }
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

  const openChannelGroups = (channel: UpstreamChannel) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setChannelMonitorDialog(null);
    setChannelGroupDialog(channel);
  };

  const loadChannelUsageHistory = useCallback(async (
    channel: UpstreamChannel,
    filters: UsageHistoryFilters,
  ) => {
    const requestSequence = ++channelUsageHistoryRequestSequence.current;
    if (usageHistoryDateRangeInvalid(filters)) {
      setChannelUsageHistoryLoading(false);
      setChannelUsageHistoryError("开始日期不能晚于结束日期");
      return;
    }
    setChannelUsageHistoryLoading(true);
    setChannelUsageHistoryError("");
    try {
      const response = await api.upstreamUsageHistory(channel.id, usageHistoryApiFilters(filters, displayTimeZone));
      if (requestSequence !== channelUsageHistoryRequestSequence.current) return;
      setChannelUsageHistory(normalizeUsageHistory(response));
    } catch (reason) {
      if (requestSequence !== channelUsageHistoryRequestSequence.current) return;
      setChannelUsageHistoryError(errorMessage(reason, "上游历史用量读取失败"));
    } finally {
      if (requestSequence === channelUsageHistoryRequestSequence.current) {
        setChannelUsageHistoryLoading(false);
      }
    }
  }, [displayTimeZone]);

  const openChannelUsageHistory = (channel: UpstreamChannel) => {
    rememberDialogTrigger();
    channelUsageHistoryRequestSequence.current += 1;
    const filters = usageHistoryDefaultFilters(displayTimeZone);
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setChannelGroupDialog(null);
    setChannelMonitorDialog(null);
    setChannelUsageHistory(null);
    setChannelUsageHistoryError("");
    setChannelUsageHistoryDraftFilters(filters);
    setChannelUsageHistoryFilters(filters);
    setChannelUsageHistoryDialog(channel);
  };

  const applyChannelUsageHistoryFilters = () => {
    if (usageHistoryDateRangeInvalid(channelUsageHistoryDraftFilters)) {
      setChannelUsageHistoryError("开始日期不能晚于结束日期");
      return;
    }
    setChannelUsageHistoryError("");
    setChannelUsageHistoryFilters({ ...channelUsageHistoryDraftFilters });
  };

  const applyChannelUsageHistoryPreset = (preset: UsageHistoryDatePreset) => {
    const filters = {
      ...usageHistoryFiltersForPreset(preset, displayTimeZone),
      apiKeyAccountId: channelUsageHistoryDraftFilters.apiKeyAccountId,
    };
    setChannelUsageHistoryError("");
    setChannelUsageHistoryDraftFilters(filters);
    setChannelUsageHistoryFilters(filters);
  };

  useEffect(() => {
    if (!channelUsageHistoryDialog) return;
    void loadChannelUsageHistory(channelUsageHistoryDialog, channelUsageHistoryFilters);
  }, [channelUsageHistoryDialog, channelUsageHistoryFilters, loadChannelUsageHistory]);

  const openPriorityIntervalConfig = (interval?: PriorityInterval) => {
    rememberDialogTrigger();
    setEditingPriorityInterval(interval || null);
    setPriorityIntervalForm(interval ? {
      name: interval.name,
      startPriority: String(interval.start_priority),
      endPriority: String(interval.end_priority),
      step: String(interval.step),
      allocationStrategy: interval.allocation_strategy || "cost_optimized",
      ratePauseEnabled: interval.rate_pause_enabled === true,
      rateAbsoluteThreshold: String(interval.rate_absolute_threshold ?? 1),
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

  const moveAccountPriority = async (
    account: UpstreamAccount,
    direction: "up" | "down",
  ) => {
    const finishOperation = onOperationStart();
    setAccountBusy(account, "priority-order");
    setError("");
    setNotice("");
    try {
      await api.moveUpstreamAccountPriority(account.sub2api_account_id, {
        direction,
        expected_identity_fingerprint: expectedIdentityFingerprint(account),
      });
      await loadData(true);
      setNotice(
        `${accountDisplayName(account)} 已与${direction === "up" ? "后一个" : "前一个"}同倍率账号互换调度优先级。`,
      );
    } catch (reason) {
      setError(errorMessage(reason, "同倍率账号优先级调整失败"));
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
    const submittedAccount = editingAccount;
    const finishOperation = onOperationStart();
    setSavingDialog(true);
    setDialogError("");
    try {
      const pendingAvailabilityTest = availabilityTestPromisesRef.current.get(accountKey(submittedAccount));
      const pendingConnectionTest = connectionTestPromisesRef.current.get(accountKey(submittedAccount));
      let testedAccount: UpstreamAccount | null = null;
      if (pendingAvailabilityTest) {
        setAccountSaveWaitingForTest(true);
        testedAccount = await pendingAvailabilityTest;
        setAccountSaveWaitingForTest(false);
      }
      if (pendingConnectionTest) {
        setAccountSaveWaitingForTest(true);
        await pendingConnectionTest;
        setAccountSaveWaitingForTest(false);
      }
      const latestData = dataRef.current;
      const latestAccount = flattenUpstreamAccounts(latestData).find(
        ({ account }) => accountKey(account) === accountKey(submittedAccount),
      )?.account || testedAccount || submittedAccount;
      const selectedChannel = latestData.channels.find((channel) => String(channel.id) === accountForm.channelId);
      const payload = buildUpstreamAccountUpdatePayload({
        account: latestAccount,
        apiKey: accountForm.apiKey,
        channelId: selectedChannel?.id ?? null,
        manualGroupMultiplier: accountForm.manualGroupMultiplier,
        remoteName: accountForm.remoteName,
      });
      payload.priority_assignment_when_disabled = accountForm.priorityAssignmentWhenDisabled === "inherit"
        ? null
        : accountForm.priorityAssignmentWhenDisabled === "enabled";
      payload.rate_pause_policy = accountForm.ratePausePolicy;
      if (accountForm.ratePausePolicy === "custom") {
        payload.rate_absolute_threshold = ratePauseThresholdPayload(accountForm.rateAbsoluteThreshold);
      } else {
        payload.rate_absolute_threshold = null;
      }
      const submittedAvailabilityMode = accountForm.availabilityCheckMode;
      payload.availability_check_mode = submittedAvailabilityMode;
      payload.availability_monitor_id = submittedAvailabilityMode === "channel_monitor"
        && accountForm.availabilityMonitorId
        ? Number(accountForm.availabilityMonitorId)
        : null;
      payload.availability_test_model = submittedAvailabilityMode === "disabled"
        ? null
        : accountForm.availabilityTestModel.trim() || null;
      const previousOrigin = urlOrigin(latestAccount.base_url);
      const nextOrigin = urlOrigin(selectedChannel ? channelBaseUrl(selectedChannel) : null);
      const credentialRebind = Boolean(
        previousOrigin && nextOrigin && previousOrigin !== nextOrigin,
      );
      if (
        credentialRebind &&
        !window.confirm(
          "账号将切换到不同的上游域名。继续会先更新 Sub2API 账号的上游地址，再把本地 API Key 配置绑定到新渠道，是否确认？",
        )
      ) {
        return;
      }
      payload.confirm_credential_rebind = credentialRebind;
      if (latestAccount.identity_rebind_required) {
        const confirmed = window.confirm(
          latestAccount.identity_binding_status === "mismatch"
            ? "检测到 sub2api 账号 ID 对应的身份已经变化。继续会把保留的本地上游配置和凭据重新绑定到当前账号，是否确认？"
            : "这是升级前尚未绑定身份的本地配置。继续会把它认领到当前 sub2api 账号，是否确认？",
        );
        if (!confirmed) return;
        payload.confirm_identity_rebind = true;
      }
      if (latestAccount.upstream_identity_rebind_required) {
        const confirmed = window.confirm(
          "检测到绑定的上游 API Key 记录 ID 已变化或存在冲突。继续会归档当前数据并重新确认上游记录，是否继续？",
        );
        if (!confirmed) return;
        payload.confirm_upstream_identity_rebind = true;
      }
      await api.updateUpstreamAccount(latestAccount.sub2api_account_id, payload);
      closeDialog();
      await loadData(true);
      scheduleBackgroundAccountRefresh();
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogsHasMore(false);
      setRateLogsLoaded(false);
      setError("");
      setNotice(
        rateWritesEnabled
          ? "账号配置已保存；可用性与上游数据正在后台探测。"
          : "账号配置已保存；可用性正在后台探测，倍率自动同步保持关闭。",
      );
    } catch (reason) {
      setDialogError(errorMessage(reason, "账号配置保存失败"));
    } finally {
      setAccountSaveWaitingForTest(false);
      setSavingDialog(false);
      finishOperation();
    }
  };

  const discoverChannel = async (channel: UpstreamChannel) => {
    const finishOperation = onOperationStart({
      kind: "channel-discovery",
      channelId: Number(channel.id),
    });
    setChannelBusy(channel, "discover");
    setError("");
    setNotice("");
    try {
      await api.discoverUpstreamChannel(channel.id);
      await loadData(true);
      await refreshChangeLogUnreadCounts();
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogsHasMore(false);
      setRateLogsLoaded(false);
      setNotice(channelDiscoverySuccessMessage(rateWritesEnabled, channelDisplayName(channel)));
    } catch (reason) {
      setNotice(errorMessage(reason, channelDiscoveryErrorMessage(rateWritesEnabled, channelDisplayName(channel))));
    } finally {
      setChannelBusy(channel, null);
      finishOperation();
    }
  };

  const deleteChannel = async (channel: UpstreamChannel) => {
    if (channelAccountCount(channel) > 0) return;
    if (!window.confirm(`确认删除空渠道「${channelDisplayName(channel)}」？`)) return;
    const finishOperation = onOperationStart();
    setChannelBusy(channel, "delete");
    setError("");
    setNotice("");
    try {
      const result = await api.deleteUpstreamChannel(channel.id);
      await loadData(true);
      setNotice(result.message || "空渠道已删除。");
    } catch (reason) {
      setError(errorMessage(reason, "空渠道删除失败"));
    } finally {
      setChannelBusy(channel, null);
      finishOperation();
    }
  };

  const discoverAll = async () => {
    if (!assignedChannels.length) {
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
        commitData(normalized);
        onCacheChange(normalized, cacheBaseUrl);
        setLiveDataValidated(true);
      } else {
        await loadData(true);
      }
      await refreshChangeLogUnreadCounts();
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
    const currentlyEnabled = account.remote_schedulable === true;
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

  const testAccountAvailability = (account: UpstreamAccount): Promise<UpstreamAccount | null> => {
    const key = accountKey(account);
    const existingTest = availabilityTestPromisesRef.current.get(key);
    if (existingTest) return existingTest;

    const testPromise = (async () => {
      const finishOperation = onOperationStart();
      setAccountBusy(account, "availability-test");
      setError("");
      setNotice("");
      try {
        const result = await api.testUpstreamAccountAvailability(
          account.sub2api_account_id,
          expectedIdentityFingerprint(account),
        );
        const dataWithTestResult = mergeUpstreamAccountSnapshot(dataRef.current, result.account);
        if (dataWithTestResult !== dataRef.current) {
          commitData(dataWithTestResult);
          onCacheChange(dataWithTestResult, cacheBaseUrl);
        }
        const refreshedData = await loadData(true);
        const refreshedEntry = refreshedData
          ? flattenUpstreamAccounts(refreshedData).find(
              (entry) => String(entry.account.sub2api_account_id) === String(account.sub2api_account_id),
            )
          : null;
        const refreshedAccount = refreshedEntry?.account || result.account;
        setAccountCollectionDialog((current) => current ? {
          ...current,
          accounts: current.accounts.map((item) =>
            String(item.sub2api_account_id) === String(account.sub2api_account_id)
              ? refreshedAccount
              : item),
          channel: current.channel
            ? refreshedEntry?.channel || current.channel
            : current.channel,
        } : current);
        const availabilityStatus = String(result.account.availability_status || "unknown").toLowerCase();
        const status = availabilityStatus === "disabled"
          ? "未启用监测"
          : availabilityStatus === "not_configured"
            ? "检测条件未配置完整"
            : upstreamStatusLabel(availabilityStatus);
        const testAttempts = finiteNumber(result.evidence.test_attempts);
        const purpose = result.evidence.test_purpose === "recovery"
          ? "恢复判定"
          : result.evidence.test_purpose === "pause"
            ? "暂停判定"
            : "可用性测试";
        const policyFailure = result.policy_error
          ? result.policy_action === "hold"
            ? "；可用性判定已完成，但自动暂停账号失败"
            : result.policy_action === "clear"
              ? "；可用性判定已完成，但自动恢复账号失败"
              : "；可用性判定已完成，但账号状态协调失败"
          : "";
        const monitorRefreshStatus = String(result.evidence.monitor_refresh_status || "");
        const monitorRefresh = monitorRefreshStatus === "refreshed"
          ? "；已先同步上游监控面板"
          : monitorRefreshStatus === "failed"
            ? "；上游监控面板同步失败，已按回退策略检测"
            : "";
        setNotice(
          `${accountDisplayName(account)} ${purpose}完成：${status}`
          + (testAttempts === null ? "" : `；实际连接测试 ${testAttempts} 次`)
          + monitorRefresh
          + policyFailure
          + "。",
        );
        return refreshedAccount;
      } catch (reason) {
        setNotice(errorMessage(reason, `${accountDisplayName(account)} 可用性测试失败`));
        return null;
      } finally {
        setAccountBusy(account, null);
        finishOperation();
      }
    })();

    availabilityTestPromisesRef.current.set(key, testPromise);
    void testPromise.finally(() => {
      if (availabilityTestPromisesRef.current.get(key) === testPromise) {
        availabilityTestPromisesRef.current.delete(key);
      }
    });
    return testPromise;
  };

  const forceAccountConnectionTest = (account: UpstreamAccount): Promise<void> => {
    const key = accountKey(account);
    const existingTest = connectionTestPromisesRef.current.get(key);
    if (existingTest) return existingTest;

    const testPromise = (async () => {
      const finishOperation = onOperationStart();
      setAccountBusy(account, "connection-test");
      setError("");
      setNotice("");
      try {
        const result = await api.testUpstreamAccountConnection(
          account.sub2api_account_id,
          expectedIdentityFingerprint(account),
        );
        const status = result.success ? "可用" : "不可用";
        const detail = result.success ? "" : `；${result.error || "连接测试失败"}`;
        setNotice(`${accountDisplayName(account)} 强制连接测试完成：${status}；模型 ${result.model}${detail}。`);
      } catch (reason) {
        setError(errorMessage(reason, `${accountDisplayName(account)} 强制连接测试失败`));
      } finally {
        setAccountBusy(account, null);
        finishOperation();
      }
    })();

    connectionTestPromisesRef.current.set(key, testPromise);
    void testPromise.finally(() => {
      if (connectionTestPromisesRef.current.get(key) === testPromise) {
        connectionTestPromisesRef.current.delete(key);
      }
    });
    return testPromise;
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
  const selectedChannelForAccountForm = data.channels.find(
    (channel) => String(channel.id) === accountForm.channelId,
  );
  const editingAccountModels = editingAccount?.available_models || [];
  const availabilityMonitoringDisabled = accountForm.availabilityCheckMode === "disabled";
  const configuredFallbackModel = accountForm.availabilityTestModel.trim()
    || channelMonitorFallbackTestModels.find(
      (model) => editingAccountModels.some((availableModel) => availableModel.id === model),
    )
    || channelMonitorFallbackTestModels[0]
    || "";
  const fallbackModelAllowed = Boolean(configuredFallbackModel)
    && editingAccountModels.some((model) => model.id === configuredFallbackModel);
  const availabilityTestModelBlocked = !configuredFallbackModel || !fallbackModelAllowed;
  const availabilityModelWarning = !availabilityMonitoringDisabled && availabilityTestModelBlocked
    ? configuredFallbackModel
      ? `${accountForm.availabilityCheckMode === "channel_monitor" ? "回退" : "独立测试"}模型 ${configuredFallbackModel} 不在该账号白名单中，禁止执行测试。`
      : `${accountForm.availabilityCheckMode === "channel_monitor" ? "回退" : "独立测试"}模型未配置或白名单尚未同步，禁止执行测试。`
    : "";

  return (
    <section className="api-key-view api-key-channel-view" aria-label="API Key 账号管理">
      <div className="api-key-subview-tabs" role="tablist" aria-label="API Key 子页面">
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
          <span>上游分组变化</span>
          {visibleUnreadCounts.upstream_changes ? (
            <span className="api-key-tab-count">{visibleUnreadCounts.upstream_changes}</span>
          ) : null}
        </button>
        <button
          aria-selected={subview === "schedule-log"}
          className={subview === "schedule-log" ? "active" : ""}
          onClick={() => onSubviewChange("schedule-log")}
          role="tab"
          type="button"
        >
          <Activity size={16} />
          <span>账号调度变化</span>
          {visibleUnreadCounts.account_scheduling_changes ? (
            <span className="api-key-tab-count">{visibleUnreadCounts.account_scheduling_changes}</span>
          ) : null}
        </button>
        <button
          aria-selected={subview === "account-rate-log"}
          className={subview === "account-rate-log" ? "active" : ""}
          onClick={() => onSubviewChange("account-rate-log")}
          role="tab"
          type="button"
        >
          <TrendingUp size={16} />
          <span>账号倍率变化</span>
          {visibleUnreadCounts.account_rate_changes ? (
            <span className="api-key-tab-count">{visibleUnreadCounts.account_rate_changes}</span>
          ) : null}
        </button>
      </div>

      {subview !== "rate-log" && subview !== "account-rate-log" && subview !== "schedule-log" ? <>
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
        <UpstreamBalanceSummary channels={assignedChannels} />
      </div>

      {error ? (
        <Feedback tone="error" onClose={() => setError("")}>{error}</Feedback>
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
              <span>上游</span>
              <select
                onChange={(event) => setAccountUpstreamFilter(event.target.value as AccountUpstreamFilter)}
                value={accountUpstreamFilter}
              >
                <option value="all">全部上游</option>
                {upstreamOptions.channels.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
                {upstreamOptions.hasUnassigned ? <option value="__unassigned__">未分配上游</option> : null}
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
              <select onChange={(event) => setAccountStatusFilter(event.target.value as AccountStatusFilter)} value={accountStatusFilter}>
                <option value="all">全部状态</option>
                <option value="enabled">已启用</option>
                <option value="disabled">已停用</option>
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
                  channelMonitorFallbackTestModels={channelMonitorFallbackTestModels}
                  displayTimeZone={displayTimeZone}
                  globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                  key={accountKey(entry.account)}
                  onConfigure={() => openAccountConfig(entry.account, entry.channel || undefined)}
                  onDelete={() => deleteRemoteAccount(entry.account)}
                  onPriorityIntervalChange={(intervalId) => void setAccountPriorityInterval(entry.account, intervalId)}
                  onPriorityTieMove={(direction) => void moveAccountPriority(entry.account, direction)}
                  onShowChannel={() => openAccountUpstream(entry)}
                  onTestAvailability={() => void testAccountAvailability(entry.account)}
                  onForceConnectionTest={() => void forceAccountConnectionTest(entry.account)}
                  onToggle={() => toggleAccountEnabled(entry.account)}
                  priorityIntervals={priorityIntervals}
                  priorityTieMove={priorityTieMoves.get(String(entry.account.sub2api_account_id))}
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
              disabled={mutationControlsDisabled || anyBusy || !assignedChannels.length}
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
            <small>{filteredChannels.length}/{occupancyChannels.length}</small>
          </label>
          <div className="api-key-segmented api-key-channel-occupancy-filter" role="group" aria-label="上游账号状态">
            <button
              aria-pressed={channelOccupancyFilter === "occupied"}
              className={channelOccupancyFilter === "occupied" ? "active" : ""}
              onClick={() => setChannelOccupancyFilter("occupied")}
              type="button"
            >有账号上游 {occupiedChannels.length}</button>
            <button
              aria-pressed={channelOccupancyFilter === "no_enabled"}
              className={channelOccupancyFilter === "no_enabled" ? "active" : ""}
              onClick={() => setChannelOccupancyFilter("no_enabled")}
              type="button"
            >无启用上游 {noEnabledChannels.length}</button>
            <button
              aria-pressed={channelOccupancyFilter === "empty"}
              className={channelOccupancyFilter === "empty" ? "active" : ""}
              onClick={() => setChannelOccupancyFilter("empty")}
              type="button"
            >无账号上游 {emptyChannels.length}</button>
          </div>
          <label className="api-key-filter-select">
            <span>状态</span>
            <select onChange={(event) => setChannelStatusFilter(event.target.value as ChannelStatusFilter)} value={channelStatusFilter}>
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
                onDelete={() => void deleteChannel(channel)}
                onDiscover={() => void discoverChannel(channel)}
                onShowAccounts={() => openChannelAccounts(channel)}
                onShowGroups={() => openChannelGroups(channel)}
                onShowMonitors={() => openChannelMonitors(channel)}
                onShowUsageHistory={() => openChannelUsageHistory(channel)}
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
          onViewAccounts={viewPriorityIntervalAccounts}
          rebalancing={priorityIntervalsBusy}
          shareSameCompositePriority={shareSameCompositePriority}
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
              {sortUpstreamAccountEntriesByName(accountCollectionDialog.accounts.map((account) => ({
                account,
                channel: accountCollectionDialog.channel,
              }))).map((entry) => (
                <AccountCard
                  account={entry.account}
                  busyAction={busyAccounts[accountKey(entry.account)]}
                  channel={entry.channel}
                  channelMonitorFallbackTestModels={channelMonitorFallbackTestModels}
                  displayTimeZone={displayTimeZone}
                  globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                  key={accountKey(entry.account)}
                  onConfigure={() => openAccountConfig(entry.account, entry.channel || undefined)}
                  onDelete={() => void deleteRemoteAccount(entry.account)}
                  onPriorityIntervalChange={(intervalId) => void setAccountPriorityInterval(entry.account, intervalId)}
                  onPriorityTieMove={(direction) => void moveAccountPriority(entry.account, direction)}
                  onShowChannel={() => openAccountUpstream(entry)}
                  onTestAvailability={() => void testAccountAvailability(entry.account)}
                  onForceConnectionTest={() => void forceAccountConnectionTest(entry.account)}
                  onToggle={() => void toggleAccountEnabled(entry.account)}
                  priorityIntervals={priorityIntervals}
                  priorityTieMove={priorityTieMoves.get(String(entry.account.sub2api_account_id))}
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
              onDiscover={() => void discoverChannel(accountUpstreamDialog)}
              onShowAccounts={() => openChannelAccounts(accountUpstreamDialog)}
              onShowGroups={() => openChannelGroups(accountUpstreamDialog)}
              onShowMonitors={() => openChannelMonitors(accountUpstreamDialog)}
              onShowUsageHistory={() => openChannelUsageHistory(accountUpstreamDialog)}
              rateWritesEnabled={rateWritesEnabled}
            />
          </div>
        </Modal>
      ) : null}

      {channelGroupDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow={`${channelGroupDialog.group_options?.length || 0} 个上游分组`}
          onClose={closeDialog}
          saving={false}
          title={channelDisplayName(channelGroupDialog)}
        >
          <ChannelGroupList channel={channelGroupDialog} />
        </Modal>
      ) : null}

      {channelMonitorDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow={`${channelMonitorDialog.channel_monitor_count ?? channelMonitorDialog.channel_monitors?.length ?? 0} 个渠道监控`}
          onClose={closeDialog}
          saving={false}
          title={channelDisplayName(channelMonitorDialog)}
        >
          <ChannelMonitorList
            channel={channelMonitorDialog}
            displayTimeZone={displayTimeZone}
            error={channelMonitorError}
            loading={channelMonitorLoading}
            onRefresh={() => void refreshChannelMonitors(channelMonitorDialog)}
          />
        </Modal>
      ) : null}

      {channelUsageHistoryDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow="历史用量与每日收支"
          onClose={closeDialog}
          saving={false}
          title={channelDisplayName(channelUsageHistoryDialog)}
        >
          <UpstreamUsageHistoryDialog
            appliedFilters={channelUsageHistoryFilters}
            channel={channelUsageHistoryDialog}
            displayTimeZone={displayTimeZone}
            draftFilters={channelUsageHistoryDraftFilters}
            error={channelUsageHistoryError}
            history={channelUsageHistory}
            loading={channelUsageHistoryLoading}
            onApplyFilters={applyChannelUsageHistoryFilters}
            onDraftFiltersChange={setChannelUsageHistoryDraftFilters}
            onPreset={applyChannelUsageHistoryPreset}
            onRefresh={() => void loadChannelUsageHistory(channelUsageHistoryDialog, channelUsageHistoryFilters)}
          />
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

              <fieldset className="api-key-field api-key-field--wide">
                <legend>优先级方案</legend>
                <div className="api-key-segmented api-key-segmented--two" role="group" aria-label="优先级分配方案">
                  {([
                    ["cost_optimized", "低倍率优先"],
                    ["fixed_step", "固定间隔"],
                  ] as const).map(([strategy, label]) => (
                    <button
                      aria-pressed={priorityIntervalForm.allocationStrategy === strategy}
                      className={priorityIntervalForm.allocationStrategy === strategy ? "active" : ""}
                      key={strategy}
                      onClick={() => setPriorityIntervalForm((current) => ({
                        ...current,
                        allocationStrategy: strategy,
                      }))}
                      type="button"
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <small>{priorityIntervalForm.allocationStrategy === "cost_optimized"
                  ? "按综合倍率比例计算成本效率，低倍率账号获得更低的优先级数值和更高的调度权重。"
                  : "按综合倍率排序后，从区间起点开始使用固定间隔依次分配优先级。"}</small>
              </fieldset>

              <label className="api-key-field">
                <span>起始优先级</span>
                <input
                  inputMode="numeric"
                  min="0"
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
                  min="1"
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, endPriority: event.target.value }))}
                  required
                  step="1"
                  type="number"
                  value={priorityIntervalForm.endPriority}
                />
              </label>
              <div className="api-key-field api-key-field--wide">
                <small>
                  同一 Sub2API 调度分组内，数值更低的区间权重更高；需要形成固定层级时，建议连续设置且不重叠，例如 [40, 70) 与 [70, 100)。
                </small>
              </div>
              {priorityIntervalForm.allocationStrategy === "fixed_step" ? (
                <label className="api-key-field api-key-field--wide">
                  <span>固定优先级间隔</span>
                  <input
                    inputMode="numeric"
                    min="1"
                    onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, step: event.target.value }))}
                    required
                    step="1"
                    type="number"
                    value={priorityIntervalForm.step}
                  />
                  <small>计算方式为起始优先级 + 排名 × 固定间隔；超过区间容量时末尾账号共用最高档位。</small>
                </label>
              ) : (
                <div className="api-key-field api-key-field--wide">
                  <small>最低和最高倍率占据区间两端，中间使用几何中位数反比例曲线；取整冲突由系统使用 1 个优先级的最小间隔处理。</small>
                </div>
              )}
              <label className="checkbox-line settings-toggle api-key-field--wide api-key-rate-pause-toggle">
                <input
                  checked={priorityIntervalForm.ratePauseEnabled}
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, ratePauseEnabled: event.target.checked }))}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <strong>综合上游倍率上涨时自动暂停账号</strong>
                  <small>绑定此区间且选择“继承”的账号会使用下面的阈值；综合倍率严格大于阈值时暂停，等于或低于阈值时不暂停。</small>
                </span>
              </label>
              <label className="api-key-field api-key-field--wide">
                <span>综合倍率阈值</span>
                <input
                  aria-label="优先级区间综合倍率阈值"
                  disabled={!priorityIntervalForm.ratePauseEnabled}
                  min="0.000001"
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, rateAbsoluteThreshold: event.target.value }))}
                  step="any"
                  type="number"
                  value={priorityIntervalForm.rateAbsoluteThreshold}
                />
                <small>设置保存后，在下一次上游同步时应用；只有综合倍率严格大于该阈值才会暂停账号。</small>
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
                <span>API 地址</span>
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
                  placeholder="留空表示与 API 地址相同"
                  type="url"
                  value={channelForm.managementBaseUrl}
                />
                <small>余额、分组和 Key 列表从管理地址读取；模型请求仍使用 API 地址。</small>
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

              {editingChannelType !== "sub2api" ? <label className="api-key-field">
                <span>NewAPI 用户 ID（余额探测）</span>
                <input
                  onChange={(event) => setChannelForm((current) => ({ ...current, upstreamUserId: event.target.value }))}
                  placeholder="填写数字用户 ID"
                  value={channelForm.upstreamUserId}
                />
                <small>NewAPI 用户余额接口通常要求 New-Api-User；请填写个人设置中显示的数字用户 ID。</small>
              </label> : null}

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
            <fieldset className="api-key-config-fields api-key-config-fieldset" disabled={savingDialog}>
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
                      availabilityMonitorId: "",
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

              <label className="api-key-field api-key-field--wide">
                <span>账号停用后仍参与优先级分配</span>
                <select
                  onChange={(event) => setAccountForm((current) => ({
                    ...current,
                    priorityAssignmentWhenDisabled: event.target.value as AccountForm["priorityAssignmentWhenDisabled"],
                  }))}
                  value={accountForm.priorityAssignmentWhenDisabled}
                >
                  <option value="inherit">
                    继承全局（当前{editingAccount.priority_assignment_when_disabled_effective ? "开启" : "关闭"}）
                  </option>
                  <option value="enabled">此账号强制开启</option>
                  <option value="disabled">此账号强制关闭</option>
                </select>
                <small>仅影响优先级计算，不会停用或启用账号；可继承全局规则，也可单独覆盖。</small>
              </label>

              <div className="api-key-field api-key-field--wide">
                <span>倍率上涨暂停策略</span>
                <select
                  aria-label="倍率上涨暂停策略"
                  onChange={(event) => setAccountForm((current) => ({
                    ...current,
                    ratePausePolicy: event.target.value as AccountForm["ratePausePolicy"],
                  }))}
                  value={accountForm.ratePausePolicy}
                >
                  <option value="inherit">继承优先级区间</option>
                  <option value="disabled">关闭</option>
                  <option value="custom">单独设置</option>
                </select>
                <small>
                  {accountForm.ratePausePolicy === "inherit"
                    ? (editingAccount.priority_interval_id == null
                      ? "当前账号未绑定优先级区间，当前配置不会启用倍率上涨暂停。"
                      : "跟随所绑定优先级区间的开关和阈值。")
                    : accountForm.ratePausePolicy === "disabled"
                      ? "此账号不会因综合上游倍率超过阈值而自动暂停。"
                      : "账号独立阈值优先于优先级区间；回落到阈值后自动恢复。"}
                </small>
              </div>
              {accountForm.ratePausePolicy === "custom" ? (
                <label className="api-key-field api-key-field--wide">
                  <span>综合倍率阈值</span>
                  <input
                    aria-label="账号综合倍率阈值"
                    min="0.000001"
                    onChange={(event) => setAccountForm((current) => ({ ...current, rateAbsoluteThreshold: event.target.value }))}
                    step="any"
                    type="number"
                    value={accountForm.rateAbsoluteThreshold}
                  />
                </label>
              ) : null}

              <div className="api-key-field api-key-field--wide">
                <span className="api-key-field-label settings-label-with-help">
                  <label htmlFor="api-key-availability-mode">API Key 可用性检测方式</label>
                  <HelpPopover label="查看 API Key 可用性检测说明">
                    默认使用监控面板模式。绑定后，面板可用时不直连账号；面板异常时按设置中的回退测试模型链选择首个属于账号白名单的模型。未绑定面板时保留该模式，并按全局开关决定是否执行回退测试。
                  </HelpPopover>
                </span>
                <select
                  id="api-key-availability-mode"
                  onChange={(event) => setAccountForm((current) => ({
                    ...current,
                    availabilityCheckMode: event.target.value as AccountForm["availabilityCheckMode"],
                    availabilityMonitorId: event.target.value === "channel_monitor" ? current.availabilityMonitorId : "",
                  }))}
                  value={accountForm.availabilityCheckMode}
                >
                  <option value="channel_monitor">绑定监控面板（默认）</option>
                  <option value="independent_model">独立模型测试</option>
                  <option value="disabled">关闭</option>
                </select>
              </div>

              {accountForm.availabilityCheckMode === "channel_monitor" ? (
                <div className="api-key-field api-key-field--wide">
                  <span className="api-key-field-label settings-label-with-help">
                    <label htmlFor="api-key-availability-monitor">绑定监控点</label>
                    <HelpPopover label="查看监控点绑定说明">
                      每个监控面板只代表上游的一个分组或模型路由，不代表上游站点整体状态。未选择面板也可保存，状态标志显示黄色外圈；是否直接使用回退测试模型链由设置中的全局开关控制。
                    </HelpPopover>
                  </span>
                  <select
                    id="api-key-availability-monitor"
                    onChange={(event) => setAccountForm((current) => ({ ...current, availabilityMonitorId: event.target.value }))}
                    value={accountForm.availabilityMonitorId}
                  >
                    <option value="">请选择具体监控面板</option>
                    {(selectedChannelForAccountForm?.channel_monitors || []).map((monitor) => (
                      <option key={String(monitor.id)} value={String(monitor.id)}>
                        {monitor.name || `监控点 #${monitor.id}`} · {monitor.primary_model || "未标注模型"}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              {!availabilityMonitoringDisabled ? (
                <label className="api-key-field api-key-field--wide">
                <span>{accountForm.availabilityCheckMode === "channel_monitor" ? "监控异常回退模型" : "独立测试模型"}</span>
                <select
                  onChange={(event) => setAccountForm((current) => ({ ...current, availabilityTestModel: event.target.value }))}
                  value={accountForm.availabilityTestModel}
                >
                  <option value="">使用设置中的默认模型</option>
                  {accountForm.availabilityTestModel
                    && !editingAccountModels.some((model) => model.id === accountForm.availabilityTestModel) ? (
                    <option disabled value={accountForm.availabilityTestModel}>
                      {accountForm.availabilityTestModel}（当前不在白名单）
                    </option>
                  ) : null}
                  {editingAccountModels.map((model) => (
                    <option key={model.id} value={model.id}>{model.display_name || model.id}</option>
                  ))}
                </select>
                <small className={availabilityModelWarning ? "form-error" : undefined}>
                  {availabilityModelWarning || (editingAccountModels.length
                      ? `已同步 ${editingAccountModels.length} 个可用模型；只能从该账号白名单选择。`
                      : editingAccount?.available_models_status === "error"
                        ? "模型白名单同步失败，请先重新同步 API Key 账号。"
                        : "尚未同步模型白名单；首次导入或本地缺失时会自动同步。")}
                </small>
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
            </fieldset>
            <DialogError message={dialogError} />
            <DialogActions
              onCancel={closeDialog}
              saving={savingDialog}
              savingLabel={accountSaveWaitingForTest ? "等待检测完成" : undefined}
            />
          </form>
        </Modal>
      ) : null}
      </> : subview === "rate-log" || subview === "account-rate-log" ? (
        <RateChangeLogView
          displayTimeZone={displayTimeZone}
          draftFilters={rateLogDraftFilters}
          error={rateLogsError}
          filtersApplied={Boolean(rateLogFilters.startDate || rateLogFilters.endDate)}
          hasMore={rateLogsHasMore}
          loading={rateLogsLoading}
          logs={rateLogs}
          channels={data.channels}
          kind={subview === "account-rate-log" ? "account_rate" : "upstream"}
          onApplyFilters={applyRateLogFilters}
          onClearFilters={clearRateLogFilters}
          onDraftFiltersChange={setRateLogDraftFilters}
          onLoadMore={() => void loadRateLogs(true)}
          onRefresh={() => void loadRateLogs()}
        />
      ) : (
        <SchedulingChangeLogView
          displayTimeZone={displayTimeZone}
          draftFilters={scheduleLogDraftFilters}
          error={scheduleLogsError}
          filtersApplied={Boolean(scheduleLogFilters.startDate || scheduleLogFilters.endDate)}
          hasMore={scheduleLogsHasMore}
          loading={scheduleLogsLoading}
          logs={scheduleLogs}
          onApplyFilters={applyScheduleLogFilters}
          onClearFilters={clearScheduleLogFilters}
          onDraftFiltersChange={setScheduleLogDraftFilters}
          onLoadMore={() => void loadScheduleLogs(true)}
          onRefresh={() => void loadScheduleLogs()}
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
  onDelete,
  onDiscover,
  onShowAccounts,
  onShowGroups,
  onShowMonitors,
  onShowUsageHistory,
  rateWritesEnabled,
}: {
  channel: UpstreamChannel;
  accountCount: number;
  displayTimeZone: string;
  busyAction?: string;
  globallyDisabled: boolean;
  onConfigureChannel: () => void;
  onDelete?: () => void;
  onDiscover: () => void;
  onShowAccounts: () => void;
  onShowGroups: () => void;
  onShowMonitors: () => void;
  onShowUsageHistory: () => void;
  rateWritesEnabled: boolean;
}) {
  const groups = channel.group_options || [];
  const visibleGroups = groups.slice(0, 4);
  const type = resolvedChannelType(channel);
  const status = channelStatus(channel);
  const apiUrl = channelBaseUrl(channel);
  const configuredManagementUrl = channel.management_base_url?.trim() || "";
  const siteUrl = configuredManagementUrl || apiUrl;
  const displayName = channelDisplayName(channel);
  const isUrlDisplayName = urlLikeDisplayName(displayName, apiUrl)
    || urlLikeDisplayName(displayName, siteUrl);
  const hasSeparateManagementUrl = Boolean(configuredManagementUrl)
    && displayCanonicalUrl(configuredManagementUrl) !== displayCanonicalUrl(apiUrl);
  const message = channelDisplayMessage(channel);
  const error = channelDisplayError(channel);
  const busy = Boolean(busyAction) || globallyDisabled;
  const discoveryCopy = upstreamDiscoveryCopy(rateWritesEnabled);
  const balanceDetails = balanceDetail(channel, displayTimeZone);
  const todayUsage = formatDailyBalanceUsed(channel, "today", displayTimeZone);
  const yesterdayUsage = formatDailyBalanceUsed(channel, "yesterday", displayTimeZone);
  return (
    <article className={
      "api-key-channel-card"
      + (channelHasAttention(channel) ? " api-key-channel-card--attention" : "")
    }>
      <header className="api-key-channel-head">
        <div className="api-key-channel-mark" aria-hidden="true"><Globe2 size={18} /></div>
        <div className="api-key-channel-title">
          <div className="api-key-channel-title-row">
            <h3 title={isUrlDisplayName ? displayName : undefined}>
              {isUrlDisplayName ? <MiddleEllipsisText text={displayName} /> : displayName}
            </h3>
            <div className="api-key-inline-chips">
              <StatusChip status={type} />
              <StatusChip status={status} />
            </div>
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
          {accountCount > 0 ? (
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
          ) : (
            <button
              aria-label={"删除空渠道 " + channelDisplayName(channel)}
              className="api-key-icon-button api-key-icon-button--danger"
              disabled={busy}
              onClick={onDelete}
              title="删除空渠道"
              type="button"
            >
              <Trash2 size={15} />
            </button>
          )}
        </div>
        <div className="api-key-channel-addresses">
          <ChannelAddressBox label="站点" url={siteUrl} />
          <ChannelAddressBox label="API" url={hasSeparateManagementUrl ? apiUrl : ""} />
        </div>
      </header>

      <div className="api-key-channel-stats">
        <ChannelStat
          badge={<StatusChip status={channel.balance_status || channel.status || "not_checked"} />}
          className="api-key-channel-stat--balance"
          icon={<WalletCards size={16} />}
          label="上游余额"
        >
          <div className="api-key-channel-balance-chips">
            <div className="api-key-channel-balance-chip api-key-chip api-key-chip--info">
              <HelpPopover
                label="查看原始余额说明"
                trigger={<span>原</span>}
                triggerClassName="api-key-balance-kind api-key-balance-kind--original"
              >
                上游钱包原始余额，直接读取自上游站点的钱包余额。
              </HelpPopover>
              <b>{formatCurrentPlatformBalance(channel)}</b>
            </div>
            <div className="api-key-channel-balance-chip api-key-chip api-key-chip--success">
              <HelpPopover
                label="查看综合余额说明"
                trigger={<span>综</span>}
                triggerClassName="api-key-balance-kind api-key-balance-kind--combined"
              >
                综合余额等于上游钱包原始余额乘以上游充值倍率。
              </HelpPopover>
              <b>{formatCurrentRechargeAdjustedBalance(channel)}</b>
            </div>
          </div>
          <div className="api-key-channel-daily-usage">
            {[yesterdayUsage, todayUsage].map((usage) => (
              <span
                className={"api-key-channel-usage-chip api-key-chip api-key-chip--" + usage.tone}
                key={usage.label}
                title={`${usage.label}消耗余额 ${usage.value}${usage.stale ? "，上游本次探测失败，显示当天最后一次有效值" : ""}`}
              >
                <span>{usage.label}{usage.stale ? "（旧）" : ""}</span><b>{usage.value}</b>
              </span>
            ))}
          </div>
          {balanceDetails ? <span>{balanceDetails}</span> : null}
        </ChannelStat>
        <ChannelStat className="api-key-channel-stat--recharge" icon={<BadgeDollarSign size={16} />} label="充值成本">
          <strong>{"¥" + formatCostPerUsd(channel.effective_recharge_multiplier) + " / $1"}</strong>
        </ChannelStat>
        <ChannelStat className="api-key-channel-stat--probe" icon={<Radar size={16} />} label="最近探测">
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
          {upstreamChannelTokenInvalid(channel)
            ? "Access Token 已失效"
            : channel.access_token_set
              ? "Access Token 已配置"
              : "缺少 Access Token"}
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

      <button
        aria-label={`查看 ${channelDisplayName(channel)} 的 ${groups.length} 个上游分组`}
        className="api-key-channel-groups"
        onClick={onShowGroups}
        type="button"
      >
        <div className="api-key-channel-section-label">
          <span>上游分组</span>
          <div className="api-key-channel-section-tools">
            <small>{groups.length}</small>
            <ArrowRight size={14} />
          </div>
        </div>
        <div className="api-key-group-chips">
          {visibleGroups.length ? visibleGroups.map((group) => (
            <span className="api-key-group-chip" key={group.id} title={group.name + " " + formatMultiplier(group.multiplier)}>
              <span>{group.name}</span>
              <strong>{formatMultiplier(group.multiplier)}</strong>
            </span>
          )) : <span className="api-key-muted">同步后显示上游分组倍率</span>}
          {groups.length > visibleGroups.length ? (
            <span className="api-key-group-chip api-key-group-chip--more">+{groups.length - visibleGroups.length}</span>
          ) : null}
        </div>
      </button>

      {error ? (
        <div className="api-key-channel-error" title={error}>
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      ) : null}

      <section className="api-key-channel-accounts" aria-label="渠道账号">
        <button
          aria-label={`查看 ${channelDisplayName(channel)} 的 ${accountCount} 个账号`}
          className="api-key-channel-account-button"
          onClick={onShowAccounts}
          type="button"
        >
          <UsersRound size={16} />
          <span>账号 {accountCount} 个</span>
          <ArrowRight size={15} />
        </button>
        <button
          aria-label={`查看 ${channelDisplayName(channel)} 的历史用量统计`}
          className="api-key-channel-account-button"
          onClick={onShowUsageHistory}
          type="button"
        >
          <ChartNoAxesCombined size={16} />
          <span>统计</span>
          <ArrowRight size={15} />
        </button>
        <button
          aria-label={`查看 ${channelDisplayName(channel)} 的 ${channel.channel_monitor_count ?? channel.channel_monitors?.length ?? 0} 个渠道状态`}
          className="api-key-channel-account-button"
          onClick={onShowMonitors}
          type="button"
        >
          <Activity size={16} />
          <span>渠道状态 {channel.channel_monitor_count ?? channel.channel_monitors?.length ?? 0} 个</span>
          <ArrowRight size={15} />
        </button>
      </section>
    </article>
  );
}

function ChannelGroupList({ channel }: { channel: UpstreamChannel }) {
  const groups = channel.group_options || [];
  return (
    <div className="api-key-group-dialog-list">
      {groups.length ? groups.map((group, index) => (
        <div className="api-key-group-dialog-row" key={group.id}>
          <span>{index + 1}</span>
          <div>
            <strong>{group.name || group.id}</strong>
            <small>{group.id}</small>
          </div>
          <b>{formatMultiplier(group.multiplier)}</b>
        </div>
      )) : (
        <div className="api-key-empty">
          <ListOrdered size={18} />
          <span>同步后显示上游分组倍率</span>
        </div>
      )}
    </div>
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
          aria-label={`查看 ${accountCount} 个待分配账号`}
          className="api-key-channel-account-button"
          onClick={onShowAccounts}
          type="button"
        >
          <UsersRound size={16} />
          <span>账号 {accountCount} 个</span>
          <ArrowRight size={15} />
        </button>
      </section>
    </article>
  );
}

function ChannelMonitorList({
  channel,
  displayTimeZone,
  error,
  loading,
  onRefresh,
}: {
  channel: UpstreamChannel;
  displayTimeZone: string;
  error: string;
  loading: boolean;
  onRefresh: () => void;
}) {
  const monitors = channel.channel_monitors || [];
  return (
    <div className="api-key-monitor-dialog-body">
      {loading ? <div className="api-key-monitor-loading" role="status"><RefreshCcw className="spin" size={16} /><span>正在读取渠道状态…</span></div> : null}
      {error ? <div className="api-key-channel-error" role="alert"><AlertTriangle size={14} /><span>{error}</span></div> : null}
      <div className="api-key-monitor-summary">
        <StatusChip status={channel.channel_monitor_status || "not_checked"} />
        <span>{channelMonitorMessage(channel)}</span>
        <time>{formatDate(channel.channel_monitor_checked_at, displayTimeZone)}</time>
        <button
          aria-label="刷新渠道监控"
          className="api-key-icon-button"
          disabled={loading}
          onClick={onRefresh}
          title="刷新渠道监控"
          type="button"
        >
          <RefreshCcw className={loading ? "spin" : undefined} size={15} />
        </button>
      </div>
      {monitors.length ? (
        <div className="api-key-monitor-list">
          {monitors.map((monitor) => (
            <ChannelMonitorCard
              displayTimeZone={displayTimeZone}
              key={String(monitor.id)}
              monitor={monitor}
            />
          ))}
        </div>
      ) : (
        <div className="api-key-empty">
          <Activity size={18} />
          <span>{channel.channel_monitor_status === "unsupported"
            ? "该上游暂不支持渠道状态接口"
            : channel.channel_monitor_status === "not_configured"
              ? "该上游未配置公开监控面板"
              : "暂无渠道状态数据"}</span>
        </div>
      )}
    </div>
  );
}

function UpstreamUsageHistoryDialog({
  appliedFilters,
  channel,
  displayTimeZone,
  draftFilters,
  error,
  history,
  loading,
  onApplyFilters,
  onDraftFiltersChange,
  onPreset,
  onRefresh,
}: {
  appliedFilters: UsageHistoryFilters;
  channel: UpstreamChannel;
  displayTimeZone: string;
  draftFilters: UsageHistoryFilters;
  error: string;
  history: UpstreamUsageHistory | null;
  loading: boolean;
  onApplyFilters: () => void;
  onDraftFiltersChange: (filters: UsageHistoryFilters) => void;
  onPreset: (preset: UsageHistoryDatePreset) => void;
  onRefresh: () => void;
}) {
  const accountOptions = usageHistoryAccountOptions(history, channel);
  const days = history?.days || [];
  const selectedAccountId = appliedFilters.apiKeyAccountId || null;
  const selectedAccount = selectedAccountId
    ? accountOptions.find((account) => String(account.sub2api_account_id) === selectedAccountId) || null
    : null;
  const totals = history?.totals || null;
  const lifetimeTotals = history?.lifetime_totals || null;
  const costUnit = historyCostUnit(days, selectedAccountId);
  const totalNet = historyNetIncome(totals);
  const lifetimeNet = historyNetIncome(lifetimeTotals);
  const appliedRange = history
    ? `${history.start_date} 至 ${history.end_date}`
    : [appliedFilters.startDate, appliedFilters.endDate].filter(Boolean).join(" 至 ");

  return (
    <div className="api-key-usage-history-dialog-body">
      <div className="api-key-usage-history-toolbar">
        <div className="api-key-usage-history-filters">
          <label>
            <span><CalendarDays size={14} />开始</span>
            <input
              max={draftFilters.endDate || undefined}
              onChange={(event) => onDraftFiltersChange({ ...draftFilters, startDate: event.target.value })}
              type="date"
              value={draftFilters.startDate}
            />
          </label>
          <label>
            <span><CalendarDays size={14} />结束</span>
            <input
              min={draftFilters.startDate || undefined}
              onChange={(event) => onDraftFiltersChange({ ...draftFilters, endDate: event.target.value })}
              type="date"
              value={draftFilters.endDate}
            />
          </label>
          <label>
            <span><KeyRound size={14} />密钥</span>
            <select
              onChange={(event) => onDraftFiltersChange({ ...draftFilters, apiKeyAccountId: event.target.value })}
              value={draftFilters.apiKeyAccountId}
            >
              <option value="">全部账号</option>
              {accountOptions.map((account) => (
                <option key={String(account.sub2api_account_id)} value={String(account.sub2api_account_id)}>
                  {usageHistoryAccountLabel(account)}
                </option>
              ))}
            </select>
          </label>
          <button
            className="api-key-button api-key-button--secondary"
            disabled={loading || usageHistoryDateRangeInvalid(draftFilters)}
            onClick={onApplyFilters}
            type="button"
          >
            <Search size={15} />
            <span>筛选</span>
          </button>
          <button
            aria-label="刷新历史用量"
            className="api-key-icon-button"
            disabled={loading}
            onClick={onRefresh}
            title="刷新"
            type="button"
          >
            <RefreshCcw className={loading ? "spin" : undefined} size={15} />
          </button>
        </div>
        <div aria-label="快捷时间筛选" className="api-key-segmented api-key-usage-history-presets" role="group">
          {([
            ["today", "今天"],
            ["seven_days", "近 7 天"],
            ["thirty_days", "近 30 天"],
            ["ninety_days", "近 90 天"],
            ["this_month", "本月"],
          ] as Array<[UsageHistoryDatePreset, string]>).map(([preset, label]) => (
            <button
              aria-pressed={usageHistoryPresetActive(preset, draftFilters, displayTimeZone)}
              className={usageHistoryPresetActive(preset, draftFilters, displayTimeZone) ? "active" : ""}
              key={preset}
              onClick={() => onPreset(preset)}
              type="button"
            >{label}</button>
          ))}
        </div>
      </div>

      <div className="api-key-usage-history-context">
        <span>{selectedAccount ? `密钥：${usageHistoryAccountLabel(selectedAccount)}` : "全部账号"}</span>
        <small>{appliedRange || "近 30 天"} · {history?.time_zone || displayTimeZone}</small>
      </div>

      {error ? <div className="api-key-channel-error api-key-usage-history-error" role="alert"><AlertTriangle size={14} /><span>{error}</span></div> : null}

      {loading && !history ? (
        <div className="api-key-empty api-key-usage-history-loading" role="status">
          <RefreshCcw className="spin" size={18} />
          <span>正在读取已保存的每日用量…</span>
        </div>
      ) : null}

      {history && !days.length ? (
        <div className="api-key-empty api-key-usage-history-empty">
          <ChartNoAxesCombined size={18} />
          <span>筛选期间暂无已保存的每日用量</span>
        </div>
      ) : null}

      {history && days.length ? (
        <>
          <div className="api-key-usage-history-totals" aria-label="筛选期间汇总">
            <UsageHistoryMetric label="原始成本" value={formatHistoryAmount(totals?.cost, costUnit)} />
            <UsageHistoryMetric label="综合成本" tone="cost" value={formatHistoryAmount(totals?.cost_adjusted, "CNY")} />
            <UsageHistoryMetric label="收入" tone="income" value={formatHistoryAmount(totals?.income, historyIncomeUnit(days))} />
            <UsageHistoryMetric label="净收入" tone={totalNet !== null && totalNet < 0 ? "negative" : "income"} value={formatHistorySignedAmount(totalNet, historyIncomeUnit(days))} />
          </div>

          <UsageHistoryLineChart
            days={days}
            selectedAccountId={selectedAccountId}
            title={selectedAccount ? "每日密钥用量" : "每日上游消耗"}
          />

          <div className="api-key-usage-history-table-wrap">
            <table className="api-key-usage-history-table">
              <thead>
                <tr>
                  <th scope="col">日期</th>
                  <th scope="col">用量</th>
                  <th scope="col">成本（原始 / 综合）</th>
                  <th scope="col">收入</th>
                  <th scope="col">净收入</th>
                </tr>
              </thead>
              <tbody>
                {days.map((day) => {
                  const dailyCost = historyDayCost(day, selectedAccountId);
                  const dailyAdjustedCost = historyDayAdjustedCost(day, selectedAccountId);
                  const dailyNet = historyNetIncome({
                    income: day.income,
                    cost: dailyCost,
                    cost_adjusted: dailyAdjustedCost,
                  });
                  return (
                    <tr key={day.date}>
                      <th scope="row">{formatUsageHistoryDate(day.date, displayTimeZone)}</th>
                      <td>{formatHistoryUsage(historyDayUsage(day, selectedAccountId), historyDayUsageUnit(day, selectedAccountId))}</td>
                      <td>
                        <span>{formatHistoryAmount(dailyCost, historyDayCostUnit(day, selectedAccountId))}</span>
                        <small>{formatHistoryAmount(dailyAdjustedCost, "CNY")}</small>
                      </td>
                      <td>{formatHistoryAmount(day.income, day.income_unit || historyIncomeUnit(days))}</td>
                      <td className={dailyNet !== null && dailyNet < 0 ? "is-negative" : "is-positive"}>
                        {formatHistorySignedAmount(dailyNet, day.income_unit || historyIncomeUnit(days))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {lifetimeTotals ? (
            <div className="api-key-usage-history-lifetime" aria-label="累计收支">
              <span>累计</span>
              <strong>综合成本 {formatHistoryAmount(lifetimeTotals.cost_adjusted, "CNY")}</strong>
              <strong>收入 {formatHistoryAmount(lifetimeTotals.income, historyIncomeUnit(days))}</strong>
              <strong className={lifetimeNet !== null && lifetimeNet < 0 ? "is-negative" : "is-positive"}>
                净收入 {formatHistorySignedAmount(lifetimeNet, historyIncomeUnit(days))}
              </strong>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function UsageHistoryMetric({ label, tone, value }: { label: string; tone?: string; value: string }) {
  return (
    <div className={"api-key-usage-history-metric" + (tone ? ` api-key-usage-history-metric--${tone}` : "")}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function UsageHistoryLineChart({
  days,
  selectedAccountId,
  title,
}: {
  days: UpstreamUsageHistory["days"];
  selectedAccountId: string | null;
  title: string;
}) {
  const series = days.map((day) => ({
    date: day.date,
    original: historyDayUsage(day, selectedAccountId),
    adjusted: historyDayAdjustedUsage(day, selectedAccountId),
  }));
  const values = series.flatMap((point) => [point.original, point.adjusted]).filter((value): value is number => value !== null);
  if (!values.length) {
    return (
      <section className="api-key-usage-history-chart api-key-usage-history-chart--empty" aria-label={title}>
        <div><ChartNoAxesCombined size={16} /><strong>{title}</strong></div>
        <span>筛选期间没有可绘制的用量数据</span>
      </section>
    );
  }

  const width = 760;
  const height = 236;
  const padding = { top: 20, right: 18, bottom: 34, left: 54 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const maximum = Math.max(...values, 0);
  const yMaximum = maximum === 0 ? 1 : maximum * 1.08;
  const xFor = (index: number) => padding.left + (series.length <= 1 ? chartWidth / 2 : (index / (series.length - 1)) * chartWidth);
  const yFor = (value: number | null) => value === null ? null : padding.top + chartHeight - (value / yMaximum) * chartHeight;
  const pointsFor = (key: "original" | "adjusted") => series
    .map((point, index) => {
      const y = yFor(point[key]);
      return y === null ? null : `${xFor(index).toFixed(1)},${y.toFixed(1)}`;
    })
    .filter((point): point is string => point !== null)
    .join(" ");
  const labelIndexes = usageHistoryLabelIndexes(series.length);
  const gridValues = [0, 0.25, 0.5, 0.75, 1];

  return (
    <section className="api-key-usage-history-chart" aria-label={title}>
      <div className="api-key-usage-history-chart-head">
        <div><ChartNoAxesCombined size={16} /><strong>{title}</strong></div>
        <div className="api-key-usage-history-chart-legend">
          <span><i className="api-key-usage-history-line-key api-key-usage-history-line-key--original" />原始</span>
          <span><i className="api-key-usage-history-line-key api-key-usage-history-line-key--adjusted" />综合</span>
        </div>
      </div>
      <svg preserveAspectRatio="none" role="img" viewBox={`0 0 ${width} ${height}`}>
        <title>{title}</title>
        {gridValues.map((fraction) => {
          const y = padding.top + chartHeight - chartHeight * fraction;
          return (
            <g key={fraction}>
              <line className="api-key-usage-history-chart-grid" x1={padding.left} x2={width - padding.right} y1={y} y2={y} />
              <text className="api-key-usage-history-chart-axis" textAnchor="end" x={padding.left - 8} y={y + 4}>
                {formatHistoryChartNumber(yMaximum * fraction)}
              </text>
            </g>
          );
        })}
        {pointsFor("original") ? <polyline className="api-key-usage-history-chart-line api-key-usage-history-chart-line--original" points={pointsFor("original")} /> : null}
        {pointsFor("adjusted") ? <polyline className="api-key-usage-history-chart-line api-key-usage-history-chart-line--adjusted" points={pointsFor("adjusted")} /> : null}
        {series.map((point, index) => {
          const originalY = yFor(point.original);
          const adjustedY = yFor(point.adjusted);
          const x = xFor(index);
          return (
            <g key={point.date}>
              {originalY !== null ? <circle className="api-key-usage-history-chart-dot api-key-usage-history-chart-dot--original" cx={x} cy={originalY} r={3}><title>{`${point.date} 原始 ${formatHistoryUsage(point.original)}`}</title></circle> : null}
              {adjustedY !== null ? <circle className="api-key-usage-history-chart-dot api-key-usage-history-chart-dot--adjusted" cx={x} cy={adjustedY} r={3}><title>{`${point.date} 综合 ${formatHistoryUsage(point.adjusted)}`}</title></circle> : null}
              {labelIndexes.has(index) ? <text className="api-key-usage-history-chart-axis" textAnchor="middle" x={x} y={height - 10}>{shortUsageHistoryDate(point.date)}</text> : null}
            </g>
          );
        })}
      </svg>
    </section>
  );
}

function ChannelMonitorCard({
  displayTimeZone,
  monitor,
}: {
  displayTimeZone: string;
  monitor: UpstreamChannelMonitor;
}) {
  const extraModels = monitor.extra_models || [];
  const timeline = recentChannelMonitorTimeline(monitor.timeline);
  const intrinsicStatus = latestChannelMonitorStatus(monitor.primary_status, monitor.timeline);
  const currentProbe = monitorCurrentProbe(monitor);
  const latestProbeAt = timeline.length
    ? timeline[timeline.length - 1].checked_at || timeline[timeline.length - 1].time
    : null;
  return (
    <article className="api-key-monitor-card">
      <header>
        <div>
          <strong>{monitor.name || `渠道 #${monitor.id}`}</strong>
          <span>{[monitor.provider, monitor.group_name].filter(Boolean).join(" · ") || "未标注分组"}</span>
        </div>
        <div className="api-key-monitor-card-status">
          <div className="api-key-monitor-card-status-row">
            <time title={formatDate(latestProbeAt, displayTimeZone)}>
              {formatDate(latestProbeAt, displayTimeZone)}
            </time>
            <StatusChip status={intrinsicStatus} />
          </div>
          <span className={`api-key-monitor-current api-key-monitor-current--${currentProbe.tone}`}>
            {currentProbe.tone === "success" ? <CheckCircle2 size={13} /> : <AlertTriangle size={13} />}
            {currentProbe.label}
          </span>
        </div>
      </header>
      <div className="api-key-monitor-primary">
        <div><span>主模型</span><strong>{monitor.primary_model || "—"}</strong></div>
        <div><span>请求延迟</span><strong>{formatMonitorLatency(monitor.primary_latency_ms)}</strong></div>
        <div><span>Ping</span><strong>{formatMonitorLatency(monitor.primary_ping_latency_ms)}</strong></div>
        <div><span>{monitor.availability_window === "24h" ? "24 小时可用率" : "7 天可用率"}</span><strong>{formatMonitorAvailability(monitor.availability_7d)}</strong></div>
      </div>
      {extraModels.length ? (
        <div className="api-key-monitor-models" aria-label="额外模型状态">
          {extraModels.map((model, index) => (
            <span className={`api-key-chip api-key-chip--${upstreamStatusTone(model.status || "unknown")}`} key={`${model.name || "model"}:${index}`}>
              <span>{model.name || model.model || "未命名模型"}</span>
              <strong>{formatMonitorLatency(model.latency_ms)}</strong>
            </span>
          ))}
        </div>
      ) : null}
      {timeline.length ? (
        <div className="api-key-monitor-timeline" aria-label="近期状态时间线">
          {timeline.map((point, index) => (
            <span
              className={`api-key-monitor-timeline-point api-key-monitor-timeline-point--${upstreamStatusTone(point.status || "unknown")}`}
              key={`${point.time || point.checked_at || "point"}:${index}`}
              title={`${formatDate(point.time || point.checked_at, displayTimeZone)} · ${upstreamStatusLabel(point.status || "unknown")} · ${formatMonitorLatency(point.latency_ms)}`}
            />
          ))}
        </div>
      ) : null}
    </article>
  );
}

function AccountCard({
  account,
  busyAction,
  channel,
  channelMonitorFallbackTestModels,
  displayTimeZone,
  globallyDisabled,
  onConfigure,
  onDelete,
  onPriorityIntervalChange,
  onPriorityTieMove,
  onShowChannel,
  onTestAvailability,
  onForceConnectionTest,
  onToggle,
  priorityIntervals,
  priorityTieMove,
  rateWritesEnabled,
}: {
  account: UpstreamAccount;
  busyAction?: string;
  channel: UpstreamChannel | null;
  channelMonitorFallbackTestModels: string[];
  displayTimeZone: string;
  globallyDisabled: boolean;
  onConfigure: () => void;
  onDelete: () => void;
  onPriorityIntervalChange: (intervalId: number | string | null) => void;
  onPriorityTieMove: (direction: "up" | "down") => void;
  onShowChannel: () => void;
  onTestAvailability: () => void;
  onForceConnectionTest: () => void;
  onToggle: () => void;
  priorityIntervals: PriorityInterval[];
  priorityTieMove?: PriorityTieMoveState;
  rateWritesEnabled: boolean;
}) {
  const busy = Boolean(busyAction) || globallyDisabled;
  const identityBlocked = Boolean(
    account.identity_rebind_required
    || account.upstream_identity_rebind_required,
  );
  const priorityIdentityBlocked = priorityIntervalAssignmentBlocked(account);
  const usesFallbackConnectionTest = !channel || account.availability_monitor_id == null;
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
  const enabled = account.remote_schedulable === true;
  const disabled = account.remote_schedulable === false;
  const effectiveStatus = enabled
    ? account.remote_status || "enabled"
    : disabled
      ? "disabled"
      : "not_checked";
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
  const upstreamGroupMissing = ["deleted", "unassigned"].includes(
    String(account.upstream_group_status || "").trim().toLowerCase(),
  ) || account.group_multiplier_status === "group_deleted";
  const priorityPending = desiredPriority !== null && priority !== desiredPriority;
  const priorityState = upstreamGroupMissing
    ? "上游分组不存在"
    : multiplierUnavailable
    ? "等待综合倍率"
    : account.priority_sync_error
      ? "优先级同步失败"
      : !hasPriorityInterval
        ? "未选定区间"
        : priorityPending
          ? "等待写入"
          : priorityStatusLabel(account.priority_sync_status);
  const usageAmount = finiteNumber(account.today_upstream_usage_amount);
  const usageUnit = account.today_upstream_usage_unit;
  const usageCheckedAt = account.today_upstream_usage_checked_at;
  const usageIsCached = account.today_upstream_usage_status === "stale";
  const usageDetail = usageAmount === null
    ? upstreamStatusLabel(account.today_upstream_usage_status || "not_checked")
    : [
        usageIsCached ? "本轮未确认，显示上次成功结果" : null,
        account.today_upstream_usage_source ? sourceLabel(account.today_upstream_usage_source) : "上游余额消耗",
        usageCheckedAt ? formatDate(usageCheckedAt, displayTimeZone) : null,
      ].filter(Boolean).join(" · ");
  const accountUpstreamType = channel
    ? resolvedChannelType(channel)
    : account.resolved_upstream_type || account.detected_upstream_type || account.upstream_type;
  const showUsage = shouldShowUpstreamAccountUsage(accountUpstreamType);
  const activePauseHolds = accountActivePauseHolds(account);
  const hasAccountMeta = identityBlocked;
  return (
    <article className={"api-key-account-card" + (disabled ? " api-key-account-card--disabled" : "")}>
      <header className="api-key-account-card-head">
        <div className="api-key-account-title-line">
          <div className="api-key-account-name">
            <strong title={accountDisplayName(account)}>{accountDisplayName(account)}</strong>
            <span className="api-key-mono">#{account.sub2api_account_id}</span>
          </div>
          <div className="api-key-account-side-chips">
            <AccountStatusIndicator
              account={account}
              activePauseHolds={activePauseHolds}
              displayTimeZone={displayTimeZone}
              status={effectiveStatus}
            />
            <AccountAvailabilityIndicator
              account={account}
              activePauseHolds={activePauseHolds}
              channel={channel || undefined}
              channelMonitorFallbackTestModels={channelMonitorFallbackTestModels}
              displayTimeZone={displayTimeZone}
            />
            {account.remote_platform?.trim() ? <PlatformChip platform={account.remote_platform} /> : null}
          </div>
        </div>
        {hasAccountMeta ? <div className="api-key-inline-chips api-key-account-meta-chips">
          {identityBlocked ? (
            <span className="api-key-chip api-key-chip--warn">
              {account.identity_rebind_required
                ? account.identity_binding_status === "mismatch"
                  ? "身份已变化"
                  : "身份待认领"
                : "上游 Key 身份待确认"}
            </span>
          ) : null}
        </div> : null}
      </header>
      <div className="api-key-account-card-group">
        <div className="api-key-account-group-label">
          <span>上游分组</span>
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
          <span>今日使用</span>
          <strong>{formatUpstreamBalance(usageAmount, usageUnit, 2)}</strong>
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
      <AccountCardConfigurationTags
        account={account}
        channel={channel || undefined}
        channelMonitorFallbackTestModels={channelMonitorFallbackTestModels}
      />
      <footer className="api-key-account-card-actions">
        {priorityTieMove ? (
          <span className="api-key-priority-tie-controls" aria-label="同倍率账号优先级排位">
            <button
              aria-label={`提高 ${accountDisplayName(account)} 的优先级数值`}
              disabled={busy || !priorityTieMove.canMoveUp}
              onClick={() => onPriorityTieMove("up")}
              title="与后一个同区间、同综合倍率账号互换优先级"
              type="button"
            >
              <ArrowUp size={13} />
            </button>
            <button
              aria-label={`降低 ${accountDisplayName(account)} 的优先级数值`}
              disabled={busy || !priorityTieMove.canMoveDown}
              onClick={() => onPriorityTieMove("down")}
              title="与前一个同区间、同综合倍率账号互换优先级"
              type="button"
            >
              <ArrowDown size={13} />
            </button>
          </span>
        ) : null}
        <button
          aria-label={`测试 ${accountDisplayName(account)} 的可用性`}
          className="api-key-icon-button"
          disabled={busy || identityBlocked || !channel || account.availability_check_mode === "disabled"}
          onClick={onTestAvailability}
          title={account.availability_check_mode === "disabled"
            ? "请先为账号绑定监控面板或设置独立测试模型"
            : usesFallbackConnectionTest
              ? "未绑定可用性监控面板，将按设置中的回退模型链测试"
              : "按自动监测逻辑测试；正常账号使用暂停判定次数，因可用性监测暂停的账号使用恢复判定次数"}
          type="button"
        >
          {busyAction === "availability-test" ? <RefreshCcw className="spin" size={15} /> : <Activity size={15} />}
        </button>
        <button
          aria-label={`强制测试 ${accountDisplayName(account)} 的连接`}
          className="api-key-icon-button"
          disabled={busy || identityBlocked}
          onClick={onForceConnectionTest}
          title={usesFallbackConnectionTest
            ? "未绑定可用性监控面板，将使用账号白名单内的回退模型测试连接"
            : "直接调用 Sub2API 连接测试接口；不会读取监控面板，也不会改变自动暂停、恢复或调度状态"}
          type="button"
        >
          {busyAction === "connection-test" ? <RefreshCcw className="spin" size={15} /> : <PlugZap size={15} />}
        </button>
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
          disabled={["availability-test", "connection-test"].includes(busyAction || "") ? false : busy}
          onClick={onConfigure}
          title={["availability-test", "connection-test"].includes(busyAction || "") ? "检测进行中，可查看设置；保存会等待检测完成" : "配置账号"}
          type="button"
        >
          <Settings2 size={15} />
        </button>
        <button
          aria-label={(enabled ? "禁用 " : "启用 ") + accountDisplayName(account)}
          aria-pressed={disabled}
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

function AccountCardConfigurationTags({
  account,
  channel,
  channelMonitorFallbackTestModels,
}: {
  account: UpstreamAccount;
  channel?: UpstreamChannel;
  channelMonitorFallbackTestModels: string[];
}) {
  const priorityParticipation = account.priority_assignment_when_disabled_effective
    ? "停用后：参与优先级"
    : "停用后：不参与优先级";
  const ratePolicy = account.rate_pause_policy === "disabled"
    ? "关闭"
    : account.rate_pause_policy === "custom"
      ? "独立"
      : "跟随区间";
  // The API resolves an account override and its priority interval into these
  // effective fields.  Using the raw account policy here left inherited
  // thresholds blank even when the interval had enabled the pause rule.
  const rateThreshold = account.rate_pause_effective_enabled
    ? formatMultiplier(account.rate_absolute_threshold)
    : null;
  const mode = account.availability_check_mode || "disabled";
  const selectedMonitor = account.availability_monitor_id == null
    ? null
    : (channel?.channel_monitors || []).find(
        (monitor) => String(monitor.id) === String(account.availability_monitor_id),
      );
  const selectedMonitorStatus = selectedMonitor
    ? latestChannelMonitorStatus(selectedMonitor.primary_status, selectedMonitor.timeline)
    : "";
  const healthyMonitorStatuses = ["available", "healthy", "operational", "ok", "success"];
  const failedMonitorStatuses = ["unavailable", "error", "failed", "timeout", "invalid"];
  const monitorDeleted = account.availability_monitor_id != null
    && channel?.channel_monitor_status === "ok"
    && !selectedMonitor;
  const channelStatus = String(channel?.channel_monitor_status || "").trim().toLowerCase();
  const monitorDegraded = selectedMonitorStatus === "degraded" || channelStatus === "degraded";
  const monitorTone = account.availability_monitor_id == null
    ? "warn"
    : monitorDegraded
      ? "warn"
      : monitorDeleted || failedMonitorStatuses.includes(selectedMonitorStatus)
      || failedMonitorStatuses.includes(channelStatus)
      ? "danger"
      : selectedMonitor && channelStatus === "ok" && healthyMonitorStatuses.includes(selectedMonitorStatus)
        ? "success"
        : "warn";
  const fallbackModel = channelMonitorFallbackTestModels.find((candidate) =>
    (account.available_models || []).some((model) => model.id === candidate),
  );
  const configuredTestModel = account.availability_test_model?.trim() || fallbackModel;
  const availabilityTag = mode === "disabled"
    ? { label: "检测：关闭", tone: "muted", title: "API Key 可用性自动检测已关闭" }
    : mode === "independent_model"
      ? {
          label: `检测：独立模型 ${configuredTestModel || "未选择模型"}`,
          tone: configuredTestModel ? "info" : "warn",
          title: configuredTestModel
            ? `使用 ${configuredTestModel} 进行 API Key 连接测试`
            : "尚未从账号白名单中选出可用于连接测试的模型",
        }
      : {
          label: account.availability_monitor_id == null
            ? "检测：未绑定监控面板"
            : monitorDeleted
              ? `检测：面板 #${account.availability_monitor_id} 已删除`
              : `检测：面板 ${selectedMonitor?.name || `#${account.availability_monitor_id}`} · ${
                selectedMonitorStatus ? upstreamStatusLabel(selectedMonitorStatus) : "等待同步"
              }`,
          tone: monitorTone,
          title: account.availability_monitor_id == null
            ? "已选择绑定监控面板，但尚未选择具体面板"
            : monitorDeleted
              ? "原绑定的上游监控面板已被删除，请重新绑定"
              : selectedMonitorStatus
                ? `当前监控面板状态：${upstreamStatusLabel(selectedMonitorStatus)}`
                : "尚未获得绑定监控面板的当前状态",
        };
  const priorityTagLabel = account.priority_assignment_when_disabled_effective ? "参与" : "排除";
  const prioritySettingLabel = account.priority_assignment_when_disabled === true
    ? "此账号强制参与"
    : account.priority_assignment_when_disabled === false
      ? "此账号强制排除"
      : "继承全局设置";
  const rateSourceLabel = account.rate_pause_effective_source === "account"
    ? "账号的独立设置"
    : account.rate_pause_effective_source === "priority_interval"
      ? `优先级区间：${account.priority_interval_name || "未命名区间"}`
      : account.rate_pause_policy === "disabled"
        ? "账号明确关闭"
        : account.priority_interval_id == null
          ? "未分配优先级区间（默认不暂停）"
          : `优先级区间：${account.priority_interval_name || "未命名区间"} 已关闭`;
  const rateEffectiveLabel = account.rate_pause_effective_enabled
    ? "倍率上涨时会自动暂停"
    : "倍率上涨时不自动暂停";
  const monitorName = selectedMonitor?.name?.trim()
    || (account.availability_monitor_id == null ? null : `#${account.availability_monitor_id}`);
  const monitorStatusLabel = selectedMonitorStatus
    ? upstreamStatusLabel(selectedMonitorStatus)
    : monitorDeleted
      ? "已删除"
      : account.availability_monitor_id == null
        ? "未绑定"
        : "等待同步";
  const availabilityMethodLabel = mode === "disabled"
    ? "已关闭"
    : mode === "independent_model"
      ? "独立模型"
      : "绑定监控面板";
  const availabilityCompactLabel = mode === "disabled"
    ? "关闭"
    : mode === "independent_model"
      ? "独立模型"
      : account.availability_monitor_id == null
        ? "未绑面板"
        : monitorDeleted
          ? "面板已删"
          : `${middleEllipsis(monitorName || "监控面板", 12)} · ${monitorStatusLabel}`;
  return (
    <div className="api-key-account-config-tags" aria-label="账号自动化配置摘要">
      <span className="api-key-account-config-tag-wrap">
        <HelpPopover
          label="查看停用后优先级分配设置"
          trigger={
            <span className={
              "api-key-chip api-key-account-config-tag "
              + (account.priority_assignment_when_disabled_effective
                ? "api-key-account-config-tag--success"
                : "api-key-account-config-tag--muted")
            }>
              {priorityTagLabel}
            </span>
          }
          triggerClassName="help-popover-trigger--content api-key-account-config-popover"
        >
          <PopoverDetails rows={[
            ["账号停用后", priorityParticipation],
            ["配置来源", prioritySettingLabel],
          ]} />
        </HelpPopover>
      </span>
      <span className="api-key-account-config-tag-wrap">
        <HelpPopover
          label="查看倍率上涨暂停策略"
          trigger={
            <span
              className={
                "api-key-chip api-key-account-config-tag "
                + (ratePolicy === "关闭"
                  ? "api-key-account-config-tag--muted"
                  : ratePolicy === "独立"
                    ? "api-key-account-config-tag--info"
                    : "api-key-account-config-tag--inherit")
              }
            >
              {ratePolicy}
            </span>
          }
          triggerClassName="help-popover-trigger--content api-key-account-config-popover"
        >
          <PopoverDetails rows={[
            ["暂停策略", ratePolicy],
            ["倍率上涨时", rateEffectiveLabel],
            ["规则取自", rateSourceLabel],
            ["暂停阈值", account.rate_pause_effective_enabled ? rateThreshold : "未启用"],
          ]} />
        </HelpPopover>
      </span>
      <span className="api-key-account-config-tag-wrap api-key-account-config-tag-wrap--availability">
        <HelpPopover
          label="查看 API Key 可用性检测设置"
          trigger={
            <span
              className={`api-key-chip api-key-account-config-tag api-key-account-config-tag--${availabilityTag.tone}`}
            >
              {availabilityCompactLabel}
            </span>
          }
          triggerClassName="help-popover-trigger--content api-key-account-config-popover"
        >
          <PopoverDetails rows={[
            ["检测方式", availabilityMethodLabel],
            ["绑定监控面板", monitorName],
            ["面板当前状态", mode === "channel_monitor" ? monitorStatusLabel : null],
            ["测试模型", configuredTestModel || null],
            ["具体说明", availabilityTag.title],
          ]} />
        </HelpPopover>
      </span>
    </div>
  );
}

function AccountAvailabilityIndicator({
  account,
  activePauseHolds,
  channel,
  channelMonitorFallbackTestModels,
  displayTimeZone,
}: {
  account: UpstreamAccount;
  activePauseHolds: UpstreamAccountPauseHold[];
  channel?: UpstreamChannel;
  channelMonitorFallbackTestModels: string[];
  displayTimeZone: string;
}) {
  const status = String(account.availability_status || "").trim().toLowerCase();
  const mode = account.availability_check_mode || "disabled";
  const source = String(account.availability_source || "").trim().toLowerCase();
  const monitoringUnconfigured = mode === "disabled";
  const monitoringGloballyDisabled = !monitoringUnconfigured && status === "disabled";
  const selectedMonitor = account.availability_monitor_id == null
    ? null
    : (channel?.channel_monitors || []).find(
        (monitor) => String(monitor.id) === String(account.availability_monitor_id),
      );
  const selectedMonitorStatus = selectedMonitor
    ? latestChannelMonitorStatus(selectedMonitor.primary_status, selectedMonitor.timeline)
    : null;
  const channelMonitorStatus = String(channel?.channel_monitor_status || "").trim().toLowerCase();
  const monitorDegraded = mode === "channel_monitor"
    && (selectedMonitorStatus === "degraded" || channelMonitorStatus === "degraded");
  const available = status === "available" || monitorDegraded;
  const unavailable = status === "unavailable" && !monitorDegraded;
  const otherPauseReasons = activePauseHolds
    .filter((hold) => hold.reason !== "channel_monitor_unavailable")
    .map((hold) => upstreamChangeReasonLabel(hold.reason));
  const automaticMonitoringPaused = !monitoringUnconfigured && (
    otherPauseReasons.length > 0
    || status === "paused"
    || status === "automation_paused"
    || source === "policy_pause"
  );
  const sourceText = monitoringUnconfigured
    ? "尚未配置自动可用性监测"
    : monitorDegraded
      ? "绑定监控面板判定（降级，视为可用）"
    : source === "channel_monitor"
      ? "绑定监控面板判定"
      : source === "channel_monitor_fallback"
        ? "监控面板未能确认可用，随后由回退连接测试判定"
        : source === "independent_model"
          ? "独立模型连接测试判定"
          : source === "policy_pause"
            ? "保留暂停自动检测前的最近一次结果"
            : "尚未完成可用性判定";
  const statusText = monitoringUnconfigured
    ? "未配置"
    : available
      ? "可用"
      : unavailable
        ? "不可用"
        : account.availability_status || "未检测";
  const monitorBindingMissing = account.availability_check_mode === "channel_monitor"
    && account.availability_monitor_id == null;
  const monitorWasDeleted = account.availability_check_mode === "channel_monitor"
    && account.availability_monitor_id != null
    && channel?.channel_monitor_status === "ok"
    && !selectedMonitor;
  const monitorAvailable = mode === "channel_monitor"
    && channel?.channel_monitor_status === "ok"
    && [
    "available",
    "healthy",
    "operational",
    "ok",
    "success",
  ].includes(selectedMonitorStatus || "");
  const chosenModel = account.availability_test_model?.trim()
    || channelMonitorFallbackTestModels.find((model) =>
      (account.available_models || []).some((availableModel) => availableModel.id === model))
    || null;
  const fallbackChainHasNoAccountModel = mode !== "disabled"
    && !account.availability_test_model?.trim()
    && channelMonitorFallbackTestModels.length > 0
    && !chosenModel;
  const bindingText = account.availability_check_mode !== "channel_monitor"
    ? null
    : selectedMonitor?.name
      ? `${selectedMonitor.name}${selectedMonitor.id == null ? "" : ` (#${selectedMonitor.id})`}`
      : monitorWasDeleted
        ? `原绑定面板 #${account.availability_monitor_id} 已被上游删除，请重新绑定`
        : monitorBindingMissing
          ? "未绑定监控面板"
          : account.availability_monitor_id == null
            ? null
            : `面板 #${account.availability_monitor_id} 暂未同步到详情`;
  const monitorStatusText = mode === "independent_model"
    ? "不适用（独立模型测试）"
    : mode !== "channel_monitor"
      ? null
      : selectedMonitorStatus
        ? upstreamStatusLabel(selectedMonitorStatus)
        : monitorWasDeleted
          ? "原绑定面板已删除"
          : monitorBindingMissing
            ? "未绑定监控面板"
            : upstreamStatusLabel(channel?.channel_monitor_status || "unknown");
  const automaticMonitoringText = monitoringUnconfigured
    ? "未配置"
    : monitoringGloballyDisabled
      ? "全局自动检测已关闭"
    : automaticMonitoringPaused
      ? `已暂停；手动检测仍可用${otherPauseReasons.length ? `（${otherPauseReasons.join("、")}）` : ""}`
      : "运行中";
  const monitorUnbound = monitorBindingMissing;
  const monitorUnknown = mode === "channel_monitor"
    && !monitorWasDeleted
    && !monitorAvailable
    && ![
      "degraded",
      "unavailable",
      "error",
      "failed",
      "timeout",
      "invalid",
    ].includes(selectedMonitorStatus || "")
    && ![
      "degraded",
      "unavailable",
      "error",
      "failed",
      "timeout",
      "invalid",
    ].includes(String(channel?.channel_monitor_status || "").trim().toLowerCase());
  const indicatorTone = monitoringUnconfigured
    ? "unconfigured"
    : automaticMonitoringPaused
      ? "paused"
      : mode === "independent_model"
        ? "monitor-independent"
      : monitorUnbound
        ? "monitor-unbound"
        : monitorDegraded
          ? "monitor-degraded"
        : monitorUnknown
          ? "monitor-unknown"
        : monitorAvailable
          ? "monitor-available"
          : "monitor-unavailable";
  const trigger = indicatorTone === "unconfigured"
    ? <CircleOff size={13} />
    : (
      <span className={
        "api-key-availability-result"
        + (available ? " api-key-availability-result--available" : "")
        + (unavailable ? " api-key-availability-result--unavailable" : "")
      }>
        {available ? <CheckCircle2 size={11} /> : null}
        {unavailable ? <X size={11} /> : null}
      </span>
    );
  return (
    <HelpPopover
      label={`查看 ${accountDisplayName(account)} 的可用性监测详情`}
      trigger={trigger}
      triggerClassName={`api-key-availability-indicator api-key-availability-indicator--${indicatorTone}`}
    >
      <PopoverDetails
        rows={[
          ["自动检测", automaticMonitoringText],
          ["最近检测结果", statusText],
          ["检测方式", sourceText],
          ["监控面板", bindingText],
          ["监控面板状态", monitorStatusText],
          ["当前回退候选模型", source === "channel_monitor_fallback" && !monitorDegraded ? chosenModel : null],
          ["回退模型链", !monitorDegraded && fallbackChainHasNoAccountModel ? "没有属于该账号模型白名单的候选模型" : null],
          ["独立模型", source === "independent_model" ? chosenModel : null],
          ["最近检测", account.availability_checked_at ? formatDate(account.availability_checked_at, displayTimeZone) : null],
          ["具体说明", monitorDegraded
            ? "监控面板当前为降级，按可用处理，不进行回退模型测试。"
            : availabilityMessageText(account.availability_message)],
        ]}
      />
    </HelpPopover>
  );
}

function AccountStatusIndicator({
  account,
  activePauseHolds,
  displayTimeZone,
  status,
}: {
  account: UpstreamAccount;
  activePauseHolds: UpstreamAccountPauseHold[];
  displayTimeZone: string;
  status: string;
}) {
  const value = String(status || "unknown").trim().toLowerCase();
  if (value !== "disabled") return <StatusChip status={value} />;
  return (
    <HelpPopover
      label={`查看 ${accountDisplayName(account)} 的停用详情`}
      trigger={<span className="api-key-chip api-key-chip--danger">{statusLabel(value)}</span>}
      triggerClassName="help-popover-trigger--content"
    >
      <span className="api-key-status-detail">
        <strong>账号已停用</strong>
        {activePauseHolds.length ? activePauseHolds.map((hold, index) => (
          <span className="api-key-status-detail-reason" key={`${hold.reason}:${index}`}>
            <strong>{pauseHoldReasonLabel(hold)}</strong>
            <PopoverDetails rows={pauseHoldDetailRows(hold, displayTimeZone)} />
          </span>
        )) : (
          <span>未记录自动暂停原因，账号可能在 sub2api 中被手动停用。</span>
        )}
      </span>
    </HelpPopover>
  );
}

function PopoverDetails({ rows }: { rows: Array<[string, string | null]> }) {
  const visibleRows = rows.filter((row): row is [string, string] => Boolean(row[1]));
  return (
    <span className="api-key-popover-details">
      {visibleRows.map(([label, value]) => (
        <span className="api-key-popover-detail-row" key={label}>
          <span>{label}</span>
          <strong>{value}</strong>
        </span>
      ))}
    </span>
  );
}

function pauseHoldDetailRows(
  hold: UpstreamAccountPauseHold,
  displayTimeZone: string,
): Array<[string, string | null]> {
  const evidence = hold.evidence || {};
  const unit = evidence.unit || (evidence.basis === "recharge_adjusted" ? "CNY" : "USD");
  const basis = evidence.basis === "recharge_adjusted"
    ? "充值倍率折算余额"
    : evidence.basis === "wallet" || evidence.basis === "upstream_wallet"
      ? "上游钱包余额"
      : evidence.basis || null;
  return [
    ["当前余额", evidence.balance == null ? null : `${evidence.balance.toFixed(2)} ${unit}`],
    ["配置阈值", evidence.threshold == null ? null : `${evidence.threshold.toFixed(2)} ${unit}`],
    ["检测口径", basis],
    ["监控结果", evidence.monitor_status ? upstreamStatusLabel(String(evidence.monitor_status)) : null],
    ["Key 状态", evidence.key_status ? upstreamHealthStatusLabel("key", evidence.key_status) : null],
    ["分组状态", evidence.group_status ? upstreamHealthStatusLabel("group", evidence.group_status) : null],
    ["触发时间", hold.triggered_at ? formatDate(hold.triggered_at, displayTimeZone) : null],
    ["恢复条件", pauseHoldRecoveryLabel(hold.recovery_mode)],
  ];
}

function pauseHoldReasonLabel(hold: UpstreamAccountPauseHold) {
  const groupStatus = String(hold.evidence?.group_status || "").trim().toLowerCase();
  if (hold.reason === "upstream_group_unavailable" && ["deleted", "removed", "absent", "not_found"].includes(groupStatus)) {
    return "上游分组已删除";
  }
  return upstreamChangeReasonLabel(hold.reason);
}

function availabilityMessageText(message?: string | null) {
  const text = String(message || "").trim();
  if (!text) return null;
  const pausedMatch = text.match(/^Availability testing is paused by ([a-z_]+)\.$/);
  if (pausedMatch) {
    const reason = pausedMatch[1] === "manual_disabled"
      ? "账号已手动停用"
      : upstreamChangeReasonLabel(pausedMatch[1]);
    return `${reason}；为节省测试 token，暂不进行可用性测试。`;
  }
  return text
    .replace("No concrete upstream monitor panel is bound.", "未绑定具体监控面板。")
    .replace("The configured channel monitor no longer exists.", "原绑定监控面板已被上游删除。")
    .replace("The configured channel monitor reported an unavailable status.", "绑定监控面板报告不可用。")
    .replace("The configured channel monitor has no usable latest status.", "绑定监控面板没有可用的最新状态。")
    .replace("Fallback testing for unbound accounts is disabled.", "设置已禁止未绑定面板的账号使用回退测试。")
    .replace("No fallback test model chain is configured.", "未配置回退测试模型链。")
    .replace("None of the fallback test models are in this API Key account's available model whitelist.", "回退模型链中没有模型位于该账号白名单。")
    .replace(/Fallback connection test succeeded with model ([^ ]+) after (\d+) attempt\(s\)\./, "已使用回退模型 $1 完成连接测试，并在第 $2 次测试成功。");
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

function accountActivePauseHolds(account: UpstreamAccount): UpstreamAccountPauseHold[] {
  if (account.active_pause_holds !== undefined) return account.active_pause_holds;
  if (!account.auto_disabled_reason) return [];
  return [{
    reason: account.auto_disabled_reason,
    triggered_at: account.last_auto_disabled_at || account.balance_guard_paused_at,
    recovery_mode: account.balance_guard_restore_eligible ? "automatic" : "manual",
    scope_channel_id: account.balance_guard_channel_id ?? account.channel_id,
  }];
}

function pauseHoldRecoveryLabel(recoveryMode?: string | null) {
  const normalized = String(recoveryMode || "").trim().toLowerCase();
  if ([
    "automatic",
    "auto",
    "balance_positive",
    "balance_at_or_above_threshold",
    "channel_monitor_recovered",
    "monitor_recovered",
    "rate_normalized",
    "rate_within_threshold",
    "rate_at_or_below_absolute_threshold",
    "upstream_healthy",
    "account_availability_healthy",
  ].includes(normalized)) {
    return "满足条件后自动恢复";
  }
  if (normalized === "manual") return "需手动恢复";
  return recoveryMode ? `恢复方式：${recoveryMode}` : "恢复方式待确认";
}

function PriorityIntervalsView({
  accounts,
  busy,
  intervals,
  onCreate,
  onDelete,
  onEdit,
  onRebalance,
  onViewAccounts,
  rebalancing,
  shareSameCompositePriority,
}: {
  accounts: UpstreamAccount[];
  busy: boolean;
  intervals: PriorityInterval[];
  onCreate: () => void;
  onDelete: (interval: PriorityInterval) => void;
  onEdit: (interval: PriorityInterval) => void;
  onRebalance: () => void;
  onViewAccounts: (interval: PriorityInterval) => void;
  rebalancing: boolean;
  shareSameCompositePriority: boolean;
}) {
  const orderedIntervals = [...intervals].sort(
    (left, right) => left.start_priority - right.start_priority || String(left.id).localeCompare(String(right.id)),
  );
  return (
    <section className="api-key-panel api-key-priority-panel" aria-label="优先级区间">
      <div className="api-key-panel-head">
        <div>
          <h2>优先级区间</h2>
          <p>每个区间可独立使用低倍率优先或固定间隔方案；{shareSameCompositePriority
            ? "相同倍率账号共用一个优先级。"
            : "同倍率账号按手动排位分开。"}</p>
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
            const sortableGroupCount = shareSameCompositePriority
              ? new Set(
                assignedAccounts
                  .map((account) => accountCompositeMultiplier(account))
                  .filter((value): value is number => value !== null)
                  .map(priorityTieMultiplierKey),
              ).size
              : sortableCount;
            const waitingCount = assignedAccounts.length - sortableCount;
            const capacity = Math.max(0, interval.end_priority - interval.start_priority);
            const sharedPriorityCount = sortableGroupCount > capacity
              ? sortableGroupCount - capacity
              : 0;
            const effectiveStep = finiteNumber(interval.effective_step) ?? interval.step;
            return (
              <article className="api-key-priority-card" key={String(interval.id)}>
                <header>
                  <div>
                    <strong>{interval.name}</strong>
                    <span className="api-key-mono">[{interval.start_priority}, {interval.end_priority})</span>
                    <span>{interval.allocation_strategy === "fixed_step" ? "固定间隔" : "低倍率优先"}</span>
                  </div>
                  <div className="api-key-row-actions">
                    <button
                      aria-label={`查看优先级区间 ${interval.name} 的账号`}
                      className="api-key-icon-button"
                      onClick={() => onViewAccounts(interval)}
                      title="查看区间账号"
                      type="button"
                    >
                      <UsersRound size={15} />
                    </button>
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
                  {interval.allocation_strategy === "fixed_step" ? (
                    <>
                      <div><span>固定间隔</span><strong>{interval.step}</strong></div>
                      <div><span>实际间隔</span><strong>{effectiveStep}</strong></div>
                    </>
                  ) : null}
                  <div><span>已选账号</span><strong>{assignedAccounts.length}</strong></div>
                  <div>
                    <span>{shareSameCompositePriority ? "倍率档位" : "参与排序"}</span>
                    <strong>{sortableGroupCount}</strong>
                  </div>
                </div>
                <p className={"api-key-priority-note" + (interval.rate_pause_enabled ? "" : " is-waiting")}>
                  倍率上涨暂停：{interval.rate_pause_enabled
                    ? `开启 · 综合倍率大于 ${formatMultiplier(interval.rate_absolute_threshold)}`
                    : "关闭"}
                </p>
                {interval.allocation_strategy === "fixed_step" && effectiveStep < interval.step ? (
                  <p className="api-key-priority-note">区间空间有限，实际最低间隔已自动缩短为 {effectiveStep}。</p>
                ) : null}
                {sharedPriorityCount ? (
                  <p className="api-key-priority-note is-warning">区间容量不足，至少 {sharedPriorityCount} 个倍率档位会与相邻档位共用优先级。</p>
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
  channels,
  displayTimeZone,
  draftFilters,
  error,
  filtersApplied,
  hasMore,
  kind,
  loading,
  logs,
  onApplyFilters,
  onClearFilters,
  onDraftFiltersChange,
  onLoadMore,
  onRefresh,
}: {
  channels: UpstreamChannel[];
  displayTimeZone: string;
  draftFilters: RateLogFilters;
  error: string;
  filtersApplied: boolean;
  hasMore: boolean;
  kind: "upstream" | "account_rate";
  loading: boolean;
  logs: UpstreamChannelChangeEvent[];
  onApplyFilters: () => void;
  onClearFilters: () => void;
  onDraftFiltersChange: (filters: RateLogFilters) => void;
  onLoadMore: () => void;
  onRefresh: () => void;
}) {
  const accountRateView = kind === "account_rate";
  const rechargeMultiplierByChannel = useMemo(() => {
    const result = new Map<string, number>();
    for (const channel of channels) {
      const multiplier = finiteNumber(channel.effective_recharge_multiplier);
      if (multiplier !== null) result.set(String(channel.id), multiplier);
    }
    return result;
  }, [channels]);
  return (
    <section className="api-key-panel api-key-rate-log-panel" aria-label={accountRateView ? "API Key 账号倍率变化记录" : "上游分组变化记录"}>
      <div className="api-key-panel-head">
        <div>
          <h2>{accountRateView ? "API Key 账号倍率变化" : "上游分组变化"}</h2>
          {/* 记录上游充值倍率、分组倍率、名称与可用性，以及 API Key 账号实际倍率变化 */}
          <p>{accountRateView ? "只记录 API Key 账号实际计费倍率变化，并标注其对应上游。" : "记录上游分组的存在性、倍率、名称与可用性。"}</p>
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
          <span>{accountRateView ? "正在读取 API Key 倍率变化…" : "正在读取上游分组变化…"}</span>
        </div>
      ) : logs.length === 0 ? (
        <div className="api-key-empty">
          <History size={18} />
          <span>{filtersApplied
            ? accountRateView ? "当前日期范围内没有 API Key 倍率变化" : "当前日期范围内没有上游分组变化"
            : accountRateView ? "暂无 API Key 倍率变化记录" : "暂无上游分组变化记录"}</span>
        </div>
      ) : (
        <div className="api-key-rate-log-list">
          {logs.map((log) => {
            const multiplierEvent = log.event_type === "channel_multiplier_changed"
              || log.event_type === "group_multiplier_changed"
              || log.event_type === "account_rate_changed";
            const nameEvent = log.event_type === "group_name_changed";
            const keyStatusEvent = log.event_type === "upstream_key_status_changed";
            const groupStatusEvent = log.event_type === "upstream_group_status_changed";
            const groupAddedEvent = log.event_type === "group_added";
            const groupRate = upstreamGroupRatePresentation(
              log,
              rechargeMultiplierByChannel.get(String(log.channel_id)),
            );
            const oldName = typeof log.details?.old_name === "string" ? log.details.old_name : null;
            const newName = typeof log.details?.new_name === "string" ? log.details.new_name : null;
            const accountName = typeof log.details?.account_name === "string" ? log.details.account_name : null;
            const category = upstreamChangeCategory(log);
            const subjectLabel = category.tone === "account"
              ? "API Key 账号"
              : category.tone === "group"
                ? "上游分组"
                : "上游配置";
            const subjectName = category.tone === "account"
              ? accountName || log.group_name || `#${log.group_id || "-"}`
              : category.tone === "group"
                ? log.group_name || `#${log.group_id || "-"}`
                : log.event_type === "channel_multiplier_changed" ? "充值倍率" : "状态";
            const groupMultiplierValue = groupRate.newGroupMultiplier ?? groupRate.oldGroupMultiplier;
            const compositeMultiplierValue = groupRate.newCompositeMultiplier ?? groupRate.oldCompositeMultiplier;
            return (
              <article
                className={`api-key-rate-log-row api-key-change-event-row api-key-change-event-row--${category.tone}${log.unread ? " is-unread" : ""}`}
                key={log.id}
              >
                <div className="api-key-rate-log-identity">
                  <div className="api-key-change-identity-head">
                    <time className="api-key-ledger-time" dateTime={log.created_at}>{formatDate(log.created_at, displayTimeZone)}</time>
                    <div className={`api-key-change-category api-key-change-category--${category.tone}`}>{category.label}</div>
                    {log.unread ? <span className="api-key-unread-chip">未读</span> : null}
                  </div>
                  <div className="api-key-change-identity-route">
                    <span>上游 <b>{log.channel_name || "#" + (log.channel_id || "-")}</b></span>
                    <span>{subjectLabel} <b>{subjectName}</b></span>
                  </div>
                </div>
                <div className="api-key-rate-log-cell api-key-rate-log-cell--primary">
                  <div className="api-key-change-message-line api-key-change-message-line--primary">
                    <span>{upstreamChannelChangeEventLabel(log.event_type)}</span>
                  </div>
                  <div className="api-key-change-message-line api-key-change-message-line--detail">
                    {nameEvent ? (
                      <div className="api-key-rate-log-flow">
                        <b>{oldName || "未命名"}</b>
                        <ArrowRight size={13} />
                        <strong>{newName || "未命名"}</strong>
                      </div>
                    ) : (
                      multiplierEvent ? (
                        <div className="api-key-rate-log-flow">
                          <b>{formatRateLogMultiplier(log.old_value)}</b>
                          <ArrowRight size={13} />
                          <strong>{formatRateLogMultiplier(log.new_value)}</strong>
                        </div>
                      ) : keyStatusEvent || groupStatusEvent ? (
                        <div className="api-key-rate-log-flow">
                          <b>{upstreamHealthStatusLabel(keyStatusEvent ? "key" : "group", log.old_status)}</b>
                          <ArrowRight size={13} />
                          <strong>{upstreamHealthStatusLabel(keyStatusEvent ? "key" : "group", log.new_status)}</strong>
                        </div>
                      ) : (
                        <div className="api-key-rate-log-flow">
                          <b>{upstreamGroupEventStatusLabel(log.old_status, log.event_type)}</b>
                          <ArrowRight size={13} />
                          <strong>{upstreamGroupEventStatusLabel(log.new_status, log.event_type)}</strong>
                        </div>
                      )
                    )}
                    {(nameEvent || groupAddedEvent) && groupMultiplierValue !== null ? (
                      <div className="api-key-group-rate-detail" title={`分组倍率 ${formatRateLogMultiplier(groupMultiplierValue)}`}>
                        <span>分组倍率</span>
                        <strong>{formatRateLogMultiplier(groupMultiplierValue)}</strong>
                      </div>
                    ) : null}
                    {(
                      (nameEvent || groupAddedEvent || log.event_type === "group_multiplier_changed")
                      && groupRate.showCompositeMultiplier
                      && compositeMultiplierValue !== null
                    ) ? (
                      <div
                        className="api-key-group-rate-detail api-key-group-rate-detail--composite"
                        title={`综合倍率 ${formatRateLogMultiplier(compositeMultiplierValue)}`}
                      >
                        <span>综合倍率</span>
                        {log.event_type === "group_multiplier_changed" ? (
                          <div className="api-key-rate-log-flow">
                            <b>{formatRateLogMultiplier(groupRate.oldCompositeMultiplier)}</b>
                            <ArrowRight size={13} />
                            <strong>{formatRateLogMultiplier(groupRate.newCompositeMultiplier)}</strong>
                          </div>
                        ) : <strong>{formatRateLogMultiplier(compositeMultiplierValue)}</strong>}
                      </div>
                    ) : null}
                  </div>
                </div>
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

function SchedulingChangeLogView({
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
  logs: AccountSchedulingChangeEvent[];
  onApplyFilters: () => void;
  onClearFilters: () => void;
  onDraftFiltersChange: (filters: RateLogFilters) => void;
  onLoadMore: () => void;
  onRefresh: () => void;
}) {
  return (
    <section className="api-key-panel api-key-rate-log-panel api-key-scheduling-log-panel" aria-label="账号调度变化记录">
      <div className="api-key-ledger-sticky">
        <div className="api-key-panel-head">
        <div>
          <h2>账号调度变化</h2>
          <p>记录插件根据余额、渠道监控、上游可用性和综合倍率策略执行的暂停、恢复及失败。</p>
        </div>
        <button
          aria-label="刷新账号调度变化记录"
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
      </div>

      {error ? (
        <div className="api-key-rate-log-error" role="alert">
          <AlertTriangle size={15} />
          <span>{error}</span>
        </div>
      ) : null}

      {loading && logs.length === 0 ? (
        <div className="api-key-empty">
          <RefreshCcw className="spin" size={18} />
          <span>正在读取账号调度变化…</span>
        </div>
      ) : logs.length === 0 ? (
        <div className="api-key-empty">
          <Activity size={18} />
          <span>{filtersApplied ? "当前日期范围内没有账号调度变化" : "暂无账号调度变化记录"}</span>
        </div>
      ) : (
        <div className="api-key-rate-log-list">
          {logs.map((log) => {
            const evidenceLabel = schedulingEvidenceLabel(log);
            const statusTone = schedulingStatusTone(log.event_type);
            const reasonLabel = schedulingReasonLabel(log);
            return (
              <article
                className={`api-key-rate-log-row api-key-change-event-row api-key-scheduling-event-row api-key-scheduling-event-row--${statusTone}${log.unread ? " is-unread" : ""}`}
                key={log.id}
              >
                <div className="api-key-rate-log-identity">
                  <div className="api-key-change-identity-head">
                    <time className="api-key-ledger-time" dateTime={log.created_at}>{formatDate(log.created_at, displayTimeZone)}</time>
                    <div className={`api-key-change-category api-key-change-category--scheduling-${statusTone}`}>
                      {schedulingResultLabel(log.event_type)}
                    </div>
                    {log.unread ? <span className="api-key-unread-chip">未读</span> : null}
                  </div>
                  <div className="api-key-change-identity-route">
                    <span>上游 <b>{log.channel_name || "未分配"}</b></span>
                    <span>API Key 账号 <b>{log.account_name || `#${log.sub2api_account_id}`}</b></span>
                  </div>
                </div>
                <div className="api-key-rate-log-cell api-key-rate-log-cell--primary">
                  <div className="api-key-change-message-line api-key-change-message-line--primary">
                    <span>调度状态</span>
                    <div className="api-key-rate-log-flow">
                      <b>{schedulableLabel(log.old_schedulable)}</b>
                      <ArrowRight size={13} />
                      <strong>{schedulableLabel(log.new_schedulable)}</strong>
                    </div>
                  </div>
                  <div className="api-key-change-message-line api-key-change-message-line--detail">
                    <HelpPopover
                      label={`查看 ${log.account_name || `账号 #${log.sub2api_account_id}`} 的暂停原因详情`}
                      trigger={<span className="api-key-scheduling-reason-trigger">{reasonLabel}</span>}
                      triggerClassName="help-popover-trigger--content"
                    >
                      <span className="api-key-status-detail">
                        <strong>{reasonLabel}</strong>
                        <PopoverDetails rows={[
                          ["账号", log.account_name || `#${log.sub2api_account_id}`],
                          ["上游", log.channel_name || "未分配"],
                          ["调度结果", schedulingResultLabel(log.event_type)],
                          ["检测详情", evidenceLabel || null],
                          ["执行错误", log.safe_error || null],
                          ["记录时间", formatDate(log.created_at, displayTimeZone)],
                        ]} />
                      </span>
                    </HelpPopover>
                    {evidenceLabel ? <span className="api-key-scheduling-evidence" title={evidenceLabel}>{evidenceLabel}</span> : null}
                    {log.safe_error ? <span className="api-key-rate-log-safe-error" title={log.safe_error}>{log.safe_error}</span> : null}
                  </div>
                </div>
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

function upstreamChannelChangeEventLabel(eventType: UpstreamChannelChangeEvent["event_type"]) {
  return ({
    channel_multiplier_changed: "渠道充值倍率变化",
    group_multiplier_changed: "上游分组倍率变化",
    group_removed: "上游分组已删除",
    group_added: "上游分组已出现",
    group_name_changed: "上游分组名称变化",
    account_rate_changed: "API Key 账号计费倍率变化",
    upstream_key_status_changed: "上游 Key 状态变化",
    upstream_group_status_changed: "上游分组状态变化",
  } as const)[eventType];
}

function upstreamChangeCategory(log: UpstreamChannelChangeEvent) {
  if (log.event_type === "account_rate_changed") return { label: "API Key 账号", tone: "account" } as const;
  if (["group_multiplier_changed", "group_removed", "group_added", "group_name_changed", "upstream_group_status_changed"].includes(log.event_type)) {
    return { label: "上游分组", tone: "group" } as const;
  }
  return { label: "上游", tone: "channel" } as const;
}

function upstreamGroupEventStatusLabel(
  status?: string | null,
  eventType?: UpstreamChannelChangeEvent["event_type"],
) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "absent") return eventType === "group_added" ? "未出现" : "已删除";
  return ({ available: "可用", deleted: "已删除", removed: "已删除", unavailable: "不可用" } as Record<string, string>)[normalized]
    || "未确认";
}

function schedulingResultLabel(eventType: AccountSchedulingChangeEvent["event_type"]) {
  return ({
    paused: "已暂停",
    restored: "已恢复",
    pause_failed: "暂停执行失败",
    restore_failed: "恢复执行失败",
  } as const)[eventType];
}

function schedulingStatusTone(eventType: AccountSchedulingChangeEvent["event_type"]) {
  return eventType === "restored"
    ? "restored"
    : eventType === "paused"
      ? "paused"
      : "failed";
}

function schedulingReasonLabel(log: AccountSchedulingChangeEvent) {
  const groupStatus = String(log.evidence?.group_status || "").trim().toLowerCase();
  if (log.reason === "upstream_group_unavailable" && ["deleted", "removed", "absent", "not_found"].includes(groupStatus)) {
    return "上游分组已删除";
  }
  return upstreamChangeReasonLabel(log.reason);
}

function schedulableLabel(value?: boolean | null) {
  return value === true ? "已启用" : value === false ? "已暂停" : "未确认";
}

function schedulingEvidenceLabel(log: AccountSchedulingChangeEvent) {
  const evidence = log.evidence;
  if (!evidence) return "";
  const observedMultiplier = finiteNumber(evidence.observed_multiplier);
  if (log.reason === "upstream_balance_negative") {
    const balance = finiteNumber(evidence.balance);
    const threshold = finiteNumber(evidence.threshold);
    if (balance === null || threshold === null) return "";
    const basis = evidence.basis === "recharge_adjusted" ? "充值倍率后余额" : "上游钱包余额";
    const unit = String(evidence.unit || (evidence.basis === "recharge_adjusted" ? "CNY" : "USD"));
    return `${basis} ${formatSchedulingEvidenceNumber(balance)} ${unit}，阈值 ${formatSchedulingEvidenceNumber(threshold)} ${unit}`;
  }
  if (log.reason === "channel_monitor_unavailable") {
    const status = upstreamStatusLabel(String(evidence.monitor_status || "unknown"));
    const testStatus = evidence.test_status
      ? upstreamStatusLabel(String(evidence.test_status))
      : null;
    return testStatus
      ? `监控状态：${status}；账号连接测试：${testStatus}`
      : `监控状态：${status}`;
  }
  if (log.reason === "upstream_rate_increase" && observedMultiplier !== null) {
    const absoluteThreshold = finiteNumber(evidence.absolute_threshold);
    if (evidence.mode === "absolute_multiplier" || absoluteThreshold !== null) {
      return absoluteThreshold === null
        ? `观测综合倍率 ${formatRateLogMultiplier(observedMultiplier)}`
        : `观测综合倍率 ${formatRateLogMultiplier(observedMultiplier)}，阈值 ${formatRateLogMultiplier(absoluteThreshold)}`;
    }
    const baseline = finiteNumber(evidence.baseline_multiplier);
    const increase = finiteNumber(evidence.increase_percent);
    const threshold = finiteNumber(evidence.threshold_percent);
    const parts = [
      baseline === null ? "" : `触发基线 ${formatRateLogMultiplier(baseline)}`,
      `观测 ${formatRateLogMultiplier(observedMultiplier)}`,
      increase === null ? "" : `上涨 ${formatSchedulingEvidenceNumber(increase)}%`,
      threshold === null ? "" : `阈值 ${formatSchedulingEvidenceNumber(threshold)}%`,
    ].filter(Boolean);
    return parts.join("，");
  }
  if (log.reason === "upstream_key_unavailable" && evidence.key_status) {
    return `上游 Key 状态：${upstreamStatusLabel(String(evidence.key_status))}`;
  }
  if (log.reason === "upstream_group_unavailable" && evidence.group_status) {
    return `上游分组状态：${upstreamStatusLabel(String(evidence.group_status))}`;
  }
  return "";
}

function formatSchedulingEvidenceNumber(value: number) {
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
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
  className,
  icon,
  label,
  children,
}: {
  badge?: React.ReactNode;
  className?: string;
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className={"api-key-channel-stat" + (className ? ` ${className}` : "")}>
      <div className="api-key-channel-stat-label">{icon}<span>{label}</span>{badge}</div>
      <div className="api-key-channel-stat-value">{children}</div>
    </div>
  );
}

function ChannelAddressBox({ label, url }: { label: "站点" | "API"; url: string }) {
  const address = url.trim();
  const content = address ? displayFullUrl(address) : "";
  const className = "api-key-channel-address" + (content ? "" : " api-key-channel-address--empty");
  const children = <><b>{label}</b>{content ? <MiddleEllipsisText text={content} /> : null}</>;
  if (!isHttpUrl(address)) {
    return <span aria-label={`${label}地址未单独配置`} className={className} title={address || `${label}地址未单独配置`}>{children}</span>;
  }
  return (
    <a className={className} href={address} rel="noreferrer" target="_blank" title={`${label}地址：${address}`}>
      {children}
      <ExternalLink size={12} />
    </a>
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

function UpstreamBalanceSummary({ channels }: { channels: UpstreamChannel[] }) {
  return (
    <section aria-label="上游渠道余额" className="api-key-balance-summary">
      <div className="api-key-balance-summary-head">
        <span>上游渠道余额</span>
        <small>{channels.length} 个渠道</small>
      </div>
      <div className="api-key-balance-summary-list">
        {channels.length ? channels.map((channel) => (
          <UpstreamBalanceCard channel={channel} key={String(channel.id)} />
        )) : <span className="api-key-muted">暂无已分配渠道</span>}
      </div>
    </section>
  );
}

function UpstreamBalanceCard({ channel }: { channel: UpstreamChannel }) {
  const configuredName = channel.display_name?.trim() || "";
  const managementUrl = channel.management_base_url?.trim() || channelBaseUrl(channel);
  const displayName = configuredName || displayFullUrl(managementUrl) || "未配置地址";
  const isUrlLabel = urlLikeDisplayName(displayName, managementUrl)
    || urlLikeDisplayName(displayName, channelBaseUrl(channel));
  const platformBalance = formatCurrentPlatformBalance(channel);
  const adjustedBalance = formatCurrentRechargeAdjustedBalance(channel);
  const rechargeMultiplier = finiteNumber(channel.effective_recharge_multiplier);
  const platformBalanceNote = hasCurrentPlatformBalance(channel)
    ? `平台余额：${platformBalance}`
    : "平台余额：当前没有可信的上游余额";
  const adjustedBalanceNote = adjustedBalance === "—"
    ? "综合余额：当前无法按上游充值倍率计算"
    : `综合余额：平台余额 × 充值倍率${rechargeMultiplier === null ? "" : ` ${rechargeMultiplier.toLocaleString("zh-CN", { maximumFractionDigits: 6 })}`} = ${adjustedBalance}`;
  const todayUsage = formatBalanceSummaryTodayUsage(channel);
  const todayUsageNote = `今日消耗余额：原始 ${todayUsage.rawValue}；综合 ${todayUsage.adjustedValue}${todayUsage.stale ? "（显示当天最后一次有效值）" : ""}`;

  return (
    <article className="api-key-balance-channel-card">
      <BalanceManagementLink
        className="api-key-balance-channel-name api-key-balance-channel-name--link"
        isUrlLabel={isUrlLabel}
        label={displayName}
        url={managementUrl}
      />
      <div className="api-key-balance-values">
        <span aria-label={platformBalanceNote} className="api-key-balance-value" title={platformBalanceNote}>
          <small>原始</small><strong>{platformBalance}</strong>
        </span>
        <span aria-label={adjustedBalanceNote} className="api-key-balance-value api-key-balance-value--adjusted" title={adjustedBalanceNote}>
          <small>综合</small><strong>{adjustedBalance}</strong>
        </span>
      </div>
      <div className="api-key-balance-today-usage" title={todayUsageNote}>
        <small>今日消耗</small>
        <strong><span>原 {todayUsage.rawValue}</span><span>综 {todayUsage.adjustedValue}</span></strong>
      </div>
    </article>
  );
}

function BalanceManagementLink({
  className,
  isUrlLabel = false,
  label,
  url,
}: {
  className: string;
  isUrlLabel?: boolean;
  label: string;
  url: string;
}) {
  const visibleLabel = isUrlLabel ? <MiddleEllipsisText text={label} /> : <span>{label}</span>;
  if (!isHttpUrl(url)) return <span className={className} title="未配置有效的管理地址">{visibleLabel}</span>;
  return (
    <a className={className} href={url} rel="noreferrer" target="_blank" title={`打开管理地址：${url}`}>
      {visibleLabel}
      <ExternalLink size={12} />
    </a>
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

function DialogActions({
  onCancel,
  saving,
  savingLabel,
}: {
  onCancel: () => void;
  saving: boolean;
  savingLabel?: string;
}) {
  return (
    <div className="api-key-dialog-actions">
      <button className="api-key-button api-key-button--secondary" disabled={saving} onClick={onCancel} type="button">取消</button>
      <button className="api-key-button api-key-button--primary" disabled={saving} type="submit">
        <Save size={16} />
        <span>{saving ? savingLabel || "保存中" : "保存配置"}</span>
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

function mergeUpstreamAccountSnapshot(
  data: UpstreamChannelsResponse,
  snapshot: UpstreamAccount,
): UpstreamChannelsResponse {
  const snapshotKey = accountKey(snapshot);
  let matched = false;
  const mergeAccount = (account: UpstreamAccount) => {
    if (accountKey(account) !== snapshotKey) return account;
    matched = true;
    return { ...account, ...snapshot };
  };
  const channels = data.channels.map((channel) => ({
    ...channel,
    accounts: channel.accounts?.map(mergeAccount),
  }));
  const unassignedAccounts = data.unassigned_accounts.map(mergeAccount);
  return matched
    ? { ...data, channels, unassigned_accounts: unassignedAccounts }
    : data;
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
    ...(account.active_pause_holds || []).map((hold) => hold.reason),
    account.priority,
    account.desired_priority,
    account.priority_interval_name,
    account.priority_sync_status,
    account.priority_sync_error,
    account.last_error,
  ].filter(Boolean).join(" ").toLowerCase();
}

function matchesChannelStatus(channel: UpstreamChannel, filter: ChannelStatusFilter) {
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
  if (upstreamChannelTokenInvalid(channel)) return "token_invalid";
  if (channel.last_error) return "discovery_failed";
  return channel.status || channel.balance_status || channel.recharge_multiplier_status || "not_checked";
}

function resolvedChannelType(channel: UpstreamChannel) {
  if (channel.upstream_type && channel.upstream_type !== "auto") return channel.upstream_type;
  return channel.resolved_upstream_type || "auto";
}

function channelDisplayMessage(channel: UpstreamChannel) {
  if (upstreamChannelTokenInvalid(channel) || isGenericUpstreamChannelError(channel.balance_message || channel.message)) return "";
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
  if (upstreamChannelTokenInvalid(channel) || isGenericUpstreamChannelError(error)) return "";
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
  const step = form.allocationStrategy === "cost_optimized" ? 1 : Number(form.step);
  const rateAbsoluteThreshold = ratePauseThresholdPayload(form.rateAbsoluteThreshold);
  if (!Number.isSafeInteger(startPriority) || !Number.isSafeInteger(endPriority)) {
    throw new Error("优先级范围必须使用整数");
  }
  if (startPriority < 0) throw new Error("起始优先级不能小于 0");
  if (endPriority <= startPriority) throw new Error("结束优先级必须大于起始优先级");
  if (!Number.isSafeInteger(step) || step < 1) throw new Error("固定优先级间隔必须是大于 0 的整数");
  return {
    name,
    start_priority: startPriority,
    end_priority: endPriority,
    step,
    allocation_strategy: form.allocationStrategy,
    rate_pause_enabled: form.ratePauseEnabled,
    rate_absolute_threshold: rateAbsoluteThreshold,
  };
}

function ratePauseThresholdPayload(absoluteThreshold: string) {
  const absolute = Number(absoluteThreshold);
  if (!Number.isFinite(absolute) || absolute <= 0 || absolute > 1000) throw new Error("综合倍率阈值必须大于 0 且不超过 1000");
  return absolute;
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

function formatRechargeAdjustedBalance(adjustedBalance: unknown, balance: unknown, rechargeMultiplier: unknown) {
  const persisted = finiteNumber(adjustedBalance);
  if (persisted !== null) return `¥${persisted.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
  const amount = finiteNumber(balance);
  const multiplier = finiteNumber(rechargeMultiplier);
  if (amount === null || multiplier === null || multiplier <= 0) return "—";
  const adjusted = amount * multiplier;
  return Number.isFinite(adjusted) ? `¥${adjusted.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}` : "—";
}

function formatMonitorLatency(value: unknown) {
  const latency = finiteNumber(value);
  return latency === null ? "—" : `${Math.round(latency).toLocaleString("zh-CN")} ms`;
}

function formatMonitorAvailability(value: unknown) {
  const availability = finiteNumber(value);
  if (availability === null) return "—";
  const normalized = availability <= 1 ? availability * 100 : availability;
  return `${normalized.toLocaleString("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 2 })}%`;
}

function monitorCurrentProbe(monitor: UpstreamChannelMonitor) {
  const status = latestChannelMonitorStatus(monitor.primary_status, monitor.timeline);
  if (["available", "healthy", "operational", "ok", "success"].includes(status)) {
    return {
      label: "当前探测可用",
      tone: "success" as const,
    };
  }
  if (status === "degraded") {
    return { label: "当前探测可用（降级）", tone: "warn" as const };
  }
  if (["error", "failed", "timeout", "unavailable"].includes(status)) {
    return {
      label: "当前探测不可用",
      tone: "danger" as const,
    };
  }
  return { label: "当前探测待确认", tone: "muted" as const };
}

function channelMonitorMessage(channel: UpstreamChannel) {
  if (channel.channel_monitor_status === "not_configured") {
    return "上游未配置公开监控面板。";
  }
  if (channel.channel_monitor_status === "unsupported") {
    return "该上游暂不支持公开监控接口。";
  }
  if (channel.channel_monitor_status === "ok" && resolvedChannelType(channel) === "newapi") {
    const count = channel.channel_monitor_count ?? channel.channel_monitors?.length ?? 0;
    return `已读取 ${count} 个 NewAPI 公开监控项。`;
  }
  return channel.channel_monitor_message || "渠道状态由上游监控接口同步。";
}

function balanceDetail(channel: UpstreamChannel, timeZone: string) {
  const details: string[] = [];
  if (!hasCurrentPlatformBalance(channel)) {
    const previous = finiteNumber(channel.balance_remaining);
    if (
      previous !== null
      && channel.balance_checked_at
      && channel.balance_source === "upstream_wallet"
    ) {
      details.push(`上次成功余额 ${formatUpstreamBalance(previous, channel.balance_unit, 2)} · ${formatDate(channel.balance_checked_at, timeZone)}`);
    }
    if (channel.balance_source === "local_api_key") {
      details.push("仅取得本站 API Key 余额，未取得上游钱包余额");
    }
    return details.join(" · ");
  }
  if (finiteNumber(channel.balance_total) !== null) {
    details.push("总额 " + formatUpstreamBalance(channel.balance_total, channel.balance_unit, 2));
  }
  if (resolvedChannelType(channel) !== "newapi" && finiteNumber(channel.balance_used) !== null) {
    details.push("上游累计已用 " + formatUpstreamBalance(channel.balance_used, channel.balance_unit, 2));
  }
  return details.join(" · ");
}

function hasCurrentPlatformBalance(channel: UpstreamChannel) {
  const status = String(channel.balance_status || "").trim().toLowerCase();
  return ["ok", "success", "available"].includes(status)
    && channel.balance_source === "upstream_wallet"
    && finiteNumber(channel.balance_remaining) !== null
    && Boolean(channel.balance_checked_at);
}

function formatCurrentPlatformBalance(channel: UpstreamChannel) {
  return hasCurrentPlatformBalance(channel)
    ? formatUpstreamBalance(channel.balance_remaining, channel.balance_unit, 2)
    : "—";
}

function formatCurrentRechargeAdjustedBalance(channel: UpstreamChannel) {
  if (!hasCurrentPlatformBalance(channel)) return "—";
  return formatRechargeAdjustedBalance(
    channel.recharge_adjusted_balance,
    channel.balance_remaining,
    channel.effective_recharge_multiplier,
  );
}

function usageHistoryDefaultFilters(timeZone: string): UsageHistoryFilters {
  return { ...usageHistoryFiltersForPreset("thirty_days", timeZone), apiKeyAccountId: "" };
}

function usageHistoryFiltersForPreset(
  preset: UsageHistoryDatePreset,
  timeZone: string,
): Pick<UsageHistoryFilters, "startDate" | "endDate"> {
  const endDate = usageHistoryDateInTimeZone(timeZone);
  if (preset === "today") return { startDate: endDate, endDate };
  if (preset === "this_month") return { startDate: `${endDate.slice(0, 8)}01`, endDate };
  const days = preset === "seven_days" ? 7 : preset === "ninety_days" ? 90 : 30;
  return { startDate: usageHistoryShiftDate(endDate, -(days - 1)), endDate };
}

function usageHistoryDateInTimeZone(timeZone: string, now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    day: "2-digit",
    month: "2-digit",
    timeZone,
    year: "numeric",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function usageHistoryShiftDate(value: string, days: number) {
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day + days));
  return date.toISOString().slice(0, 10);
}

function usageHistoryDateRangeInvalid(filters: Pick<UsageHistoryFilters, "startDate" | "endDate">) {
  return Boolean(filters.startDate && filters.endDate && filters.startDate > filters.endDate);
}

function usageHistoryApiFilters(filters: UsageHistoryFilters, timeZone: string): UpstreamUsageHistoryFilters {
  return {
    startDate: filters.startDate || undefined,
    endDate: filters.endDate || undefined,
    apiKeyAccountId: filters.apiKeyAccountId || undefined,
    timeZone,
  };
}

function usageHistoryPresetActive(
  preset: UsageHistoryDatePreset,
  filters: Pick<UsageHistoryFilters, "startDate" | "endDate">,
  timeZone: string,
) {
  const range = usageHistoryFiltersForPreset(preset, timeZone);
  return filters.startDate === range.startDate && filters.endDate === range.endDate;
}

function normalizeUsageHistory(history: UpstreamUsageHistory): UpstreamUsageHistory {
  const zeroTotals = {
    balance_used: 0,
    balance_used_adjusted: 0,
    upstream_api_key_usage: 0,
    income: 0,
    cost: null,
    cost_adjusted: null,
  };
  return {
    ...history,
    api_key_accounts: Array.isArray(history.api_key_accounts) ? history.api_key_accounts : [],
    days: Array.isArray(history.days)
      ? history.days.map((day) => ({
          ...day,
          api_key_accounts: Array.isArray(day.api_key_accounts) ? day.api_key_accounts : [],
        }))
      : [],
    totals: history.totals || zeroTotals,
    lifetime_totals: history.lifetime_totals || history.totals || zeroTotals,
  };
}

function usageHistoryAccountOptions(history: UpstreamUsageHistory | null, channel: UpstreamChannel) {
  const options = history?.api_key_accounts || (channel.accounts || []).map((account) => ({
    sub2api_account_id: account.sub2api_account_id,
    account_name: account.remote_name || null,
    upstream_api_key_record_id: account.upstream_api_key_record_id,
  }));
  const unique = new Map<string, UpstreamUsageHistory["api_key_accounts"][number]>();
  for (const account of options) {
    const id = account?.sub2api_account_id;
    if (id === null || id === undefined || id === "") continue;
    unique.set(String(id), account);
  }
  return [...unique.values()];
}

function usageHistoryAccountLabel(account: UpstreamUsageHistory["api_key_accounts"][number]) {
  return account.account_name?.trim() || `账号 #${account.sub2api_account_id}`;
}

function usageHistoryDayAccount(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  if (!selectedAccountId) return null;
  return day.api_key_accounts.find((account) => String(account.sub2api_account_id) === selectedAccountId) || null;
}

function historyDayUsage(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  if (!selectedAccountId) return finiteNumber(day.balance_used);
  return finiteNumber(day.upstream_api_key_usage) ?? finiteNumber(usageHistoryDayAccount(day, selectedAccountId)?.upstream_usage);
}

function historyDayAdjustedUsage(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  if (!selectedAccountId) return finiteNumber(day.balance_used_adjusted);
  return finiteNumber(usageHistoryDayAccount(day, selectedAccountId)?.upstream_usage_adjusted);
}

function historyDayUsageUnit(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  if (!selectedAccountId) return day.balance_unit || null;
  return usageHistoryDayAccount(day, selectedAccountId)?.upstream_usage_unit || day.balance_unit || null;
}

function historyDayCost(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  return finiteNumber(day.cost) ?? historyDayUsage(day, selectedAccountId);
}

function historyDayAdjustedCost(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  return finiteNumber(day.cost_adjusted) ?? historyDayAdjustedUsage(day, selectedAccountId);
}

function historyDayCostUnit(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  return historyDayUsageUnit(day, selectedAccountId);
}

function historyCostUnit(days: UpstreamUsageHistory["days"], selectedAccountId: string | null) {
  return days.map((day) => historyDayCostUnit(day, selectedAccountId)).find(Boolean) || historyBalanceUnit(days);
}

function historyBalanceUnit(days: UpstreamUsageHistory["days"]) {
  return days.find((day) => day.balance_unit)?.balance_unit || null;
}

function historyIncomeUnit(days: UpstreamUsageHistory["days"]) {
  return days.find((day) => day.income_unit)?.income_unit || "CNY";
}

function historyNetIncome(value: Pick<UpstreamUsageHistory["totals"], "income" | "cost" | "cost_adjusted"> | null) {
  const income = finiteNumber(value?.income);
  // Income and raw upstream cost share the upstream currency; the adjusted cost is a separate recharge-basis amount.
  const cost = finiteNumber(value?.cost) ?? finiteNumber(value?.cost_adjusted);
  if (income === null && cost === null) return null;
  return (income || 0) - (cost || 0);
}

function formatHistoryAmount(value: unknown, unit?: string | null) {
  return finiteNumber(value) === null ? "—" : formatUpstreamBalance(value, unit || "CNY", 2);
}

function formatHistoryUsage(value: unknown, unit?: string | null) {
  return finiteNumber(value) === null ? "—" : formatUpstreamBalance(value, unit || undefined, 2);
}

function formatHistorySignedAmount(value: number | null, unit?: string | null) {
  if (value === null || !Number.isFinite(value)) return "—";
  const prefix = value > 0 ? "+" : value < 0 ? "−" : "";
  return prefix + formatHistoryAmount(Math.abs(value), unit);
}

function formatHistoryChartNumber(value: number) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toLocaleString("zh-CN", { maximumFractionDigits: 1 })}M`;
  if (value >= 1_000) return `${(value / 1_000).toLocaleString("zh-CN", { maximumFractionDigits: 1 })}k`;
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function usageHistoryLabelIndexes(length: number) {
  const indexes = new Set<number>();
  if (!length) return indexes;
  const labelCount = Math.min(length, 5);
  for (let index = 0; index < labelCount; index += 1) {
    indexes.add(Math.round((index / Math.max(1, labelCount - 1)) * (length - 1)));
  }
  return indexes;
}

function shortUsageHistoryDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value.slice(5).replace("-", "/") : value;
}

function formatUsageHistoryDate(value: string, timeZone: string) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  return formatDate(value, timeZone);
}

function formatBalanceSummaryTodayUsage(channel: UpstreamChannel) {
  const amount = channel.today_balance_used;
  const status = String(channel.today_balance_status || "not_checked").toLowerCase();
  const visible = finiteNumber(amount) !== null && ["ok", "stale", "stored"].includes(status);
  return {
    rawValue: visible
      ? formatUpstreamBalance(amount, channel.today_balance_unit || channel.balance_unit, 2)
      : "-",
    adjustedValue: visible
      ? formatRechargeAdjustedBalance(
          channel.today_recharge_adjusted_balance_used,
          amount,
          channel.effective_recharge_multiplier,
        )
      : "-",
    stale: status === "stale",
  };
}

function formatDailyBalanceUsed(
  channel: UpstreamChannel,
  period: "today" | "yesterday",
  timeZone: string,
) {
  const yesterday = period === "yesterday";
  const amount = yesterday ? channel.yesterday_balance_used : channel.today_balance_used;
  const adjustedAmount = yesterday ? null : channel.today_recharge_adjusted_balance_used;
  const unit = yesterday ? channel.yesterday_balance_unit : channel.today_balance_unit;
  const status = String(
    (yesterday ? channel.yesterday_balance_status : channel.today_balance_status) || "not_checked",
  ).toLowerCase();
  const checkedAt = yesterday
    ? channel.yesterday_balance_checked_at
    : channel.today_balance_checked_at;
  const hasCurrentValue = finiteNumber(amount) !== null && isToday(checkedAt, timeZone);
  const current = (status === "ok" || status === "stored")
    && hasCurrentValue;
  const stale = status === "stale" && hasCurrentValue;
  const unsupported = /^(?:credentials_missing|not_available|unsupported)$/.test(status);
  const visible = current || stale;
  const rawValue = visible
    ? formatUpstreamBalance(amount, unit || channel.balance_unit, 2)
    : "-";
  const adjustedValue = visible && !yesterday
    ? formatRechargeAdjustedBalance(adjustedAmount, amount, channel.effective_recharge_multiplier)
    : "-";
  return {
    label: yesterday ? "昨日" : "今日",
    stale,
    tone: current
      ? "success"
      : stale
        ? "warn"
        : unsupported
          ? "muted"
          : isFailureStatus(status)
            ? "danger"
            : "muted",
    rawValue,
    adjustedValue,
    value: `原 ${rawValue} · 综 ${adjustedValue}`,
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

function displayFullUrl(value?: string | null) {
  if (!value) return "";
  return value.trim().replace(/\/+$/, "");
}

function urlLikeDisplayName(label: string, url: string) {
  const normalized = label.trim();
  return isHttpUrl(normalized)
    || normalized === displayFullUrl(url)
    || normalized === displayHost(url);
}

function middleEllipsis(value: string, maxLength = 54) {
  if (value.length <= maxLength) return value;
  const marker = "...";
  const available = Math.max(2, maxLength - marker.length);
  const leading = Math.ceil(available / 2);
  const trailing = Math.floor(available / 2);
  return value.slice(0, leading) + marker + value.slice(-trailing);
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
    sub2api_daily_usage: "上游今日实际消耗",
    upstream_api_key_actual_cost: "上游今日实际消耗",
    local_sub2api_today_cost_converted: "本站今日用量换算",
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
