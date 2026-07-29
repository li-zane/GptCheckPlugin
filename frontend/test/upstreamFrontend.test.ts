import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  api,
  NO_FRONTEND_TIMEOUT,
  upstreamLegacyBindingCounts,
  upstreamChangeLogsPath,
  upstreamRateChangeLogsPath,
  upstreamUsageHistoryPath,
} from "../src/api.ts";
import {
  automationDurationDisplayValue,
  automationDurationSecondsValue,
  preferredAutomationDurationUnit,
} from "../src/automationDuration.ts";
import {
  accountCanBeLivenessTested,
  livenessAccountIds,
  MAX_LIVENESS_ACCOUNTS,
} from "../src/accountLiveness.ts";
import { accountFilterFacetCandidates } from "../src/accountFilterFacets.ts";
import { sortAccountsForTable } from "../src/accountTableSort.ts";
import {
  buildUpstreamAccountUpdatePayload,
  canSetManualMultiplier,
} from "../src/upstreamAccountForm.ts";
import { channelCredentialBindingChanged } from "../src/upstreamCredentialBinding.ts";
import {
  CHANNEL_MONITOR_TIMELINE_LIMIT,
  latestChannelMonitorStatus,
  recentChannelMonitorTimeline,
} from "../src/channelMonitorPresentation.ts";
import {
  CHANGE_LOG_READ_RETRY_DELAYS_MS,
  departedChangeLogSubview,
  pendingReadThroughId,
  visibleChangeLogUnreadCounts,
} from "../src/changeLogReadState.ts";
import {
  changeLogCacheKey,
  clearChangeLogCache,
  clearChangeLogMemoryCache,
  markChangeLogCacheRead,
  mergeChangeLogItems,
  readChangeLogCache,
  writeChangeLogCache,
} from "../src/changeLogCache.ts";
import {
  isGenericUpstreamChannelError,
  partitionUpstreamChannels,
  upstreamChannelTokenInvalid,
} from "../src/upstreamChannelPresentation.ts";
import {
  eventDurationBreakdown,
  eventDurationMs,
  formatElapsedDuration,
  timestampDurationMs,
} from "../src/durationPresentation.ts";
import {
  firstUnusedFallbackModel,
  MAX_FALLBACK_TEST_MODELS,
  moveFallbackModel,
  normalizeFallbackModelChain,
} from "../src/fallbackModelChain.ts";
import { LatestRequestCoordinator } from "../src/latestRequest.ts";
import { oauthUsageBackgroundRefreshIntervals } from "../src/oauthUsageRefresh.ts";
import {
  persistOverviewBalanceAlertDismissed,
  readOverviewBalanceAlertDismissed,
} from "../src/overviewBalanceAlertPreference.ts";
import {
  clearUpstreamOverviewCache,
  readUpstreamOverviewCache,
  sanitizeUpstreamOverview,
  upstreamOverviewCacheKey,
  upstreamOverviewHasLiveMutationData,
  writeUpstreamOverviewCache,
} from "../src/upstreamOverviewCache.ts";
import {
  upstreamChangeReasonLabel,
  upstreamHealthStatusLabel,
  upstreamStatusLabel,
  upstreamStatusTone,
} from "../src/upstreamLabels.ts";
import {
  accountBillingRateChange,
  normalizedUpstreamMultiplier,
  remoteSchedulableChange,
  upstreamGroupStatusChange,
  upstreamKeyStatusChange,
  upstreamRateChange,
  upstreamRechargeRateChange,
  upstreamChangeSummary,
  upstreamGroupRatePresentation,
} from "../src/upstreamRatePresentation.ts";
import {
  accountCompositeMultiplier,
  filterUpstreamAccountEntries,
  flattenUpstreamAccounts,
  priorityIntervalAssignmentBlocked,
  priorityIntervalAssignmentNeedsConfirmation,
  priorityTieMoveOptions,
  priorityTieMultiplierKey,
  sortUpstreamAccountEntries,
  sortUpstreamAccountEntriesByName,
  upstreamAccountMatchesStatus,
  upstreamAccountPlatforms,
  upstreamAccountChannels,
} from "../src/upstreamPriorityPresentation.ts";
import {
  apiAccountSyncMessage,
  apiAccountLegacyBindingConfirmationMessage,
  accountRateStatusLabel,
  channelDiscoveryErrorMessage,
  channelDiscoverySuccessMessage,
  upstreamDiscoveryCopy,
  upstreamMutationControlsDisabled,
  upstreamRateWritesAllowed,
} from "../src/upstreamSyncPresentation.ts";
import { sortUsageLimitSamples } from "../src/usageSampleSort.ts";
import {
  apiKeySubviewPaths,
  normalizePathname,
  pathForRoute,
  pathForView,
  routeFromPath,
  viewFromPath,
  viewPaths,
  type ApiKeySubview,
  type View,
} from "../src/viewRouting.ts";
import {
  formatUpstreamBalance,
  rechargeAdjustedUsage,
  shouldShowUpstreamAccountUsage,
  visibleUpstreamBalanceMessage,
} from "../src/upstreamUsagePresentation.ts";
import {
  buildDisplayedUsageEstimate,
  usageDetailAccountCounts,
  usageProblemAccountUnusedQuota,
} from "../src/usageEstimatePresentation.ts";
import type {
  Account,
  AccountUsageEstimate,
  UpstreamAccount,
  UpstreamChannelsResponse,
  UsageEstimate,
  UsageLimitSample,
  UsageWindowEstimate,
} from "../src/types.ts";

const usageSamples: UsageLimitSample[] = [
  {
    id: 2,
    account_key: "two",
    email: null,
    sub2api_account_id: "2",
    plan_cohort: "plus",
    subscription_type: "plus",
    subscription_label: "Plus",
    reset_key: "two",
    reset_at: null,
    observed_limit: 20,
    raw_spent: 20,
    used_percent: 100,
    created_at: "2026-07-15T01:00:00Z",
    updated_at: "2026-07-15T03:00:00Z",
  },
  {
    id: 1,
    account_key: "one",
    email: null,
    sub2api_account_id: "1",
    plan_cohort: "plus",
    subscription_type: "plus",
    subscription_label: "Plus",
    reset_key: "one",
    reset_at: null,
    observed_limit: 10,
    raw_spent: 10,
    used_percent: 100,
    created_at: "2026-07-15T01:00:00Z",
    updated_at: "2026-07-15T04:00:00Z",
  },
];

test("latest request coordination rejects stale foreground and background work", () => {
  const requests = new LatestRequestCoordinator();
  const first = requests.beginForeground();
  assert.equal(requests.beginBackground(), null);

  const second = requests.beginForeground();
  assert.equal(first.isCurrent(), false);
  assert.equal(first.finish(), false);
  assert.equal(second.isCurrent(), true);
  assert.equal(second.finish(), true);

  const background = requests.beginBackground();
  assert.ok(background);
  const foreground = requests.beginForeground();
  assert.equal(background.isCurrent(), false);
  assert.equal(background.finish(), false);
  requests.invalidate();
  assert.equal(foreground.finish(), false);

  const requestBeforeReset = requests.beginForeground();
  requests.invalidate();
  const backgroundAfterReset = requests.beginBackground();
  assert.ok(backgroundAfterReset);
  const requestAfterReset = requests.beginForeground();
  assert.equal(requestBeforeReset.finish(), false);
  assert.equal(requestAfterReset.isCurrent(), true);
  assert.equal(requestAfterReset.finish(), true);
});

test("usage estimate presentation filters accounts and rebuilds aggregates", () => {
  const usageWindow = (
    overrides: Partial<UsageWindowEstimate> = {},
  ): UsageWindowEstimate => ({
    used_percent: 20,
    spent: 2,
    raw_spent: 2,
    baseline_spent: 0,
    estimate_spent: 2,
    estimate_basis: "sample",
    spend_source: "usage",
    estimated_limit: 10,
    remaining: 8,
    remaining_percent: 80,
    reset_at: null,
    remaining_seconds: null,
    requests: null,
    tokens: null,
    estimable: true,
    rate_limited: false,
    source: "test",
    window_kind: "five_hour",
    window_minutes: 300,
    window_label: "5h",
    ...overrides,
  });
  const usageAccount = (
    id: string,
    overrides: Partial<AccountUsageEstimate> = {},
  ): AccountUsageEstimate => ({
    email: `${id}@example.com`,
    account_name: id,
    sub2api_account_id: id,
    platform: "openai",
    account_type: "oauth",
    subscription_plan: "plus",
    subscription_type: "plus",
    subscription_label: "Plus",
    subscription_billing_period: "monthly",
    has_active_subscription: true,
    status: "active",
    schedulable: true,
    deactive: false,
    error: false,
    rate_limited: false,
    rate_limited_windows: [],
    usage_estimate_enabled: true,
    rate_multiplier: 1,
    groups: [{ id: "group-1", name: "Group 1" }],
    usage_error: null,
    five_hour: usageWindow(),
    seven_day: usageWindow({
      window_kind: "seven_day",
      window_minutes: 10_080,
      window_label: "7d",
    }),
    seven_day_token_history: {
      total_spent: 0,
      total_tokens: 0,
      total_estimated_limit: 0,
      window_count: 0,
      windows: [],
    },
    ...overrides,
  });
  const active = usageAccount("active");
  const paused = usageAccount("paused", { schedulable: false });
  const limited = usageAccount("limited", {
    rate_limited: true,
    five_hour: usageWindow({ rate_limited: true }),
  });
  const failed = usageAccount("failed", { error: true });
  const emptyAggregate = {
    spent: 0,
    estimated_limit: null,
    remaining: null,
    remaining_percent: null,
    used_percent: null,
    account_count: 0,
    enabled_account_count: 0,
    estimable_accounts: 0,
  };
  const estimate: UsageEstimate = {
    updated_at: "2026-07-27T00:00:00Z",
    refreshed_usage: true,
    formula: {},
    overall: {
      account_count: 4,
      five_hour: emptyAggregate,
      seven_day: emptyAggregate,
    },
    groups: [{
      group_id: "group-1",
      group_name: "Group 1",
      account_count: 4,
      five_hour: emptyAggregate,
      seven_day: emptyAggregate,
    }],
    accounts: [active, paused, limited, failed],
  };

  const displayed = buildDisplayedUsageEstimate(estimate, false);

  assert.deepEqual(
    displayed.accounts.map((account) => account.sub2api_account_id),
    ["active", "limited"],
  );
  assert.deepEqual(usageDetailAccountCounts(displayed.accounts), {
    normal: 1,
    rateLimited: 1,
  });
  assert.equal(displayed.overall.five_hour.estimated_limit, 10);
  assert.equal(displayed.overall.five_hour.remaining, 8);
  assert.equal(usageProblemAccountUnusedQuota(estimate.accounts).accountCount, 1);
});

test("view routes canonicalize paths and keep every dashboard view addressable", () => {
  const expectedRoutes: Array<[View, string]> = Object.entries(viewPaths) as Array<[View, string]>;
  for (const [view, path] of expectedRoutes) {
    assert.equal(pathForView(view), path);
    assert.equal(viewFromPath(path), view);
    assert.equal(viewFromPath(`${path}/`), view);
  }
  assert.equal(normalizePathname("/settings///"), "/settings");
  assert.equal(viewFromPath("/"), "overview");
  assert.equal(viewFromPath("/not-a-view"), "overview");
  assert.deepEqual(routeFromPath("/api-keys"), { view: "api-keys", apiKeySubview: "channels" });
  assert.deepEqual(routeFromPath("/api-keys/"), { view: "api-keys", apiKeySubview: "channels" });

  const expectedApiKeyRoutes = Object.entries(apiKeySubviewPaths) as Array<[ApiKeySubview, string]>;
  for (const [apiKeySubview, path] of expectedApiKeyRoutes) {
    const route = { view: "api-keys" as const, apiKeySubview };
    assert.deepEqual(routeFromPath(path), route);
    assert.deepEqual(routeFromPath(`${path}/`), route);
    assert.equal(pathForRoute(route), path);
  }
});

