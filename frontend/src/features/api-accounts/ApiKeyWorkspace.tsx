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
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  ExternalLink,
  Eye,
  EyeOff,
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
  ZoomIn,
  ZoomOut,
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

import { api, upstreamLegacyBindingCounts } from "../../shared/api";
import { HelpPopover } from "../../HelpPopover";
import { MiddleEllipsisText } from "../../MiddleEllipsisText";
import {
  latestUpstreamMonitorStatus,
  recentUpstreamMonitorTimeline,
} from "../../upstreamMonitorPresentation";
import {
  CHANGE_LOG_READ_RETRY_DELAYS_MS,
  departedChangeLogSubview,
  pendingReadThroughId,
  visibleChangeLogUnreadCounts,
} from "../../changeLogReadState";
import {
  changeLogCacheKey,
  getChangeLogSessionStorage,
  markChangeLogCategoryCachesRead,
  markChangeLogCacheRead,
  readChangeLogCache,
  writeChangeLogCache,
} from "../../changeLogCache";
import {
  normalizeChangeLogPageSize,
  normalizeChangeLogPageSizeOptions,
} from "../../changeLogPageSize";
import {
  isGenericUpstreamError,
  partitionUpstreams,
  upstreamTokenInvalid,
} from "../../upstreamPresentation";
import {
  buildApiAccountUpdatePayload,
  canSetManualMultiplier,
  expectedIdentityFingerprint,
} from "../../apiAccountForm";
import { upstreamCredentialBindingChanged } from "../../upstreamCredentialBinding";
import {
  upstreamChangeReasonLabel,
  upstreamHealthStatusLabel,
  upstreamStatusLabel,
  upstreamStatusTone,
  type UpstreamHealthKind,
} from "../../upstreamLabels";
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
} from "../../upstreamRatePresentation";
import {
  apiAccountSyncMessage,
  apiAccountLegacyBindingConfirmationMessage,
  accountRateStatusLabel,
  upstreamDiscoveryErrorMessage,
  upstreamDiscoverySuccessMessage,
  upstreamDiscoveryCopy,
  upstreamMutationControlsDisabled,
} from "../../upstreamSyncPresentation";
import {
  accountCompositeMultiplier,
  filterApiAccountEntries,
  flattenApiAccounts,
  priorityIntervalAssignmentBlocked,
  priorityIntervalAssignmentNeedsConfirmation,
  priorityTieMultiplierKey,
  priorityTieMoveOptions,
  sortApiAccountEntries,
  sortApiAccountEntriesByName,
  upstreamAccountUpstreams,
  upstreamAccountPlatforms,
  upstreamAccountMatchesStatus,
  type ApiAccountEntry,
  type ApiAccountTieSort,
  type PriorityTieMoveState,
} from "../../upstreamPriorityPresentation";
import { upstreamOverviewHasLiveMutationData } from "../../upstreamOverviewCache";
import {
  formatUpstreamBalance,
  visibleUpstreamBalanceMessage,
} from "../../upstreamUsagePresentation";
import { routeFromPath, type ApiKeySubview } from "../../viewRouting";
import type {
  ApiKeyViewOperation,
  PriorityInterval,
  PriorityAllocationStrategy,
  PriorityIntervalInput,
  AccountSchedulingChangeEvent,
  ChangeLogUnreadCounts,
  ApiAccount,
  ApiAccountPauseHold,
  Upstream,
  UpstreamMonitor,
  UpstreamChangeEvent,
  UpstreamOverviewResponse,
  UpstreamUpdate,
  UpstreamType,
  UpstreamUsageHistory,
  UpstreamUsageHistoryFilters,
} from "../../domain";

type UpstreamStatusFilter = "all" | "pending" | "attention" | "undiscovered";
type AccountStatusFilter = UpstreamStatusFilter | "enabled" | "disabled";
type AccountTieSort = ApiAccountTieSort;
type UpstreamOccupancyFilter = "occupied" | "no_enabled" | "empty";
type RateLogFilters = { startDate: string; endDate: string };
type PriorityIntervalFilter = "all" | "unassigned" | string;
type PlatformFilter = "all" | "__unknown__" | string;
type AccountUpstreamFilter = "all" | "__unassigned__" | string;

const changeLogUnreadRefreshIntervalMs = 12_000;

type UpstreamForm = {
  displayName: string;
  baseUrl: string;
  managementBaseUrl: string;
  upstreamType: UpstreamType;
  probeEnabled: boolean;
  accessToken: string;
  clearAccessToken: boolean;
  refreshToken: string;
  clearRefreshToken: boolean;
  loginUsername: string;
  loginPassword: string;
  clearLoginCredentials: boolean;
  upstreamUserId: string;
  manualRechargeMultiplier: string;
};

type UpstreamCredentialField = "accessToken" | "refreshToken" | "loginUsername" | "loginPassword";
type UpstreamCredentialVisibility = Record<UpstreamCredentialField, boolean>;

const hiddenUpstreamCredentials: UpstreamCredentialVisibility = {
  accessToken: false,
  refreshToken: false,
  loginUsername: false,
  loginPassword: false,
};

type AccountForm = {
  upstreamId: string;
  apiKey: string;
  manualGroupMultiplier: string;
  remoteName: string;
  priorityAssignmentWhenDisabled: "inherit" | "enabled" | "disabled";
  ratePausePolicy: "inherit" | "disabled" | "custom";
  rateAbsoluteThreshold: string;
  availabilityCheckMode: "upstream_monitor" | "independent_model" | "disabled";
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
  accounts: ApiAccount[];
  upstream: Upstream | null;
  title: string;
};

type UsageHistoryFilters = {
  startDate: string;
  endDate: string;
  apiKeyAccountId: string;
};

type UsageHistoryDatePreset = "today" | "seven_days" | "thirty_days" | "ninety_days" | "this_month";

const emptyData: UpstreamOverviewResponse = {
  upstreams: [],
  unassigned_accounts: [],
};

const emptyUpstreamForm: UpstreamForm = {
  displayName: "",
  baseUrl: "",
  managementBaseUrl: "",
  upstreamType: "auto",
  probeEnabled: true,
  accessToken: "",
  clearAccessToken: false,
  refreshToken: "",
  clearRefreshToken: false,
  loginUsername: "",
  loginPassword: "",
  clearLoginCredentials: false,
  upstreamUserId: "",
  manualRechargeMultiplier: "",
};

