import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  ArrowUpDown,
  ChevronDown,
  CheckCircle2,
  CircleHelp,
  Clock3,
  Copy,
  Database,
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
import { createContext, FormEvent, lazy, Suspense, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { api, AUTH_EXPIRED_EVENT, upstreamLegacyBindingCounts } from "./api";
import {
  automationDurationDisplayValue,
  automationDurationSecondsValue,
  automationDurationUnits,
  preferredAutomationDurationUnit,
  type AutomationDurationUnit,
} from "./automationDuration";
import { HelpPopover } from "./HelpPopover";
import { MiddleEllipsisText } from "./MiddleEllipsisText";
import { oauthUsageBackgroundRefreshIntervals } from "./oauthUsageRefresh";
import {
  persistOverviewBalanceAlertDismissed,
  readOverviewBalanceAlertDismissed,
} from "./overviewBalanceAlertPreference";
import {
  accountCanBeLivenessTested,
  livenessAccountIds,
  MAX_LIVENESS_ACCOUNTS,
} from "./accountLiveness";
import { accountFilterFacetCandidates } from "./accountFilterFacets";
import {
  accountEstimateHasEffectiveError,
  accountRateLimitShouldBeVisible,
} from "./accountRateLimitPresentation";
import { isOAuthPhoneVerificationStopped } from "./accountErrorPresentation";
import { sortAccountsForTable } from "./accountTableSort";
import {
  firstUnusedFallbackModel,
  MAX_FALLBACK_TEST_MODELS,
  moveFallbackModel,
  normalizeFallbackModelChain,
} from "./fallbackModelChain";
import { LatestRequestCoordinator } from "./latestRequest";
import {
  apiAccountLegacyBindingConfirmationMessage,
  apiAccountSyncMessage,
  upstreamRateWritesAllowed,
} from "./upstreamSyncPresentation";

import {
  accountBillingRateChange,
  groupRateChange,
  remoteSchedulableChange,
  upstreamGroupStatusChange,
  upstreamKeyStatusChange,
  upstreamRateChange,
  upstreamChangeSummary,
  type UpstreamStateChange,
} from "./upstreamRatePresentation";
import {
  upstreamChangeReasonLabel,
  upstreamHealthStatusLabel,
  upstreamStatusTone,
  type UpstreamHealthKind,
} from "./upstreamLabels";
import {
  filterUsageLimitSamples,
  sortUsageLimitSamples,
  usageSampleDatePresets,
  usageSampleDateRangeForPreset,
  type UsageSampleDatePreset,
  type UsageSampleSortDirection,
  type UsageSampleSortField,
} from "./usageSampleSort";
import {
  clearChangeLogCache,
  getChangeLogSessionStorage,
} from "./changeLogCache";
import {
  normalizeChangeLogPageSizeOptions,
  parseChangeLogPageSizeOptions,
} from "./changeLogPageSize";
import {
  clearUpstreamOverviewCache,
  getUpstreamOverviewSessionStorage,
  readUpstreamOverviewCache,
  upstreamOverviewCacheScope,
  writeUpstreamOverviewCache,
} from "./upstreamOverviewCache";
import {
  pathForRoute,
  routeFromPath,
  type ApiKeySubview,
  type AppRoute,
  type View,
} from "./viewRouting";
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
} from "./usageEstimatePresentation";
import type {
  Account,
  AccountExceptionRecord,
  AccountLivenessModel,
  AccountLivenessTestResult,
  AccountUsageEstimate,
  ApiKeyViewOperation,
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
  UpstreamChangeLog,
  UpstreamChannel,
  UpstreamChannelsResponse,
} from "./types";

const loadApiKeyAccountsView = () => import("./ApiKeyAccountsView");
const ApiKeyAccountsView = lazy(async () => ({
  default: (await loadApiKeyAccountsView()).ApiKeyAccountsView,
}));
const loadHistoryView = () => import("./HistoryView");
const HistoryView = lazy(async () => ({
  default: (await loadHistoryView()).HistoryView,
}));
const loadAccountEditorDialog = () => import("./AccountEditorDialog");
const AccountEditorDialog = lazy(async () => ({
  default: (await loadAccountEditorDialog()).AccountEditorDialog,
}));

type Theme = "light" | "dark";
type AccountCounts = { actual: number; deduped: number; duplicates: number };
type AccountStatusFilter = "all" | "normal" | "normal-no-rate-limit" | "five-hour-rate-limited" | "seven-day-rate-limited" | "monthly-rate-limited" | "error" | "deactive";
type AccountSortField = "account" | "imported_at";
type SortDirection = "asc" | "desc";
type AccountJumpTarget = { email: string | null; sub2apiAccountId: string | null; requestedAt: number };

const defaultTimeZone = "Asia/Shanghai";
const defaultSiteName = "sub2api AT 刷新机";
const defaultUsageLimitSampleThresholdPercent = 99;

function clearFrontendSessionCaches() {
  clearUpstreamOverviewCache(getUpstreamOverviewSessionStorage());
  clearChangeLogCache(getChangeLogSessionStorage());
}
const usageLimitWindowKeys = ["five_hour", "seven_day", "monthly"] as const;
const coreSubscriptionTypes = new Set(["plus", "team", "pro", "free", "k12", "unknown"]);
const seatBasedSubscriptionTypes = new Set(["team", "k12", "enterprise", "enterprise-edu", "edu"]);
const defaultUsageLimitPlanRanges: UsageLimitPlanRanges = {
  five_hour: { lower: 15, upper: 25 },
  seven_day: { lower: 100, upper: 140 },
  monthly: { lower: 400, upper: 560 },
};
const defaultUsageLimitRanges = Object.fromEntries(
  [...coreSubscriptionTypes].map((subscriptionType) => [
    subscriptionType,
    subscriptionType === "team"
      ? { ...cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges), monthly: { lower: 100, upper: 300 } }
      : cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges),
  ]),
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
  oauth_account_sync_enabled: true,
  oauth_login_mode: "protocol",
  oauth_stop_on_phone_verification: false,
  recovery_enabled: false,
  monitor_interval_seconds: 300,
  usage_refresh_enabled: false,
  usage_refresh_interval_seconds: 3600,
  usage_refresh_max_concurrency: 20,
  api_key_account_sync_enabled: true,
  api_key_account_sync_interval_seconds: 300,
  upstream_sync_enabled: false,
  upstream_sync_interval_seconds: 900,
  upstream_sync_max_concurrency: 10,
  upstream_rate_sync_enabled: false,
  upstream_priority_sync_enabled: true,
  manual_upstream_sync_rate_enabled: true,
  manual_upstream_sync_priority_enabled: true,
  manual_upstream_sync_upstream_health_enabled: true,
  manual_upstream_sync_channel_monitors_enabled: true,
  manual_upstream_sync_account_availability_enabled: false,
  manual_upstream_sync_balance_guard_enabled: true,
  manual_upstream_sync_rate_pause_enabled: true,
  api_key_auto_disable_on_upstream_unavailable: false,
  api_key_auto_pause_on_negative_balance_enabled: false,
  api_key_auto_pause_on_channel_monitor_unavailable_enabled: false,
  api_key_availability_all_tests_must_succeed: false,
  channel_monitor_auto_probe_enabled: true,
  account_model_whitelist_sync_enabled: false,
  account_model_whitelist_sync_interval_seconds: 3600,
  account_model_whitelist_sync_each_time: false,
  channel_monitor_fallback_without_monitor_enabled: false,
  channel_monitor_fallback_test_models: [],
  channel_monitor_fallback_test_model: "",
  channel_monitor_fallback_test_attempts: 1,
  channel_monitor_recovery_test_attempts: 1,
  channel_monitor_test_attempt_interval_seconds: 0,
  available_test_models: [],
  upstream_negative_balance_basis: "wallet",
  upstream_balance_pause_threshold: 0,
  show_stale_negative_balance_alert: true,
  priority_assign_disabled_api_key_accounts: false,
  priority_share_same_composite_multiplier: false,
  upstream_rate_log_retention_days: 90,
  change_log_page_size: 50,
  change_log_page_size_options: [20, 50, 100, 200],
  upstream_usage_data_retention_days: 90,
  discord_bot_notifications_enabled: false,
  discord_bot_token_set: false,
  discord_bot_token_hint: null,
  discord_bot_channel_id: "",
  notify_oauth_account_disabled: false,
  notify_account_enabled: false,
  notify_api_key_rate_changed: false,
  notify_upstream_group_changed: false,
  notify_upstream_balance_low: false,
  notify_upstream_token_invalid: false,
  usage_limit_sample_five_hour_threshold_percent: 0,
  usage_limit_sample_seven_day_threshold_percent: 0,
  usage_limit_default_ranges: defaultUsageLimitRanges,
  refresh_max_concurrency: 2,
  protocol_refresh_max_concurrency: 2,
  browser_refresh_max_concurrency: 1,
  browser_min_available_memory_mb: 500,
  subscription_refresh_batch_size: 3,
  subscription_refresh_max_concurrency: 3,
  account_liveness_max_concurrency: 3,
  last_scan_at: null,
  last_scan_status: null,
  last_scan_message: null,
  display_timezone: defaultTimeZone,
  site_name: defaultSiteName,
  site_logo_url: null,
  site_logo_updated_at: null,
};

