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
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { FormEvent, lazy, Suspense, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { api, upstreamLegacyBindingCounts } from "../../shared/api";
import { useAuth } from "../auth/AuthGate";
import { AppShell } from "../layout/AppShell";
import { DashboardProvider, type DashboardPages } from "./DashboardContext";
import { queryKeys } from "../queryKeys";
import { useVisiblePageRefresh } from "../useVisiblePageRefresh";
import { useOperations } from "../OperationContext";
import { NowContext, TimeZoneContext } from "../../shared/hooks/displayContext";
import {
  automationDurationDisplayValue,
  automationDurationSecondsValue,
  automationDurationUnits,
  preferredAutomationDurationUnit,
  type AutomationDurationUnit,
} from "../../automationDuration";
import { HelpPopover } from "../../HelpPopover";
import { MiddleEllipsisText } from "../../MiddleEllipsisText";
import { oauthUsageBackgroundRefreshIntervals } from "../../oauthUsageRefresh";
import {
  persistOverviewBalanceAlertDismissed,
  readOverviewBalanceAlertDismissed,
} from "../../overviewBalanceAlertPreference";
import {
  accountCanBeLivenessTested,
  livenessAccountIds,
  MAX_LIVENESS_ACCOUNTS,
} from "../../accountLiveness";
import { accountFilterFacetCandidates } from "../../accountFilterFacets";
import {
  accountEstimateHasEffectiveError,
  accountRateLimitShouldBeVisible,
} from "../../accountRateLimitPresentation";
import { isOAuthPhoneVerificationStopped } from "../../accountErrorPresentation";
import { sortAccountsForTable } from "../../accountTableSort";
import {
  firstUnusedFallbackModel,
  MAX_FALLBACK_TEST_MODELS,
  moveFallbackModel,
  normalizeFallbackModelChain,
} from "../../fallbackModelChain";
import { LatestRequestCoordinator } from "../../latestRequest";
import {
  apiAccountLegacyBindingConfirmationMessage,
  apiAccountSyncMessage,
  upstreamRateWritesAllowed,
} from "../../upstreamSyncPresentation";

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
  filterUsageLimitSamples,
  sortUsageLimitSamples,
  usageSampleDatePresets,
  usageSampleDateRangeForPreset,
  type UsageSampleDatePreset,
  type UsageSampleSortDirection,
  type UsageSampleSortField,
} from "../../usageSampleSort";
import {
  clearChangeLogCache,
  getChangeLogSessionStorage,
} from "../../changeLogCache";
import {
  normalizeChangeLogPageSizeOptions,
  parseChangeLogPageSizeOptions,
} from "../../changeLogPageSize";
import {
  clearUpstreamOverviewCache,
  getUpstreamOverviewSessionStorage,
  readUpstreamOverviewCache,
  upstreamOverviewCacheScope,
  writeUpstreamOverviewCache,
} from "../../upstreamOverviewCache";
import {
  pathForRoute,
  routeFromPath,
  type ApiKeySubview,
  type AppRoute,
  type View,
} from "../../viewRouting";
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

const loadApiKeyAccountsView = () => import("../../features/api-accounts/ApiKeyWorkspace");
const loadAccountEditorDialog = () => import("../../AccountEditorDialog");
const AccountEditorDialog = lazy(async () => ({
  default: (await loadAccountEditorDialog()).AccountEditorDialog,
}));

type Theme = "light" | "dark";
type AccountCounts = { actual: number; deduped: number; duplicates: number };
type AccountStatusFilter = "all" | "normal" | "normal-no-rate-limit" | "five-hour-rate-limited" | "seven-day-rate-limited" | "monthly-rate-limited" | "error" | "deactive";
type AccountSortField = "account" | "imported_at";
type SortDirection = "asc" | "desc";
type AccountJumpTarget = { email: string | null; managementAccountId: string | null; requestedAt: number };

const defaultTimeZone = "Asia/Shanghai";
const defaultSiteName = "账号管理助手";
const defaultUsageLimitSampleThresholdPercent = 99;

