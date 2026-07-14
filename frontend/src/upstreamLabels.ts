export function rateChangeReasonLabel(reason?: string | null) {
  const value = normalizedStatus(reason);
  return ({
    automatic_sync: "自动同步",
    channel_discovery: "渠道同步",
    group_changed: "上游分组变化",
    upstream_group_change: "上游分组变化",
    upstream_recharge_change: "上游充值成本变化",
    local_recharge_change: "本地充值成本变化",
    manual_fallback: "手动倍率回退",
    rate_changed: "计费倍率变化",
    target_recalculated: "目标倍率重算",
    rate_drift: "账号倍率偏离目标",
    settings_changed: "同步设置变化",
    observed: "仅记录观测",
    applied: "已应用账号倍率",
    apply_failed: "账号倍率应用失败",
    skipped: "已跳过应用",
  } as Record<string, string>)[value] || reason || "倍率同步";
}

export function upstreamStatusLabel(status: string) {
  return ({
    ok: "正常",
    success: "正常",
    active: "启用",
    inactive: "停用",
    enabled: "启用",
    disabled: "停用",
    error: "失败",
    failed: "失败",
    invalid: "无效",
    unavailable: "不可用",
    unsupported: "不支持",
    missing: "缺少配置",
    missing_credentials: "缺少凭据",
    credentials_missing: "未绑定凭据",
    undiscovered: "待探测",
    not_discovered: "待探测",
    not_checked: "未检查",
    not_ready: "不可计算",
    pending: "待处理",
    pending_apply: "待同步",
    observed: "已观测",
    applied: "已应用",
    apply_failed: "应用失败",
    synced: "已同步",
    unchanged: "无变化",
    skipped: "已跳过",
    stale: "上次结果",
    in_sync: "已一致",
    discovered: "已探测",
    discovery_failed: "探测失败",
    fallback_manual: "手动回退",
    group_rate_unavailable: "上游分组倍率不可用",
    manual: "手动",
    default: "默认值",
    managed: "已托管",
    unmanaged: "未托管",
    auto: "自动识别",
    newapi: "NewAPI",
    sub2api: "Sub2API",
    openai: "OpenAI",
    anthropic: "Anthropic",
    api_key: "API Key",
    unknown: "待确认",
  } as Record<string, string>)[normalizedStatus(status)] || status;
}

export function upstreamStatusTone(status: string) {
  const value = normalizedStatus(status);
  if (["ok", "success", "active", "enabled", "in_sync", "managed", "applied", "synced", "unchanged"].includes(value)) return "success";
  if (value === "unmanaged" || value === "unknown") return "muted";
  if (value === "inactive" || value === "apply_failed") return "danger";
  if (/(error|fail|invalid|unavailable|unsupported|missing|disabled|blocked|denied)/.test(value)) return "danger";
  if (/(pending|stale|undiscovered|not_discovered|not_checked|not_ready|fallback_manual|manual|default|skipped)/.test(value)) return "warn";
  if (/(auto|observed|discovered|newapi|sub2api|openai|anthropic|api_key)/.test(value)) return "info";
  return "muted";
}

function normalizedStatus(value?: string | null) {
  return String(value || "").trim().toLowerCase();
}
