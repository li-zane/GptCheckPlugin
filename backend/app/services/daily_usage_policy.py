from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TRANSIENT_DAILY_USAGE_STATUSES = frozenset(
    {"failed", "network_error", "timeout", "unavailable"}
)
TRANSIENT_DAILY_USAGE_REASONS = frozenset(
    {
        "network",
        "overall_discovery_error",
        "response_missing",
        "timeout",
        "upstream_failure",
    }
)
QUIET_DAILY_USAGE_STATUSES = frozenset(
    {
        "credentials_missing",
        "not_available",
        "not_checked",
        "unsupported",
    }
)


@dataclass(frozen=True)
class DailyUsageFailureDecision:
    normalized_reason: str
    retain_cached_value: bool
    should_log: bool


def daily_usage_failure_decision(
    *,
    status: str,
    reason: str,
    cached_amount: float | None,
    checked_at: datetime | None,
    time_zone: str,
    fallback_time_zone: str,
    now: datetime,
) -> DailyUsageFailureDecision:
    normalized_status = str(status or "").strip().lower()
    normalized_reason = str(reason or "").strip().lower()
    transient = _is_transient_failure(normalized_status, normalized_reason)
    retain_cached_value = transient and daily_usage_cache_is_current(
        cached_amount=cached_amount,
        checked_at=checked_at,
        time_zone=time_zone,
        fallback_time_zone=fallback_time_zone,
        now=now,
    )
    return DailyUsageFailureDecision(
        normalized_reason=normalized_reason,
        retain_cached_value=retain_cached_value,
        should_log=normalized_status not in QUIET_DAILY_USAGE_STATUSES,
    )


def daily_usage_cache_is_current(
    *,
    cached_amount: float | None,
    checked_at: datetime | None,
    time_zone: str,
    fallback_time_zone: str,
    now: datetime,
) -> bool:
    if (
        cached_amount is None
        or cached_amount < 0
        or not isinstance(checked_at, datetime)
    ):
        return False
    normalized_checked_at = (
        checked_at.replace(tzinfo=timezone.utc)
        if checked_at.tzinfo is None
        else checked_at
    )
    normalized_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    zone = _time_zone_or_fallback(time_zone, fallback_time_zone)
    return (
        normalized_checked_at.astimezone(zone).date()
        == normalized_now.astimezone(zone).date()
    )


def _is_transient_failure(status: str, reason: str) -> bool:
    if status in TRANSIENT_DAILY_USAGE_STATUSES:
        return True
    if status != "error":
        return False
    if reason in TRANSIENT_DAILY_USAGE_REASONS:
        return True
    if not reason.startswith("http_"):
        return False
    try:
        status_code = int(reason[5:])
    except ValueError:
        return False
    return status_code in {408, 425, 429} or 500 <= status_code <= 599


def _time_zone_or_fallback(time_zone: str, fallback_time_zone: str) -> tzinfo:
    for candidate in (time_zone, fallback_time_zone):
        try:
            return ZoneInfo(str(candidate or "").strip())
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            continue
    return timezone.utc