function clearFrontendSessionCaches() {
  clearUpstreamOverviewCache(getUpstreamOverviewSessionStorage());
  clearChangeLogCache(getChangeLogSessionStorage());
}
const usageLimitWindowKeys = ["five_hour", "seven_day", "monthly"] as const;
const coreSubscriptionTypes = new Set(["plus", "team", "pro", "free", "k12", "unknown"]);
const nonExpiringSubscriptionTypes = new Set(["free", "team", "k12", "enterprise", "enterprise-edu", "edu"]);
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
const refreshClockIntervalMs = 30_000;
const activeViewRefreshIntervalMs = 30_000;
const usageDetailPageSizeOptions = [25, 50, 100] as const;
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
  management_site_base_url: "http://localhost:8080/api/v1",
  management_site_port: 8080,
  management_site_base_url_source: "env",
  management_site_x_api_key_set: false,
  management_site_x_api_key_hint: null,
  management_site_auto_recover_state: true,
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
  manual_upstream_monitor_sync_enabled: true,
  manual_upstream_sync_account_availability_enabled: false,
  manual_upstream_sync_balance_guard_enabled: true,
  manual_upstream_sync_rate_pause_enabled: true,
  api_key_auto_disable_on_upstream_unavailable: false,
  api_key_auto_pause_on_negative_balance_enabled: false,
  api_account_auto_pause_on_upstream_monitor_unavailable_enabled: false,
  api_key_availability_all_tests_must_succeed: false,
  upstream_monitor_auto_probe_enabled: true,
  account_model_whitelist_sync_enabled: false,
  account_model_whitelist_sync_interval_seconds: 3600,
  account_model_whitelist_sync_each_time: false,
  upstream_monitor_fallback_without_monitor_enabled: false,
  upstream_monitor_fallback_test_models: [],
  upstream_monitor_fallback_test_model: "",
  upstream_monitor_fallback_test_attempts: 1,
  upstream_monitor_recovery_test_attempts: 1,
  upstream_monitor_test_attempt_interval_seconds: 0,
  available_test_models: [],
  upstream_negative_balance_basis: "wallet",
  upstream_balance_pause_threshold: 0,
  show_stale_negative_balance_alert: true,
  priority_assign_disabled_api_key_accounts: false,
  priority_share_same_upstream_actual_multiplier: false,
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
import {
  ThemeToggle,
  ToolbarTimeButton,
  accountDisplayCounts,
  appSettingsEqual,
  credentialBindingOrigin,
  downloadTextFile,
  ensureFaviconLink,
  formatDate,
  getInitialTheme,
  latestEventByKinds,
  selectedAccountDeleteItem,
  useRefreshClock,
  versionedSiteLogoUrl,
  cloneUsageLimitPlanRanges,
} from "../../features/app-shell/AppShellSupport";


