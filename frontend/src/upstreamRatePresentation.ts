import type { UpstreamChangeLog, UpstreamChannelChangeEvent } from "./types";

export type RateChangeDirection = "increase" | "decrease" | "unchanged" | "unknown";

export type UpstreamRateChange = {
  oldValue: number | null;
  newValue: number | null;
  delta: number | null;
  direction: RateChangeDirection;
};

export type UpstreamStateChange = {
  oldValue: string | null;
  newValue: string | null;
  direction: "changed" | "unchanged" | "unknown";
};

export type UpstreamGroupRatePresentation = {
  oldGroupMultiplier: number | null;
  newGroupMultiplier: number | null;
  oldCompositeMultiplier: number | null;
  newCompositeMultiplier: number | null;
  rechargeMultiplier: number | null;
  showCompositeMultiplier: boolean;
};

/** Converts an upstream group rate to the 1 CNY : 1 USD comparison basis. */
export function normalizedUpstreamMultiplier(groupMultiplier: unknown, rechargeMultiplier: unknown) {
  const group = finiteNumber(groupMultiplier);
  const recharge = finiteNumber(rechargeMultiplier);
  return group === null || recharge === null ? null : group * recharge;
}

export function multiplierChange(oldValue: unknown, newValue: unknown): UpstreamRateChange {
  const oldNumber = finiteNumber(oldValue);
  const newNumber = finiteNumber(newValue);
  if (oldNumber === null || newNumber === null) {
    return { oldValue: oldNumber, newValue: newNumber, delta: null, direction: "unknown" };
  }

  const delta = newNumber - oldNumber;
  const tolerance = Math.max(1e-12, Math.max(Math.abs(oldNumber), Math.abs(newNumber)) * 1e-10);
  if (Math.abs(delta) <= tolerance) {
    return { oldValue: oldNumber, newValue: newNumber, delta: 0, direction: "unchanged" };
  }
  return {
    oldValue: oldNumber,
    newValue: newNumber,
    delta,
    direction: delta > 0 ? "increase" : "decrease",
  };
}

export function upstreamGroupRatePresentation(
  log: UpstreamChannelChangeEvent,
  fallbackRechargeMultiplier?: unknown,
): UpstreamGroupRatePresentation {
  const oldGroupMultiplier = finiteNumber(log.old_value);
  const newGroupMultiplier = finiteNumber(log.new_value);
  const oldRechargeMultiplier = finiteNumber(log.details?.old_recharge_multiplier)
    ?? finiteNumber(log.details?.recharge_multiplier)
    ?? finiteNumber(fallbackRechargeMultiplier);
  const newRechargeMultiplier = finiteNumber(log.details?.new_recharge_multiplier)
    ?? finiteNumber(log.details?.recharge_multiplier)
    ?? finiteNumber(fallbackRechargeMultiplier);
  const rechargeMultiplier = newRechargeMultiplier ?? oldRechargeMultiplier;
  const showCompositeMultiplier = [oldRechargeMultiplier, newRechargeMultiplier]
    .some((value) => value !== null && Math.abs(value - 1) > 1e-12);

  return {
    oldGroupMultiplier,
    newGroupMultiplier,
    oldCompositeMultiplier: normalizedUpstreamMultiplier(
      oldGroupMultiplier,
      oldRechargeMultiplier,
    ),
    newCompositeMultiplier: normalizedUpstreamMultiplier(
      newGroupMultiplier,
      newRechargeMultiplier,
    ),
    rechargeMultiplier,
    showCompositeMultiplier,
  };
}

/**
 * New logs persist normalized values directly. Older logs are still useful, so
 * derive the same values from their group and recharge fields when necessary.
 */
export function upstreamRateChange(log: UpstreamChangeLog): UpstreamRateChange {
  const oldRechargeMultiplier = finiteNumber(log.old_upstream_recharge_multiplier)
    ?? finiteNumber(log.upstream_recharge_multiplier);
  const newRechargeMultiplier = finiteNumber(log.new_upstream_recharge_multiplier)
    ?? finiteNumber(log.upstream_recharge_multiplier);
  const oldValue = finiteNumber(log.old_upstream_multiplier)
    ?? normalizedUpstreamMultiplier(log.old_group_multiplier, oldRechargeMultiplier);
  const newValue = finiteNumber(log.new_upstream_multiplier)
    ?? normalizedUpstreamMultiplier(log.new_group_multiplier, newRechargeMultiplier);

  return multiplierChange(oldValue, newValue);
}

