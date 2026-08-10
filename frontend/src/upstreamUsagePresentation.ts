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

export function formatUpstreamBalance(
  value: unknown,
  unit?: string | null,
  fractionDigits?: number,
) {
  const amount = finiteNumber(value);
  if (amount === null) return "—";
  const digits = fractionDigits === undefined
    ? { maximumFractionDigits: 4 }
    : { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits };
  const formatted = amount.toLocaleString("zh-CN", digits);
  const normalizedUnit = String(unit || "USD").trim().toUpperCase();
  if (normalizedUnit === "USD") return "$" + formatted;
  if (normalizedUnit === "USDT") return "$" + formatted + " USDT";
  if (normalizedUnit === "CNY" || normalizedUnit === "RMB") return "¥" + formatted;
  return formatted + " " + normalizedUnit;
}

export function visibleUpstreamBalanceMessage(message?: string | null) {
  const value = String(message || "").trim();
  if (/^Balance read from the (?:NewAPI|Sub2API) user account\.?$/i.test(value)) return "";
  return value;
}

function finiteNonNegative(value: unknown): number | null {
  const parsed = finiteNumber(value);
  if (parsed === null) return null;
  return parsed >= 0 ? parsed : null;
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function finitePositive(value: unknown): number | null {
  const parsed = finiteNonNegative(value);
  return parsed !== null && parsed > 0 ? parsed : null;
}
