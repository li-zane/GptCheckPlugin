export function rechargeAdjustedUsage(
  usageAmount: unknown,
  rechargeMultiplier: unknown,
): number | null {
  const amount = finiteNonNegative(usageAmount);
  const multiplier = finitePositive(rechargeMultiplier);
  if (amount === null || multiplier === null) return null;
  const result = amount * multiplier;
  return Number.isFinite(result) ? result : null;
}

function finiteNonNegative(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function finitePositive(value: unknown): number | null {
  const parsed = finiteNonNegative(value);
  return parsed !== null && parsed > 0 ? parsed : null;
}