test("App navigation uses history state, a page refresh control, and a top settings submit", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const rootPackage = JSON.parse(readFileSync(new URL("../../package.json", import.meta.url), "utf8"));
  const productionServer = readFileSync(new URL("../../backend/run_production.py", import.meta.url), "utf8");
  assert.match(source, /window\.history\.pushState\(null, "", nextPath\)/);
  assert.match(source, /window\.addEventListener\("popstate", handlePopState\)/);
  assert.match(source, /window\.history\.replaceState\(null, "", canonicalPath\)/);
  assert.doesNotMatch(source, /window\.location\.reload\(\)/);
  assert.match(source, /const refreshCurrentView = useCallback\(async \(\) =>/);
  assert.match(source, /onClick=\{\(\) => void refreshCurrentView\(\)\}/);
  assert.match(source, /form="runtime-settings-form"/);
  assert.match(source, /id="runtime-settings-form"/);
  assert.equal((source.match(/保存设置/g) || []).length, 1);
  const refreshButton = source.indexOf('aria-label="刷新当前页面数据"');
  const settingsButton = source.indexOf('className="primary-button toolbar-settings-save"');
  const firstSyncButton = source.indexOf("<ToolbarTimeButton", refreshButton);
  assert.ok(refreshButton >= 0 && settingsButton > refreshButton && settingsButton < firstSyncButton);
  assert.match(source, /disabled=\{syncBusy \|\| settingsFormInvalid\}/);
  const viteConfig = readFileSync(new URL("../vite.config.ts", import.meta.url), "utf8");
  assert.match(viteConfig, /port:\s*5173/);
  assert.match(viteConfig, /strictPort:\s*true/);
  assert.match(viteConfig, /"\^\/api\(\?:\/\|\$\)"/);
  assert.match(rootPackage.scripts["backend:dev"], /--port 5173$/);
  assert.match(productionServer, /port=5173/);
  assert.doesNotMatch(rootPackage.scripts.dev, /frontend:dev/);
  assert.match(source, /const loadApiKeyAccountsView = \(\) => import\("\.\/ApiKeyAccountsView"\)/);
  assert.match(source, /const ApiKeyAccountsView = lazy\(async \(\) =>/);
  assert.match(source, /<Suspense fallback=\{<Empty label="正在加载 API Key 页面" \/>\}>/);
  assert.match(source, /if \(item\.id === "api-keys"\) void loadApiKeyAccountsView\(\)/);
  assert.match(source, /const loadHistoryView = \(\) => import\("\.\/HistoryView"\)/);
  assert.match(source, /const HistoryView = lazy\(async \(\) =>/);
  assert.doesNotMatch(source, /from "\.\/HistoryView"/);
  const historySource = readFileSync(new URL("../src/HistoryView.tsx", import.meta.url), "utf8");
  assert.match(historySource, /export function HistoryView/);
  assert.match(source, /view !== "usage-samples" \|\| usageLimitSamples \|\| usageLimitSamplesLoading/);
  assert.match(source, /loadUsageLimitSamples\(\)\.catch\(\(\) => undefined\);/);
  assert.match(source, /discord-bot-setup-screenshot/);
  assert.match(source, /discord-bot-setup-screenshot-button/);
  assert.match(source, /DiscordBotSetupScreenshotPreviewDialog/);
  assert.match(source, /discord\.com\/developers\/applications/);
  assert.match(source, /Public Bot.*公开/);
  assert.match(source, /Private Bot.*私有，推荐/);
  assert.match(source, /User Install.*用户安装/);
  assert.match(source, /Guild Install.*服务器安装/);
  assert.match(source, /已授权的应用/);
  assert.match(source, /General Information \/ Delete App/);
  assert.match(source, /删除的是整个应用，不是单独删除 Bot/);
  const styleSource = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(styleSource, /\.discord-bot-setup-guide-body[\s\S]*overflow-y:\s*auto/);
  assert.match(styleSource, /\.discord-bot-setup-screenshot/);
  assert.match(styleSource, /\.discord-bot-screenshot-preview-dialog/);
  assert.match(styleSource, /\.discord-bot-install-mode-grid[\s\S]*grid-template-columns:\s*repeat\(2/);
});

test("automatic pause settings keep balance controls while multiplier policy belongs to intervals and accounts", () => {
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const accountSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(appSource, /上游余额低于阈值时自动暂停 API Key 账号/);
  assert.match(appSource, /余额达到或高于阈值且其他暂停原因均解除后自动恢复/);
  assert.match(appSource, /upstream_balance_pause_threshold: balancePauseThresholdNumber/);
  assert.match(appSource, /首页显示上次已知低余额提醒/);
  assert.doesNotMatch(appSource, /api_key_auto_pause_on_upstream_rate_increase_enabled:/);
  assert.doesNotMatch(appSource, /upstream_rate_pause_mode:/);
  assert.doesNotMatch(appSource, /综合上游倍率上涨时自动暂停 API Key 账号/);
  assert.match(accountSource, /综合上游倍率上涨时自动暂停账号/);
  assert.match(accountSource, /rate_pause_enabled: form\.ratePauseEnabled/);
  assert.match(accountSource, /<option value="inherit">继承优先级区间<\/option>/);
  assert.match(accountSource, /<option value="disabled">关闭<\/option>/);
  assert.match(accountSource, /<option value="custom">单独设置<\/option>/);
  assert.match(accountSource, /当前账号未绑定优先级区间，当前配置不会启用倍率上涨暂停/);
  assert.match(accountSource, /const rateThreshold = account\.rate_pause_effective_enabled\s*\?\s*formatMultiplier\(account\.rate_absolute_threshold\)/);
  assert.match(accountSource, /几何中位数反比例曲线/);
  assert.match(accountSource, /低倍率优先/);
  assert.match(accountSource, /固定间隔/);
  assert.doesNotMatch(accountSource, /最低优先级间隔/);
  assert.match(accountSource, /取整冲突由系统使用 1 个优先级的最小间隔处理/);
  assert.match(accountSource, /同一 Sub2API 调度分组内，数值更低的区间权重更高/);
  assert.match(accountSource, /\[40, 70\) 与 \[70, 100\)/);
  assert.doesNotMatch(accountSource, /相对涨幅/);
  assert.doesNotMatch(accountSource, /rate_pause_mode/);
  assert.doesNotMatch(accountSource, /rate_increase_threshold_percent/);
  assert.match(accountSource, /ratePauseThresholdPayload\(accountForm\.rateAbsoluteThreshold\)/);
  assert.doesNotMatch(appSource, /aria-disabled=\{!apiKeyChannelMonitorPauseEnabled\}/);
});

test("API key availability settings preserve unbound monitor mode and support explicit disabling", () => {
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const accountSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const apiSource = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");
  const helpSource = readFileSync(new URL("../src/HelpPopover.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(accountSource, /<option value="channel_monitor">绑定监控面板（默认）<\/option>/);
  assert.match(accountSource, /<option value="disabled">关闭<\/option>/);
  assert.match(accountSource, /请选择具体监控面板/);
  const monitorSelectId = accountSource.indexOf('id="api-key-availability-monitor"');
  const monitorSelectStart = accountSource.lastIndexOf("<select", monitorSelectId);
  const monitorSelectEnd = accountSource.indexOf("</select>", monitorSelectId);
  assert.ok(monitorSelectId >= 0 && monitorSelectStart >= 0 && monitorSelectEnd > monitorSelectId);
  assert.doesNotMatch(accountSource.slice(monitorSelectStart, monitorSelectEnd), /required/);
  assert.match(accountSource, /const submittedAvailabilityMode = accountForm\.availabilityCheckMode;/);
  assert.doesNotMatch(accountSource, /accountForm\.availabilityCheckMode === "channel_monitor"\s*&& !accountForm\.availabilityMonitorId\s*\? "disabled"/);
  assert.match(accountSource, /payload\.availability_test_model = submittedAvailabilityMode === "disabled"/);
  assert.match(accountSource, /indicatorTone === "unconfigured"\s*\? <CircleOff size=\{13\}/);
  assert.match(accountSource, /api-key-availability-indicator--\$\{indicatorTone\}/);
  assert.match(accountSource, /automaticMonitoringPaused\s*\? "paused"/);
  assert.match(accountSource, /automaticMonitoringPaused/);
  assert.match(accountSource, /已暂停；手动检测仍可用/);
  assert.match(accountSource, /没有属于该账号模型白名单的候选模型/);
  assert.match(accountSource, /可用性与上游数据正在后台探测/);
  assert.match(accountSource, /channel\.background_discovery_pending === true/);
  assert.match(accountSource, /generation !== backgroundRefreshGeneration\.current/);
  assert.match(accountSource, /consecutiveFailures >= 9/);
  assert.match(accountSource, /scheduleBackgroundAccountRefresh\(\)/);
  assert.match(accountSource, /loadData\(true, true\)/);
  assert.match(accountSource, /api\.testUpstreamAccountAvailability\(/);
  assert.match(accountSource, /api\.testUpstreamAccountConnection\(/);
  assert.match(accountSource, /强制连接测试完成/);
  assert.match(accountSource, /<AccountCardConfigurationTags/);
  assert.match(accountSource, /middleEllipsis\(monitorName \|\| "监控面板", 12\)/);
  assert.match(accountSource, /api-key-account-config-tag-wrap--availability/);
  assert.match(accountSource, /label="查看 API Key 可用性检测设置"/);
  assert.match(accountSource, /正常账号使用暂停判定次数/);
  assert.doesNotMatch(accountSource, /连续不可用监测轮次/);
  assert.doesNotMatch(accountSource, /连续可用监测轮次/);
  assert.match(appSource, /暂停判定测试次数/);
  assert.match(appSource, /恢复判定测试次数/);
  assert.match(appSource, /未绑定监控面板时使用回退模型链/);
  assert.match(appSource, /channel_monitor_fallback_test_models: fallbackTestModels/);
  assert.match(appSource, /按从上到下的顺序选择账号白名单中第一个存在的模型/);
  assert.match(appSource, /aria-label=\{`回退测试模型 \$\{index \+ 1\}`\}/);
  assert.match(appSource, /上移回退测试模型/);
  assert.match(appSource, /下移回退测试模型/);
  assert.match(appSource, /删除回退测试模型/);
  assert.match(appSource, /新增模型/);
  assert.match(appSource, /className="settings-model-chain-row" key=\{selectedModel\}/);
  assert.match(appSource, /function FallbackModelChainDialog/);
  assert.match(appSource, /className="settings-model-chain-field settings-model-chain-summary"/);
  assert.match(appSource, /fallbackModelDialogOpen \? \(/);
  assert.match(styles, /\.settings-model-chain-dialog-list\s*\{[^}]*max-height:[^;]+;[^}]*overflow-y:\s*auto;/s);
  assert.doesNotMatch(appSource, /channel-monitor-fallback-model-options/);
  assert.match(appSource, /任意一次连接成功即判定可用，全部失败才暂停或保持暂停/);
  assert.match(appSource, /label="API Key 账号可用性监测与自动暂停"/);
  assert.doesNotMatch(appSource, /跟随 API Key 上游同步/);
  assert.match(appSource, /label="API Key 账号可用性监测与自动暂停"[\s\S]*?<AutomationSettingInherited>跟随上游同步<\/AutomationSettingInherited>/);
  const availabilityRequestStart = apiSource.indexOf("testUpstreamAccountAvailability:");
  const availabilityRequestEnd = apiSource.indexOf("deleteRemoteUpstreamAccount:", availabilityRequestStart);
  assert.ok(availabilityRequestStart >= 0 && availabilityRequestEnd > availabilityRequestStart);
  assert.match(apiSource.slice(availabilityRequestStart, availabilityRequestEnd), /NO_FRONTEND_TIMEOUT/);
  assert.match(apiSource.slice(availabilityRequestStart, availabilityRequestEnd), /testUpstreamAccountConnection:/);
  assert.doesNotMatch(apiSource.slice(availabilityRequestStart, availabilityRequestEnd), /90_000/);
  assert.match(appSource, /globallyBusy=\{busy \|\| apiKeySyncBusy \|\| pageRefreshing\}/);
  assert.doesNotMatch(appSource, /必须绑定具体监控点/);
  assert.match(appSource, /监控面板代表上游的单个分组或模型路由，不代表上游站点整体状态/);
  assert.doesNotMatch(appSource, /明确结果连续达到设置阈值后才暂停或恢复/);
  assert.match(helpSource, /createPortal\(/);
  assert.match(helpSource, /onMouseEnter=\{\(\) => setOpen\(true\)\}/);
  assert.match(helpSource, /onClick=\{\(event\) =>/);
  assert.match(helpSource, /event\.stopPropagation\(\)/);
  assert.match(helpSource, /if \(!nextPinned\) event\.currentTarget\.blur\(\)/);
  assert.match(helpSource, /document\.addEventListener\("pointerdown"/);
  assert.match(styles, /\.help-popover-content\s*\{[^}]*position: fixed;/s);
  assert.match(styles, /\.help-popover-content\s*\{[^}]*pointer-events: auto;/s);
  assert.doesNotMatch(styles, /\.help-popover:hover \.help-popover-content/);
});

test("fallback model chain preserves unique order and supports list controls", () => {
  assert.deepEqual(
    normalizeFallbackModelChain([" gpt-5.4-mini ", "grok-4.5", "gpt-5.4-mini", ""]),
    ["gpt-5.4-mini", "grok-4.5"],
  );
  assert.deepEqual(normalizeFallbackModelChain([], " legacy-model "), ["legacy-model"]);
  assert.equal(
    normalizeFallbackModelChain(Array.from({ length: 12 }, (_, index) => `model-${index}`)).length,
    MAX_FALLBACK_TEST_MODELS,
  );
  assert.deepEqual(moveFallbackModel(["a", "b", "c"], 1, -1), ["b", "a", "c"]);
  assert.deepEqual(moveFallbackModel(["a", "b", "c"], 1, 1), ["a", "c", "b"]);
  assert.deepEqual(moveFallbackModel(["a", "b"], 0, -1), ["a", "b"]);
  assert.equal(firstUnusedFallbackModel(["a", "b", "c"], ["a", "c"]), "b");
  assert.equal(firstUnusedFallbackModel(["a"], ["a"]), null);
});

test("automation durations keep a seconds payload while allowing manual units", () => {
  assert.equal(preferredAutomationDurationUnit("30"), "second");
  assert.equal(preferredAutomationDurationUnit("900"), "minute");
  assert.equal(preferredAutomationDurationUnit("3600"), "hour");
  assert.equal(automationDurationDisplayValue("90", "minute"), "1.5");
  assert.equal(automationDurationDisplayValue("3600", "hour"), "1");
  assert.equal(automationDurationDisplayValue("30", "hour"), "0.008333");
  assert.equal(automationDurationSecondsValue("1.5", "minute"), "90");
  assert.equal(automationDurationSecondsValue("0.5", "hour"), "1800");

  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const accountSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const typesSource = readFileSync(new URL("../src/types.ts", import.meta.url), "utf8");

  assert.ok((appSource.match(/<AutomationSettingDuration/g) || []).length >= 6);
  assert.match(appSource, /min=\{Number\(automationDurationDisplayValue\(String\(minSeconds\), unit\)\)\}/);
  assert.match(appSource, /max=\{Number\(automationDurationDisplayValue\(String\(maxSeconds\), unit\)\)\}/);
  assert.match(appSource, /channel_monitor_test_attempt_interval_seconds: channelMonitorTestAttemptIntervalNumber/);
  assert.match(appSource, /channelMonitorTestAttemptIntervalNumber > 300/);
  assert.match(appSource, /多次测试间隔/);
  assert.equal((typesSource.match(/channel_monitor_test_attempt_interval_seconds/g) || []).length, 2);
  assert.match(styles, /\.automation-setting-duration\s*\{[^}]*grid-template-columns:/s);
  assert.match(styles, /\.automation-settings-table\s*\{[^}]*display: grid;[^}]*gap: 9px;/s);
  assert.match(styles, /@media \(max-width: 560px\)[\s\S]*?\.automation-setting-row\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\);/s);

  assert.doesNotMatch(appSource, /autoComplete="new-password"\s+disabled=\{clearDiscordBotToken\}/);
  assert.match(appSource, /if \(token\) setClearDiscordBotToken\(false\)/);
  assert.match(appSource, /setDiscordNotificationsEnabled\(false\)/);
  assert.match(appSource, /if \(clearDiscordBotToken\) \{[\s\S]*?\} else if \(discordBotToken\.trim\(\)\)/);

  assert.doesNotMatch(accountSource, /failedMonitorStatuses = \[[^\]]*degraded/);
  assert.match(accountSource, /\? "monitor-degraded"/);
  assert.match(accountSource, /当前探测可用（降级）/);
  assert.doesNotMatch(accountSource, /status === "degraded"[\s\S]{0,120}当前探测不可用/);
  assert.match(accountSource, /const available = status === "available" \|\| monitorDegraded/);
  assert.match(accountSource, /绑定监控面板判定（降级，视为可用）/);
  assert.match(accountSource, /监控面板当前为降级，按可用处理，不进行回退模型测试/);
  assert.match(appSource, /面板状态为可用或降级时都直接判定账号可用，不进行回退模型测试/);
  assert.match(styles, /\.api-key-availability-indicator--monitor-degraded\s*\{[^}]*var\(--warn-bg\)[^}]*var\(--warn-ink\)/s);
  assert.match(accountSource, /disabled=\{busy \|\| identityBlocked\}\s+onClick=\{onForceConnectionTest\}/);
  assert.match(accountSource, /未绑定可用性监控面板，将使用账号白名单内的回退模型测试连接/);
  assert.match(accountSource, /api-key-scheduling-log-panel/);
  assert.match(styles, /\.api-key-scheduling-log-panel \.api-key-ledger-sticky\s*\{\s*position: static;/);
  assert.match(styles, /\.workspace--api-keys \.topbar h1\s*\{[^}]*font-size: 21px;/s);
  assert.match(styles, /@media \(max-width: 760px\)[\s\S]*?\.api-key-subview-tabs\s*\{[^}]*display: flex;[^}]*overflow-x: auto;/s);
  assert.match(styles, /\.api-key-rate-pause-toggle \.settings-toggle-copy\s*\{[^}]*display: grid;/s);
  assert.doesNotMatch(styles, /\.api-key-rate-pause-toggle\s*\{[^}]*min-height:\s*0;/s);
  assert.match(accountSource, /综合倍率严格大于阈值时暂停，等于或低于阈值时不暂停/);
});

test("API key operation feedback uses the title bar and upstream cards fill four desktop columns", () => {
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const accountSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(appSource, /onNotice=\{setNotice\}/);
  assert.match(accountSource, /const setNotice = onNotice/);
  assert.doesNotMatch(accountSource, /<Feedback tone="success"/);
  assert.match(styles, /\.api-key-channel-grid\s*\{[^}]*grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);[^}]*justify-content: stretch;[^}]*width: 100%;/s);
  assert.match(styles, /@media \(max-width: 1180px\)[\s\S]*?\.api-key-channel-grid\s*\{[^}]*grid-template-columns: repeat\(3, minmax\(0, 1fr\)\);/s);
  assert.match(styles, /@media \(max-width: 900px\)[\s\S]*?\.api-key-channel-grid\s*\{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/s);
  assert.doesNotMatch(styles, /\.api-key-channel-grid\s*\{[^}]*grid-template-columns: repeat\(auto-fit,/s);
});

test("API key change pages expose separate ledgers and unread highlighting", () => {
  assert.equal(apiKeySubviewPaths["rate-log"], "/api-keys/upstream-changes");
  assert.equal(apiKeySubviewPaths["schedule-log"], "/api-keys/scheduling-changes");
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(source, /记录上游充值倍率、分组倍率、名称与可用性，以及 API Key 账号实际倍率变化/);
  assert.match(source, /记录插件根据余额、渠道监控、上游可用性和综合倍率策略执行的暂停、恢复及失败/);
  assert.match(source, /markUpstreamChannelChangesRead/);
  assert.match(source, /markAccountSchedulingChangesRead/);
  assert.match(source, /departedChangeLogSubview\(previousSubview, subview\)/);
  assert.match(source, /changeLogUnreadRefreshIntervalMs = 12_000/);
  assert.match(source, /document\.addEventListener\("visibilitychange", refreshWhenVisible\)/);
  assert.match(source, /item\.unread && item\.id <= throughId \? \{ \.\.\.item, unread: false \} : item/);
  assert.match(source, /api-key-unread-chip/);
  assert.match(source, /className="api-key-change-identity-head"/);
  assert.match(source, /<time className="api-key-ledger-time"[\s\S]*?api-key-change-category/s);
  assert.match(source, /className="api-key-change-identity-route"/);
  assert.match(source, /<span>上游 <b>[\s\S]*?<span>\{subjectLabel\}/s);
  assert.doesNotMatch(source, /<i aria-hidden="true">\|<\/i>/);
  assert.match(source, /api-key-change-message-line--primary">\s*<span>\{upstreamChannelChangeEventLabel\(log\.event_type\)\}<\/span>\s*<\/div>\s*<div className="api-key-change-message-line api-key-change-message-line--detail">\s*\{nameEvent \? \(\s*<div className="api-key-rate-log-flow">/s);
  assert.match(source, /\(nameEvent \|\| groupAddedEvent\) && groupMultiplierValue !== null/);
  assert.match(source, /className="api-key-change-message-line api-key-change-message-line--detail"/);
  assert.match(source, /api-key-change-event-row api-key-scheduling-event-row/);
  assert.match(source, /api-key-change-category--scheduling-\$\{statusTone\}/);
  assert.match(source, /<span>API Key 账号 <b>\{log\.account_name/);
  assert.match(source, /function schedulingEvidenceLabel/);
  assert.match(source, /账号连接测试：\$\{testStatus\}/);
  assert.match(source, /观测综合倍率/);
  assert.match(source, /阈值 \$\{formatSchedulingEvidenceNumber\(threshold\)\}/);
  assert.match(styles, /\.api-key-rate-log-row\.is-unread/);
  assert.match(styles, /\.api-key-change-event-row\s*\{[^}]*border-bottom: 4px solid[^}]*grid-template-columns: minmax\(300px, 1fr\) minmax\(360px, 1fr\);/s);
  assert.match(styles, /\.api-key-change-event-row \.api-key-rate-log-identity\s*\{[^}]*grid-template-rows: 22px 24px;[^}]*min-height: 51px;/s);
  assert.match(styles, /\.api-key-change-event-row \.api-key-rate-log-cell--primary\s*\{[^}]*grid-template-rows: repeat\(2, minmax\(22px, 1fr\)\);/s);
  assert.match(styles, /\.api-key-scheduling-event-row\s*\{[^}]*border-bottom: 4px solid/s);
  assert.match(styles, /\.api-key-scheduling-event-row--paused\s*\{[^}]*border-left-color: var\(--danger-ink\)/s);
  assert.match(styles, /\.api-key-change-category--scheduling-restored\s*\{[^}]*var\(--ok-bg\)/s);
  assert.match(styles, /\.api-key-scheduling-evidence/);
});

test("change ledger badges clear immediately for the open subview", () => {
  assert.deepEqual(CHANGE_LOG_READ_RETRY_DELAYS_MS, [1_000, 2_000, 4_000]);
  assert.equal(pendingReadThroughId(null, [
    { id: 11, unread: false },
    { id: 12, unread: true },
    { id: 10, unread: true },
  ]), 12);
  assert.equal(pendingReadThroughId(12, [
    { id: 13, unread: false },
    { id: 14, unread: true },
  ]), 14);
  assert.equal(pendingReadThroughId(null, [{ id: 20, unread: false }]), null);

  assert.equal(departedChangeLogSubview("accounts", "rate-log"), null);
  assert.equal(departedChangeLogSubview("rate-log", "rate-log"), null);
  assert.equal(departedChangeLogSubview("rate-log", "schedule-log"), "rate-log");
  assert.equal(departedChangeLogSubview("schedule-log", "accounts"), "schedule-log");

  const unreadCounts = {
    upstream_changes: 3,
    account_rate_changes: 2,
    account_scheduling_changes: 1,
  };
  assert.deepEqual(visibleChangeLogUnreadCounts(unreadCounts, "rate-log"), {
    ...unreadCounts,
    upstream_changes: 0,
  });
  assert.deepEqual(visibleChangeLogUnreadCounts(unreadCounts, "schedule-log"), {
    ...unreadCounts,
    account_scheduling_changes: 0,
  });
  assert.deepEqual(visibleChangeLogUnreadCounts(unreadCounts, "account-rate-log"), {
    ...unreadCounts,
    account_rate_changes: 0,
  });
  assert.deepEqual(visibleChangeLogUnreadCounts(unreadCounts, "accounts"), unreadCounts);

  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(source, /previousSubviewRef\.current !== "rate-log"/);
  assert.match(source, /previousSubviewRef\.current !== "schedule-log"/);
  assert.match(source, /rateLogsRequestSequence\.current \+= 1/);
  assert.match(source, /scheduleLogsRequestSequence\.current \+= 1/);
  assert.match(source, /CHANGE_LOG_READ_RETRY_DELAYS_MS\[retryAttempt\]/);
  assert.match(source, /const nextRoute = routeFromPath\(window\.location\.pathname\)/);
  assert.match(source, /const stillViewingSameLog = nextRoute\.view === "api-keys"/);
  assert.match(source, /visibleChangeLogUnreadCounts\(changeLogUnreadCounts, subview\)/);
  assert.match(source, /await refreshChangeLogUnreadCounts\(\);/);
  assert.match(source, /await api\.discoverUpstreamChannel\(channel\.id\);[\s\S]*?await refreshChangeLogUnreadCounts\(\);/);
  assert.match(source, /const result = await api\.syncApiKeyAccounts[\s\S]*?await refreshChangeLogUnreadCounts\(\);/);
  assert.match(source, /await api\.discoverUpstreamChannel\(channel\.id\);[\s\S]*?await refreshChangeLogUnreadCounts\(\);/);
  assert.match(source, /const result = await api\.syncApiKeyAccounts[\s\S]*?await refreshChangeLogUnreadCounts\(\);/);
  assert.match(source, /useLayoutEffect\(\(\) => \{\s*componentMountedRef\.current = true;/);
  assert.doesNotMatch(source, /unmountReadTimerRef/);
});

test("account table sorting supports names and imported timestamps in both directions", () => {
  const accounts: UpstreamAccount[] = [
    {
      account_name: "Charlie",
      duplicate_rank: 0,
      email: "charlie@example.com",
      sub2api_account_id: "3",
      sub2api_imported_at: null,
    },
    {
      account_name: "alice",
      duplicate_rank: 0,
      email: "alice@example.com",
      sub2api_account_id: "1",
      sub2api_imported_at: "2026-07-18T08:00:00Z",
    },
    {
      account_name: "Bravo",
      duplicate_rank: 0,
      email: "bravo@example.com",
      sub2api_account_id: "2",
      sub2api_imported_at: "2026-07-17T08:00:00Z",
    },
  ] as Account[];

  assert.deepEqual(sortAccountsForTable(accounts, "account", "asc").map((account) => account.sub2api_account_id), ["1", "2", "3"]);
  assert.deepEqual(sortAccountsForTable(accounts, "account", "desc").map((account) => account.sub2api_account_id), ["3", "2", "1"]);
  assert.deepEqual(sortAccountsForTable(accounts, "imported_at", "asc").map((account) => account.sub2api_account_id), ["2", "1", "3"]);
  assert.deepEqual(sortAccountsForTable(accounts, "imported_at", "desc").map((account) => account.sub2api_account_id), ["1", "2", "3"]);
  assert.deepEqual(accounts.map((account) => account.sub2api_account_id), ["3", "1", "2"]);
});

test("new account and upstream controls are present without exposing Sub2API-only NewAPI fields", () => {
  const accountSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const helpSource = readFileSync(new URL("../src/HelpPopover.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(accountSource, /<option value="enabled">已启用<\/option>/);
  assert.match(accountSource, /<option value="disabled">已停用<\/option>/);
  assert.match(accountSource, /<UpstreamBalanceSummary channels=\{assignedChannels\} \/>/);
  assert.match(accountSource, /<span>渠道状态 \{channel\.channel_monitor_count/);
  assert.match(accountSource, /<span>今日使用<\/span>/);
  assert.match(accountSource, /<section aria-label="上游余额" className="api-key-balance-summary">/);
  assert.match(accountSource, /<div className="api-key-balance-summary-head">\s*<span>上游余额<\/span>/s);
  assert.doesNotMatch(accountSource, /上游渠道余额/);
  assert.match(accountSource, /<option value="inherit">/);
  assert.match(accountSource, /priority_assignment_when_disabled = accountForm\.priorityAssignmentWhenDisabled === "inherit"\s*\? null/s);
  assert.doesNotMatch(accountSource, /today_upstream_usage_amount \?\? account\.upstream_usage_amount/);
  assert.match(accountSource, /local_sub2api_today_cost_converted: "本站今日用量换算"/);
  assert.match(accountSource, /channelForm\.upstreamType === "auto"\s*\? editingChannel\?\.resolved_upstream_type \|\| "auto"/s);
  assert.match(accountSource, /editingChannelType !== "sub2api" \? <label className="api-key-field">\s*<span>NewAPI 用户 ID（余额探测）<\/span>/s);
  assert.match(accountSource, /<span>API 地址<\/span>/);
  assert.match(accountSource, /<UpstreamBalanceCard channel=\{channel\} key=\{String\(channel\.id\)\} \/>/);
  assert.match(accountSource, /const configuredName = channel\.display_name\?\.trim\(\) \|\| "";/);
  assert.match(accountSource, /className="api-key-balance-channel-name api-key-balance-channel-name--link"/);
  assert.match(accountSource, /urlLikeDisplayName\(displayName, managementUrl\)/);
  assert.match(accountSource, /isUrlLabel=\{isUrlLabel\}/);
  assert.match(accountSource, /isUrlDisplayName \? <MiddleEllipsisText text=\{displayName\} \/> : displayName/);
  assert.match(accountSource, /function BalanceManagementLink[\s\S]*href=\{url\}/);
  assert.match(accountSource, /const balanceNote = `\$\{platformBalanceNote\}；\$\{adjustedBalanceNote\}`/);
  assert.match(
    accountSource,
    /<div aria-label=\{balanceNote\} className="api-key-balance-metric" title=\{balanceNote\}>\s*<small>余额<\/small>\s*<strong><span>原 \{platformBalance\}<\/span><span>综 \{adjustedBalance\}<\/span><\/strong>/s,
  );
  assert.doesNotMatch(accountSource, /className="api-key-balance-values"/);
  assert.match(styles, /\.api-key-balance-summary\s*\{[^}]*grid-column: 1 \/ -1;/s);
  assert.match(styles, /\.api-key-balance-summary\s*\{[^}]*background: var\(--panel\);[^}]*box-shadow: var\(--shadow\);/s);
  assert.match(styles, /\.api-key-balance-summary-list\s*\{[^}]*grid-template-columns: repeat\(auto-fill, minmax\(min\(100%, 146px\), 146px\)\);/s);
  assert.match(styles, /\.api-key-balance-channel-card\s*\{[^}]*grid-template-rows: auto auto auto;/s);
  assert.match(styles, /\.api-key-balance-metric\s*\{[^}]*display: grid;[^}]*gap: 3px;/s);
  assert.match(styles, /\.api-key-dialog-account-grid\s*\{[^}]*height: 100%;[^}]*max-height: 100%;[^}]*overflow-y: auto;/s);
  const openMonitor = accountSource.match(/const openChannelMonitors = \(channel: UpstreamChannel\) => \{[\s\S]*?\n  \};/)?.[0] || "";
  assert.ok(openMonitor);
  assert.doesNotMatch(openMonitor, /refreshChannelMonitors/);
  assert.match(accountSource, /onRefresh=\{\(\) => void refreshChannelMonitors\(channelMonitorDialog\)\}/);
  assert.doesNotMatch(accountSource, /监控失败时测试模型/);
  assert.doesNotMatch(accountSource, /saveChannelMonitorTestModels/);
  assert.match(accountSource, /请选择具体监控面板/);
  assert.match(accountSource, /面板可用时不直连账号/);
  assert.match(accountSource, /白名单尚未同步，禁止执行测试/);
  assert.match(accountSource, /editingAccountModels\.map/);
  assert.match(accountSource, /<AccountAvailabilityIndicator\s+account=\{account\}/);
  assert.match(accountSource, /api-key-monitor-card-status-row/);

  assert.match(appSource, /api_key_auto_pause_on_negative_balance_enabled: apiKeyNegativeBalancePauseEnabled/);
  assert.match(appSource, /api_key_auto_pause_on_channel_monitor_unavailable_enabled: apiKeyChannelMonitorPauseEnabled/);
  assert.match(appSource, /channel_monitor_auto_probe_enabled: channelMonitorAutoProbeEnabled/);
  assert.match(appSource, /account_model_whitelist_sync_enabled: accountModelWhitelistSyncEnabled/);
  assert.match(appSource, /channel_monitor_fallback_test_attempts: Math\.max/);
  assert.match(appSource, /channel_monitor_recovery_test_attempts: Math\.max/);
  assert.doesNotMatch(appSource, /channel_monitor_unavailable_consecutive_threshold: channelMonitorUnavailableThresholdNumber/);
  assert.doesNotMatch(appSource, /channel_monitor_recovery_consecutive_threshold: channelMonitorRecoveryThresholdNumber/);
  assert.doesNotMatch(appSource, /api_key_auto_pause_on_upstream_rate_increase_enabled:/);
  assert.doesNotMatch(appSource, /upstream_rate_increase_threshold_percent:/);
  assert.match(accountSource, /payload\.rate_pause_policy = accountForm\.ratePausePolicy/);
  assert.match(accountSource, /rate_pause_effective_source === "account"/);
  assert.match(appSource, /仅处理已绑定具体监控面板或已启用独立模型测试的账号/);
  assert.match(appSource, /没有可用回退模型时不会据此暂停账号/);
  assert.match(appSource, /自动探测上游渠道监控/);
  assert.match(appSource, /自动刷新账号可用模型白名单/);
  assert.match(appSource, /settings\.available_test_models/);
  assert.match(accountSource, /账号独立阈值优先于优先级区间；回落到阈值后自动恢复/);
  assert.match(appSource, /channelHasLowBalance\(\s*channel,\s*showStaleNegativeBalanceAlert,\s*balanceBasis,\s*balanceThreshold/s);
  assert.match(appSource, /show_stale_negative_balance_alert: showStaleNegativeBalanceAlert/);
  assert.match(appSource, /aria-label="关闭提示"/);
  assert.match(appSource, /onClick=\{\(\) => setNotice\(""\)\}/);
  assert.match(appSource, /settings-local-nav/);
  assert.match(appSource, /id="settings-connection"/);
  assert.match(appSource, /id="settings-notifications"/);
  assert.match(appSource, /上游 Key \/ 分组不可用时自动停用 API Key 账号/);
  assert.match(appSource, /上游恢复后，仅自动恢复由本插件暂停的账号/);
  assert.match(appSource, /余额达到或高于阈值且其他暂停原因均解除后自动恢复/);
  assert.match(appSource, /停用的 API Key 账号也参与优先级分配/);
  assert.doesNotMatch(appSource, /checked=\{priorityAssignDisabledAccounts\}\s+disabled=/);
  assert.match(appSource, /同综合倍率账号使用相同优先级/);
  assert.match(appSource, /priority_share_same_composite_multiplier: priorityShareSameCompositeMultiplier/);
  assert.match(appSource, /shareSameCompositePriority=\{settings\.priority_share_same_composite_multiplier \?\? false\}/);
  assert.match(accountSource, /shareSameCompositePriority \? new Map\(\) : priorityTieMoveOptions\(allAccounts\)/);
  assert.match(accountSource, /相同倍率账号共用一个优先级/);
  assert.match(accountSource, /\.map\(priorityTieMultiplierKey\)/);
  assert.doesNotMatch(accountSource, /最低优先级间隔/);
  assert.match(accountSource, /form\.allocationStrategy === "cost_optimized" \? 1 : Number\(form\.step\)/);
  assert.doesNotMatch(accountSource, /最低优先级间隔/);
  assert.match(accountSource, /form\.allocationStrategy === "cost_optimized" \? 1 : Number\(form\.step\)/);
  assert.match(appSource, /继续按当前口径和阈值展示上次成功余额；不会使用过期余额暂停账号/);
  assert.match(appSource, /channel\.balance_source !== "upstream_wallet"/);
  assert.match(appSource, /!channel\.balance_checked_at/);
  assert.match(appSource, /priority_assign_disabled_api_key_accounts: priorityAssignDisabledAccounts/);
  assert.match(appSource, /discord_bot_notifications_enabled: discordNotificationsEnabled/);
  assert.match(appSource, /notify_oauth_account_disabled: notifyAccountScheduling/);
  assert.match(appSource, /notify_account_enabled: notifyAccountScheduling/);
  assert.match(appSource, /notify_upstream_token_invalid: notifyUpstreamTokenInvalid/);
  assert.match(appSource, />账号调度<\/span>/);
  assert.match(appSource, />倍率变化<\/span>/);
  assert.doesNotMatch(appSource, /账号停用（OAuth \/ API Key）/);
  assert.doesNotMatch(appSource, /账号启用（OAuth \/ API Key）/);
  assert.match(appSource, /上游分组变化/);
  assert.match(appSource, />上游令牌失效<\/span>/);
  assert.match(appSource, /onTestNotification=\{\(\) => runAction\(api\.testNotification/);
  assert.match(appSource, />发送测试通知<\/span>/);
  assert.match(appSource, /payload\.clear_discord_bot_token = true/);
  assert.match(appSource, /onClick=\{\(\) => onLocateAccount\(account\)\}/);
  assert.match(appSource, /await api\.updateSiteLogo\(branding\.logoFile\)/);
  assert.match(appSource, /file\.size > 1024 \* 1024/);
  assert.match(appSource, /其他设置已保存，但 Logo 更新失败/);
  assert.match(accountSource, /refreshUpstreamChannelMonitors/);
  assert.match(accountSource, /当前探测可用/);
  assert.match(accountSource, /model\.name \|\| model\.model/);
  assert.match(accountSource, /point\.time \|\| point\.checked_at/);
  assert.match(accountSource, /account\.active_pause_holds !== undefined/);
  assert.doesNotMatch(accountSource, /visiblePauseHolds\.map\(\(hold, index\) =>/);
  assert.match(accountSource, /pauseHoldReasonLabel\(hold\)/);
  assert.match(accountSource, /<AccountStatusIndicator/);
  assert.match(accountSource, /查看 \$\{accountDisplayName\(account\)\} 的停用详情/);
  assert.match(accountSource, /查看 \$\{accountDisplayName\(account\)\} 的可用性监测详情/);
  assert.match(accountSource, /监控面板未能确认可用，随后由回退连接测试判定/);
  assert.match(accountSource, /为节省测试 token，暂不进行可用性测试/);
  assert.match(accountSource, /\["当前回退候选模型", source === "channel_monitor_fallback" && !monitorDegraded \? chosenModel : null\]/);
  assert.doesNotMatch(accountSource, /监控面板未能确认可用后连接测试判定/);
  assert.match(accountSource, /\["配置阈值", evidence\.threshold/);
  assert.match(helpSource, /trigger\?: ReactNode/);
  assert.doesNotMatch(accountSource, /不具备自动恢复资格/);
  assert.doesNotMatch(accountSource, />可自动恢复</);
  assert.match(styles, /\.settings-local-nav\s*\{/);
  assert.match(styles, /\.settings-auto-pause-thresholds\s*\{/);
  assert.match(appSource, /new ResizeObserver\(updateLayoutOffsets\)/);
  assert.match(styles, /\.topbar\s*\{[\s\S]*?position:\s*sticky;/);
  assert.match(styles, /\.api-key-subview-tabs\s*\{[\s\S]*?position:\s*sticky;/);
  assert.match(styles, /\.settings-local-nav\s*\{[\s\S]*?top:\s*calc\(var\(--app-sidebar-offset\) \+ var\(--app-header-height\)\);/);
  assert.match(styles, /@media \(max-width: 760px\)[\s\S]*?\.settings-local-nav\s*\{\s*top:\s*calc\(var\(--app-sidebar-offset\) \+ var\(--app-header-height\)\);/);
  assert.match(styles, /\.notice > button\s*\{/);
  assert.match(styles, /\.api-key-monitor-current--success/);
  assert.match(styles, /\.api-key-channel-card\s*\{\s*height: auto;\s*min-height: 371px;/);
  assert.match(styles, /@media \(max-width: 680px\)[\s\S]*?\.api-key-channel-card\s*\{\s*height: auto;\s*min-height: 440px;/);
  assert.doesNotMatch(styles, /\.api-key-channel-accounts\s*\{[^}]*margin-top:\s*auto;/s);
  assert.match(styles, /\.api-key-channel-grid\s*\{[^}]*grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/s);
  assert.match(styles, /@media \(max-width: 760px\)[\s\S]*?\.api-key-channel-grid\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\);[^}]*max-width: 360px;[^}]*width: 100%;/s);
  assert.doesNotMatch(styles, /api-key-channel-card--groups-expanded/);
  assert.match(accountSource, /trigger=\{<span>原<\/span>\}/);
  assert.match(accountSource, /trigger=\{<span>综<\/span>\}/);
  assert.match(accountSource, /上游钱包原始余额，直接读取自上游站点的钱包余额/);
  assert.match(accountSource, /综合余额等于上游钱包原始余额乘以上游充值倍率/);
  assert.match(accountSource, /onClick=\{onShowGroups\}/);
  assert.match(accountSource, /<ChannelGroupList channel=\{channelGroupDialog\} \/>/);
  assert.match(styles, /\.api-key-group-dialog-list\s*\{[^}]*grid-template-columns: repeat\(auto-fill, minmax\(min\(100%, 210px\), 240px\)\);/s);
  assert.doesNotMatch(accountSource, /api-key-account-rate-pause-tags/);
  assert.doesNotMatch(accountSource, /api-key-rate-pause-tag--independent/);
  assert.doesNotMatch(accountSource, /api-key-rate-pause-tag--inherited/);
  assert.doesNotMatch(accountSource, /api-key-rate-pause-tag--paused/);
  assert.doesNotMatch(styles, /\.api-key-account-rate-pause-tags/);
  assert.doesNotMatch(styles, /\.api-key-account-rate-pause-summary/);
  assert.match(accountSource, /onTestAvailability=\{\(\) => void testAccountAvailability\(entry\.account\)\}/);
  assert.match(accountSource, /onForceConnectionTest=\{\(\) => void forceAccountConnectionTest\(entry\.account\)\}/);
  assert.match(accountSource, /<PlugZap size=\{15\} \/>/);
  assert.doesNotMatch(accountSource, /onTestAvailability=\{\(\) => \{\s*closeDialog\(\);/);
  assert.doesNotMatch(accountSource, /onDelete=\{\(\) => \{\s*closeDialog\(\);/);
  assert.doesNotMatch(accountSource, /onPriorityIntervalChange=\{\(intervalId\) => \{\s*closeDialog\(\);/);
  assert.doesNotMatch(accountSource, /onToggle=\{\(\) => \{\s*closeDialog\(\);/);
  assert.doesNotMatch(accountSource, /onDiscover=\{\(\) => \{\s*closeDialog\(\);/);
  assert.match(accountSource, /availabilityTestPromisesRef = useRef<Map<string, Promise<UpstreamAccount \| null>>>/);
  assert.match(accountSource, /await pendingAvailabilityTest/);
  assert.match(accountSource, /const latestAccount = flattenUpstreamAccounts\(latestData\)[\s\S]*?\?\.account \|\| testedAccount \|\| submittedAccount;/);
  assert.match(accountSource, /mergeUpstreamAccountSnapshot\(dataRef\.current, result\.account\)/);
  assert.match(accountSource, /return \{ \.\.\.account, \.\.\.snapshot \};/);
  assert.match(accountSource, /const currentData = dataRef\.current;[\s\S]*?channels: currentData\.channels\.map/);
  assert.match(accountSource, /if \(!focusable\.length\) \{\s*event\.preventDefault\(\);\s*dialog\.focus\(\);/);
  assert.match(accountSource, /savingLabel=\{accountSaveWaitingForTest \? "等待检测完成" : undefined\}/);
  assert.match(accountSource, /disabled=\{\["availability-test", "connection-test"\]\.includes\(busyAction \|\| ""\) \? false : busy\}/);
  assert.match(accountSource, /<fieldset className="api-key-config-fields api-key-config-fieldset" disabled=\{savingDialog\}>/);
  assert.match(appSource, /dialogRef\.current\.querySelectorAll<HTMLElement>/);
  assert.match(appSource, /fallbackModelDialogTriggerRef\.current\?\.focus\(\)/);
  assert.doesNotMatch(styles, /api-key-rate-pause-tag--inherited/);
  assert.doesNotMatch(accountSource, /className="api-key-scheduling-columns"/);
  assert.match(accountSource, /暂停原因详情/);
  assert.doesNotMatch(accountSource, />同时生效<\/span>/);
});

test("priority intervals can open account management with the interval filter applied", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(source, /const viewPriorityIntervalAccounts = useCallback\(\(interval: PriorityInterval\) =>/);
  assert.match(source, /setAccountSearch\(""\);[\s\S]*setAccountStatusFilter\("all"\);[\s\S]*setAccountUpstreamFilter\("all"\);[\s\S]*setPlatformFilter\("all"\);/);
  assert.match(source, /setPriorityIntervalFilter\(String\(interval\.id\)\);\s*onSubviewChange\("accounts"\);/);
  assert.match(source, /aria-label=\{`查看优先级区间 \$\{interval\.name\} 的账号`\}/);
  assert.match(source, /onViewAccounts=\{viewPriorityIntervalAccounts\}/);
});

test("channel monitor current status uses the newest timestamp rather than response order", () => {
  assert.equal(
    latestChannelMonitorStatus("degraded", [
      { checked_at: "2026-07-20T10:00:00Z", status: "available" },
      { checked_at: "2026-07-20T08:00:00Z", status: "unavailable" },
      { checked_at: "2026-07-20T09:00:00Z", status: "timeout" },
    ]),
    "available",
  );
  assert.equal(
    latestChannelMonitorStatus("healthy", [
      { checked_at: null, status: "unavailable" },
    ]),
    "healthy",
  );
});

test("channel monitor timeline keeps the newest 60 records in past-to-now order", () => {
  const timeline = Array.from({ length: 75 }, (_, index) => ({
    checked_at: new Date(Date.UTC(2026, 6, 20, 0, index)).toISOString(),
    status: index % 2 ? "available" : "error",
  })).reverse();
  const recent = recentChannelMonitorTimeline(timeline);

  assert.equal(recent.length, CHANNEL_MONITOR_TIMELINE_LIMIT);
  assert.equal(recent[0]?.checked_at, "2026-07-20T00:15:00.000Z");
  assert.equal(recent[59]?.checked_at, "2026-07-20T01:14:00.000Z");
});

test("overview balance alert dismissal persists across page reloads", () => {
  const storage = new MemoryStorage();
  assert.equal(readOverviewBalanceAlertDismissed(storage), false);

  persistOverviewBalanceAlertDismissed(storage);

  assert.equal(readOverviewBalanceAlertDismissed(storage), true);
});

test("change log cache restores, merges, and marks records read without crossing scopes", () => {
  const storage = new MemoryStorage();
  const key = changeLogCacheKey(
    "https://sub2api.example.com/",
    "upstream",
    "2026-07-01",
    "",
    "Asia/Shanghai",
  );
  const accountRateKey = changeLogCacheKey(
    "https://sub2api.example.com/",
    "account_rate",
    "2026-07-01",
    "",
    "Asia/Shanghai",
  );
  assert.notEqual(key, accountRateKey);

  const older = {
    id: 10,
    channel_id: 1,
    event_type: "group_added" as const,
    created_at: "2026-07-20T01:00:00Z",
    unread: true,
  };
  const newer = {
    id: 11,
    channel_id: 1,
    event_type: "group_removed" as const,
    created_at: "2026-07-20T02:00:00Z",
    unread: true,
  };
  writeChangeLogCache(storage, key, {
    items: [older],
    hasMore: false,
    unreadCount: 1,
    lastReadId: 0,
  });
  const merged = mergeChangeLogItems([older], [newer, { ...older, unread: false }]);
  assert.deepEqual(merged.map((item) => item.id), [11, 10]);
  assert.equal(merged[1].unread, false);
  writeChangeLogCache(storage, key, {
    items: merged,
    hasMore: false,
    unreadCount: 2,
    lastReadId: 0,
  });

  clearChangeLogMemoryCache();
  const restored = readChangeLogCache(storage, key);
  assert.deepEqual(restored?.items.map((item) => item.id), [11, 10]);
  assert.equal(readChangeLogCache(storage, accountRateKey), null);

  markChangeLogCacheRead(storage, key, 11);
  clearChangeLogMemoryCache();
  const marked = readChangeLogCache(storage, key);
  assert.equal(marked?.unreadCount, 0);
  assert.equal(marked?.lastReadId, 11);
  assert.equal(marked?.items.every((item) => !item.unread), true);
});

test("change log cache clearing removes persisted and in-memory entries only", () => {
  const storage = new MemoryStorage();
  const key = changeLogCacheKey(
    "https://sub2api.example.com/",
    "upstream",
    "",
    "",
    "Asia/Shanghai",
  );
  storage.setItem("unrelated", "keep");
  writeChangeLogCache(storage, key, {
    items: [{
      id: 12,
      channel_id: 1,
      event_type: "group_added",
      created_at: "2026-07-20T03:00:00Z",
      unread: true,
    }],
    hasMore: false,
    unreadCount: 1,
    lastReadId: 0,
  });

  clearChangeLogCache(storage);

  assert.equal(storage.getItem("unrelated"), "keep");
  assert.equal(storage.getItem(key), null);
  assert.equal(readChangeLogCache(storage, key), null);
  assert.doesNotThrow(() => clearChangeLogCache(null));
});

test("API key view warms default change-log caches before a record subview is opened", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(source, /const warmDefaultChangeLogCaches = useCallback/);
  assert.match(source, /Promise\.allSettled\(tasks\)/);
  assert.match(source, /subview !== "rate-log"/);
  assert.match(source, /subview !== "account-rate-log"/);
  assert.match(source, /subview !== "schedule-log"/);
  assert.match(source, /void warmDefaultChangeLogCaches\(\)/);
});

test("subscription refresh concurrency preserves zero as unlimited", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /String\(settings\.subscription_refresh_max_concurrency \?\? 3\)/);
  assert.match(source, /subscriptionRefreshMaxConcurrencyNumber < 0/);
  assert.match(
    source,
    /max=\{20\}\s+min=\{0\}\s+onChange=\{\(event\) => setSubscriptionRefreshMaxConcurrency\(event\.target\.value\)\}/s,
  );
  assert.match(
    source,
    /subscription_refresh_max_concurrency: subscriptionRefreshMaxConcurrencyNumber/,
  );
});

test("subscription presentation keeps seat-based K12 plans visible", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /seatBasedSubscriptionTypes = new Set\(\["team", "k12"/);
  assert.match(source, /return account\.has_active_subscription === false && !seatBasedSubscriptionTypes\.has\(subscriptionType\)/);
  assert.match(source, /return subscriptionIsInvalid\(account\) \? "订阅无效" : plan === "active" \? "正常" : plan/);
});

test("upstream channels expose occupied, no-enabled, and empty upstream filters", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(source, /useState<ChannelOccupancyFilter>\("occupied"\)/);
  assert.match(source, />有账号上游 \{occupiedChannels\.length\}<\/button>/);
  assert.match(source, />无启用上游 \{noEnabledChannels\.length\}<\/button>/);
  assert.match(source, />无账号上游 \{emptyChannels\.length\}<\/button>/);
  assert.match(source, /partitionUpstreamChannels\(data\.channels\)/);
  assert.match(source, /channelOccupancyFilter !== "occupied"/);
  assert.match(source, /上游未配置公开监控面板/);
  assert.match(source, /accountCount > 0 \? \(/);
  assert.match(source, /api\.deleteUpstreamChannel\(channel\.id\)/);
  assert.match(source, /title="删除空渠道"/);
});

test("upstream channel occupancy partitions are mutually exclusive", () => {
  const makeChannel = (
    id: number,
    accountCount: number,
    schedulingStates: boolean[],
  ) => ({
    id,
    account_count: accountCount,
    accounts: schedulingStates.map((remote_schedulable, index) => ({
      sub2api_account_id: `${id}-${index}`,
      remote_schedulable,
    })),
  }) as UpstreamChannel;
  const partitions = partitionUpstreamChannels([
    makeChannel(1, 2, [true, false]),
    makeChannel(2, 2, [false, false]),
    makeChannel(3, 0, []),
    makeChannel(4, 2, [false]),
  ]);

  assert.deepEqual(partitions.assigned.map((channel) => channel.id), [1, 2, 4]);
  assert.deepEqual(partitions.enabled.map((channel) => channel.id), [1, 4]);
  assert.deepEqual(partitions.noEnabled.map((channel) => channel.id), [2]);
  assert.deepEqual(partitions.empty.map((channel) => channel.id), [3]);
  assert.equal(new Set([
    ...partitions.enabled.map((channel) => channel.id),
    ...partitions.noEnabled.map((channel) => channel.id),
    ...partitions.empty.map((channel) => channel.id),
  ]).size, 4);
});

test("enabled account filtering excludes unknown scheduling state", () => {
  const account = (remote_schedulable: boolean | null) => ({
    remote_schedulable,
  }) as Parameters<typeof upstreamAccountMatchesStatus>[0];

  assert.equal(upstreamAccountMatchesStatus(account(true), "enabled"), true);
  assert.equal(upstreamAccountMatchesStatus(account(false), "enabled"), false);
  assert.equal(upstreamAccountMatchesStatus(account(null), "enabled"), false);
  assert.equal(upstreamAccountMatchesStatus(account(false), "disabled"), true);
  assert.equal(upstreamAccountMatchesStatus(account(null), "disabled"), false);
});

test("overview error count follows the live account rows", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(
    source,
    /const errorAccountCount = accounts\.filter\(\s*\(account\) => !account\.deactive && accountHasError\(account\),/s,
  );
  assert.match(source, /\{ label: "错误", value: errorAccountCount, icon: AlertTriangle, tone: "warn" \}/);
});

test("upstream balance presentation keeps card amounts readable and hides technical copy", () => {
  assert.equal(formatUpstreamBalance(1234.567, "USD", 2), "$1,234.57");
  assert.equal(formatUpstreamBalance(12, "CNY", 2), "¥12.00");
  assert.equal(formatUpstreamBalance(2.3456, "USDT"), "$2.3456 USDT");
  assert.equal(formatUpstreamBalance(null, "USD", 2), "—");
  assert.equal(visibleUpstreamBalanceMessage("Balance read from the NewAPI user account."), "");
  assert.equal(visibleUpstreamBalanceMessage("Balance read from the Sub2API user account."), "");
  assert.equal(visibleUpstreamBalanceMessage("上游余额读取失败"), "上游余额读取失败");
});

test("account usage is shown for Sub2API but omitted for NewAPI", () => {
  assert.equal(shouldShowUpstreamAccountUsage("sub2api"), true);
  assert.equal(shouldShowUpstreamAccountUsage("NEWAPI"), false);
  assert.equal(shouldShowUpstreamAccountUsage(null), true);
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(source, /account\.resolved_upstream_type \|\| account\.detected_upstream_type \|\| account\.upstream_type/);
  assert.match(source, /\{showUsage \? <div className="api-key-account-usage">/);
  assert.match(source, /resolvedChannelType\(channel\) !== "newapi" && finiteNumber\(channel\.balance_used\)/);
});

test("upstream channel cards keep URLs and daily usage compact", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const dailyBalanceFormatter = source.match(
    /function formatDailyBalanceUsed\([\s\S]*?\n\}/,
  )?.[0] || "";
  assert.match(source, /const siteUrl = configuredManagementUrl \|\| apiUrl/);
  assert.match(source, /<ChannelAddressBox label="站点" url=\{siteUrl\} \/>/);
  assert.match(source, /<ChannelAddressBox label="API" url=\{hasSeparateManagementUrl \? apiUrl : ""\} \/>/);
  assert.match(source, /<MiddleEllipsisText text=\{content\} \/>/);
  const ellipsisSource = readFileSync(new URL("../src/MiddleEllipsisText.tsx", import.meta.url), "utf8");
  assert.match(ellipsisSource, /const ELLIPSIS = "\.\.\."/);
  assert.match(ellipsisSource, /element\.clientWidth/);
  assert.match(source, /\[yesterdayUsage, todayUsage\]\.map/);
  assert.match(source, /className="api-key-channel-stat--recharge"/);
  assert.match(source, /<ChannelStat className="api-key-channel-stat--probe" icon=\{<Radar size=\{16\} \/>\} label="最近探测">/);
  assert.doesNotMatch(source, /api-key-channel-stat--probe" badge=/);
  assert.match(source, /const stale = status === "stale" && hasCurrentValue/);
  assert.match(source, /usage\.stale \? "（旧）" : ""/);
  assert.match(source, /current \|\| stale/);
  assert.match(source, /unsupported\s*\?\s*"muted"/);
  assert.match(styles, /\.api-key-channel-addresses\s*\{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/s);
  assert.match(styles, /\.api-key-channel-address\s*\{[^}]*overflow: hidden;[^}]*white-space: nowrap;/s);
  assert.match(styles, /\.api-key-group-chips\s*\{[^}]*max-height: 55px;[^}]*padding-bottom: 2px;/s);
  assert.match(styles, /\.api-key-channel-head\s*\{[^}]*background: color-mix[^}]*border-bottom: 1px solid var\(--line\);/s);
  assert.match(styles, /\.api-key-channel-grid\s*\{[^}]*grid-template-columns: repeat\(4, minmax\(0, 1fr\)\);/s);
  assert.match(styles, /\.api-key-channel-stats\s*\{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/s);
  assert.match(styles, /\.api-key-channel-stat--balance\s*\{[^}]*grid-column: 1 \/ -1;/s);
  assert.doesNotMatch(styles, /\.api-key-channel-stat:last-child\s*\{/);
  assert.match(styles, /\.api-key-channel-stats\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\) max-content max-content;/s);
  assert.match(styles, /\.api-key-channel-stat--balance\s*\{[^}]*grid-column: auto;/s);
  assert.match(styles, /\.api-key-channel-stat-value strong\s*\{[^}]*white-space: nowrap;/s);
  assert.doesNotMatch(styles, /\.api-key-channel-stat--probe \.api-key-channel-stat-value strong\s*\{[^}]*font-size:/s);
  assert.match(styles, /\.api-key-channel-stat--balance \.api-key-channel-balance-chip > b\s*\{[^}]*font-size: 11px;/s);
  assert.match(styles, /\.api-key-channel-daily-usage\s*\{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/s);
  assert.match(dailyBalanceFormatter, /value:\s*adjustedValue,/);
  assert.doesNotMatch(dailyBalanceFormatter, /value:\s*`原 /);
  assert.match(
    dailyBalanceFormatter,
    /detail:\s*`[^`]*原始 \$\{rawValue\}[^`]*综合（考虑充值倍率）\$\{adjustedValue\}[^`]*`/,
  );
  assert.match(source, /title=\{usage\.detail\}/);
  assert.match(source, /aria-label=\{usage\.detail\}/);
  assert.match(
    styles,
    /\.api-key-channel-accounts\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.05fr\)\s+minmax\(0,\s*0\.65fr\)\s+minmax\(0,\s*1\.3fr\);/s,
  );
  assert.doesNotMatch(
    styles,
    /@media \(max-width: 760px\)(?:(?!@media)[\s\S])*?\.api-key-channel-accounts \.api-key-channel-account-button:nth-child\(3\)\s*\{[^}]*grid-column:\s*1 \/ -1;/,
  );
  assert.match(
    styles,
    /@media \(max-width: 480px\)(?:(?!@media)[\s\S])*?\.api-key-channel-account-button\s*>\s*svg:last-child\s*\{[^}]*display:\s*none;/,
  );
});

test("API key dense views keep readable type and collapse change records on narrow screens", () => {
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(styles, /\.api-key-rate-log-row \.api-key-rate-log-identity > strong\s*\{[^}]*font-size: 14px;/s);
  assert.match(styles, /\.api-key-upstream-transition > span:first-child\s*\{[^}]*font-size: 11px;/s);
  assert.match(styles, /\.api-key-channel-stat-label\s*\{[^}]*font-size: 12px;/s);
  assert.match(styles, /\.api-key-account-group-label > span:first-child\s*\{[^}]*font-size: 12px;/s);
  assert.match(styles, /\.api-key-priority-card-stats span\s*\{[^}]*font-size: 11px;/s);
  assert.match(styles, /@media \(max-width: 520px\)\s*\{\s*\.api-key-rate-log-filters,\s*\.api-key-rate-log-row\s*\{\s*grid-template-columns: minmax\(0, 1fr\);/s);
});

test("help popover triggers stay below sticky ledger headers", () => {
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const accountSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(styles, /\.help-popover\s*\{[^}]*position:\s*relative;[^}]*\}/s);
  assert.doesNotMatch(styles, /\.help-popover\s*\{[^}]*z-index\s*:/s);
  assert.match(styles, /\.help-popover-content\s*\{[^}]*position:\s*fixed;[^}]*z-index:\s*100;/s);
  assert.match(styles, /\.api-key-ledger-sticky\s*\{[^}]*position:\s*sticky;[^}]*z-index:\s*22;/s);
  assert.match(accountSource, /evidence\.monitor_status \? upstreamStatusLabel/);
  assert.match(accountSource, /"account_availability_healthy"/);
});

test("API key account numbers stay beside the account name", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const nameIndex = source.indexOf('<div className="api-key-account-name">');
  const numberIndex = source.indexOf('<span className="api-key-mono">#{account.sub2api_account_id}</span>', nameIndex);
  const statusIndex = source.indexOf('<AccountStatusIndicator', nameIndex);
  assert.ok(nameIndex >= 0 && numberIndex > nameIndex && statusIndex > numberIndex);
  assert.match(source, /<div className="api-key-account-side-chips">\s*<AccountStatusIndicator/s);
  assert.match(styles, /\.api-key-account-name\s*\{[^}]*display: flex;[^}]*gap: 5px;/s);
  assert.match(styles, /\.api-key-account-name > strong\s*\{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;/s);
  assert.match(styles, /\.api-key-account-side-chips\s*\{[^}]*margin-left: auto;/s);
  assert.match(styles, /\.api-key-account-priority\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\);/s);
  assert.match(styles, /\.api-key-account-priority-value\s*\{[^}]*border-left: 0;[^}]*border-top: 1px solid var\(--line\);/s);
});

test("upstream API key identity conflicts require explicit confirmation", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(source, /latestAccount\.upstream_identity_rebind_required/);
  assert.match(source, /payload\.confirm_upstream_identity_rebind = true/);
  assert.match(source, /account\.identity_rebind_required\s*\|\|\s*account\.upstream_identity_rebind_required/);
  assert.match(source, /上游 Key 身份待确认/);
});

test("history duration helpers format totals, stages, and refresh jobs", () => {
  const details = {
    duration_ms: 1_904,
    account_list_duration_ms: 420,
    inventory_duration_ms: "1200",
    probe_duration_ms: -5,
  };
  assert.equal(eventDurationMs(details), 1_904);
  assert.equal(formatElapsedDuration(532), "532 ms");
  assert.equal(formatElapsedDuration(1_904), "1.90 秒");
  assert.equal(formatElapsedDuration(62_000), "1 分 02 秒");
  assert.deepEqual(
    eventDurationBreakdown(details).map(({ label, durationMs }) => [label, durationMs]),
    [["获取账号清单", 420], ["同步本地清单", 1_200], ["探测上游", 0]],
  );
  assert.equal(
    timestampDurationMs("2026-07-17T01:00:00Z", "2026-07-17T01:00:02.500Z"),
    2_500,
  );
  assert.equal(timestampDurationMs(null, null), null);
});

test("API key dialogs move, trap, and restore keyboard focus", () => {
  const source = readFileSync(
    new URL("../src/ApiKeyAccountsView.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /closeButtonRef\.current\?\.focus\(\)/);
  assert.match(source, /!dialog\.contains\(document\.activeElement\)/);
  assert.match(source, /\(event\.shiftKey \? last : first\)\.focus\(\)/);
  assert.match(source, /restoreTarget\?\.isConnected/);
  assert.match(source, /tabIndex=\{-1\}/);
});

test("usage samples switch between quota and recorded-time directions", () => {
  assert.deepEqual(sortUsageLimitSamples(usageSamples, "quota", "asc").map((sample) => sample.id), [1, 2]);
  assert.deepEqual(sortUsageLimitSamples(usageSamples, "quota", "desc").map((sample) => sample.id), [2, 1]);
  assert.deepEqual(sortUsageLimitSamples(usageSamples, "recorded_at", "asc").map((sample) => sample.id), [2, 1]);
  assert.deepEqual(sortUsageLimitSamples(usageSamples, "recorded_at", "desc").map((sample) => sample.id), [1, 2]);
});

test("long-running workflows rely on backend deadlines and explicit cancellation", () => {
  assert.equal(NO_FRONTEND_TIMEOUT, null);
});

test("liveness long requests have no timer but still honor explicit abort", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  let timerCalls = 0;
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout: ((...args: Parameters<typeof setTimeout>) => {
      timerCalls += 1;
      return setTimeout(...args);
    }) as typeof window.setTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = ((_input: string | URL | Request, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
  })) as typeof fetch;
  const controller = new AbortController();
  try {
    const request = api.testAccountLiveness(["1"], "gpt-test", controller.signal);
    controller.abort();
    await assert.rejects(request, (reason: unknown) => reason instanceof DOMException && reason.name === "AbortError");
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
  assert.equal(timerCalls, 0);
});

test("liveness selection accepts numeric OAuth accounts and rejects API-key accounts", () => {
  assert.equal(accountCanBeLivenessTested({
    account_type: "openai-oauth",
    platform: "openai",
    sub2api_account_id: "42",
  }), true);
  assert.equal(accountCanBeLivenessTested({
    account_type: "api_key",
    platform: "openai",
    sub2api_account_id: "42",
  }), false);
  assert.equal(accountCanBeLivenessTested({
    account_type: "oauth",
    platform: "openai",
    sub2api_account_id: "not-an-id",
  }), false);
});

test("liveness request ids are deduplicated and capped at the backend limit", () => {
  const accounts = Array.from({ length: MAX_LIVENESS_ACCOUNTS + 5 }, (_, index) => ({
    account_type: "oauth",
    platform: "openai",
    sub2api_account_id: String(index + 1),
  }));
  accounts.splice(2, 0, { ...accounts[0] });
  const ids = livenessAccountIds(accounts);
  assert.equal(ids.length, MAX_LIVENESS_ACCOUNTS);
  assert.equal(new Set(ids).size, MAX_LIVENESS_ACCOUNTS);
  assert.deepEqual(ids.slice(0, 3), ["1", "2", "3"]);
});

test("account filter facets only offer values represented under the other active filter", () => {
  const accounts = [
    { id: 1, status: "error", subscription: "plus" },
    { id: 2, status: "normal", subscription: "plus" },
    { id: 3, status: "normal", subscription: "k12" },
  ];

  const errorFacets = accountFilterFacetCandidates(
    accounts,
    "error",
    "",
    (account, status) => status === "all" || account.status === status,
    (account) => account.subscription,
  );
  assert.deepEqual(errorFacets.subscriptionOptionAccounts.map((account) => account.subscription), ["plus"]);
  assert.deepEqual(errorFacets.filteredAccounts.map((account) => account.id), [1]);

  const plusFacets = accountFilterFacetCandidates(
    accounts,
    "all",
    "plus",
    (account, status) => status === "all" || account.status === status,
    (account) => account.subscription,
  );
  assert.deepEqual(plusFacets.statusOptionAccounts.map((account) => account.id), [1, 2]);
  assert.deepEqual(plusFacets.filteredAccounts.map((account) => account.id), [1, 2]);
});

test("upstream usage preserves zero and converts USD usage with recharge cost only", () => {
  assert.equal(rechargeAdjustedUsage(12.375, 0.0621), 0.7684875);
  assert.equal(rechargeAdjustedUsage(0, 0.0621), 0);
  assert.equal(rechargeAdjustedUsage(null, 0.0621), null);
  assert.equal(rechargeAdjustedUsage(12.375, null), null);
  assert.equal(rechargeAdjustedUsage(-1, 0.0621), null);
  assert.equal(rechargeAdjustedUsage(true, 0.0621), null);
});

test("liveness results expose separate copy actions for account names and emails", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const resultBlock = source.slice(
    source.indexOf("{result.results.map((item) =>"),
    source.indexOf("function AccountRow"),
  );
  assert.match(resultBlock, /title=\{item\.account_name\?\.trim\(\) \? "复制账号名称"/);
  assert.match(resultBlock, /title="复制账号邮箱"/);
  assert.match(resultBlock, /value=\{accountName\}/);
  assert.match(resultBlock, /value=\{email\}/);
});

test("stale-sensitive upstream mutations include the expected identity fingerprint", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
  const fingerprint = "a".repeat(64);
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({
      path: String(input),
      body: JSON.parse(String(init?.body || "{}")),
    });
    return new Response("{}", {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  }) as typeof fetch;
  try {
    await api.updateUpstreamAccount(7, {
      channel_id: null,
      expected_identity_fingerprint: fingerprint,
    });
    await api.deleteUpstreamAccount(7, fingerprint);
    await api.discoverUpstreamAccount(7, fingerprint);
    await api.setUpstreamAccountEnabled(7, false, fingerprint);
    await api.deleteRemoteUpstreamAccount(7, fingerprint);
    await api.applyUpstreamAccountRate(7, 1.25, fingerprint);
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(requests.map((request) => request.path), [
    "/api/upstream-accounts/7",
    "/api/upstream-accounts/7",
    "/api/upstream-accounts/7/discover",
    "/api/upstream-accounts/7/enabled",
    "/api/upstream-accounts/7/remote",
    "/api/upstream-accounts/7/apply",
  ]);
  for (const request of requests) {
    assert.equal(request.body.expected_identity_fingerprint, fingerprint);
  }
});

test("API key inventory sync uses the lightweight inventory endpoint", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const requests: Array<{ path: string; method: string }> = [];
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({ path: String(input), method: String(init?.method || "GET") });
    return new Response(JSON.stringify({ channels: [], unassigned_accounts: [], priority_intervals: [] }), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  }) as typeof fetch;
  try {
    await api.syncApiKeyInventory();
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(requests, [{
    path: "/api/upstream-channels/sync-inventory",
    method: "POST",
  }]);
});

test("upstream overview reads the persisted snapshot unless an explicit refresh is requested", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const paths: string[] = [];
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = (async (input: string | URL | Request) => {
    paths.push(String(input));
    return new Response(JSON.stringify({ channels: [], unassigned_accounts: [], priority_intervals: [] }), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  }) as typeof fetch;
  try {
    await api.upstreamChannels();
    await api.upstreamChannels(true);
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(paths, [
    "/api/upstream-channels?refresh=false",
    "/api/upstream-channels?refresh=true",
  ]);
});

test("site logo upload sends the selected image as the raw request body", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const file = new File(["logo-bytes"], "site-logo.png", { type: "image/png" });
  let captured: { path: string; init?: RequestInit } | null = null;
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    captured = { path: String(input), init };
    return new Response(JSON.stringify({ site_logo_url: "/api/settings/logo", site_logo_updated_at: "2026-07-19T00:00:00Z" }), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  }) as typeof fetch;
  try {
    await api.updateSiteLogo(file);
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }

  assert.ok(captured);
  assert.equal(captured.path, "/api/settings/logo");
  assert.equal(captured.init?.method, "PUT");
  assert.equal(captured.init?.body, file);
  assert.equal((captured.init?.headers as Record<string, string>)["Content-Type"], "image/png");
});

test("toolbar account syncs refresh only their affected data", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const apiKeySyncTimeBlock = source.slice(
    source.indexOf("const lastApiKeySyncEvent"),
    source.indexOf("const oauthSyncActionTime"),
  );
  assert.match(apiKeySyncTimeBlock, /"manual_api_key_inventory_sync"/);
  assert.match(apiKeySyncTimeBlock, /"manual_upstream_sync"/);
  assert.match(apiKeySyncTimeBlock, /"api_key_inventory_sync"/);
  assert.match(apiKeySyncTimeBlock, /"upstream_sync"/);

  const syncBlock = source.slice(
    source.indexOf("const runSyncAction"),
    source.indexOf("if (authState === \"checking\")"),
  );
  assert.match(syncBlock, /api\.syncApiKeyAccounts/);
  assert.match(syncBlock, /scheduleOAuthUsageBackgroundRefresh\(syncResult\.usage_pending\)/);
  assert.doesNotMatch(syncBlock, /await loadAll\(\)/);
  assert.doesNotMatch(syncBlock, /api\.syncApiKeyInventory\(\)/);
  assert.match(syncBlock, /oauthSyncOperationRef/);
  assert.match(syncBlock, /apiKeySyncOperationRef/);
  assert.doesNotMatch(syncBlock, /syncOperationRef\.current/);
  assert.match(syncBlock, /loadAllRequestSequenceRef\.current \+= 1/);
  assert.match(syncBlock, /setApiKeyRefreshVersion\(\(current\) => current \+ 1\)/);
});

test("toolbar API key sync remains available during a single-channel discovery", () => {
  const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const viewSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(appSource, /disabled=\{busy \|\| apiKeySyncBusy \|\| apiKeyViewBlocking\}/);
  assert.match(appSource, /operation\.kind === "channel-discovery"/);
  assert.match(appSource, /skipChannelIds/);
  assert.match(
    viewSource,
    /kind: "channel-discovery"[\s\S]*channelId: Number\(channel\.id\)/,
  );
});

test("OAuth sync refreshes usage snapshots in bounded non-blocking retries", () => {
  assert.deepEqual(oauthUsageBackgroundRefreshIntervals(0), [0]);
  assert.deepEqual(oauthUsageBackgroundRefreshIntervals(3), [250, 1_000, 2_500, 4_000]);

  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const syncBlock = source.slice(
    source.indexOf("const runOAuthSync"),
    source.indexOf("const runApiKeySync"),
  );
  assert.match(syncBlock, /scheduleOAuthUsageBackgroundRefresh\(syncResult\.usage_pending\)/);
  assert.doesNotMatch(syncBlock, /await scheduleOAuthUsageBackgroundRefresh/);
});

test("validated API key data is never replaced by the sanitized display cache", () => {
  const source = readFileSync(
    new URL("../src/ApiKeyAccountsView.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /if \(!cachedData \|\| hasDataRef\.current\) return/);
  assert.match(source, /requestSequence\.current \+= 1/);
  assert.match(source, /upstreamOverviewHasLiveMutationData\(cachedData\)/);
});

test("sub2api credential changes invalidate the API key display cache", () => {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(
    source,
    /payload\.sub2api_x_api_key\?\.trim\(\) \|\| payload\.clear_sub2api_x_api_key/,
  );
  assert.match(
    source,
    /nextSettings\.sub2api_base_url !== previousSub2ApiBaseUrl\s+\|\| changesSub2ApiCredential/,
  );
  assert.match(
    source,
    /upstreamOverviewCacheScope\(previousBaseUrl\)[\s\S]{0,700}setUsageEstimate\(null\);[\s\S]{0,150}setUsageLimitSamples\(null\);[\s\S]{0,150}resetUsageEstimateRequests\(\);/,
  );
  assert.match(
    source,
    /nextSettings\.sub2api_base_url !== previousSub2ApiBaseUrl[\s\S]{0,650}setUsageEstimate\(null\);[\s\S]{0,150}setUsageLimitSamples\(null\);[\s\S]{0,150}resetUsageEstimateRequests\(\);/,
  );
  assert.match(
    source,
    /api\s*\.me\(\)[\s\S]{0,900}\.catch\(\(\) => \{[\s\S]{0,300}setUsageEstimate\(null\);[\s\S]{0,150}setUsageLimitSamples\(null\);[\s\S]{0,150}resetUsageEstimateRequests\(\);/,
  );
  assert.match(
    source,
    /function clearFrontendSessionCaches\(\) \{[\s\S]*clearUpstreamOverviewCache[\s\S]*clearChangeLogCache/,
  );
  assert.equal(
    [...source.matchAll(/clearFrontendSessionCaches\(\);/g)].length,
    5,
  );
  assert.match(
    source,
    /await api\.logout\(\);[\s\S]{0,500}setUsageEstimate\(null\);[\s\S]{0,200}resetUsageEstimateRequests\(\);[\s\S]{0,100}setAuthState\("out"\)/,
  );
});

test("priority interval API requests use stable paths and identity-checked assignment", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const requests: Array<{ path: string; method: string; body: Record<string, unknown> }> = [];
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    requests.push({
      path: String(input),
      method: String(init?.method || "GET"),
      body: init?.body ? JSON.parse(String(init.body)) : {},
    });
    return new Response(JSON.stringify({}), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  }) as typeof fetch;
  const interval = { name: "低成本", start_priority: 40, end_priority: 70, step: 2 };
  const fingerprint = "c".repeat(64);
  try {
    await api.createPriorityInterval(interval);
    await api.updatePriorityInterval(3, interval);
    await api.deletePriorityInterval(3);
    await api.setUpstreamAccountPriorityInterval(9, {
      priority_interval_id: 3,
      expected_identity_fingerprint: fingerprint,
      confirm_identity_rebind: true,
    });
    await api.moveUpstreamAccountPriority(9, {
      direction: "up",
      expected_identity_fingerprint: fingerprint,
    });
    await api.rebalancePriorityIntervals();
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(requests.map(({ path, method }) => ({ path, method })), [
    { path: "/api/upstream-accounts/priority-intervals", method: "POST" },
    { path: "/api/upstream-accounts/priority-intervals/3", method: "PUT" },
    { path: "/api/upstream-accounts/priority-intervals/3", method: "DELETE" },
    { path: "/api/upstream-accounts/9/priority-interval", method: "PUT" },
    { path: "/api/upstream-accounts/9/priority-order", method: "PUT" },
    { path: "/api/upstream-accounts/priority-intervals/rebalance", method: "POST" },
  ]);
  assert.deepEqual(requests[3].body, {
    priority_interval_id: 3,
    expected_identity_fingerprint: fingerprint,
    confirm_identity_rebind: true,
  });
  assert.deepEqual(requests[4].body, {
    direction: "up",
    expected_identity_fingerprint: fingerprint,
  });
});

test("API key account presentation flattens once, sorts cheap multipliers first, and keeps unavailable last", () => {
  const overview: UpstreamChannelsResponse = {
    channels: [{
      id: 5,
      display_name: "渠道甲",
      accounts: [
        {
          sub2api_account_id: 2,
          remote_name: "未知倍率",
          remote_platform: "anthropic",
          composite_multiplier: null,
          priority_interval_id: null,
        },
        {
          sub2api_account_id: 1,
          remote_name: "低倍率",
          remote_platform: "OpenAI",
          composite_multiplier: 0.2,
          priority_interval_id: 7,
        },
        {
          sub2api_account_id: 3,
          remote_name: "更低倍率",
          remote_platform: "openai",
          effective_group_multiplier: 0.5,
          effective_recharge_multiplier: 0.2,
          priority_interval_id: 7,
        },
      ],
    }],
    unassigned_accounts: [
      { sub2api_account_id: 1, remote_name: "重复快照", composite_multiplier: 9 },
      { sub2api_account_id: 4, remote_name: "未分配渠道", remote_platform: null, composite_multiplier: 0.3 },
    ],
  };
  const entries = flattenUpstreamAccounts(overview);
  assert.deepEqual(sortUpstreamAccountEntries(entries).map(({ account }) => account.sub2api_account_id), [3, 1, 4, 2]);
  assert.equal(accountCompositeMultiplier(entries.find(({ account }) => account.sub2api_account_id === 3)!.account), 0.1);
  assert.deepEqual(
    sortUpstreamAccountEntries(filterUpstreamAccountEntries(entries, {
      channel: "5",
      interval: "7",
      platform: "openai",
      query: "渠道甲",
    })).map(({ account }) => account.sub2api_account_id),
    [3, 1],
  );
  assert.deepEqual(
    filterUpstreamAccountEntries(entries, {
      channel: "all",
      interval: "unassigned",
      platform: "anthropic",
      query: "",
    }).map(({ account }) => account.sub2api_account_id),
    [2],
  );
  assert.deepEqual(
    filterUpstreamAccountEntries(entries, {
      channel: "__unassigned__",
      interval: "unassigned",
      platform: "__unknown__",
      query: "",
    }).map(({ account }) => account.sub2api_account_id),
    [4],
  );
  assert.deepEqual(upstreamAccountPlatforms(entries), {
    hasUnknown: true,
    platforms: [
      { value: "anthropic", label: "anthropic" },
      { value: "openai", label: "OpenAI" },
    ],
  });
  assert.deepEqual(upstreamAccountChannels(entries), {
    hasUnassigned: true,
    channels: [{ value: "5", label: "渠道甲" }],
  });
});

test("equal multiplier accounts sort by name and expose persistent neighbor moves", () => {
  const accounts = [
    {
      sub2api_account_id: 2,
      remote_name: "Beta",
      composite_multiplier: 0.1,
      desired_priority: 42,
      priority_interval_id: 7,
    },
    {
      sub2api_account_id: 1,
      remote_name: "alpha",
      composite_multiplier: 0.1,
      desired_priority: 40,
      priority_interval_id: 7,
    },
  ];
  const entries = accounts.map((account) => ({ account, channel: null }));

  assert.deepEqual(
    sortUpstreamAccountEntries(entries).map(({ account }) => account.sub2api_account_id),
    [1, 2],
  );
  assert.deepEqual(
    sortUpstreamAccountEntriesByName([...entries].reverse()).map(({ account }) => account.sub2api_account_id),
    [1, 2],
  );
  assert.deepEqual(priorityTieMoveOptions(accounts).get("1"), {
    canMoveDown: false,
    canMoveUp: true,
    peerCount: 2,
  });
  assert.deepEqual(priorityTieMoveOptions(accounts).get("2"), {
    canMoveDown: true,
    canMoveUp: false,
    peerCount: 2,
  });

  accounts[0].priority_tiebreak_order = 0;
  accounts[0].priority_tiebreak_multiplier = 0.1;
  accounts[1].priority_tiebreak_order = 1;
  accounts[1].priority_tiebreak_multiplier = 0.1;
  assert.equal(priorityTieMoveOptions(accounts).get("2")?.canMoveUp, true);

  const nearEqual = accounts.map((account, index) => ({
    ...account,
    sub2api_account_id: index + 10,
    composite_multiplier: index === 0 ? 1.0000000000001 : 1.0000000000002,
    priority_tiebreak_order: null,
    priority_tiebreak_multiplier: null,
  }));
  assert.equal(priorityTieMoveOptions(nearEqual).size, 0);
  assert.equal(priorityTieMultiplierKey(1.00000000000025), "1.0000000000002");
  assert.equal(priorityTieMultiplierKey(5e-14), "0.0000000000000");
  assert.equal(priorityTieMultiplierKey(0.00123), "0.0012300000000");
});

test("priority interval assignment allows explicitly claiming legacy identities but blocks mismatches", () => {
  const unbound = {
    sub2api_account_id: 1,
    identity_binding_status: "unbound" as const,
    identity_rebind_required: true,
  };
  const mismatch = {
    ...unbound,
    identity_binding_status: "mismatch" as const,
  };
  const bound = {
    ...unbound,
    identity_binding_status: "bound" as const,
    identity_rebind_required: false,
  };

  assert.equal(priorityIntervalAssignmentNeedsConfirmation(unbound), true);
  assert.equal(priorityIntervalAssignmentBlocked(unbound), false);
  assert.equal(priorityIntervalAssignmentNeedsConfirmation(mismatch), false);
  assert.equal(priorityIntervalAssignmentBlocked(mismatch), true);
  assert.equal(priorityIntervalAssignmentBlocked(bound), false);
});

async function apiKeySyncRequestBody(
  overview: UpstreamChannelsResponse,
  confirmLegacyBindings: boolean,
  skipChannelIds: number[] = [],
) {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  const requests: Array<{ path: string; method: string; body: Record<string, unknown> | null }> = [];
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    requests.push({
      path,
      method: init?.method || "GET",
      body: init?.body ? JSON.parse(String(init.body)) : null,
    });
    return new Response(JSON.stringify({ total: 1, succeeded: 1, failed: 0, channels: [] }), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  }) as typeof fetch;

  try {
    await api.syncApiKeyAccounts(overview, confirmLegacyBindings, skipChannelIds);
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(requests.map(({ path, method }) => ({ path, method })), [
    { path: "/api/upstream-channels/discover-all", method: "POST" },
  ]);
  return requests[0].body;
}

const apiKeySyncOverview: UpstreamChannelsResponse = {
  channels: [{
    id: 9,
    accounts: [
      {
        sub2api_account_id: 2,
        identity_fingerprint: "b".repeat(64),
        api_key_origin_rebind_required: true,
      },
      {
        sub2api_account_id: 3,
        identity_fingerprint: "c".repeat(64),
        identity_binding_status: "bound",
      },
    ],
  }],
  unassigned_accounts: [
    {
      sub2api_account_id: "1",
      identity_fingerprint: "a".repeat(64),
      identity_binding_status: "unbound",
    },
  ],
};

test("confirmed API account sync sends strict identity bindings", async () => {
  assert.deepEqual(await apiKeySyncRequestBody(apiKeySyncOverview, true), {
    confirm_legacy_bindings: true,
    account_bindings: [
      { sub2api_account_id: 1, expected_identity_fingerprint: "a".repeat(64) },
      { sub2api_account_id: 2, expected_identity_fingerprint: "b".repeat(64) },
    ],
  });
});

test("legacy API account binding summary matches the confirmation payload", () => {
  assert.deepEqual(upstreamLegacyBindingCounts(apiKeySyncOverview), {
    unbound: 1,
    originRebind: 1,
  });
  assert.match(
    apiAccountLegacyBindingConfirmationMessage({ unbound: 1, originRebind: 1 }),
    /身份待绑定：1 个[\s\S]*来源待重新绑定：1 个/,
  );
});

test("unconfirmed API account sync omits binding confirmation payload", async () => {
  assert.deepEqual(await apiKeySyncRequestBody(apiKeySyncOverview, false), {});
});

test("API account sync sends channels already under manual discovery as skips", async () => {
  assert.deepEqual(await apiKeySyncRequestBody(apiKeySyncOverview, false, [7, 11]), {
    skip_channel_ids: [7, 11],
  });
});

test("confirmed API account sync with no accounts omits binding confirmation payload", async () => {
  assert.deepEqual(await apiKeySyncRequestBody({ channels: [], unassigned_accounts: [] }, true), {});
});

test("confirmed API account sync supports more than 500 pending bindings", async () => {
  const pendingAccounts = Array.from({ length: 501 }, (_, index) => ({
    sub2api_account_id: index + 1,
    identity_fingerprint: (index + 1).toString(16).padStart(64, "0"),
    identity_binding_status: "unbound" as const,
  }));
  const body = await apiKeySyncRequestBody(
    { channels: [], unassigned_accounts: pendingAccounts },
    true,
  );
  assert.equal(body?.confirm_legacy_bindings, true);
  assert.equal((body?.account_bindings as unknown[]).length, 501);
});

test("channel credential rebind checks canonical and management origins independently", () => {
  const accountSource = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(accountSource, /先更新 Sub2API 账号的上游地址，再把本地 API Key 配置绑定到新渠道/);
  const channel = {
    id: 1,
    canonical_base_url: "https://api.old.example/v1",
    management_base_url: "https://manage.example/admin",
  };
  assert.equal(channelCredentialBindingChanged(
    channel,
    "https://api.new.example/v1",
    "https://manage.example/other",
  ), true);
  assert.equal(channelCredentialBindingChanged(
    channel,
    "https://api.old.example/v2",
    "https://manage.example/other",
  ), false);
});

test("manual and fallback-manual multipliers remain editable", () => {
  for (const group_multiplier_source of ["manual", "fallback_manual"]) {
    const account = {
      sub2api_account_id: 7,
      identity_fingerprint: "a".repeat(64),
      effective_group_multiplier: 2,
      group_multiplier_source,
      group_multiplier_status: "in_sync",
    };
    assert.equal(canSetManualMultiplier(account), true);
    assert.deepEqual(buildUpstreamAccountUpdatePayload({
      account,
      apiKey: "  key-value  ",
      channelId: 3,
      manualGroupMultiplier: "0.25",
    }), {
      channel_id: 3,
      expected_identity_fingerprint: "a".repeat(64),
      manual_group_multiplier: 0.25,
      api_key: "key-value",
    });
  }
});

test("automatic upstream multipliers omit manual_group_multiplier", () => {
  const payload = buildUpstreamAccountUpdatePayload({
    account: {
      sub2api_account_id: 8,
      identity_fingerprint: "b".repeat(64),
      effective_group_multiplier: 1,
      group_multiplier_source: "upstream_key",
      group_multiplier_status: "in_sync",
    },
    apiKey: "",
    channelId: 4,
    manualGroupMultiplier: "",
  });
  assert.deepEqual(payload, {
    channel_id: 4,
    expected_identity_fingerprint: "b".repeat(64),
  });
  assert.equal(Object.hasOwn(payload, "manual_group_multiplier"), false);
});

test("account rename payload trims changes and omits an unchanged remote name", () => {
  const account = {
    sub2api_account_id: 8,
    identity_fingerprint: "b".repeat(64),
    remote_name: "Current name",
    effective_group_multiplier: 1,
    group_multiplier_source: "upstream_key",
    group_multiplier_status: "in_sync",
  };
  const renamed = buildUpstreamAccountUpdatePayload({
    account,
    apiKey: "",
    channelId: 4,
    manualGroupMultiplier: "",
    remoteName: "  New name  ",
  });
  assert.equal(renamed.remote_name, "New name");

  const unchanged = buildUpstreamAccountUpdatePayload({
    account,
    apiKey: "",
    channelId: 4,
    manualGroupMultiplier: "",
    remoteName: " Current name ",
  });
  assert.equal(Object.hasOwn(unchanged, "remote_name"), false);
});

test("account rename payload rejects empty and oversized names", () => {
  const account = {
    sub2api_account_id: 8,
    identity_fingerprint: "b".repeat(64),
    remote_name: "Current name",
    effective_group_multiplier: 1,
    group_multiplier_source: "upstream_key",
    group_multiplier_status: "in_sync",
  };
  const build = (remoteName: string) => buildUpstreamAccountUpdatePayload({
    account,
    apiKey: "",
    channelId: 4,
    manualGroupMultiplier: "",
    remoteName,
  });
  assert.throws(() => build("   "), /不能为空/);
  assert.throws(() => build("x".repeat(101)), /100/);
});

test("an unchanged legacy name over 100 characters does not block other updates", () => {
  const legacyName = "x".repeat(150);
  const payload = buildUpstreamAccountUpdatePayload({
    account: {
      sub2api_account_id: 8,
      identity_fingerprint: "b".repeat(64),
      remote_name: legacyName,
      effective_group_multiplier: 1,
      group_multiplier_source: "upstream_key",
      group_multiplier_status: "in_sync",
    },
    apiKey: "new-key",
    channelId: 4,
    manualGroupMultiplier: "",
    remoteName: legacyName,
  });
  assert.equal(Object.hasOwn(payload, "remote_name"), false);
  assert.equal(payload.api_key, "new-key");
});

test("clearing an editable manual multiplier sends an intentional null", () => {
  const payload = buildUpstreamAccountUpdatePayload({
    account: {
      sub2api_account_id: 9,
      identity_fingerprint: "c".repeat(64),
      effective_group_multiplier: 0.5,
      group_multiplier_source: "manual",
      group_multiplier_status: "manual",
    },
    apiKey: "",
    channelId: null,
    manualGroupMultiplier: "",
  });
  assert.equal(payload.manual_group_multiplier, null);
});

test("session cache is scoped by the complete sub2api URL including API version", () => {
  const storage = new MemoryStorage();
  const baseV1 = "http://localhost:8080/api/v1";
  const response = { channels: [], unassigned_accounts: [], local_recharge_multiplier: 0.1 };
  writeUpstreamOverviewCache(storage, baseV1, response);

  assert.deepEqual(readUpstreamOverviewCache(storage, baseV1)?.local_recharge_multiplier, 0.1);
  assert.equal(readUpstreamOverviewCache(storage, "http://localhost:8080/api/v2"), null);
  assert.equal(readUpstreamOverviewCache(storage, "http://localhost:8081/api/v1"), null);
  assert.notEqual(upstreamOverviewCacheKey(baseV1), upstreamOverviewCacheKey("http://localhost:8080/api/v2"));
  assert.notEqual(upstreamOverviewCacheKey(baseV1), upstreamOverviewCacheKey("http://localhost:8080/API/v1"));
});

test("session cache strips credentials and credential hints", () => {
  const unsafe = {
    priority_intervals: [{
      id: 4,
      name: "低成本",
      start_priority: 40,
      end_priority: 70,
      step: 2,
      allocation_strategy: "cost_optimized",
      rate_pause_enabled: true,
      rate_pause_mode: "absolute_multiplier",
      rate_increase_threshold_percent: 25,
      rate_absolute_threshold: 1.25,
      account_count: 1,
      effective_step: 2,
    }],
    channels: [{
      id: 2,
      display_name: "上游",
      account_count: 1,
      probe_enabled: false,
      access_token: "secret-access",
      refresh_token: "secret-refresh",
      access_token_set: true,
      recharge_adjusted_balance: 8.5,
      balance_guard_state: "insufficient",
      balance_guard_basis: "recharge_adjusted",
      balance_guard_value: -0.5,
      balance_guard_checked_at: "2026-07-16T08:03:00Z",
      balance_guard_paused_count: 1,
      channel_monitor_count: 1,
      channel_monitor_status: "ok",
      channel_monitor_checked_at: "2026-07-16T08:04:00Z",
      channel_monitor_test_models: { 12: "gpt-5.5", invalid: "ignored" },
      channel_monitors: [{
        id: 12,
        name: "主线路",
        provider: "OpenAI",
        primary_model: "gpt-5",
        primary_status: "available",
        primary_latency_ms: 218,
        availability_7d: 99.95,
        extra_models: [{ model: "gpt-4.1", status: "available", latency_ms: 190 }],
        timeline: [{ checked_at: "2026-07-16T08:00:00Z", status: "available", latency_ms: 218 }],
        effective_status: "available",
        effective_source: "connection_test",
        fallback_test_status: "available",
        fallback_test_model: "gpt-5.5",
        fallback_test_attempts: 2,
        fallback_test_checked_at: "2026-07-16T08:04:00Z",
      }],
      today_balance_used: 3.25,
      today_balance_unit: "USD",
      today_balance_status: "ok",
      today_balance_checked_at: "2026-07-16T08:02:00Z",
      yesterday_balance_used: 2.75,
      yesterday_balance_unit: "USD",
      yesterday_balance_status: "ok",
      yesterday_balance_checked_at: "2026-07-16T08:02:00Z",
      accounts: [{
        sub2api_account_id: 10,
        remote_name: "账号",
        remote_platform: "anthropic",
        api_key: "secret-key",
        encrypted_api_key: "secret-ciphertext",
        api_key_hint: "key-tail",
        api_key_set: true,
        identity_fingerprint: "a".repeat(64),
        identity_binding_status: "bound",
        priority: 40,
        desired_priority: 42,
        priority_interval_id: 4,
        priority_interval_name: "低成本",
        priority_sync_status: "pending",
        priority_assignment_when_disabled: true,
        priority_assignment_when_disabled_effective: true,
        rate_pause_policy: "inherit",
        rate_pause_effective_enabled: true,
        rate_pause_effective_source: "priority_interval",
        rate_pause_mode: "absolute_multiplier",
        rate_increase_threshold_percent: 25,
        rate_absolute_threshold: 1.25,
        composite_multiplier: 0.2,
        balance_guard_restore_eligible: true,
        balance_guard_channel_id: 2,
        balance_guard_paused_at: "2026-07-16T08:03:00Z",
        upstream_usage_amount: 12.375,
        upstream_usage_unit: "USD",
        upstream_usage_checked_at: "2026-07-16T08:01:30Z",
        today_upstream_usage_amount: 1.875,
        today_upstream_usage_unit: "USD",
        today_upstream_usage_status: "ok",
        today_upstream_usage_source: "sub2api_daily_usage",
        today_upstream_usage_checked_at: "2026-07-16T08:02:30Z",
        upstream_key_status: "disabled",
        upstream_group_status: "invalid",
        upstream_health_invalid_count: 2,
        upstream_health_checked_at: "2026-07-16T08:00:00Z",
        upstream_key_checked_at: "2026-07-16T08:00:10Z",
        upstream_group_checked_at: "2026-07-16T08:00:20Z",
        availability_check_mode: "independent_model",
        availability_monitor_id: 12,
        availability_test_model: "gpt-5.5",
        available_models: [
          { id: "gpt-5.5", display_name: "GPT 5.5" },
          { id: "gpt-5.5", display_name: "duplicate" },
        ],
        available_models_status: "ok",
        available_models_checked_at: "2026-07-16T08:03:30Z",
        availability_status: "available",
        availability_unavailable_count: 0,
        availability_recovery_count: 2,
        availability_checked_at: "2026-07-16T08:04:00Z",
        availability_source: "independent_model",
        auto_disabled_reason: "Upstream key is disabled.",
        last_auto_disabled_at: "2026-07-16T08:01:00Z",
        active_pause_holds: [{
          reason: "channel_monitor_unavailable",
          triggered_at: "2026-07-16T08:05:00Z",
          recovery_mode: "monitor_recovered",
          scope_channel_id: 2,
          evidence: {
            monitor_status: "unavailable",
            threshold: 3,
            unit: "CNY",
            ignored: "must-not-survive",
          },
          evidence_json: { token: "secret-hold-evidence" },
        }, {
          reason: "upstream_rate_increase",
          triggered_at: "2026-07-16T08:06:00Z",
          recovery_mode: "rate_within_threshold",
          scope_channel_id: 2,
        }],
        pause_owned_by_plugin: true,
        auto_restore_eligible: true,
        auto_pause_episode_id: "episode-1",
        auto_pause_channel_id: 2,
        auto_paused_at: "2026-07-16T08:05:30Z",
      }],
    }],
    unassigned_accounts: [],
  };
  const safe = sanitizeUpstreamOverview(unsafe);
  assert.ok(safe);
  assert.equal(safe.channels[0].access_token_set, true);
  assert.equal(safe.channels[0].account_count, 1);
  assert.equal(safe.channels[0].probe_enabled, false);
  assert.equal(safe.channels[0].recharge_adjusted_balance, 8.5);
  assert.equal(safe.channels[0].balance_guard_state, "insufficient");
  assert.equal(safe.channels[0].balance_guard_basis, "recharge_adjusted");
  assert.equal(safe.channels[0].balance_guard_value, -0.5);
  assert.equal(safe.channels[0].balance_guard_paused_count, 1);
  assert.equal(safe.channels[0].channel_monitor_count, 1);
  assert.equal((safe.channels[0] as Record<string, unknown>).channel_monitor_test_models, undefined);
  assert.equal(safe.channels[0].channel_monitors?.[0].primary_latency_ms, 218);
  assert.equal(safe.channels[0].channel_monitors?.[0].extra_models?.[0].name, "gpt-4.1");
  assert.equal(safe.channels[0].channel_monitors?.[0].timeline?.[0].time, "2026-07-16T08:00:00Z");
  assert.equal(safe.channels[0].channel_monitors?.[0].timeline?.[0].status, "available");
  assert.equal(safe.channels[0].channel_monitors?.[0].effective_source, undefined);
  assert.equal(safe.channels[0].channel_monitors?.[0].fallback_test_attempts, undefined);
  assert.equal(safe.channels[0].today_balance_used, 3.25);
  assert.equal(safe.channels[0].today_balance_status, "ok");
  assert.equal(safe.channels[0].yesterday_balance_used, 2.75);
  assert.equal(safe.channels[0].yesterday_balance_status, "ok");
  assert.equal(safe.channels[0].accounts?.[0].api_key_set, true);
  assert.equal(safe.channels[0].accounts?.[0].api_key_hint, undefined);
  assert.equal(safe.channels[0].accounts?.[0].identity_fingerprint, undefined);
  assert.equal(safe.channels[0].accounts?.[0].remote_platform, "anthropic");
  assert.equal(safe.channels[0].accounts?.[0].priority_interval_id, 4);
  assert.equal(safe.channels[0].accounts?.[0].priority_assignment_when_disabled, true);
  assert.equal(safe.channels[0].accounts?.[0].priority_assignment_when_disabled_effective, true);
  assert.equal(safe.channels[0].accounts?.[0].rate_pause_policy, "inherit");
  assert.equal(safe.channels[0].accounts?.[0].rate_pause_effective_enabled, true);
  assert.equal(safe.channels[0].accounts?.[0].rate_pause_effective_source, "priority_interval");
  assert.equal("rate_pause_mode" in (safe.channels[0].accounts?.[0] || {}), false);
  assert.equal("rate_increase_threshold_percent" in (safe.channels[0].accounts?.[0] || {}), false);
  assert.equal(safe.channels[0].accounts?.[0].rate_absolute_threshold, 1.25);
  assert.deepEqual(safe.channels[0].accounts?.[0].available_models, [
    { id: "gpt-5.5", display_name: "GPT 5.5" },
  ]);
  assert.equal(safe.channels[0].accounts?.[0].available_models_status, "ok");
  assert.equal(safe.channels[0].accounts?.[0].availability_source, "independent_model");
  assert.equal(safe.channels[0].accounts?.[0].composite_multiplier, 0.2);
  assert.equal(safe.channels[0].accounts?.[0].balance_guard_restore_eligible, true);
  assert.equal(safe.channels[0].accounts?.[0].balance_guard_channel_id, 2);
  assert.equal(safe.channels[0].accounts?.[0].upstream_usage_amount, 12.375);
  assert.equal(safe.channels[0].accounts?.[0].upstream_usage_unit, "USD");
  assert.equal(safe.channels[0].accounts?.[0].today_upstream_usage_amount, 1.875);
  assert.equal(safe.channels[0].accounts?.[0].today_upstream_usage_status, "ok");
  assert.equal(safe.channels[0].accounts?.[0].today_upstream_usage_source, "sub2api_daily_usage");
  assert.equal(safe.channels[0].accounts?.[0].upstream_key_status, "disabled");
  assert.equal(safe.channels[0].accounts?.[0].upstream_group_status, "invalid");
  assert.equal(safe.channels[0].accounts?.[0].upstream_health_invalid_count, 2);
  assert.equal(safe.channels[0].accounts?.[0].upstream_health_checked_at, "2026-07-16T08:00:00Z");
  assert.equal(safe.channels[0].accounts?.[0].upstream_key_checked_at, "2026-07-16T08:00:10Z");
  assert.equal(safe.channels[0].accounts?.[0].upstream_group_checked_at, "2026-07-16T08:00:20Z");
  assert.equal(safe.channels[0].accounts?.[0].availability_check_mode, "independent_model");
  assert.equal(safe.channels[0].accounts?.[0].availability_monitor_id, 12);
  assert.equal(safe.channels[0].accounts?.[0].availability_test_model, "gpt-5.5");
  assert.equal(safe.channels[0].accounts?.[0].availability_status, "available");
  assert.equal(safe.channels[0].accounts?.[0].availability_recovery_count, 2);
  assert.equal(safe.channels[0].accounts?.[0].auto_disabled_reason, "Upstream key is disabled.");
  assert.equal(safe.channels[0].accounts?.[0].last_auto_disabled_at, "2026-07-16T08:01:00Z");
  assert.deepEqual(safe.channels[0].accounts?.[0].active_pause_holds, [{
    reason: "channel_monitor_unavailable",
    triggered_at: "2026-07-16T08:05:00Z",
    recovery_mode: "monitor_recovered",
    scope_channel_id: 2,
    evidence: {
      balance: undefined,
      basis: undefined,
      threshold: 3,
      unit: "CNY",
      key_status: undefined,
      group_status: undefined,
      monitor_status: "unavailable",
      unavailable_count: undefined,
      test_status: undefined,
      test_purpose: undefined,
      test_attempts: undefined,
      max_test_attempts: undefined,
      baseline_multiplier: undefined,
      mode: undefined,
      observed_multiplier: undefined,
      absolute_threshold: undefined,
      increase_percent: undefined,
      threshold_percent: undefined,
    },
  }, {
    reason: "upstream_rate_increase",
    triggered_at: "2026-07-16T08:06:00Z",
    recovery_mode: "rate_within_threshold",
    scope_channel_id: 2,
  }]);
  assert.equal(safe.channels[0].accounts?.[0].pause_owned_by_plugin, true);
  assert.equal(safe.channels[0].accounts?.[0].auto_restore_eligible, true);
  assert.equal(safe.channels[0].accounts?.[0].auto_pause_episode_id, "episode-1");
  assert.equal(safe.channels[0].accounts?.[0].auto_pause_channel_id, 2);
  assert.equal(safe.channels[0].accounts?.[0].auto_paused_at, "2026-07-16T08:05:30Z");
  assert.deepEqual(safe.priority_intervals, [{
    id: 4,
    name: "低成本",
    start_priority: 40,
    end_priority: 70,
    step: 2,
    allocation_strategy: "cost_optimized",
    rate_pause_enabled: true,
    rate_absolute_threshold: 1.25,
    account_count: 1,
    effective_step: 2,
  }]);

  const serialized = JSON.stringify(safe);
  assert.doesNotMatch(serialized, /secret-access|secret-refresh|secret-key|secret-ciphertext|key-tail/);
  assert.doesNotMatch(serialized, /secret-hold-evidence|evidence_json/);
  assert.doesNotMatch(serialized, /"access_token"|"refresh_token"|"api_key"|"encrypted_api_key"|"api_key_hint"/);
  assert.equal(upstreamOverviewHasLiveMutationData(safe), false);
  assert.equal(
    upstreamOverviewHasLiveMutationData(unsafe as unknown as UpstreamChannelsResponse),
    true,
  );
});

test("account status filters keep rate drift separate from priority and use discovery timestamps", () => {
  const account = {
    sub2api_account_id: 8,
    managed: true,
    api_key_set: true,
    would_change: false,
    priority: 40,
    desired_priority: 42,
    priority_sync_status: "pending",
    last_discovered_at: "2026-07-17T00:00:00Z",
  };
  assert.equal(upstreamAccountMatchesStatus(account, "pending"), false);
  assert.equal(upstreamAccountMatchesStatus({ ...account, would_change: true }, "pending"), true);
  assert.equal(upstreamAccountMatchesStatus(account, "undiscovered"), false);
  assert.equal(upstreamAccountMatchesStatus({ ...account, last_discovered_at: null }, "undiscovered"), true);
  assert.equal(
    upstreamAccountMatchesStatus({ ...account, identity_rebind_required: true }, "attention"),
    true,
  );
});

test("clearing the cache removes every sub2api scope only", () => {
  const storage = new MemoryStorage();
  storage.setItem("unrelated", "keep");
  writeUpstreamOverviewCache(storage, "http://localhost:8080/api/v1", { channels: [], unassigned_accounts: [] });
  writeUpstreamOverviewCache(storage, "http://localhost:8081/api/v1", { channels: [], unassigned_accounts: [] });
  clearUpstreamOverviewCache(storage);
  assert.equal(storage.length, 1);
  assert.equal(storage.getItem("unrelated"), "keep");
});

test("storage restrictions fall back to the in-memory safe response", () => {
  const response = { channels: [], unassigned_accounts: [] };
  const safeResponse = writeUpstreamOverviewCache(null, "http://localhost:8080/api/v1", response);
  assert.deepEqual(safeResponse?.channels, []);
  assert.deepEqual(safeResponse?.unassigned_accounts, []);
  assert.equal(readUpstreamOverviewCache(null, "http://localhost:8080/api/v1"), null);
  assert.doesNotThrow(() => clearUpstreamOverviewCache(null));
});

test("unsafe numeric upstream ids are discarded from cached display state", () => {
  const safe = sanitizeUpstreamOverview({
    channels: [
      { id: Number.MAX_SAFE_INTEGER + 1, accounts: [] },
      { id: 3, accounts: [{ sub2api_account_id: Number.MAX_SAFE_INTEGER + 1 }] },
    ],
    unassigned_accounts: [
      { sub2api_account_id: Number.MAX_SAFE_INTEGER + 1 },
      { sub2api_account_id: 12 },
    ],
  });

  assert.ok(safe);
  assert.deepEqual(safe.channels.map((channel) => channel.id), [3]);
  assert.deepEqual(safe.channels[0].accounts, []);
  assert.deepEqual(safe.unassigned_accounts.map((account) => account.sub2api_account_id), [12]);
});

test("discovery copy distinguishes read-only probing from rate application", () => {
  const readOnly = upstreamDiscoveryCopy(false);
  assert.equal(readOnly.bulkLabel, "探测全部渠道");
  assert.equal(readOnly.channelAriaPrefix, "探测渠道");
  assert.match(readOnly.allSuccess, /未修改账号计费倍率/);
  assert.match(channelDiscoverySuccessMessage(false, "渠道甲"), /未修改账号计费倍率/);
  assert.equal(channelDiscoveryErrorMessage(false, "渠道甲"), "渠道甲 探测失败");

  const applying = upstreamDiscoveryCopy(true);
  assert.equal(applying.bulkLabel, "探测并应用全部渠道");
  assert.equal(applying.channelAriaPrefix, "探测并应用渠道");
  assert.match(applying.allSuccess, /探测并应用完成/);
  assert.match(channelDiscoverySuccessMessage(true, "渠道甲"), /探测并应用/);
  assert.equal(channelDiscoveryErrorMessage(true, "渠道甲"), "渠道甲 探测并应用失败");
});

test("global automation pause disables upstream rate-write presentation", () => {
  assert.equal(upstreamRateWritesAllowed(true, false), true);
  assert.equal(upstreamRateWritesAllowed(true, true), false);
  assert.equal(upstreamRateWritesAllowed(false, false), false);
});

test("API account sync summaries report empty, complete, and partial results", () => {
  assert.equal(
    apiAccountSyncMessage({ total: 0, succeeded: 0, failed: 0 }, false),
    "未在 sub2api 中发现可同步的 API Key 渠道。",
  );
  assert.match(
    apiAccountSyncMessage({ total: 3, succeeded: 3, failed: 0 }, false),
    /3 个渠道探测成功/,
  );
  assert.match(
    apiAccountSyncMessage({ total: 3, succeeded: 2, failed: 1 }, true),
    /2\/3 个渠道探测并应用成功，1 个失败/,
  );
  assert.match(
    apiAccountSyncMessage({ total: 3, succeeded: 0, failed: 0, cached: 2, skipped: 1 }, true),
    /2 个渠道使用缓存，1 个按渠道设置跳过/,
  );
});

test("account rate labels reflect current comparison and automatic write mode", () => {
  assert.equal(accountRateStatusLabel(null, undefined, false), "待计算");
  assert.equal(accountRateStatusLabel(2, undefined, true), "待确认当前倍率");
  assert.equal(accountRateStatusLabel(2, false, false), "已同步");
  assert.equal(accountRateStatusLabel(2, true, true), "待自动同步");
  assert.equal(accountRateStatusLabel(2, true, false), "待应用（自动同步关闭）");
});

test("cached upstream mutations stay disabled until a live response succeeds", () => {
  assert.equal(upstreamMutationControlsDisabled({
    liveDataValidated: false,
    loading: false,
    refreshing: false,
  }), true);
  assert.equal(upstreamMutationControlsDisabled({
    liveDataValidated: true,
    loading: false,
    refreshing: false,
  }), false);
  assert.equal(upstreamMutationControlsDisabled({
    liveDataValidated: true,
    loading: false,
    refreshing: true,
  }), true);
});

test("upstream change reasons and statuses use user-facing Chinese labels", () => {
  assert.equal(upstreamChangeReasonLabel("upstream_group_change"), "上游分组变化");
  assert.equal(upstreamChangeReasonLabel("upstream_recharge_change"), "上游充值成本变化");
  assert.equal(upstreamChangeReasonLabel("local_recharge_change"), "本地充值成本变化");
  assert.equal(upstreamChangeReasonLabel("target_recalculated"), "目标倍率重算");
  assert.equal(upstreamChangeReasonLabel("rate_drift"), "账号倍率偏离目标");
  assert.equal(upstreamChangeReasonLabel("upstream_key_disabled"), "上游 Key 已禁用");
  assert.equal(upstreamChangeReasonLabel("upstream_group_invalid"), "上游分组已删除");
  assert.equal(upstreamChangeReasonLabel("account_auto_disabled"), "账号已自动禁用");
  assert.equal(upstreamChangeReasonLabel("upstream_auto_disable"), "上游失效后自动禁用");
  assert.equal(upstreamChangeReasonLabel("upstream_key_recovered"), "上游 Key 恢复可用");
  assert.equal(upstreamChangeReasonLabel("upstream_group_recovered"), "上游分组恢复可用");
  assert.equal(upstreamChangeReasonLabel("negative_balance"), "上游余额不足");
  assert.equal(upstreamChangeReasonLabel("channel_monitor_unavailable"), "渠道监控与回退测试不可用");
  assert.equal(upstreamChangeReasonLabel("upstream_rate_increase"), "综合上游倍率上涨");
  assert.equal(upstreamHealthStatusLabel("key", "disabled"), "已禁用");
  assert.equal(upstreamHealthStatusLabel("group", "invalid"), "已失效");
  assert.equal(upstreamHealthStatusLabel("group", "deleted"), "已删除");
  assert.equal(upstreamHealthStatusLabel("key", "expired"), "已过期");
  assert.equal(upstreamHealthStatusLabel("key", "quota_exhausted"), "额度耗尽");
  assert.equal(upstreamHealthStatusLabel("group", "unassigned"), "未分配");
  assert.equal(upstreamHealthStatusLabel("key", "unknown"), "未确认");
  assert.equal(upstreamStatusLabel("observed"), "已观测");
  assert.equal(upstreamStatusLabel("applied"), "已应用");
  assert.equal(upstreamStatusLabel("apply_failed"), "应用失败");
  assert.equal(upstreamStatusLabel("skipped"), "已跳过");
  assert.equal(upstreamStatusLabel("openai"), "OpenAI");
  assert.equal(upstreamStatusLabel("anthropic"), "Anthropic");
  assert.equal(upstreamStatusLabel("gemini"), "Gemini");
  assert.equal(upstreamStatusLabel("xai"), "xAI");
  assert.equal(upstreamStatusLabel("operational"), "可用");
  assert.equal(upstreamStatusLabel("token_invalid"), "Token 失效");
  assert.equal(upstreamStatusLabel("degraded"), "降级");
  assert.equal(upstreamStatusLabel("timeout"), "超时");
  assert.equal(upstreamStatusTone("healthy"), "success");
  assert.equal(upstreamStatusTone("operational"), "success");
  assert.equal(upstreamStatusTone("degraded"), "warn");
  assert.equal(upstreamStatusTone("timeout"), "danger");
});

test("upstream token failures replace generic channel errors", () => {
  assert.equal(upstreamChannelTokenInvalid({ balance_status: "token_invalid" }), true);
  assert.equal(upstreamChannelTokenInvalid({ channel_monitor_status: "credentials_rejected" }), true);
  assert.equal(upstreamChannelTokenInvalid({ last_error: "Upstream channel token is invalid." }), true);
  assert.equal(upstreamChannelTokenInvalid({ balance_status: "error", last_error: "network timeout" }), false);
  assert.equal(isGenericUpstreamChannelError("Unable to read the upstream channel."), true);
  assert.equal(isGenericUpstreamChannelError("Upstream channel discovery failed."), true);
  assert.equal(isGenericUpstreamChannelError("The upstream rejected the credentials."), false);
});

test("rate log presentation prefers persisted 1:1 upstream multipliers", () => {
  const change = upstreamRateChange({
    id: 1,
    sub2api_account_id: 42,
    old_group_multiplier: 2,
    new_group_multiplier: 4,
    upstream_recharge_multiplier: 0.1,
    old_upstream_multiplier: 0.25,
    new_upstream_multiplier: 0.4,
    status: "observed",
    created_at: "2026-07-13T00:00:00Z",
  });

  assert.equal(change.oldValue, 0.25);
  assert.equal(change.newValue, 0.4);
  assert.equal(change.direction, "increase");
  assert.ok(Math.abs((change.delta || 0) - 0.15) < 1e-12);
});

test("upstream change summaries identify the concrete upstream transition", () => {
  const summary = upstreamChangeSummary({
    id: 7,
    sub2api_account_id: 42,
    old_group_id: "basic",
    new_group_id: "premium",
    old_group_name: "基础",
    new_group_name: "高级",
    old_group_multiplier: 1,
    new_group_multiplier: 2,
    old_upstream_group_status: "available",
    new_upstream_group_status: "invalid",
    old_upstream_key_status: "available",
    new_upstream_key_status: "disabled",
    old_upstream_recharge_multiplier: 0.1,
    new_upstream_recharge_multiplier: 0.2,
    old_remote_schedulable: true,
    new_remote_schedulable: false,
    old_target_rate: 0.1,
    new_target_rate: 0.4,
    status: "observed",
    created_at: "2026-07-16T08:00:00Z",
  });

  assert.equal(
    summary,
    "上游分组名称 基础 -> 高级；上游分组倍率变化；上游分组不可用；上游 Key 状态变化；上游充值倍率变化；账号调度状态变化；账号计费倍率变化",
  );
});

test("legacy rate logs derive normalized upstream multipliers from recharge cost", () => {
  const increase = upstreamRateChange({
    id: 2,
    sub2api_account_id: 43,
    old_group_multiplier: 1,
    new_group_multiplier: 2.8,
    upstream_recharge_multiplier: 0.0621,
    status: "observed",
    created_at: "2026-07-13T00:00:00Z",
  });
  assert.equal(increase.oldValue, 0.0621);
  assert.equal(increase.newValue, 0.17388);
  assert.equal(increase.direction, "increase");
  assert.ok(Math.abs((increase.delta || 0) - 0.11178) < 1e-12);

  const decrease = upstreamRateChange({
    id: 3,
    sub2api_account_id: 44,
    old_group_multiplier: 2,
    new_group_multiplier: 0.5,
    upstream_recharge_multiplier: 0.1,
    status: "observed",
    created_at: "2026-07-13T00:00:00Z",
  });
  assert.equal(decrease.direction, "decrease");
  assert.ok((decrease.delta || 0) < 0);
  assert.equal(normalizedUpstreamMultiplier(null, 0.1), null);
});

test("upstream recharge changes update the normalized upstream multiplier", () => {
  const log = {
    id: 4,
    sub2api_account_id: 45,
    old_group_multiplier: 2,
    new_group_multiplier: 2,
    old_upstream_recharge_multiplier: 0.1,
    new_upstream_recharge_multiplier: 0.2,
    upstream_recharge_multiplier: 0.2,
    status: "applied",
    created_at: "2026-07-14T00:00:00Z",
  };

  const recharge = upstreamRechargeRateChange(log);
  const normalized = upstreamRateChange(log);
  assert.deepEqual(
    { oldValue: recharge.oldValue, newValue: recharge.newValue, direction: recharge.direction },
    { oldValue: 0.1, newValue: 0.2, direction: "increase" },
  );
  assert.equal(normalized.oldValue, 0.2);
  assert.equal(normalized.newValue, 0.4);
  assert.equal(normalized.direction, "increase");
});

test("channel group changes expose first-seen and recharge-adjusted multipliers", () => {
  const added = upstreamGroupRatePresentation({
    id: 1,
    event_type: "group_added",
    new_value: 2,
    details: { new_recharge_multiplier: 0.25 },
    created_at: "2026-07-26T00:00:00Z",
    unread: true,
  });
  assert.equal(added.newGroupMultiplier, 2);
  assert.equal(added.newCompositeMultiplier, 0.5);
  assert.equal(added.showCompositeMultiplier, true);

  const changed = upstreamGroupRatePresentation({
    id: 2,
    event_type: "group_multiplier_changed",
    old_value: 2,
    new_value: 3,
    details: { old_recharge_multiplier: 0.25, new_recharge_multiplier: 0.25 },
    created_at: "2026-07-26T00:00:00Z",
    unread: false,
  });
  assert.equal(changed.oldCompositeMultiplier, 0.5);
  assert.equal(changed.newCompositeMultiplier, 0.75);

  const renamed = upstreamGroupRatePresentation({
    id: 4,
    event_type: "group_name_changed",
    old_value: 0.6,
    new_value: 0.6,
    details: { old_recharge_multiplier: 0.1, new_recharge_multiplier: 0.1 },
    created_at: "2026-07-26T00:00:00Z",
    unread: false,
  });
  assert.equal(renamed.newGroupMultiplier, 0.6);
  assert.equal(renamed.newCompositeMultiplier, 0.06);
  assert.equal(renamed.showCompositeMultiplier, true);

  const legacy = upstreamGroupRatePresentation({
    id: 3,
    event_type: "group_multiplier_changed",
    old_value: 1,
    new_value: 2,
    created_at: "2026-07-26T00:00:00Z",
    unread: false,
  }, 0.1);
  assert.equal(legacy.oldCompositeMultiplier, 0.1);
  assert.equal(legacy.newCompositeMultiplier, 0.2);
  assert.equal(legacy.showCompositeMultiplier, true);
});

test("upstream health transitions preserve unknown, invalid, and auto-disabled states", () => {
  const log = {
    id: 8,
    sub2api_account_id: 48,
    old_upstream_key_status: "available",
    new_upstream_key_status: "disabled",
    old_upstream_group_status: "available",
    new_upstream_group_status: "invalid",
    old_remote_schedulable: true,
    new_remote_schedulable: false,
    status: "observed",
    created_at: "2026-07-16T00:00:00Z",
  };
  assert.deepEqual(upstreamKeyStatusChange(log), {
    oldValue: "available",
    newValue: "disabled",
    direction: "changed",
  });
  assert.deepEqual(upstreamGroupStatusChange(log), {
    oldValue: "available",
    newValue: "invalid",
    direction: "changed",
  });
  assert.deepEqual(remoteSchedulableChange(log), {
    oldValue: "enabled",
    newValue: "disabled",
    direction: "changed",
  });
  assert.deepEqual(upstreamKeyStatusChange({
    id: 9,
    sub2api_account_id: 49,
    new_upstream_key_status: "unknown",
    status: "observed",
    created_at: "2026-07-16T00:00:00Z",
  }), {
    oldValue: null,
    newValue: "unknown",
    direction: "unknown",
  });
});

test("account billing presentation prefers a changed target over current readback", () => {
  const change = accountBillingRateChange({
    id: 5,
    sub2api_account_id: 46,
    old_target_rate: 1,
    new_target_rate: 2,
    old_current_rate: 1,
    new_current_rate: 1,
    status: "observed",
    created_at: "2026-07-14T00:00:00Z",
  });

  assert.equal(change.oldValue, 1);
  assert.equal(change.newValue, 2);
  assert.equal(change.direction, "increase");
});

test("upstream change log query includes cursor, inclusive date filters, and display time zone", () => {
  const path = upstreamChangeLogsPath(25, 80, {
    startDate: "2026-07-01",
    endDate: "2026-07-14",
    timeZone: "Asia/Shanghai",
  });

  const url = new URL(path, "http://localhost");
  assert.equal(url.pathname, "/api/upstream-accounts/upstream-change-logs");
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    limit: "25",
    before_id: "80",
    start_date: "2026-07-01",
    end_date: "2026-07-14",
    time_zone: "Asia/Shanghai",
  });
  assert.equal(
    new URL(upstreamRateChangeLogsPath(25, 80), "http://localhost").pathname,
    "/api/upstream-accounts/rate-change-logs",
  );
});

test("upstream usage history query encodes channel, date, account, and display time zone filters", () => {
  const path = upstreamUsageHistoryPath(42, {
    startDate: "2026-07-01",
    endDate: "2026-07-28",
    apiKeyAccountId: 88,
    timeZone: "Asia/Shanghai",
  });
  const url = new URL(path, "http://localhost");
  assert.equal(url.pathname, "/api/upstream-channels/42/usage-history");
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    start_date: "2026-07-01",
    end_date: "2026-07-28",
    api_key_account_id: "88",
    time_zone: "Asia/Shanghai",
  });
  assert.equal(upstreamUsageHistoryPath("channel / 1"), "/api/upstream-channels/channel%20%2F%201/usage-history");
});

test("key-filtered usage history displays the selected key usage rather than the channel aggregate", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(
    source,
    /function historyDayUsage\(day: UpstreamUsageHistory\["days"\]\[number\], selectedAccountId: string \| null\) \{\s+if \(!selectedAccountId\) return finiteNumber\(day\.balance_used\);\s+return finiteNumber\(usageHistoryDayAccount\(day, selectedAccountId\)\?\.upstream_usage\);\s+\}/,
  );
});

test("usage history net income uses recharge-adjusted cost in the revenue currency", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  assert.match(
    source,
    /function historyNetIncome\([^]*?const cost = finiteNumber\(value\?\.cost_adjusted\);/,
  );
});

test("upstream change API prefers the new ledger and falls back only when unavailable", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  const paths: string[] = [];
  let newEndpointAvailable = true;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const path = String(input);
    paths.push(path);
    if (path.includes("/upstream-change-logs") && !newEndpointAvailable) {
      return new Response(JSON.stringify({ detail: "Not Found" }), {
        headers: { "Content-Type": "application/json" },
        status: 404,
      });
    }
    return new Response("[]", { headers: { "Content-Type": "application/json" }, status: 200 });
  }) as typeof fetch;
  try {
    await api.upstreamChangeLogs(6);
    assert.match(paths[0], /\/upstream-change-logs\?/);
    assert.equal(paths.length, 1);

    newEndpointAvailable = false;
    paths.length = 0;
    await api.upstreamChangeLogs(6);
    assert.match(paths[0], /\/upstream-change-logs\?/);
    assert.match(paths[1], /\/rate-change-logs\?/);
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
});

test("separate change ledger APIs include filters, unread counts, and mark-read cursors", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  globalThis.window = {
    clearTimeout,
    dispatchEvent: () => true,
    setTimeout,
  } as unknown as Window & typeof globalThis;
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const path = String(input);
    calls.push({ path, init });
    const body = path.includes("change-log-unread-counts")
      ? { upstream_changes: 2, account_scheduling_changes: 3 }
      : path.includes("mark-read")
        ? { message: "ok" }
        : { items: [], unread_count: 0, last_read_id: 0 };
    return new Response(JSON.stringify(body), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    });
  }) as typeof fetch;
  try {
    await api.upstreamChannelChangeEvents(25, 80, {
      startDate: "2026-07-01",
      endDate: "2026-07-21",
      timeZone: "Asia/Shanghai",
    });
    await api.accountSchedulingChangeEvents(10);
    await api.changeLogUnreadCounts();
    await api.markUpstreamChannelChangesRead(91);
    await api.markAccountSchedulingChangesRead(92);

    const channelUrl = new URL(calls[0].path, "http://localhost");
    assert.equal(channelUrl.pathname, "/api/upstream-accounts/channel-change-events");
    assert.deepEqual(Object.fromEntries(channelUrl.searchParams), {
      limit: "25",
      before_id: "80",
      start_date: "2026-07-01",
      end_date: "2026-07-21",
      time_zone: "Asia/Shanghai",
    });
    assert.match(calls[1].path, /\/api\/upstream-accounts\/scheduling-change-events\?/);
    assert.equal(calls[2].path, "/api/upstream-accounts/change-log-unread-counts");
    assert.equal(calls[3].init?.method, "POST");
    assert.deepEqual(JSON.parse(String(calls[3].init?.body)), { through_id: 91 });
    assert.equal(calls[4].init?.method, "POST");
    assert.deepEqual(JSON.parse(String(calls[4].init?.body)), { through_id: 92 });
  } finally {
    globalThis.window = originalWindow;
    globalThis.fetch = originalFetch;
  }
});

class MemoryStorage {
  readonly values = new Map<string, string>();

  get length() {
    return this.values.size;
  }

  clear() {
    this.values.clear();
  }

  getItem(key: string) {
    return this.values.get(key) ?? null;
  }

  key(index: number) {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string) {
    this.values.delete(key);
  }

  setItem(key: string, value: string) {
    this.values.set(key, value);
  }
}
