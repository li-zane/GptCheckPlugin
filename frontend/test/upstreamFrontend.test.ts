import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  api,
  NO_FRONTEND_TIMEOUT,
  upstreamLegacyBindingCounts,
  upstreamChangeLogsPath,
  upstreamRateChangeLogsPath,
} from "../src/api.ts";
import {
  accountCanBeLivenessTested,
  livenessAccountIds,
  MAX_LIVENESS_ACCOUNTS,
} from "../src/accountLiveness.ts";
import { accountFilterFacetCandidates } from "../src/accountFilterFacets.ts";
import {
  buildUpstreamAccountUpdatePayload,
  canSetManualMultiplier,
} from "../src/upstreamAccountForm.ts";
import { channelCredentialBindingChanged } from "../src/upstreamCredentialBinding.ts";
import {
  eventDurationBreakdown,
  eventDurationMs,
  formatElapsedDuration,
  timestampDurationMs,
} from "../src/durationPresentation.ts";
import { oauthUsageBackgroundRefreshIntervals } from "../src/oauthUsageRefresh.ts";
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
} from "../src/upstreamLabels.ts";
import {
  accountBillingRateChange,
  normalizedUpstreamMultiplier,
  remoteSchedulableChange,
  upstreamGroupStatusChange,
  upstreamKeyStatusChange,
  upstreamRateChange,
  upstreamRechargeRateChange,
} from "../src/upstreamRatePresentation.ts";
import {
  accountCompositeMultiplier,
  filterUpstreamAccountEntries,
  flattenUpstreamAccounts,
  priorityIntervalAssignmentBlocked,
  priorityIntervalAssignmentNeedsConfirmation,
  sortUpstreamAccountEntries,
  upstreamAccountMatchesStatus,
  upstreamAccountPlatforms,
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
import type { UpstreamChannelsResponse, UsageLimitSample } from "../src/types.ts";

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
  assert.match(source, /window\.history\.pushState\(null, "", nextPath\)/);
  assert.match(source, /window\.addEventListener\("popstate", handlePopState\)/);
  assert.match(source, /window\.history\.replaceState\(null, "", canonicalPath\)/);
  assert.match(source, /window\.location\.reload\(\)/);
  assert.match(source, /form="runtime-settings-form"/);
  assert.match(source, /id="runtime-settings-form"/);
  assert.equal((source.match(/保存设置/g) || []).length, 1);
  const refreshButton = source.indexOf('aria-label="刷新页面"');
  const settingsButton = source.indexOf('className="primary-button toolbar-settings-save"');
  const firstSyncButton = source.indexOf("<ToolbarTimeButton", refreshButton);
  assert.ok(refreshButton >= 0 && settingsButton > refreshButton && settingsButton < firstSyncButton);
  assert.match(source, /disabled=\{syncBusy \|\| settingsFormInvalid\}/);
  const viteConfig = readFileSync(new URL("../vite.config.ts", import.meta.url), "utf8");
  assert.match(viteConfig, /"\^\/api\(\?:\/\|\$\)"/);
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
  assert.match(source, /middleEllipsis\(displayCanonicalUrl\(url\)\)/);
  assert.match(source, /\[yesterdayUsage, todayUsage\]\.map/);
  assert.match(source, /className="api-key-channel-stat--recharge"/);
  assert.match(styles, /\.api-key-channel-urls\s*\{[^}]*flex-wrap: nowrap !important;[^}]*overflow: hidden;/s);
  assert.match(styles, /\.api-key-channel-stats\s*\{[^}]*grid-template-columns: minmax\(0, 1\.65fr\) minmax\(110px, 0\.62fr\)/s);
  assert.match(styles, /\.api-key-channel-daily-usage\s*\{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/s);
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

test("API key account numbers stay beside the account name", () => {
  const source = readFileSync(new URL("../src/ApiKeyAccountsView.tsx", import.meta.url), "utf8");
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
  const nameIndex = source.indexOf('<div className="api-key-account-name">');
  const numberIndex = source.indexOf('<span className="api-key-mono">#{account.sub2api_account_id}</span>', nameIndex);
  const statusIndex = source.indexOf('<StatusChip status={effectiveStatus} />', nameIndex);
  assert.ok(nameIndex >= 0 && numberIndex > nameIndex && statusIndex > numberIndex);
  assert.match(source, /<div className="api-key-account-side-chips">\s*<StatusChip status=\{effectiveStatus\}/s);
  assert.match(styles, /\.api-key-account-name\s*\{[^}]*display: flex;[^}]*gap: 5px;/s);
  assert.match(styles, /\.api-key-account-name > strong\s*\{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;/s);
  assert.match(styles, /\.api-key-account-side-chips\s*\{[^}]*margin-left: auto;/s);
  assert.match(styles, /\.api-key-account-priority\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\);/s);
  assert.match(styles, /\.api-key-account-priority-value\s*\{[^}]*border-left: 0;[^}]*border-top: 1px solid var\(--line\);/s);
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
    { path: "/api/upstream-accounts/priority-intervals/rebalance", method: "POST" },
  ]);
  assert.deepEqual(requests[3].body, {
    priority_interval_id: 3,
    expected_identity_fingerprint: fingerprint,
    confirm_identity_rebind: true,
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
      interval: "7",
      platform: "openai",
      query: "渠道甲",
    })).map(({ account }) => account.sub2api_account_id),
    [3, 1],
  );
  assert.deepEqual(
    filterUpstreamAccountEntries(entries, {
      interval: "unassigned",
      platform: "anthropic",
      query: "",
    }).map(({ account }) => account.sub2api_account_id),
    [2],
  );
  assert.deepEqual(
    filterUpstreamAccountEntries(entries, {
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
    await api.syncApiKeyAccounts(overview, confirmLegacyBindings);
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
        composite_multiplier: 0.2,
        upstream_usage_amount: 12.375,
        upstream_usage_unit: "USD",
        upstream_usage_checked_at: "2026-07-16T08:01:30Z",
        upstream_key_status: "disabled",
        upstream_group_status: "invalid",
        upstream_health_invalid_count: 2,
        upstream_health_checked_at: "2026-07-16T08:00:00Z",
        upstream_key_checked_at: "2026-07-16T08:00:10Z",
        upstream_group_checked_at: "2026-07-16T08:00:20Z",
        auto_disabled_reason: "Upstream key is disabled.",
        last_auto_disabled_at: "2026-07-16T08:01:00Z",
      }],
    }],
    unassigned_accounts: [],
  };
  const safe = sanitizeUpstreamOverview(unsafe);
  assert.ok(safe);
  assert.equal(safe.channels[0].access_token_set, true);
  assert.equal(safe.channels[0].account_count, 1);
  assert.equal(safe.channels[0].probe_enabled, false);
  assert.equal(safe.channels[0].today_balance_used, 3.25);
  assert.equal(safe.channels[0].today_balance_status, "ok");
  assert.equal(safe.channels[0].yesterday_balance_used, 2.75);
  assert.equal(safe.channels[0].yesterday_balance_status, "ok");
  assert.equal(safe.channels[0].accounts?.[0].api_key_set, true);
  assert.equal(safe.channels[0].accounts?.[0].api_key_hint, undefined);
  assert.equal(safe.channels[0].accounts?.[0].identity_fingerprint, undefined);
  assert.equal(safe.channels[0].accounts?.[0].remote_platform, "anthropic");
  assert.equal(safe.channels[0].accounts?.[0].priority_interval_id, 4);
  assert.equal(safe.channels[0].accounts?.[0].composite_multiplier, 0.2);
  assert.equal(safe.channels[0].accounts?.[0].upstream_usage_amount, 12.375);
  assert.equal(safe.channels[0].accounts?.[0].upstream_usage_unit, "USD");
  assert.equal(safe.channels[0].accounts?.[0].upstream_key_status, "disabled");
  assert.equal(safe.channels[0].accounts?.[0].upstream_group_status, "invalid");
  assert.equal(safe.channels[0].accounts?.[0].upstream_health_invalid_count, 2);
  assert.equal(safe.channels[0].accounts?.[0].upstream_health_checked_at, "2026-07-16T08:00:00Z");
  assert.equal(safe.channels[0].accounts?.[0].upstream_key_checked_at, "2026-07-16T08:00:10Z");
  assert.equal(safe.channels[0].accounts?.[0].upstream_group_checked_at, "2026-07-16T08:00:20Z");
  assert.equal(safe.channels[0].accounts?.[0].auto_disabled_reason, "Upstream key is disabled.");
  assert.equal(safe.channels[0].accounts?.[0].last_auto_disabled_at, "2026-07-16T08:01:00Z");
  assert.deepEqual(safe.priority_intervals, [{
    id: 4,
    name: "低成本",
    start_priority: 40,
    end_priority: 70,
    step: 2,
    account_count: 1,
    effective_step: 2,
  }]);

  const serialized = JSON.stringify(safe);
  assert.doesNotMatch(serialized, /secret-access|secret-refresh|secret-key|secret-ciphertext|key-tail/);
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
  assert.equal(upstreamChangeReasonLabel("upstream_group_invalid"), "上游分组已失效");
  assert.equal(upstreamChangeReasonLabel("account_auto_disabled"), "账号已自动禁用");
  assert.equal(upstreamChangeReasonLabel("upstream_auto_disable"), "上游失效后自动禁用");
  assert.equal(upstreamChangeReasonLabel("upstream_key_recovered"), "上游 Key 恢复可用");
  assert.equal(upstreamChangeReasonLabel("upstream_group_recovered"), "上游分组恢复可用");
  assert.equal(upstreamHealthStatusLabel("key", "disabled"), "已禁用");
  assert.equal(upstreamHealthStatusLabel("group", "invalid"), "已失效");
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