export function groupRateChange(log: UpstreamChangeLog) {
  return multiplierChange(log.old_group_multiplier, log.new_group_multiplier);
}

export function accountBillingRateChange(log: UpstreamChangeLog) {
  const target = multiplierChange(log.old_target_rate, log.new_target_rate);
  if (target.direction === "increase" || target.direction === "decrease") return target;
  const current = multiplierChange(log.old_current_rate, log.new_current_rate);
  if (current.direction === "increase" || current.direction === "decrease") return current;
  return target.newValue !== null || target.oldValue !== null ? target : current;
}

export function upstreamRechargeRateChange(log: UpstreamChangeLog) {
  return multiplierChange(
    log.old_upstream_recharge_multiplier,
    log.new_upstream_recharge_multiplier,
  );
}

export function upstreamKeyStatusChange(log: UpstreamChangeLog) {
  return stateChange(log.old_upstream_key_status, log.new_upstream_key_status);
}

export function upstreamGroupStatusChange(log: UpstreamChangeLog) {
  return stateChange(log.old_upstream_group_status, log.new_upstream_group_status);
}

export function remoteSchedulableChange(log: UpstreamChangeLog) {
  return stateChange(
    schedulableStatus(log.old_remote_schedulable),
    schedulableStatus(log.new_remote_schedulable),
  );
}

export function upstreamChangeSummary(log: UpstreamChangeLog) {
  const changes: string[] = [];
  const oldGroupLabel = normalizedText(log.old_group_name) || normalizedText(log.old_group_id);
  const newGroupLabel = normalizedText(log.new_group_name) || normalizedText(log.new_group_id);
  if (oldGroupLabel !== null && newGroupLabel !== null && oldGroupLabel !== newGroupLabel) {
    changes.push(`上游分组名称 ${oldGroupLabel} -> ${newGroupLabel}`);
  }
  const groupRate = groupRateChange(log);
  if (groupRate.direction === "increase" || groupRate.direction === "decrease") {
    changes.push("上游分组倍率变化");
  }
  const groupStatus = upstreamGroupStatusChange(log);
  if (groupStatus.direction === "changed") {
    const next = groupStatus.newValue || "unknown";
    changes.push(isAvailableStatus(next) ? "上游分组恢复可用" : "上游分组不可用");
  }
  if (upstreamKeyStatusChange(log).direction === "changed") changes.push("上游 Key 状态变化");
  if (upstreamRechargeRateChange(log).direction === "increase" || upstreamRechargeRateChange(log).direction === "decrease") {
    changes.push("上游充值倍率变化");
  }
  if (remoteSchedulableChange(log).direction === "changed") changes.push("账号调度状态变化");
  if (accountBillingRateChange(log).direction === "increase" || accountBillingRateChange(log).direction === "decrease") {
    changes.push("账号计费倍率变化");
  }
  return [...new Set(changes)].join("；");
}

export function stateChange(oldValue: unknown, newValue: unknown): UpstreamStateChange {
  const oldStatus = normalizedState(oldValue);
  const newStatus = normalizedState(newValue);
  if (oldStatus === null || newStatus === null) {
    return { oldValue: oldStatus, newValue: newStatus, direction: "unknown" };
  }
  return {
    oldValue: oldStatus,
    newValue: newStatus,
    direction: oldStatus === newStatus ? "unchanged" : "changed",
  };
}

function finiteNumber(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizedState(value: unknown) {
  if (value === null || value === undefined) return null;
  const normalized = String(value).trim().toLowerCase();
  return normalized || null;
}

function normalizedText(value: unknown) {
  if (value === null || value === undefined) return null;
  const normalized = String(value).trim();
  return normalized || null;
}

function isAvailableStatus(value: string) {
  return ["active", "available", "enabled", "ok", "success", "usable", "valid"].includes(value);
}

function schedulableStatus(value: boolean | null | undefined) {
  if (value === null || value === undefined) return null;
  return value ? "enabled" : "disabled";
}
