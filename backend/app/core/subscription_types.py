from __future__ import annotations

import copy
import json
import math
import re
from typing import Any


SUBSCRIPTION_TYPE_UNKNOWN = "unknown"
USAGE_LIMIT_WINDOW_KEYS = ("five_hour", "seven_day", "monthly")
CORE_SUBSCRIPTION_TYPES = ("plus", "team", "pro", "free", "k12")
MAX_SUBSCRIPTION_TYPES = 100
MAX_USAGE_LIMIT_VALUE = 1_000_000_000.0

_DEFAULT_WEEKLY_RANGE = {"lower": 100.0, "upper": 140.0}
_DEFAULT_WINDOW_RANGES = {
    "five_hour": {"lower": 15.0, "upper": 25.0},
    "seven_day": copy.deepcopy(_DEFAULT_WEEKLY_RANGE),
    "monthly": {"lower": _DEFAULT_WEEKLY_RANGE["lower"] * 4, "upper": _DEFAULT_WEEKLY_RANGE["upper"] * 4},
}
DEFAULT_USAGE_LIMIT_RANGES = {
    subscription_type: copy.deepcopy(_DEFAULT_WINDOW_RANGES)
    for subscription_type in (*CORE_SUBSCRIPTION_TYPES, SUBSCRIPTION_TYPE_UNKNOWN)
}
DEFAULT_USAGE_LIMIT_RANGES["team"]["monthly"] = {"lower": 100.0, "upper": 300.0}

_COMPACT_ALIASES = {
    "chatgptplusplan": "plus",
    "chatgptplus": "plus",
    "plus": "plus",
    "chatgptteamplan": "team",
    "chatgptteam": "team",
    "team": "team",
    "chatgptbusinessplan": "team",
    "business": "team",
    "chatgptproplan": "pro",
    "chatgptpro": "pro",
    "pro": "pro",
    "chatgptfreeplan": "free",
    "chatgptfree": "free",
    "free": "free",
    "chatgptk12plan": "k12",
    "chatgptk12": "k12",
    "k12plan": "k12",
    "k12": "k12",
}
_LABELS = {
    "plus": "Plus",
    "team": "Team",
    "pro": "Pro",
    "free": "Free",
    "k12": "K12",
    SUBSCRIPTION_TYPE_UNKNOWN: "Unknown",
}
_NON_PLAN_VALUES = {
    "active",
    "inactive",
    "none",
    "null",
    "oauth",
    "openai",
    "subscription",
    "plan",
    SUBSCRIPTION_TYPE_UNKNOWN,
}


def normalize_subscription_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return SUBSCRIPTION_TYPE_UNKNOWN

    compact = re.sub(r"[^a-z0-9]+", "", text)
    alias = _COMPACT_ALIASES.get(compact)
    if alias:
        return alias
    if "k12" in compact:
        return "k12"
    candidate = re.sub(r"^chatgpt[^a-z0-9]*", "", text)
    candidate = re.sub(r"[^a-z0-9]*plan$", "", candidate)
    compact_candidate = re.sub(r"[^a-z0-9]+", "", candidate)
    alias = _COMPACT_ALIASES.get(compact_candidate)
    if alias:
        return alias

    slug = re.sub(r"[^a-z0-9]+", "-", candidate).strip("-")
    if not slug or slug in _NON_PLAN_VALUES:
        return SUBSCRIPTION_TYPE_UNKNOWN
    return slug[:64]


def subscription_type_label(value: Any) -> str:
    subscription_type = normalize_subscription_type(value)
    if subscription_type in _LABELS:
        return _LABELS[subscription_type]
    return " ".join(part.upper() if any(char.isdigit() for char in part) else part.capitalize() for part in subscription_type.split("-"))


def normalize_usage_limit_ranges(value: Any) -> dict[str, dict[str, dict[str, float]]]:
    parsed = _parse_ranges(value)
    ranges = copy.deepcopy(DEFAULT_USAGE_LIMIT_RANGES)
    if not isinstance(parsed, dict):
        return ranges

    for raw_type, raw_windows in list(parsed.items())[:MAX_SUBSCRIPTION_TYPES]:
        subscription_type = normalize_subscription_type(raw_type)
        if subscription_type == SUBSCRIPTION_TYPE_UNKNOWN and str(raw_type).strip().lower() != SUBSCRIPTION_TYPE_UNKNOWN:
            continue
        if not isinstance(raw_windows, dict):
            continue
        plan_ranges = copy.deepcopy(ranges.get(subscription_type) or ranges[SUBSCRIPTION_TYPE_UNKNOWN])
        for window_key in USAGE_LIMIT_WINDOW_KEYS:
            normalized_range = _normalize_range(raw_windows.get(window_key))
            if normalized_range is not None:
                plan_ranges[window_key] = normalized_range
        if subscription_type != "team":
            plan_ranges["monthly"] = _monthly_range_from_weekly(plan_ranges["seven_day"])
        ranges[subscription_type] = plan_ranges
    return ranges


def usage_limit_bounds(
    ranges: dict[str, dict[str, dict[str, float]]] | None,
    window_key: str,
    subscription_type: Any,
) -> tuple[float, float]:
    normalized_ranges = normalize_usage_limit_ranges(ranges)
    normalized_type = normalize_subscription_type(subscription_type)
    plan_ranges = normalized_ranges.get(normalized_type) or normalized_ranges[SUBSCRIPTION_TYPE_UNKNOWN]
    window_range = plan_ranges.get(window_key) or normalized_ranges[SUBSCRIPTION_TYPE_UNKNOWN][window_key]
    return float(window_range["lower"]), float(window_range["upper"])


def subscription_type_sort_key(value: Any) -> tuple[int, str]:
    normalized = normalize_subscription_type(value)
    try:
        return CORE_SUBSCRIPTION_TYPES.index(normalized), normalized
    except ValueError:
        if normalized == SUBSCRIPTION_TYPE_UNKNOWN:
            return len(CORE_SUBSCRIPTION_TYPES) + 1, normalized
        return len(CORE_SUBSCRIPTION_TYPES), normalized


def _parse_ranges(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _normalize_range(value: Any) -> dict[str, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        lower, upper = value
    elif isinstance(value, dict):
        lower, upper = value.get("lower"), value.get("upper")
    else:
        return None
    try:
        lower_value = float(lower)
        upper_value = float(upper)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lower_value) or not math.isfinite(upper_value):
        return None
    if lower_value < 0 or upper_value < lower_value or upper_value > MAX_USAGE_LIMIT_VALUE:
        return None
    return {"lower": lower_value, "upper": upper_value}


def _monthly_range_from_weekly(weekly_range: dict[str, float]) -> dict[str, float]:
    return {
        "lower": min(float(weekly_range["lower"]) * 4, MAX_USAGE_LIMIT_VALUE),
        "upper": min(float(weekly_range["upper"]) * 4, MAX_USAGE_LIMIT_VALUE),
    }