function App() {
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const [authState, setAuthState] = useState<"checking" | "in" | "out">("checking");
  const [route, setRoute] = useState<AppRoute>(() => {
    const nextRoute = routeFromPath(window.location.pathname);
    const canonicalPath = pathForRoute(nextRoute);
    if (window.location.pathname !== canonicalPath) {
      window.history.replaceState(null, "", canonicalPath);
    }
    return nextRoute;
  });
  const view = route.view;
  const [summary, setSummary] = useState<Summary>(emptySummary);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [mailboxes, setMailboxes] = useState<Mailbox[]>([]);
  const [phones, setPhones] = useState<PhoneNumber[]>([]);
  const [jobs, setJobs] = useState<RefreshJob[]>([]);
  const [events, setEvents] = useState<AppEvent[]>([]);
  const [exceptionRecords, setExceptionRecords] = useState<AccountExceptionRecord[]>([]);
  const [accountJumpTarget, setAccountJumpTarget] = useState<AccountJumpTarget | null>(null);
  const [settings, setSettings] = useState<AppSettings>(emptySettings);
  const [apiKeyAccountsCache, setApiKeyAccountsCache] = useState<UpstreamChannelsResponse | null>(null);
  const [apiKeyRefreshVersion, setApiKeyRefreshVersion] = useState(0);
  const [apiKeyViewBusy, setApiKeyViewBusy] = useState(false);
  const [apiKeyViewBlocking, setApiKeyViewBlocking] = useState(false);
  const apiKeyAccountsCacheBaseUrlRef = useRef<string | null>(null);
  const apiKeyViewOperationTokensRef = useRef(new Map<symbol, ApiKeyViewOperation>());
  const loadAllRequestSequenceRef = useRef(0);
  const settingsMutationGenerationRef = useRef(0);
  const settingsMutationPendingRef = useRef(false);
  const oauthSyncOperationRef = useRef(false);
  const apiKeySyncOperationRef = useRef(false);
  const oauthUsageRefreshGenerationRef = useRef(0);
  const oauthUsageRefreshTimersRef = useRef(new Set<number>());
  const usageEstimateRequestsRef = useRef(new LatestRequestCoordinator());
  const usageEstimateRefreshRequestRef = useRef<Promise<UsageEstimate> | null>(null);
  const [usageEstimate, setUsageEstimate] = useState<UsageEstimate | null>(null);
  const [usageLimitSamples, setUsageLimitSamples] = useState<UsageLimitSamples | null>(null);
  const [usageLimitSamplesLoading, setUsageLimitSamplesLoading] = useState(false);
  const [usageLimitSamplesError, setUsageLimitSamplesError] = useState("");
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState("");
  const [usageEstimateRefreshed, setUsageEstimateRefreshed] = useState(false);
  const [notice, setNotice] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [pageRefreshing, setPageRefreshing] = useState(false);
  const [oauthSyncBusy, setOAuthSyncBusy] = useState(false);
  const [apiKeySyncBusy, setApiKeySyncBusy] = useState(false);
  const [settingsFormInvalid, setSettingsFormInvalid] = useState(false);
  const siteName = settings.site_name?.trim() || defaultSiteName;
  const siteLogoUrl = settings.site_logo_url?.trim() || "/logo.png";
  const siteFaviconUrl = versionedSiteLogoUrl(siteLogoUrl, settings.site_logo_updated_at);
  const now = useRefreshClock();
  const lastOAuthSyncEvent = useMemo(() => latestEventByKinds(events, ["manual_sync", "monitor_sync"]), [events]);
  const lastApiKeySyncEvent = useMemo(
    () => latestEventByKinds(events, [
      "manual_api_key_inventory_sync",
      "manual_upstream_sync",
      "api_key_inventory_sync",
      "upstream_sync",
    ]),
    [events],
  );
  const oauthSyncActionTime = lastOAuthSyncEvent?.created_at ?? null;
  const apiKeySyncActionTime = lastApiKeySyncEvent?.created_at ?? null;
  const syncBusy = busy || oauthSyncBusy || apiKeySyncBusy || apiKeyViewBusy;
  const toggleTheme = useCallback(() => setTheme((current) => (current === "dark" ? "light" : "dark")), []);
  const navigateToView = useCallback((nextView: View) => {
    const nextRoute: AppRoute = {
      view: nextView,
      apiKeySubview: nextView === "api-keys" ? "channels" : "accounts",
    };
    const nextPath = pathForRoute(nextRoute);
    if (window.location.pathname !== nextPath) {
      window.history.pushState(null, "", nextPath);
    }
    setRoute(nextRoute);
  }, []);
  const navigateToApiKeySubview = useCallback((apiKeySubview: ApiKeySubview) => {
    const nextRoute: AppRoute = { view: "api-keys", apiKeySubview };
    const nextPath = pathForRoute(nextRoute);
    if (window.location.pathname !== nextPath) {
      window.history.pushState(null, "", nextPath);
    }
    setRoute(nextRoute);
  }, []);

  useEffect(() => {
    const handlePopState = () => {
      const nextRoute = routeFromPath(window.location.pathname);
      const canonicalPath = pathForRoute(nextRoute);
      if (window.location.pathname !== canonicalPath) {
        window.history.replaceState(null, "", canonicalPath);
      }
      setRoute(nextRoute);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const beginApiKeyViewOperation = useCallback((operation: ApiKeyViewOperation = { kind: "blocking" }) => {
    const token = Symbol("api-key-view-operation");
    apiKeyViewOperationTokensRef.current.set(token, operation);
    setApiKeyViewBusy(true);
    setApiKeyViewBlocking(
      Array.from(apiKeyViewOperationTokensRef.current.values()).some((item) => item.kind === "blocking"),
    );
    let released = false;
    return () => {
      if (released) return;
      released = true;
      apiKeyViewOperationTokensRef.current.delete(token);
      setApiKeyViewBusy(apiKeyViewOperationTokensRef.current.size > 0);
      setApiKeyViewBlocking(
        Array.from(apiKeyViewOperationTokensRef.current.values()).some((item) => item.kind === "blocking"),
      );
    };
  }, []);

  const cacheApiKeyAccounts = useCallback((response: UpstreamChannelsResponse, responseBaseUrl: string) => {
    const activeBaseUrl = apiKeyAccountsCacheBaseUrlRef.current;
    if (!activeBaseUrl || upstreamOverviewCacheScope(activeBaseUrl) !== upstreamOverviewCacheScope(responseBaseUrl)) return;
    const safeResponse = writeUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), responseBaseUrl, response);
    if (safeResponse) setApiKeyAccountsCache(response);
  }, []);

  const resetUsageEstimateRequests = useCallback(() => {
    usageEstimateRequestsRef.current.invalidate();
    usageEstimateRefreshRequestRef.current = null;
    setUsageLoading(false);
  }, []);

  const loadAll = useCallback(async ({ includePhones = true }: { includePhones?: boolean } = {}) => {
    if (settingsMutationPendingRef.current) return;
    const requestSequence = ++loadAllRequestSequenceRef.current;
    const settingsGeneration = settingsMutationGenerationRef.current;
    const phonePromise = includePhones ? api.phones().catch(() => null) : Promise.resolve<PhoneNumber[] | null>(null);
    const exceptionRecordsPromise = api.exceptionRecords().catch(() => null);
    const [nextSummary, nextAccounts, nextMailboxes, nextPhones, nextJobs, nextEvents, nextExceptionRecords, nextSettings, nextUpstreamOverview] = await Promise.all([
      api.summary(),
      api.accounts(),
      api.mailboxes(),
      phonePromise,
      api.jobs(),
      api.events(),
      exceptionRecordsPromise,
      api.settings(),
      api.upstreamChannels(false).catch(() => null),
    ]);
    if (
      requestSequence !== loadAllRequestSequenceRef.current
      || settingsGeneration !== settingsMutationGenerationRef.current
    ) return;
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
    const previousBaseUrl = apiKeyAccountsCacheBaseUrlRef.current;
    if (!previousBaseUrl) {
      apiKeyAccountsCacheBaseUrlRef.current = nextSettings.sub2api_base_url;
      const cachedOverview = nextUpstreamOverview
        ? writeUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), nextSettings.sub2api_base_url, nextUpstreamOverview)
        : readUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), nextSettings.sub2api_base_url);
      setApiKeyAccountsCache(nextUpstreamOverview || cachedOverview);
    } else if (upstreamOverviewCacheScope(previousBaseUrl) !== upstreamOverviewCacheScope(nextSettings.sub2api_base_url)) {
      clearFrontendSessionCaches();
      apiKeyAccountsCacheBaseUrlRef.current = nextSettings.sub2api_base_url;
      setUsageEstimate(null);
      setUsageLimitSamples(null);
      resetUsageEstimateRequests();
      const safeOverview = nextUpstreamOverview
        ? writeUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), nextSettings.sub2api_base_url, nextUpstreamOverview)
        : null;
      setApiKeyAccountsCache(nextUpstreamOverview || safeOverview);
    } else {
      apiKeyAccountsCacheBaseUrlRef.current = nextSettings.sub2api_base_url;
      if (nextUpstreamOverview) cacheApiKeyAccounts(nextUpstreamOverview, nextSettings.sub2api_base_url);
    }
    setSettings((current) => (appSettingsEqual(current, nextSettings) ? current : nextSettings));
  }, [cacheApiKeyAccounts, resetUsageEstimateRequests]);

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

  const loadUsageEstimate = useCallback((refresh = true): Promise<UsageEstimate> => {
    const activeRefresh = usageEstimateRefreshRequestRef.current;
    if (activeRefresh) return activeRefresh;

    const request = usageEstimateRequestsRef.current.beginForeground();
    setUsageLoading(true);
    setUsageError("");
    const requestPromise = (async () => {
      try {
        const nextEstimate = await api.usageEstimate(refresh);
        if (request.isCurrent()) {
          setUsageEstimate(nextEstimate);
          setUsageEstimateRefreshed(refresh);
        }
        return nextEstimate;
      } catch (error) {
        if (request.isCurrent()) {
          const message = error instanceof Error ? error.message : "额度估算读取失败";
          setUsageError(message);
        }
        throw error;
      } finally {
        if (request.finish()) setUsageLoading(false);
      }
    })();

    if (refresh) {
      usageEstimateRefreshRequestRef.current = requestPromise;
      void requestPromise.then(
        () => {
          if (usageEstimateRefreshRequestRef.current === requestPromise) {
            usageEstimateRefreshRequestRef.current = null;
          }
        },
        () => {
          if (usageEstimateRefreshRequestRef.current === requestPromise) {
            usageEstimateRefreshRequestRef.current = null;
          }
        },
      );
    }
    return requestPromise;
  }, []);

  const cancelOAuthUsageBackgroundRefresh = useCallback(() => {
    oauthUsageRefreshGenerationRef.current += 1;
    for (const timer of oauthUsageRefreshTimersRef.current) window.clearTimeout(timer);
    oauthUsageRefreshTimersRef.current.clear();
  }, []);

  const scheduleOAuthUsageBackgroundRefresh = useCallback((usagePending?: number) => {
    cancelOAuthUsageBackgroundRefresh();
    const generation = oauthUsageRefreshGenerationRef.current;
    const intervals = oauthUsageBackgroundRefreshIntervals(usagePending);

    const scheduleNext = (index: number) => {
      if (index >= intervals.length || generation !== oauthUsageRefreshGenerationRef.current) return;
      const timer = window.setTimeout(async () => {
        oauthUsageRefreshTimersRef.current.delete(timer);
        if (generation !== oauthUsageRefreshGenerationRef.current) return;
        const request = usageEstimateRequestsRef.current.beginBackground();
        if (!request) {
          scheduleNext(index + 1);
          return;
        }
        try {
          const nextEstimate = await api.usageEstimate(false);
          if (
            generation === oauthUsageRefreshGenerationRef.current
            && request.isCurrent()
          ) {
            setUsageEstimate(nextEstimate);
            setUsageEstimateRefreshed(true);
            setUsageError("");
          }
        } catch {
          // A later bounded retry can still pick up the completed background snapshot.
        } finally {
          request.finish();
        }
        scheduleNext(index + 1);
      }, intervals[index]);
      oauthUsageRefreshTimersRef.current.add(timer);
    };

    scheduleNext(0);
  }, [cancelOAuthUsageBackgroundRefresh]);

  useEffect(() => {
    if (authState !== "in") cancelOAuthUsageBackgroundRefresh();
    return cancelOAuthUsageBackgroundRefresh;
  }, [authState, cancelOAuthUsageBackgroundRefresh]);

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

  const refreshCurrentView = useCallback(async () => {
    if (pageRefreshing) return;
    setPageRefreshing(true);
    setNotice("");
    loadAllRequestSequenceRef.current += 1;
    try {
      if (view === "overview") {
        const [nextSummary, nextAccounts, nextJobs, nextEvents, nextUpstream] = await Promise.all([
          api.summary(),
          api.accounts(),
          api.jobs(),
          api.events(),
          api.upstreamChannels(false).catch(() => null),
        ]);
        setSummary(nextSummary);
        setAccounts(nextAccounts);
        setJobs(nextJobs);
        setEvents(nextEvents);
        if (nextUpstream) cacheApiKeyAccounts(nextUpstream, settings.sub2api_base_url);
        await loadUsageEstimate(false);
        setApiKeyRefreshVersion((current) => current + 1);
      } else if (view === "accounts") {
        const [nextAccounts, nextMailboxes] = await Promise.all([api.accounts(), api.mailboxes()]);
        setAccounts(nextAccounts);
        setMailboxes(nextMailboxes);
        await loadUsageEstimate(false);
      } else if (view === "api-keys") {
        const nextUpstream = await api.upstreamChannels(false);
        cacheApiKeyAccounts(nextUpstream, settings.sub2api_base_url);
        setApiKeyRefreshVersion((current) => current + 1);
      } else if (view === "usage") {
        await loadUsageEstimate(false);
      } else if (view === "usage-samples") {
        await loadUsageLimitSamples();
      } else if (view === "mailboxes") {
        setMailboxes(await api.mailboxes());
      } else if (view === "phones") {
        setPhones(await api.phones());
      } else if (view === "history") {
        const [nextJobs, nextEvents, nextExceptionRecords] = await Promise.all([
          api.jobs(),
          api.events(),
          api.exceptionRecords(),
        ]);
        setJobs(nextJobs);
        setEvents(nextEvents);
        setExceptionRecords(nextExceptionRecords);
      } else if (view === "settings") {
        setSettings(await api.settings());
      }
      setNotice("当前页面数据已刷新");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "当前页面刷新失败");
    } finally {
      setPageRefreshing(false);
    }
  }, [cacheApiKeyAccounts, loadUsageEstimate, loadUsageLimitSamples, pageRefreshing, settings.sub2api_base_url, view]);

  useEffect(() => {
    const handleAuthExpired = () => {
      clearFrontendSessionCaches();
      apiKeyAccountsCacheBaseUrlRef.current = null;
      setApiKeyAccountsCache(null);
      setUsageEstimate(null);
      setUsageLimitSamples(null);
      resetUsageEstimateRequests();
      setAuthState("out");
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, [resetUsageEstimateRequests]);

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
      .catch(() => {
        clearFrontendSessionCaches();
        apiKeyAccountsCacheBaseUrlRef.current = null;
        setApiKeyAccountsCache(null);
        setUsageEstimate(null);
        setUsageLimitSamples(null);
        resetUsageEstimateRequests();
        setAuthState("out");
      });
  }, [loadAll, resetUsageEstimateRequests]);

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
    if (authState !== "in" || view !== "usage-samples" || usageLimitSamples || usageLimitSamplesLoading) return;
    loadUsageLimitSamples().catch(() => undefined);
  }, [authState, loadUsageLimitSamples, usageLimitSamples, usageLimitSamplesLoading, view]);

  useEffect(() => {
    if (authState !== "in" || usageLoading) return;
    if (usageError) return;
    if (view === "overview" || view === "accounts") {
      if (usageEstimate) return;
      loadUsageEstimate(false).catch(() => undefined);
      return;
    }
    if (view === "usage") {
      if (!usageEstimate) {
        loadUsageEstimate(false).catch(() => undefined);
        return;
      }
      if (!usageEstimateRefreshed) loadUsageEstimate(true).catch(() => undefined);
    }
  }, [authState, loadUsageEstimate, usageError, usageEstimate, usageEstimateRefreshed, usageLoading, view]);

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      window.localStorage.setItem(themeStorageKey, theme);
    } catch {
      // Ignore storage failures in restricted browser contexts.
    }
  }, [theme]);

  useLayoutEffect(() => {
    const root = document.documentElement;
    const updateLayoutOffsets = () => {
      const topbar = document.querySelector<HTMLElement>(".topbar");
      const sidebar = document.querySelector<HTMLElement>(".sidebar");
      if (topbar) {
        root.style.setProperty("--app-header-height", `${topbar.getBoundingClientRect().height}px`);
      }
      if (sidebar) {
        root.style.setProperty("--app-sidebar-height", `${sidebar.getBoundingClientRect().height}px`);
      }
    };

    updateLayoutOffsets();
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(updateLayoutOffsets);
    const topbar = document.querySelector<HTMLElement>(".topbar");
    const sidebar = document.querySelector<HTMLElement>(".sidebar");
    if (resizeObserver) {
      if (topbar) resizeObserver.observe(topbar);
      if (sidebar) resizeObserver.observe(sidebar);
    }
    window.addEventListener("resize", updateLayoutOffsets);
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener("resize", updateLayoutOffsets);
    };
  }, [authState, notice, siteName, view]);

  useLayoutEffect(() => {
    document.querySelector<HTMLElement>(".workspace")?.scrollTo({ top: 0, left: 0, behavior: "auto" });
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [route.view, route.apiKeySubview]);

  useEffect(() => {
    document.title = siteName;
  }, [siteName]);

  useEffect(() => {
    const favicon = ensureFaviconLink();
    favicon.type = "image/png";
    favicon.href = siteFaviconUrl;
  }, [siteFaviconUrl]);

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

  const saveSettings = async (
    payload: AppSettingsUpdate,
    branding?: { logoFile: File | null; resetLogo: boolean },
  ) => {
    const previousSub2ApiBaseUrl = settings.sub2api_base_url;
    const nextSub2ApiBaseUrl = payload.sub2api_base_url || previousSub2ApiBaseUrl;
    const changesSub2ApiCredential = Boolean(
      payload.sub2api_x_api_key?.trim() || payload.clear_sub2api_x_api_key,
    );
    const reusesExistingCredential = settings.sub2api_x_api_key_set
      && !payload.clear_sub2api_x_api_key
      && !payload.sub2api_x_api_key?.trim();
    const changesCredentialOrigin =
      credentialBindingOrigin(previousSub2ApiBaseUrl) !== credentialBindingOrigin(nextSub2ApiBaseUrl);
    if (
      changesCredentialOrigin
      && reusesExistingCredential
      && !payload.confirm_sub2api_credential_rebind
    ) {
      const confirmed = window.confirm(
        "sub2api 域名已改变。继续会把当前管理凭据重新绑定到新域名，是否确认？",
      );
      if (!confirmed) return;
      payload = { ...payload, confirm_sub2api_credential_rebind: true };
    }
    settingsMutationGenerationRef.current += 1;
    loadAllRequestSequenceRef.current += 1;
    settingsMutationPendingRef.current = true;
    setBusy(true);
    setNotice("");
    try {
      let nextSettings = await api.updateSettings(payload);
      let brandingError = "";
      try {
        if (branding?.resetLogo) {
          await api.resetSiteLogo();
          nextSettings = await api.settings();
        } else if (branding?.logoFile) {
          await api.updateSiteLogo(branding.logoFile);
          nextSettings = await api.settings();
        }
      } catch (error) {
        brandingError = error instanceof Error ? error.message : "Logo 更新失败";
        nextSettings = await api.settings().catch(() => nextSettings);
      }
      settingsMutationGenerationRef.current += 1;
      loadAllRequestSequenceRef.current += 1;
      settingsMutationPendingRef.current = false;
      setSettings(nextSettings);
      if (
        nextSettings.sub2api_base_url !== previousSub2ApiBaseUrl
        || changesSub2ApiCredential
      ) {
        clearFrontendSessionCaches();
        apiKeyAccountsCacheBaseUrlRef.current = nextSettings.sub2api_base_url;
        setApiKeyAccountsCache(null);
        setUsageEstimate(null);
        setUsageLimitSamples(null);
        resetUsageEstimateRequests();
      }
      setNotice(brandingError ? `其他设置已保存，但 Logo 更新失败：${brandingError}` : "设置已保存");
      await loadAll().catch((error) => {
        const prefix = brandingError
          ? `其他设置已保存，但 Logo 更新失败：${brandingError}`
          : "设置已保存";
        setNotice(error instanceof Error ? `${prefix}；刷新页面数据失败：${error.message}` : `${prefix}；刷新页面数据失败`);
      });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "设置保存失败");
    } finally {
      settingsMutationPendingRef.current = false;
      setBusy(false);
    }
  };

  const runSyncAction = async <T,>(
    kind: "oauth" | "api-key",
    action: () => Promise<T>,
    success: string,
    refreshAffectedData: (result: T) => Promise<void>,
  ) => {
    const operationRef = kind === "oauth" ? oauthSyncOperationRef : apiKeySyncOperationRef;
    if (
      operationRef.current
      || busy
      || (kind === "api-key" && Array.from(apiKeyViewOperationTokensRef.current.values()).some(
        (operation) => operation.kind === "blocking",
      ))
    ) return;
    operationRef.current = true;
    if (kind === "oauth") setOAuthSyncBusy(true);
    else setApiKeySyncBusy(true);
    setNotice("");
    try {
      const result = await action();
      setNotice(
        result && typeof result === "object" && "message" in result && typeof result.message === "string"
          ? result.message
          : success,
      );
      await refreshAffectedData(result);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "同步失败");
    } finally {
      if (kind === "oauth") setOAuthSyncBusy(false);
      else setApiKeySyncBusy(false);
      operationRef.current = false;
    }
  };

  const runOAuthSync = () => {
    cancelOAuthUsageBackgroundRefresh();
    void runSyncAction(
      "oauth",
      () => api.sync(),
      "OAuth 账号同步完成",
      async (syncResult) => {
        const [nextSummary, nextAccounts, nextJobs, nextEvents, nextExceptionRecords] = await Promise.all([
          api.summary(),
          api.accounts(),
          api.jobs(),
          api.events(),
          api.exceptionRecords().catch(() => null),
        ]);
        loadAllRequestSequenceRef.current += 1;
        setSummary(nextSummary);
        setAccounts(nextAccounts);
        setJobs(nextJobs);
        setEvents(nextEvents);
        if (nextExceptionRecords) setExceptionRecords(nextExceptionRecords);
        scheduleOAuthUsageBackgroundRefresh(syncResult.usage_pending);
      },
    );
  };

  const runApiKeySync = () => void runSyncAction(
    "api-key",
    async () => {
      const liveOverview = await api.upstreamChannels();
      const bindingCounts = upstreamLegacyBindingCounts(liveOverview);
      const confirmationRequired = bindingCounts.unbound > 0 || bindingCounts.originRebind > 0;
      if (
        confirmationRequired
        && !window.confirm(apiAccountLegacyBindingConfirmationMessage(bindingCounts))
      ) {
        return { message: "已取消 API 账号同步。", cancelled: true };
      }
      const skipChannelIds = Array.from(apiKeyViewOperationTokensRef.current.values())
        .filter((operation): operation is Extract<ApiKeyViewOperation, { kind: "channel-discovery" }> => (
          operation.kind === "channel-discovery"
        ))
        .map((operation) => operation.channelId);
      const result = await api.syncApiKeyAccounts(
        liveOverview,
        confirmationRequired,
        Array.from(new Set(skipChannelIds)),
      );
      const overview = result.overview || {
        ...liveOverview,
        channels: result.channels || [],
        unassigned_accounts: liveOverview.unassigned_accounts,
      };
      cacheApiKeyAccounts(overview, settings.sub2api_base_url);
      setApiKeyRefreshVersion((current) => current + 1);
      const accountCount = overview.channels.reduce(
        (total, channel) => total + (channel.account_count || channel.accounts?.length || 0),
        overview.unassigned_accounts.length,
      );
      return {
        message: `已同步 ${accountCount} 个 API Key 账号；${apiAccountSyncMessage(
          result,
          upstreamRateWritesAllowed(settings.upstream_rate_sync_enabled, settings.automation_paused),
        )}`,
        cancelled: false,
      };
    },
    "API 账号同步完成",
    async () => {
      setEvents(await api.events());
    },
  );

  if (authState === "checking") {
    return <BootScreen />;
  }

  if (authState === "out") {
    return (
      <LoginScreen
        logoUrl={siteLogoUrl}
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
    { id: "api-keys", label: "API Key", icon: KeyRound },
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
            <img alt="" onError={fallbackSiteLogo} src={siteLogoUrl} />
          </div>
          <div>
            <strong>{siteName}</strong>
          </div>
        </div>

        <nav className="nav">
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                aria-label={item.label}
                className={view === item.id ? "nav-item active" : "nav-item"}
                key={item.id}
                onClick={() => navigateToView(item.id)}
                onFocus={() => {
                  if (item.id === "api-keys") void loadApiKeyAccountsView();
                }}
                onMouseEnter={() => {
                  if (item.id === "api-keys") void loadApiKeyAccountsView();
                }}
                title={item.label}
                type="button"
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="sidebar-actions">
          <ThemeToggle sidebar theme={theme} onToggleTheme={toggleTheme} />
          <button
            aria-label="退出"
            className="ghost-button"
            disabled={syncBusy}
            type="button"
            title="退出"
            onClick={async () => {
              setBusy(true);
              try {
                await api.logout();
              } finally {
                setBusy(false);
                clearFrontendSessionCaches();
                apiKeyAccountsCacheBaseUrlRef.current = null;
                setApiKeyAccountsCache(null);
                setUsageEstimate(null);
                setUsageLimitSamples(null);
                resetUsageEstimateRequests();
                setAuthState("out");
              }
            }}
          >
            <LogOut size={17} />
            <span>退出</span>
          </button>
        </div>
      </aside>

      <section className={`workspace workspace--${view}`}>
        <header className="topbar">
          <div>
            <p className="eyebrow">本机管理面板</p>
            <h1>{titleFor(view)}</h1>
          </div>
          <div className="topbar-actions">
            {notice ? (
              <div className="notice" role="status">
                <span>{notice}</span>
                <button
                  aria-label="关闭提示"
                  onClick={() => setNotice("")}
                  title="关闭"
                  type="button"
                >
                  <X size={15} />
                </button>
              </div>
            ) : null}
            <button
              aria-label="刷新当前页面数据"
              className="icon-button toolbar-page-refresh"
              disabled={pageRefreshing || syncBusy}
              onClick={() => void refreshCurrentView()}
              title="刷新当前页面数据"
              type="button"
            >
              <RefreshCcw className={pageRefreshing ? "spin" : ""} size={17} />
            </button>
            {view === "settings" ? (
              <button
                className="primary-button toolbar-settings-save"
                disabled={syncBusy || settingsFormInvalid}
                form="runtime-settings-form"
                type="submit"
              >
                <Save size={17} />
                <span>保存设置</span>
              </button>
            ) : null}
            <ToolbarTimeButton
              disabled={busy || oauthSyncBusy}
              icon={RefreshCcw}
              label="同步 OAuth 账号"
              loading={oauthSyncBusy}
              onClick={runOAuthSync}
              time={oauthSyncActionTime}
            />
            <ToolbarTimeButton
              disabled={busy || apiKeySyncBusy || apiKeyViewBlocking}
              icon={KeyRound}
              label="同步 API 账号"
              loading={apiKeySyncBusy}
              onClick={runApiKeySync}
              time={apiKeySyncActionTime}
            />
          </div>
        </header>

        {view === "overview" ? (
          <Overview
            summary={summary}
            accounts={accounts}
            accountCounts={accountCounts}
            apiKeyRefreshVersion={apiKeyRefreshVersion}
            jobs={jobs}
            events={events}
            problemUnusedQuota={problemUnusedQuota}
            showStaleNegativeBalanceAlert={settings.show_stale_negative_balance_alert ?? true}
            balanceBasis={settings.upstream_negative_balance_basis || "wallet"}
            balanceThreshold={settings.upstream_balance_pause_threshold ?? 0}
            upstreamOverview={apiKeyAccountsCache}
            usageByAccountId={usageByAccountId}
            usageByEmail={usageByEmail}
            onOpenUpstreamChannels={() => navigateToApiKeySubview("channels")}
          />
        ) : null}
        {view === "accounts" ? (
          <AccountsView
            accounts={accounts}
            accountJumpTarget={accountJumpTarget}
            busy={syncBusy}
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
            onAccountEdited={(message) => {
              setNotice(message);
              void api.accounts().then(setAccounts);
            }}
          />
        ) : null}
        {view === "api-keys" ? (
          <Suspense fallback={<Empty label="正在加载 API Key 页面" />}>
            <ApiKeyAccountsView
              cacheBaseUrl={settings.sub2api_base_url}
              cachedData={apiKeyAccountsCache}
              channelMonitorFallbackTestModels={settings.channel_monitor_fallback_test_models?.length
                ? settings.channel_monitor_fallback_test_models
                : settings.channel_monitor_fallback_test_model
                  ? [settings.channel_monitor_fallback_test_model]
                  : []}
              displayTimeZone={settings.display_timezone || defaultTimeZone}
              changeLogPageSize={settings.change_log_page_size || 50}
              changeLogPageSizeOptions={settings.change_log_page_size_options}
              globallyBusy={busy || apiKeySyncBusy || pageRefreshing}
              key={upstreamOverviewCacheScope(settings.sub2api_base_url)}
              onCacheChange={cacheApiKeyAccounts}
              onNotice={setNotice}
              onOperationStart={beginApiKeyViewOperation}
              onSubviewChange={navigateToApiKeySubview}
              rateWritesEnabled={upstreamRateWritesAllowed(
                settings.upstream_rate_sync_enabled,
                settings.automation_paused,
              )}
              refreshVersion={apiKeyRefreshVersion}
              shareSameCompositePriority={settings.priority_share_same_composite_multiplier ?? false}
              subview={route.apiKeySubview}
            />
          </Suspense>
        ) : null}
        {view === "usage" ? (
          <UsageEstimateView
            estimate={usageEstimate}
            error={usageError}
            loading={usageLoading}
            onLocateAccount={(account) => {
              setAccountJumpTarget({
                email: account.email,
                sub2apiAccountId: account.sub2api_account_id,
                requestedAt: Date.now(),
              });
              navigateToView("accounts");
            }}
          />
        ) : null}
        {view === "usage-samples" ? (
          <UsageLimitSamplesView
            data={usageLimitSamples}
            error={usageLimitSamplesError}
            loading={usageLimitSamplesLoading || busy}
            onDelete={(sampleId) =>
              runAction(async () => {
                const result = await api.deleteUsageLimitSample(sampleId);
                await loadUsageLimitSamples();
                return result;
              }, "额度样本已删除")
            }
            onDeleteMany={(sampleIds) =>
              runAction(async () => {
                const result = await api.deleteUsageLimitSamples(sampleIds);
                await loadUsageLimitSamples();
                return result;
              }, `已删除 ${sampleIds.length} 条额度样本`)
            }
            onRefresh={loadUsageLimitSamples}
          />
        ) : null}
        {view === "mailboxes" ? (
          <MailboxView
            mailboxes={mailboxes}
            busy={syncBusy}
            onImport={(content, provider) => runAction(() => api.importMailboxes(content, provider), "导入完成")}
            onDelete={(id) => runAction(() => api.deleteMailbox(id), "已删除")}
            onDeleteMany={(ids) => runAction(() => api.deleteMailboxes(ids), `已删除 ${ids.length} 个邮箱`)}
          />
        ) : null}
        {view === "phones" ? (
          <PhoneView
            accounts={accounts}
            busy={syncBusy}
            phones={phones}
            onDelete={(id) => runAction(() => api.deletePhone(id), "已删除手机号")}
            onDeleteMany={(ids) => runAction(() => api.deletePhones(ids), `已删除 ${ids.length} 个手机号`)}
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
          <Suspense fallback={<Empty label="正在加载历史页面" />}>
            <HistoryView
              busy={syncBusy}
              events={events}
              exceptionRecords={exceptionRecords}
              formatDate={formatDate}
              jobs={jobs}
              now={now}
              timeZone={settings.display_timezone || defaultTimeZone}
              onClear={() => runAction(api.clearHistory, "历史已清空")}
              onDeleteExceptionRecord={(id) => runAction(() => api.deleteExceptionRecord(id), "异常账号记录已删除")}
              onLocateAccount={(record) => {
                setAccountJumpTarget({
                  email: record.email,
                  sub2apiAccountId: record.sub2api_account_id,
                  requestedAt: Date.now(),
                });
                navigateToView("accounts");
              }}
            />
          </Suspense>
        ) : null}
        {view === "settings" ? (
          <SettingsView
            busy={syncBusy}
            logoUrl={siteLogoUrl}
            settings={settings}
            subscriptionTypes={[...new Set(accounts.map((account) => account.subscription_type).filter(Boolean))]}
            onScan={() => runAction(api.scanSub2Api, "扫描完成")}
            onSave={saveSettings}
            onTestNotification={() => runAction(api.testNotification, "测试通知已发送")}
            onValidityChange={setSettingsFormInvalid}
          />
        ) : null}
      </section>
      </main>
      </NowContext.Provider>
    </TimeZoneContext.Provider>
  );
}

