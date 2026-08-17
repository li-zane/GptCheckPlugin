import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { createAppQueryClient } from "../src/app/queryClient.ts";
import { clearSessionStorageCaches } from "../src/app/queryInvalidation.ts";
import { mutationInvalidationMatrix, queryKeys } from "../src/app/queryKeys.ts";
import { cx } from "../src/shared/lib/cx.ts";

test("query client preserves explicit refresh and retry semantics", () => {
  const client = createAppQueryClient();
  const defaults = client.getDefaultOptions();
  assert.equal(defaults.queries?.retry, false);
  assert.equal(defaults.queries?.refetchOnWindowFocus, false);
  assert.equal(defaults.queries?.refetchOnReconnect, false);
  assert.equal(defaults.mutations?.retry, false);
});

test("query keys isolate complete management-site scopes", () => {
  const first = queryKeys.upstreams("https://one.example/api/v1");
  const second = queryKeys.upstreams("https://one.example/api/v2");
  assert.deepEqual(first, ["upstreams", "https://one.example/api/v1"]);
  assert.notDeepEqual(first, second);
  assert.deepEqual(queryKeys.changeLogs("account_rate"), ["changeLogs", "account_rate"]);
});

test("mutation invalidation matrix stays domain-specific", () => {
  assert.deepEqual(mutationInvalidationMatrix.account, [
    ["accounts"], ["summary"], ["usage"], ["history"],
  ]);
  assert.deepEqual(mutationInvalidationMatrix.upstream, [["upstreams"], ["changeLogs"]]);
  assert.deepEqual(mutationInvalidationMatrix.settings, [["settings"]]);
});

test("session cache cleanup removes only frontend display snapshots", () => {
  const values = new Map([
    ["sub2api-at-upstream-overview:v9:https%3A%2F%2Fexample.com", "upstream"],
    ["sub2api-at-change-log:v3:https%3A%2F%2Fexample.com:upstream", "logs"],
    ["unrelated", "keep"],
  ]);
  const storage = {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: (key: string) => values.get(key) ?? null,
    key: (index: number) => [...values.keys()][index] ?? null,
    removeItem: (key: string) => { values.delete(key); },
    setItem: (key: string, value: string) => { values.set(key, value); },
  } satisfies Storage;
  clearSessionStorageCaches(storage);
  assert.equal(storage.getItem("sub2api-at-upstream-overview:v9:https%3A%2F%2Fexample.com"), null);
  assert.equal(storage.getItem("sub2api-at-change-log:v3:https%3A%2F%2Fexample.com:upstream"), null);
  assert.equal(storage.getItem("unrelated"), "keep");
});

test("cx joins module classes without adding a dependency", () => {
  assert.equal(cx("base", false, undefined, "active", null), "base active");
});

test("dashboard pages own their render entry and no longer depend on LegacyPage", () => {
  const pageFiles = [
    "accounts/AccountsPage.tsx",
    "overview/OverviewPage.tsx",
    "usage/UsagePage.tsx",
    "usage-samples/UsageSamplesPage.tsx",
    "mailboxes/MailboxesPage.tsx",
    "phones/PhonesPage.tsx",
    "history/HistoryPage.tsx",
    "settings/SettingsPage.tsx",
    "api-keys/UpstreamsPage.tsx",
    "api-keys/AccountsPage.tsx",
    "api-keys/PriorityIntervalsPage.tsx",
    "api-keys/UpstreamChangesPage.tsx",
    "api-keys/AccountRateChangesPage.tsx",
    "api-keys/SchedulingChangesPage.tsx",
  ];
  for (const file of pageFiles) {
    const source = readFileSync(new URL(`../src/pages/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /LegacyPage/);
  }
  const shell = readFileSync(new URL("../src/app/layout/AppShell.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(shell, /page\s*:/);
  assert.match(shell, /<Outlet \/>/);
  const controller = readFileSync(new URL("../src/app/dashboard/DashboardController.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(controller, /LegacyApp/);
});

test("legacy styles are an explicit migration boundary", () => {
  const main = readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8");
  const legacyStyles = readFileSync(new URL("../src/shared/styles/legacy.css", import.meta.url), "utf8");
  assert.match(main, /shared\/styles\/legacy\.css/);
  assert.match(legacyStyles, /@import ["']\.\.\/\.\.\/features\/legacy\/legacy\.css["']/);
});
