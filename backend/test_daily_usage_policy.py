from __future__ import annotations

from datetime import datetime, timezone

from app.services.daily_usage_policy import daily_usage_failure_decision


def test_transient_failure_retains_same_local_day_cache() -> None:
    decision = daily_usage_failure_decision(
        status="error",
        reason="http_503",
        cached_amount=3.25,
        checked_at=datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc),
        time_zone="Asia/Shanghai",
        fallback_time_zone="Asia/Shanghai",
        now=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
    )

    assert decision.retain_cached_value
    assert decision.should_log
    assert decision.normalized_reason == "http_503"


def test_authoritative_failure_never_retains_cache() -> None:
    decision = daily_usage_failure_decision(
        status="error",
        reason="http_401",
        cached_amount=3.25,
        checked_at=datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc),
        time_zone="Asia/Shanghai",
        fallback_time_zone="Asia/Shanghai",
        now=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
    )

    assert not decision.retain_cached_value
    assert decision.should_log


def test_invalid_timezone_uses_fallback_and_old_cache_is_rejected() -> None:
    decision = daily_usage_failure_decision(
        status="timeout",
        reason="timeout",
        cached_amount=3.25,
        checked_at=datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc),
        time_zone="not/a-zone",
        fallback_time_zone="Asia/Shanghai",
        now=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
    )

    assert not decision.retain_cached_value


def test_invalid_primary_and_fallback_timezones_use_utc() -> None:
    decision = daily_usage_failure_decision(
        status="timeout",
        reason="timeout",
        cached_amount=2.5,
        checked_at=datetime(2026, 7, 28, 0, 30, tzinfo=timezone.utc),
        time_zone="Invalid/Primary",
        fallback_time_zone="Invalid/Fallback",
        now=datetime(2026, 7, 28, 23, 30, tzinfo=timezone.utc),
    )

    assert decision.retain_cached_value


def test_unsupported_status_stays_quiet() -> None:
    decision = daily_usage_failure_decision(
        status="unsupported",
        reason="field_missing",
        cached_amount=3.25,
        checked_at=datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc),
        time_zone="Asia/Shanghai",
        fallback_time_zone="Asia/Shanghai",
        now=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
    )

    assert not decision.retain_cached_value
    assert not decision.should_log