function LoginScreen({
  logoUrl,
  siteName,
  theme,
  onToggleTheme,
  onLogin,
}: {
  logoUrl: string;
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
            <img alt="" onError={fallbackSiteLogo} src={logoUrl} />
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

function ThemeToggle({
  theme,
  onToggleTheme,
  compact = false,
  sidebar = false,
}: {
  theme: Theme;
  onToggleTheme: () => void;
  compact?: boolean;
  sidebar?: boolean;
}) {
  const isDark = theme === "dark";
  const Icon = isDark ? Sun : Moon;
  const label = isDark ? "浅色" : "暗色";
  const title = isDark ? "切换到浅色模式" : "切换到暗色模式";
  const className = sidebar
    ? "ghost-button theme-toggle sidebar-theme-toggle"
    : compact
      ? "secondary-button theme-toggle compact"
      : "secondary-button theme-toggle";

  return (
    <button
      aria-label={title}
      className={className}
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
  onOpenUpstreamChannels,
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
  upstreamOverview: UpstreamChannelsResponse | null;
  usageByAccountId: Map<string, AccountUsageEstimate>;
  usageByEmail: Map<string, AccountUsageEstimate>;
  onOpenUpstreamChannels: () => void;
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
  const lowBalanceChannels = (upstreamOverview?.channels || []).filter((channel) =>
    channelHasLowBalance(
      channel,
      showStaleNegativeBalanceAlert,
      balanceBasis,
      balanceThreshold,
    ),
  );
  const lowBalanceSignature = lowBalanceChannels
    .map((channel) => `${channel.id}:${channel.balance_guard_state}:${channel.balance_guard_value}:${channel.balance_remaining}:${channel.balance_checked_at}`)
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
            <strong>上游渠道余额不足</strong>
            <span>{lowBalanceChannels.map((channel) => `${channel.display_name || channel.base_url || `渠道 #${channel.id}`} ${formatBalanceGuardValue(channel, balanceBasis)}${isStaleLowBalance(channel, balanceBasis, balanceThreshold) ? "（上次结果）" : ""}`).join(" · ")}</span>
          </div>
          <button className="secondary-button" onClick={onOpenUpstreamChannels} type="button">
            <span>查看渠道</span>
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
            <p className="panel-subtitle">按 sub2api 当前窗口状态分开显示 5h、7d 与月限流账号。</p>
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
    <section className="panel api-key-rate-log-panel" aria-label="最近 API Key 上游变化">
      <div className="panel-toolbar">
        <div>
          <PanelTitle title="最近 API Key 上游变化" icon={KeyRound} />
          <p className="panel-subtitle">按发生时间展示上游 Key、分组、综合倍率与账号调度状态。</p>
        </div>
        <button
          aria-label="刷新最近 API Key 上游变化"
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
          <span>暂无 API Key 上游变化</span>
        </div>
      ) : (
        <div className="api-key-rate-log-list">
          {logs.map((log) => (
            <article className="api-key-rate-log-row" key={log.id}>
              <div className="api-key-rate-log-identity">
                <strong>{log.account_name || `API Key 账号 #${log.sub2api_account_id}`}</strong>
                <span>
                  {log.channel_name || "未分配渠道"}
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
              <OverviewRateChangeCell change={upstreamRateChange(log)} emphasize label="综合倍率" />
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
  onAccountEdited,
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
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
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
  const filteredAccounts = useMemo(
    () =>
      accountFilterCandidates.filteredAccounts.filter((account) => {
        const usage = usageForAccount(account, usageByAccountId, usageByEmail);
        return textMatchesSearch(
          [
            account.account_name,
            account.email,
            account.sub2api_account_id,
            account.sub2api_imported_at,
            account.last_seen_at,
            account.updated_at,
            account.platform,
            account.account_type,
            account.status,
            account.sub2api_error_code,
            account.sub2api_error_message,
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
        );
      }),
    [accountFilterCandidates.filteredAccounts, accountSearch, mailboxByEmail, usageByAccountId, usageByEmail],
  );
  const selectedAccounts = accounts.filter((account) => selectedAccountKeys[accountRowKey(account)]);
  const selectedAccountCount = selectedAccounts.length;
  const selectedLivenessAccounts = selectedAccounts.filter(accountCanBeLivenessTested);
  const selectedLivenessAccountCount = selectedLivenessAccounts.length;
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
            <span
              className={
                "liveness-selection-count"
                + (selectedLivenessAccountCount > MAX_LIVENESS_ACCOUNTS ? " is-over-limit" : "")
              }
              role="status"
            >
              已选 {selectedLivenessAccountCount} 个 OAuth GPT 账号
            </span>
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
                  <AccountTableSortButton
                    active={accountSortField === "account"}
                    direction={accountSortDirection}
                    label="账号"
                    onClick={() => toggleAccountSort("account")}
                  />
                </th>
                <th>sub2api ID</th>
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
                  onEdit={setEditingAccount}
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
      {editingAccount ? (
        <Suspense fallback={null}>
          <AccountEditorDialog
            account={editingAccount}
            onClose={() => setEditingAccount(null)}
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
    (account) => String(account.sub2api_account_id || "") === sourceAccountId,
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
            <span>sub2api 正在逐批测试连接...</span>
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
                    <th>sub2api ID</th>
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
            aria-label={`编辑 ${account.account_name || account.email}`}
            className="icon-button"
            disabled={busy || !account.sub2api_account_id || !onEdit}
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
  const activeTone = subscriptionIsInvalid(account) ? "warn" : account.has_active_subscription === true ? "ok" : "ink";
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
                  {detailAccounts.map((account, index) => (
                    <tr key={`${account.email}:${account.sub2api_account_id || index}`}>
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
  onDelete,
  onDeleteMany,
  onRefresh,
}: {
  data: UsageLimitSamples | null;
  loading: boolean;
  error: string;
  onDelete: (sampleId: number) => Promise<void> | void;
  onDeleteMany: (sampleIds: number[]) => Promise<void> | void;
  onRefresh: () => Promise<unknown>;
}) {
  const timeZone = useDisplayTimeZone();
  const sampleWindowGroups = useMemo(
    () =>
      usageLimitWindowKeys
        .map((windowKey) => {
          const cohorts = (data?.windows || []).filter(
            (window) => window.window_key === windowKey && window.samples.length > 0,
          );
          return {
            windowKey,
            label: cohorts[0]?.label || windowKey,
            sampleCount: cohorts.reduce((total, window) => total + window.samples.length, 0),
            cohorts,
          };
        })
        .filter((group) => group.sampleCount > 0),
    [data?.windows],
  );
  const [selectedWindowKey, setSelectedWindowKey] = useState<string>("");
  useEffect(() => {
    if (!sampleWindowGroups.length) {
      if (selectedWindowKey) {
        setSelectedWindowKey("");
      }
      return;
    }
    if (!sampleWindowGroups.some((group) => group.windowKey === selectedWindowKey)) {
      setSelectedWindowKey(sampleWindowGroups[0].windowKey);
    }
  }, [sampleWindowGroups, selectedWindowKey]);
  const selectedWindowGroup = useMemo(
    () => sampleWindowGroups.find((group) => group.windowKey === selectedWindowKey) || sampleWindowGroups[0] || null,
    [sampleWindowGroups, selectedWindowKey],
  );
  const [selectedSubscriptionKey, setSelectedSubscriptionKey] = useState<string>("");
  useEffect(() => {
    const cohorts = selectedWindowGroup?.cohorts || [];
    if (!cohorts.length) {
      if (selectedSubscriptionKey) {
        setSelectedSubscriptionKey("");
      }
      return;
    }
    if (!cohorts.some((cohort) => cohort.plan_cohort === selectedSubscriptionKey)) {
      setSelectedSubscriptionKey(cohorts[0].plan_cohort);
    }
  }, [selectedSubscriptionKey, selectedWindowGroup]);
  const selectedSubscription = useMemo(
    () =>
      selectedWindowGroup?.cohorts.find((cohort) => cohort.plan_cohort === selectedSubscriptionKey) ||
      selectedWindowGroup?.cohorts[0] ||
      null,
    [selectedSubscriptionKey, selectedWindowGroup],
  );
  const [sampleSortField, setSampleSortField] = useState<UsageSampleSortField>("quota");
  const [sampleSortDirection, setSampleSortDirection] = useState<UsageSampleSortDirection>("asc");
  const [sampleStartDate, setSampleStartDate] = useState("");
  const [sampleEndDate, setSampleEndDate] = useState("");
  const [sampleDatePreset, setSampleDatePreset] = useState<UsageSampleDatePreset | null>(null);
  const [selectedSampleIds, setSelectedSampleIds] = useState<Set<number>>(() => new Set());
  const selectAllSamplesRef = useRef<HTMLInputElement>(null);
  const sampleDateRangeInvalid = Boolean(sampleStartDate && sampleEndDate && sampleStartDate > sampleEndDate);
  const filteredSamples = useMemo(
    () => sampleDateRangeInvalid
      ? []
      : filterUsageLimitSamples(
        selectedSubscription?.samples || [],
        sampleStartDate,
        sampleEndDate,
        timeZone,
      ),
    [sampleDateRangeInvalid, sampleEndDate, sampleStartDate, selectedSubscription?.samples, timeZone],
  );
  const sortedSamples = useMemo(
    () => sortUsageLimitSamples(filteredSamples, sampleSortField, sampleSortDirection),
    [filteredSamples, sampleSortDirection, sampleSortField],
  );
  const allVisibleSamplesSelected = sortedSamples.length > 0
    && sortedSamples.every((sample) => selectedSampleIds.has(sample.id));
  const someVisibleSamplesSelected = sortedSamples.some((sample) => selectedSampleIds.has(sample.id));
  useEffect(() => {
    setSelectedSampleIds(new Set());
  }, [sampleEndDate, sampleStartDate, selectedSubscriptionKey, selectedWindowKey]);
  useEffect(() => {
    const availableIds = new Set((selectedSubscription?.samples || []).map((sample) => sample.id));
    setSelectedSampleIds((current) => {
      const next = new Set([...current].filter((sampleId) => availableIds.has(sampleId)));
      return next.size === current.size ? current : next;
    });
  }, [selectedSubscription?.samples]);
  useEffect(() => {
    if (selectAllSamplesRef.current) {
      selectAllSamplesRef.current.indeterminate = someVisibleSamplesSelected && !allVisibleSamplesSelected;
    }
  }, [allVisibleSamplesSelected, someVisibleSamplesSelected]);
  const toggleSampleSort = (field: UsageSampleSortField) => {
    if (field === sampleSortField) {
      setSampleSortDirection((current) => current === "asc" ? "desc" : "asc");
      return;
    }
    setSampleSortField(field);
    setSampleSortDirection(field === "recorded_at" ? "desc" : "asc");
  };
  const applySampleDatePreset = (preset: UsageSampleDatePreset) => {
    const range = usageSampleDateRangeForPreset(preset, timeZone);
    setSampleStartDate(range.startDate);
    setSampleEndDate(range.endDate);
    setSampleDatePreset(preset);
  };
  const clearSampleDateFilter = () => {
    setSampleStartDate("");
    setSampleEndDate("");
    setSampleDatePreset(null);
  };
  const toggleVisibleSamples = () => {
    setSelectedSampleIds((current) => {
      const next = new Set(current);
      if (allVisibleSamplesSelected) {
        sortedSamples.forEach((sample) => next.delete(sample.id));
      } else {
        sortedSamples.forEach((sample) => next.add(sample.id));
      }
      return next;
    });
  };
  const toggleSampleSelection = (sampleId: number) => {
    setSelectedSampleIds((current) => {
      const next = new Set(current);
      if (next.has(sampleId)) next.delete(sampleId);
      else next.add(sampleId);
      return next;
    });
  };
  const deleteSelectedSamples = () => {
    const sampleIds = [...selectedSampleIds];
    if (!sampleIds.length) return;
    if (window.confirm(`确定删除选中的 ${sampleIds.length} 条额度样本吗？此操作不可撤销。`)) {
      void onDeleteMany(sampleIds);
    }
  };

  return (
    <div className="stack">
      <section className="panel usage-samples-hero">
        <div className="panel-toolbar">
          <div>
            <PanelTitle title="额度样本" icon={Radar} />
            <p className="panel-subtitle">
              展示本地保存、用于推断官方窗口额度的限流样本；样本持续累积，仅在手动选择后删除。
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
          <span>按订阅类型与真实窗口分别统计</span>
          <span>样本数量 &lt; 10 条时使用默认区间</span>
          <span>样本数量 ≥ 10 条后使用 mean ± 3 sigma</span>
        </div>
      </section>

      {!data && loading ? <Empty label="正在读取额度样本" /> : null}
      {data && sampleWindowGroups.length ? (
        <>
          <div className="usage-sample-tabs" role="tablist" aria-label="额度样本视图切换">
            {sampleWindowGroups.map((group) => (
              <button
                key={group.windowKey}
                className={selectedWindowKey === group.windowKey ? "usage-sample-tab active" : "usage-sample-tab"}
                onClick={() => {
                  setSelectedWindowKey(group.windowKey);
                  setSelectedSubscriptionKey(group.cohorts[0]?.plan_cohort || "");
                }}
                role="tab"
                aria-selected={selectedWindowKey === group.windowKey}
                type="button"
              >
                <span className="usage-sample-tab-label">{group.label}</span>
                <span className="usage-sample-tab-metrics">
                  <small><strong>{group.sampleCount}</strong> 样本</small>
                  <small><strong>{group.cohorts.length}</strong> 订阅</small>
                </span>
              </button>
            ))}
          </div>
          {selectedWindowGroup ? (
            <div className="usage-sample-subscription-tabs" role="tablist" aria-label={`${selectedWindowGroup.label} 订阅类型切换`}>
              {selectedWindowGroup.cohorts.map((cohort) => (
                <button
                  className={selectedSubscription?.plan_cohort === cohort.plan_cohort ? "usage-sample-subscription-tab active" : "usage-sample-subscription-tab"}
                  key={`${cohort.window_key}:${cohort.plan_cohort}`}
                  onClick={() => setSelectedSubscriptionKey(cohort.plan_cohort)}
                  role="tab"
                  aria-selected={selectedSubscription?.plan_cohort === cohort.plan_cohort}
                  type="button"
                >
                  <span>{cohort.plan_label}</span>
                  <strong>{cohort.samples.length}</strong>
                </button>
              ))}
            </div>
          ) : null}
          {selectedSubscription ? (
            <section className="usage-sample-management" aria-label="样本日期筛选与批量管理">
              <div className="usage-sample-date-fields">
                <label>
                  <span>开始日期</span>
                  <input
                    aria-invalid={sampleDateRangeInvalid}
                    max={sampleEndDate || undefined}
                    onChange={(event) => {
                      setSampleStartDate(event.target.value);
                      setSampleDatePreset(null);
                    }}
                    type="date"
                    value={sampleStartDate}
                  />
                </label>
                <label>
                  <span>结束日期</span>
                  <input
                    aria-invalid={sampleDateRangeInvalid}
                    min={sampleStartDate || undefined}
                    onChange={(event) => {
                      setSampleEndDate(event.target.value);
                      setSampleDatePreset(null);
                    }}
                    type="date"
                    value={sampleEndDate}
                  />
                </label>
              </div>
              <div className="usage-sample-quick-filters" role="group" aria-label="快捷日期筛选">
                {usageSampleDatePresets.map((preset) => (
                  <button
                    aria-pressed={sampleDatePreset === preset.id}
                    className={sampleDatePreset === preset.id ? "usage-sample-filter-chip active" : "usage-sample-filter-chip"}
                    key={preset.id}
                    onClick={() => applySampleDatePreset(preset.id)}
                    type="button"
                  >
                    {preset.label}
                  </button>
                ))}
                {sampleStartDate || sampleEndDate ? (
                  <button className="usage-sample-filter-clear" onClick={clearSampleDateFilter} type="button">
                    <X size={14} />
                    <span>清除日期</span>
                  </button>
                ) : null}
              </div>
              <div className="usage-sample-selection-actions">
                <span>
                  显示 <strong>{sortedSamples.length}</strong> / 共 {selectedSubscription.samples.length} 条 · 已选 {selectedSampleIds.size} 条
                </span>
                <button
                  className="secondary-button"
                  disabled={loading || sortedSamples.length === 0}
                  onClick={toggleVisibleSamples}
                  type="button"
                >
                  {allVisibleSamplesSelected ? <X size={16} /> : <CheckCircle2 size={16} />}
                  <span>{allVisibleSamplesSelected ? "取消全选" : "全选当前"}</span>
                </button>
                <button
                  className="danger-button"
                  disabled={loading || selectedSampleIds.size === 0}
                  onClick={deleteSelectedSamples}
                  type="button"
                >
                  <Trash2 size={16} />
                  <span>删除已选</span>
                </button>
              </div>
              {sampleDateRangeInvalid ? <span className="form-error">开始日期不能晚于结束日期</span> : null}
            </section>
          ) : null}
          <div className="usage-samples-grid">
            {selectedSubscription ? (
              <section className="panel usage-sample-window" key={`${selectedSubscription.window_key}:${selectedSubscription.plan_cohort}`}>
                <div className="usage-sample-window-head">
                  <div>
                    <PanelTitle title={`${selectedSubscription.label} 样本 · ${selectedSubscription.plan_label}`} icon={TimerReset} />
                    <p className="panel-subtitle">
                      {selectedSubscription.calibration.source === "sigma" ? "当前使用统计区间" : "当前使用默认区间"} · 共 {selectedSubscription.samples.length} 条 ·
                      当前显示 {sortedSamples.length} 条 · 更新 {formatDate(data.updated_at, timeZone)}
                    </p>
                  </div>
                  <div className="usage-sample-calibration">
                    <strong>
                      {formatMoney(selectedSubscription.calibration.lower)} - {formatMoney(selectedSubscription.calibration.upper)}
                    </strong>
                    <span>
                      均值 {formatMoney(selectedSubscription.calibration.mean)} · sigma {formatMoney(selectedSubscription.calibration.sigma)}
                    </span>
                  </div>
                </div>
                <div className="table-wrap">
                  <table className="usage-sample-table">
                  <thead>
                    <tr>
                      <th className="usage-sample-select-cell">
                        <input
                          aria-label="全选当前筛选样本"
                          checked={allVisibleSamplesSelected}
                          disabled={loading || sortedSamples.length === 0}
                          onChange={toggleVisibleSamples}
                          ref={selectAllSamplesRef}
                          type="checkbox"
                        />
                      </th>
                      <th>#</th>
                      <th>套餐</th>
                      <th>邮箱</th>
                      <th aria-sort={sampleSortField === "quota" ? (sampleSortDirection === "asc" ? "ascending" : "descending") : "none"}>
                        <button
                          aria-label={`按额度${sampleSortField === "quota" && sampleSortDirection === "asc" ? "降序" : "升序"}排列`}
                          className={sampleSortField === "quota" ? "usage-sample-sort active" : "usage-sample-sort"}
                          onClick={() => toggleSampleSort("quota")}
                          title="切换额度排序"
                          type="button"
                        >
                          <span>窗口总额</span>
                          {sampleSortField !== "quota" ? <ArrowUpDown size={14} /> : sampleSortDirection === "asc" ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
                        </button>
                      </th>
                      <th>限流已用</th>
                      <th>官方百分比</th>
                      <th>重置</th>
                      <th aria-sort={sampleSortField === "recorded_at" ? (sampleSortDirection === "asc" ? "ascending" : "descending") : "none"}>
                        <button
                          aria-label={`按记录时间${sampleSortField === "recorded_at" && sampleSortDirection === "desc" ? "升序" : "降序"}排列`}
                          className={sampleSortField === "recorded_at" ? "usage-sample-sort active" : "usage-sample-sort"}
                          onClick={() => toggleSampleSort("recorded_at")}
                          title="切换记录时间排序"
                          type="button"
                        >
                          <span>记录时间</span>
                          {sampleSortField !== "recorded_at" ? <ArrowUpDown size={14} /> : sampleSortDirection === "asc" ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
                        </button>
                      </th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedSamples.map((sample, index) => (
                      <tr className={selectedSampleIds.has(sample.id) ? "is-selected" : ""} key={sample.id}>
                        <td className="usage-sample-select-cell">
                          <input
                            aria-label={`选择额度样本 ${sample.id}`}
                            checked={selectedSampleIds.has(sample.id)}
                            disabled={loading}
                            onChange={() => toggleSampleSelection(sample.id)}
                            type="checkbox"
                          />
                        </td>
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
                        <td>
                          <button
                            aria-label={`删除额度样本 ${sample.id}`}
                            className="icon-button usage-sample-delete"
                            disabled={loading}
                            onClick={() => {
                              if (window.confirm(`确定删除额度样本 #${sample.id} 吗？此操作不可撤销。`)) {
                                void onDelete(sample.id);
                              }
                            }}
                            title="删除此样本"
                            type="button"
                          >
                            <Trash2 size={15} />
                          </button>
                        </td>
                      </tr>
                    ))}
                    {!sortedSamples.length ? (
                      <tr>
                        <td className="usage-sample-filter-empty" colSpan={10}>
                          {sampleDateRangeInvalid ? "请调整日期范围" : "当前日期范围内没有样本"}
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                  </table>
                </div>
              </section>
            ) : null}
          </div>
        </>
      ) : null}
      {data && !sampleWindowGroups.length ? <Empty label="暂无额度样本" /> : null}
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
  onDeleteMany,
}: {
  mailboxes: Mailbox[];
  busy: boolean;
  onImport: (content: string, provider: string) => void;
  onDelete: (id: number) => void;
  onDeleteMany: (ids: number[]) => Promise<void> | void;
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
  const [selectedMailboxIds, setSelectedMailboxIds] = useState<Set<number>>(() => new Set());
  const selectAllMailboxesRef = useRef<HTMLInputElement>(null);
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
  const allVisibleMailboxesSelected = filteredMailboxes.length > 0
    && filteredMailboxes.every((mailbox) => selectedMailboxIds.has(mailbox.id));
  const someVisibleMailboxesSelected = filteredMailboxes.some((mailbox) => selectedMailboxIds.has(mailbox.id));

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

  useEffect(() => {
    const availableIds = new Set(mailboxes.map((mailbox) => mailbox.id));
    setSelectedMailboxIds((current) => {
      const next = new Set([...current].filter((mailboxId) => availableIds.has(mailboxId)));
      return next.size === current.size ? current : next;
    });
    if (selectedMailbox && !availableIds.has(selectedMailbox.id)) setSelectedMailbox(null);
  }, [mailboxes, selectedMailbox]);

  useEffect(() => {
    if (selectAllMailboxesRef.current) {
      selectAllMailboxesRef.current.indeterminate = someVisibleMailboxesSelected && !allVisibleMailboxesSelected;
    }
  }, [allVisibleMailboxesSelected, someVisibleMailboxesSelected]);

  const toggleVisibleMailboxes = () => {
    setSelectedMailboxIds((current) => {
      const next = new Set(current);
      if (allVisibleMailboxesSelected) filteredMailboxes.forEach((mailbox) => next.delete(mailbox.id));
      else filteredMailboxes.forEach((mailbox) => next.add(mailbox.id));
      return next;
    });
  };
  const toggleMailboxSelection = (mailboxId: number) => {
    setSelectedMailboxIds((current) => {
      const next = new Set(current);
      if (next.has(mailboxId)) next.delete(mailboxId);
      else next.add(mailboxId);
      return next;
    });
  };
  const deleteSelectedMailboxes = () => {
    const mailboxIds = [...selectedMailboxIds];
    if (!mailboxIds.length) return;
    if (window.confirm(`确定删除选中的 ${mailboxIds.length} 个邮箱吗？此操作不可撤销。`)) {
      void onDeleteMany(mailboxIds);
    }
  };

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
                <option value="url">URL 取件</option>
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
            placeholder={"gpt@example.com----mail@hotmail.com----mail_password----client_id----refresh_token\nurl@example.com----https://mail.example.com/messages/TOKEN/url%40example.com\naccount@example.com----inbox@example.com----https://mail.example.com/messages/TOKEN/inbox%40example.com"}
            rows={6}
            value={content}
          />
          <p className="form-hint">支持批量导入，一行一个；URL 取件支持“GPT 邮箱----取件 URL”或“GPT 邮箱----取件邮箱----取件 URL”。</p>
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
            <span className="resource-selection-count" aria-live="polite">已选 {selectedMailboxIds.size}</span>
            <button
              className="danger-button resource-bulk-delete"
              disabled={busy || selectedMailboxIds.size === 0}
              onClick={deleteSelectedMailboxes}
              type="button"
            >
              <Trash2 size={16} />
              <span>删除已选</span>
            </button>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="resource-select-cell">
                  <input
                    aria-label="全选当前筛选邮箱"
                    checked={allVisibleMailboxesSelected}
                    disabled={busy || filteredMailboxes.length === 0}
                    onChange={toggleVisibleMailboxes}
                    ref={selectAllMailboxesRef}
                    type="checkbox"
                  />
                </th>
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
                <tr className={selectedMailboxIds.has(mailbox.id) ? "is-selected" : ""} key={mailbox.id}>
                  <td className="resource-select-cell">
                    <input
                      aria-label={`选择邮箱 ${mailbox.gpt_email}`}
                      checked={selectedMailboxIds.has(mailbox.id)}
                      disabled={busy}
                      onChange={() => toggleMailboxSelection(mailbox.id)}
                      type="checkbox"
                    />
                  </td>
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

function AccountErrorDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const errorSummary = accountErrorSummary(account);
  const remoteMessage = String(account.sub2api_error_message || "").trim();
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
            <strong>sub2api 状态码</strong>
            <span>{account.sub2api_error_code || "-"}</span>
          </div>
        </div>
        <div className="phone-dialog-card error-dialog-body">
          <strong>{remoteMessage ? "sub2api 报错" : "完整报错"}</strong>
          <pre>{remoteMessage || localMessage || (account.remote_error ? "sub2api 账号异常" : "无错误详情")}</pre>
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

function PhoneView({
  phones,
  accounts,
  busy,
  onImport,
  onExport,
  onRefreshStatuses,
  onUpdateBindings,
  onDelete,
  onDeleteMany,
}: {
  phones: PhoneNumber[];
  accounts: Account[];
  busy: boolean;
  onImport: (content: string) => Promise<void> | void;
  onExport: () => Promise<void>;
  onRefreshStatuses: () => Promise<void> | void;
  onUpdateBindings: (id: number, accountEmails: string[]) => Promise<void> | void;
  onDelete: (id: number) => Promise<void> | void;
  onDeleteMany: (ids: number[]) => Promise<void> | void;
}) {
  const timeZone = useDisplayTimeZone();
  const [content, setContent] = useState("");
  const [phoneSearch, setPhoneSearch] = useState("");
  const [selectedPhone, setSelectedPhone] = useState<PhoneNumber | null>(null);
  const [selectedPhoneIds, setSelectedPhoneIds] = useState<Set<number>>(() => new Set());
  const selectAllPhonesRef = useRef<HTMLInputElement>(null);
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
  const allVisiblePhonesSelected = filteredPhones.length > 0
    && filteredPhones.every((phone) => selectedPhoneIds.has(phone.id));
  const someVisiblePhonesSelected = filteredPhones.some((phone) => selectedPhoneIds.has(phone.id));

  useEffect(() => {
    const availableIds = new Set(phones.map((phone) => phone.id));
    setSelectedPhoneIds((current) => {
      const next = new Set([...current].filter((phoneId) => availableIds.has(phoneId)));
      return next.size === current.size ? current : next;
    });
    if (selectedPhone && !availableIds.has(selectedPhone.id)) setSelectedPhone(null);
  }, [phones, selectedPhone]);

  useEffect(() => {
    if (selectAllPhonesRef.current) {
      selectAllPhonesRef.current.indeterminate = someVisiblePhonesSelected && !allVisiblePhonesSelected;
    }
  }, [allVisiblePhonesSelected, someVisiblePhonesSelected]);

  const toggleVisiblePhones = () => {
    setSelectedPhoneIds((current) => {
      const next = new Set(current);
      if (allVisiblePhonesSelected) filteredPhones.forEach((phone) => next.delete(phone.id));
      else filteredPhones.forEach((phone) => next.add(phone.id));
      return next;
    });
  };
  const togglePhoneSelection = (phoneId: number) => {
    setSelectedPhoneIds((current) => {
      const next = new Set(current);
      if (next.has(phoneId)) next.delete(phoneId);
      else next.add(phoneId);
      return next;
    });
  };
  const deleteSelectedPhones = () => {
    const phoneIds = [...selectedPhoneIds];
    if (!phoneIds.length) return;
    if (window.confirm(`确定删除选中的 ${phoneIds.length} 个手机号吗？此操作不可撤销。`)) {
      void onDeleteMany(phoneIds);
    }
  };

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
            <span className="resource-selection-count" aria-live="polite">已选 {selectedPhoneIds.size}</span>
            <button
              className="danger-button resource-bulk-delete"
              disabled={busy || selectedPhoneIds.size === 0}
              onClick={deleteSelectedPhones}
              type="button"
            >
              <Trash2 size={16} />
              <span>删除已选</span>
            </button>
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
                <th className="resource-select-cell">
                  <input
                    aria-label="全选当前筛选手机号"
                    checked={allVisiblePhonesSelected}
                    disabled={busy || filteredPhones.length === 0}
                    onChange={toggleVisiblePhones}
                    ref={selectAllPhonesRef}
                    type="checkbox"
                  />
                </th>
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
                  <tr className={selectedPhoneIds.has(phone.id) ? "is-selected" : ""} key={phone.id}>
                    <td className="resource-select-cell">
                      <input
                        aria-label={`选择手机号 ${phone.phone_number}`}
                        checked={selectedPhoneIds.has(phone.id)}
                        disabled={busy}
                        onChange={() => togglePhoneSelection(phone.id)}
                        type="checkbox"
                      />
                    </td>
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

function AutomationSettingRow({
  checked,
  description,
  interval,
  label,
  manual,
  onChange,
  threads,
}: {
  checked: boolean;
  description?: string;
  interval: ReactNode;
  label: string;
  manual?: ReactNode;
  onChange: (checked: boolean) => void;
  threads: ReactNode;
}) {
  return (
    <div className="automation-setting-row" role="group" aria-label={label}>
      <label className="automation-setting-toggle">
        <input checked={checked} onChange={(event) => onChange(event.target.checked)} type="checkbox" />
        <span className="automation-setting-label">
          <span className="settings-label-with-help">
            <strong>{label}</strong>
            {description ? <HelpPopover label={`查看${label}说明`}>{description}</HelpPopover> : null}
          </span>
        </span>
      </label>
      <div className="automation-setting-cell">
        <span className="automation-setting-mobile-label">线程数</span>
        {threads}
      </div>
      <div className="automation-setting-cell">
        <span className="automation-setting-mobile-label">自动执行间隔</span>
        {interval}
      </div>
      <div className="automation-setting-cell automation-setting-cell--manual">
        <span className="automation-setting-mobile-label">手动同步</span>
        {manual ?? <AutomationSettingInherited>不适用</AutomationSettingInherited>}
      </div>
    </div>
  );
}

function AutomationSettingManualCheckbox({
  checked,
  disabled = false,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="automation-setting-manual" title={label}>
      <input
        aria-label={label}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        type="checkbox"
      />
      <span>{checked ? "执行" : "跳过"}</span>
    </label>
  );
}

function AutomationSettingNumber({
  ariaLabel,
  disabled = false,
  max,
  min,
  onChange,
  step,
  suffix,
  value,
}: {
  ariaLabel: string;
  disabled?: boolean;
  max: number;
  min: number;
  onChange: (value: string) => void;
  step?: number | "any";
  suffix?: string;
  value: string;
}) {
  return (
    <div className="automation-setting-number">
      <input
        aria-label={ariaLabel}
        disabled={disabled}
        max={max}
        min={min}
        onChange={(event) => onChange(event.target.value)}
        step={step}
        type="number"
        value={value}
      />
      {suffix ? <span>{suffix}</span> : null}
    </div>
  );
}

function AutomationSettingInherited({ children }: { children: ReactNode }) {
  return (
    <div aria-disabled="true" className="automation-setting-inherited">
      {children}
    </div>
  );
}

function scrollToSettingsSection(sectionId: string) {
  document.getElementById(sectionId)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

const settingsNavigation: ReadonlyArray<{ icon: LucideIcon; id: string; label: string }> = [
  { icon: Link2, id: "settings-connection", label: "基础连接" },
  { icon: RefreshCcw, id: "settings-oauth", label: "OAuth 账号" },
  { icon: Database, id: "settings-api-key-sync", label: "API Key 与上游" },
  { icon: ShieldCheck, id: "settings-api-key-policies", label: "可用性与暂停" },
  { icon: TimerReset, id: "settings-usage", label: "用量与订阅" },
  { icon: Database, id: "settings-data-management", label: "数据管理" },
  { icon: Activity, id: "settings-notifications", label: "通知" },
  { icon: Globe2, id: "settings-display-security", label: "界面偏好" },
];

function FallbackModelChainDialog({
  availableModels,
  enabled,
  models,
  onChange,
  onClose,
}: {
  availableModels: AccountLivenessModel[];
  enabled: boolean;
  models: string[];
  onChange: (models: string[]) => void;
  onClose: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const availableModelIds = useMemo(
    () => Array.from(new Set(availableModels.map((model) => model.id.trim()).filter(Boolean))),
    [availableModels],
  );
  const nextModel = firstUnusedFallbackModel(availableModelIds, models);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop settings-model-chain-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-labelledby="fallback-model-chain-title" aria-modal="true" className="mail-dialog settings-model-chain-dialog" ref={dialogRef} role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">API Key 可用性检测</p>
            <h2 id="fallback-model-chain-title">配置回退测试模型链</h2>
          </div>
          <button aria-label="关闭回退测试模型链配置" className="icon-button" onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>

        <p className="settings-model-chain-dialog-copy">
          按从上到下的顺序选择账号白名单中第一个存在的模型。最多可配置 {MAX_FALLBACK_TEST_MODELS} 个模型。
        </p>

        <div className="settings-model-chain-list settings-model-chain-dialog-list">
          {models.length ? models.map((selectedModel, index) => {
            const configuredModel = availableModels.find((model) => model.id === selectedModel);
            const options = configuredModel
              ? availableModels
              : [{ id: selectedModel, display_name: `${selectedModel}（当前配置）` }, ...availableModels];
            return (
              <div className="settings-model-chain-row" key={selectedModel}>
                <span className="settings-model-chain-order" aria-hidden="true">{index + 1}</span>
                <select
                  aria-label={`回退测试模型 ${index + 1}`}
                  disabled={!enabled}
                  onChange={(event) => {
                    const model = event.target.value;
                    onChange(models.map((item, itemIndex) => itemIndex === index ? model : item));
                  }}
                  value={selectedModel}
                >
                  {options.map((model) => (
                    <option
                      disabled={model.id !== selectedModel && models.includes(model.id)}
                      key={model.id}
                      value={model.id}
                    >
                      {model.display_name || model.id}
                    </option>
                  ))}
                </select>
                <button
                  aria-label={`上移回退测试模型 ${selectedModel}`}
                  className="icon-button settings-model-chain-action"
                  disabled={!enabled || index === 0}
                  onClick={() => onChange(moveFallbackModel(models, index, -1))}
                  title="上移"
                  type="button"
                >
                  <ArrowUp size={15} />
                </button>
                <button
                  aria-label={`下移回退测试模型 ${selectedModel}`}
                  className="icon-button settings-model-chain-action"
                  disabled={!enabled || index === models.length - 1}
                  onClick={() => onChange(moveFallbackModel(models, index, 1))}
                  title="下移"
                  type="button"
                >
                  <ArrowDown size={15} />
                </button>
                <button
                  aria-label={`删除回退测试模型 ${selectedModel}`}
                  className="icon-button danger settings-model-chain-action"
                  disabled={!enabled}
                  onClick={() => onChange(models.filter((_, itemIndex) => itemIndex !== index))}
                  title="删除"
                  type="button"
                >
                  <Trash2 size={15} />
                </button>
              </div>
            );
          }) : (
            <span className="settings-model-chain-empty">尚未配置回退测试模型</span>
          )}
        </div>

        <div className="settings-model-chain-dialog-actions">
          <button
            className="secondary-button settings-model-chain-add"
            disabled={!enabled || !nextModel || models.length >= MAX_FALLBACK_TEST_MODELS}
            onClick={() => {
              if (nextModel) onChange([...models, nextModel]);
            }}
            type="button"
          >
            <Plus size={15} />
            新增模型
          </button>
          <button className="primary-button" onClick={onClose} type="button">完成</button>
        </div>
      </section>
    </div>
  );
}

type DiscordSetupScreenshot = {
  alt: string;
  src: string;
  title: string;
};

function DiscordBotSetupScreenshotPreviewDialog({
  onClose,
  screenshot,
}: {
  onClose: () => void;
  screenshot: DiscordSetupScreenshot;
}) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop discord-bot-screenshot-preview-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-labelledby="discord-bot-screenshot-preview-title" aria-modal="true" className="mail-dialog discord-bot-screenshot-preview-dialog" ref={dialogRef} role="dialog">
        <header className="mail-dialog-head">
          <h2 id="discord-bot-screenshot-preview-title">{screenshot.title}</h2>
          <button aria-label="关闭截图预览" className="icon-button" onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="discord-bot-screenshot-preview-body">
          <img alt={screenshot.alt} src={screenshot.src} />
        </div>
      </section>
    </div>
  );
}

function DiscordBotSetupGuideDialog({ onClose }: { onClose: () => void }) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const screenshotTriggerRef = useRef<HTMLButtonElement | null>(null);
  const [previewScreenshot, setPreviewScreenshot] = useState<DiscordSetupScreenshot | null>(null);
  const screenshotPreviewOpenRef = useRef(false);
  screenshotPreviewOpenRef.current = previewScreenshot !== null;
  const closeScreenshotPreview = useCallback(() => {
    setPreviewScreenshot(null);
    window.requestAnimationFrame(() => screenshotTriggerRef.current?.focus());
  }, []);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (screenshotPreviewOpenRef.current) return;
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
      ));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  return (
    <div
      className="modal-backdrop settings-model-chain-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section aria-labelledby="discord-bot-setup-guide-title" aria-modal="true" className="mail-dialog discord-bot-setup-dialog" ref={dialogRef} role="dialog">
        <header className="mail-dialog-head">
          <div>
            <p className="eyebrow">Discord Bot 通知</p>
            <h2 id="discord-bot-setup-guide-title">配置指南</h2>
          </div>
          <button aria-label="关闭 Discord Bot 配置指南" className="icon-button" onClick={onClose} ref={closeButtonRef} title="关闭" type="button">
            <X size={17} />
          </button>
        </header>
        <div className="discord-bot-setup-guide-body">
          <p className="discord-bot-setup-guide-intro">推荐且受支持的方式是“私有 Bot + 服务器安装 + 单一通知频道”。不要只做用户安装，也不要把私信频道 ID 填作通知频道。</p>
          <div className="discord-bot-security-note">
            <ShieldCheck size={18} />
            <div>
              <strong>私有安装</strong>
              <span>关闭 Public Bot 后，只有应用所有者或团队成员可以把 Bot 加入服务器；现有服务器安装不会因此失效。</span>
            </div>
          </div>
          <section aria-labelledby="discord-bot-install-modes-title" className="discord-bot-install-modes">
            <div className="discord-bot-install-modes-head">
              <h3 id="discord-bot-install-modes-title">安装方式与区别</h3>
              <p><code>Public / Private</code> 决定谁能安装，<code>User / Guild Install</code> 决定安装到哪里，两组设置互不替代。</p>
            </div>
            <div className="discord-bot-install-mode-grid">
              <div className="discord-bot-install-mode">
                <strong><code>Public Bot</code>（公开）</strong>
                <p>在 <code>Bot</code> 页面开启 <code>Public Bot</code>，为 <code>Guild Install</code> 配置安装链接后即可分享；任何拿到链接、且有目标服务器管理权限的人都能安装。公开不会直接暴露 Token，但会扩大滥用、垃圾交互和运维排查范围。</p>
              </div>
              <div className="discord-bot-install-mode">
                <strong><code>Private Bot</code>（私有，推荐）</strong>
                <p>关闭 <code>Public Bot</code>，只有应用所有者或团队成员可以安装。将 <code>Install Link</code> 设为 <code>None</code>，再通过 <code>OAuth2 / URL Generator</code> 生成自用链接。</p>
              </div>
              <div className="discord-bot-install-mode">
                <strong><code>User Install</code>（用户安装）</strong>
                <p>应用安装到个人账号或私信上下文，Bot 不会成为服务器成员，也无法按本插件要求访问服务器通知频道，因此不适用于本插件。</p>
              </div>
              <div className="discord-bot-install-mode">
                <strong><code>Guild Install</code>（服务器安装）</strong>
                <p>Bot 会真正加入所选服务器，可取得频道权限并注册服务器命令。本插件仅支持这种安装方式，安装者需要目标服务器的“管理服务器”权限。</p>
              </div>
            </div>
          </section>
          <ol className="discord-bot-setup-guide-steps">
            <li className="discord-bot-setup-step">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">1</span>
                <div>
                  <strong>创建私有 Bot 并取得 Token</strong>
                  <p>在 Developer Portal 创建或选择应用，进入 <code>Bot</code> 页面创建 Bot。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li>关闭 <code>Public Bot</code>，限制为应用所有者或团队成员安装。</li>
                    <li>本插件不需要 Message Content、Server Members 或 Presence 等特权 Intent，可保持关闭。</li>
                    <li>点击 <code>Reset Token</code> 取得 Token；Token 只填入本页，重新生成后必须同步更新。</li>
                  </ul>
                  <a className="discord-bot-setup-link" href="https://discord.com/developers/applications" rel="noreferrer" target="_blank">
                    打开 Developer Portal <ExternalLink size={14} />
                  </a>
                </div>
              </div>
              <button
                aria-label="打开创建 Bot 并取得 Token 的截图"
                className="discord-bot-setup-screenshot-button"
                onClick={(event) => {
                  screenshotTriggerRef.current = event.currentTarget;
                  setPreviewScreenshot({
                    alt: "Discord Developer Portal 的机器人页面，令牌区域位于用户名下方",
                    src: "/discord-bot-token-guide.png",
                    title: "创建 Bot 并取得 Token",
                  });
                }}
                type="button"
              >
                <img alt="Discord Developer Portal 的机器人页面，令牌区域位于用户名下方" className="discord-bot-setup-screenshot" loading="lazy" src="/discord-bot-token-guide.png" />
                <span aria-hidden="true" className="discord-bot-setup-screenshot-action"><ZoomIn size={16} />点击查看原图</span>
              </button>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">2</span>
                <div>
                  <strong>配置服务器安装范围</strong>
                  <p>进入 <code>Installation</code> 页面，将安装方式限定为服务器安装。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li><code>Installation Contexts</code> 只保留 <code>Guild Install</code>，取消 <code>User Install</code>。</li>
                    <li><code>Install Link</code> 选择 <code>None</code>。私有 Bot 使用 Discord 提供的默认链接会导致 <code>Public Bot</code> 无法保存为关闭。</li>
                  </ul>
                  <a className="discord-bot-setup-link" href="https://discord.com/developers/docs/tutorials/setting-up-a-bot-application" rel="noreferrer" target="_blank">
                    打开官方安装说明 <ExternalLink size={14} />
                  </a>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">3</span>
                <div>
                  <strong>生成服务器安装链接并安装</strong>
                  <p>进入 <code>OAuth2 / URL Generator</code>，生成一次只供自己使用的服务器安装链接。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li><code>Scopes</code> 勾选 <code>bot</code> 与 <code>applications.commands</code>。</li>
                    <li><code>Bot Permissions</code> 只勾选 <code>View Channels</code>、<code>Send Messages</code>、<code>Embed Links</code>，不要授予 <code>Administrator</code>。</li>
                    <li>复制页面底部生成的 URL，用应用所有者或团队成员账号打开，选择你管理的目标服务器并完成授权。</li>
                    <li>安装完成后，先在服务器成员列表确认 Bot 已出现。</li>
                    <li>如果服务器不可选，当前 Discord 账号需要该服务器的“管理服务器”权限。</li>
                  </ul>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">4</span>
                <div>
                  <strong>检查频道权限并复制频道 ID</strong>
                  <p>在目标服务器频道的权限设置中，确认 Bot 能查看频道、发送消息和嵌入链接。随后在 Discord 客户端的“高级设置”中启用开发者模式，右键该服务器频道并选择“复制频道 ID”。</p>
                  <a className="discord-bot-setup-link" href="https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID" rel="noreferrer" target="_blank">
                    打开官方开发者模式说明 <ExternalLink size={14} />
                  </a>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">5</span>
                <div>
                  <strong>保存插件设置</strong>
                  <p>回到本页填写 Bot Token 和服务器频道 ID，启用 Discord Bot 通知，选择需要的通知事件并保存。保存后，后端会在该服务器注册 <code>/balance</code> 与 <code>/quota</code>，通常数秒内出现。</p>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">6</span>
                <div>
                  <strong>发送测试并验证命令</strong>
                  <p>点击“发送测试通知”，再到已配置频道输入 <code>/balance</code> 或 <code>/quota</code>。频道不存在、Bot 未加入服务器和权限不足会分别显示具体提示。</p>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">7</span>
                <div>
                  <strong>现有安装是否需要重装</strong>
                  <ul className="discord-bot-setup-detail-list">
                    <li>Bot 已在目标服务器，且安装时已有 <code>bot</code> 与 <code>applications.commands</code>：关闭 Public Bot 后无需重装。</li>
                    <li>之前只按旧指南做了 User Install 或只装到私信：需要执行一次 Guild Install，让 Bot 真正加入目标服务器。</li>
                    <li>缺少命令 scope 时需要重新打开安装链接授权；仅修改频道权限时无需重装。</li>
                    <li>旧的用户安装可在 Discord“用户设置 / 已授权的应用”中移除。</li>
                  </ul>
                </div>
              </div>
            </li>
            <li className="discord-bot-setup-step discord-bot-setup-step--compact">
              <div className="discord-bot-setup-step-copy">
                <span className="discord-bot-setup-step-number">8</span>
                <div>
                  <strong>卸载或永久删除 Bot</strong>
                  <p>先确认需要移除的范围。仅停止本插件通知时，不需要删除整个 Discord 应用。</p>
                  <ul className="discord-bot-setup-detail-list">
                    <li><strong>从某个服务器卸载：</strong>先在本插件设置中关闭 Discord Bot 通知并保存，再到该服务器的成员列表，右键 Bot 并选择“移出服务器”或“踢出”。其他服务器中的安装不受影响。</li>
                    <li><strong>撤销旧的 User Install：</strong>打开 Discord“用户设置 / 已授权的应用”，找到该应用并选择“取消授权”。这不会自动移除服务器中的 Guild Install。</li>
                    <li><strong>永久删除：</strong>进入 <code>Developer Portal / 应用 / General Information / Delete App</code>，按提示确认。这里删除的是整个应用，不是单独删除 Bot；Token、所有服务器安装和命令都会失效，且不可恢复。</li>
                  </ul>
                </div>
              </div>
            </li>
          </ol>
          <p className="discord-bot-setup-guide-note">Public Bot 不会直接泄露 Token，但会允许其他人把应用安装到更多服务器，增加滥用、垃圾交互和运维排查面。此插件只需要一个私有服务器安装。不要把 Bot Token 发到聊天、截图或提交到代码仓库。</p>
        </div>
      </section>
      {previewScreenshot ? <DiscordBotSetupScreenshotPreviewDialog onClose={closeScreenshotPreview} screenshot={previewScreenshot} /> : null}
    </div>
  );
}

function SettingsView({
  settings,
  logoUrl,
  subscriptionTypes,
  busy,
  onSave,
  onScan,
  onTestNotification,
  onValidityChange,
}: {
  settings: AppSettings;
  logoUrl: string;
  subscriptionTypes: string[];
  busy: boolean;
  onSave: (
    payload: AppSettingsUpdate,
    branding?: { logoFile: File | null; resetLogo: boolean },
  ) => Promise<void> | void;
  onScan: () => Promise<void> | void;
  onTestNotification: () => Promise<void> | void;
  onValidityChange: (invalid: boolean) => void;
}) {
  const [siteName, setSiteName] = useState(settings.site_name || defaultSiteName);
  const [instanceUrl, setInstanceUrl] = useState(toSub2ApiInstanceUrl(settings.sub2api_base_url));
  const [recoveryEnabled, setRecoveryEnabled] = useState(settings.recovery_enabled);
  const [autoRecoverState, setAutoRecoverState] = useState(settings.sub2api_auto_recover_state);
  const [automationPaused, setAutomationPaused] = useState(settings.automation_paused);
  const [oauthAccountSyncEnabled, setOauthAccountSyncEnabled] = useState(settings.oauth_account_sync_enabled ?? true);
  const [oauthLoginMode, setOauthLoginMode] = useState<"protocol" | "browser">(
    settings.oauth_login_mode ?? "protocol",
  );
  const [oauthStopOnPhoneVerification, setOauthStopOnPhoneVerification] = useState(
    settings.oauth_stop_on_phone_verification ?? false,
  );
  const [interval, setInterval] = useState(String(settings.monitor_interval_seconds));
  const [usageRefreshEnabled, setUsageRefreshEnabled] = useState(settings.usage_refresh_enabled);
  const [usageRefreshInterval, setUsageRefreshInterval] = useState(String(settings.usage_refresh_interval_seconds));
  const [usageRefreshMaxConcurrency, setUsageRefreshMaxConcurrency] = useState(
    String(settings.usage_refresh_max_concurrency ?? 20),
  );
  const [apiKeyAccountSyncEnabled, setApiKeyAccountSyncEnabled] = useState(settings.api_key_account_sync_enabled ?? true);
  const [apiKeyAccountSyncInterval, setApiKeyAccountSyncInterval] = useState(
    String(settings.api_key_account_sync_interval_seconds ?? 300),
  );
  const [upstreamSyncEnabled, setUpstreamSyncEnabled] = useState(settings.upstream_sync_enabled ?? false);
  const [upstreamSyncInterval, setUpstreamSyncInterval] = useState(
    String(settings.upstream_sync_interval_seconds ?? 900),
  );
  const [upstreamSyncMaxConcurrency, setUpstreamSyncMaxConcurrency] = useState(
    String(settings.upstream_sync_max_concurrency ?? 10),
  );
  const [upstreamRateSyncEnabled, setUpstreamRateSyncEnabled] = useState(settings.upstream_rate_sync_enabled ?? false);
  const [upstreamPrioritySyncEnabled, setUpstreamPrioritySyncEnabled] = useState(
    settings.upstream_priority_sync_enabled ?? true,
  );
  const [manualUpstreamRateEnabled, setManualUpstreamRateEnabled] = useState(
    settings.manual_upstream_sync_rate_enabled ?? true,
  );
  const [manualUpstreamPriorityEnabled, setManualUpstreamPriorityEnabled] = useState(
    settings.manual_upstream_sync_priority_enabled ?? true,
  );
  const [manualUpstreamHealthEnabled, setManualUpstreamHealthEnabled] = useState(
    settings.manual_upstream_sync_upstream_health_enabled ?? true,
  );
  const [manualChannelMonitorsEnabled, setManualChannelMonitorsEnabled] = useState(
    settings.manual_upstream_sync_channel_monitors_enabled ?? true,
  );
  const [manualAccountAvailabilityEnabled, setManualAccountAvailabilityEnabled] = useState(
    settings.manual_upstream_sync_account_availability_enabled ?? false,
  );
  const [manualBalanceGuardEnabled, setManualBalanceGuardEnabled] = useState(
    settings.manual_upstream_sync_balance_guard_enabled ?? true,
  );
  const [manualRatePauseEnabled, setManualRatePauseEnabled] = useState(
    settings.manual_upstream_sync_rate_pause_enabled ?? true,
  );
  const [apiKeyAutoDisableEnabled, setApiKeyAutoDisableEnabled] = useState(
    settings.api_key_auto_disable_on_upstream_unavailable ?? false,
  );
  const [apiKeyNegativeBalancePauseEnabled, setApiKeyNegativeBalancePauseEnabled] = useState(
    settings.api_key_auto_pause_on_negative_balance_enabled ?? false,
  );
  const [apiKeyChannelMonitorPauseEnabled, setApiKeyChannelMonitorPauseEnabled] = useState(
    settings.api_key_auto_pause_on_channel_monitor_unavailable_enabled ?? false,
  );
  const [apiKeyAvailabilityAllTestsMustSucceed, setApiKeyAvailabilityAllTestsMustSucceed] = useState(
    settings.api_key_availability_all_tests_must_succeed ?? false,
  );
  const [channelMonitorAutoProbeEnabled, setChannelMonitorAutoProbeEnabled] = useState(
    settings.channel_monitor_auto_probe_enabled ?? true,
  );
  const [accountModelWhitelistSyncEnabled, setAccountModelWhitelistSyncEnabled] = useState(
    settings.account_model_whitelist_sync_enabled ?? settings.account_model_whitelist_sync_each_time ?? false,
  );
  const [accountModelWhitelistSyncInterval, setAccountModelWhitelistSyncInterval] = useState(
    String(settings.account_model_whitelist_sync_interval_seconds ?? 3600),
  );
  const [channelMonitorFallbackWithoutMonitorEnabled, setChannelMonitorFallbackWithoutMonitorEnabled] = useState(
    settings.channel_monitor_fallback_without_monitor_enabled ?? false,
  );
  const [channelMonitorFallbackTestModels, setChannelMonitorFallbackTestModels] = useState<string[]>(() =>
    normalizeFallbackModelChain(
      settings.channel_monitor_fallback_test_models,
      settings.channel_monitor_fallback_test_model,
    ),
  );
  const [fallbackModelDialogOpen, setFallbackModelDialogOpen] = useState(false);
  const fallbackModelDialogTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeFallbackModelDialog = useCallback(() => {
    setFallbackModelDialogOpen(false);
    window.requestAnimationFrame(() => fallbackModelDialogTriggerRef.current?.focus());
  }, []);
  const [channelMonitorFallbackTestAttempts, setChannelMonitorFallbackTestAttempts] = useState(
    String(settings.channel_monitor_fallback_test_attempts ?? 1),
  );
  const [channelMonitorRecoveryTestAttempts, setChannelMonitorRecoveryTestAttempts] = useState(
    String(settings.channel_monitor_recovery_test_attempts ?? 1),
  );
  const [channelMonitorTestAttemptInterval, setChannelMonitorTestAttemptInterval] = useState(
    String(settings.channel_monitor_test_attempt_interval_seconds ?? 0),
  );
  const [negativeBalanceBasis, setNegativeBalanceBasis] = useState<"wallet" | "recharge_adjusted">(
    settings.upstream_negative_balance_basis || "wallet",
  );
  const [balancePauseThreshold, setBalancePauseThreshold] = useState(
    String(settings.upstream_balance_pause_threshold ?? 0),
  );
  const [showStaleNegativeBalanceAlert, setShowStaleNegativeBalanceAlert] = useState(
    settings.show_stale_negative_balance_alert ?? true,
  );
  const [priorityAssignDisabledAccounts, setPriorityAssignDisabledAccounts] = useState(
    settings.priority_assign_disabled_api_key_accounts ?? false,
  );
  const [priorityShareSameCompositeMultiplier, setPriorityShareSameCompositeMultiplier] = useState(
    settings.priority_share_same_composite_multiplier ?? false,
  );
  const [discordNotificationsEnabled, setDiscordNotificationsEnabled] = useState(
    settings.discord_bot_notifications_enabled ?? false,
  );
  const [discordSetupGuideOpen, setDiscordSetupGuideOpen] = useState(false);
  const discordSetupGuideTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeDiscordSetupGuide = useCallback(() => {
    setDiscordSetupGuideOpen(false);
    window.requestAnimationFrame(() => discordSetupGuideTriggerRef.current?.focus());
  }, []);
  const [discordBotToken, setDiscordBotToken] = useState("");
  const [clearDiscordBotToken, setClearDiscordBotToken] = useState(false);
  const [discordChannelId, setDiscordChannelId] = useState(settings.discord_bot_channel_id || "");
  const [notifyAccountScheduling, setNotifyAccountScheduling] = useState(
    (settings.notify_oauth_account_disabled ?? false) || (settings.notify_account_enabled ?? false),
  );
  const [notifyApiKeyRateChanged, setNotifyApiKeyRateChanged] = useState(settings.notify_api_key_rate_changed ?? false);
  const [notifyUpstreamGroupChanged, setNotifyUpstreamGroupChanged] = useState(settings.notify_upstream_group_changed ?? false);
  const [notifyUpstreamBalanceLow, setNotifyUpstreamBalanceLow] = useState(settings.notify_upstream_balance_low ?? false);
  const [notifyUpstreamTokenInvalid, setNotifyUpstreamTokenInvalid] = useState(
    settings.notify_upstream_token_invalid ?? false,
  );
  const [upstreamRateLogRetentionDays, setUpstreamRateLogRetentionDays] = useState(
    String(settings.upstream_rate_log_retention_days || 90),
  );
  const [changeLogPageSize, setChangeLogPageSize] = useState(settings.change_log_page_size || 50);
  const [changeLogPageSizeOptionsInput, setChangeLogPageSizeOptionsInput] = useState(
    normalizeChangeLogPageSizeOptions(settings.change_log_page_size_options).join(", "),
  );
  const [upstreamUsageDataRetentionDays, setUpstreamUsageDataRetentionDays] = useState(
    String(settings.upstream_usage_data_retention_days ?? settings.upstream_data_retention_days ?? 90),
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
    String(settings.protocol_refresh_max_concurrency ?? 2),
  );
  const [browserRefreshMaxConcurrency, setBrowserRefreshMaxConcurrency] = useState(
    String(settings.browser_refresh_max_concurrency ?? 1),
  );
  const [browserMinAvailableMemoryMb, setBrowserMinAvailableMemoryMb] = useState(
    String(settings.browser_min_available_memory_mb ?? 500),
  );
  const [subscriptionRefreshBatchSize, setSubscriptionRefreshBatchSize] = useState(
    String(settings.subscription_refresh_batch_size || 3),
  );
  const [subscriptionRefreshMaxConcurrency, setSubscriptionRefreshMaxConcurrency] = useState(
    String(settings.subscription_refresh_max_concurrency ?? 3),
  );
  const [accountLivenessMaxConcurrency, setAccountLivenessMaxConcurrency] = useState(
    String(settings.account_liveness_max_concurrency ?? 3),
  );
  const [displayTimeZone, setDisplayTimeZone] = useState(settings.display_timezone || defaultTimeZone);
  const [xApiKey, setXApiKey] = useState("");
  const [clearXApiKey, setClearXApiKey] = useState(false);
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [logoPreviewUrl, setLogoPreviewUrl] = useState<string | null>(null);
  const [resetLogo, setResetLogo] = useState(false);
  const [logoError, setLogoError] = useState("");
  const settingsPageRef = useRef<HTMLDivElement | null>(null);
  const [activeSettingsSection, setActiveSettingsSection] = useState(settingsNavigation[0].id);

  useEffect(() => {
    if (!logoFile) {
      setLogoPreviewUrl(null);
      return;
    }
    const previewUrl = URL.createObjectURL(logoFile);
    setLogoPreviewUrl(previewUrl);
    return () => URL.revokeObjectURL(previewUrl);
  }, [logoFile]);

  useEffect(() => {
    const settingsPage = settingsPageRef.current;
    const workspace = settingsPage?.closest<HTMLElement>(".workspace--settings");
    const documentScrollRoot = document.scrollingElement as HTMLElement | null;
    if (!settingsPage) return undefined;

    const getScrollRoot = () => {
      const workspaceStyle = workspace ? window.getComputedStyle(workspace) : null;
      const workspaceOwnsScroll = Boolean(
        workspace
        && workspace.scrollHeight > workspace.clientHeight
        && ["auto", "overlay", "scroll"].includes(workspaceStyle?.overflowY || ""),
      );
      return workspaceOwnsScroll ? workspace : documentScrollRoot;
    };

    let animationFrame = 0;
    const updateActiveSettingsSection = () => {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(() => {
        const scrollRoot = getScrollRoot();
        if (!scrollRoot) return;
        const rootTop = scrollRoot === documentScrollRoot ? 0 : scrollRoot.getBoundingClientRect().top;
        const activationLine = rootTop
          + Math.min(220, scrollRoot.clientHeight * 0.3);
        const maxScrollTop = scrollRoot.scrollHeight - scrollRoot.clientHeight;
        const isAtBottom = maxScrollTop > 0 && scrollRoot.scrollTop >= maxScrollTop - 2;
        let nextSectionId = settingsNavigation[0].id;

        for (const item of settingsNavigation) {
          const section = settingsPage.querySelector<HTMLElement>(`#${item.id}`);
          if (!section) continue;
          if (section.getBoundingClientRect().top > activationLine) break;
          nextSectionId = item.id;
        }

        if (isAtBottom) {
          nextSectionId = settingsNavigation[settingsNavigation.length - 1].id;
        }
        setActiveSettingsSection((current) => current === nextSectionId ? current : nextSectionId);
      });
    };

    const scrollTargets: HTMLElement[] = [];
    if (workspace) scrollTargets.push(workspace);
    if (documentScrollRoot && documentScrollRoot !== workspace) scrollTargets.push(documentScrollRoot);
    scrollTargets.forEach((target) => target.addEventListener("scroll", updateActiveSettingsSection, { passive: true }));
    window.addEventListener("scroll", updateActiveSettingsSection, { passive: true });
    window.addEventListener("resize", updateActiveSettingsSection);
    updateActiveSettingsSection();

    return () => {
      scrollTargets.forEach((target) => target.removeEventListener("scroll", updateActiveSettingsSection));
      window.removeEventListener("scroll", updateActiveSettingsSection);
      window.removeEventListener("resize", updateActiveSettingsSection);
      window.cancelAnimationFrame(animationFrame);
    };
  }, []);

  useEffect(() => {
    const settingsPage = settingsPageRef.current;
    const nav = settingsPage?.querySelector<HTMLElement>(".settings-local-nav");
    const activeButton = nav?.querySelector<HTMLElement>('button[aria-current="location"]');
    if (!nav || !activeButton) return undefined;

    const navRect = nav.getBoundingClientRect();
    const buttonRect = activeButton.getBoundingClientRect();
    const edgePadding = 8;
    if (buttonRect.left < navRect.left + edgePadding) {
      nav.scrollTo({
        behavior: "smooth",
        left: nav.scrollLeft - (navRect.left + edgePadding - buttonRect.left),
      });
    } else if (buttonRect.right > navRect.right - edgePadding) {
      nav.scrollTo({
        behavior: "smooth",
        left: nav.scrollLeft + (buttonRect.right - (navRect.right - edgePadding)),
      });
    }
    return undefined;
  }, [activeSettingsSection]);

  useEffect(() => {
    setSiteName(settings.site_name || defaultSiteName);
    setInstanceUrl(toSub2ApiInstanceUrl(settings.sub2api_base_url));
    setRecoveryEnabled(settings.recovery_enabled);
    setAutoRecoverState(settings.sub2api_auto_recover_state);
    setAutomationPaused(settings.automation_paused);
    setOauthAccountSyncEnabled(settings.oauth_account_sync_enabled ?? true);
    setOauthLoginMode(settings.oauth_login_mode ?? "protocol");
    setOauthStopOnPhoneVerification(settings.oauth_stop_on_phone_verification ?? false);
    setInterval(String(settings.monitor_interval_seconds));
    setUsageRefreshEnabled(settings.usage_refresh_enabled);
    setUsageRefreshInterval(String(settings.usage_refresh_interval_seconds));
    setUsageRefreshMaxConcurrency(String(settings.usage_refresh_max_concurrency ?? 20));
    setApiKeyAccountSyncEnabled(settings.api_key_account_sync_enabled ?? true);
    setApiKeyAccountSyncInterval(String(settings.api_key_account_sync_interval_seconds ?? 300));
    setUpstreamSyncEnabled(settings.upstream_sync_enabled ?? false);
    setUpstreamSyncInterval(String(settings.upstream_sync_interval_seconds ?? 900));
    setUpstreamSyncMaxConcurrency(String(settings.upstream_sync_max_concurrency ?? 10));
    setUpstreamRateSyncEnabled(settings.upstream_rate_sync_enabled ?? false);
    setUpstreamPrioritySyncEnabled(settings.upstream_priority_sync_enabled ?? true);
    setManualUpstreamRateEnabled(settings.manual_upstream_sync_rate_enabled ?? true);
    setManualUpstreamPriorityEnabled(settings.manual_upstream_sync_priority_enabled ?? true);
    setManualUpstreamHealthEnabled(settings.manual_upstream_sync_upstream_health_enabled ?? true);
    setManualChannelMonitorsEnabled(settings.manual_upstream_sync_channel_monitors_enabled ?? true);
    setManualAccountAvailabilityEnabled(
      settings.manual_upstream_sync_account_availability_enabled ?? false,
    );
    setManualBalanceGuardEnabled(settings.manual_upstream_sync_balance_guard_enabled ?? true);
    setManualRatePauseEnabled(settings.manual_upstream_sync_rate_pause_enabled ?? true);
    setApiKeyAutoDisableEnabled(settings.api_key_auto_disable_on_upstream_unavailable ?? false);
    setApiKeyNegativeBalancePauseEnabled(settings.api_key_auto_pause_on_negative_balance_enabled ?? false);
    setApiKeyChannelMonitorPauseEnabled(
      settings.api_key_auto_pause_on_channel_monitor_unavailable_enabled ?? false,
    );
    setApiKeyAvailabilityAllTestsMustSucceed(
      settings.api_key_availability_all_tests_must_succeed ?? false,
    );
    setChannelMonitorAutoProbeEnabled(settings.channel_monitor_auto_probe_enabled ?? true);
    setAccountModelWhitelistSyncEnabled(
      settings.account_model_whitelist_sync_enabled ?? settings.account_model_whitelist_sync_each_time ?? false,
    );
    setAccountModelWhitelistSyncInterval(String(settings.account_model_whitelist_sync_interval_seconds ?? 3600));
    setChannelMonitorFallbackWithoutMonitorEnabled(
      settings.channel_monitor_fallback_without_monitor_enabled ?? false,
    );
    setChannelMonitorFallbackTestModels(normalizeFallbackModelChain(
      settings.channel_monitor_fallback_test_models,
      settings.channel_monitor_fallback_test_model,
    ));
    setChannelMonitorFallbackTestAttempts(String(settings.channel_monitor_fallback_test_attempts ?? 1));
    setChannelMonitorRecoveryTestAttempts(String(settings.channel_monitor_recovery_test_attempts ?? 1));
    setChannelMonitorTestAttemptInterval(String(settings.channel_monitor_test_attempt_interval_seconds ?? 0));
    setNegativeBalanceBasis(settings.upstream_negative_balance_basis || "wallet");
    setBalancePauseThreshold(String(settings.upstream_balance_pause_threshold ?? 0));
    setShowStaleNegativeBalanceAlert(settings.show_stale_negative_balance_alert ?? true);
    setPriorityAssignDisabledAccounts(settings.priority_assign_disabled_api_key_accounts ?? false);
    setPriorityShareSameCompositeMultiplier(
      settings.priority_share_same_composite_multiplier ?? false,
    );
    setDiscordNotificationsEnabled(settings.discord_bot_notifications_enabled ?? false);
    setDiscordBotToken("");
    setClearDiscordBotToken(false);
    setDiscordChannelId(settings.discord_bot_channel_id || "");
    setNotifyAccountScheduling(
      (settings.notify_oauth_account_disabled ?? false) || (settings.notify_account_enabled ?? false),
    );
    setNotifyApiKeyRateChanged(settings.notify_api_key_rate_changed ?? false);
    setNotifyUpstreamGroupChanged(settings.notify_upstream_group_changed ?? false);
    setNotifyUpstreamBalanceLow(settings.notify_upstream_balance_low ?? false);
    setNotifyUpstreamTokenInvalid(settings.notify_upstream_token_invalid ?? false);
    setUpstreamRateLogRetentionDays(String(settings.upstream_rate_log_retention_days || 90));
    setChangeLogPageSize(settings.change_log_page_size || 50);
    setChangeLogPageSizeOptionsInput(
      normalizeChangeLogPageSizeOptions(settings.change_log_page_size_options).join(", "),
    );
    setUpstreamUsageDataRetentionDays(
      String(settings.upstream_usage_data_retention_days ?? settings.upstream_data_retention_days ?? 90),
    );
    setUsageLimitSampleFiveHourThreshold(String(settings.usage_limit_sample_five_hour_threshold_percent ?? 0));
    setUsageLimitSampleSevenDayThreshold(String(settings.usage_limit_sample_seven_day_threshold_percent ?? 0));
    setUsageLimitDefaultRanges(mergeUsageLimitDefaultRanges(settings.usage_limit_default_ranges, subscriptionTypes));
    setNewSubscriptionType("");
    setProtocolRefreshMaxConcurrency(
      String(settings.protocol_refresh_max_concurrency ?? 2),
    );
    setBrowserRefreshMaxConcurrency(String(settings.browser_refresh_max_concurrency ?? 1));
    setBrowserMinAvailableMemoryMb(String(settings.browser_min_available_memory_mb ?? 500));
    setSubscriptionRefreshBatchSize(String(settings.subscription_refresh_batch_size || 3));
    setSubscriptionRefreshMaxConcurrency(String(settings.subscription_refresh_max_concurrency ?? 3));
    setAccountLivenessMaxConcurrency(String(settings.account_liveness_max_concurrency ?? 3));
    setDisplayTimeZone(settings.display_timezone || defaultTimeZone);
    setXApiKey("");
    setClearXApiKey(false);
    setLogoFile(null);
    setResetLogo(false);
    setLogoError("");
  }, [
    settings.automation_paused,
    settings.account_liveness_max_concurrency,
    settings.api_key_account_sync_enabled,
    settings.api_key_account_sync_interval_seconds,
    settings.api_key_auto_disable_on_upstream_unavailable,
    settings.api_key_auto_pause_on_negative_balance_enabled,
    settings.api_key_auto_pause_on_channel_monitor_unavailable_enabled,
    settings.api_key_availability_all_tests_must_succeed,
    settings.channel_monitor_auto_probe_enabled,
    settings.manual_upstream_sync_rate_enabled,
    settings.manual_upstream_sync_priority_enabled,
    settings.manual_upstream_sync_upstream_health_enabled,
    settings.manual_upstream_sync_channel_monitors_enabled,
    settings.manual_upstream_sync_account_availability_enabled,
    settings.manual_upstream_sync_balance_guard_enabled,
    settings.manual_upstream_sync_rate_pause_enabled,
    settings.account_model_whitelist_sync_enabled,
    settings.account_model_whitelist_sync_interval_seconds,
    settings.account_model_whitelist_sync_each_time,
    settings.channel_monitor_fallback_without_monitor_enabled,
    settings.channel_monitor_fallback_test_models,
    settings.channel_monitor_fallback_test_model,
    settings.channel_monitor_fallback_test_attempts,
    settings.channel_monitor_recovery_test_attempts,
    settings.channel_monitor_test_attempt_interval_seconds,
    settings.upstream_negative_balance_basis,
    settings.upstream_balance_pause_threshold,
    settings.show_stale_negative_balance_alert,
    settings.priority_assign_disabled_api_key_accounts,
    settings.priority_share_same_composite_multiplier,
    settings.discord_bot_notifications_enabled,
    settings.discord_bot_token_hint,
    settings.discord_bot_token_set,
    settings.discord_bot_channel_id,
    settings.notify_oauth_account_disabled,
    settings.notify_account_enabled,
    settings.notify_api_key_rate_changed,
    settings.notify_upstream_group_changed,
    settings.notify_upstream_balance_low,
    settings.notify_upstream_token_invalid,
    settings.browser_min_available_memory_mb,
    settings.browser_refresh_max_concurrency,
    settings.display_timezone,
    settings.monitor_interval_seconds,
    settings.oauth_account_sync_enabled,
    settings.oauth_login_mode,
    settings.oauth_stop_on_phone_verification,
    settings.protocol_refresh_max_concurrency,
    settings.recovery_enabled,
    settings.site_name,
    settings.site_logo_url,
    settings.site_logo_updated_at,
    settings.subscription_refresh_batch_size,
    settings.subscription_refresh_max_concurrency,
    settings.sub2api_auto_recover_state,
    settings.sub2api_base_url,
    settings.sub2api_x_api_key_hint,
    settings.sub2api_x_api_key_set,
    settings.usage_refresh_enabled,
    settings.usage_refresh_interval_seconds,
    settings.usage_refresh_max_concurrency,
    settings.upstream_rate_log_retention_days,
    settings.change_log_page_size,
    settings.change_log_page_size_options,
    settings.upstream_usage_data_retention_days,
    settings.upstream_data_retention_days,
    settings.upstream_rate_sync_enabled,
    settings.upstream_priority_sync_enabled ?? true,
    settings.upstream_sync_enabled,
    settings.upstream_sync_interval_seconds,
    settings.upstream_sync_max_concurrency,
    settings.usage_limit_sample_five_hour_threshold_percent,
    settings.usage_limit_sample_seven_day_threshold_percent,
    settings.usage_limit_default_ranges,
    subscriptionTypesKey,
  ]);

  const intervalNumber = Number(interval);
  const usageRefreshIntervalNumber = Number(usageRefreshInterval);
  const usageRefreshMaxConcurrencyNumber = Number(usageRefreshMaxConcurrency);
  const apiKeyAccountSyncIntervalNumber = Number(apiKeyAccountSyncInterval);
  const accountModelWhitelistSyncIntervalNumber = Number(accountModelWhitelistSyncInterval);
  const upstreamSyncIntervalNumber = Number(upstreamSyncInterval);
  const upstreamSyncMaxConcurrencyNumber = Number(upstreamSyncMaxConcurrency);
  const balancePauseThresholdNumber = Number(balancePauseThreshold);
  const upstreamRateLogRetentionDaysNumber = Number(upstreamRateLogRetentionDays);
  const upstreamUsageDataRetentionDaysNumber = Number(upstreamUsageDataRetentionDays);
  const changeLogPageSizeOptions = parseChangeLogPageSizeOptions(changeLogPageSizeOptionsInput);
  const visibleChangeLogPageSizeOptions = changeLogPageSizeOptions
    || normalizeChangeLogPageSizeOptions(settings.change_log_page_size_options);
  const usageLimitSampleFiveHourThresholdNumber = Number(usageLimitSampleFiveHourThreshold);
  const usageLimitSampleSevenDayThresholdNumber = Number(usageLimitSampleSevenDayThreshold);
  const protocolRefreshMaxConcurrencyNumber = Number(protocolRefreshMaxConcurrency);
  const browserRefreshMaxConcurrencyNumber = Number(browserRefreshMaxConcurrency);
  const browserMinAvailableMemoryMbNumber = Number(browserMinAvailableMemoryMb);
  const subscriptionRefreshBatchSizeNumber = Number(subscriptionRefreshBatchSize);
  const subscriptionRefreshMaxConcurrencyNumber = Number(subscriptionRefreshMaxConcurrency);
  const accountLivenessMaxConcurrencyNumber = Number(accountLivenessMaxConcurrency);
  const channelMonitorTestAttemptIntervalNumber = Number(channelMonitorTestAttemptInterval);
  const cleanSiteName = siteName.trim();
  const cleanDiscordChannelId = discordChannelId.trim();
  const discordConfigurationInvalid = discordNotificationsEnabled && (
    !cleanDiscordChannelId
    || cleanDiscordChannelId.length > 64
    || ((!settings.discord_bot_token_set || clearDiscordBotToken) && !discordBotToken.trim())
  );
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
    intervalNumber > 86_400 ||
    !Number.isInteger(usageRefreshIntervalNumber) ||
    usageRefreshIntervalNumber < 60 ||
    usageRefreshIntervalNumber > 86_400 ||
    !Number.isInteger(usageRefreshMaxConcurrencyNumber) ||
    usageRefreshMaxConcurrencyNumber < 0 ||
    usageRefreshMaxConcurrencyNumber > 100 ||
    !Number.isInteger(apiKeyAccountSyncIntervalNumber) ||
    apiKeyAccountSyncIntervalNumber < 30 ||
    apiKeyAccountSyncIntervalNumber > 86_400 ||
    !Number.isInteger(accountModelWhitelistSyncIntervalNumber) ||
    accountModelWhitelistSyncIntervalNumber < 60 ||
    accountModelWhitelistSyncIntervalNumber > 86_400 ||
    !Number.isInteger(upstreamSyncIntervalNumber) ||
    upstreamSyncIntervalNumber < 60 ||
    upstreamSyncIntervalNumber > 86_400 ||
    !Number.isInteger(upstreamSyncMaxConcurrencyNumber) ||
    upstreamSyncMaxConcurrencyNumber < 0 ||
    upstreamSyncMaxConcurrencyNumber > 50 ||
    channelMonitorTestAttemptInterval.trim() === "" ||
    !Number.isInteger(channelMonitorTestAttemptIntervalNumber) ||
    channelMonitorTestAttemptIntervalNumber < 0 ||
    channelMonitorTestAttemptIntervalNumber > 300 ||
    !Number.isFinite(balancePauseThresholdNumber) ||
    balancePauseThresholdNumber < -1_000_000_000 ||
    balancePauseThresholdNumber > 1_000_000_000 ||
    !Number.isInteger(upstreamRateLogRetentionDaysNumber) ||
    upstreamRateLogRetentionDaysNumber < 1 ||
    upstreamRateLogRetentionDaysNumber > 3650 ||
    !Number.isInteger(upstreamUsageDataRetentionDaysNumber) ||
    upstreamUsageDataRetentionDaysNumber < 1 ||
    upstreamUsageDataRetentionDaysNumber > 3650 ||
    changeLogPageSizeOptions === null ||
    !changeLogPageSizeOptions.includes(changeLogPageSize) ||
    !Number.isFinite(usageLimitSampleFiveHourThresholdNumber) ||
    usageLimitSampleFiveHourThresholdNumber < 0 ||
    usageLimitSampleFiveHourThresholdNumber > 100 ||
    !Number.isFinite(usageLimitSampleSevenDayThresholdNumber) ||
    usageLimitSampleSevenDayThresholdNumber < 0 ||
    usageLimitSampleSevenDayThresholdNumber > 100 ||
    !Number.isInteger(protocolRefreshMaxConcurrencyNumber) ||
    protocolRefreshMaxConcurrencyNumber < 0 ||
    protocolRefreshMaxConcurrencyNumber > 50 ||
    !Number.isInteger(browserRefreshMaxConcurrencyNumber) ||
    browserRefreshMaxConcurrencyNumber < 0 ||
    browserRefreshMaxConcurrencyNumber > 50 ||
    !Number.isInteger(browserMinAvailableMemoryMbNumber) ||
    browserMinAvailableMemoryMbNumber < 0 ||
    browserMinAvailableMemoryMbNumber > 1_048_576 ||
    !Number.isInteger(subscriptionRefreshBatchSizeNumber) ||
    subscriptionRefreshBatchSizeNumber < 1 ||
    subscriptionRefreshBatchSizeNumber > 100 ||
    !Number.isInteger(subscriptionRefreshMaxConcurrencyNumber) ||
    subscriptionRefreshMaxConcurrencyNumber < 0 ||
    subscriptionRefreshMaxConcurrencyNumber > 20 ||
    !Number.isInteger(accountLivenessMaxConcurrencyNumber) ||
    accountLivenessMaxConcurrencyNumber < 0 ||
    accountLivenessMaxConcurrencyNumber > 50 ||
    usageLimitDefaultRangesInvalid ||
    discordConfigurationInvalid ||
    Boolean(logoError);

  useEffect(() => {
    onValidityChange(invalid);
  }, [invalid, onValidityChange]);

  useEffect(() => () => onValidityChange(false), [onValidityChange]);

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
    setUsageLimitDefaultRanges((current) => {
      const nextPlanRanges = {
        ...current[subscriptionType],
        [windowKey]: {
          ...current[subscriptionType][windowKey],
          [bound]: Number(value),
        },
      };
      return {
        ...current,
        [subscriptionType]: subscriptionType === "team" ? nextPlanRanges : deriveMonthlyUsageLimitRange(nextPlanRanges),
      };
    });
  };

  const selectLogoFile = (file: File | null) => {
    if (!file) {
      setLogoFile(null);
      setLogoError("");
      return;
    }
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      setLogoFile(null);
      setLogoError("Logo 仅支持 PNG、JPEG 或 WebP");
      return;
    }
    if (file.size > 1024 * 1024) {
      setLogoFile(null);
      setLogoError("Logo 文件不能超过 1 MB");
      return;
    }
    setLogoError("");
    setResetLogo(false);
    setLogoFile(file);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (invalid) return;
    const fallbackTestModels = normalizeFallbackModelChain(channelMonitorFallbackTestModels);
    const payload: AppSettingsUpdate = {
      site_name: cleanSiteName,
      sub2api_base_url: toSub2ApiInstanceUrl(instanceUrl),
      recovery_enabled: recoveryEnabled,
      sub2api_auto_recover_state: autoRecoverState,
      automation_paused: automationPaused,
      oauth_account_sync_enabled: oauthAccountSyncEnabled,
      oauth_login_mode: oauthLoginMode,
      oauth_stop_on_phone_verification: oauthStopOnPhoneVerification,
      monitor_interval_seconds: intervalNumber,
      usage_refresh_enabled: usageRefreshEnabled,
      usage_refresh_interval_seconds: usageRefreshIntervalNumber,
      usage_refresh_max_concurrency: usageRefreshMaxConcurrencyNumber,
      api_key_account_sync_enabled: apiKeyAccountSyncEnabled,
      api_key_account_sync_interval_seconds: apiKeyAccountSyncIntervalNumber,
      upstream_sync_enabled: upstreamSyncEnabled,
      upstream_sync_interval_seconds: upstreamSyncIntervalNumber,
      upstream_sync_max_concurrency: upstreamSyncMaxConcurrencyNumber,
      upstream_rate_sync_enabled: upstreamRateSyncEnabled,
      upstream_priority_sync_enabled: upstreamPrioritySyncEnabled,
      manual_upstream_sync_rate_enabled: manualUpstreamRateEnabled,
      manual_upstream_sync_priority_enabled: manualUpstreamPriorityEnabled,
      manual_upstream_sync_upstream_health_enabled: manualUpstreamHealthEnabled,
      manual_upstream_sync_channel_monitors_enabled: manualChannelMonitorsEnabled,
      manual_upstream_sync_account_availability_enabled: manualAccountAvailabilityEnabled,
      manual_upstream_sync_balance_guard_enabled: manualBalanceGuardEnabled,
      manual_upstream_sync_rate_pause_enabled: manualRatePauseEnabled,
      api_key_auto_disable_on_upstream_unavailable: apiKeyAutoDisableEnabled,
      api_key_auto_pause_on_negative_balance_enabled: apiKeyNegativeBalancePauseEnabled,
      api_key_auto_pause_on_channel_monitor_unavailable_enabled: apiKeyChannelMonitorPauseEnabled,
      api_key_availability_all_tests_must_succeed: apiKeyAvailabilityAllTestsMustSucceed,
      channel_monitor_auto_probe_enabled: channelMonitorAutoProbeEnabled,
      account_model_whitelist_sync_enabled: accountModelWhitelistSyncEnabled,
      account_model_whitelist_sync_interval_seconds: accountModelWhitelistSyncIntervalNumber,
      channel_monitor_fallback_without_monitor_enabled: channelMonitorFallbackWithoutMonitorEnabled,
      channel_monitor_fallback_test_models: fallbackTestModels,
      channel_monitor_fallback_test_model: fallbackTestModels[0] || "",
      channel_monitor_fallback_test_attempts: Math.max(1, Math.min(5, Number(channelMonitorFallbackTestAttempts) || 1)),
      channel_monitor_recovery_test_attempts: Math.max(1, Math.min(5, Number(channelMonitorRecoveryTestAttempts) || 1)),
      channel_monitor_test_attempt_interval_seconds: channelMonitorTestAttemptIntervalNumber,
      upstream_negative_balance_basis: negativeBalanceBasis,
      upstream_balance_pause_threshold: balancePauseThresholdNumber,
      show_stale_negative_balance_alert: showStaleNegativeBalanceAlert,
      priority_assign_disabled_api_key_accounts: priorityAssignDisabledAccounts,
      priority_share_same_composite_multiplier: priorityShareSameCompositeMultiplier,
      upstream_rate_log_retention_days: upstreamRateLogRetentionDaysNumber,
      change_log_page_size: changeLogPageSize,
      change_log_page_size_options: changeLogPageSizeOptions || visibleChangeLogPageSizeOptions,
      upstream_usage_data_retention_days: upstreamUsageDataRetentionDaysNumber,
      discord_bot_notifications_enabled: discordNotificationsEnabled,
      discord_bot_channel_id: cleanDiscordChannelId,
      notify_oauth_account_disabled: notifyAccountScheduling,
      notify_account_enabled: notifyAccountScheduling,
      notify_api_key_rate_changed: notifyApiKeyRateChanged,
      notify_upstream_group_changed: notifyUpstreamGroupChanged,
      notify_upstream_balance_low: notifyUpstreamBalanceLow,
      notify_upstream_token_invalid: notifyUpstreamTokenInvalid,
      usage_limit_sample_five_hour_threshold_percent: usageLimitSampleFiveHourThresholdNumber,
      usage_limit_sample_seven_day_threshold_percent: usageLimitSampleSevenDayThresholdNumber,
      usage_limit_default_ranges: usageLimitDefaultRanges,
      protocol_refresh_max_concurrency: protocolRefreshMaxConcurrencyNumber,
      browser_refresh_max_concurrency: browserRefreshMaxConcurrencyNumber,
      browser_min_available_memory_mb: browserMinAvailableMemoryMbNumber,
      subscription_refresh_batch_size: subscriptionRefreshBatchSizeNumber,
      subscription_refresh_max_concurrency: subscriptionRefreshMaxConcurrencyNumber,
      account_liveness_max_concurrency: accountLivenessMaxConcurrencyNumber,
      display_timezone: displayTimeZone,
    };
    if (xApiKey.trim()) {
      payload.sub2api_x_api_key = xApiKey.trim();
    }
    if (clearXApiKey) {
      payload.clear_sub2api_x_api_key = true;
    }
    if (clearDiscordBotToken) {
      payload.clear_discord_bot_token = true;
    } else if (discordBotToken.trim()) {
      payload.discord_bot_token = discordBotToken.trim();
    }
    await onSave(payload, { logoFile, resetLogo });
  };
  const automationSwitches = [
    oauthAccountSyncEnabled,
    apiKeyAccountSyncEnabled,
    recoveryEnabled,
    upstreamSyncEnabled,
    upstreamRateSyncEnabled,
    upstreamPrioritySyncEnabled,
    apiKeyAutoDisableEnabled,
    apiKeyNegativeBalancePauseEnabled,
    apiKeyChannelMonitorPauseEnabled,
    channelMonitorAutoProbeEnabled,
    accountModelWhitelistSyncEnabled,
    usageRefreshEnabled,
  ];
  const enabledAutomationCount = automationSwitches.filter(Boolean).length;

  return (
    <div className="settings-page" ref={settingsPageRef}>
      <nav aria-label="设置页面导航" className="settings-local-nav">
        <div className="settings-local-nav-heading">
          <span><Settings2 size={16} />设置分组</span>
          <small>{settingsNavigation.length} 个功能区</small>
        </div>
        <div className="settings-local-nav-items">
          {settingsNavigation.map(({ icon: Icon, id, label }) => (
            <button
              aria-current={activeSettingsSection === id ? "location" : undefined}
              key={id}
              onClick={() => scrollToSettingsSection(id)}
              type="button"
            >
              <Icon size={16} />
              <span>{label}</span>
            </button>
          ))}
        </div>
      </nav>

      <div className="settings-content">
        <section className="settings-runtime-bar" id="settings-runtime">
          <div className="settings-runtime-copy">
            <span>运行控制</span>
            <strong>{automationPaused ? "自动任务已暂停" : "自动任务运行中"}</strong>
            <small>已开启 {enabledAutomationCount}/{automationSwitches.length} 项自动任务</small>
          </div>
          <label className="checkbox-line settings-toggle settings-global-toggle">
            <input
              checked={automationPaused}
              onChange={(event) => setAutomationPaused(event.target.checked)}
              type="checkbox"
            />
            <span>暂停全部自动任务</span>
          </label>
        </section>

        <section className="settings-form-shell">
        <form className="settings-form" id="runtime-settings-form" onSubmit={submit}>
          <fieldset className="settings-section settings-section--connection" id="settings-connection">
            <legend>基础与连接</legend>
            <div className="settings-grid settings-main-grid settings-connection-grid">
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
            <div className="site-logo-setting">
              <div className="site-logo-preview">
                <img alt="站点 Logo 预览" onError={fallbackSiteLogo} src={resetLogo ? "/logo.png" : logoPreviewUrl || logoUrl} />
              </div>
              <div className="site-logo-controls">
                <span><ImageIcon size={15} />站点 Logo</span>
                <div>
                  <label className="secondary-button site-logo-upload">
                    <Upload size={16} />
                    <span>选择图片</span>
                    <input
                      accept="image/png,image/jpeg,image/webp"
                      onChange={(event) => selectLogoFile(event.target.files?.[0] || null)}
                      type="file"
                    />
                  </label>
                  <button
                    className="icon-button"
                    onClick={() => {
                      setLogoFile(null);
                      setLogoError("");
                      setResetLogo(true);
                    }}
                    title="恢复默认 Logo"
                    type="button"
                  >
                    <RefreshCcw size={16} />
                  </button>
                </div>
                {logoFile ? <small>{logoFile.name}</small> : resetLogo ? <small>保存后恢复默认 Logo</small> : null}
                {logoError ? <small className="form-error">{logoError}</small> : null}
              </div>
            </div>
            </div>

            <div className="settings-connection-tools">
              <div className="settings-connection-secret">
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
                  <label className="checkbox-line settings-toggle">
                    <input
                      checked={clearXApiKey}
                      onChange={(event) => setClearXApiKey(event.target.checked)}
                      type="checkbox"
                    />
                    <span>清空已保存密钥</span>
                  </label>
                </div>
                <div className="key-state">
                  <KeyRound size={16} />
                  {settings.sub2api_x_api_key_set ? (
                    <span className="settings-secret-state">
                      <span>已保存</span>
                      {settings.sub2api_x_api_key_hint ? (
                        <MiddleEllipsisText text={settings.sub2api_x_api_key_hint} />
                      ) : null}
                    </span>
                  ) : <span>未设置</span>}
                </div>
              </div>

              <div className="settings-scan-inline" id="settings-scan">
                <div className="settings-subsection-heading">
                  <span><Radar size={16} /><strong>连接检测</strong></span>
                  <button className="secondary-button" disabled={busy} onClick={onScan} type="button">
                    <Radar size={16} />
                    <span>扫描 sub2api</span>
                  </button>
                </div>
                <div className="settings-status settings-connection-status">
                  <SignalLine label="配置来源" value={sourceLabel(settings.sub2api_base_url_source)} />
                  <SignalLine label="当前地址" value={settings.sub2api_base_url} />
                  <SignalLine label="上次扫描" value={settings.last_scan_at ? formatDate(settings.last_scan_at, displayTimeZone) : "暂无"} />
                  <SignalLine label="扫描结果" value={settings.last_scan_message || "暂无"} />
                </div>
              </div>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--automation" id="settings-oauth">
            <legend>OAuth 账号</legend>
            <AutomationSettingsTable>
              <AutomationSettingRow
                checked={oauthAccountSyncEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="OAuth 账号同步间隔"
                    maxSeconds={86_400}
                    minSeconds={30}
                    onChange={setInterval}
                    value={interval}
                  />
                )}
                label="同步 sub2api OAuth 账号"
                onChange={setOauthAccountSyncEnabled}
                threads={<AutomationSettingInherited>无需设置</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={recoveryEnabled}
                interval={<AutomationSettingInherited>跟随 OAuth 同步</AutomationSettingInherited>}
                label="刷新 OAuth 账号凭证"
                onChange={setRecoveryEnabled}
                threads={(
                  <div className="automation-setting-split">
                    <label>
                      <span>协议</span>
                      <input
                        aria-label="OAuth 协议刷新线程数"
                        max={50}
                        min={0}
                        onChange={(event) => setProtocolRefreshMaxConcurrency(event.target.value)}
                        type="number"
                        value={protocolRefreshMaxConcurrency}
                      />
                    </label>
                    <label>
                      <span>浏览器</span>
                      <input
                        aria-label="OAuth 浏览器刷新线程数"
                        max={50}
                        min={0}
                        onChange={(event) => setBrowserRefreshMaxConcurrency(event.target.value)}
                        type="number"
                        value={browserRefreshMaxConcurrency}
                      />
                    </label>
                  </div>
                )}
              />
              <AutomationSettingRow
                checked={usageRefreshEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="OAuth 用量窗口同步间隔"
                    maxSeconds={86_400}
                    minSeconds={60}
                    onChange={setUsageRefreshInterval}
                    value={usageRefreshInterval}
                  />
                )}
                label="同步 OAuth 账号用量窗口"
                onChange={setUsageRefreshEnabled}
                threads={(
                  <AutomationSettingNumber
                    ariaLabel="OAuth 用量窗口同步线程数"
                    max={100}
                    min={0}
                    onChange={setUsageRefreshMaxConcurrency}
                    value={usageRefreshMaxConcurrency}
                  />
                )}
              />
            </AutomationSettingsTable>

            <div
              className="settings-auto-pause-policy settings-oauth-policy"
              role="group"
              aria-label="OAuth 重新登录策略"
            >
              <div className="settings-policy-heading">
                <span className="settings-label-with-help">
                  <strong>重新登录策略</strong>
                  <HelpPopover label="查看 OAuth 重新登录策略说明">
                    Refresh Token 刷新失败后，使用这里选定的方式重新登录 OpenAI OAuth；协议模式与无头浏览器模式不会互相回退。
                  </HelpPopover>
                </span>
                <span className={`api-key-chip api-key-chip--${recoveryEnabled ? "success" : "muted"}`}>
                  {recoveryEnabled ? "已启用" : "已关闭"}
                </span>
              </div>
              <div className="settings-oauth-policy-controls">
                <div className="settings-oauth-mode-field">
                  <span>重新登录方式</span>
                  <div className="api-key-segmented api-key-segmented--two" role="group" aria-label="OAuth 重新登录方式">
                    <button
                      aria-pressed={oauthLoginMode === "protocol"}
                      className={oauthLoginMode === "protocol" ? "active" : ""}
                      onClick={() => setOauthLoginMode("protocol")}
                      type="button"
                    >协议</button>
                    <button
                      aria-pressed={oauthLoginMode === "browser"}
                      className={oauthLoginMode === "browser" ? "active" : ""}
                      onClick={() => setOauthLoginMode("browser")}
                      type="button"
                    >无头浏览器</button>
                  </div>
                </div>
                <label className="checkbox-line settings-toggle settings-oauth-phone-stop">
                  <input
                    checked={oauthStopOnPhoneVerification}
                    onChange={(event) => setOauthStopOnPhoneVerification(event.target.checked)}
                    type="checkbox"
                  />
                  <span className="settings-toggle-copy">
                    <span className="settings-label-with-help">
                      <strong>遇到手机验证码时停止</strong>
                      <HelpPopover label="查看手机验证码停止策略说明">
                        开启后，重新 OAuth 遇到手机验证会立即终止；任务日志和账号错误标签会记录该原因。
                      </HelpPopover>
                    </span>
                  </span>
                </label>
              </div>
            </div>

            <div className="settings-grid settings-section-grid settings-oauth-resource-grid">
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
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={autoRecoverState}
                  onChange={(event) => setAutoRecoverState(event.target.checked)}
                  type="checkbox"
                />
                <span>凭证刷新成功后恢复 sub2api 调度状态</span>
              </label>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--automation" id="settings-api-key-sync">
            <legend>API Key 与上游同步</legend>
            <AutomationSettingsTable>
              <AutomationSettingRow
                checked={apiKeyAccountSyncEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="API Key 账号同步间隔"
                    maxSeconds={86_400}
                    minSeconds={30}
                    onChange={setApiKeyAccountSyncInterval}
                    value={apiKeyAccountSyncInterval}
                  />
                )}
                label="同步 sub2api API Key 账号"
                onChange={setApiKeyAccountSyncEnabled}
                threads={<AutomationSettingInherited>无需设置</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={accountModelWhitelistSyncEnabled}
                description="开启后按独立间隔刷新已导入账号的可用模型白名单；关闭时仍会在首次导入或本地缺失白名单时补齐。"
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="账号可用模型白名单刷新间隔"
                    maxSeconds={86_400}
                    minSeconds={60}
                    onChange={setAccountModelWhitelistSyncInterval}
                    value={accountModelWhitelistSyncInterval}
                  />
                )}
                label="自动刷新账号可用模型白名单"
                onChange={setAccountModelWhitelistSyncEnabled}
                threads={<AutomationSettingInherited>无需设置</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={upstreamSyncEnabled}
                interval={(
                  <AutomationSettingDuration
                    ariaLabel="API Key 上游同步间隔"
                    maxSeconds={86_400}
                    minSeconds={60}
                    onChange={setUpstreamSyncInterval}
                    value={upstreamSyncInterval}
                  />
                )}
                label="同步 API Key 账号上游"
                onChange={setUpstreamSyncEnabled}
                threads={(
                  <AutomationSettingNumber
                    ariaLabel="API Key 上游同步线程数"
                    max={50}
                    min={0}
                    onChange={setUpstreamSyncMaxConcurrency}
                    value={upstreamSyncMaxConcurrency}
                  />
                )}
              />
              <AutomationSettingRow
                checked={upstreamRateSyncEnabled}
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="修改 API Key 账号计费倍率"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualUpstreamRateEnabled}
                    disabled={!upstreamRateSyncEnabled}
                    label="手动同步时修改 API Key 账号计费倍率"
                    onChange={setManualUpstreamRateEnabled}
                  />
                )}
                onChange={setUpstreamRateSyncEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={channelMonitorAutoProbeEnabled}
                description="关闭后保留上次已保存的渠道监控结果；打开渠道状态弹窗不会触发请求。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="自动探测上游渠道监控"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualChannelMonitorsEnabled}
                    disabled={!channelMonitorAutoProbeEnabled}
                    label="手动同步时探测上游渠道监控"
                    onChange={setManualChannelMonitorsEnabled}
                  />
                )}
                onChange={setChannelMonitorAutoProbeEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={upstreamPrioritySyncEnabled}
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="修改 API Key 账号优先级"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualUpstreamPriorityEnabled}
                    disabled={!upstreamPrioritySyncEnabled}
                    label="手动同步时修改 API Key 账号优先级"
                    onChange={setManualUpstreamPriorityEnabled}
                  />
                )}
                onChange={setUpstreamPrioritySyncEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
            </AutomationSettingsTable>
          </fieldset>

          <fieldset className="settings-section settings-section--automation" id="settings-api-key-policies">
            <legend>可用性与暂停</legend>
            <AutomationSettingsTable>
              <AutomationSettingRow
                checked={apiKeyChannelMonitorPauseEnabled}
                description="自动检测与策略判定跟随上游同步任务执行；手动检测不受自动任务暂停或其他暂停原因限制。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="API Key 账号可用性监测与自动暂停"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualAccountAvailabilityEnabled}
                    disabled={!apiKeyChannelMonitorPauseEnabled}
                    label="手动同步时检测 API Key 账号可用性"
                    onChange={setManualAccountAvailabilityEnabled}
                  />
                )}
                onChange={setApiKeyChannelMonitorPauseEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={apiKeyAutoDisableEnabled}
                description="按每次上游同步的当前 Key / 分组状态即时判断；上游恢复后，仅自动恢复由本插件暂停的账号。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="上游 Key / 分组不可用时自动停用 API Key 账号"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualUpstreamHealthEnabled}
                    disabled={!apiKeyAutoDisableEnabled}
                    label="手动同步时执行上游 Key 和分组状态暂停判定"
                    onChange={setManualUpstreamHealthEnabled}
                  />
                )}
                onChange={setApiKeyAutoDisableEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
              <AutomationSettingRow
                checked={apiKeyNegativeBalancePauseEnabled}
                description="仅暂停探测时已启用的账号；余额达到或高于阈值且其他暂停原因均解除后自动恢复。"
                interval={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
                label="上游余额低于阈值时自动暂停 API Key 账号"
                manual={(
                  <AutomationSettingManualCheckbox
                    checked={manualBalanceGuardEnabled}
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    label="手动同步时执行上游余额暂停判定"
                    onChange={setManualBalanceGuardEnabled}
                  />
                )}
                onChange={setApiKeyNegativeBalancePauseEnabled}
                threads={<AutomationSettingInherited>跟随上游同步</AutomationSettingInherited>}
              />
            </AutomationSettingsTable>
            <div className="settings-automation-policies">
              <div
                className="settings-auto-pause-policy"
                role="group"
                aria-label="渠道监控不可用自动暂停策略"
              >
                <div className="settings-policy-heading">
                  <span className="settings-label-with-help">
                    <strong>API Key 账号可用性监测策略</strong>
                    <HelpPopover label="查看 API Key 可用性监测策略说明">
                      仅处理已绑定具体监控面板或已启用独立模型测试的账号。监控面板代表上游的单个分组或模型路由，不代表上游站点整体状态；面板状态为可用或降级时都直接判定账号可用，不进行回退模型测试，降级仅以黄色状态提示。面板缺失、读取失败、状态未知或不可用时才按账号白名单模型回退测试。自动检测跟随上游同步；存在余额、上游 Key、分组或倍率等其他暂停原因时保留上次结果并暂停自动连接测试，手动检测仍会先刷新监控面板及其状态详情并执行。启用账号的暂停判定使用“暂停判定测试次数”，因可用性监测而暂停的账号使用独立的“恢复判定测试次数”；{apiKeyAvailabilityAllTestsMustSucceed ? "全部连接测试均成功才判定可用，任意一次失败都会暂停或保持暂停。" : "任意一次连接成功即判定可用，全部失败才暂停或保持暂停。"}没有可用回退模型时不会据此暂停账号。
                    </HelpPopover>
                  </span>
                  <span className={`api-key-chip api-key-chip--${apiKeyChannelMonitorPauseEnabled ? "success" : "muted"}`}>
                    {apiKeyChannelMonitorPauseEnabled ? "已启用" : "已关闭"}
                  </span>
                </div>
                <div className="settings-auto-pause-thresholds">
                  <div className="settings-model-chain-field settings-model-chain-summary">
                    <span className="settings-label-with-help">
                      回退测试模型链
                      <HelpPopover label="查看回退测试模型链说明">
                        按从上到下的顺序选择账号白名单中第一个存在的模型。账号单独配置的测试模型优先于全局模型链，最多可配置 10 个模型。
                      </HelpPopover>
                    </span>
                    <span className="settings-model-chain-summary-value">
                      {channelMonitorFallbackTestModels.length
                        ? `已配置 ${channelMonitorFallbackTestModels.length} 个模型`
                        : "尚未配置"}
                    </span>
                    <button
                      className="secondary-button settings-model-chain-configure"
                      onClick={() => setFallbackModelDialogOpen(true)}
                      ref={fallbackModelDialogTriggerRef}
                      type="button"
                    >
                      <Settings2 size={15} />
                      配置
                    </button>
                  </div>
                  <label className="checkbox-line settings-toggle settings-fallback-without-monitor">
                    <input
                      checked={channelMonitorFallbackWithoutMonitorEnabled}
                      disabled={!apiKeyChannelMonitorPauseEnabled}
                      onChange={(event) => setChannelMonitorFallbackWithoutMonitorEnabled(event.target.checked)}
                      type="checkbox"
                    />
                    <span className="settings-toggle-copy">
                      <span className="settings-label-with-help">
                        <strong>未绑定监控面板时使用回退模型链</strong>
                        <HelpPopover label="查看未绑定面板回退说明">
                          关闭时，选择“绑定监控面板”但未选择具体面板的账号会显示为未配置，不发起连接测试，也不会因此自动暂停。已绑定面板被删除或报告不可用时仍会回退测试。
                        </HelpPopover>
                      </span>
                    </span>
                  </label>
                  <label className="checkbox-line settings-toggle settings-availability-success-policy">
                    <input
                      checked={apiKeyAvailabilityAllTestsMustSucceed}
                      onChange={(event) => setApiKeyAvailabilityAllTestsMustSucceed(event.target.checked)}
                      type="checkbox"
                    />
                    <span className="settings-toggle-copy">
                      <span className="settings-label-with-help">
                        <strong>全部连接测试成功才判定可用</strong>
                        <HelpPopover label="查看连续连接测试成功判定说明">
                          关闭时，连续测试中任意一次成功即可判定可用；开启时，会执行配置的全部测试次数，只有全部成功才判定可用，任意一次失败都会判定不可用。此策略同时用于暂停与恢复判定。
                        </HelpPopover>
                      </span>
                    </span>
                  </label>
                  <label>
                    <span className="settings-label-with-help">
                      暂停判定测试次数
                      <HelpPopover label="查看暂停判定测试次数说明">
                        对当前未被可用性监测暂停的账号发起 1 至 5 次连接测试，并按当前成功判定策略决定是否暂停账号。
                      </HelpPopover>
                    </span>
                    <AutomationSettingNumber
                      ariaLabel="账号暂停判定测试次数"
                      max={5}
                      min={1}
                      onChange={setChannelMonitorFallbackTestAttempts}
                      value={channelMonitorFallbackTestAttempts}
                    />
                  </label>
                  <label>
                    <span className="settings-label-with-help">
                      恢复判定测试次数
                      <HelpPopover label="查看恢复判定测试次数说明">
                        对因可用性监测而自动暂停的账号发起 1 至 5 次连接测试，并按当前成功判定策略决定是否恢复账号。
                      </HelpPopover>
                    </span>
                    <AutomationSettingNumber
                      ariaLabel="账号恢复判定测试次数"
                      max={5}
                      min={1}
                      onChange={setChannelMonitorRecoveryTestAttempts}
                      value={channelMonitorRecoveryTestAttempts}
                    />
                  </label>
                  <label className="settings-test-attempt-interval">
                    <span className="settings-label-with-help">
                      多次测试间隔
                      <HelpPopover label="查看多次测试间隔说明">
                        当暂停或恢复判定需要测试多次时，每次测试后等待此时间再开始下一次；设为 0 秒时连续测试。
                      </HelpPopover>
                    </span>
                    <AutomationSettingDuration
                      ariaLabel="账号多次连接测试间隔"
                      maxSeconds={300}
                      minSeconds={0}
                      onChange={setChannelMonitorTestAttemptInterval}
                      value={channelMonitorTestAttemptInterval}
                    />
                  </label>
                </div>
              </div>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={priorityAssignDisabledAccounts}
                  onChange={(event) => setPriorityAssignDisabledAccounts(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>停用的 API Key 账号也参与优先级分配</strong>
                    <HelpPopover label="查看停用账号优先级说明">
                      仅控制优先级计算，与上游状态停用、余额暂停及自动恢复无关。
                    </HelpPopover>
                  </span>
                </span>
              </label>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={priorityShareSameCompositeMultiplier}
                  onChange={(event) => setPriorityShareSameCompositeMultiplier(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>同综合倍率账号使用相同优先级</strong>
                    <HelpPopover label="查看同倍率账号优先级说明">
                      开启后，同一优先级区间内综合倍率相同的账号共用一个调度优先级；不同倍率档位仍按区间步长递增，账号卡片不再提供同倍率排位按钮。
                    </HelpPopover>
                  </span>
                </span>
              </label>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={manualRatePauseEnabled}
                  onChange={(event) => setManualRatePauseEnabled(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>手动同步时执行账号倍率暂停判定</strong>
                    <HelpPopover label="查看手动倍率暂停判定说明">
                      关闭后，手动同步仍会刷新分组倍率和综合倍率，但不会新增或解除账号的倍率暂停原因；自动上游同步不受影响。
                    </HelpPopover>
                  </span>
                </span>
              </label>
              <div className="settings-balance-basis" aria-disabled={!apiKeyNegativeBalancePauseEnabled}>
                <span>余额暂停判断口径与阈值</span>
                <div className="api-key-segmented" role="group" aria-label="上游负余额判断口径">
                  <button
                    aria-pressed={negativeBalanceBasis === "wallet"}
                    className={negativeBalanceBasis === "wallet" ? "active" : ""}
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    onClick={() => setNegativeBalanceBasis("wallet")}
                    type="button"
                  >上游钱包余额</button>
                  <button
                    aria-pressed={negativeBalanceBasis === "recharge_adjusted"}
                    className={negativeBalanceBasis === "recharge_adjusted" ? "active" : ""}
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    onClick={() => setNegativeBalanceBasis("recharge_adjusted")}
                    type="button"
                  >充值倍率后余额</button>
                </div>
                <label className="settings-balance-threshold">
                  <span>暂停阈值</span>
                  <input
                    aria-label="上游余额暂停阈值"
                    disabled={!apiKeyNegativeBalancePauseEnabled}
                    max={1_000_000_000}
                    min={-1_000_000_000}
                    onChange={(event) => setBalancePauseThreshold(event.target.value)}
                    step="any"
                    type="number"
                    value={balancePauseThreshold}
                  />
                  <b>{negativeBalanceBasis === "recharge_adjusted" ? "¥" : "$"}</b>
                </label>
              </div>
              <label className="checkbox-line settings-toggle settings-automation-policy">
                <input
                  checked={showStaleNegativeBalanceAlert}
                  onChange={(event) => setShowStaleNegativeBalanceAlert(event.target.checked)}
                  type="checkbox"
                />
                <span className="settings-toggle-copy">
                  <span className="settings-label-with-help">
                    <strong>首页显示上次已知低余额提醒</strong>
                    <HelpPopover label="查看历史余额提醒说明">
                      最新探测失败时，继续按当前口径和阈值展示上次成功余额；不会使用过期余额暂停账号。
                    </HelpPopover>
                  </span>
                </span>
              </label>
            </div>
            <div className="settings-grid settings-section-grid settings-policy-resource-grid">
              <label>
                账号测活最大线程数
                <input
                  max={50}
                  min={0}
                  onChange={(event) => setAccountLivenessMaxConcurrency(event.target.value)}
                  title="账号页手动测活使用此线程数"
                  type="number"
                  value={accountLivenessMaxConcurrency}
                />
              </label>
            </div>
          </fieldset>

          <fieldset className="settings-section" id="settings-usage">
            <legend>用量与订阅</legend>
            <div className="settings-grid settings-section-grid">
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
                min={0}
                onChange={(event) => setSubscriptionRefreshMaxConcurrency(event.target.value)}
                type="number"
                value={subscriptionRefreshMaxConcurrency}
              />
            </label>
            </div>
            <section className="quota-range-settings settings-subsection" id="settings-quota-ranges">
              <div className="settings-subsection-heading">
                <strong>订阅默认额度区间</strong>
                <span>{Object.keys(usageLimitDefaultRanges).length} 种订阅</span>
              </div>
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
                            disabled={subscriptionType !== "team" && windowKey === "monthly"}
                            min={0}
                            onChange={(event) => updateUsageLimitRange(subscriptionType, windowKey, "lower", event.target.value)}
                            step="0.01"
                            type="number"
                            title={subscriptionType !== "team" && windowKey === "monthly" ? "自动使用 7d 下限的 4 倍" : undefined}
                            value={planRanges[windowKey].lower}
                          />
                        </label>
                        <label>
                          上限
                          <input
                            aria-label={`${subscriptionTypeLabel(subscriptionType)} ${usageLimitWindowLabel(windowKey)} 上限`}
                            disabled={subscriptionType !== "team" && windowKey === "monthly"}
                            min={0}
                            onChange={(event) => updateUsageLimitRange(subscriptionType, windowKey, "upper", event.target.value)}
                            step="0.01"
                            type="number"
                            title={subscriptionType !== "team" && windowKey === "monthly" ? "自动使用 7d 上限的 4 倍" : undefined}
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
            </section>
          </fieldset>

          <fieldset className="settings-section settings-section--api-key" id="settings-data-management">
            <legend>数据管理</legend>
            <div className="settings-grid settings-section-grid">
              <label>
                上游变化保留天数
                <input
                  max={3650}
                  min={1}
                  onChange={(event) => setUpstreamRateLogRetentionDays(event.target.value)}
                  type="number"
                  value={upstreamRateLogRetentionDays}
                />
              </label>
              <label>
                上游数据存储天数
                <input
                  max={3650}
                  min={1}
                  onChange={(event) => setUpstreamUsageDataRetentionDays(event.target.value)}
                  type="number"
                  value={upstreamUsageDataRetentionDays}
                />
                <small>每日明细到期后会清理；各上游的累计成本和收入始终保留。</small>
              </label>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--notifications" id="settings-notifications">
            <legend>
              <span>Discord Bot 通知</span>
              <button
                aria-label="查看 Discord Bot 配置指南"
                className="icon-button discord-bot-setup-guide-trigger"
                onClick={() => setDiscordSetupGuideOpen(true)}
                ref={discordSetupGuideTriggerRef}
                title="查看 Discord Bot 的创建、安装和通知频道配置步骤"
                type="button"
              >
                <CircleHelp size={16} />
              </button>
            </legend>
            <label className="checkbox-line settings-toggle settings-global-toggle">
              <input
                checked={discordNotificationsEnabled}
                onChange={(event) => setDiscordNotificationsEnabled(event.target.checked)}
                type="checkbox"
              />
              <span>启用 Discord Bot 通知</span>
            </label>
            <div className="settings-grid settings-section-grid notification-channel-grid">
              <label>
                Bot Token
                <input
                  autoComplete="new-password"
                  onChange={(event) => {
                    const token = event.target.value;
                    setDiscordBotToken(token);
                    if (token) setClearDiscordBotToken(false);
                  }}
                  placeholder={settings.discord_bot_token_set ? `已保存 ${settings.discord_bot_token_hint || ""}` : "输入 Discord Bot Token"}
                  type="password"
                  value={discordBotToken}
                />
              </label>
              <label>
                Channel ID
                <input
                  maxLength={64}
                  onChange={(event) => setDiscordChannelId(event.target.value)}
                  placeholder="Discord 频道 ID"
                  value={discordChannelId}
                />
              </label>
            </div>
            <label className="checkbox-line settings-toggle">
              <input
                checked={clearDiscordBotToken}
                disabled={!settings.discord_bot_token_set && !discordBotToken}
                onChange={(event) => {
                  setClearDiscordBotToken(event.target.checked);
                  if (event.target.checked) {
                    setDiscordBotToken("");
                    setDiscordNotificationsEnabled(false);
                  }
                }}
                type="checkbox"
              />
              <span>清空已保存 Bot Token</span>
            </label>
            <div className="settings-notification-events">
              <label className="checkbox-line settings-toggle">
                <input checked={notifyAccountScheduling} onChange={(event) => setNotifyAccountScheduling(event.target.checked)} type="checkbox" />
                <span className="settings-label-with-help">
                  <span>账号调度</span>
                  <HelpPopover label="查看账号调度通知范围">
                    包含 OAuth 与 API Key 账号的停用、启用和自动恢复；自动恢复会保留此前的暂停原因。
                  </HelpPopover>
                </span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyApiKeyRateChanged} onChange={(event) => setNotifyApiKeyRateChanged(event.target.checked)} type="checkbox" />
                <span>倍率变化</span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyUpstreamGroupChanged} onChange={(event) => setNotifyUpstreamGroupChanged(event.target.checked)} type="checkbox" />
                <span>上游分组变化</span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyUpstreamBalanceLow} onChange={(event) => setNotifyUpstreamBalanceLow(event.target.checked)} type="checkbox" />
                <span>上游余额不足</span>
              </label>
              <label className="checkbox-line settings-toggle">
                <input checked={notifyUpstreamTokenInvalid} onChange={(event) => setNotifyUpstreamTokenInvalid(event.target.checked)} type="checkbox" />
                <span>上游令牌失效</span>
              </label>
            </div>
            <div className="settings-notification-actions">
              <button
                className="secondary-button"
                disabled={busy || !discordNotificationsEnabled || !settings.discord_bot_token_set || !cleanDiscordChannelId}
                onClick={onTestNotification}
                title="使用已保存的 Discord 配置发送测试消息"
                type="button"
              >
                <Send size={16} />
                <span>发送测试通知</span>
              </button>
            </div>
          </fieldset>

          <fieldset className="settings-section settings-section--display-security" id="settings-display-security">
            <legend>界面偏好</legend>
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

            <div className="settings-grid settings-section-grid settings-change-log-pagination-grid">
              <label>
                变化记录默认每页条数
                <select
                  onChange={(event) => setChangeLogPageSize(Number(event.target.value))}
                  value={changeLogPageSize}
                >
                  {visibleChangeLogPageSizeOptions.map((size) => (
                    <option key={size} value={size}>{size} 条</option>
                  ))}
                </select>
                <small>统一应用到三个变化记录页面，并随服务端配置保存。</small>
              </label>
              <label>
                每页条数可选项
                <input
                  aria-invalid={changeLogPageSizeOptions === null}
                  inputMode="numeric"
                  onChange={(event) => {
                    const value = event.target.value;
                    setChangeLogPageSizeOptionsInput(value);
                    const options = parseChangeLogPageSizeOptions(value);
                    if (options?.length) {
                      setChangeLogPageSize((current) => options.includes(current) ? current : options[0]);
                    }
                  }}
                  placeholder="20, 50, 100, 200"
                  value={changeLogPageSizeOptionsInput}
                />
                <small>使用逗号或空格分隔，最多 20 项；每项为 1 至 200 的整数。</small>
              </label>
            </div>
          </fieldset>
        </form>
        </section>
      </div>

      {fallbackModelDialogOpen ? (
        <FallbackModelChainDialog
          availableModels={settings.available_test_models || []}
          enabled={apiKeyChannelMonitorPauseEnabled}
          models={channelMonitorFallbackTestModels}
          onChange={setChannelMonitorFallbackTestModels}
          onClose={closeFallbackModelDialog}
        />
      ) : null}
      {discordSetupGuideOpen ? <DiscordBotSetupGuideDialog onClose={closeDiscordSetupGuide} /> : null}
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

function StackedAccountIdentity({ accountName, email }: { accountName: string | null | undefined; email: string }) {
  return (
    <div className="account-identity-cell">
      <CopyTextButton className="account-identity-copy-button account-name-copy-button" title="复制账号名称" value={accountName?.trim() || email} />
      <CopyTextButton className="account-identity-copy-button account-email-copy-button mono" title="复制账号邮箱" value={email} />
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
  disabled,
  icon: Icon,
  label,
  loading,
  onClick,
  time,
}: {
  disabled: boolean;
  icon: LucideIcon;
  label: string;
  loading: boolean;
  onClick: () => void;
  time: string | null;
}) {
  const timeZone = useDisplayTimeZone();
  const shortTime = time ? formatClockTime(time, timeZone) : "--:--";
  const fullTime = time ? formatFullDate(time, timeZone) : "暂无刷新记录";
  return (
    <button className="secondary-button toolbar-time-button" disabled={disabled} onClick={onClick} title={`上次刷新时间 ${fullTime}`} type="button">
      <Icon className={loading ? "spin" : ""} size={17} />
      <span>{label}</span>
      <span className="toolbar-time">{shortTime}</span>
    </button>
  );
}

function SignalLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="signal-row">
      <span>{label}</span>
      <strong>{isPhoneUrlSource(value) ? <MiddleEllipsisText text={value} /> : value}</strong>
    </div>
  );
}

function AutomationSettingsTable({ children }: { children: ReactNode }) {
  return (
    <div className="automation-settings-table">
      <div className="automation-settings-head">
        <span>功能开关</span>
        <span className="settings-label-with-help">
          线程数
          <HelpPopover label="查看线程数说明">
            填 0 表示本批任务不限并发，取得账号或渠道清单后会同时发起。
          </HelpPopover>
        </span>
        <span>自动执行间隔</span>
        <span className="settings-label-with-help">
          手动同步
          <HelpPopover label="查看手动同步说明">
            勾选后，点击“同步 API Key 账号”或上游卡片的同步按钮时会执行该项；取消勾选可只刷新基础上游数据，减少等待和连接测试消耗。
          </HelpPopover>
        </span>
      </div>
      {children}
    </div>
  );
}

function AutomationSettingDuration({
  ariaLabel,
  disabled = false,
  maxSeconds,
  minSeconds,
  onChange,
  value,
}: {
  ariaLabel: string;
  disabled?: boolean;
  maxSeconds: number;
  minSeconds: number;
  onChange: (value: string) => void;
  value: string;
}) {
  const [unit, setUnit] = useState<AutomationDurationUnit>(() => preferredAutomationDurationUnit(value));
  const displayValue = automationDurationDisplayValue(value, unit);

  return (
    <div className="automation-setting-duration">
      <input
        aria-label={ariaLabel}
        disabled={disabled}
        max={Number(automationDurationDisplayValue(String(maxSeconds), unit))}
        min={Number(automationDurationDisplayValue(String(minSeconds), unit))}
        onChange={(event) => onChange(automationDurationSecondsValue(event.target.value, unit))}
        step="any"
        type="number"
        value={displayValue}
      />
      <select
        aria-label={`${ariaLabel}单位`}
        disabled={disabled}
        onChange={(event) => setUnit(event.target.value as AutomationDurationUnit)}
        value={unit}
      >
        {automationDurationUnits.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </div>
  );
}

function formatMoney(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }
  return `$${value.toFixed(2)}`;
}

function formatBalanceGuardValue(
  channel: UpstreamChannel,
  balanceBasis: "wallet" | "recharge_adjusted",
) {
  if (String(channel.balance_guard_state || "").toLowerCase() === "insufficient") {
    const value = Number(channel.balance_guard_value);
    if (Number.isFinite(value)) {
      const formatted = value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      return channel.balance_guard_basis === "recharge_adjusted" ? `¥${formatted}` : `$${formatted}`;
    }
  }
  const value = historicalBalanceValue(channel, balanceBasis);
  if (value === null) return "待确认";
  const formatted = value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (balanceBasis === "recharge_adjusted") return `¥${formatted}`;
  const unit = String(channel.balance_unit || "USD").trim().toUpperCase();
  return unit === "USD" ? `$${formatted}` : `${unit} ${formatted}`;
}

function channelHasLowBalance(
  channel: UpstreamChannel,
  includeStale: boolean,
  balanceBasis: "wallet" | "recharge_adjusted",
  threshold: number,
) {
  const activeGuard = ["insufficient", "negative", "paused"].includes(
    String(channel.balance_guard_state || "").toLowerCase(),
  );
  return activeGuard || (includeStale && hasKnownLowBalance(channel, balanceBasis, threshold));
}

function isStaleLowBalance(
  channel: UpstreamChannel,
  balanceBasis: "wallet" | "recharge_adjusted",
  threshold: number,
) {
  return hasKnownLowBalance(channel, balanceBasis, threshold)
    && !["insufficient", "negative", "paused"].includes(
      String(channel.balance_guard_state || "").toLowerCase(),
    );
}

function hasKnownLowBalance(
  channel: UpstreamChannel,
  balanceBasis: "wallet" | "recharge_adjusted",
  threshold: number,
) {
  const balance = historicalBalanceValue(channel, balanceBasis);
  return balance !== null && balance < threshold;
}

function historicalBalanceValue(
  channel: UpstreamChannel,
  balanceBasis: "wallet" | "recharge_adjusted",
) {
  const balance = Number(channel.balance_remaining);
  if (
    channel.balance_source !== "upstream_wallet"
    || !channel.balance_checked_at
    || !Number.isFinite(balance)
  ) return null;
  if (balanceBasis === "wallet") return balance;
  const multiplier = Number(channel.effective_recharge_multiplier);
  return Number.isFinite(multiplier) && multiplier > 0 ? balance * multiplier : null;
}

function fallbackSiteLogo(event: { currentTarget: HTMLImageElement }) {
  const image = event.currentTarget;
  if (image.getAttribute("src") === "/logo.png") return;
  image.src = "/logo.png";
}

function ensureFaviconLink() {
  const existing = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  if (existing) return existing;
  const favicon = document.createElement("link");
  favicon.rel = "icon";
  document.head.appendChild(favicon);
  return favicon;
}

function versionedSiteLogoUrl(url: string, updatedAt: string | null | undefined) {
  const value = url.trim() || "/logo.png";
  if (!updatedAt || value.startsWith("data:") || value.startsWith("blob:")) return value;
  try {
    const next = new URL(value, window.location.origin);
    next.searchParams.set("v", updatedAt);
    return value.startsWith("http://") || value.startsWith("https://")
      ? next.toString()
      : `${next.pathname}${next.search}${next.hash}`;
  } catch {
    return value;
  }
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
    "api-keys": "API Key 账号管理",
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

function deriveMonthlyUsageLimitRange(value: UsageLimitPlanRanges): UsageLimitPlanRanges {
  const ranges = cloneUsageLimitPlanRanges(value);
  ranges.monthly = {
    lower: Math.min(ranges.seven_day.lower * 4, 1_000_000_000),
    upper: Math.min(ranges.seven_day.upper * 4, 1_000_000_000),
  };
  return ranges;
}

function mergeUsageLimitDefaultRanges(value: UsageLimitDefaultRanges, detectedTypes: string[]): UsageLimitDefaultRanges {
  const merged = Object.fromEntries(
    Object.entries({ ...defaultUsageLimitRanges, ...(value || {}) }).map(([subscriptionType, ranges]) => [
      normalizeSubscriptionType(subscriptionType),
      normalizeSubscriptionType(subscriptionType) === "team"
        ? cloneUsageLimitPlanRanges(ranges)
        : deriveMonthlyUsageLimitRange(ranges),
    ]),
  ) as UsageLimitDefaultRanges;
  const fallback = merged.unknown || cloneUsageLimitPlanRanges(defaultUsageLimitPlanRanges);
  for (const rawType of detectedTypes) {
    const subscriptionType = normalizeSubscriptionType(rawType);
    if (subscriptionType !== "unknown" && !merged[subscriptionType]) {
      merged[subscriptionType] = deriveMonthlyUsageLimitRange(fallback);
    }
  }
  merged.unknown = deriveMonthlyUsageLimitRange(fallback);
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

function refreshJobStatusLabel(status: string) {
  const normalized = status.trim().toLowerCase();
  if (normalized === "completed" || normalized === "success") return "完成";
  if (normalized === "failed" || normalized === "error") return "失败";
  if (normalized === "running") return "运行中";
  if (normalized === "queued" || normalized === "pending") return "等待中";
  return status || "未知";
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
  return subscriptionIsInvalid(account) ? "订阅无效" : plan === "active" ? "正常" : plan;
}

function subscriptionIsInvalid(
  account: Pick<Account, "has_active_subscription" | "subscription_plan" | "subscription_type">,
) {
  const subscriptionType = normalizeSubscriptionType(account.subscription_type || account.subscription_plan);
  return account.has_active_subscription === false && !seatBasedSubscriptionTypes.has(subscriptionType);
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
  account: Pick<Account, "remote_error" | "last_error" | "sub2api_error_code" | "sub2api_error_message">,
): { label: string; tone: string; title?: string } | null {
  const remoteCode = account.sub2api_error_code;
  const detail = String(account.sub2api_error_message || account.last_error || "").trim();
  const text = `${account.sub2api_error_message || ""} ${account.last_error || ""}`.toLowerCase();
  if (isOAuthPhoneVerificationStopped(account.sub2api_error_message, account.last_error)) {
    return {
      label: "手机验证已终止",
      tone: "warn",
      title: "尝试重新 OAuth，但遇到手机验证码而终止",
    };
  }
  if (remoteCode) {
    return { label: `sub2api ${remoteCode}`, tone: "error" };
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

function credentialBindingOrigin(value: string) {
  try {
    return new URL(toSub2ApiInstanceUrl(value)).origin.toLowerCase();
  } catch {
    return value.trim().toLowerCase();
  }
}

function isAbortError(reason: unknown) {
  return reason instanceof DOMException && reason.name === "AbortError";
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

export default App;
