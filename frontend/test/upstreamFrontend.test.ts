import assert from "node:assert/strict";
import test from "node:test";

import {
  api,
  NO_FRONTEND_TIMEOUT,
  upstreamRateChangeLogsPath,
} from "../src/api.ts";
import {
  accountCanBeLivenessTested,
  livenessAccountIds,
  MAX_LIVENESS_ACCOUNTS,
} from "../src/accountLiveness.ts";
import {
  buildUpstreamAccountUpdatePayload,
  canSetManualMultiplier,
} from "../src/upstreamAccountForm.ts";
import { channelCredentialBindingChanged } from "../src/upstreamCredentialBinding.ts";
import {
  clearUpstreamOverviewCache,
  readUpstreamOverviewCache,
  sanitizeUpstreamOverview,
  upstreamOverviewCacheKey,
  writeUpstreamOverviewCache,
} from "../src/upstreamOverviewCache.ts";
import { rateChangeReasonLabel, upstreamStatusLabel } from "../src/upstreamLabels.ts";
import {
  accountBillingRateChange,
  normalizedUpstreamMultiplier,
  upstreamRateChange,
  upstreamRechargeRateChange,
} from "../src/upstreamRatePresentation.ts";
import {
  apiAccountSyncMessage,
  accountRateStatusLabel,
  channelDiscoveryErrorMessage,
  channelDiscoverySuccessMessage,
  upstreamDiscoveryCopy,
  upstreamMutationControlsDisabled,
} from "../src/upstreamSyncPresentation.ts";
import { sortUsageLimitSamples } from "../src/usageSampleSort.ts";
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
    channels: [{
      id: 2,
      display_name: "上游",
      access_token: "secret-access",
      refresh_token: "secret-refresh",
      access_token_set: true,
      accounts: [{
        sub2api_account_id: 10,
        remote_name: "账号",
        remote_platform: "anthropic",
        api_key: "secret-key",
        encrypted_api_key: "secret-ciphertext",
        api_key_hint: "key-tail",
        api_key_set: true,
      }],
    }],
    unassigned_accounts: [],
  };
  const safe = sanitizeUpstreamOverview(unsafe);
  assert.ok(safe);
  assert.equal(safe.channels[0].access_token_set, true);
  assert.equal(safe.channels[0].accounts?.[0].api_key_set, true);
  assert.equal(safe.channels[0].accounts?.[0].api_key_hint, undefined);
  assert.equal(safe.channels[0].accounts?.[0].remote_platform, "anthropic");

  const serialized = JSON.stringify(safe);
  assert.doesNotMatch(serialized, /secret-access|secret-refresh|secret-key|secret-ciphertext|key-tail/);
  assert.doesNotMatch(serialized, /"access_token"|"refresh_token"|"api_key"|"encrypted_api_key"|"api_key_hint"/);
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

test("rate change reasons and statuses use user-facing Chinese labels", () => {
  assert.equal(rateChangeReasonLabel("upstream_group_change"), "上游分组变化");
  assert.equal(rateChangeReasonLabel("upstream_recharge_change"), "上游充值成本变化");
  assert.equal(rateChangeReasonLabel("local_recharge_change"), "本地充值成本变化");
  assert.equal(rateChangeReasonLabel("target_recalculated"), "目标倍率重算");
  assert.equal(rateChangeReasonLabel("rate_drift"), "账号倍率偏离目标");
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

test("rate log query includes cursor, inclusive date filters, and display time zone", () => {
  const path = upstreamRateChangeLogsPath(25, 80, {
    startDate: "2026-07-01",
    endDate: "2026-07-14",
    timeZone: "Asia/Shanghai",
  });

  const url = new URL(path, "http://localhost");
  assert.equal(url.pathname, "/api/upstream-accounts/rate-change-logs");
  assert.deepEqual(Object.fromEntries(url.searchParams), {
    limit: "25",
    before_id: "80",
    start_date: "2026-07-01",
    end_date: "2026-07-14",
    time_zone: "Asia/Shanghai",
  });
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
