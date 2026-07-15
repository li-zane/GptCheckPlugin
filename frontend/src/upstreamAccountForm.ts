import type { UpstreamAccount, UpstreamAccountUpdate } from "./types";

const manualMultiplierStates = new Set(["manual", "fallback_manual"]);
const unavailableMultiplierStates = new Set([
  "base_url_missing",
  "credentials_missing",
  "discovery_failed",
  "group_rate_unavailable",
  "group_selection_missing",
  "invalid",
  "not_ready",
  "unavailable",
]);

export function canSetManualMultiplier(account: UpstreamAccount) {
  const source = normalizedText(account.group_multiplier_source);
  const status = normalizedText(account.group_multiplier_status);
  return (
    manualMultiplierStates.has(source) ||
    manualMultiplierStates.has(status) ||
    unavailableMultiplierStates.has(status) ||
    finiteNumber(account.effective_group_multiplier) === null
  );
}

export function buildUpstreamAccountUpdatePayload({
  account,
  apiKey,
  channelId,
  manualGroupMultiplier,
}: {
  account: UpstreamAccount;
  apiKey: string;
  channelId: number | string | null;
  manualGroupMultiplier: string;
}): UpstreamAccountUpdate {
  const payload: UpstreamAccountUpdate = {
    channel_id: channelId,
    expected_identity_fingerprint: expectedIdentityFingerprint(account),
  };
  if (canSetManualMultiplier(account)) {
    payload.manual_group_multiplier = optionalPositiveNumber(manualGroupMultiplier, "手动分组倍率");
  }
  if (apiKey.trim()) payload.api_key = apiKey.trim();
  return payload;
}

export function expectedIdentityFingerprint(account: UpstreamAccount) {
  const fingerprint = String(account.identity_fingerprint || "").trim();
  if (!/^[a-f0-9]{64}$/.test(fingerprint)) {
    throw new Error("账号身份校验信息已过期，请刷新 API Key 账号列表后重试。");
  }
  return fingerprint;
}

function optionalPositiveNumber(value: string, label: string) {
  if (!value.trim()) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new Error(label + "必须大于 0");
  return number;
}

function normalizedText(value: unknown) {
  return String(value || "").trim().toLowerCase();
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}