function DashboardController() {
  const { signOut } = useAuth();
  const operations = useOperations();
  const queryClient = useQueryClient();
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const location = useLocation();
  const navigate = useNavigate();
  const route = useMemo(() => routeFromPath(location.pathname), [location.pathname]);
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
  const [apiKeyAccountsCache, setApiKeyAccountsCache] = useState<UpstreamOverviewResponse | null>(null);
  const [apiKeyRefreshVersion, setApiKeyRefreshVersion] = useState(0);
  const apiKeyAccountsCacheBaseUrlRef = useRef<string | null>(null);
  const loadAllRequestSequenceRef = useRef(0);
  const settingsMutationGenerationRef = useRef(0);
  const settingsMutationPendingRef = useRef(false);
  const oauthSyncOperationRef = useRef(false);
  const apiKeySyncOperationRef = useRef(false);
  const oauthUsageRefreshGenerationRef = useRef(0);
  const oauthUsageRefreshTimersRef = useRef(new Set<number>());
  const usageEstimateRequestsRef = useRef(new LatestRequestCoordinator());
  const usageEstimateReadRequestRef = useRef<Promise<UsageEstimate> | null>(null);
  const usageEstimateRefreshRequestRef = useRef<Promise<UsageEstimate> | null>(null);
  const [usageEstimate, setUsageEstimate] = useState<UsageEstimate | null>(null);
  const [usageLimitSamples, setUsageLimitSamples] = useState<UsageLimitSamples | null>(null);
  const [usageLimitSamplesLoading, setUsageLimitSamplesLoading] = useState(false);
  const [usageLimitSamplesError, setUsageLimitSamplesError] = useState("");
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState("");
  const [notice, setNotice] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [pageRefreshing, setPageRefreshing] = useState(false);
  const [oauthSyncBusy, setOAuthSyncBusy] = useState(false);
  const [apiKeySyncBusy, setApiKeySyncBusy] = useState(false);
  const [settingsFormInvalid, setSettingsFormInvalid] = useState(false);
  const topbarRef = useRef<HTMLElement | null>(null);
  const sidebarRef = useRef<HTMLElement | null>(null);
  const workspaceRef = useRef<HTMLElement | null>(null);
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
  const syncBusy = busy || oauthSyncBusy || apiKeySyncBusy || operations.busy;
  const toggleTheme = useCallback(() => setTheme((current) => (current === "dark" ? "light" : "dark")), []);
  const navigateToView = useCallback((nextView: View) => {
    const nextRoute: AppRoute = {
      view: nextView,
      apiKeySubview: nextView === "api-keys" ? "upstreams" : "accounts",
    };
    const nextPath = pathForRoute(nextRoute);
    if (location.pathname !== nextPath) navigate(nextPath);
  }, [location.pathname, navigate]);
  const navigateToApiKeySubview = useCallback((apiKeySubview: ApiKeySubview) => {
    const nextRoute: AppRoute = { view: "api-keys", apiKeySubview };
    const nextPath = pathForRoute(nextRoute);
    if (location.pathname !== nextPath) navigate(nextPath);
  }, [location.pathname, navigate]);

  const beginApiKeyViewOperation = useCallback((operation: ApiKeyViewOperation = { kind: "blocking" }) => {
    return operations.start(operation.kind, operation.kind === "upstream-discovery" ? operation.upstreamId : undefined);
  }, [operations]);

  const cacheApiKeyAccounts = useCallback((response: UpstreamOverviewResponse, responseBaseUrl: string) => {
    const activeBaseUrl = apiKeyAccountsCacheBaseUrlRef.current;
    if (!activeBaseUrl || upstreamOverviewCacheScope(activeBaseUrl) !== upstreamOverviewCacheScope(responseBaseUrl)) return;
    const safeResponse = writeUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), responseBaseUrl, response);
    if (safeResponse) setApiKeyAccountsCache(response);
  }, []);

  const resetUsageEstimateRequests = useCallback(() => {
    usageEstimateRequestsRef.current.invalidate();
    usageEstimateReadRequestRef.current = null;
    usageEstimateRefreshRequestRef.current = null;
    setUsageLoading(false);
  }, []);

  const loadAll = useCallback(async ({ includePhones = true }: { includePhones?: boolean } = {}) => {
    if (settingsMutationPendingRef.current) return;
    const requestSequence = ++loadAllRequestSequenceRef.current;
    const settingsGeneration = settingsMutationGenerationRef.current;
    const requestIsCurrent = () => (
      requestSequence === loadAllRequestSequenceRef.current
      && settingsGeneration === settingsMutationGenerationRef.current
    );
    const commitResponse = async <T,>(request: Promise<T>, commit: (value: T) => void) => {
      const value = await request;
      if (requestIsCurrent()) commit(value);
      return value;
    };

    const settingsPromise = queryClient.fetchQuery({ queryKey: queryKeys.settings, queryFn: api.settings });
    const commitSettingsAndOverview = (async () => {
      const nextSettings = await settingsPromise;
      const nextUpstreamOverview = await queryClient.fetchQuery({
        queryKey: queryKeys.upstreams(upstreamOverviewCacheScope(nextSettings.management_site_base_url)),
        queryFn: () => api.upstreams(false),
      }).catch(() => null);
      if (!requestIsCurrent()) return;
      const previousBaseUrl = apiKeyAccountsCacheBaseUrlRef.current;
      if (!previousBaseUrl) {
        apiKeyAccountsCacheBaseUrlRef.current = nextSettings.management_site_base_url;
        const cachedOverview = nextUpstreamOverview
          ? writeUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), nextSettings.management_site_base_url, nextUpstreamOverview)
          : readUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), nextSettings.management_site_base_url);
        setApiKeyAccountsCache(nextUpstreamOverview || cachedOverview);
      } else if (upstreamOverviewCacheScope(previousBaseUrl) !== upstreamOverviewCacheScope(nextSettings.management_site_base_url)) {
        clearFrontendSessionCaches();
        apiKeyAccountsCacheBaseUrlRef.current = nextSettings.management_site_base_url;
        setUsageEstimate(null);
        setUsageLimitSamples(null);
        resetUsageEstimateRequests();
        const safeOverview = nextUpstreamOverview
          ? writeUpstreamOverviewCache(getUpstreamOverviewSessionStorage(), nextSettings.management_site_base_url, nextUpstreamOverview)
          : null;
        setApiKeyAccountsCache(nextUpstreamOverview || safeOverview);
      } else {
        apiKeyAccountsCacheBaseUrlRef.current = nextSettings.management_site_base_url;
        if (nextUpstreamOverview) cacheApiKeyAccounts(nextUpstreamOverview, nextSettings.management_site_base_url);
      }
      setSettings((current) => (appSettingsEqual(current, nextSettings) ? current : nextSettings));
    })();

    const summaryPromise = queryClient.fetchQuery({ queryKey: queryKeys.summary, queryFn: api.summary });
    const accountsPromise = queryClient.fetchQuery({ queryKey: queryKeys.accounts, queryFn: api.accounts });
    const mailboxesPromise = queryClient.fetchQuery({ queryKey: queryKeys.mailboxes, queryFn: api.mailboxes });
    const phonePromise = includePhones
      ? queryClient.fetchQuery({ queryKey: queryKeys.phones, queryFn: api.phones }).catch(() => null)
      : Promise.resolve<PhoneNumber[] | null>(null);
    const jobsPromise = queryClient.fetchQuery({ queryKey: queryKeys.historyJobs, queryFn: api.jobs });
    const eventsPromise = queryClient.fetchQuery({ queryKey: queryKeys.historyEvents, queryFn: api.events });
    const exceptionRecordsPromise = queryClient.fetchQuery({
      queryKey: queryKeys.historyExceptions,
      queryFn: api.exceptionRecords,
    }).catch(() => null);

    await Promise.all([
      commitResponse(summaryPromise, setSummary),
      commitResponse(accountsPromise, setAccounts),
      commitResponse(mailboxesPromise, setMailboxes),
      commitResponse(phonePromise, (nextPhones) => {
        if (nextPhones) setPhones(nextPhones);
      }),
      commitResponse(jobsPromise, setJobs),
      commitResponse(eventsPromise, setEvents),
      commitResponse(exceptionRecordsPromise, (nextExceptionRecords) => {
        if (nextExceptionRecords) setExceptionRecords(nextExceptionRecords);
      }),
      commitSettingsAndOverview,
    ]);
  }, [cacheApiKeyAccounts, queryClient, resetUsageEstimateRequests]);

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
      .filter((account) => Boolean(account.management_account_id))
      .map((account) => [account.management_account_id || "", account] as const);
    return new Map(entries);
  }, [usageEstimate]);

  const accountCounts = useMemo(() => accountDisplayCounts(accounts), [accounts]);
  const problemUnusedQuota = useMemo(() => (usageEstimate ? usageProblemAccountUnusedQuota(usageEstimate.accounts) : null), [usageEstimate]);

  const loadUsageEstimate = useCallback((refresh = true): Promise<UsageEstimate> => {
    const activeRefresh = usageEstimateRefreshRequestRef.current;
    if (activeRefresh) return activeRefresh;
    const activeRead = usageEstimateReadRequestRef.current;
    if (!refresh && activeRead) return activeRead;

    const request = usageEstimateRequestsRef.current.beginForeground();
    setUsageLoading(true);
    setUsageError("");
    const requestPromise = (async () => {
      try {
        const nextEstimate = await queryClient.fetchQuery({
          queryKey: queryKeys.usageEstimate(refresh),
          queryFn: () => api.usageEstimate(refresh),
        });
        if (request.isCurrent()) {
          setUsageEstimate(nextEstimate);
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

    const activeRequestRef = refresh ? usageEstimateRefreshRequestRef : usageEstimateReadRequestRef;
    activeRequestRef.current = requestPromise;
    void requestPromise.then(
      () => {
        if (activeRequestRef.current === requestPromise) activeRequestRef.current = null;
      },
      () => {
        if (activeRequestRef.current === requestPromise) activeRequestRef.current = null;
      },
    );
    return requestPromise;
  }, [queryClient]);

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
    return cancelOAuthUsageBackgroundRefresh;
  }, [cancelOAuthUsageBackgroundRefresh]);

  const loadUsageLimitSamples = useCallback(async () => {
    setUsageLimitSamplesLoading(true);
    setUsageLimitSamplesError("");
    try {
      setUsageLimitSamples(await queryClient.fetchQuery({
        queryKey: queryKeys.usageSamples,
        queryFn: api.usageLimitSamples,
      }));
    } catch (error) {
      setUsageLimitSamplesError(error instanceof Error ? error.message : "额度样本读取失败");
    } finally {
      setUsageLimitSamplesLoading(false);
    }
  }, [queryClient]);

  const refreshViewData = useCallback(async (
    view: View,
    { background, refreshUsage }: { background: boolean; refreshUsage: boolean },
  ) => {
      if (view === "overview") {
        const [nextSummary, nextAccounts, nextJobs, nextEvents, nextUpstream] = await Promise.all([
          api.summary(),
          api.accounts(),
          api.jobs(),
          api.events(),
          api.upstreams(false).catch(() => null),
        ]);
        setSummary(nextSummary);
        setAccounts(nextAccounts);
        setJobs(nextJobs);
        setEvents(nextEvents);
        if (nextUpstream) cacheApiKeyAccounts(nextUpstream, settings.management_site_base_url);
        await loadUsageEstimate(false);
        setApiKeyRefreshVersion((current) => current + 1);
      } else if (view === "accounts") {
        const [nextAccounts, nextMailboxes] = await Promise.all([api.accounts(), api.mailboxes()]);
        setAccounts(nextAccounts);
        setMailboxes(nextMailboxes);
        await loadUsageEstimate(false);
      } else if (view === "api-keys") {
        const nextUpstream = await api.upstreams(false);
        cacheApiKeyAccounts(nextUpstream, settings.management_site_base_url);
        setApiKeyRefreshVersion((current) => current + 1);
      } else if (view === "usage") {
        await loadUsageEstimate(refreshUsage);
      } else if (view === "usage-samples") {
        if (background) {
          setUsageLimitSamples(await api.usageLimitSamples());
        } else {
          await loadUsageLimitSamples();
        }
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
      } else if (view === "settings" && !background) {
        setSettings(await api.settings());
      }
  }, [cacheApiKeyAccounts, loadUsageEstimate, loadUsageLimitSamples, settings.management_site_base_url]);

  const refreshCurrentViewInBackground = useCallback(async () => {
    if (document.visibilityState !== "visible" || pageRefreshing || syncBusy) return;
    try {
      await refreshViewData(view, { background: true, refreshUsage: false });
    } catch {
      // Preserve the last successful snapshot; the next interval can recover.
    }
  }, [pageRefreshing, refreshViewData, syncBusy, view]);

  const refreshCurrentView = useCallback(async () => {
    if (pageRefreshing) return;
    setPageRefreshing(true);
    setNotice("");
    loadAllRequestSequenceRef.current += 1;
    try {
      await refreshViewData(view, { background: false, refreshUsage: view === "usage" });
      setNotice("当前页面数据已刷新");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "当前页面刷新失败");
    } finally {
      setPageRefreshing(false);
    }
  }, [pageRefreshing, refreshViewData, view]);

  useVisiblePageRefresh(view, refreshCurrentViewInBackground);

  useEffect(() => {
    loadAll().catch((error) => setNotice(error instanceof Error ? error.message : "数据读取失败"));
  }, [loadAll]);

  useEffect(() => {
    if (view !== "usage-samples" || usageLimitSamples || usageLimitSamplesLoading) return;
    loadUsageLimitSamples().catch(() => undefined);
  }, [loadUsageLimitSamples, usageLimitSamples, usageLimitSamplesLoading, view]);

  useEffect(() => {
    if (usageLoading) return;
    if (usageError) return;
    if (usageEstimate) return;
    if (view === "overview" || view === "accounts" || view === "usage") {
      loadUsageEstimate(false).catch(() => undefined);
    }
  }, [loadUsageEstimate, usageError, usageEstimate, usageLoading, view]);

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
      const topbar = topbarRef.current;
      const sidebar = sidebarRef.current;
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
    const topbar = topbarRef.current;
    const sidebar = sidebarRef.current;
    if (resizeObserver) {
      if (topbar) resizeObserver.observe(topbar);
      if (sidebar) resizeObserver.observe(sidebar);
    }
    window.addEventListener("resize", updateLayoutOffsets);
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener("resize", updateLayoutOffsets);
    };
  }, [siteName, view]);

  useLayoutEffect(() => {
    workspaceRef.current?.scrollTo({ top: 0, left: 0, behavior: "auto" });
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
    const previousManagementSiteBaseUrl = settings.management_site_base_url;
    const nextManagementSiteBaseUrl = payload.management_site_base_url || previousManagementSiteBaseUrl;
    const changesManagementSiteCredential = Boolean(
      payload.management_site_x_api_key?.trim() || payload.clear_management_site_x_api_key,
    );
    const reusesExistingCredential = settings.management_site_x_api_key_set
      && !payload.clear_management_site_x_api_key
      && !payload.management_site_x_api_key?.trim();
    const changesCredentialOrigin =
      credentialBindingOrigin(previousManagementSiteBaseUrl) !== credentialBindingOrigin(nextManagementSiteBaseUrl);
    if (
      changesCredentialOrigin
      && reusesExistingCredential
      && !payload.confirm_management_site_credential_rebind
    ) {
      const confirmed = window.confirm(
        "管理站点域名已改变。继续会把当前管理凭据重新绑定到新域名，是否确认？",
      );
      if (!confirmed) return;
      payload = { ...payload, confirm_management_site_credential_rebind: true };
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
        nextSettings.management_site_base_url !== previousManagementSiteBaseUrl
        || changesManagementSiteCredential
      ) {
        clearFrontendSessionCaches();
        apiKeyAccountsCacheBaseUrlRef.current = nextSettings.management_site_base_url;
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
      || (kind === "api-key" && operations.blocking)
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
      const liveOverview = await api.upstreams();
      const bindingCounts = upstreamLegacyBindingCounts(liveOverview);
      const confirmationRequired = bindingCounts.unbound > 0 || bindingCounts.originRebind > 0;
      if (
        confirmationRequired
        && !window.confirm(apiAccountLegacyBindingConfirmationMessage(bindingCounts))
      ) {
        return { message: "已取消 API 账号同步。", cancelled: true };
      }
      const skipUpstreamIds = [...operations.discoveringUpstreamIds];
      const result = await api.syncApiKeyAccounts(
        liveOverview,
        confirmationRequired,
        Array.from(new Set(skipUpstreamIds)),
      );
      const overview = result.overview || {
        ...liveOverview,
        upstreams: result.upstreams || [],
        unassigned_accounts: liveOverview.unassigned_accounts,
      };
      cacheApiKeyAccounts(overview, settings.management_site_base_url);
      setApiKeyRefreshVersion((current) => current + 1);
      const accountCount = overview.upstreams.reduce(
        (total, upstream) => total + (upstream.account_count || upstream.accounts?.length || 0),
        overview.unassigned_accounts.length,
      );
      return {
        message: `已同步 ${accountCount} 个 API 账号；${apiAccountSyncMessage(
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

  const dashboardPages: DashboardPages = {
    overview: {
      summary, accounts, accountCounts, apiKeyRefreshVersion, jobs, events, problemUnusedQuota,
      showStaleNegativeBalanceAlert: settings.show_stale_negative_balance_alert ?? true,
      balanceBasis: settings.upstream_negative_balance_basis || "wallet",
      balanceThreshold: settings.upstream_balance_pause_threshold ?? 0,
      upstreamOverview: apiKeyAccountsCache, usageByAccountId, usageByEmail,
      onOpenUpstreams: () => navigateToApiKeySubview("upstreams"),
    },
    accounts: {
      accounts, accountJumpTarget, busy: syncBusy, mailboxes, usageByAccountId, usageByEmail,
      onDeleteDeactivated: () => runAction(api.deleteDeactivatedAccounts, "已删除封禁/重复账号"),
      onDeleteSelectedAccounts: (selectedAccounts) =>
        runAction(() => api.deleteSelectedAccounts(selectedAccounts.map(selectedAccountDeleteItem)), "已删除所选账号"),
      onAccountJumpHandled: () => setAccountJumpTarget(null),
      onDeleteRemote: (account) => runAction(async () => {
        const result = await api.deleteRemoteAccount(account.management_account_id || "");
        setUsageEstimate(null);
        return result;
      }, "已删除 管理站点 API 账号"),
      onToggleDeleteUnlock: (account, unlocked) => runAction(
        () => api.updateRemoteAccountDeleteLock(account.management_account_id || "", unlocked),
        unlocked ? "已解锁删除保护" : "已恢复删除保护",
      ),
      onRefresh: (email) => runAction(() => api.refresh(email), "已创建检测/刷新任务"),
      onToggleUsageEstimate: (id, enabled) => runAction(async () => {
        const result = await api.updateAccountUsageEstimate(id, enabled);
        await loadUsageEstimate(false);
        return result;
      }, enabled ? "已纳入额度估算" : "已排除额度估算"),
      onToggleRefreshLock: (account, unlocked) => runAction(
        () => api.updateAccountRefreshLock(account.id, unlocked),
        unlocked ? "已解锁自动刷新" : "已恢复自动刷新锁定",
      ),
      onAccountEdited: (message) => {
        setNotice(message);
        void api.accounts().then(setAccounts);
      },
      onNotice: setNotice,
    },
    apiKeys: {
      cacheBaseUrl: settings.management_site_base_url,
      cachedData: apiKeyAccountsCache,
      upstreamMonitorFallbackTestModels: settings.upstream_monitor_fallback_test_models?.length
        ? settings.upstream_monitor_fallback_test_models
        : settings.upstream_monitor_fallback_test_model ? [settings.upstream_monitor_fallback_test_model] : [],
      displayTimeZone: settings.display_timezone || defaultTimeZone,
      changeLogPageSize: settings.change_log_page_size || 50,
      changeLogPageSizeOptions: settings.change_log_page_size_options,
      globallyBusy: busy || apiKeySyncBusy || pageRefreshing,
      onCacheChange: cacheApiKeyAccounts,
      onNotice: setNotice,
      onOperationStart: beginApiKeyViewOperation,
      onSubviewChange: navigateToApiKeySubview,
      rateWritesEnabled: upstreamRateWritesAllowed(settings.upstream_rate_sync_enabled, settings.automation_paused),
      refreshVersion: apiKeyRefreshVersion,
      shareSameCompositePriority: settings.priority_share_same_upstream_actual_multiplier ?? false,
      subview: route.apiKeySubview,
    },
    usage: {
      estimate: usageEstimate, error: usageError, loading: usageLoading,
      onLocateAccount: (account) => {
        setAccountJumpTarget({ email: account.email, managementAccountId: account.management_account_id, requestedAt: Date.now() });
        navigateToView("accounts");
      },
    },
    usageSamples: {
      data: usageLimitSamples, error: usageLimitSamplesError, loading: usageLimitSamplesLoading || busy,
      onDelete: (sampleId) => runAction(async () => {
        const result = await api.deleteUsageLimitSample(sampleId);
        await loadUsageLimitSamples();
        return result;
      }, "额度样本已删除"),
      onDeleteMany: (sampleIds) => runAction(async () => {
        const result = await api.deleteUsageLimitSamples(sampleIds);
        await loadUsageLimitSamples();
        return result;
      }, `已删除 ${sampleIds.length} 条额度样本`),
      onRefresh: loadUsageLimitSamples,
    },
    mailboxes: {
      mailboxes, busy: syncBusy,
      onImport: (content, provider) => runAction(() => api.importMailboxes(content, provider), "导入完成"),
      onDelete: (id) => runAction(() => api.deleteMailbox(id), "已删除"),
      onDeleteMany: (ids) => runAction(() => api.deleteMailboxes(ids), `已删除 ${ids.length} 个邮箱`),
      onNotice: setNotice,
    },
    phones: {
      accounts, busy: syncBusy, phones,
      onDelete: (id) => runAction(() => api.deletePhone(id), "已删除手机号"),
      onDeleteMany: (ids) => runAction(() => api.deletePhones(ids), `已删除 ${ids.length} 个手机号`),
      onExport: async () => {
        const result = await api.exportPhones();
        downloadTextFile("phones.txt", result.message || "");
        setNotice("手机号已导出");
      },
      onImport: (content) => runAction(() => api.importPhones(content), "导入完成"),
      onRefreshStatuses: () => runAction(api.refreshPhoneStatuses, "接码状态已刷新"),
      onUpdateBindings: (id, accountEmails) => runAction(() => api.updatePhoneBindings(id, accountEmails), "绑定已更新"),
    },
    history: {
      busy: syncBusy, events, exceptionRecords, formatDate, jobs, now,
      timeZone: settings.display_timezone || defaultTimeZone,
      onClear: () => runAction(api.clearHistory, "历史已清空"),
      onDeleteExceptionRecord: (id) => runAction(() => api.deleteExceptionRecord(id), "异常账号记录已删除"),
      onLocateAccount: (record) => {
        setAccountJumpTarget({ email: record.email, managementAccountId: record.management_account_id, requestedAt: Date.now() });
        navigateToView("accounts");
      },
    },
    settings: {
      busy: syncBusy, logoUrl: siteLogoUrl, settings,
      subscriptionTypes: [...new Set(accounts.map((account) => account.subscription_type).filter(Boolean))],
      onScan: () => runAction(api.scanManagementSite, "扫描完成"),
      onSave: saveSettings,
      onTestNotification: () => runAction(api.testNotification, "测试通知已发送"),
      onValidityChange: setSettingsFormInvalid,
    },
  };

  return (
    <TimeZoneContext.Provider value={settings.display_timezone || defaultTimeZone}>
      <NowContext.Provider value={now}>
        <DashboardProvider value={dashboardPages}>
          <AppShell
            apiKeySyncAction={(
            <ToolbarTimeButton
              disabled={busy || apiKeySyncBusy || operations.blocking}
              icon={KeyRound}
              label="同步 API 账号"
              loading={apiKeySyncBusy}
              onClick={runApiKeySync}
              time={apiKeySyncActionTime}
            />
            )}
            brandName={siteName}
            busy={syncBusy}
            logoUrl={siteLogoUrl}
            notice={notice}
            oauthSyncAction={(
            <ToolbarTimeButton
              disabled={busy || oauthSyncBusy}
              icon={RefreshCcw}
              label="同步 OAuth 账号"
              loading={oauthSyncBusy}
              onClick={runOAuthSync}
              time={oauthSyncActionTime}
            />
            )}
            onDismissNotice={() => setNotice("")}
            onLogout={async () => {
            setBusy(true);
            try {
              await signOut();
            } finally {
              setBusy(false);
              apiKeyAccountsCacheBaseUrlRef.current = null;
              setApiKeyAccountsCache(null);
              setUsageEstimate(null);
              setUsageLimitSamples(null);
              resetUsageEstimateRequests();
            }
            }}
            onPreloadApiKeys={() => void loadApiKeyAccountsView()}
            onRefresh={() => void refreshCurrentView()}
            pageRefreshing={pageRefreshing}
            settingsSaveDisabled={syncBusy || settingsFormInvalid}
            sidebarRef={sidebarRef}
            themeAction={<ThemeToggle sidebar theme={theme} onToggleTheme={toggleTheme} />}
            topbarRef={topbarRef}
            workspaceRef={workspaceRef}
          />
        </DashboardProvider>
      </NowContext.Provider>
    </TimeZoneContext.Provider>
  );
}

export default DashboardController;
