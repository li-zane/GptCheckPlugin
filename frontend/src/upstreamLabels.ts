export function upstreamChangeReasonLabel(reason?: string | null) {
  const value = normalizedStatus(reason);
  return ({
    automatic_sync: "自动同步",
    upstream_discovery: "上游同步",
    group_changed: "上游分组变化",
    upstream_group_change: "上游分组变化",
    upstream_recharge_change: "上游充值倍率变化",
    management_recharge_change: "管理站点充值倍率变化",
    manual_fallback: "手动倍率回退",
    rate_changed: "计费倍率变化",
    expected_billing_multiplier_recalculated: "预期账号计费倍率重算",
    rate_drift: "账号计费倍率不一致",
    upstream_key_status_change: "上游 Key 状态变化",
    upstream_key_status_changed: "上游 Key 状态变化",
    upstream_key_change: "上游 Key 状态变化",
    upstream_key_disabled: "上游 Key 已禁用",
    upstream_key_unavailable: "上游 Key 不可用",
    upstream_key_restored: "上游 Key 恢复可用",
    upstream_group_status_change: "上游分组状态变化",
    upstream_group_status_changed: "上游分组状态变化",
    upstream_group_availability_change: "上游分组状态变化",
    upstream_group_invalid: "上游分组已删除",
    upstream_group_unavailable: "上游分组不可用",
    upstream_group_restored: "上游分组恢复可用",
    negative_balance: "上游余额不足",
    account_auto_disabled: "账号已自动禁用",
    auto_disable: "账号已自动禁用",
    upstream_auto_disable: "上游失效后自动禁用",
    upstream_balance_negative: "上游余额低于阈值",
    upstream_monitor_unavailable: "上游监控与回退测试不可用",
    upstream_rate_increase: "上游实际倍率上涨",
    upstream_key_recovered: "上游 Key 恢复可用",
    upstream_group_recovered: "上游分组恢复可用",
    remote_schedulable_change: "账号调度状态变化",
    upstream_health_change: "上游可用性变化",
    auto_disabled_upstream_unavailable: "上游失效后自动禁用",
    settings_changed: "同步设置变化",
    observed: "仅记录观测",
    applied: "已应用账号倍率",
    apply_failed: "账号倍率应用失败",
    skipped: "已跳过应用",
  } as Record<string, string>)[value] || reason || "上游同步";
}

export type UpstreamHealthKind = "key" | "group" | "account";

export function upstreamHealthStatusLabel(kind: UpstreamHealthKind, status?: string | null) {
  const value = normalizedStatus(status);
  if (!value) return "未记录";
  const common = ({
    unknown: "未确认",
    not_checked: "未检查",
    not_configured: "未配置",
    unavailable: "不可用",
    deleted: "已删除",
    removed: "已删除",
    absent: "已删除",
    not_found: "未找到",
    expired: "已过期",
    quota_exhausted: "额度耗尽",
    unassigned: "未分配",
    account_disabled: "账号已禁用",
    already_disabled: "账号已停用",
    disable_failed: "禁用失败",
    missing: "未找到",
  } as Record<string, string>)[value];
  if (common) return common;
  if (kind === "group") {
    return ({
      available: "有效",
      valid: "有效",
      active: "有效",
      invalid: "已失效",
      disabled: "已失效",
      group_missing: "已失效",
      deleted: "已删除",
      removed: "已删除",
      absent: "已删除",
    } as Record<string, string>)[value] || upstreamStatusLabel(value);
  }
  if (kind === "key") {
    return ({
      available: "可用",
      usable: "可用",
      active: "可用",
      enabled: "可用",
      invalid: "无效",
      disabled: "已禁用",
      key_missing: "未找到",
    } as Record<string, string>)[value] || upstreamStatusLabel(value);
  }
  return ({
    available: "已启用",
    active: "已启用",
    enabled: "已启用",
    disabled: "已禁用",
    inactive: "已禁用",
  } as Record<string, string>)[value] || upstreamStatusLabel(value);
}

export function upstreamStatusLabel(status: string) {
  return ({
    ok: "正常",
    success: "正常",
    healthy: "正常",
    operational: "可用",
    degraded: "降级",
    timeout: "超时",
    estimated: "估算值",
    active: "启用",
    inactive: "停用",
    enabled: "启用",
    disabled: "停用",
    error: "失败",
    failed: "失败",
    invalid: "无效",
    unavailable: "不可用",
    deleted: "已删除",
    removed: "已删除",
    absent: "已删除",
    available: "可用",
    usable: "可用",
    valid: "有效",
    not_found: "未找到",
    expired: "已过期",
    quota_exhausted: "额度耗尽",
    unassigned: "未分配",
    account_disabled: "账号已禁用",
    already_disabled: "账号已停用",
    disable_failed: "禁用失败",
    unsupported: "不支持",
    missing: "缺少配置",
    missing_credentials: "缺少凭据",
    credentials_missing: "未绑定凭据",
    token_invalid: "Token 失效",
    undiscovered: "待探测",
    not_discovered: "待探测",
    not_checked: "未检查",
    not_configured: "未配置",
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
    gemini: "Gemini",
    google: "Google",
    xai: "xAI",
    grok: "Grok",
    deepseek: "DeepSeek",
    api_key: "API Key",
    unknown: "待确认",
  } as Record<string, string>)[normalizedStatus(status)] || status;
}

export function upstreamStatusTone(status: string) {
  const value = normalizedStatus(status);
  if (["ok", "success", "healthy", "operational", "active", "available", "usable", "valid", "enabled", "in_sync", "managed", "applied", "synced", "unchanged"].includes(value)) return "success";
  if (value === "unmanaged" || value === "unknown") return "muted";
  if (value === "inactive" || value === "apply_failed" || value === "timeout") return "danger";
  if (/(error|fail|invalid|unavailable|unsupported|missing|not[_-]?found|disabled|expired|exhausted|unassigned|blocked|denied|token_invalid|deleted|removed|absent)/.test(value)) return "danger";
  if (/(pending|stale|degraded|estimated|undiscovered|not_discovered|not_checked|not_configured|not_ready|fallback_manual|manual|default|skipped)/.test(value)) return "warn";
  if (/(auto|observed|discovered|newapi|sub2api|openai|anthropic|api_key)/.test(value)) return "info";
  return "muted";
}

function normalizedStatus(value?: string | null) {
  return String(value || "").trim().toLowerCase();
}
