export type AutomationDurationUnit = "second" | "minute" | "hour";

export const automationDurationUnits: Array<{
  label: string;
  multiplier: number;
  value: AutomationDurationUnit;
}> = [
  { label: "秒", multiplier: 1, value: "second" },
  { label: "分钟", multiplier: 60, value: "minute" },
  { label: "小时", multiplier: 3_600, value: "hour" },
];

export function automationDurationMultiplier(unit: AutomationDurationUnit): number {
  return automationDurationUnits.find((option) => option.value === unit)?.multiplier || 1;
}

export function preferredAutomationDurationUnit(value: string): AutomationDurationUnit {
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 3_600 && seconds % 3_600 === 0) return "hour";
  if (Number.isFinite(seconds) && seconds >= 60 && seconds % 60 === 0) return "minute";
  return "second";
}

export function automationDurationDisplayValue(
  secondsValue: string,
  unit: AutomationDurationUnit,
): string {
  const seconds = Number(secondsValue);
  if (secondsValue === "" || !Number.isFinite(seconds)) return secondsValue;
  return String(Number((seconds / automationDurationMultiplier(unit)).toFixed(6)));
}

export function automationDurationSecondsValue(
  displayValue: string,
  unit: AutomationDurationUnit,
): string {
  if (displayValue === "") return "";
  const seconds = Number(displayValue) * automationDurationMultiplier(unit);
  return Number.isFinite(seconds) ? String(Math.round(seconds)) : displayValue;
}
