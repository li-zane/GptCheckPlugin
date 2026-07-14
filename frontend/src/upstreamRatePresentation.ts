import type { UpstreamRateChangeLog } from "./types";

export type RateChangeDirection = "increase" | "decrease" | "unchanged" | "unknown";

export type UpstreamRateChange = {
  oldValue: number | null;
  newValue: number | null;
  delta: number | null;
  direction: RateChangeDirection;
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

/**
 * New logs persist normalized values directly. Older logs are still useful, so
 * derive the same values from their group and recharge fields when necessary.
 */
export function upstreamRateChange(log: UpstreamRateChangeLog): UpstreamRateChange {
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

export function groupRateChange(log: UpstreamRateChangeLog) {
  return multiplierChange(log.old_group_multiplier, log.new_group_multiplier);
}

export function accountBillingRateChange(log: UpstreamRateChangeLog) {
  const target = multiplierChange(log.old_target_rate, log.new_target_rate);
  if (target.direction === "increase" || target.direction === "decrease") return target;
  const current = multiplierChange(log.old_current_rate, log.new_current_rate);
  if (current.direction === "increase" || current.direction === "decrease") return current;
  return target.newValue !== null || target.oldValue !== null ? target : current;
}

export function upstreamRechargeRateChange(log: UpstreamRateChangeLog) {
  return multiplierChange(
    log.old_upstream_recharge_multiplier,
    log.new_upstream_recharge_multiplier,
  );
}

function finiteNumber(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const number = typeof value === "number" ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}