const emptyAccountForm: AccountForm = {
  upstreamId: "",
  apiKey: "",
  manualGroupMultiplier: "",
  remoteName: "",
  priorityAssignmentWhenDisabled: "inherit",
  ratePausePolicy: "inherit",
  rateAbsoluteThreshold: "1",
  availabilityCheckMode: "upstream_monitor",
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
  changeLogPageSize,
  changeLogPageSizeOptions,
  upstreamMonitorFallbackTestModels,
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
  cachedData: UpstreamOverviewResponse | null;
  changeLogPageSize: number;
  changeLogPageSizeOptions: number[];
  upstreamMonitorFallbackTestModels: string[];
  displayTimeZone: string;
  globallyBusy: boolean;
  onCacheChange: (data: UpstreamOverviewResponse, baseUrl: string) => void;
  onNotice: (message: string) => void;
  onOperationStart: (operation?: ApiKeyViewOperation) => () => void;
  onSubviewChange: (subview: ApiKeySubview) => void;
  rateWritesEnabled: boolean;
  refreshVersion: number;
  shareSameCompositePriority: boolean;
  subview: ApiKeySubview;
}) {
  const availableChangeLogPageSizes = useMemo(
    () => normalizeChangeLogPageSizeOptions(changeLogPageSizeOptions),
    [changeLogPageSizeOptions],
  );
  const changeLogPageSizeOptionsKey = availableChangeLogPageSizes.join(",");
  const [data, setData] = useState<UpstreamOverviewResponse>(cachedData || emptyData);
  const [loading, setLoading] = useState(!cachedData);
  const [refreshing, setRefreshing] = useState(Boolean(cachedData));
  const [bulkDiscovering, setBulkDiscovering] = useState(false);
  const [busyUpstreams, setBusyUpstreams] = useState<Record<string, string>>({});
  const [busyAccounts, setBusyAccounts] = useState<Record<string, string>>({});
  const [upstreamSearch, setUpstreamSearch] = useState("");
  const [upstreamStatusFilter, setUpstreamStatusFilter] = useState<UpstreamStatusFilter>("all");
  const [upstreamOccupancyFilter, setUpstreamOccupancyFilter] = useState<UpstreamOccupancyFilter>("occupied");
  const [accountSearch, setAccountSearch] = useState("");
  const [accountTieSort, setAccountTieSort] = useState<AccountTieSort>("name");
  const [accountStatusFilter, setAccountStatusFilter] = useState<AccountStatusFilter>("all");
  const [accountUpstreamFilter, setAccountUpstreamFilter] = useState<AccountUpstreamFilter>("all");
  const [priorityIntervalFilter, setPriorityIntervalFilter] = useState<PriorityIntervalFilter>("all");
  const [platformFilter, setPlatformFilter] = useState<PlatformFilter>("all");
  const [error, setError] = useState("");
  const [rateLogs, setRateLogs] = useState<UpstreamChangeEvent[]>([]);
  const [rateLogsLoaded, setRateLogsLoaded] = useState(false);
  const [rateLogsLoading, setRateLogsLoading] = useState(false);
  const [rateLogsError, setRateLogsError] = useState("");
  const [rateLogPage, setRateLogPage] = useState(1);
  const [rateLogPageSize, setRateLogPageSize] = useState(() => normalizeChangeLogPageSize(changeLogPageSize, availableChangeLogPageSizes));
  const [rateLogTotalCount, setRateLogTotalCount] = useState(0);
  const [rateLogDraftFilters, setRateLogDraftFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [rateLogFilters, setRateLogFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [scheduleLogs, setScheduleLogs] = useState<AccountSchedulingChangeEvent[]>([]);
  const [scheduleLogsLoaded, setScheduleLogsLoaded] = useState(false);
  const [scheduleLogsLoading, setScheduleLogsLoading] = useState(false);
  const [scheduleLogsError, setScheduleLogsError] = useState("");
  const [scheduleLogPage, setScheduleLogPage] = useState(1);
  const [scheduleLogPageSize, setScheduleLogPageSize] = useState(() => normalizeChangeLogPageSize(changeLogPageSize, availableChangeLogPageSizes));
  const [scheduleLogTotalCount, setScheduleLogTotalCount] = useState(0);
  const [scheduleLogDraftFilters, setScheduleLogDraftFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [scheduleLogFilters, setScheduleLogFilters] = useState<RateLogFilters>({ startDate: "", endDate: "" });
  const [changeLogUnreadCounts, setChangeLogUnreadCounts] = useState<ChangeLogUnreadCounts>({
    upstream_changes: 0,
    account_rate_changes: 0,
    account_scheduling_changes: 0,
  });
  const visibleUnreadCounts = visibleChangeLogUnreadCounts(changeLogUnreadCounts, subview);
  const [editingUpstream, setEditingUpstream] = useState<Upstream | null>(null);
  const [upstreamForm, setUpstreamForm] = useState<UpstreamForm>(emptyUpstreamForm);
  const [upstreamCredentialVisibility, setUpstreamCredentialVisibility] = useState<UpstreamCredentialVisibility>(hiddenUpstreamCredentials);
  const [loadingUpstreamCredential, setLoadingUpstreamCredential] = useState<UpstreamCredentialField | null>(null);
  const [editingAccount, setEditingAccount] = useState<ApiAccount | null>(null);
  const [accountForm, setAccountForm] = useState<AccountForm>(emptyAccountForm);
  const [priorityIntervalDialogOpen, setPriorityIntervalDialogOpen] = useState(false);
  const [editingPriorityInterval, setEditingPriorityInterval] = useState<PriorityInterval | null>(null);
  const [priorityIntervalForm, setPriorityIntervalForm] = useState<PriorityIntervalForm>(emptyPriorityIntervalForm);
  const [priorityIntervalsBusy, setPriorityIntervalsBusy] = useState(false);
  const [accountCollectionDialog, setAccountCollectionDialog] = useState<AccountCollectionDialog | null>(null);
  const [accountUpstreamDialog, setAccountUpstreamDialog] = useState<Upstream | null>(null);
  const [upstreamGroupDialog, setUpstreamGroupDialog] = useState<Upstream | null>(null);
  const [upstreamMonitorDialog, setUpstreamMonitorDialog] = useState<Upstream | null>(null);
  const [upstreamUsageHistoryDialog, setUpstreamUsageHistoryDialog] = useState<Upstream | null>(null);
  const [upstreamUsageHistory, setUpstreamUsageHistory] = useState<UpstreamUsageHistory | null>(null);
  const [upstreamUsageHistoryLoading, setUpstreamUsageHistoryLoading] = useState(false);
  const [upstreamUsageHistoryError, setUpstreamUsageHistoryError] = useState("");
  const [upstreamUsageHistoryDraftFilters, setUpstreamUsageHistoryDraftFilters] = useState<UsageHistoryFilters>(() => (
    usageHistoryDefaultFilters(displayTimeZone)
  ));
  const [upstreamUsageHistoryFilters, setUpstreamUsageHistoryFilters] = useState<UsageHistoryFilters>(() => (
    usageHistoryDefaultFilters(displayTimeZone)
  ));
  const [upstreamMonitorLoading, setUpstreamMonitorLoading] = useState(false);
  const [upstreamMonitorError, setUpstreamMonitorError] = useState("");
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
  const upstreamCredentialRequestSequence = useRef(0);
  const upstreamUsageHistoryRequestSequence = useRef(0);
  const unreadCountsRequestSequence = useRef(0);
  const rateLogsRef = useRef<UpstreamChangeEvent[]>([]);
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
  const dataRef = useRef<UpstreamOverviewResponse>(cachedData || emptyData);
  const availabilityTestPromisesRef = useRef<Map<string, Promise<ApiAccount | null>>>(new Map());
  const connectionTestPromisesRef = useRef<Map<string, Promise<void>>>(new Map());
  const refreshVersionRef = useRef(refreshVersion);
  const dialogRef = useRef<HTMLElement | null>(null);
  const lastFocusedElementRef = useRef<HTMLElement | null>(null);
  const commitData = useCallback((nextData: UpstreamOverviewResponse) => {
    dataRef.current = nextData;
    setData(nextData);
    setAccountCollectionDialog((current) => {
      if (!current) return current;
      const upstreamId = current.upstream?.upstream_id;
      if (upstreamId == null) {
        return { ...current, accounts: nextData.unassigned_accounts };
      }
      const refreshedUpstream = nextData.upstreams.find(
        (upstream) => String(upstream.upstream_id) === String(upstreamId),
      );
      return refreshedUpstream
        ? {
            ...current,
            accounts: refreshedUpstream.accounts || [],
            upstream: refreshedUpstream,
            title: upstreamDisplayName(refreshedUpstream),
          }
        : current;
    });
    const refreshDialogUpstream = (
      current: Upstream | null,
    ): Upstream | null => {
      if (!current) return current;
      return nextData.upstreams.find(
        (upstream) => String(upstream.upstream_id) === String(current.upstream_id),
      ) || current;
    };
    setAccountUpstreamDialog(refreshDialogUpstream);
    setUpstreamGroupDialog(refreshDialogUpstream);
    setUpstreamMonitorDialog(refreshDialogUpstream);
    setUpstreamUsageHistoryDialog(refreshDialogUpstream);
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
      rateLogPage,
      rateLogPageSize,
    )
  ), [cacheBaseUrl, displayTimeZone, rateLogFilters.endDate, rateLogFilters.startDate, rateLogPage, rateLogPageSize]);
  const schedulingLogCacheKey = useCallback(() => (
    changeLogCacheKey(
      cacheBaseUrl,
      "scheduling",
      scheduleLogFilters.startDate,
      scheduleLogFilters.endDate,
      displayTimeZone,
      scheduleLogPage,
      scheduleLogPageSize,
    )
  ), [cacheBaseUrl, displayTimeZone, scheduleLogFilters.endDate, scheduleLogFilters.startDate, scheduleLogPage, scheduleLogPageSize]);
  const localMutationBusy = savingDialog
    || priorityIntervalsBusy
    || bulkDiscovering
    || Object.keys(busyUpstreams).length > 0
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
      const response = await api.upstreams();
      if (
        sequence !== requestSequence.current
        || activeCacheBaseUrlRef.current !== requestBaseUrl
      ) return null;
      const normalized = {
        ...response,
        upstreams: Array.isArray(response.upstreams) ? response.upstreams : [],
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
        setError(errorMessage(reason, "上游读取失败"));
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
      const stillPending = response.upstreams.some(
        (upstream) => upstream.background_discovery_pending === true,
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

  const loadRateLogs = useCallback(async () => {
    const logSubview = subview === "account-rate-log" ? "account-rate-log" : "rate-log";
    const category = logSubview === "account-rate-log" ? "account_rate" : "upstream";
    const cacheKey = rateLogCacheKey(category);
    rateLogCacheKeysRef.current[category] = cacheKey;
    const sequence = ++rateLogsRequestSequence.current;
    setRateLogsLoading(true);
    setRateLogsError("");
    try {
      const page = await api.upstreamChangeEvents(rateLogPageSize, null, {
        startDate: rateLogFilters.startDate || undefined,
        endDate: rateLogFilters.endDate || undefined,
        timeZone: displayTimeZone,
      }, category, rateLogPage);
      if (
        sequence !== rateLogsRequestSequence.current
        || !componentMountedRef.current
        || previousSubviewRef.current !== logSubview
        || (logSubview === "rate-log" && previousSubviewRef.current !== "rate-log")
      ) return;
      const totalPages = Math.max(1, Math.ceil(page.total_count / page.page_size));
      if (rateLogPage > totalPages) {
        setRateLogPage(totalPages);
        return;
      }
      const next = page.items;
      rateLogsRef.current = next;
      setRateLogs(next);
      setRateLogTotalCount(page.total_count);
      writeChangeLogCache(getChangeLogSessionStorage(), cacheKey, {
        items: next,
        hasMore: rateLogPage < totalPages,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
        totalCount: page.total_count,
        page: page.page,
        pageSize: page.page_size,
      });
      const pendingRef = category === "account_rate"
        ? pendingAccountRateLogReadThroughIdRef
        : pendingRateLogReadThroughIdRef;
      pendingRef.current = pendingReadThroughId(
        pendingRef.current,
        next,
      );
      setChangeLogUnreadCounts((current) => ({
        ...current,
        [category === "account_rate" ? "account_rate_changes" : "upstream_changes"]: page.unread_count,
      }));
      setRateLogsLoaded(true);
    } catch (reason) {
      if (sequence === rateLogsRequestSequence.current) {
        setRateLogsError(errorMessage(
          reason,
          category === "account_rate" ? "API 账号倍率变化记录读取失败" : "上游分组变化记录读取失败",
        ));
        setRateLogsLoaded(true);
      }
    } finally {
      if (sequence === rateLogsRequestSequence.current) setRateLogsLoading(false);
    }
  }, [displayTimeZone, rateLogCacheKey, rateLogFilters, rateLogPage, rateLogPageSize, subview]);

  const loadScheduleLogs = useCallback(async () => {
    const cacheKey = schedulingLogCacheKey();
    scheduleLogCacheKeyRef.current = cacheKey;
    const sequence = ++scheduleLogsRequestSequence.current;
    setScheduleLogsLoading(true);
    setScheduleLogsError("");
    try {
      const page = await api.accountSchedulingChangeEvents(scheduleLogPageSize, null, {
        startDate: scheduleLogFilters.startDate || undefined,
        endDate: scheduleLogFilters.endDate || undefined,
        timeZone: displayTimeZone,
      }, scheduleLogPage);
      if (
        sequence !== scheduleLogsRequestSequence.current
        || !componentMountedRef.current
        || previousSubviewRef.current !== "schedule-log"
      ) return;
      const totalPages = Math.max(1, Math.ceil(page.total_count / page.page_size));
      if (scheduleLogPage > totalPages) {
        setScheduleLogPage(totalPages);
        return;
      }
      const next = page.items;
      scheduleLogsRef.current = next;
      setScheduleLogs(next);
      setScheduleLogTotalCount(page.total_count);
      writeChangeLogCache(getChangeLogSessionStorage(), cacheKey, {
        items: next,
        hasMore: scheduleLogPage < totalPages,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
        totalCount: page.total_count,
        page: page.page,
        pageSize: page.page_size,
      });
      pendingScheduleLogReadThroughIdRef.current = pendingReadThroughId(
        pendingScheduleLogReadThroughIdRef.current,
        next,
      );
      setChangeLogUnreadCounts((current) => ({
        ...current,
        account_scheduling_changes: page.unread_count,
      }));
      setScheduleLogsLoaded(true);
    } catch (reason) {
      if (sequence === scheduleLogsRequestSequence.current) {
        setScheduleLogsError(errorMessage(reason, "账号调度变化读取失败"));
        setScheduleLogsLoaded(true);
      }
    } finally {
      if (sequence === scheduleLogsRequestSequence.current) setScheduleLogsLoading(false);
    }
  }, [displayTimeZone, scheduleLogFilters, scheduleLogPage, scheduleLogPageSize, schedulingLogCacheKey]);

  const warmDefaultChangeLogCaches = useCallback(async () => {
    const storage = getChangeLogSessionStorage();
    const filters = { timeZone: displayTimeZone };
    const defaultPageSize = normalizeChangeLogPageSize(changeLogPageSize, availableChangeLogPageSizes);
    const upstreamCacheKey = changeLogCacheKey(cacheBaseUrl, "upstream", "", "", displayTimeZone, 1, defaultPageSize);
    const accountRateCacheKey = changeLogCacheKey(cacheBaseUrl, "account_rate", "", "", displayTimeZone, 1, defaultPageSize);
    const schedulingCacheKey = changeLogCacheKey(cacheBaseUrl, "scheduling", "", "", displayTimeZone, 1, defaultPageSize);
    const warmRateCache = async (category: "upstream" | "account_rate", cacheKey: string) => {
      const page = await api.upstreamChangeEvents(defaultPageSize, null, filters, category, 1);
      writeChangeLogCache(storage, cacheKey, {
        items: page.items,
        hasMore: page.items.length < page.total_count,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
        totalCount: page.total_count,
        page: page.page,
        pageSize: page.page_size,
      });
    };
    const warmSchedulingCache = async () => {
      const page = await api.accountSchedulingChangeEvents(defaultPageSize, null, filters, 1);
      writeChangeLogCache(storage, schedulingCacheKey, {
        items: page.items,
        hasMore: page.items.length < page.total_count,
        unreadCount: page.unread_count,
        lastReadId: page.last_read_id,
        totalCount: page.total_count,
        page: page.page,
        pageSize: page.page_size,
      });
    };
    const tasks: Array<Promise<void>> = [];
    if (subview !== "rate-log") tasks.push(warmRateCache("upstream", upstreamCacheKey));
    if (subview !== "account-rate-log") tasks.push(warmRateCache("account_rate", accountRateCacheKey));
    if (subview !== "schedule-log") tasks.push(warmSchedulingCache());
    await Promise.allSettled(tasks);
  }, [availableChangeLogPageSizes, cacheBaseUrl, changeLogPageSize, displayTimeZone, subview]);

  const markRateLogsReadOnLeave = useCallback(async (
    category: "upstream" | "account_rate",
    updateLocalState = true,
  ) => {
    const logSubview = category === "account_rate" ? "account-rate-log" : "rate-log";
    const pendingRef = category === "account_rate"
      ? pendingAccountRateLogReadThroughIdRef
      : pendingRateLogReadThroughIdRef;
    for (let retryAttempt = 0; ; retryAttempt += 1) {
      const throughId = pendingRef.current;
      if (throughId === null) return;
      const cacheKey = rateLogCacheKeysRef.current[category];
      pendingRef.current = null;
      try {
        await api.markUpstreamChangesRead(throughId, category);
        const storage = getChangeLogSessionStorage();
        markChangeLogCategoryCachesRead(storage, cacheBaseUrl, category, throughId);
        if (cacheKey) markChangeLogCacheRead(storage, cacheKey, throughId);
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
  }, [cacheBaseUrl, refreshChangeLogUnreadCounts]);

  const markScheduleLogsReadOnLeave = useCallback(async (updateLocalState = true) => {
    for (let retryAttempt = 0; ; retryAttempt += 1) {
      const throughId = pendingScheduleLogReadThroughIdRef.current;
      if (throughId === null) return;
      const cacheKey = scheduleLogCacheKeyRef.current;
      pendingScheduleLogReadThroughIdRef.current = null;
      try {
        await api.markAccountSchedulingChangesRead(throughId);
        const storage = getChangeLogSessionStorage();
        markChangeLogCategoryCachesRead(storage, cacheBaseUrl, "scheduling", throughId);
        if (cacheKey) markChangeLogCacheRead(storage, cacheKey, throughId);
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
  }, [cacheBaseUrl, refreshChangeLogUnreadCounts]);

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
    const cached = readChangeLogCache<UpstreamChangeEvent>(
      getChangeLogSessionStorage(),
      cacheKey,
    );
    const items = cached?.items || [];
    rateLogsRef.current = items;
    setRateLogs(items);
    setRateLogTotalCount(cached?.totalCount || 0);
    setRateLogsLoaded(Boolean(cached));
    setRateLogsLoading(false);
    setRateLogsError("");
    const pendingRef = category === "account_rate"
      ? pendingAccountRateLogReadThroughIdRef
      : pendingRateLogReadThroughIdRef;
    pendingRef.current = pendingReadThroughId(pendingRef.current, items);
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
    setScheduleLogTotalCount(cached?.totalCount || 0);
    setScheduleLogsLoaded(Boolean(cached));
    setScheduleLogsLoading(false);
    setScheduleLogsError("");
    pendingScheduleLogReadThroughIdRef.current = pendingReadThroughId(
      pendingScheduleLogReadThroughIdRef.current,
      items,
    );
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
    setRateLogPage(1);
    setRateLogTotalCount(0);
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
    setRateLogPage(1);
    setRateLogTotalCount(0);
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
    setScheduleLogPage(1);
    setScheduleLogTotalCount(0);
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
    setScheduleLogPage(1);
    setScheduleLogTotalCount(0);
    setScheduleLogFilters(emptyFilters);
    setScheduleLogsLoaded(false);
  }, []);

  useEffect(() => {
    void refreshChangeLogUnreadCounts();
  }, [refreshChangeLogUnreadCounts, refreshVersion]);

  useEffect(() => {
    const nextPageSize = normalizeChangeLogPageSize(changeLogPageSize, availableChangeLogPageSizes);
    setRateLogPageSize(nextPageSize);
    setScheduleLogPageSize(nextPageSize);
    setRateLogPage(1);
    setScheduleLogPage(1);
  }, [availableChangeLogPageSizes, changeLogPageSize]);

  useEffect(() => {
    const scope = `${cacheBaseUrl}|${displayTimeZone}|${changeLogPageSize}|${changeLogPageSizeOptionsKey}`;
    if (warmedChangeLogCacheScopeRef.current === scope) return;
    warmedChangeLogCacheScopeRef.current = scope;
    void warmDefaultChangeLogCaches();
  }, [cacheBaseUrl, changeLogPageSize, changeLogPageSizeOptionsKey, displayTimeZone, warmDefaultChangeLogCaches]);

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
    setEditingUpstream(null);
    setEditingAccount(null);
    setPriorityIntervalDialogOpen(false);
    setEditingPriorityInterval(null);
    setPriorityIntervalForm(emptyPriorityIntervalForm);
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setUpstreamGroupDialog(null);
    setUpstreamMonitorDialog(null);
    upstreamUsageHistoryRequestSequence.current += 1;
    setUpstreamUsageHistoryDialog(null);
    setUpstreamUsageHistory(null);
    setUpstreamUsageHistoryLoading(false);
    setUpstreamUsageHistoryError("");
    setUpstreamMonitorLoading(false);
    setUpstreamMonitorError("");
    setUpstreamForm(emptyUpstreamForm);
    upstreamCredentialRequestSequence.current += 1;
    setUpstreamCredentialVisibility(hiddenUpstreamCredentials);
    setLoadingUpstreamCredential(null);
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
      !editingUpstream
      && !editingAccount
      && !priorityIntervalDialogOpen
      && !accountCollectionDialog
      && !accountUpstreamDialog
      && !upstreamGroupDialog
      && !upstreamMonitorDialog
      && !upstreamUsageHistoryDialog
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
    upstreamGroupDialog,
    upstreamMonitorDialog,
    upstreamUsageHistoryDialog,
    closeDialog,
    editingAccount,
    editingUpstream,
    priorityIntervalDialogOpen,
    savingDialog,
  ]);

  const allAccountEntries = useMemo(() => flattenApiAccounts(data), [data]);
  const allAccounts = useMemo(() => allAccountEntries.map(({ account }) => account), [allAccountEntries]);
  const priorityTieMoves = useMemo(
    () => shareSameCompositePriority ? new Map() : priorityTieMoveOptions(allAccounts),
    [allAccounts, shareSameCompositePriority],
  );
  const upstreamPartitions = useMemo(() => partitionUpstreams(data.upstreams), [data.upstreams]);
  const assignedUpstreams = upstreamPartitions.assigned;
  const occupiedUpstreams = upstreamPartitions.enabled;
  const noEnabledUpstreams = upstreamPartitions.noEnabled;
  const emptyUpstreams = upstreamPartitions.empty;
  const occupancyUpstreams = upstreamOccupancyFilter === "occupied"
    ? occupiedUpstreams
    : upstreamOccupancyFilter === "no_enabled"
      ? noEnabledUpstreams
      : emptyUpstreams;
  const priorityIntervals = data.priority_intervals || [];
  const upstreamOptions = useMemo(() => upstreamAccountUpstreams(allAccountEntries), [allAccountEntries]);
  const platformOptions = useMemo(() => upstreamAccountPlatforms(allAccountEntries), [allAccountEntries]);
  const filteredAccountEntries = useMemo(() => {
    const filtered = filterApiAccountEntries(allAccountEntries, {
      upstream: accountUpstreamFilter,
      interval: priorityIntervalFilter,
      platform: platformFilter,
      query: accountSearch,
    }).filter(({ account }) => upstreamAccountMatchesStatus(account, accountStatusFilter));
    return sortApiAccountEntries(filtered, accountTieSort);
  }, [accountSearch, accountStatusFilter, accountTieSort, accountUpstreamFilter, allAccountEntries, platformFilter, priorityIntervalFilter]);
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
      upstreams: assignedUpstreams.length,
      accounts: allAccounts.length,
      pending: allAccounts.filter((account) => account.would_change === true).length,
      readableBalances: assignedUpstreams.filter(hasCurrentPlatformBalance).length,
    }),
    [allAccounts, assignedUpstreams],
  );

  const filteredUpstreams = useMemo(() => {
    const query = upstreamSearch.trim().toLowerCase();
    return occupancyUpstreams
      .map((upstream) => {
        const upstreamMatches = !query || upstreamSearchText(upstream).includes(query);
        const accounts = (upstream.accounts || []).filter(
          (account) =>
            upstreamAccountMatchesStatus(account, upstreamStatusFilter) &&
            (upstreamMatches || accountSearchText(account).includes(query)),
        );
        const upstreamStatusMatches = matchesUpstreamStatus(upstream, upstreamStatusFilter);
        return { upstream, accounts, visible: (upstreamMatches && upstreamStatusMatches) || accounts.length > 0 };
      })
      .filter((entry) => entry.visible);
  }, [upstreamSearch, upstreamStatusFilter, occupancyUpstreams]);

  const filteredUnassigned = useMemo(() => {
    const query = upstreamSearch.trim().toLowerCase();
    if (upstreamOccupancyFilter !== "occupied") return [];
    if (upstreamStatusFilter !== "all" && upstreamStatusFilter !== "attention") return [];
    return data.unassigned_accounts.filter((account) => !query || accountSearchText(account).includes(query));
  }, [upstreamOccupancyFilter, upstreamSearch, upstreamStatusFilter, data.unassigned_accounts]);

  const setUpstreamBusy = (upstream: Upstream, action: string | null) => {
    const key = upstreamKey(upstream);
    setBusyUpstreams((current) => updateBusyMap(current, key, action));
  };

  const setAccountBusy = (account: ApiAccount, action: string | null) => {
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

  const openUpstreamConfig = (upstream: Upstream) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setUpstreamMonitorDialog(null);
    setEditingUpstream(upstream);
    setDialogError("");
    upstreamCredentialRequestSequence.current += 1;
    setUpstreamCredentialVisibility(hiddenUpstreamCredentials);
    setLoadingUpstreamCredential(null);
    setUpstreamForm({
      displayName: upstream.display_name || "",
      baseUrl: upstreamBaseUrl(upstream),
      managementBaseUrl: upstream.management_url || "",
      upstreamType: upstream.resolved_platform_type || upstream.platform_type || "auto",
      probeEnabled: upstream.probe_enabled !== false,
      accessToken: "",
      clearAccessToken: false,
      refreshToken: "",
      clearRefreshToken: false,
      loginUsername: "",
      loginPassword: "",
      clearLoginCredentials: false,
      upstreamUserId: upstream.upstream_user_id || "",
      manualRechargeMultiplier: numberInputValue(upstream.upstream_recharge_multiplier_override),
    });
  };

  const toggleUpstreamCredential = async (field: UpstreamCredentialField) => {
    if (!editingUpstream) return;
    if (upstreamCredentialVisibility[field]) {
      setUpstreamCredentialVisibility((current) => ({ ...current, [field]: false }));
      return;
    }

    const hasStoredValue = field === "accessToken"
      ? Boolean(editingUpstream.access_token_set)
      : field === "refreshToken"
        ? Boolean(editingUpstream.refresh_token_set)
        : Boolean(editingUpstream.login_credentials_set);
    const hasCurrentValue = field === "loginUsername" || field === "loginPassword"
      ? Boolean(upstreamForm.loginUsername || upstreamForm.loginPassword)
      : Boolean(upstreamForm[field]);
    if (!hasStoredValue || hasCurrentValue) {
      setUpstreamCredentialVisibility((current) => ({ ...current, [field]: true }));
      return;
    }

    const requestId = ++upstreamCredentialRequestSequence.current;
    setLoadingUpstreamCredential(field);
    setDialogError("");
    try {
      const credentials = await api.upstreamCredentials(editingUpstream.upstream_id);
      if (requestId !== upstreamCredentialRequestSequence.current) return;
      setUpstreamForm((current) => {
        if (field === "accessToken") {
          return { ...current, accessToken: current.accessToken || credentials.access_token || "" };
        }
        if (field === "refreshToken") {
          return { ...current, refreshToken: current.refreshToken || credentials.refresh_token || "" };
        }
        return {
          ...current,
          loginUsername: current.loginUsername || credentials.login_username || "",
          loginPassword: current.loginPassword || credentials.login_password || "",
        };
      });
      setUpstreamCredentialVisibility((current) => ({ ...current, [field]: true }));
    } catch (reason) {
      if (requestId === upstreamCredentialRequestSequence.current) {
        setDialogError(errorMessage(reason, "已保存凭据读取失败"));
      }
    } finally {
      if (requestId === upstreamCredentialRequestSequence.current) {
        setLoadingUpstreamCredential(null);
      }
    }
  };

  const openAccountConfig = (account: ApiAccount, fallbackUpstream?: Upstream) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setUpstreamMonitorDialog(null);
    setEditingAccount(account);
    setDialogError("");
    setAccountForm({
      upstreamId: String(account.upstream_id ?? fallbackUpstream?.upstream_id ?? ""),
      apiKey: "",
      manualGroupMultiplier: numberInputValue(account.upstream_group_multiplier_override),
      remoteName: account.remote_name || "",
      priorityAssignmentWhenDisabled: account.priority_assignment_when_disabled === true
        ? "enabled"
        : account.priority_assignment_when_disabled === false
          ? "disabled"
          : "inherit",
      ratePausePolicy: account.rate_pause_policy || "inherit",
      rateAbsoluteThreshold: numberInputValue(account.rate_absolute_threshold) || "1",
      availabilityCheckMode: account.availability_check_mode || "upstream_monitor",
      availabilityMonitorId: account.availability_monitor_id == null
        ? ""
        : String(account.availability_monitor_id),
      availabilityTestModel: account.availability_test_model || "",
    });
  };

  const openUpstreamAccounts = (upstream: Upstream) => {
    rememberDialogTrigger();
    setAccountUpstreamDialog(null);
    setAccountCollectionDialog({
      accounts: upstream.accounts || [],
      upstream,
      title: upstreamDisplayName(upstream),
    });
  };

  const openUpstreamMonitors = (upstream: Upstream) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setUpstreamMonitorError("");
    setUpstreamMonitorDialog(upstream);
  };

  const refreshUpstreamMonitors = async (upstream: Upstream) => {
    const finishOperation = onOperationStart();
    setUpstreamBusy(upstream, "monitors");
    setUpstreamMonitorLoading(true);
    setUpstreamMonitorError("");
    try {
      const response = await api.refreshUpstreamMonitors(upstream.upstream_id);
      const currentData = dataRef.current;
      const nextData = {
        ...currentData,
        upstreams: currentData.upstreams.map((item) => String(item.upstream_id) === String(response.upstream_id)
          ? {
              ...item,
              upstream_monitors: response.upstream_monitors,
              upstream_monitor_count: response.upstream_monitor_count,
              upstream_monitor_status: response.upstream_monitor_status,
              upstream_monitor_message: response.upstream_monitor_message,
              upstream_monitor_checked_at: response.upstream_monitor_checked_at,
            }
          : item),
      };
      commitData(nextData);
      onCacheChange(nextData, cacheBaseUrl);
      setUpstreamMonitorDialog((current) => current && String(current.upstream_id) === String(response.upstream_id)
        ? {
            ...current,
            upstream_monitors: response.upstream_monitors,
            upstream_monitor_count: response.upstream_monitor_count,
            upstream_monitor_status: response.upstream_monitor_status,
            upstream_monitor_message: response.upstream_monitor_message,
            upstream_monitor_checked_at: response.upstream_monitor_checked_at,
          }
        : current);
      setNotice("上游状态已更新。");
    } catch (reason) {
      setUpstreamMonitorError(errorMessage(reason, "上游状态读取失败"));
    } finally {
      setUpstreamMonitorLoading(false);
      setUpstreamBusy(upstream, null);
      finishOperation();
    }
  };

  const openUnassignedAccounts = () => {
    rememberDialogTrigger();
    setAccountUpstreamDialog(null);
    setAccountCollectionDialog({
      accounts: data.unassigned_accounts,
      upstream: null,
      title: "待分配账号",
    });
  };

  const openAccountUpstream = (entry: ApiAccountEntry) => {
    if (!entry.upstream) return;
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(entry.upstream);
  };

  const openUpstreamGroups = (upstream: Upstream) => {
    rememberDialogTrigger();
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setUpstreamMonitorDialog(null);
    setUpstreamGroupDialog(upstream);
  };

  const loadUpstreamUsageHistory = useCallback(async (
    upstream: Upstream,
    filters: UsageHistoryFilters,
  ) => {
    const requestSequence = ++upstreamUsageHistoryRequestSequence.current;
    if (usageHistoryDateRangeInvalid(filters)) {
      setUpstreamUsageHistoryLoading(false);
      setUpstreamUsageHistoryError("开始日期不能晚于结束日期");
      return;
    }
    setUpstreamUsageHistoryLoading(true);
    setUpstreamUsageHistoryError("");
    try {
      const response = await api.upstreamUsageHistory(upstream.upstream_id, usageHistoryApiFilters(filters, displayTimeZone));
      if (requestSequence !== upstreamUsageHistoryRequestSequence.current) return;
      setUpstreamUsageHistory(normalizeUsageHistory(response));
    } catch (reason) {
      if (requestSequence !== upstreamUsageHistoryRequestSequence.current) return;
      setUpstreamUsageHistoryError(errorMessage(reason, "上游历史用量读取失败"));
    } finally {
      if (requestSequence === upstreamUsageHistoryRequestSequence.current) {
        setUpstreamUsageHistoryLoading(false);
      }
    }
  }, [displayTimeZone]);

  const openUpstreamUsageHistory = (
    upstream: Upstream,
    apiKeyAccountId?: number | string | null,
  ) => {
    rememberDialogTrigger();
    upstreamUsageHistoryRequestSequence.current += 1;
    const filters = {
      ...usageHistoryDefaultFilters(displayTimeZone),
      apiKeyAccountId: apiKeyAccountId == null ? "" : String(apiKeyAccountId),
    };
    setAccountCollectionDialog(null);
    setAccountUpstreamDialog(null);
    setUpstreamGroupDialog(null);
    setUpstreamMonitorDialog(null);
    setUpstreamUsageHistory(null);
    setUpstreamUsageHistoryError("");
    setUpstreamUsageHistoryDraftFilters(filters);
    setUpstreamUsageHistoryFilters(filters);
    setUpstreamUsageHistoryDialog(upstream);
  };

  const applyUpstreamUsageHistoryFilters = () => {
    if (usageHistoryDateRangeInvalid(upstreamUsageHistoryDraftFilters)) {
      setUpstreamUsageHistoryError("开始日期不能晚于结束日期");
      return;
    }
    setUpstreamUsageHistoryError("");
    setUpstreamUsageHistoryFilters({ ...upstreamUsageHistoryDraftFilters });
  };

  const applyUpstreamUsageHistoryPreset = (preset: UsageHistoryDatePreset) => {
    const filters = {
      ...usageHistoryFiltersForPreset(preset, displayTimeZone),
      apiKeyAccountId: upstreamUsageHistoryDraftFilters.apiKeyAccountId,
    };
    setUpstreamUsageHistoryError("");
    setUpstreamUsageHistoryDraftFilters(filters);
    setUpstreamUsageHistoryFilters(filters);
  };

  useEffect(() => {
    if (!upstreamUsageHistoryDialog) return;
    void loadUpstreamUsageHistory(upstreamUsageHistoryDialog, upstreamUsageHistoryFilters);
  }, [upstreamUsageHistoryDialog, upstreamUsageHistoryFilters, loadUpstreamUsageHistory]);

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
    account: ApiAccount,
    priorityIntervalId: number | string | null,
  ) => {
    const confirmIdentityRebind = priorityIntervalAssignmentNeedsConfirmation(account);
    if (
      confirmIdentityRebind
      && !window.confirm(
        "这是升级前尚未绑定身份的本地配置。继续会先校验并认领当前 管理站点 API 账号，再分配优先级区间，是否确认？",
      )
    ) return;
    const finishOperation = onOperationStart();
    setAccountBusy(account, "priority");
    setError("");
    setNotice("");
    try {
      await api.setApiAccountPriorityInterval(account.management_account_id, {
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
    account: ApiAccount,
    direction: "up" | "down",
  ) => {
    const finishOperation = onOperationStart();
    setAccountBusy(account, "priority-order");
    setError("");
    setNotice("");
    try {
      await api.moveApiAccountPriority(account.management_account_id, {
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

  const saveUpstream = async (event: FormEvent) => {
    event.preventDefault();
    if (!editingUpstream) return;
    const finishOperation = onOperationStart();
    setSavingDialog(true);
    setDialogError("");
    try {
      const baseUrl = upstreamForm.baseUrl.trim();
      if (!baseUrl) throw new Error("请填写上游地址");
      assertHttpsUrl(baseUrl);
      const managementBaseUrl = upstreamForm.managementBaseUrl.trim();
      if (managementBaseUrl) assertHttpsUrl(managementBaseUrl);
      const credentialRebind = upstreamCredentialBindingChanged(
        editingUpstream,
        baseUrl,
        managementBaseUrl,
      );
      if (
        credentialRebind &&
        !window.confirm(
          "上游域名已改变。继续会把该上游及账号凭据重新绑定到新域名，是否确认？",
        )
      ) {
        return;
      }
      const payload: UpstreamUpdate = {
        display_name: nullableText(upstreamForm.displayName),
        api_endpoint_url: baseUrl,
        management_url: nullableText(managementBaseUrl),
        platform_type: upstreamForm.upstreamType,
        probe_enabled: upstreamForm.probeEnabled,
        upstream_user_id: nullableText(upstreamForm.upstreamUserId),
        clear_access_token: upstreamForm.clearAccessToken,
        confirm_credential_rebind: credentialRebind,
        upstream_recharge_multiplier_override: optionalPositiveNumber(
          upstreamForm.manualRechargeMultiplier,
          "手动充值倍率",
        ),
      };
      if (!upstreamForm.clearAccessToken && upstreamForm.accessToken.trim()) {
        payload.access_token = upstreamForm.accessToken.trim();
      }
      if (showUpstreamRefreshToken) {
        payload.clear_refresh_token = upstreamForm.clearRefreshToken;
        if (!upstreamForm.clearRefreshToken && upstreamForm.refreshToken.trim()) {
          payload.refresh_token = upstreamForm.refreshToken.trim();
        }
      }
      if (showUpstreamLoginCredentials) {
        payload.clear_login_credentials = upstreamForm.clearLoginCredentials;
        const loginUsername = upstreamForm.loginUsername.trim();
        const loginPassword = upstreamForm.loginPassword;
        if (!upstreamForm.clearLoginCredentials && (loginUsername || loginPassword)) {
          if (!loginUsername || !loginPassword) {
            throw new Error("登录账号和密码需要同时填写");
          }
          payload.login_username = loginUsername;
          payload.login_password = loginPassword;
        }
      }
      await api.updateUpstream(editingUpstream.upstream_id, payload);
      closeDialog();
      await loadData(true);
      setError("");
      setNotice("上游配置已保存；可立即同步余额、分组和账号倍率。");
    } catch (reason) {
      setDialogError(errorMessage(reason, "上游配置保存失败"));
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
      let testedAccount: ApiAccount | null = null;
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
      const latestAccount = flattenApiAccounts(latestData).find(
        ({ account }) => accountKey(account) === accountKey(submittedAccount),
      )?.account || testedAccount || submittedAccount;
      const selectedUpstream = latestData.upstreams.find((upstream) => String(upstream.upstream_id) === accountForm.upstreamId);
      const payload = buildApiAccountUpdatePayload({
        account: latestAccount,
        apiKey: accountForm.apiKey,
        upstreamId: selectedUpstream?.upstream_id ?? null,
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
      payload.availability_monitor_id = submittedAvailabilityMode === "upstream_monitor"
        && accountForm.availabilityMonitorId
        ? Number(accountForm.availabilityMonitorId)
        : null;
      payload.availability_test_model = submittedAvailabilityMode === "disabled"
        ? null
        : accountForm.availabilityTestModel.trim() || null;
      const previousOrigin = urlOrigin(latestAccount.api_endpoint_url);
      const nextOrigin = urlOrigin(selectedUpstream ? upstreamBaseUrl(selectedUpstream) : null);
      const credentialRebind = Boolean(
        previousOrigin && nextOrigin && previousOrigin !== nextOrigin,
      );
      if (
        credentialRebind &&
        !window.confirm(
          "账号将切换到不同的上游域名。继续会先更新管理站点 API 账号的上游地址，再把本地 API Key 配置绑定到新上游，是否确认？",
        )
      ) {
        return;
      }
      payload.confirm_credential_rebind = credentialRebind;
      if (latestAccount.identity_rebind_required) {
        const confirmed = window.confirm(
          latestAccount.identity_binding_status === "mismatch"
            ? "检测到 管理站点 API 账号 ID 对应的身份已经变化。继续会把保留的本地上游配置和凭据重新绑定到当前账号，是否确认？"
            : "这是升级前尚未绑定身份的本地配置。继续会把它认领到当前 管理站点 API 账号，是否确认？",
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
      await api.updateApiAccount(latestAccount.management_account_id, payload);
      closeDialog();
      await loadData(true);
      scheduleBackgroundAccountRefresh();
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogPage(1);
      setRateLogTotalCount(0);
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

  const discoverUpstream = async (upstream: Upstream) => {
    const finishOperation = onOperationStart({
      kind: "upstream-discovery",
      upstreamId: upstream.upstream_id,
    });
    setUpstreamBusy(upstream, "discover");
    setError("");
    setNotice("");
    try {
      await api.discoverUpstream(upstream.upstream_id);
      await loadData(true);
      await refreshChangeLogUnreadCounts();
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogPage(1);
      setRateLogTotalCount(0);
      setRateLogsLoaded(false);
      setNotice(upstreamDiscoverySuccessMessage(rateWritesEnabled, upstreamDisplayName(upstream)));
    } catch (reason) {
      setNotice(errorMessage(reason, upstreamDiscoveryErrorMessage(rateWritesEnabled, upstreamDisplayName(upstream))));
    } finally {
      setUpstreamBusy(upstream, null);
      finishOperation();
    }
  };

  const deleteUpstream = async (upstream: Upstream) => {
    if (upstreamAccountCount(upstream) > 0) return;
    if (!window.confirm(`确认删除空上游「${upstreamDisplayName(upstream)}」？`)) return;
    const finishOperation = onOperationStart();
    setUpstreamBusy(upstream, "delete");
    setError("");
    setNotice("");
    try {
      const result = await api.deleteUpstream(upstream.upstream_id);
      await loadData(true);
      setNotice(result.message || "空上游已删除。");
    } catch (reason) {
      setError(errorMessage(reason, "空上游删除失败"));
    } finally {
      setUpstreamBusy(upstream, null);
      finishOperation();
    }
  };

  const discoverAll = async () => {
    if (!assignedUpstreams.length) {
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
          upstreams: Array.isArray(result.overview.upstreams) ? result.overview.upstreams : [],
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
      setRateLogPage(1);
      setRateLogTotalCount(0);
      setRateLogsLoaded(false);
      setNotice(apiAccountSyncMessage(result, rateWritesEnabled));
    } catch (reason) {
      setError(errorMessage(reason, upstreamDiscoveryCopy(rateWritesEnabled).allError));
    } finally {
      setBulkDiscovering(false);
      finishOperation();
    }
  };

  const toggleAccountEnabled = async (account: ApiAccount) => {
    const finishOperation = onOperationStart();
    const currentlyEnabled = account.remote_schedulable === true;
    setAccountBusy(account, currentlyEnabled ? "disable" : "enable");
    setError("");
    setNotice("");
    try {
      await api.setApiAccountEnabled(
        account.management_account_id,
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

  const testAccountAvailability = (account: ApiAccount): Promise<ApiAccount | null> => {
    const key = accountKey(account);
    const existingTest = availabilityTestPromisesRef.current.get(key);
    if (existingTest) return existingTest;

    const testPromise = (async () => {
      const finishOperation = onOperationStart();
      setAccountBusy(account, "availability-test");
      setError("");
      setNotice("");
      try {
        const result = await api.testApiAccountAvailability(
          account.management_account_id,
          expectedIdentityFingerprint(account),
        );
        const dataWithTestResult = mergeApiAccountSnapshot(dataRef.current, result.account);
        if (dataWithTestResult !== dataRef.current) {
          commitData(dataWithTestResult);
          onCacheChange(dataWithTestResult, cacheBaseUrl);
        }
        const refreshedData = await loadData(true);
        const refreshedEntry = refreshedData
          ? flattenApiAccounts(refreshedData).find(
              (entry) => String(entry.account.management_account_id) === String(account.management_account_id),
            )
          : null;
        const refreshedAccount = refreshedEntry?.account || result.account;
        setAccountCollectionDialog((current) => current ? {
          ...current,
          accounts: current.accounts.map((item) =>
            String(item.management_account_id) === String(account.management_account_id)
              ? refreshedAccount
              : item),
          upstream: current.upstream
            ? refreshedEntry?.upstream || current.upstream
            : current.upstream,
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

  const forceAccountConnectionTest = (account: ApiAccount): Promise<void> => {
    const key = accountKey(account);
    const existingTest = connectionTestPromisesRef.current.get(key);
    if (existingTest) return existingTest;

    const testPromise = (async () => {
      const finishOperation = onOperationStart();
      setAccountBusy(account, "connection-test");
      setError("");
      setNotice("");
      try {
        const result = await api.testApiAccountConnection(
          account.management_account_id,
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

  const deleteRemoteAccount = async (account: ApiAccount) => {
    const confirmed = window.confirm(
      "确认从管理站点删除「" + accountDisplayName(account) + "」？\n\n管理站点账号 ID #" +
        account.management_account_id +
        " 及其本地上游配置会一并删除，此操作无法撤销。",
    );
    if (!confirmed) return;
    const finishOperation = onOperationStart();
    setAccountBusy(account, "delete");
    setError("");
    setNotice("");
    try {
      await api.deleteRemoteApiAccount(
        account.management_account_id,
        expectedIdentityFingerprint(account),
      );
      await loadData(true);
      rateLogsRequestSequence.current += 1;
      setRateLogs([]);
      setRateLogsLoading(false);
      setRateLogPage(1);
      setRateLogTotalCount(0);
      setRateLogsLoaded(false);
      setNotice("已从管理站点删除 " + accountDisplayName(account) + "。");
    } catch (reason) {
      setError(errorMessage(reason, "管理站点 API 账号删除失败"));
    } finally {
      setAccountBusy(account, null);
      finishOperation();
    }
  };

  const editingUpstreamType =
    upstreamForm.upstreamType === "auto"
      ? editingUpstream?.resolved_platform_type || "auto"
      : upstreamForm.upstreamType;
  const showUpstreamRefreshToken = editingUpstreamType === "sub2api";
  const showUpstreamLoginCredentials = editingUpstreamType !== "newapi";
  const anyBusy = globallyBusy || localMutationBusy;
  const discoveryCopy = upstreamDiscoveryCopy(rateWritesEnabled);
  const mutationControlsDisabled = upstreamMutationControlsDisabled({
    liveDataValidated,
    loading,
    refreshing,
  });
  const selectedUpstreamForAccountForm = data.upstreams.find(
    (upstream) => String(upstream.upstream_id) === accountForm.upstreamId,
  );
  const editingAccountModels = editingAccount?.available_models || [];
  const availabilityMonitoringDisabled = accountForm.availabilityCheckMode === "disabled";
  const configuredFallbackModel = accountForm.availabilityTestModel.trim()
    || upstreamMonitorFallbackTestModels.find(
      (model) => editingAccountModels.some((availableModel) => availableModel.id === model),
    )
    || upstreamMonitorFallbackTestModels[0]
    || "";
  const fallbackModelAllowed = Boolean(configuredFallbackModel)
    && editingAccountModels.some((model) => model.id === configuredFallbackModel);
  const availabilityTestModelBlocked = !configuredFallbackModel || !fallbackModelAllowed;
  const availabilityModelWarning = !availabilityMonitoringDisabled && availabilityTestModelBlocked
    ? configuredFallbackModel
      ? `${accountForm.availabilityCheckMode === "upstream_monitor" ? "回退" : "独立测试"}模型 ${configuredFallbackModel} 不在该账号白名单中，禁止执行测试。`
      : `${accountForm.availabilityCheckMode === "upstream_monitor" ? "回退" : "独立测试"}模型未配置或白名单尚未同步，禁止执行测试。`
    : "";

  return (
    <section className="api-key-view api-key-channel-view" aria-label="API 账号管理">
      <div className="api-key-subview-tabs" role="tablist" aria-label="API Key 子页面">
        <button
          aria-selected={subview === "upstreams"}
          className={subview === "upstreams" ? "active" : ""}
          onClick={() => onSubviewChange("upstreams")}
          role="tab"
          type="button"
        >
          <Globe2 size={16} />
          <span>上游</span>
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
      <div className="api-key-summary" aria-label="上游汇总">
        <SummaryItem label="上游" value={summary.upstreams} tone="blue" />
        <SummaryItem label="API 账号" value={summary.accounts} tone="green" />
        <SummaryItem label="倍率不一致" value={summary.pending} tone="amber" />
        <SummaryItem
          label="管理站点充值倍率"
          value={"¥" + formatCostPerUsd(data.management_recharge_multiplier) + " / $1"}
          tone="teal"
          detail={sourceLabel(data.management_recharge_source)}
        />
        <UpstreamBalanceSummary upstreams={assignedUpstreams} />
      </div>

      {error ? (
        <Feedback tone="error" onClose={() => setError("")}>{error}</Feedback>
      ) : null}
      {subview === "accounts" ? (
        <section className="api-key-panel api-key-accounts-panel" aria-label="API 账号">
          <div className="api-key-panel-head">
            <div>
              <h2>API 账号</h2>
              <p>按上游实际倍率从低到高排列；上游实际倍率不可用的账号显示在末尾。</p>
            </div>
          </div>

          <div className="api-key-filters api-key-account-filters">
            <label className="api-key-search">
              <Search size={16} />
              <span className="api-key-sr-only">搜索 API 账号</span>
              <input
                onChange={(event) => setAccountSearch(event.target.value)}
                placeholder="搜索账号、上游、ID 或分组"
                type="search"
                value={accountSearch}
              />
              <small>{filteredAccountEntries.length}/{allAccountEntries.length}</small>
            </label>
            <div className="api-key-account-tie-sort" role="group" aria-label="同倍率排序">
              <span>同倍率排序</span>
              <div className="api-key-segmented api-key-segmented--two">
                <button
                  aria-pressed={accountTieSort === "name"}
                  className={accountTieSort === "name" ? "active" : ""}
                  onClick={() => setAccountTieSort("name")}
                  type="button"
                >字母</button>
                <button
                  aria-pressed={accountTieSort === "priority"}
                  className={accountTieSort === "priority" ? "active" : ""}
                  onClick={() => setAccountTieSort("priority")}
                  type="button"
                >优先级</button>
              </div>
            </div>
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
                {upstreamOptions.upstreams.map((option) => (
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
              <span>正在读取 API 账号…</span>
            </div>
          ) : filteredAccountEntries.length ? (
            <div className="api-key-account-grid api-key-account-management-grid">
              {filteredAccountEntries.map((entry) => (
                <AccountCard
                  account={entry.account}
                  busyAction={busyAccounts[accountKey(entry.account)]}
                  upstream={entry.upstream}
                  upstreamMonitorFallbackTestModels={upstreamMonitorFallbackTestModels}
                  displayTimeZone={displayTimeZone}
                  globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                  key={accountKey(entry.account)}
                  onConfigure={() => openAccountConfig(entry.account, entry.upstream || undefined)}
                  onDelete={() => deleteRemoteAccount(entry.account)}
                  onPriorityIntervalChange={(intervalId) => void setAccountPriorityInterval(entry.account, intervalId)}
                  onPriorityTieMove={(direction) => void moveAccountPriority(entry.account, direction)}
                  onShowUpstream={() => openAccountUpstream(entry)}
                  onShowUsageHistory={() => entry.upstream
                    ? openUpstreamUsageHistory(entry.upstream, entry.account.management_account_id)
                    : undefined}
                  onTestAvailability={() => void testAccountAvailability(entry.account)}
                  onForceConnectionTest={() => void forceAccountConnectionTest(entry.account)}
                  onToggle={() => toggleAccountEnabled(entry.account)}
                  priorityIntervals={priorityIntervals}
                  priorityTieMove={priorityTieMoves.get(String(entry.account.management_account_id))}
                  rateWritesEnabled={rateWritesEnabled}
                />
              ))}
            </div>
          ) : (
            <div className="api-key-empty">
              <KeyRound size={18} />
              <span>{allAccountEntries.length ? "没有匹配的 API 账号" : "暂无可管理的 API 账号"}</span>
            </div>
          )}
        </section>
      ) : null}

      {subview === "upstreams" ? (
      <section className="api-key-panel api-key-channel-panel">
        <div className="api-key-panel-head">
          <div>
            <h2>上游</h2>
            <p>同站点按规范 URL 合并。账号倍率 = 上游分组倍率 × 上游充值倍率 ÷ 管理站点充值倍率。</p>
          </div>
          <div className="api-key-toolbar-actions">
            <button
              className="api-key-button api-key-button--secondary"
              disabled={mutationControlsDisabled || anyBusy || !assignedUpstreams.length}
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
            <span className="api-key-sr-only">搜索上游或账号</span>
            <input
              onChange={(event) => setUpstreamSearch(event.target.value)}
              placeholder="搜索上游、URL、账号、ID 或分组"
              type="search"
              value={upstreamSearch}
            />
            <small>{filteredUpstreams.length}/{occupancyUpstreams.length}</small>
          </label>
          <div className="api-key-segmented api-key-channel-occupancy-filter" role="group" aria-label="上游账号状态">
            <button
              aria-pressed={upstreamOccupancyFilter === "occupied"}
              className={upstreamOccupancyFilter === "occupied" ? "active" : ""}
              onClick={() => setUpstreamOccupancyFilter("occupied")}
              type="button"
            >有账号上游 {occupiedUpstreams.length}</button>
            <button
              aria-pressed={upstreamOccupancyFilter === "no_enabled"}
              className={upstreamOccupancyFilter === "no_enabled" ? "active" : ""}
              onClick={() => setUpstreamOccupancyFilter("no_enabled")}
              type="button"
            >无启用上游 {noEnabledUpstreams.length}</button>
            <button
              aria-pressed={upstreamOccupancyFilter === "empty"}
              className={upstreamOccupancyFilter === "empty" ? "active" : ""}
              onClick={() => setUpstreamOccupancyFilter("empty")}
              type="button"
            >无账号上游 {emptyUpstreams.length}</button>
          </div>
          <label className="api-key-filter-select">
            <span>状态</span>
            <select onChange={(event) => setUpstreamStatusFilter(event.target.value as UpstreamStatusFilter)} value={upstreamStatusFilter}>
              <option value="all">全部状态</option>
              <option value="pending">倍率不一致</option>
              <option value="attention">需要处理</option>
              <option value="undiscovered">尚未探测</option>
            </select>
          </label>
        </div>

        {loading && data.upstreams.length === 0 && data.unassigned_accounts.length === 0 ? (
          <div className="api-key-empty">
            <RefreshCcw className="spin" size={18} />
            <span>正在读取上游…</span>
          </div>
        ) : filteredUpstreams.length === 0 && filteredUnassigned.length === 0 ? (
          <div className="api-key-empty">
            <Globe2 size={18} />
            <span>{summary.accounts || summary.upstreams ? "没有匹配的上游或账号" : "暂无可管理的 API 账号"}</span>
          </div>
        ) : (
          <div className="api-key-channel-grid">
            {filteredUpstreams.map(({ upstream }) => (
              <UpstreamCard
                accountCount={upstreamAccountCount(upstream)}
                busyAction={busyUpstreams[upstreamKey(upstream)]}
                upstream={upstream}
                displayTimeZone={displayTimeZone}
                globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                key={upstreamKey(upstream)}
                onConfigureUpstream={() => openUpstreamConfig(upstream)}
                onDelete={() => void deleteUpstream(upstream)}
                onDiscover={() => void discoverUpstream(upstream)}
                onShowAccounts={() => openUpstreamAccounts(upstream)}
                onShowGroups={() => openUpstreamGroups(upstream)}
                onShowMonitors={() => openUpstreamMonitors(upstream)}
                onShowUsageHistory={() => openUpstreamUsageHistory(upstream)}
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
          eyebrow={`${accountCollectionDialog.accounts.length} 个 API 账号`}
          onClose={closeDialog}
          saving={false}
          title={accountCollectionDialog.title}
        >
          {accountCollectionDialog.accounts.length ? (
            <div className="api-key-account-grid api-key-dialog-account-grid">
              {sortApiAccountEntriesByName(accountCollectionDialog.accounts.map((account) => ({
                account,
                upstream: accountCollectionDialog.upstream,
              }))).map((entry) => (
                <AccountCard
                  account={entry.account}
                  busyAction={busyAccounts[accountKey(entry.account)]}
                  upstream={entry.upstream}
                  upstreamMonitorFallbackTestModels={upstreamMonitorFallbackTestModels}
                  displayTimeZone={displayTimeZone}
                  globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
                  key={accountKey(entry.account)}
                  onConfigure={() => openAccountConfig(entry.account, entry.upstream || undefined)}
                  onDelete={() => void deleteRemoteAccount(entry.account)}
                  onPriorityIntervalChange={(intervalId) => void setAccountPriorityInterval(entry.account, intervalId)}
                  onPriorityTieMove={(direction) => void moveAccountPriority(entry.account, direction)}
                  onShowUpstream={() => openAccountUpstream(entry)}
                  onShowUsageHistory={() => entry.upstream
                    ? openUpstreamUsageHistory(entry.upstream, entry.account.management_account_id)
                    : undefined}
                  onTestAvailability={() => void testAccountAvailability(entry.account)}
                  onForceConnectionTest={() => void forceAccountConnectionTest(entry.account)}
                  onToggle={() => void toggleAccountEnabled(entry.account)}
                  priorityIntervals={priorityIntervals}
                  priorityTieMove={priorityTieMoves.get(String(entry.account.management_account_id))}
                  rateWritesEnabled={rateWritesEnabled}
                />
              ))}
            </div>
          ) : <div className="api-key-empty"><KeyRound size={18} /><span>当前没有 API 账号</span></div>}
        </Modal>
      ) : null}

      {accountUpstreamDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow="账号所属上游"
          onClose={closeDialog}
          saving={false}
          title={upstreamDisplayName(accountUpstreamDialog)}
        >
          <div className="api-key-dialog-channel-card">
            <UpstreamCard
              accountCount={upstreamAccountCount(accountUpstreamDialog)}
              busyAction={busyUpstreams[upstreamKey(accountUpstreamDialog)]}
              upstream={accountUpstreamDialog}
              displayTimeZone={displayTimeZone}
              globallyDisabled={mutationControlsDisabled || bulkDiscovering || globallyBusy}
              onConfigureUpstream={() => openUpstreamConfig(accountUpstreamDialog)}
              onDiscover={() => void discoverUpstream(accountUpstreamDialog)}
              onShowAccounts={() => openUpstreamAccounts(accountUpstreamDialog)}
              onShowGroups={() => openUpstreamGroups(accountUpstreamDialog)}
              onShowMonitors={() => openUpstreamMonitors(accountUpstreamDialog)}
              onShowUsageHistory={() => openUpstreamUsageHistory(accountUpstreamDialog)}
              rateWritesEnabled={rateWritesEnabled}
            />
          </div>
        </Modal>
      ) : null}

      {upstreamGroupDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow={`${upstreamGroupDialog.group_options?.length || 0} 个上游分组`}
          onClose={closeDialog}
          saving={false}
          title={upstreamDisplayName(upstreamGroupDialog)}
        >
          <UpstreamGroupList upstream={upstreamGroupDialog} />
        </Modal>
      ) : null}

      {upstreamMonitorDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow={`${upstreamMonitorDialog.upstream_monitor_count ?? upstreamMonitorDialog.upstream_monitors?.length ?? 0} 个上游监控`}
          onClose={closeDialog}
          saving={false}
          title={upstreamDisplayName(upstreamMonitorDialog)}
        >
          <UpstreamMonitorList
            upstream={upstreamMonitorDialog}
            displayTimeZone={displayTimeZone}
            error={upstreamMonitorError}
            loading={upstreamMonitorLoading}
            onRefresh={() => void refreshUpstreamMonitors(upstreamMonitorDialog)}
          />
        </Modal>
      ) : null}

      {upstreamUsageHistoryDialog ? (
        <Modal
          dialogRef={dialogRef}
          eyebrow={upstreamUsageHistoryFilters.apiKeyAccountId
            ? "API 账号统计数据"
            : "上游统计数据"}
          onClose={closeDialog}
          saving={false}
          title={upstreamDisplayName(upstreamUsageHistoryDialog)}
        >
          <UpstreamUsageHistoryDialog
            appliedFilters={upstreamUsageHistoryFilters}
            upstream={upstreamUsageHistoryDialog}
            displayTimeZone={displayTimeZone}
            draftFilters={upstreamUsageHistoryDraftFilters}
            error={upstreamUsageHistoryError}
            history={upstreamUsageHistory}
            loading={upstreamUsageHistoryLoading}
            onApplyFilters={applyUpstreamUsageHistoryFilters}
            onDraftFiltersChange={setUpstreamUsageHistoryDraftFilters}
            onPreset={applyUpstreamUsageHistoryPreset}
            onRefresh={() => void loadUpstreamUsageHistory(upstreamUsageHistoryDialog, upstreamUsageHistoryFilters)}
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
                  ? "按上游实际倍率比例计算成本效率，低倍率账号获得更低的优先级数值和更高的调度权重。"
                  : "按上游实际倍率排序后，从区间起点开始使用固定间隔依次分配优先级。"}</small>
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
                  同一管理站点调度分组内，数值更低的区间权重更高；需要形成固定层级时，建议连续设置且不重叠，例如 [40, 70) 与 [70, 100)。
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
                  <strong>上游实际倍率上涨时自动暂停账号</strong>
                  <small>绑定此区间且选择“继承”的账号会使用下面的阈值；上游实际倍率严格大于阈值时暂停，等于或低于阈值时不暂停。</small>
                </span>
              </label>
              <label className="api-key-field api-key-field--wide">
                <span>上游实际倍率阈值</span>
                <input
                  aria-label="优先级区间上游实际倍率阈值"
                  disabled={!priorityIntervalForm.ratePauseEnabled}
                  min="0.000001"
                  onChange={(event) => setPriorityIntervalForm((current) => ({ ...current, rateAbsoluteThreshold: event.target.value }))}
                  step="any"
                  type="number"
                  value={priorityIntervalForm.rateAbsoluteThreshold}
                />
                <small>设置保存后，在下一次上游同步时应用；只有上游实际倍率严格大于该阈值才会暂停账号。</small>
              </label>
            </div>
            <DialogError message={dialogError} />
            <DialogActions onCancel={closeDialog} saving={savingDialog} />
          </form>
        </Modal>
      ) : null}

      {editingUpstream ? (
        <Modal title={"配置上游 · " + upstreamDisplayName(editingUpstream)} eyebrow="上游" onClose={closeDialog} dialogRef={dialogRef} saving={savingDialog}>
          <form className="api-key-config-form api-key-channel-form" onSubmit={saveUpstream}>
            <div className="api-key-config-fields">
              <label className="api-key-field">
                <span>上游名称</span>
                <input
                  autoFocus
                  onChange={(event) => setUpstreamForm((current) => ({ ...current, displayName: event.target.value }))}
                  placeholder={displayHost(upstreamForm.baseUrl)}
                  value={upstreamForm.displayName}
                />
              </label>
              <label className="api-key-field">
                <span>API 地址</span>
                <input
                  onChange={(event) => setUpstreamForm((current) => ({ ...current, baseUrl: event.target.value }))}
                  placeholder="https://example.com"
                  required
                  type="url"
                  value={upstreamForm.baseUrl}
                />
                <small>末尾 /v1、/api/v1 与多余斜杠会归并到同一上游。</small>
              </label>

              <label className="api-key-field api-key-field--wide">
                <span>管理地址（可选）</span>
                <input
                  onChange={(event) => setUpstreamForm((current) => ({ ...current, managementBaseUrl: event.target.value }))}
                  placeholder="留空表示与 API 地址相同"
                  type="url"
                  value={upstreamForm.managementBaseUrl}
                />
                <small>余额、分组和 Key 列表从管理地址读取；模型请求仍使用 API 地址。</small>
              </label>

              <fieldset className="api-key-field api-key-field--wide">
                <legend>上游协议</legend>
                <div className="api-key-segmented">
                  {(["auto", "newapi", "sub2api"] as UpstreamType[]).map((type) => (
                    <button
                      aria-pressed={upstreamForm.upstreamType === type}
                      className={upstreamForm.upstreamType === type ? "active" : ""}
                      key={type}
                      onClick={() => setUpstreamForm((current) => ({ ...current, upstreamType: type }))}
                      type="button"
                    >
                      {statusLabel(type)}
                    </button>
                  ))}
                </div>
              </fieldset>

              <label className="api-key-field api-key-field--wide api-key-clear-token">
                <input
                  checked={upstreamForm.probeEnabled}
                  onChange={(event) =>
                    setUpstreamForm((current) => ({
                      ...current,
                      probeEnabled: event.target.checked,
                    }))
                  }
                  type="checkbox"
                />
                <span>纳入自动上游探测</span>
                <small>仅在全局“同步 API 账号上游”开启时生效；关闭后保留上次观测结果，也不会自动修改该上游账号的倍率、优先级或启用状态。</small>
              </label>

              <label className="api-key-field">
                <span>{editingUpstreamType === "newapi" ? "访问令牌" : "Access Token"}</span>
                <div className="api-key-secret-input">
                  <input
                    autoComplete="new-password"
                    disabled={upstreamForm.clearAccessToken}
                    onChange={(event) => setUpstreamForm((current) => ({ ...current, accessToken: event.target.value }))}
                    placeholder={editingUpstream.access_token_set
                      ? "已保存；留空保持"
                      : editingUpstreamType === "newapi"
                        ? "粘贴上游访问令牌"
                        : "粘贴上游 Access Token"}
                    type={upstreamCredentialVisibility.accessToken ? "text" : "password"}
                    value={upstreamForm.accessToken}
                  />
                  <button
                    aria-label={upstreamCredentialVisibility.accessToken ? "隐藏 Access Token" : "查看 Access Token"}
                    aria-pressed={upstreamCredentialVisibility.accessToken}
                    className="api-key-secret-toggle"
                    disabled={upstreamForm.clearAccessToken || loadingUpstreamCredential !== null}
                    onClick={() => void toggleUpstreamCredential("accessToken")}
                    title={upstreamCredentialVisibility.accessToken ? "隐藏 Access Token" : "查看 Access Token"}
                    type="button"
                  >
                    {loadingUpstreamCredential === "accessToken"
                      ? <RefreshCcw className="spin" size={16} />
                      : upstreamCredentialVisibility.accessToken
                        ? <EyeOff size={16} />
                        : <Eye size={16} />}
                  </button>
                </div>
                <label className="api-key-clear-token">
                  <input
                    checked={upstreamForm.clearAccessToken}
                    disabled={!editingUpstream.access_token_set && !upstreamForm.accessToken}
                    onChange={(event) =>
                      setUpstreamForm((current) => ({
                        ...current,
                        accessToken: event.target.checked ? "" : current.accessToken,
                        clearAccessToken: event.target.checked,
                      }))
                    }
                    type="checkbox"
                  />
                  <span>清除已保存的{editingUpstreamType === "newapi" ? "访问令牌" : "Access Token"}</span>
                </label>
              </label>

              {showUpstreamRefreshToken ? (
                <label className="api-key-field">
                  <span>Refresh Token（自动续期）</span>
                  <div className="api-key-secret-input">
                    <input
                      autoComplete="new-password"
                      disabled={upstreamForm.clearRefreshToken}
                      onChange={(event) =>
                        setUpstreamForm((current) => ({ ...current, refreshToken: event.target.value }))
                      }
                      placeholder={editingUpstream.refresh_token_set ? "已保存；留空保持" : "粘贴上游 Refresh Token"}
                      type={upstreamCredentialVisibility.refreshToken ? "text" : "password"}
                      value={upstreamForm.refreshToken}
                    />
                    <button
                      aria-label={upstreamCredentialVisibility.refreshToken ? "隐藏 Refresh Token" : "查看 Refresh Token"}
                      aria-pressed={upstreamCredentialVisibility.refreshToken}
                      className="api-key-secret-toggle"
                      disabled={upstreamForm.clearRefreshToken || loadingUpstreamCredential !== null}
                      onClick={() => void toggleUpstreamCredential("refreshToken")}
                      title={upstreamCredentialVisibility.refreshToken ? "隐藏 Refresh Token" : "查看 Refresh Token"}
                      type="button"
                    >
                      {loadingUpstreamCredential === "refreshToken"
                        ? <RefreshCcw className="spin" size={16} />
                        : upstreamCredentialVisibility.refreshToken
                          ? <EyeOff size={16} />
                          : <Eye size={16} />}
                    </button>
                  </div>
                  <label className="api-key-clear-token">
                    <input
                      checked={upstreamForm.clearRefreshToken}
                      disabled={!editingUpstream.refresh_token_set && !upstreamForm.refreshToken}
                      onChange={(event) =>
                        setUpstreamForm((current) => ({
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

              {showUpstreamLoginCredentials ? (
                <fieldset className="api-key-field api-key-field--wide api-key-login-credentials">
                  <legend>账号密码登录（可选）</legend>
                  <div className="api-key-config-fields api-key-config-fields--nested">
                    <label className="api-key-field">
                      <span>登录账号</span>
                      <div className="api-key-secret-input">
                        <input
                          autoComplete="username"
                          disabled={upstreamForm.clearLoginCredentials}
                          onChange={(event) => setUpstreamForm((current) => ({ ...current, loginUsername: event.target.value }))}
                          placeholder={editingUpstream.login_credentials_set ? "已保存；留空保持" : "邮箱或登录账号"}
                          type={upstreamCredentialVisibility.loginUsername ? "text" : "password"}
                          value={upstreamForm.loginUsername}
                        />
                        <button
                          aria-label={upstreamCredentialVisibility.loginUsername ? "隐藏登录账号" : "查看登录账号"}
                          aria-pressed={upstreamCredentialVisibility.loginUsername}
                          className="api-key-secret-toggle"
                          disabled={upstreamForm.clearLoginCredentials || loadingUpstreamCredential !== null}
                          onClick={() => void toggleUpstreamCredential("loginUsername")}
                          title={upstreamCredentialVisibility.loginUsername ? "隐藏登录账号" : "查看登录账号"}
                          type="button"
                        >
                          {loadingUpstreamCredential === "loginUsername"
                            ? <RefreshCcw className="spin" size={16} />
                            : upstreamCredentialVisibility.loginUsername
                              ? <EyeOff size={16} />
                              : <Eye size={16} />}
                        </button>
                      </div>
                    </label>
                    <label className="api-key-field">
                      <span>登录密码</span>
                      <div className="api-key-secret-input">
                        <input
                          autoComplete="new-password"
                          disabled={upstreamForm.clearLoginCredentials}
                          onChange={(event) => setUpstreamForm((current) => ({ ...current, loginPassword: event.target.value }))}
                          placeholder={editingUpstream.login_credentials_set ? "已保存；留空保持" : "登录密码"}
                          type={upstreamCredentialVisibility.loginPassword ? "text" : "password"}
                          value={upstreamForm.loginPassword}
                        />
                        <button
                          aria-label={upstreamCredentialVisibility.loginPassword ? "隐藏登录密码" : "查看登录密码"}
                          aria-pressed={upstreamCredentialVisibility.loginPassword}
                          className="api-key-secret-toggle"
                          disabled={upstreamForm.clearLoginCredentials || loadingUpstreamCredential !== null}
                          onClick={() => void toggleUpstreamCredential("loginPassword")}
                          title={upstreamCredentialVisibility.loginPassword ? "隐藏登录密码" : "查看登录密码"}
                          type="button"
                        >
                          {loadingUpstreamCredential === "loginPassword"
                            ? <RefreshCcw className="spin" size={16} />
                            : upstreamCredentialVisibility.loginPassword
                              ? <EyeOff size={16} />
                              : <Eye size={16} />}
                        </button>
                      </div>
                    </label>
                  </div>
                  <label className="api-key-clear-token">
                    <input
                      checked={upstreamForm.clearLoginCredentials}
                      disabled={!editingUpstream.login_credentials_set && !upstreamForm.loginUsername && !upstreamForm.loginPassword}
                      onChange={(event) =>
                        setUpstreamForm((current) => ({
                          ...current,
                          loginUsername: event.target.checked ? "" : current.loginUsername,
                          loginPassword: event.target.checked ? "" : current.loginPassword,
                          clearLoginCredentials: event.target.checked,
                        }))
                      }
                      type="checkbox"
                    />
                    <span>清除已保存的登录账号和密码</span>
                  </label>
                  <small>无 Refresh Token 的 Sub2API 上游会使用 curl_cffi 浏览器指纹登录，自动刷新 Access Token；登录响应包含 Refresh Token 时也会保存。</small>
                </fieldset>
              ) : null}

              {editingUpstreamType !== "sub2api" ? <label className="api-key-field">
                <span>NewAPI 用户 ID（余额探测）</span>
                <input
                  onChange={(event) => setUpstreamForm((current) => ({ ...current, upstreamUserId: event.target.value }))}
                  placeholder="填写数字用户 ID"
                  value={upstreamForm.upstreamUserId}
                />
                <small>NewAPI 用户余额接口通常要求 New-Api-User；请填写个人设置中显示的数字用户 ID。</small>
              </label> : null}

              <label className="api-key-field api-key-field--wide">
                <span>手动充值倍率（¥ / $1）</span>
                <input
                  inputMode="decimal"
                  min="0"
                  onChange={(event) =>
                    setUpstreamForm((current) => ({ ...current, manualRechargeMultiplier: event.target.value }))
                  }
                  placeholder="留空使用自动探测值"
                  step="any"
                  type="number"
                  value={upstreamForm.manualRechargeMultiplier}
                />
                <small>人民币实付 ÷ 获得的美元额度。例如 ¥1 获得 $10，填写 0.1，表示 ¥0.1 可使用 $1 额度。</small>
              </label>

              <TokenGuide upstreamType={editingUpstreamType} />
            </div>
            <DialogError message={dialogError} />
            <DialogActions onCancel={closeDialog} saving={savingDialog} />
          </form>
        </Modal>
      ) : null}

      {editingAccount ? (
        <Modal title={"配置账号 · " + accountDisplayName(editingAccount)} eyebrow={"#" + editingAccount.management_account_id} onClose={closeDialog} dialogRef={dialogRef} saving={savingDialog}>
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
                <span>所属上游</span>
                <select
                  onChange={(event) =>
                    setAccountForm((current) => ({
                      ...current,
                      upstreamId: event.target.value,
                      availabilityMonitorId: "",
                    }))
                  }
                  value={accountForm.upstreamId}
                >
                  <option value="">未分配上游</option>
                  {data.upstreams.map((upstream) => (
                    <option key={upstreamKey(upstream)} value={String(upstream.upstream_id)}>
                      {upstreamDisplayName(upstream)} · {displayHost(upstreamBaseUrl(upstream))}
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
                  {finiteNumber(editingAccount.upstream_group_multiplier) === null
                    ? statusLabel(editingAccount.group_multiplier_status || "not_ready")
                    : formatMultiplier(editingAccount.upstream_group_multiplier) + " · " + sourceLabel(editingAccount.group_multiplier_source)}
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
                      ? "此账号不会因上游实际倍率超过阈值而自动暂停。"
                      : "账号独立阈值优先于优先级区间；回落到阈值后自动恢复。"}
                </small>
              </div>
              {accountForm.ratePausePolicy === "custom" ? (
                <label className="api-key-field api-key-field--wide">
                  <span>上游实际倍率阈值</span>
                  <input
                    aria-label="账号上游实际倍率阈值"
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
                    availabilityMonitorId: event.target.value === "upstream_monitor" ? current.availabilityMonitorId : "",
                  }))}
                  value={accountForm.availabilityCheckMode}
                >
                  <option value="upstream_monitor">绑定监控面板（默认）</option>
                  <option value="independent_model">独立模型测试</option>
                  <option value="disabled">关闭</option>
                </select>
              </div>

              {accountForm.availabilityCheckMode === "upstream_monitor" ? (
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
                    {(selectedUpstreamForAccountForm?.upstream_monitors || []).map((monitor) => (
                      <option key={String(monitor.id)} value={String(monitor.id)}>
                        {monitor.name || `监控点 #${monitor.id}`} · {monitor.primary_model || "未标注模型"}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              {!availabilityMonitoringDisabled ? (
                <label className="api-key-field api-key-field--wide">
                <span>{accountForm.availabilityCheckMode === "upstream_monitor" ? "监控异常回退模型" : "独立测试模型"}</span>
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
                        ? "模型白名单同步失败，请先重新同步 API 账号。"
                        : "尚未同步模型白名单；首次导入或本地缺失时会自动同步。")}
                </small>
                </label>
              ) : null}

              <div className="api-key-form-note api-key-field--wide">
                <BadgeDollarSign size={16} />
                <span>
                  {rateWritesEnabled
                    ? "目标倍率 = 上游分组倍率 × 上游充值倍率 ÷ 管理站点充值倍率；保存后自动同步。"
                    : "目标倍率 = 上游分组倍率 × 上游充值倍率 ÷ 管理站点充值倍率；自动同步关闭，仅计算目标倍率。"}
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
          loading={rateLogsLoading}
          logs={rateLogs}
          upstreams={data.upstreams}
          kind={subview === "account-rate-log" ? "account_rate" : "upstream"}
          page={rateLogPage}
          pageSize={rateLogPageSize}
          pageSizeOptions={availableChangeLogPageSizes}
          totalCount={rateLogTotalCount}
          onApplyFilters={applyRateLogFilters}
          onClearFilters={clearRateLogFilters}
          onDraftFiltersChange={setRateLogDraftFilters}
          onPageChange={setRateLogPage}
          onPageSizeChange={(pageSize) => {
            setRateLogPageSize(pageSize);
            setRateLogPage(1);
          }}
          onRefresh={() => void loadRateLogs()}
        />
      ) : (
        <SchedulingChangeLogView
          displayTimeZone={displayTimeZone}
          draftFilters={scheduleLogDraftFilters}
          error={scheduleLogsError}
          filtersApplied={Boolean(scheduleLogFilters.startDate || scheduleLogFilters.endDate)}
          loading={scheduleLogsLoading}
          logs={scheduleLogs}
          page={scheduleLogPage}
          pageSize={scheduleLogPageSize}
          pageSizeOptions={availableChangeLogPageSizes}
          totalCount={scheduleLogTotalCount}
          onApplyFilters={applyScheduleLogFilters}
          onClearFilters={clearScheduleLogFilters}
          onDraftFiltersChange={setScheduleLogDraftFilters}
          onPageChange={setScheduleLogPage}
          onPageSizeChange={(pageSize) => {
            setScheduleLogPageSize(pageSize);
            setScheduleLogPage(1);
          }}
          onRefresh={() => void loadScheduleLogs()}
        />
      )}
    </section>
  );
}

function UpstreamCard({
  upstream,
  accountCount,
  displayTimeZone,
  busyAction,
  globallyDisabled,
  onConfigureUpstream,
  onDelete,
  onDiscover,
  onShowAccounts,
  onShowGroups,
  onShowMonitors,
  onShowUsageHistory,
  rateWritesEnabled,
}: {
  upstream: Upstream;
  accountCount: number;
  displayTimeZone: string;
  busyAction?: string;
  globallyDisabled: boolean;
  onConfigureUpstream: () => void;
  onDelete?: () => void;
  onDiscover: () => void;
  onShowAccounts: () => void;
  onShowGroups: () => void;
  onShowMonitors: () => void;
  onShowUsageHistory: () => void;
  rateWritesEnabled: boolean;
}) {
  const groups = upstream.group_options || [];
  const visibleGroups = groups.slice(0, 4);
  const type = resolvedUpstreamType(upstream);
  const status = upstreamStatus(upstream);
  const apiUrl = upstreamBaseUrl(upstream);
  const configuredManagementUrl = upstream.management_url?.trim() || "";
  const siteUrl = configuredManagementUrl || apiUrl;
  const displayName = upstreamDisplayName(upstream);
  const isUrlDisplayName = urlLikeDisplayName(displayName, apiUrl)
    || urlLikeDisplayName(displayName, siteUrl);
  const hasSeparateManagementUrl = Boolean(configuredManagementUrl)
    && displayCanonicalUrl(configuredManagementUrl) !== displayCanonicalUrl(apiUrl);
  const message = upstreamDisplayMessage(upstream);
  const error = upstreamDisplayError(upstream);
  const busy = Boolean(busyAction) || globallyDisabled;
  const discoveryCopy = upstreamDiscoveryCopy(rateWritesEnabled);
  const balanceDetails = balanceDetail(upstream);
  const todayUsage = formatDailyBalanceUsed(upstream, "today", displayTimeZone);
  const yesterdayUsage = formatDailyBalanceUsed(upstream, "yesterday", displayTimeZone);
  return (
    <article className={
      "api-key-channel-card"
      + (upstreamHasAttention(upstream) ? " api-key-channel-card--attention" : "")
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
            aria-label={"配置上游 " + upstreamDisplayName(upstream)}
            className="api-key-icon-button"
            disabled={busy}
            onClick={onConfigureUpstream}
            title="配置上游"
            type="button"
          >
            <Pencil size={15} />
          </button>
          {accountCount > 0 ? (
            <button
              aria-label={discoveryCopy.upstreamAriaPrefix + " " + upstreamDisplayName(upstream)}
              className="api-key-icon-button api-key-icon-button--discover"
              disabled={busy}
              onClick={onDiscover}
              title={discoveryCopy.upstreamTitle}
              type="button"
            >
              <Radar className={busyAction === "discover" ? "spin" : ""} size={15} />
            </button>
          ) : (
            <button
              aria-label={"删除空上游 " + upstreamDisplayName(upstream)}
              className="api-key-icon-button api-key-icon-button--danger"
              disabled={busy}
              onClick={onDelete}
              title="删除空上游"
              type="button"
            >
              <Trash2 size={15} />
            </button>
          )}
        </div>
        <div className="api-key-channel-addresses">
          <UpstreamAddressBox label="站点" url={siteUrl} />
          <UpstreamAddressBox label="API" url={hasSeparateManagementUrl ? apiUrl : ""} />
        </div>
      </header>

      <div className="api-key-channel-stats">
        <UpstreamStat
          badge={<StatusChip status={upstream.balance_status || upstream.status || "not_checked"} />}
          className="api-key-channel-stat--balance"
          icon={<WalletCards size={16} />}
          label="余额"
        >
          <div className="api-key-channel-balance-chips">
            <div className="api-key-channel-balance-chip api-key-chip api-key-chip--info">
              <HelpPopover
                label="查看钱包余额说明"
                trigger={<span>钱包余额</span>}
                triggerClassName="api-key-balance-kind api-key-balance-kind--original"
              >
                钱包余额是上游返回的原始美元钱包额度。
              </HelpPopover>
              <b>{formatCurrentPlatformBalance(upstream)}</b>
            </div>
            <div className="api-key-channel-balance-chip api-key-chip api-key-chip--success">
              <HelpPopover
                label="查看实际余额说明"
                trigger={<span>实际余额</span>}
                triggerClassName="api-key-balance-kind api-key-balance-kind--combined"
              >
                实际余额等于钱包余额乘以上游充值倍率，以人民币计算。
              </HelpPopover>
              <b>{formatCurrentRechargeAdjustedBalance(upstream)}</b>
            </div>
          </div>
          <div className="api-key-channel-daily-usage">
            {[yesterdayUsage, todayUsage].map((usage) => (
              <span
                aria-label={usage.detail}
                className={"api-key-channel-usage-chip api-key-chip api-key-chip--" + usage.tone}
                key={usage.label}
                title={usage.detail}
              >
                <span>{usage.label}{usage.stale ? "（旧）" : ""}</span><b>{usage.value}</b>
              </span>
            ))}
          </div>
          {balanceDetails ? <span>{balanceDetails}</span> : null}
        </UpstreamStat>
        <UpstreamStat className="api-key-channel-stat--recharge" icon={<BadgeDollarSign size={16} />} label="充值倍率">
          <strong>{"¥" + formatCostPerUsd(upstream.upstream_recharge_multiplier) + " / $1"}</strong>
        </UpstreamStat>
        <UpstreamStat className="api-key-channel-stat--probe" icon={<Radar size={16} />} label="最近探测">
          <strong>{formatDate(upstream.last_discovered_at || upstream.checked_at, displayTimeZone)}</strong>
        </UpstreamStat>
      </div>

      <div className="api-key-channel-credential-line">
        <span className={upstream.probe_enabled === false ? "needs-attention" : "is-ready"}>
          <Radar size={13} />
          {upstream.probe_enabled === false ? "自动探测已关闭" : "自动探测已开启"}
        </span>
        <span className={upstream.access_token_set ? "is-ready" : "needs-attention"}>
          <KeyRound size={13} />
          {upstreamTokenInvalid(upstream)
            ? (type === "newapi" ? "访问令牌已失效" : "Access Token 已失效")
            : upstream.access_token_set
              ? (type === "newapi" ? "访问令牌已配置" : "Access Token 已配置")
              : (type === "newapi" ? "缺少访问令牌" : "缺少 Access Token")}
        </span>
        {type === "sub2api" ? (
          <span className={upstream.refresh_token_set ? "is-ready" : "needs-attention"}>
            <RefreshCcw size={13} />
            {upstream.refresh_token_set
              ? "Refresh Token 已配置"
              : upstream.login_credentials_set
                ? "登录续期已配置"
                : "缺少 Refresh Token 或登录凭据"}
          </span>
        ) : null}
        {upstream.upstream_user_id ? <span className="api-key-mono">用户 {upstream.upstream_user_id}</span> : null}
        {message ? (
          <span className="api-key-channel-message" title={message}>
            {message}
          </span>
        ) : null}
      </div>

      <button
        aria-label={`查看 ${upstreamDisplayName(upstream)} 的 ${groups.length} 个上游分组`}
        className="api-key-channel-groups"
        onClick={onShowGroups}
        type="button"
      >
        <div className="api-key-channel-section-label">
          <span>上游可用分组</span>
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

      <section className="api-key-channel-accounts" aria-label="上游账号">
        <button
          aria-label={`查看 ${upstreamDisplayName(upstream)} 的 ${accountCount} 个账号`}
          className="api-key-channel-account-button"
          onClick={onShowAccounts}
          type="button"
        >
          <UsersRound size={16} />
          <span>账号 {accountCount} 个</span>
          <ArrowRight size={15} />
        </button>
        <button
          aria-label={`查看 ${upstreamDisplayName(upstream)} 的历史用量统计`}
          className="api-key-channel-account-button"
          onClick={onShowUsageHistory}
          type="button"
        >
          <ChartNoAxesCombined size={16} />
          <span>统计</span>
          <ArrowRight size={15} />
        </button>
        <button
          aria-label={`查看 ${upstreamDisplayName(upstream)} 的 ${upstream.upstream_monitor_count ?? upstream.upstream_monitors?.length ?? 0} 个上游状态`}
          className="api-key-channel-account-button"
          onClick={onShowMonitors}
          type="button"
        >
          <Activity size={16} />
          <span>上游状态 {upstream.upstream_monitor_count ?? upstream.upstream_monitors?.length ?? 0} 个</span>
          <ArrowRight size={15} />
        </button>
      </section>
    </article>
  );
}

function UpstreamGroupList({ upstream }: { upstream: Upstream }) {
  const groups = upstream.group_options || [];
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
          <span className="api-key-channel-url">选择上游后才能读取分组并计算目标倍率</span>
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

function UpstreamMonitorList({
  upstream,
  displayTimeZone,
  error,
  loading,
  onRefresh,
}: {
  upstream: Upstream;
  displayTimeZone: string;
  error: string;
  loading: boolean;
  onRefresh: () => void;
}) {
  const monitors = upstream.upstream_monitors || [];
  return (
    <div className="api-key-monitor-dialog-body">
      {loading ? <div className="api-key-monitor-loading" role="status"><RefreshCcw className="spin" size={16} /><span>正在读取上游状态…</span></div> : null}
      {error ? <div className="api-key-channel-error" role="alert"><AlertTriangle size={14} /><span>{error}</span></div> : null}
      <div className="api-key-monitor-summary">
        <StatusChip status={upstream.upstream_monitor_status || "not_checked"} />
        <span>{upstreamMonitorMessage(upstream)}</span>
        <time>{formatDate(upstream.upstream_monitor_checked_at, displayTimeZone)}</time>
        <button
          aria-label="刷新上游监控"
          className="api-key-icon-button"
          disabled={loading}
          onClick={onRefresh}
          title="刷新上游监控"
          type="button"
        >
          <RefreshCcw className={loading ? "spin" : undefined} size={15} />
        </button>
      </div>
      {monitors.length ? (
        <div className="api-key-monitor-list">
          {monitors.map((monitor) => (
            <UpstreamMonitorCard
              displayTimeZone={displayTimeZone}
              key={String(monitor.id)}
              monitor={monitor}
            />
          ))}
        </div>
      ) : (
        <div className="api-key-empty">
          <Activity size={18} />
          <span>{upstream.upstream_monitor_status === "unsupported"
            ? "该上游暂不支持上游状态接口"
            : upstream.upstream_monitor_status === "not_configured"
              ? "该上游未配置公开监控面板"
              : "暂无上游状态数据"}</span>
        </div>
      )}
    </div>
  );
}

function UpstreamUsageHistoryDialog({
  appliedFilters,
  upstream,
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
  upstream: Upstream;
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
  const accountOptions = usageHistoryAccountOptions(history, upstream);
  const days = history?.days || [];
  const selectedAccountId = appliedFilters.apiKeyAccountId || null;
  const selectedAccount = selectedAccountId
    ? accountOptions.find((account) => String(account.management_account_id) === selectedAccountId) || null
    : null;
  const totals = history?.totals || null;
  const lifetimeTotals = history?.lifetime_totals || null;
  const totalProfit = historyProfit(totals);
  const totalProfitMargin = historyProfitMargin(totals);
  const lifetimeProfit = historyProfit(lifetimeTotals);
  const lifetimeProfitMargin = historyProfitMargin(lifetimeTotals);
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
                <option key={String(account.management_account_id)} value={String(account.management_account_id)}>
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
            <UsageHistoryMetric label="上游实际成本" tone="upstream" value={formatHistoryAmount(totals?.upstream_actual_cost_cny, "CNY")} />
            <UsageHistoryMetric label="管理站点账号成本" tone="cost" value={formatHistoryAmount(totals?.management_account_cost_cny, "CNY")} />
            <UsageHistoryMetric label="实际收入" tone="income" value={formatHistoryAmount(totals?.actual_income_cny, "CNY")} />
            <UsageHistoryMetric label="利润" tone={totalProfit !== null && totalProfit < 0 ? "negative" : "profit"} value={formatHistorySignedAmount(totalProfit, "CNY")} />
            <UsageHistoryMetric label="利润率" tone={totalProfitMargin !== null && totalProfitMargin < 0 ? "negative" : "profit"} value={formatHistoryPercent(totalProfitMargin)} />
          </div>

          <UsageHistoryFinancialChart
            days={days}
            selectedAccountId={selectedAccountId}
            title={selectedAccount ? "API 账号每日收支" : "上游每日收支"}
          />

          <div className="api-key-usage-history-table-wrap">
            <table className="api-key-usage-history-table">
              <thead>
                <tr>
                  <th scope="col">日期</th>
                  <th scope="col">上游实际成本</th>
                  <th scope="col">管理站点账号成本</th>
                  <th scope="col">收入</th>
                  <th scope="col">利润</th>
                  <th scope="col">利润率</th>
                </tr>
              </thead>
              <tbody>
                {days.map((day) => {
                  const upstreamCost = historyDayUpstreamCost(day, selectedAccountId);
                  const managementAccountCost = historyDayManagementAccountCost(day, selectedAccountId);
                  const selectedDayAccount = usageHistoryDayAccount(day, selectedAccountId);
                  const dailyIncome = finiteNumber(
                    selectedAccountId ? selectedDayAccount?.actual_income_cny : day.actual_income_cny,
                  );
                  const dailyProfit = historyDayProfit(day, selectedAccountId);
                  const dailyProfitMargin = historyDayProfitMargin(day, selectedAccountId, dailyProfit);
                  return (
                    <tr key={day.date}>
                      <th scope="row">{formatUsageHistoryDate(day.date, displayTimeZone)}</th>
                      <td>{formatHistoryAmount(upstreamCost, "CNY")}</td>
                      <td>{formatHistoryAmount(managementAccountCost, "CNY")}</td>
                      <td title={historyIncomeBreakdownTitle(day, selectedAccountId)}>
                        {formatHistoryAmount(dailyIncome, "CNY")}
                      </td>
                      <td className={dailyProfit !== null && dailyProfit < 0 ? "is-negative" : "is-positive"}>
                        {formatHistorySignedAmount(dailyProfit, "CNY")}
                      </td>
                      <td className={dailyProfitMargin !== null && dailyProfitMargin < 0 ? "is-negative" : "is-positive"}>
                        {formatHistoryPercent(dailyProfitMargin)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {lifetimeTotals ? (
            <div className="api-key-usage-history-lifetime" aria-label="累计收支">
              <span>数据库累计</span>
              <strong>上游实际成本 {formatHistoryAmount(lifetimeTotals.upstream_actual_cost_cny, "CNY")}</strong>
              <strong>管理站点账号成本 {formatHistoryAmount(lifetimeTotals.management_account_cost_cny, "CNY")}</strong>
              <strong>实际收入 {formatHistoryAmount(lifetimeTotals.actual_income_cny, "CNY")}</strong>
              <strong className={lifetimeProfit !== null && lifetimeProfit < 0 ? "is-negative" : "is-positive"}>
                利润 {formatHistorySignedAmount(lifetimeProfit, "CNY")}
              </strong>
              <strong className={lifetimeProfitMargin !== null && lifetimeProfitMargin < 0 ? "is-negative" : "is-positive"}>
                利润率 {formatHistoryPercent(lifetimeProfitMargin)}
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

type UsageHistoryChartSeriesKey = "upstreamCost" | "managementAccountCost" | "actualIncome" | "profit" | "profitMargin";

function UsageHistoryFinancialChart({
  days,
  selectedAccountId,
  title,
}: {
  days: UpstreamUsageHistory["days"];
  selectedAccountId: string | null;
  title: string;
}) {
  const series = days.map((day) => {
    const profit = historyDayProfit(day, selectedAccountId);
    return {
      date: day.date,
      upstreamCost: historyDayUpstreamCost(day, selectedAccountId),
      managementAccountCost: historyDayManagementAccountCost(day, selectedAccountId),
      actualIncome: finiteNumber(
        selectedAccountId
          ? usageHistoryDayAccount(day, selectedAccountId)?.actual_income_cny
          : day.actual_income_cny,
      ),
      profit,
      profitMargin: historyDayProfitMargin(day, selectedAccountId, profit),
    };
  });
  const maxHorizontalZoom = Math.max(1, Math.min(6, Math.ceil(series.length / 7)));
  const [horizontalZoom, setHorizontalZoom] = useState(1);
  const [visibleSeries, setVisibleSeries] = useState<Record<UsageHistoryChartSeriesKey, boolean>>({
    upstreamCost: true,
    managementAccountCost: true,
    actualIncome: true,
    profit: true,
    profitMargin: true,
  });
  useEffect(() => {
    setHorizontalZoom((current) => Math.min(current, maxHorizontalZoom));
  }, [maxHorizontalZoom]);
  const toggleSeries = (key: UsageHistoryChartSeriesKey) => {
    setVisibleSeries((current) => ({ ...current, [key]: !current[key] }));
  };
  const values = series.flatMap((point) => [
    point.upstreamCost,
    point.managementAccountCost,
    point.actualIncome,
    point.profit,
  ]).filter((value): value is number => value !== null);
  if (!values.length) {
    return (
      <section className="api-key-usage-history-chart api-key-usage-history-chart--empty" aria-label={title}>
        <div><ChartNoAxesCombined size={16} /><strong>{title}</strong></div>
        <span>筛选期间没有可绘制的收支数据</span>
      </section>
    );
  }

  const width = 760;
  const height = 236;
  const axisWidth = 54;
  const padding = { top: 20, right: 18, bottom: 34, left: 8 };
  const zoomedWidth = (width - axisWidth) * horizontalZoom;
  const chartWidth = zoomedWidth - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const maximum = Math.max(...values, 0);
  const minimum = Math.min(...values, 0);
  const margin = Math.max(maximum - minimum, 1) * 0.08;
  const yMaximum = maximum === minimum ? maximum + 1 : maximum + margin;
  const yMinimum = maximum === minimum ? minimum - 1 : minimum - margin;
  const yRange = yMaximum - yMinimum;
  const marginValues = series
    .map((point) => point.profitMargin)
    .filter((value): value is number => value !== null);
  const marginMaximum = Math.max(...marginValues, 0);
  const marginMinimum = Math.min(...marginValues, 0);
  const marginPadding = Math.max(marginMaximum - marginMinimum, 1) * 0.08;
  const marginYMaximum = marginMaximum === marginMinimum
    ? marginMaximum + 1
    : marginMaximum + marginPadding;
  const marginYMinimum = marginMaximum === marginMinimum
    ? marginMinimum - 1
    : marginMinimum - marginPadding;
  const marginYRange = marginYMaximum - marginYMinimum;
  const xFor = (index: number) => padding.left + (series.length <= 1 ? chartWidth / 2 : (index / (series.length - 1)) * chartWidth);
  const yFor = (value: number | null) => value === null
    ? null
    : padding.top + ((yMaximum - value) / yRange) * chartHeight;
  const yForMargin = (value: number | null) => value === null
    ? null
    : padding.top + ((marginYMaximum - value) / marginYRange) * chartHeight;
  const linePoints = (field: "upstreamCost" | "managementAccountCost" | "actualIncome") => series
    .map((point, index) => {
      const y = yFor(point[field]);
      return y === null ? null : `${xFor(index).toFixed(1)},${y.toFixed(1)}`;
    })
    .filter((point): point is string => point !== null)
    .join(" ");
  const upstreamPoints = linePoints("upstreamCost");
  const managementAccountPoints = linePoints("managementAccountCost");
  const actualIncomePoints = linePoints("actualIncome");
  const marginPoints = series
    .map((point, index) => {
      const y = yForMargin(point.profitMargin);
      return y === null ? null : `${xFor(index).toFixed(1)},${y.toFixed(1)}`;
    })
    .filter((point): point is string => point !== null)
    .join(" ");
  const zeroY = yFor(0) ?? padding.top + chartHeight;
  const barWidth = Math.max(3, Math.min(16, chartWidth / Math.max(series.length, 1) * 0.44));
  const labelIndexes = usageHistoryLabelIndexes(series.length);
  const gridValues = [0, 0.25, 0.5, 0.75, 1];

  return (
    <section className="api-key-usage-history-chart" aria-label={title}>
      <div className="api-key-usage-history-chart-head">
        <div><ChartNoAxesCombined size={16} /><strong>{title}</strong></div>
        <div aria-label="图表显示项" className="api-key-usage-history-chart-legend" role="group">
          <button aria-pressed={visibleSeries.profitMargin} className={visibleSeries.profitMargin ? "" : "is-hidden"} onClick={() => toggleSeries("profitMargin")} type="button"><i className="api-key-usage-history-line-key api-key-usage-history-line-key--margin" />利润率（右轴）</button>
          <button aria-pressed={visibleSeries.upstreamCost} className={visibleSeries.upstreamCost ? "" : "is-hidden"} onClick={() => toggleSeries("upstreamCost")} type="button"><i className="api-key-usage-history-line-key api-key-usage-history-line-key--upstream" />上游实际成本</button>
          <button aria-pressed={visibleSeries.managementAccountCost} className={visibleSeries.managementAccountCost ? "" : "is-hidden"} onClick={() => toggleSeries("managementAccountCost")} type="button"><i className="api-key-usage-history-line-key api-key-usage-history-line-key--management" />管理站点账号成本</button>
          <button aria-pressed={visibleSeries.actualIncome} className={visibleSeries.actualIncome ? "" : "is-hidden"} onClick={() => toggleSeries("actualIncome")} type="button"><i className="api-key-usage-history-line-key api-key-usage-history-line-key--income" />实际收入</button>
          <button aria-pressed={visibleSeries.profit} className={visibleSeries.profit ? "" : "is-hidden"} onClick={() => toggleSeries("profit")} type="button"><i className="api-key-usage-history-bar-key" />利润</button>
        </div>
      </div>
      <div className="api-key-usage-history-chart-viewport">
        <svg aria-hidden="true" className="api-key-usage-history-chart-y-axis" preserveAspectRatio="none" viewBox={`0 0 ${axisWidth} ${height}`}>
          {gridValues.map((fraction) => {
            const y = padding.top + chartHeight - chartHeight * fraction;
            const value = yMinimum + yRange * fraction;
            return (
              <text className="api-key-usage-history-chart-axis" key={fraction} textAnchor="end" x={axisWidth - 8} y={y + 4}>
                {formatHistoryChartNumber(value)}
              </text>
            );
          })}
        </svg>
        <div className="api-key-usage-history-chart-scroll">
          <svg preserveAspectRatio="none" role="img" style={{ width: `${horizontalZoom * 100}%` }} viewBox={`0 0 ${zoomedWidth} ${height}`}>
            <title>{title}</title>
            {gridValues.map((fraction) => {
              const y = padding.top + chartHeight - chartHeight * fraction;
              return <line className="api-key-usage-history-chart-grid" key={fraction} x1={padding.left} x2={zoomedWidth - padding.right} y1={y} y2={y} />;
            })}
            <line className="api-key-usage-history-chart-zero" x1={padding.left} x2={zoomedWidth - padding.right} y1={zeroY} y2={zeroY} />
            {visibleSeries.profit ? series.map((point, index) => {
              const profitY = yFor(point.profit);
              if (profitY === null) return null;
              return (
                <rect
                  className={"api-key-usage-history-profit-bar" + (point.profit !== null && point.profit < 0 ? " is-negative" : "")}
                  height={Math.max(1, Math.abs(zeroY - profitY))}
                  key={`profit:${point.date}`}
                  width={barWidth}
                  x={xFor(index) - barWidth / 2}
                  y={Math.min(zeroY, profitY)}
                >
                  <title>{`${point.date} 利润 ${formatHistorySignedAmount(point.profit, "CNY")}`}</title>
                </rect>
              );
            }) : null}
            {visibleSeries.upstreamCost && upstreamPoints ? <polyline className="api-key-usage-history-chart-line api-key-usage-history-chart-line--upstream" points={upstreamPoints} /> : null}
            {visibleSeries.managementAccountCost && managementAccountPoints ? <polyline className="api-key-usage-history-chart-line api-key-usage-history-chart-line--management" points={managementAccountPoints} /> : null}
            {visibleSeries.actualIncome && actualIncomePoints ? <polyline className="api-key-usage-history-chart-line api-key-usage-history-chart-line--income" points={actualIncomePoints} /> : null}
            {visibleSeries.profitMargin && marginPoints ? <polyline className="api-key-usage-history-chart-line api-key-usage-history-chart-line--margin" points={marginPoints} /> : null}
            {series.map((point, index) => {
              const x = xFor(index);
              const upstreamY = yFor(point.upstreamCost);
              const managementAccountY = yFor(point.managementAccountCost);
              const actualIncomeY = yFor(point.actualIncome);
              const marginY = yForMargin(point.profitMargin);
              return (
                <g key={point.date}>
                  {visibleSeries.upstreamCost && upstreamY !== null ? <circle className="api-key-usage-history-chart-dot api-key-usage-history-chart-dot--upstream" cx={x} cy={upstreamY} r={2.7}><title>{`${point.date} 上游实际成本 ${formatHistoryAmount(point.upstreamCost, "CNY")}`}</title></circle> : null}
                  {visibleSeries.managementAccountCost && managementAccountY !== null ? <circle className="api-key-usage-history-chart-dot api-key-usage-history-chart-dot--management" cx={x} cy={managementAccountY} r={2.7}><title>{`${point.date} 管理站点账号成本 ${formatHistoryAmount(point.managementAccountCost, "CNY")}`}</title></circle> : null}
                  {visibleSeries.actualIncome && actualIncomeY !== null ? <circle className="api-key-usage-history-chart-dot api-key-usage-history-chart-dot--income" cx={x} cy={actualIncomeY} r={2.7}><title>{`${point.date} 实际收入 ${formatHistoryAmount(point.actualIncome, "CNY")}`}</title></circle> : null}
                  {visibleSeries.profitMargin && marginY !== null ? <circle className="api-key-usage-history-chart-dot api-key-usage-history-chart-dot--margin" cx={x} cy={marginY} r={2.7}><title>{`${point.date} 利润率 ${formatHistoryPercent(point.profitMargin)}`}</title></circle> : null}
                  {labelIndexes.has(index) ? <text className="api-key-usage-history-chart-axis" textAnchor="middle" x={x} y={height - 10}>{shortUsageHistoryDate(point.date)}</text> : null}
                </g>
              );
            })}
          </svg>
        </div>
        {visibleSeries.profitMargin ? <svg aria-hidden="true" className="api-key-usage-history-chart-margin-axis" preserveAspectRatio="none" viewBox={`0 0 ${axisWidth} ${height}`}>
          {gridValues.map((fraction) => {
            const y = padding.top + chartHeight - chartHeight * fraction;
            const value = marginYMinimum + marginYRange * fraction;
            return (
              <text className="api-key-usage-history-chart-axis" key={fraction} textAnchor="start" x={8} y={y + 4}>
                {formatHistoryPercent(value)}
              </text>
            );
          })}
        </svg> : <div aria-hidden="true" className="api-key-usage-history-chart-margin-axis" />}
      </div>
      {maxHorizontalZoom > 1 ? (
        <div className="api-key-usage-history-chart-zoom">
          <ZoomOut aria-hidden="true" size={14} />
          <input
            aria-label={`${title} 横向缩放`}
            max={maxHorizontalZoom}
            min="1"
            onChange={(event) => setHorizontalZoom(Number(event.target.value))}
            step="0.25"
            title="横向缩放"
            type="range"
            value={horizontalZoom}
          />
          <ZoomIn aria-hidden="true" size={14} />
        </div>
      ) : null}
    </section>
  );
}

function UpstreamMonitorCard({
  displayTimeZone,
  monitor,
}: {
  displayTimeZone: string;
  monitor: UpstreamMonitor;
}) {
  const extraModels = monitor.extra_models || [];
  const timeline = recentUpstreamMonitorTimeline(monitor.timeline);
  const intrinsicStatus = latestUpstreamMonitorStatus(monitor.primary_status, monitor.timeline);
  const currentProbe = monitorCurrentProbe(monitor);
  const latestProbeAt = timeline.length
    ? timeline[timeline.length - 1].checked_at || timeline[timeline.length - 1].time
    : null;
  return (
    <article className="api-key-monitor-card">
      <header>
        <div>
          <strong>{monitor.name || `上游 #${monitor.id}`}</strong>
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
  upstream,
  upstreamMonitorFallbackTestModels,
  displayTimeZone,
  globallyDisabled,
  onConfigure,
  onDelete,
  onPriorityIntervalChange,
  onPriorityTieMove,
  onShowUpstream,
  onShowUsageHistory,
  onTestAvailability,
  onForceConnectionTest,
  onToggle,
  priorityIntervals,
  priorityTieMove,
  rateWritesEnabled,
}: {
  account: ApiAccount;
  busyAction?: string;
  upstream: Upstream | null;
  upstreamMonitorFallbackTestModels: string[];
  displayTimeZone: string;
  globallyDisabled: boolean;
  onConfigure: () => void;
  onDelete: () => void;
  onPriorityIntervalChange: (intervalId: number | string | null) => void;
  onPriorityTieMove: (direction: "up" | "down") => void;
  onShowUpstream: () => void;
  onShowUsageHistory: () => void;
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
  const usesFallbackConnectionTest = !upstream || account.availability_monitor_id == null;
  const current = finiteNumber(account.management_billing_multiplier);
  const groupMultiplier = finiteNumber(account.upstream_group_multiplier);
  const normalizedMultiplier = accountCompositeMultiplier(account);
  const groupMultiplierTitle = groupMultiplier === null
    ? ""
    : normalizedMultiplier === null
      ? "上游原始分组倍率 " + formatMultiplier(groupMultiplier)
      : "上游原始分组倍率 " + formatMultiplier(groupMultiplier)
        + "；1:1 折算 " + formatMultiplier(normalizedMultiplier)
        + "（上游分组倍率 × 上游充值倍率）";
  const enabled = account.remote_schedulable === true;
  const disabled = account.remote_schedulable === false;
  const schedulingStatus = enabled ? "enabled" : disabled ? "disabled" : "not_checked";
  const currentRateLabel = formatMultiplier(current);
  const upstreamActualMultiplierLabel = formatMultiplier(normalizedMultiplier);
  const rechargeMultiplierLabel = formatMultiplier(account.upstream_recharge_multiplier);
  const upstreamActualMultiplierTitle = normalizedMultiplier === null
    ? "上游实际倍率暂不可用：缺少上游充值倍率或分组倍率"
    : `上游实际倍率为 ${upstreamActualMultiplierLabel}（上游分组倍率 ${formatMultiplier(groupMultiplier)} × 上游充值倍率 ${rechargeMultiplierLabel}）`;
  const managementBillingMultiplierTitle = `管理站点当前配置的账号计费倍率为 ${currentRateLabel}`;
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
    ? "等待上游实际倍率"
    : account.priority_sync_error
      ? "优先级同步失败"
      : !hasPriorityInterval
        ? "未选定区间"
        : priorityPending
          ? "等待写入"
          : priorityStatusLabel(account.priority_sync_status);
  const consumptionCny = finiteNumber(account.today_consumption_cny);
  const upstreamCostCny = finiteNumber(account.today_upstream_actual_cost_cny);
  const managementAccountCostCny = finiteNumber(account.today_management_account_cost_cny);
  const incomeCny = finiteNumber(account.today_actual_income_cny);
  const lastUsedAt = account.last_used_at;
  const usageTitle = [
    `上游实际成本 ${formatHistoryAmount(upstreamCostCny, "CNY")}`,
    `管理站点账号成本 ${formatHistoryAmount(managementAccountCostCny, "CNY")}`,
    account.today_upstream_usage_status === "stale"
      || account.today_management_site_stats_status === "stale"
      ? "本轮未确认，显示当天最后一次有效值"
      : null,
  ].filter(Boolean).join(" · ");
  const activePauseHolds = accountActivePauseHolds(account);
  const hasAccountMeta = identityBlocked;
  return (
    <article className={"api-key-account-card" + (disabled ? " api-key-account-card--disabled" : "")}>
      <header className="api-key-account-card-head">
        <div className="api-key-account-title-line">
          <div className="api-key-account-name">
            <strong title={accountDisplayName(account)}>{accountDisplayName(account)}</strong>
            <span className="api-key-mono">#{account.management_account_id}</span>
          </div>
          <div className="api-key-account-side-chips">
            <AccountAvailabilityIndicator
              account={account}
              activePauseHolds={activePauseHolds}
              upstream={upstream || undefined}
              upstreamMonitorFallbackTestModels={upstreamMonitorFallbackTestModels}
              displayTimeZone={displayTimeZone}
            />
            {account.remote_platform?.trim() ? (
              <PlatformChip
                account={account}
                activePauseHolds={activePauseHolds}
                displayTimeZone={displayTimeZone}
                platform={account.remote_platform}
                status={schedulingStatus}
              />
            ) : null}
            <button
              aria-label={"从管理站点删除 " + accountDisplayName(account)}
              className="api-key-account-delete-button"
              disabled={busy || identityBlocked}
              onClick={onDelete}
              title="删除 管理站点 API 账号"
              type="button"
            >
              <X size={18} strokeWidth={2.4} />
            </button>
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
          <span className="api-key-account-group-label-text">上游分组</span>
          <AccountMonitorBindingTag account={account} upstream={upstream || undefined} />
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
        <div className="api-key-account-usage" title={usageTitle}>
          <span><small>消耗</small><strong>{formatHistoryAmount(consumptionCny, "CNY")}</strong></span>
          <span><small>实际收入</small><strong>{formatHistoryAmount(incomeCny, "CNY")}</strong></span>
          <span><small>最近</small><strong>{lastUsedAt ? formatDate(lastUsedAt, displayTimeZone) : "尚未使用"}</strong></span>
        </div>
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
            <PriorityParticipationTag account={account} />
            <span className="api-key-account-priority-number">
              <strong>{formatPriority(priority)}</strong>
              {priorityPending ? <><ArrowRight size={13} /><b>{formatPriority(desiredPriority)}</b></> : null}
            </span>
          </span>
          <div className="api-key-inline-chips api-key-account-rate-chips">
            <RateChip label="上游实际倍率" value={upstreamActualMultiplierLabel} title={upstreamActualMultiplierTitle} tone="combined" />
            <RateChip label="管理站点账号计费倍率" pending={account.would_change === true} value={currentRateLabel} title={managementBillingMultiplierTitle} tone="current" />
          </div>
        </div>
      </div>
      <footer className="api-key-account-card-actions">
        {priorityTieMove ? (
          <span className="api-key-priority-tie-controls" aria-label="同倍率账号优先级排位">
            <button
              aria-label={`提高 ${accountDisplayName(account)} 的优先级数值`}
              disabled={busy || !priorityTieMove.canMoveUp}
              onClick={() => onPriorityTieMove("up")}
              title="与后一个同区间、同上游实际倍率账号互换优先级"
              type="button"
            >
              <ArrowUp size={13} />
            </button>
            <button
              aria-label={`降低 ${accountDisplayName(account)} 的优先级数值`}
              disabled={busy || !priorityTieMove.canMoveDown}
              onClick={() => onPriorityTieMove("down")}
              title="与前一个同区间、同上游实际倍率账号互换优先级"
              type="button"
            >
              <ArrowDown size={13} />
            </button>
          </span>
        ) : null}
        <button
          aria-label={`测试 ${accountDisplayName(account)} 的可用性`}
          className="api-key-icon-button"
          disabled={busy || identityBlocked || !upstream || account.availability_check_mode === "disabled"}
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
            : "直接调用管理站点连接测试接口；不会读取监控面板，也不会改变自动暂停、恢复或调度状态"}
          type="button"
        >
          {busyAction === "connection-test" ? <RefreshCcw className="spin" size={15} /> : <PlugZap size={15} />}
        </button>
        <button
          aria-label={`查看 ${accountDisplayName(account)} 的统计数据`}
          className="api-key-icon-button"
          disabled={!upstream}
          onClick={onShowUsageHistory}
          title={upstream ? "查看 API 账号统计数据" : "未分配上游"}
          type="button"
        >
          <ChartNoAxesCombined size={15} />
        </button>
        <button
          aria-label={upstream ? `查看 ${accountDisplayName(account)} 的上游` : `${accountDisplayName(account)} 未分配上游`}
          className="api-key-icon-button"
          disabled={!upstream}
          onClick={onShowUpstream}
          title={upstream ? "查看上游" : "未分配上游"}
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
      </footer>
    </article>
  );
}

function PriorityParticipationTag({ account }: { account: ApiAccount }) {
  const participates = account.priority_assignment_when_disabled_effective === true;
  const setting = account.priority_assignment_when_disabled === true
    ? "此账号强制参与"
    : account.priority_assignment_when_disabled === false
      ? "此账号强制排除"
      : "继承全局设置";
  return (
    <HelpPopover
      label="查看停用后优先级计算设置"
      trigger={
        <span className={
          "api-key-chip api-key-account-priority-participation "
          + (participates ? "api-key-chip--success" : "api-key-chip--muted")
        }>
          {participates ? "停用后参与" : "停用后排除"}
        </span>
      }
      triggerClassName="help-popover-trigger--content"
    >
      <PopoverDetails rows={[
        ["账号停用后", participates ? "参与优先级计算" : "不参与优先级计算"],
        ["配置来源", setting],
      ]} />
    </HelpPopover>
  );
}

function AccountAvailabilityIndicator({
  account,
  activePauseHolds,
  upstream,
  upstreamMonitorFallbackTestModels,
  displayTimeZone,
}: {
  account: ApiAccount;
  activePauseHolds: ApiAccountPauseHold[];
  upstream?: Upstream;
  upstreamMonitorFallbackTestModels: string[];
  displayTimeZone: string;
}) {
  const status = String(account.availability_status || "").trim().toLowerCase();
  const mode = account.availability_check_mode || "disabled";
  const source = String(account.availability_source || "").trim().toLowerCase();
  const monitoringUnconfigured = mode === "disabled";
  const monitoringGloballyDisabled = !monitoringUnconfigured && status === "disabled";
  const selectedMonitor = account.availability_monitor_id == null
    ? null
    : (upstream?.upstream_monitors || []).find(
        (monitor) => String(monitor.id) === String(account.availability_monitor_id),
      );
  const selectedMonitorStatus = selectedMonitor
    ? latestUpstreamMonitorStatus(selectedMonitor.primary_status, selectedMonitor.timeline)
    : null;
  const upstreamMonitorStatus = normalizeMonitorStatus(upstream?.upstream_monitor_status);
  const monitorDegraded = mode === "upstream_monitor"
    && (selectedMonitorStatus === "degraded" || upstreamMonitorStatus === "degraded");
  const available = status === "available" || monitorDegraded;
  const unavailable = status === "unavailable" && !monitorDegraded;
  const otherPauseReasons = activePauseHolds
    .filter((hold) => hold.reason !== "upstream_monitor_unavailable")
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
    : source === "upstream_monitor"
      ? "绑定监控面板判定"
      : source === "upstream_monitor_fallback"
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
  const monitorBindingMissing = account.availability_check_mode === "upstream_monitor"
    && account.availability_monitor_id == null;
  const monitorWasDeleted = account.availability_check_mode === "upstream_monitor"
    && account.availability_monitor_id != null
    && upstreamMonitorStatus === "available"
    && !selectedMonitor;
  const monitorAvailable = mode === "upstream_monitor"
    && upstreamMonitorStatus === "available"
    && [
    "available",
    "healthy",
    "operational",
    "ok",
    "success",
  ].includes(selectedMonitorStatus || "");
  const chosenModel = account.availability_test_model?.trim()
    || upstreamMonitorFallbackTestModels.find((model) =>
      (account.available_models || []).some((availableModel) => availableModel.id === model))
    || null;
  const fallbackChainHasNoAccountModel = mode !== "disabled"
    && !account.availability_test_model?.trim()
    && upstreamMonitorFallbackTestModels.length > 0
    && !chosenModel;
  const bindingText = account.availability_check_mode !== "upstream_monitor"
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
    : mode !== "upstream_monitor"
      ? null
      : selectedMonitorStatus
        ? upstreamStatusLabel(selectedMonitorStatus)
        : monitorWasDeleted
          ? "原绑定面板已删除"
          : monitorBindingMissing
            ? "未绑定监控面板"
            : upstreamStatusLabel(upstream?.upstream_monitor_status || "unknown");
  const automaticMonitoringText = monitoringUnconfigured
    ? "未配置"
    : monitoringGloballyDisabled
      ? "全局自动检测已关闭"
    : automaticMonitoringPaused
      ? `已暂停；手动检测仍可用${otherPauseReasons.length ? `（${otherPauseReasons.join("、")}）` : ""}`
      : "运行中";
  const monitorUnbound = monitorBindingMissing;
  const monitorUnknown = mode === "upstream_monitor"
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
    ].includes(String(upstream?.upstream_monitor_status || "").trim().toLowerCase());
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
  const resultTone = available ? "available" : unavailable ? "unavailable" : "unknown";
  const trigger = (
    <svg
      aria-hidden="true"
      className={`api-key-availability-icon api-key-availability-icon--${indicatorTone} api-key-availability-icon--${resultTone}`}
      shapeRendering="geometricPrecision"
      viewBox="0 0 24 24"
    >
      <circle className="api-key-availability-icon-ring" cx="12" cy="12" r="11.5" />
      <circle className="api-key-availability-icon-core" cx="12" cy="12" r="6" />
    </svg>
  );
  return (
    <HelpPopover
      label={`查看 ${accountDisplayName(account)} 的可用性监测详情`}
      trigger={trigger}
      triggerClassName="api-key-availability-indicator"
    >
      <PopoverDetails
        rows={[
          ["自动检测", automaticMonitoringText],
          ["最近检测结果", statusText],
          ["检测方式", sourceText],
          ["监控面板", bindingText],
          ["监控面板状态", monitorStatusText],
          ["当前回退候选模型", source === "upstream_monitor_fallback" && !monitorDegraded ? chosenModel : null],
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

function AccountMonitorBindingTag({
  account,
  upstream,
}: {
  account: ApiAccount;
  upstream?: Upstream;
}) {
  const mode = account.availability_check_mode || "disabled";
  const selectedMonitor = account.availability_monitor_id == null
    ? null
    : (upstream?.upstream_monitors || []).find(
        (monitor) => String(monitor.id) === String(account.availability_monitor_id),
      );
  const selectedMonitorStatus = selectedMonitor
    ? latestUpstreamMonitorStatus(selectedMonitor.primary_status, selectedMonitor.timeline)
    : "";
  const upstreamStatus = normalizeMonitorStatus(upstream?.upstream_monitor_status);
  const monitorDeleted = mode === "upstream_monitor"
    && account.availability_monitor_id != null
    && upstreamStatus === "available"
    && !selectedMonitor;
  const healthyStatuses = ["available", "healthy", "operational", "ok", "success"];
  const unavailableStatuses = ["unavailable", "error", "failed", "timeout", "invalid"];
  const monitorName = selectedMonitor?.name?.trim()
    || (account.availability_monitor_id == null ? null : `#${account.availability_monitor_id}`);
  const monitorStatus = selectedMonitorStatus
    ? upstreamStatusLabel(selectedMonitorStatus)
    : monitorDeleted
      ? "已删除"
      : account.availability_monitor_id == null
        ? "未绑定"
        : "等待同步";
  const tone = mode === "disabled"
    || mode === "independent_model"
    || account.availability_monitor_id == null
    ? "muted"
    : monitorDeleted
      || unavailableStatuses.includes(selectedMonitorStatus)
      || unavailableStatuses.includes(upstreamStatus)
      ? "danger"
      : healthyStatuses.includes(selectedMonitorStatus) && upstreamStatus === "available"
        ? "success"
        : "warn";
  const label = mode === "disabled"
    ? "监控面板：未启用"
    : mode === "independent_model"
      ? "监控面板：独立模型"
      : account.availability_monitor_id == null
        ? "监控面板：未绑定"
        : monitorDeleted
          ? "监控面板：已删除"
          : `监控面板：${middleEllipsis(monitorName || "等待同步", 14)}`;
  return (
    <HelpPopover
      label={`查看 ${accountDisplayName(account)} 的监控面板绑定`}
      trigger={<span>{label}</span>}
      triggerClassName={`api-key-account-monitor-tag api-key-chip api-key-chip--${tone} help-popover-trigger--content`}
    >
      <PopoverDetails rows={[
        ["检测方式", mode === "upstream_monitor" ? "绑定监控面板" : mode === "independent_model" ? "独立模型测试" : "未启用"],
        ["绑定监控面板", mode === "upstream_monitor" ? monitorName || "未绑定" : null],
        ["面板当前状态", mode === "upstream_monitor" ? monitorStatus : null],
        ["具体说明", monitorDeleted
          ? "原绑定的监控面板已被上游删除，请重新绑定。"
          : account.availability_monitor_id == null && mode === "upstream_monitor"
            ? "已选择绑定监控面板，但尚未选择具体面板。"
            : null],
      ]} />
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
  hold: ApiAccountPauseHold,
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

function pauseHoldReasonLabel(hold: ApiAccountPauseHold) {
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
    .replace(/All (\d+) fallback connection tests succeeded with model ([^ ]+)\./, "已使用回退模型 $2 完成 $1 次连接测试，且全部成功。")
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

function accountActivePauseHolds(account: ApiAccount): ApiAccountPauseHold[] {
  if (account.active_pause_holds !== undefined) return account.active_pause_holds;
  if (!account.auto_disabled_reason) return [];
  return [{
    reason: account.auto_disabled_reason,
    triggered_at: account.last_auto_disabled_at || account.balance_guard_paused_at,
    recovery_mode: account.balance_guard_restore_eligible ? "automatic" : "manual",
    scope_upstream_id: account.balance_guard_upstream_id ?? account.upstream_id,
  }];
}

function pauseHoldRecoveryLabel(recoveryMode?: string | null) {
  const normalized = String(recoveryMode || "").trim().toLowerCase();
  if ([
    "automatic",
    "auto",
    "balance_positive",
    "balance_at_or_above_threshold",
    "upstream_monitor_recovered",
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
  accounts: ApiAccount[];
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
                    ? `开启 · 上游实际倍率大于 ${formatMultiplier(interval.rate_absolute_threshold)}`
                    : "关闭"}
                </p>
                {interval.allocation_strategy === "fixed_step" && effectiveStep < interval.step ? (
                  <p className="api-key-priority-note">区间空间有限，实际最低间隔已自动缩短为 {effectiveStep}。</p>
                ) : null}
                {sharedPriorityCount ? (
                  <p className="api-key-priority-note is-warning">区间容量不足，至少 {sharedPriorityCount} 个倍率档位会与相邻档位共用优先级。</p>
                ) : null}
                {waitingCount ? (
                  <p className="api-key-priority-note is-waiting">{waitingCount} 个账号等待上游实际倍率，不参与容量和优先级计算。</p>
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
  upstreams,
  displayTimeZone,
  draftFilters,
  error,
  filtersApplied,
  kind,
  loading,
  logs,
  page,
  pageSize,
  pageSizeOptions,
  totalCount,
  onApplyFilters,
  onClearFilters,
  onDraftFiltersChange,
  onPageChange,
  onPageSizeChange,
  onRefresh,
}: {
  upstreams: Upstream[];
  displayTimeZone: string;
  draftFilters: RateLogFilters;
  error: string;
  filtersApplied: boolean;
  kind: "upstream" | "account_rate";
  loading: boolean;
  logs: UpstreamChangeEvent[];
  page: number;
  pageSize: number;
  pageSizeOptions: number[];
  totalCount: number;
  onApplyFilters: () => void;
  onClearFilters: () => void;
  onDraftFiltersChange: (filters: RateLogFilters) => void;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  onRefresh: () => void;
}) {
  const accountRateView = kind === "account_rate";
  const rechargeMultiplierByUpstream = useMemo(() => {
    const result = new Map<string, number>();
    for (const upstream of upstreams) {
      const multiplier = finiteNumber(upstream.upstream_recharge_multiplier);
      if (multiplier !== null) result.set(String(upstream.upstream_id), multiplier);
    }
    return result;
  }, [upstreams]);
  return (
    <section className="api-key-panel api-key-rate-log-panel" aria-label={accountRateView ? "API 账号倍率变化记录" : "上游分组变化记录"}>
      <div className="api-key-panel-head">
        <div>
          <h2>{accountRateView ? "API 账号倍率变化" : "上游分组变化"}</h2>
          {/* 记录上游充值倍率、分组倍率、名称与可用性，以及 API 账号实际倍率变化 */}
          <p>{accountRateView ? "只记录 API 账号实际计费倍率变化，并标注其对应上游。" : "记录上游分组的存在性、倍率、名称与可用性。"}</p>
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
          <span>{accountRateView ? "正在读取 API 账号倍率变化…" : "正在读取上游分组变化…"}</span>
        </div>
      ) : logs.length === 0 ? (
        <div className="api-key-empty">
          <History size={18} />
          <span>{filtersApplied
            ? accountRateView ? "当前日期范围内没有 API 账号倍率变化" : "当前日期范围内没有上游分组变化"
            : accountRateView ? "暂无 API 账号倍率变化记录" : "暂无上游分组变化记录"}</span>
        </div>
      ) : (
        <div className="api-key-rate-log-list">
          {logs.map((log) => {
            const multiplierEvent = log.event_type === "upstream_recharge_multiplier_changed"
              || log.event_type === "group_multiplier_changed"
              || log.event_type === "account_rate_changed";
            const nameEvent = log.event_type === "group_name_changed";
            const keyStatusEvent = log.event_type === "upstream_key_status_changed";
            const groupStatusEvent = log.event_type === "upstream_group_status_changed";
            const groupAddedEvent = log.event_type === "group_added";
            const groupRate = upstreamGroupRatePresentation(
              log,
              rechargeMultiplierByUpstream.get(String(log.upstream_id)),
            );
            const oldName = typeof log.details?.old_name === "string" ? log.details.old_name : null;
            const newName = typeof log.details?.new_name === "string" ? log.details.new_name : null;
            const accountName = typeof log.details?.account_name === "string" ? log.details.account_name : null;
            const category = upstreamChangeCategory(log);
            const subjectLabel = category.tone === "account"
              ? "API 账号"
              : category.tone === "group"
                ? "上游分组"
                : "上游配置";
            const subjectName = category.tone === "account"
              ? accountName || log.group_name || `#${log.group_id || "-"}`
              : category.tone === "group"
                ? log.group_name || `#${log.group_id || "-"}`
                : log.event_type === "upstream_recharge_multiplier_changed" ? "充值倍率" : "状态";
            const groupMultiplierValue = groupRate.newGroupMultiplier ?? groupRate.oldGroupMultiplier;
            const compositeMultiplierValue = groupRate.newCompositeMultiplier ?? groupRate.oldCompositeMultiplier;
            const rateChangeReason = accountRateView ? accountRateChangeReasonLabel(log) : null;
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
                    <span>上游 <b>{log.upstream_name || "#" + (log.upstream_id || "-")}</b></span>
                    <span>{subjectLabel} <b>{subjectName}</b></span>
                  </div>
                </div>
                <div className="api-key-rate-log-cell api-key-rate-log-cell--primary">
                  <div className="api-key-change-message-line api-key-change-message-line--primary">
                    <span>{upstreamChangeEventLabel(log.event_type)}</span>
                    {rateChangeReason ? <span className="api-key-rate-change-reason">原因：{rateChangeReason}</span> : null}
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
                        title={`上游实际倍率 ${formatRateLogMultiplier(compositeMultiplierValue)}`}
                      >
                        <span>上游实际倍率</span>
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

      <ChangeLogPagination
        loading={loading}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
        page={page}
        pageSize={pageSize}
        pageSizeOptions={pageSizeOptions}
        totalCount={totalCount}
      />
    </section>
  );
}

function SchedulingChangeLogView({
  displayTimeZone,
  draftFilters,
  error,
  filtersApplied,
  loading,
  logs,
  page,
  pageSize,
  pageSizeOptions,
  totalCount,
  onApplyFilters,
  onClearFilters,
  onDraftFiltersChange,
  onPageChange,
  onPageSizeChange,
  onRefresh,
}: {
  displayTimeZone: string;
  draftFilters: RateLogFilters;
  error: string;
  filtersApplied: boolean;
  loading: boolean;
  logs: AccountSchedulingChangeEvent[];
  page: number;
  pageSize: number;
  pageSizeOptions: number[];
  totalCount: number;
  onApplyFilters: () => void;
  onClearFilters: () => void;
  onDraftFiltersChange: (filters: RateLogFilters) => void;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  onRefresh: () => void;
}) {
  return (
    <section className="api-key-panel api-key-rate-log-panel api-key-scheduling-log-panel" aria-label="账号调度变化记录">
      <div className="api-key-ledger-sticky">
        <div className="api-key-panel-head">
        <div>
          <h2>账号调度变化</h2>
          <p>记录插件根据余额、上游监控、上游可用性和上游实际倍率策略执行的暂停、恢复及失败。</p>
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
                    <span>上游 <b>{log.upstream_name || "未分配"}</b></span>
                    <span>API 账号 <b>{log.account_name || `#${log.management_account_id}`}</b></span>
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
                      label={`查看 ${log.account_name || `账号 #${log.management_account_id}`} 的暂停原因详情`}
                      trigger={<span className="api-key-scheduling-reason-trigger">{reasonLabel}</span>}
                      triggerClassName="help-popover-trigger--content"
                    >
                      <span className="api-key-status-detail">
                        <strong>{reasonLabel}</strong>
                        <PopoverDetails rows={[
                          ["账号", log.account_name || `#${log.management_account_id}`],
                          ["上游", log.upstream_name || "未分配"],
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

      <ChangeLogPagination
        loading={loading}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
        page={page}
        pageSize={pageSize}
        pageSizeOptions={pageSizeOptions}
        totalCount={totalCount}
      />
    </section>
  );
}

function ChangeLogPagination({
  loading,
  onPageChange,
  onPageSizeChange,
  page,
  pageSize,
  pageSizeOptions,
  totalCount,
}: {
  loading: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  page: number;
  pageSize: number;
  pageSizeOptions: number[];
  totalCount: number;
}) {
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));
  const currentPage = Math.min(Math.max(1, page), totalPages);
  return (
    <nav aria-label="变化记录分页" className="api-key-change-pagination">
      <span className="api-key-change-pagination-total">共 {totalCount} 条</span>
      <div className="api-key-change-pagination-nav">
        <button aria-label="第一页" disabled={loading || currentPage <= 1} onClick={() => onPageChange(1)} title="第一页" type="button">
          <ChevronsLeft size={16} />
        </button>
        <button aria-label="上一页" disabled={loading || currentPage <= 1} onClick={() => onPageChange(currentPage - 1)} title="上一页" type="button">
          <ChevronLeft size={16} />
        </button>
        <span>第 {currentPage} / {totalPages} 页</span>
        <button aria-label="下一页" disabled={loading || currentPage >= totalPages} onClick={() => onPageChange(currentPage + 1)} title="下一页" type="button">
          <ChevronRight size={16} />
        </button>
        <button aria-label="最后一页" disabled={loading || currentPage >= totalPages} onClick={() => onPageChange(totalPages)} title="最后一页" type="button">
          <ChevronsRight size={16} />
        </button>
      </div>
      <label className="api-key-change-page-size">
        <span>每页</span>
        <select
          aria-label="每页展示条数"
          disabled={loading}
          onChange={(event) => onPageSizeChange(normalizeChangeLogPageSize(Number(event.target.value), pageSizeOptions))}
          value={pageSize}
        >
          {pageSizeOptions.map((size) => <option key={size} value={size}>{size} 条</option>)}
        </select>
      </label>
    </nav>
  );
}

export function accountRateChangeReasonLabel(log: UpstreamChangeEvent) {
  const reason = String(log.details?.reason || "").trim();
  const transitionReason = String(log.details?.transition_reason || "").trim();
  const labels: Record<string, string> = {
    upstream_group_change: "上游分组倍率变化",
    upstream_group_assignment_change: "修改上游分组",
    upstream_group_name_change: "上游分组名称变化",
    upstream_recharge_change: "上游充值倍率变化",
    management_recharge_change: "管理站点充值倍率变化",
    expected_billing_multiplier_recalculated: "预期账号计费倍率重新计算",
    upstream_key_status_change: "上游令牌状态变化",
    upstream_key_recovered: "上游令牌恢复",
    upstream_group_status_change: "上游分组状态变化",
    upstream_group_recovered: "上游分组恢复",
    upstream_auto_disable: "上游异常自动暂停",
    automatic_pause_restored: "上游恢复后自动恢复",
    rate_drift: "账号倍率被外部修改",
    external_observed: "账号倍率被外部修改",
    automatic_apply: "历史记录未记录具体原因",
  };
  if (labels[reason]) return labels[reason];
  if (labels[transitionReason]) return labels[transitionReason];
  return reason || transitionReason || "历史记录未记录具体原因";
}

function upstreamChangeEventLabel(eventType: UpstreamChangeEvent["event_type"]) {
  return ({
    upstream_recharge_multiplier_changed: "上游充值倍率变化",
    group_multiplier_changed: "上游分组倍率变化",
    group_removed: "上游分组已删除",
    group_added: "上游分组已出现",
    group_name_changed: "上游分组名称变化",
    account_rate_changed: "API 账号计费倍率变化",
    upstream_key_status_changed: "上游 Key 状态变化",
    upstream_group_status_changed: "上游分组状态变化",
  } as const)[eventType];
}

function upstreamChangeCategory(log: UpstreamChangeEvent) {
  if (log.event_type === "account_rate_changed") return { label: "API 账号", tone: "account" } as const;
  if (["group_multiplier_changed", "group_removed", "group_added", "group_name_changed", "upstream_group_status_changed"].includes(log.event_type)) {
    return { label: "上游分组", tone: "group" } as const;
  }
  return { label: "上游", tone: "channel" } as const;
}

function upstreamGroupEventStatusLabel(
  status?: string | null,
  eventType?: UpstreamChangeEvent["event_type"],
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
  if (log.reason === "upstream_monitor_unavailable") {
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
        ? `观测上游实际倍率 ${formatRateLogMultiplier(observedMultiplier)}`
        : `观测上游实际倍率 ${formatRateLogMultiplier(observedMultiplier)}，阈值 ${formatRateLogMultiplier(absoluteThreshold)}`;
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

function UpstreamStat({
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

function UpstreamAddressBox({ label, url }: { label: "站点" | "API"; url: string }) {
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

function UpstreamBalanceSummary({ upstreams }: { upstreams: Upstream[] }) {
  return (
    <section aria-label="上游余额汇总" className="api-key-balance-summary">
      <div className="api-key-balance-summary-head">
        <span>上游余额汇总</span>
        <small>{upstreams.length} 个上游</small>
      </div>
      <div className="api-key-balance-summary-list">
        {upstreams.length ? upstreams.map((upstream) => (
          <UpstreamBalanceCard upstream={upstream} key={String(upstream.upstream_id)} />
        )) : <span className="api-key-muted">暂无已分配上游</span>}
      </div>
    </section>
  );
}

function UpstreamBalanceCard({ upstream }: { upstream: Upstream }) {
  const configuredName = upstream.display_name?.trim() || "";
  const managementUrl = upstream.management_url?.trim() || upstreamBaseUrl(upstream);
  const displayName = configuredName || displayFullUrl(managementUrl) || "未配置地址";
  const isUrlLabel = urlLikeDisplayName(displayName, managementUrl)
    || urlLikeDisplayName(displayName, upstreamBaseUrl(upstream));
  const platformBalance = formatCurrentPlatformBalance(upstream);
  const adjustedBalance = formatCurrentRechargeAdjustedBalance(upstream);
  const rechargeMultiplier = finiteNumber(upstream.upstream_recharge_multiplier);
  const platformBalanceNote = hasCurrentPlatformBalance(upstream)
    ? `钱包余额：${platformBalance}`
    : hasCachedPlatformBalance(upstream)
      ? `上次成功读取的钱包余额：${platformBalance}`
      : "钱包余额：当前没有可信的上游余额";
  const adjustedBalanceNote = adjustedBalance === "—"
    ? "实际余额：当前无法按上游充值倍率计算"
    : `实际余额：钱包余额 × 充值倍率${rechargeMultiplier === null ? "" : ` ${rechargeMultiplier.toLocaleString("zh-CN", { maximumFractionDigits: 6 })}`} = ${adjustedBalance}`;
  const balanceNote = `${platformBalanceNote}；${adjustedBalanceNote}`;
  const todayUsage = formatBalanceSummaryTodayUsage(upstream);
  const todayUsageNote = `今日实际消耗：${todayUsage.value}${todayUsage.stale ? "（显示当天最后一次有效值）" : ""}`;

  return (
    <article className="api-key-balance-channel-card">
      <BalanceManagementLink
        className="api-key-balance-channel-name api-key-balance-channel-name--link"
        isUrlLabel={isUrlLabel}
        label={displayName}
        url={managementUrl}
      />
      <div aria-label={balanceNote} className="api-key-balance-metric" title={balanceNote}>
        <small>余额</small>
        <strong><span>钱包 {platformBalance}</span><span>实际 {adjustedBalance}</span></strong>
      </div>
      <div aria-label={todayUsageNote} className="api-key-balance-metric api-key-balance-today-usage" title={todayUsageNote}>
        <small>今日实际消耗</small>
        <strong>{todayUsage.value}</strong>
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
    </div>
  );
}

function StatusChip({ status }: { status?: string | null }) {
  const value = String(status || "unknown").trim().toLowerCase();
  return <span className={"api-key-chip api-key-chip--" + statusTone(value)}>{statusLabel(value)}</span>;
}

function PlatformChip({
  account,
  activePauseHolds,
  displayTimeZone,
  platform,
  status,
}: {
  account: ApiAccount;
  activePauseHolds: ApiAccountPauseHold[];
  displayTimeZone: string;
  platform: string;
  status?: string;
}) {
  const value = platform.trim();
  const schedulingStatus = String(status || "not_checked").trim().toLowerCase();
  const tone = schedulingStatus === "enabled"
    ? "success"
    : schedulingStatus === "disabled"
      ? "danger"
      : "info";
  const disabled = schedulingStatus === "disabled";
  return (
    <HelpPopover
      label={`查看 ${accountDisplayName(account)} 的账号状态详情`}
      trigger={<span>{value}</span>}
      triggerClassName={`api-key-platform-chip api-key-chip api-key-chip--${tone} help-popover-trigger--content`}
    >
      <span className="api-key-status-detail">
        <strong>{disabled ? "账号已停用" : schedulingStatus === "enabled" ? "账号已启用" : "账号状态待确认"}</strong>
        <PopoverDetails rows={[
          ["平台", value],
          ["调度状态", statusLabel(schedulingStatus)],
        ]} />
        {disabled ? activePauseHolds.length ? activePauseHolds.map((hold, index) => (
          <span className="api-key-status-detail-reason" key={`${hold.reason}:${index}`}>
            <strong>{pauseHoldReasonLabel(hold)}</strong>
            <PopoverDetails rows={pauseHoldDetailRows(hold, displayTimeZone)} />
          </span>
        )) : (
          <span>未记录自动暂停原因，账号可能在管理站点中被手动停用。</span>
        ) : (
          <span>{schedulingStatus === "enabled" ? "账号当前可参与调度。" : "等待下一次账号状态同步。"}</span>
        )}
      </span>
    </HelpPopover>
  );
}

function upstreamKey(upstream: Upstream) {
  return String(upstream.upstream_id);
}

function accountKey(account: ApiAccount) {
  return String(account.management_account_id);
}

function mergeApiAccountSnapshot(
  data: UpstreamOverviewResponse,
  snapshot: ApiAccount,
): UpstreamOverviewResponse {
  const snapshotKey = accountKey(snapshot);
  let matched = false;
  const mergeAccount = (account: ApiAccount) => {
    if (accountKey(account) !== snapshotKey) return account;
    matched = true;
    return { ...account, ...snapshot };
  };
  const upstreams = data.upstreams.map((upstream) => ({
    ...upstream,
    accounts: upstream.accounts?.map(mergeAccount),
  }));
  const unassignedAccounts = data.unassigned_accounts.map(mergeAccount);
  return matched
    ? { ...data, upstreams: upstreams, unassigned_accounts: unassignedAccounts }
    : data;
}

function upstreamDisplayName(upstream: Upstream) {
  return upstream.display_name?.trim() || displayHost(upstreamBaseUrl(upstream)) || "未命名上游";
}

function accountDisplayName(account: ApiAccount) {
  return account.remote_name?.trim() || "API 账号 #" + account.management_account_id;
}

function upstreamBaseUrl(upstream: Upstream) {
  return upstream.api_endpoint_url?.trim() || upstream.api_endpoint_url?.trim() || "";
}

function upstreamSearchText(upstream: Upstream) {
  return [
    upstream.display_name,
    upstream.api_endpoint_url,
    upstream.api_endpoint_url,
    upstream.management_url,
    upstream.platform_type,
    upstream.resolved_platform_type,
    upstream.upstream_user_id,
    upstream.recharge_multiplier_status,
    upstream.balance_status,
    upstream.status,
    upstream.message,
    upstream.last_error,
    ...(upstream.group_options || []).flatMap((group) => [group.id, group.name]),
  ].filter(Boolean).join(" ").toLowerCase();
}

function accountSearchText(account: ApiAccount) {
  return [
    account.management_account_id,
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

function matchesUpstreamStatus(upstream: Upstream, filter: UpstreamStatusFilter) {
  if (filter === "all") return true;
  if (filter === "attention") return upstreamHasAttention(upstream);
  if (filter === "undiscovered") return !upstream.last_discovered_at;
  return (upstream.accounts || []).some((account) => account.would_change === true);
}

function upstreamHasAttention(upstream: Upstream) {
  if (upstream.last_error || (!upstream.access_token_set && !upstream.login_credentials_set)) return true;
  if (
    resolvedUpstreamType(upstream) === "sub2api"
    && !upstream.refresh_token_set
    && !upstream.login_credentials_set
  ) return true;
  return [upstream.status, upstream.balance_status, upstream.recharge_multiplier_status]
    .filter(Boolean)
    .some((status) => isFailureStatus(status));
}

function upstreamStatus(upstream: Upstream) {
  if (upstreamTokenInvalid(upstream)) return "token_invalid";
  if (upstream.last_error) return "discovery_failed";
  return upstream.status || upstream.balance_status || upstream.recharge_multiplier_status || "not_checked";
}

function resolvedUpstreamType(upstream: Upstream) {
  if (upstream.platform_type && upstream.platform_type !== "auto") return upstream.platform_type;
  return upstream.resolved_platform_type || "auto";
}

function upstreamDisplayMessage(upstream: Upstream) {
  if (upstreamTokenInvalid(upstream) || isGenericUpstreamError(upstream.balance_message || upstream.message)) return "";
  const balanceStatus = String(upstream.balance_status || "").trim().toLowerCase();
  const type = resolvedUpstreamType(upstream);
  if (balanceStatus === "credentials_missing") {
    if (type === "newapi" && upstream.access_token_set && !upstream.upstream_user_id) {
      return "缺少数字 New-Api-User ID，暂时无法读取余额";
    }
    return "缺少登录 Access Token，暂时无法读取余额";
  }

  const message = visibleUpstreamBalanceMessage(upstream.balance_message || upstream.message);
  if (/rejected the balance credentials/i.test(message)) {
    return "上游拒绝余额凭据，请检查 Access Token 和用户 ID";
  }
  return message;
}

function upstreamDisplayError(upstream: Upstream) {
  const error = upstream.last_error || "";
  if (!error || error === upstream.balance_message || error === upstream.message) return "";
  if (upstreamTokenInvalid(upstream) || isGenericUpstreamError(error)) return "";
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
    multiplier_unavailable: "等待上游实际倍率",
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
  if (!Number.isFinite(absolute) || absolute <= 0 || absolute > 1000) throw new Error("上游实际倍率阈值必须大于 0 且不超过 1000");
  return absolute;
}

function priorityIntervalAccountCount(interval: PriorityInterval, accounts: ApiAccount[]) {
  const responseCount = finiteNumber(interval.account_count);
  if (responseCount !== null) return responseCount;
  return accounts.filter(
    (account) => String(account.priority_interval_id ?? "") === String(interval.id),
  ).length;
}

function upstreamAccountCount(upstream: Upstream) {
  const responseCount = finiteNumber(upstream.account_count);
  return responseCount === null ? (upstream.accounts || []).length : Math.max(0, Math.trunc(responseCount));
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

function normalizeMonitorStatus(value: unknown) {
  const status = String(value || "").trim().toLowerCase();
  return ({
    active: "available",
    enabled: "available",
    ok: "available",
    success: "available",
  } as Record<string, string>)[status] || status;
}

function monitorCurrentProbe(monitor: UpstreamMonitor) {
  const status = latestUpstreamMonitorStatus(monitor.primary_status, monitor.timeline);
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

function upstreamMonitorMessage(upstream: Upstream) {
  if (upstream.upstream_monitor_status === "not_configured") {
    return "上游未配置公开监控面板。";
  }
  if (upstream.upstream_monitor_status === "unsupported") {
    return "该上游暂不支持公开监控接口。";
  }
  if (upstream.upstream_monitor_status === "ok" && resolvedUpstreamType(upstream) === "newapi") {
    const count = upstream.upstream_monitor_count ?? upstream.upstream_monitors?.length ?? 0;
    return `已读取 ${count} 个 NewAPI 公开监控项。`;
  }
  return upstream.upstream_monitor_message || "上游状态由上游监控接口同步。";
}

function balanceDetail(upstream: Upstream) {
  const details: string[] = [];
  if (!hasCurrentPlatformBalance(upstream)) {
    if (upstream.balance_source === "local_api_key") {
      details.push("仅取得本站 API Key 余额，未取得上游钱包余额");
    }
    return details.join(" · ");
  }
  if (finiteNumber(upstream.wallet_total_usd) !== null) {
    details.push("总额 " + formatUpstreamBalance(upstream.wallet_total_usd, upstream.balance_unit, 2));
  }
  if (resolvedUpstreamType(upstream) !== "newapi" && finiteNumber(upstream.wallet_used_usd) !== null) {
    details.push("上游累计已用 " + formatUpstreamBalance(upstream.wallet_used_usd, upstream.balance_unit, 2));
  }
  return details.join(" · ");
}

function hasCurrentPlatformBalance(upstream: Upstream) {
  const status = String(upstream.balance_status || "").trim().toLowerCase();
  return ["ok", "success", "available"].includes(status)
    && upstream.balance_source === "upstream_wallet"
    && finiteNumber(upstream.wallet_balance_usd) !== null
    && Boolean(upstream.balance_checked_at);
}

function hasCachedPlatformBalance(upstream: Upstream) {
  return upstream.balance_source === "upstream_wallet"
    && finiteNumber(upstream.wallet_balance_usd) !== null
    && Boolean(upstream.balance_checked_at);
}

function formatCurrentPlatformBalance(upstream: Upstream) {
  return hasCachedPlatformBalance(upstream)
    ? formatUpstreamBalance(upstream.wallet_balance_usd, upstream.balance_unit, 2)
    : "—";
}

function formatCurrentRechargeAdjustedBalance(upstream: Upstream) {
  if (!hasCachedPlatformBalance(upstream)) return "—";
  return formatRechargeAdjustedBalance(
    upstream.actual_balance_cny,
    upstream.wallet_balance_usd,
    upstream.upstream_recharge_multiplier,
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
    upstream_wallet_cost_usd: 0,
    upstream_actual_cost_cny: 0,
    management_account_cost_usd: 0,
    management_account_cost_cny: 0,
    management_user_charge_usd: 0,
    actual_income_cny: 0,
    consumption_cny: 0,
    profit_cny: 0,
    profit_margin: null,
  };
  return {
    ...history,
    api_accounts: Array.isArray(history.api_accounts) ? history.api_accounts : [],
    days: Array.isArray(history.days)
      ? history.days.map((day) => ({
          ...day,
          api_accounts: Array.isArray(day.api_accounts) ? day.api_accounts : [],
        }))
      : [],
    totals: history.totals || zeroTotals,
    lifetime_totals: history.lifetime_totals || history.totals || zeroTotals,
  };
}

function usageHistoryAccountOptions(history: UpstreamUsageHistory | null, upstream: Upstream) {
  const options = history?.api_accounts || (upstream.accounts || []).map((account) => ({
    management_account_id: account.management_account_id,
    account_name: account.remote_name || null,
    remote_key_id: account.remote_key_id,
    upstream_api_key_id: account.upstream_api_key_id,
  }));
  const unique = new Map<string, UpstreamUsageHistory["api_accounts"][number]>();
  for (const account of options) {
    const id = account?.management_account_id;
    if (id === null || id === undefined || id === "") continue;
    unique.set(String(id), account);
  }
  return [...unique.values()];
}

function usageHistoryAccountLabel(account: UpstreamUsageHistory["api_accounts"][number]) {
  return account.account_name?.trim() || `账号 #${account.management_account_id}`;
}

function usageHistoryDayAccount(day: UpstreamUsageHistory["days"][number], selectedAccountId: string | null) {
  if (!selectedAccountId) return null;
  return day.api_accounts.find((account) => String(account.management_account_id) === selectedAccountId) || null;
}

function historyDayUpstreamCost(
  day: UpstreamUsageHistory["days"][number],
  selectedAccountId: string | null,
) {
  const account = usageHistoryDayAccount(day, selectedAccountId);
  if (selectedAccountId) {
    return finiteNumber(account?.upstream_actual_cost_cny);
  }
  return finiteNumber(day.upstream_actual_cost_cny);
}

function historyDayManagementAccountCost(
  day: UpstreamUsageHistory["days"][number],
  selectedAccountId: string | null,
) {
  const account = usageHistoryDayAccount(day, selectedAccountId);
  return selectedAccountId
    ? finiteNumber(account?.management_account_cost_cny)
    : finiteNumber(day.management_account_cost_cny);
}

function historyDayProfit(
  day: UpstreamUsageHistory["days"][number],
  selectedAccountId: string | null,
) {
  const account = usageHistoryDayAccount(day, selectedAccountId);
  const persisted = selectedAccountId
    ? finiteNumber(account?.profit_cny)
    : finiteNumber(day.profit_cny);
  if (persisted !== null) return persisted;
  const income = selectedAccountId
    ? finiteNumber(account?.actual_income_cny)
    : finiteNumber(day.actual_income_cny);
  const costs = [
    historyDayUpstreamCost(day, selectedAccountId),
    historyDayManagementAccountCost(day, selectedAccountId),
  ].filter((value): value is number => value !== null);
  return income === null || !costs.length ? null : income - Math.max(...costs);
}

function historyDayProfitMargin(
  day: UpstreamUsageHistory["days"][number],
  selectedAccountId: string | null,
  profit: number | null,
) {
  const account = usageHistoryDayAccount(day, selectedAccountId);
  const persisted = selectedAccountId
    ? finiteNumber(account?.profit_margin)
    : finiteNumber(day.profit_margin);
  if (persisted !== null) return persisted;
  const costs = [
    historyDayUpstreamCost(day, selectedAccountId),
    historyDayManagementAccountCost(day, selectedAccountId),
  ].filter((value): value is number => value !== null);
  return calculateProfitMargin(costs.length ? Math.max(...costs) : null, profit);
}

function historyIncomeBreakdownTitle(
  day: UpstreamUsageHistory["days"][number],
  selectedAccountId: string | null,
) {
  const account = usageHistoryDayAccount(day, selectedAccountId);
  const multiplier = finiteNumber(
    selectedAccountId ? account?.management_recharge_multiplier : day.management_recharge_multiplier,
  );
  const income = finiteNumber(selectedAccountId ? account?.actual_income_cny : day.actual_income_cny);
  if (multiplier === null || income === null) return undefined;
  return `用户扣费（人民币）${formatHistoryAmount(income, "CNY")}；按当日充值倍率 ${formatCostPerUsd(multiplier)} 折算`;
}

function historyProfit(value: UpstreamUsageHistory["totals"] | null) {
  const persisted = finiteNumber(value?.profit_cny);
  if (persisted !== null) return persisted;
  const income = finiteNumber(value?.actual_income_cny);
  const costs = [
    finiteNumber(value?.upstream_actual_cost_cny),
    finiteNumber(value?.management_account_cost_cny),
  ].filter((item): item is number => item !== null);
  return income === null || !costs.length ? null : income - Math.max(...costs);
}

function historyProfitMargin(value: UpstreamUsageHistory["totals"] | null) {
  const persisted = finiteNumber(value?.profit_margin);
  if (persisted !== null) return persisted;
  const costs = [
    finiteNumber(value?.upstream_actual_cost_cny),
    finiteNumber(value?.management_account_cost_cny),
  ].filter((item): item is number => item !== null);
  return calculateProfitMargin(
    finiteNumber(value?.consumption_cny) ?? (costs.length ? Math.max(...costs) : null),
    historyProfit(value),
  );
}

function calculateProfitMargin(cost: number | null, profit: number | null) {
  return cost === null || profit === null || cost <= 0 ? null : profit / cost * 100;
}

function formatHistoryAmount(value: unknown, unit?: string | null) {
  return finiteNumber(value) === null ? "—" : formatUpstreamBalance(value, unit || "CNY", 2);
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

function formatBalanceSummaryTodayUsage(upstream: Upstream) {
  const amount = upstream.today_upstream_wallet_cost_usd;
  const status = String(upstream.today_balance_status || "not_checked").toLowerCase();
  const visible = finiteNumber(amount) !== null && ["ok", "stale", "stored"].includes(status);
  return {
    value: visible
      ? formatRechargeAdjustedBalance(
          upstream.today_upstream_actual_cost_cny,
          amount,
          upstream.upstream_recharge_multiplier,
        )
      : "-",
    stale: status === "stale",
  };
}

function formatDailyBalanceUsed(
  upstream: Upstream,
  period: "today" | "yesterday",
  timeZone: string,
) {
  const yesterday = period === "yesterday";
  const amount = yesterday ? upstream.yesterday_upstream_wallet_cost_usd : upstream.today_upstream_wallet_cost_usd;
  const adjustedAmount = yesterday
    ? upstream.yesterday_upstream_actual_cost_cny
    : upstream.today_upstream_actual_cost_cny;
  const status = String(
    (yesterday ? upstream.yesterday_balance_status : upstream.today_balance_status) || "not_checked",
  ).toLowerCase();
  const checkedAt = yesterday
    ? upstream.yesterday_balance_checked_at
    : upstream.today_balance_checked_at;
  const hasCurrentValue = finiteNumber(amount) !== null && isToday(checkedAt, timeZone);
  const current = (status === "ok" || status === "stored")
    && hasCurrentValue;
  const stale = status === "stale" && hasCurrentValue;
  const unsupported = /^(?:credentials_missing|not_available|unsupported)$/.test(status);
  const visible = current || stale;
  const adjustedValue = visible
    ? formatRechargeAdjustedBalance(adjustedAmount, amount, upstream.upstream_recharge_multiplier)
    : "-";
  const staleDetail = stale ? "；上游本次探测失败，显示当天最后一次有效值" : "";
  const label = yesterday ? "昨日实际消耗" : "今日实际消耗";
  return {
    label,
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
    adjustedValue,
    value: adjustedValue,
    detail: `${label}：${adjustedValue}（已考虑充值倍率）${staleDetail}`,
  };
}

function formatHistoryPercent(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "-";
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}%`;
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
  if (!value) return "未填写上游地址";
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
    sub2api_settings: "管理站点设置",
    sub2api: "Sub2API",
    sub2api_daily_usage: "上游今日实际消耗",
    upstream_api_key_actual_cost: "上游今日实际消耗",
    management_account_cost_converted: "管理站点账号成本",
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
    throw new Error("上游地址格式不正确");
  }
  if (url.protocol !== "https:") {
    throw new Error("上游地址必须使用 https");
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

function credentialPlaceholder(account: ApiAccount) {
  if (!account.api_key_set) return "粘贴账号 API Key";
  return "已配置；留空保持";
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}
