from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.upstream_urls import canonicalize_upstream_url, upstream_url_origin
from app.models import (
    AccountSchedulingChangeLog,
    ApiAccountDataArchive,
    ApiAccount,
    ApiAccountPauseHold,
    Upstream,
    UpstreamPriorityInterval,
    UpstreamRateChangeLog,
)
from app.schemas import (
    ApiAccountOut,
    ApiAccountUpdate,
    ApiAccountDiscoverAllOut,
    UpstreamGroupOptionOut,
)
from app.services.daily_usage_policy import daily_usage_cache_is_current
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError
from app.services.notifications import (
    enqueue_api_key_account_state_changed,
    enqueue_api_key_rate_changed,
)
from app.services.runtime_config import get_runtime_config_service
from app.services.upstream_client import DEFAULT_TODAY_TIME_ZONE, discover_upstream


RATE_QUANTUM = Decimal("0.0001")
MAX_MULTIPLIER = Decimal("1000")
DEFAULT_ENCRYPTION_KEY = "change-me-encryption-key"
JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
SIGNED_BIGINT_MAX = 9_223_372_036_854_775_807
INVALID_UPSTREAM_KEY_STATUSES = frozenset({"disabled", "expired", "quota_exhausted"})
INVALID_UPSTREAM_GROUP_STATUSES = frozenset({"unavailable", "unassigned", "deleted"})
AUTO_PAUSE_REASON_BALANCE = "upstream_balance_negative"
AUTO_PAUSE_REASON_KEY = "upstream_key_unavailable"
AUTO_PAUSE_REASON_GROUP = "upstream_group_unavailable"
AUTO_PAUSE_REASON_MONITOR = "upstream_monitor_unavailable"
AUTO_PAUSE_REASON_RATE = "upstream_rate_increase"
AUTO_PAUSE_REASON_ORDER = (
    AUTO_PAUSE_REASON_BALANCE,
    AUTO_PAUSE_REASON_KEY,
    AUTO_PAUSE_REASON_GROUP,
    AUTO_PAUSE_REASON_MONITOR,
    AUTO_PAUSE_REASON_RATE,
)
logger = logging.getLogger(__name__)


class UpstreamRecordIdentityStatus(str, Enum):
    VERIFIED = "verified"
    INCONCLUSIVE = "inconclusive"
    REBIND_REQUIRED = "rebind_required"


def resolve_rate_pause_policy(
    config: ApiAccount | None,
    interval: UpstreamPriorityInterval | None = None,
) -> dict[str, Any]:
    """Resolve account override > priority interval > disabled."""
    if config is None:
        return {
            "enabled": False,
            "source": "disabled",
            "absolute_threshold": None,
        }
    policy = str(config.rate_pause_policy or "inherit").strip().lower()
    if policy == "custom":
        return {
            "enabled": True,
            "source": "account",
            "absolute_threshold": float(config.rate_absolute_threshold or 1.0),
        }
    if policy == "disabled":
        return {
            "enabled": False,
            "source": "disabled",
            "absolute_threshold": None,
        }
    if interval is not None and bool(interval.rate_pause_enabled):
        return {
            "enabled": True,
            "source": "priority_interval",
            "absolute_threshold": float(interval.rate_absolute_threshold or 1.0),
        }
    if interval is not None:
        return {
            "enabled": False,
            "source": "disabled",
            "absolute_threshold": None,
        }
    return {
        "enabled": False,
        "source": "disabled",
        "absolute_threshold": None,
    }


def _normalize_available_models(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value[:1000]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()[:160]
        if not model_id or model_id in seen or any(ord(character) < 32 for character in model_id):
            continue
        display_name = str(item.get("display_name") or model_id).strip()[:200]
        seen.add(model_id)
        result.append({"id": model_id, "display_name": display_name or model_id})
        if len(result) >= 500:
            break
    return result


def _model_check_version(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class UpstreamHealthTransition:
    old_key_status: str
    new_key_status: str
    old_group_status: str
    new_group_status: str
    previous_invalid_count: int
    confirmed_invalid: bool
    key_observed: bool = False
    group_observed: bool = False
    observed_group_id: str | None = None
    observed_group_name: str | None = None


class ApiAccountServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.public_message = message
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _value(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _decimal_multiplier(value: Any, *, allow_zero: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed > MAX_MULTIPLIER:
        return None
    if allow_zero:
        return parsed if parsed >= 0 else None
    return parsed if parsed > 0 else None


def _float_multiplier(value: Any, *, allow_zero: bool = False) -> float | None:
    parsed = _decimal_multiplier(value, allow_zero=allow_zero)
    return float(parsed) if parsed is not None else None


def _balance_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or abs(parsed) > 1_000_000_000_000_000:
        return None
    return parsed


def _cny_amount(amount: Any, multiplier: Any) -> float | None:
    parsed_amount = _balance_number(amount)
    parsed_multiplier = _balance_number(multiplier)
    if (
        parsed_amount is None
        or parsed_amount < 0
        or parsed_multiplier is None
        or parsed_multiplier < 0
    ):
        return None
    converted = parsed_amount * parsed_multiplier
    return converted if math.isfinite(converted) else None


def _maximum_known_cost(*values: float | None) -> float | None:
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _calculate_expected_management_billing_multiplier(
    group_multiplier: Decimal,
    upstream_recharge_multiplier: Decimal,
    management_recharge_multiplier: Decimal,
) -> Decimal | None:
    try:
        if (
            not group_multiplier.is_finite()
            or not upstream_recharge_multiplier.is_finite()
            or not management_recharge_multiplier.is_finite()
            or group_multiplier <= 0
            or upstream_recharge_multiplier <= 0
            or management_recharge_multiplier <= 0
        ):
            return None
        raw_target = group_multiplier * upstream_recharge_multiplier / management_recharge_multiplier
        if not raw_target.is_finite() or raw_target <= 0 or raw_target > MAX_MULTIPLIER:
            return None
        target = _quantize_rate(raw_target)
    except (DecimalException, ValueError):
        return None
    return target if target.is_finite() and Decimal("0") < target <= MAX_MULTIPLIER else None


def _calculate_upstream_actual_multiplier(
    group_multiplier: Any,
    upstream_recharge_multiplier: Any,
) -> float | None:
    group = _decimal_multiplier(group_multiplier)
    recharge = _decimal_multiplier(upstream_recharge_multiplier)
    if group is None or recharge is None:
        return None
    try:
        value = group * recharge
    except DecimalException:
        return None
    return float(value) if value.is_finite() and value > 0 else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _safe_text(value: Any, *, secrets: tuple[str | None, ...] = (), limit: int = 300) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).replace("\x00", "").split())
    if not text:
        return None
    secret_values: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        cleaned = str(secret).strip()
        if not cleaned:
            continue
        secret_values.add(cleaned)
        if cleaned.lower().startswith("bearer ") and cleaned[7:].strip():
            secret_values.add(cleaned[7:].strip())
    for secret in sorted(secret_values, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text[:limit]


def _datetime_text(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _latest_datetime(*values: datetime | None) -> datetime | None:
    candidates = [value for value in values if isinstance(value, datetime)]
    if not candidates:
        return None

    def as_timestamp(value: datetime) -> float:
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return normalized.timestamp()

    return max(candidates, key=as_timestamp)


def _safe_pause_evidence(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed = {
        "balance",
        "basis",
        "threshold",
        "unit",
        "key_status",
        "group_status",
        "monitor_status",
        "unavailable_count",
        "test_status",
        "test_purpose",
        "test_attempts",
        "max_test_attempts",
        "baseline_multiplier",
        "mode",
        "observed_multiplier",
        "absolute_threshold",
        "increase_percent",
        "threshold_percent",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        raw = value.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool):
            result[key] = raw
            continue
        if isinstance(raw, (int, float, Decimal)):
            parsed = _balance_number(raw)
            if parsed is not None:
                result[key] = parsed
            continue
        text = _safe_text(raw, limit=128)
        if text is not None:
            result[key] = text
    return result or None


def _normalize_base_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 500:
        return None
    try:
        return canonicalize_upstream_url(text)
    except (TypeError, ValueError):
        return None


def _remote_base_url(account: dict[str, Any]) -> str | None:
    credentials = account.get("credentials")
    sources = [credentials, account]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("base_url", "baseURL", "api_base", "api_base_url", "base_uri", "endpoint"):
            normalized = _normalize_base_url(source.get(key))
            if normalized:
                return normalized
    return None


def _sanitize_group_options(
    value: Any,
    *,
    secrets: tuple[str | None, ...] = (),
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value[:500]:
        group_id = _value(item, "id", "group_id", "groupId", "name")
        name = _value(item, "name", "group_name", "groupName", "description")
        multiplier = _float_multiplier(
            _value(item, "multiplier", "rate_multiplier", "rateMultiplier", "ratio")
        )
        if group_id is None or multiplier is None:
            continue
        normalized_id = _safe_text(group_id, secrets=secrets, limit=128) or ""
        normalized_name = _safe_text(
            name if name is not None else group_id,
            secrets=secrets,
            limit=200,
        ) or ""
        if not normalized_id or not normalized_name or normalized_id in seen:
            continue
        seen.add(normalized_id)
        result.append({"id": normalized_id, "name": normalized_name, "multiplier": multiplier})
    return result


class ApiAccountService:
    def __init__(self, sub2api: Sub2ApiClient | None = None, priorities: Any | None = None) -> None:
        self.sub2api = sub2api or Sub2ApiClient()
        self._priorities = priorities
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def _priority_service(self):
        if self._priorities is None:
            from app.services.upstream_priorities import UpstreamPriorityService

            self._priorities = UpstreamPriorityService(accounts=self)
        return self._priorities

    async def _rebalance_priorities_best_effort(self, db: AsyncSession) -> None:
        try:
            await self._priority_service().rebalance(db)
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            await db.rollback()
            logger.exception("Priority rebalance failed after an upstream account mutation.")

    async def _rebalance_priorities_after_discovery(self, db: AsyncSession) -> None:
        try:
            runtime = get_runtime_config_service()
            getter = getattr(runtime, "get_upstream_priority_sync_enabled", None)
            if getter is not None and not bool(await getter()):
                return
        except Exception:
            return
        await self._rebalance_priorities_best_effort(db)

    @staticmethod
    def _normalized_health_status(value: Any, allowed: frozenset[str]) -> str | None:
        normalized = str(value or "").strip().lower()
        return normalized if normalized in allowed else None

    def apply_authoritative_upstream_state(
        self,
        config: ApiAccount,
        state: Any,
        *,
        now: datetime,
    ) -> UpstreamHealthTransition:
        old_key_status = config.upstream_key_status or "not_checked"
        old_group_status = config.upstream_group_status or "not_checked"
        previous_invalid_count = min(
            2,
            max(0, int(config.upstream_health_invalid_count or 0)),
        )
        observed_key_status = self._normalized_health_status(
            _value(state, "key_status"),
            frozenset({"active", *INVALID_UPSTREAM_KEY_STATUSES}),
        )
        observed_group_status = self._normalized_health_status(
            _value(state, "group_status"),
            frozenset({"available", *INVALID_UPSTREAM_GROUP_STATUSES}),
        )
        observed_group_id = _safe_text(_value(state, "group_id"), limit=128)
        observed_group_name = _safe_text(_value(state, "group_name"), limit=200)

        if observed_key_status is not None:
            config.upstream_key_status = observed_key_status
            config.upstream_key_checked_at = now
        if observed_group_status is not None:
            config.upstream_group_status = observed_group_status
            config.upstream_group_checked_at = now

        confirmed_invalid = bool(
            observed_key_status in INVALID_UPSTREAM_KEY_STATUSES
            or observed_group_status in INVALID_UPSTREAM_GROUP_STATUSES
        )
        confirmed_healthy = (
            observed_key_status == "active"
            and observed_group_status == "available"
        )
        if confirmed_invalid:
            config.upstream_health_invalid_count = min(2, previous_invalid_count + 1)
        elif confirmed_healthy:
            config.upstream_health_invalid_count = 0
        else:
            config.upstream_health_invalid_count = 0

        return UpstreamHealthTransition(
            old_key_status=old_key_status,
            new_key_status=config.upstream_key_status or "not_checked",
            old_group_status=old_group_status,
            new_group_status=config.upstream_group_status or "not_checked",
            previous_invalid_count=previous_invalid_count,
            confirmed_invalid=confirmed_invalid,
            key_observed=observed_key_status is not None,
            group_observed=observed_group_status is not None,
            observed_group_id=observed_group_id,
            observed_group_name=observed_group_name,
        )

    @staticmethod
    def clear_upstream_usage_state(config: ApiAccount) -> None:
        config.upstream_wallet_cost_usd = None
        config.upstream_usage_unit = None
        config.upstream_usage_checked_at = None
        config.today_upstream_wallet_cost_usd = None
        config.today_upstream_usage_unit = None
        config.today_upstream_usage_status = "not_checked"
        config.today_upstream_usage_source = None
        config.today_upstream_usage_checked_at = None
        config.today_management_account_cost_usd = None
        config.today_management_user_charge_usd = None
        config.today_management_site_stats_status = "not_checked"
        config.today_management_site_stats_checked_at = None

    def archive_data_before_invalidation(
        self,
        db: AsyncSession,
        config: ApiAccount,
        *,
        reason: str,
        channel: Upstream | None = None,
    ) -> None:
        """Keep a safe, dated snapshot when a deliberate identity reset clears data.

        A failed probe must never call this method: the live record remains the
        authoritative last-known result in that case. Archives only separate
        data that can no longer be attributed safely after a rebind.
        """
        known_secrets = (
            (
                decrypt_text(config.encrypted_api_key)
                if not config.api_key_origin_rebind_required
                else None
            ),
            decrypt_text(config.encrypted_access_token),
        )
        account_name = _safe_text(
            config.remote_name,
            secrets=known_secrets,
            limit=200,
        )
        snapshot = {
            "remote": {
                "id": config.management_account_id,
                "name": account_name,
                "platform": _safe_text(config.remote_platform, limit=64),
                "type": _safe_text(config.remote_account_type, limit=32),
                "status": _safe_text(config.remote_status, limit=64),
                "schedulable": config.remote_schedulable,
                "priority": config.remote_priority,
                "snapshot_updated_at": _datetime_text(config.remote_snapshot_updated_at),
            },
            "upstream": {
                "api_endpoint_url": _safe_text(
                    config.api_endpoint_url,
                    secrets=known_secrets,
                    limit=500,
                ),
                "platform_type": _safe_text(
                    config.resolved_platform_type or config.platform_type,
                    limit=32,
                ),
                "remote_upstream_api_key_id": config.remote_upstream_api_key_id,
                "group_id": _safe_text(config.selected_group_id, limit=128),
                "group_name": _safe_text(config.selected_group_name, limit=200),
                "upstream_group_multiplier": config.upstream_group_multiplier,
                "upstream_recharge_multiplier": config.upstream_recharge_multiplier,
                "wallet_balance_usd": config.wallet_balance_usd,
                "balance_unit": _safe_text(config.balance_unit, limit=32),
                "balance_checked_at": _datetime_text(config.balance_checked_at),
                "today_upstream_wallet_cost_usd": config.today_upstream_wallet_cost_usd,
                "today_upstream_usage_unit": _safe_text(
                    config.today_upstream_usage_unit,
                    limit=32,
                ),
                "today_upstream_usage_status": _safe_text(
                    config.today_upstream_usage_status,
                    limit=32,
                ),
                "today_upstream_usage_checked_at": _datetime_text(
                    config.today_upstream_usage_checked_at
                ),
                "today_management_account_cost_usd": config.today_management_account_cost_usd,
                "today_management_user_charge_usd": config.today_management_user_charge_usd,
                "today_management_site_stats_status": _safe_text(
                    config.today_management_site_stats_status,
                    limit=32,
                ),
                "today_management_site_stats_checked_at": _datetime_text(
                    config.today_management_site_stats_checked_at
                ),
                "upstream_wallet_cost_usd": config.upstream_wallet_cost_usd,
                "upstream_usage_unit": _safe_text(config.upstream_usage_unit, limit=32),
                "upstream_usage_checked_at": _datetime_text(config.upstream_usage_checked_at),
                "upstream_key_status": _safe_text(config.upstream_key_status, limit=32),
                "upstream_group_status": _safe_text(config.upstream_group_status, limit=32),
                "last_discovered_at": _datetime_text(config.last_discovered_at),
            },
        }
        # Do not create empty rows for configurations that never held a remote
        # snapshot or an upstream reading.
        has_observed_data = any(
            value is not None
            for value in (
                config.remote_snapshot_updated_at,
                config.balance_checked_at,
                config.today_upstream_usage_checked_at,
                config.today_management_site_stats_checked_at,
                config.upstream_usage_checked_at,
                config.last_discovered_at,
                config.last_applied_at,
            )
        )
        if not has_observed_data:
            return
        observed_at = _latest_datetime(
            config.remote_snapshot_updated_at,
            config.balance_checked_at,
            config.today_upstream_usage_checked_at,
            config.today_management_site_stats_checked_at,
            config.upstream_usage_checked_at,
            config.last_discovered_at,
            config.last_applied_at,
        )
        db.add(
            ApiAccountDataArchive(
                management_account_id=config.management_account_id,
                remote_identity_fingerprint=config.remote_identity_fingerprint,
                account_name=account_name,
                upstream_id=config.upstream_id,
                upstream_name=(
                    _safe_text(channel.display_name, limit=200)
                    if channel is not None
                    else None
                ),
                reason=reason[:64],
                snapshot=snapshot,
                observed_at=observed_at,
            )
        )

    @staticmethod
    def apply_upstream_usage_state(
        config: ApiAccount,
        state: Any,
        *,
        now: datetime,
        time_zone: str = DEFAULT_TODAY_TIME_ZONE,
        secrets: tuple[str | None, ...] = (),
    ) -> None:
        def clear_unavailable_usage() -> None:
            config.today_upstream_wallet_cost_usd = None
            config.today_upstream_usage_unit = None
            config.today_upstream_usage_status = "not_available"
            config.today_upstream_usage_source = None
            config.today_upstream_usage_checked_at = None
            # These fields are the compatibility mirror for older clients.
            config.upstream_wallet_cost_usd = None
            config.upstream_usage_unit = None
            config.upstream_usage_checked_at = None

        def retain_last_usage() -> bool:
            """Keep a dated successful value when this probe cannot confirm usage."""
            current_amount = _balance_number(config.today_upstream_wallet_cost_usd)
            if (
                current_amount is not None
                and current_amount >= 0
                and daily_usage_cache_is_current(
                    cached_amount=current_amount,
                    checked_at=config.today_upstream_usage_checked_at,
                    time_zone=time_zone,
                    fallback_time_zone=DEFAULT_TODAY_TIME_ZONE,
                    now=now,
                )
            ):
                config.today_upstream_usage_status = "stale"
                config.today_upstream_usage_source = (
                    config.today_upstream_usage_source
                    or "upstream_api_key_actual_cost"
                )
                return True

            legacy_amount = _balance_number(config.upstream_wallet_cost_usd)
            if (
                legacy_amount is None
                or legacy_amount < 0
                or not daily_usage_cache_is_current(
                    cached_amount=legacy_amount,
                    checked_at=config.upstream_usage_checked_at,
                    time_zone=time_zone,
                    fallback_time_zone=DEFAULT_TODAY_TIME_ZONE,
                    now=now,
                )
            ):
                return False
            config.today_upstream_wallet_cost_usd = legacy_amount
            config.today_upstream_usage_unit = config.upstream_usage_unit or "USD"
            config.today_upstream_usage_status = "stale"
            config.today_upstream_usage_source = "upstream_api_key_actual_cost"
            config.today_upstream_usage_checked_at = config.upstream_usage_checked_at
            return True

        if state is None:
            if not retain_last_usage():
                clear_unavailable_usage()
            return
        usage_amount = _balance_number(_value(state, "usage_amount"))
        if usage_amount is None or usage_amount < 0:
            if not retain_last_usage():
                clear_unavailable_usage()
            return
        config.today_upstream_wallet_cost_usd = usage_amount
        config.today_upstream_usage_unit = (
            _safe_text(_value(state, "usage_unit"), secrets=secrets, limit=32) or "USD"
        )
        config.today_upstream_usage_status = "ok"
        config.today_upstream_usage_source = "upstream_api_key_actual_cost"
        config.today_upstream_usage_checked_at = now
        # Populate legacy fields until older clients migrate their label.
        config.upstream_wallet_cost_usd = usage_amount
        config.upstream_usage_unit = (
            _safe_text(_value(state, "usage_unit"), secrets=secrets, limit=32) or "USD"
        )
        config.upstream_usage_checked_at = now

    @staticmethod
    def apply_local_today_usage_fallback(
        config: ApiAccount,
        local_cost: Any,
        management_billing_multiplier: Any,
        *,
        now: datetime,
    ) -> bool:
        if config.today_upstream_usage_status == "ok":
            return False
        cost_value = _balance_number(local_cost)
        group = _decimal_multiplier(config.upstream_group_multiplier)
        rate = _decimal_multiplier(management_billing_multiplier)
        if cost_value is None or cost_value < 0 or group is None or rate is None:
            return False
        try:
            converted = Decimal(str(cost_value)) * group / rate
        except DecimalException:
            return False
        converted_value = _balance_number(converted)
        if converted_value is None or converted_value < 0:
            return False
        config.today_upstream_wallet_cost_usd = converted_value
        config.today_upstream_usage_unit = "USD"
        config.today_upstream_usage_status = "estimated"
        config.today_upstream_usage_source = "local_sub2api_today_cost_converted"
        config.today_upstream_usage_checked_at = now
        config.upstream_wallet_cost_usd = converted_value
        config.upstream_usage_unit = "USD"
        config.upstream_usage_checked_at = now
        return True

    async def get_sub2api_today_stats(
        self,
        account_ids: list[int],
    ) -> dict[int, dict[str, float | None]]:
        client_methods = type(self.sub2api).__dict__
        client_instance_methods = getattr(self.sub2api, "__dict__", {})
        legacy_overridden = (
            "get_account_today_costs" in client_methods
            or "get_account_today_costs" in client_instance_methods
        )
        stats_overridden = (
            "get_account_today_stats" in client_methods
            or "get_account_today_stats" in client_instance_methods
        )
        prefer_legacy = legacy_overridden and not stats_overridden

        getter = getattr(self.sub2api, "get_account_today_stats", None)
        if callable(getter) and not prefer_legacy:
            values = await getter(account_ids)
            if isinstance(values, dict):
                return {
                    int(account_id): {
                        "cost": _balance_number(_value(stats, "cost")),
                        "user_cost": _balance_number(_value(stats, "user_cost")),
                    }
                    for account_id, stats in values.items()
                    if _balance_number(account_id) is not None
                }
        legacy_getter = getattr(self.sub2api, "get_account_today_costs", None)
        if not callable(legacy_getter):
            return {}
        legacy_values = await legacy_getter(account_ids)
        if not isinstance(legacy_values, dict):
            return {}
        return {
            int(account_id): {"cost": cost, "user_cost": None}
            for account_id, raw_cost in legacy_values.items()
            if (cost := _balance_number(raw_cost)) is not None and cost >= 0
        }

    async def get_sub2api_daily_stats(
        self,
        account_ids: list[int],
        *,
        days: int = 2,
    ) -> dict[int, dict[date, dict[str, float | None]]]:
        client_methods = type(self.sub2api).__dict__
        client_instance_methods = getattr(self.sub2api, "__dict__", {})
        legacy_overridden = (
            "get_account_daily_costs" in client_methods
            or "get_account_daily_costs" in client_instance_methods
        )
        stats_overridden = (
            "get_account_daily_stats" in client_methods
            or "get_account_daily_stats" in client_instance_methods
        )
        prefer_legacy = legacy_overridden and not stats_overridden

        getter = getattr(self.sub2api, "get_account_daily_stats", None)
        if callable(getter) and not prefer_legacy:
            values = await getter(account_ids, days=days)
            if isinstance(values, dict):
                return values

        legacy_getter = getattr(self.sub2api, "get_account_daily_costs", None)
        if not callable(legacy_getter):
            return {}
        legacy_values = await legacy_getter(account_ids, days=days)
        if not isinstance(legacy_values, dict):
            return {}
        return {
            int(account_id): {
                usage_date: {"cost": cost, "user_cost": None}
                for usage_date, raw_cost in daily_costs.items()
                if (cost := _balance_number(raw_cost)) is not None and cost >= 0
            }
            for account_id, daily_costs in legacy_values.items()
            if isinstance(daily_costs, dict)
        }

    @staticmethod
    def apply_sub2api_today_stats(
        config: ApiAccount,
        stats: Any,
        *,
        now: datetime,
        time_zone: str = DEFAULT_TODAY_TIME_ZONE,
    ) -> None:
        cost = _balance_number(_value(stats, "cost"))
        user_cost = _balance_number(_value(stats, "user_cost"))
        cost = cost if cost is not None and cost >= 0 else None
        user_cost = user_cost if user_cost is not None and user_cost >= 0 else None
        if cost is not None or user_cost is not None:
            config.today_management_account_cost_usd = cost
            config.today_management_user_charge_usd = user_cost
            config.today_management_site_stats_status = "ok"
            config.today_management_site_stats_checked_at = now
            return
        cached_cost = _balance_number(config.today_management_account_cost_usd)
        cached_user_cost = _balance_number(config.today_management_user_charge_usd)
        if (
            (cached_cost is not None or cached_user_cost is not None)
            and daily_usage_cache_is_current(
                cached_amount=cached_cost if cached_cost is not None else cached_user_cost,
                checked_at=config.today_management_site_stats_checked_at,
                time_zone=time_zone,
                fallback_time_zone=DEFAULT_TODAY_TIME_ZONE,
                now=now,
            )
        ):
            config.today_management_site_stats_status = "stale"
            return
        config.today_management_account_cost_usd = None
        config.today_management_user_charge_usd = None
        config.today_management_site_stats_status = "not_available"
        config.today_management_site_stats_checked_at = None

    @staticmethod
    def active_pause_holds(
        config: ApiAccount,
    ) -> list[ApiAccountPauseHold]:
        order = {reason: index for index, reason in enumerate(AUTO_PAUSE_REASON_ORDER)}
        return sorted(
            (hold for hold in config.pause_holds if hold.active),
            key=lambda hold: (
                order.get(hold.reason, len(order)),
                _parse_datetime(hold.triggered_at)
                or datetime.min.replace(tzinfo=timezone.utc),
                hold.id or 0,
            ),
        )

    @staticmethod
    def pause_episode_reasons(config: ApiAccount) -> list[str]:
        episode_started_at = _parse_datetime(config.auto_paused_at)
        reasons = {
            hold.reason
            for hold in config.pause_holds
            if hold.active
            or (
                episode_started_at is not None
                and (_parse_datetime(hold.resolved_at) or datetime.min.replace(tzinfo=timezone.utc))
                >= episode_started_at
            )
        }
        if config.auto_disabled_reason:
            reasons.add(config.auto_disabled_reason)
        return [reason for reason in AUTO_PAUSE_REASON_ORDER if reason in reasons]

    @staticmethod
    def _pause_hold(
        config: ApiAccount,
        reason: str,
    ) -> ApiAccountPauseHold | None:
        return next((hold for hold in config.pause_holds if hold.reason == reason), None)

    @staticmethod
    def set_pause_hold(
        config: ApiAccount,
        reason: str,
        *,
        active: bool,
        scope_upstream_id: int | None,
        recovery_mode: str,
        now: datetime,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        if reason not in AUTO_PAUSE_REASON_ORDER:
            raise ValueError("Unsupported automatic pause reason.")
        hold = ApiAccountService._pause_hold(config, reason)
        safe_evidence = _safe_pause_evidence(evidence)
        if active:
            if hold is None:
                config.pause_holds.append(
                    ApiAccountPauseHold(
                        reason=reason,
                        active=True,
                        scope_upstream_id=scope_upstream_id,
                        triggered_at=now,
                        recovery_mode=recovery_mode,
                        evidence_json=safe_evidence,
                    )
                )
                return True
            changed = not hold.active
            if not hold.active:
                hold.active = True
                hold.triggered_at = now
                hold.resolved_at = None
            if hold.scope_upstream_id != scope_upstream_id:
                hold.scope_upstream_id = scope_upstream_id
                changed = True
            if hold.recovery_mode != recovery_mode:
                hold.recovery_mode = recovery_mode
                changed = True
            if safe_evidence is not None and hold.evidence_json != safe_evidence:
                hold.evidence_json = safe_evidence
                changed = True
            return changed
        if hold is None or not hold.active:
            return False
        hold.active = False
        hold.resolved_at = now
        if safe_evidence is not None:
            hold.evidence_json = safe_evidence
        return True

    @staticmethod
    def resolve_all_pause_holds(
        config: ApiAccount,
        *,
        now: datetime,
        clear_ownership: bool = True,
    ) -> None:
        for hold in config.pause_holds:
            if hold.active:
                hold.active = False
                hold.resolved_at = now
        if clear_ownership:
            ApiAccountService.clear_pause_ownership(config)
        else:
            # Preserve ownership until an authoritative probe can restore the
            # remote account after its upstream identity changes.
            ApiAccountService.sync_pause_compatibility_fields(config)

    @staticmethod
    def clear_pause_ownership(config: ApiAccount) -> None:
        config.auto_pause_episode_id = None
        config.pause_owned_by_plugin = False
        config.auto_pause_upstream_id = None
        config.auto_paused_at = None
        config.pause_operation = None
        config.auto_disabled_reason = None
        config.last_auto_disabled_at = None
        config.balance_guard_restore_eligible = False
        config.balance_guard_upstream_id = None
        config.balance_guard_paused_at = None
        config.balance_guard_operation = None

    @staticmethod
    def sync_pause_compatibility_fields(config: ApiAccount) -> None:
        holds = ApiAccountService.active_pause_holds(config)
        primary = holds[0] if holds else None
        if config.pause_owned_by_plugin and primary is not None:
            config.auto_disabled_reason = primary.reason
            config.last_auto_disabled_at = config.auto_paused_at or primary.triggered_at
        else:
            config.auto_disabled_reason = None
            config.last_auto_disabled_at = None

        balance_hold = next(
            (hold for hold in holds if hold.reason == AUTO_PAUSE_REASON_BALANCE),
            None,
        )
        owns_balance_pause = bool(config.pause_owned_by_plugin and balance_hold is not None)
        config.balance_guard_restore_eligible = owns_balance_pause
        config.balance_guard_upstream_id = (
            balance_hold.scope_upstream_id if owns_balance_pause else None
        )
        config.balance_guard_paused_at = config.auto_paused_at if owns_balance_pause else None
        config.balance_guard_operation = config.pause_operation if owns_balance_pause else None

    def update_upstream_health_pause_holds(
        self,
        config: ApiAccount,
        transition: UpstreamHealthTransition,
        *,
        enabled: bool | None,
        automation_paused: bool,
        upstream_id: int | None,
        now: datetime,
    ) -> None:
        if automation_paused or enabled is None:
            return
        if not enabled:
            self.set_pause_hold(
                config,
                AUTO_PAUSE_REASON_KEY,
                active=False,
                scope_upstream_id=upstream_id,
                recovery_mode="upstream_healthy",
                now=now,
            )
            self.set_pause_hold(
                config,
                AUTO_PAUSE_REASON_GROUP,
                active=False,
                scope_upstream_id=upstream_id,
                recovery_mode="upstream_healthy",
                now=now,
            )
            return

        confirmed = config.upstream_health_invalid_count >= 2
        if transition.key_observed:
            key_invalid = transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES
            if confirmed or not key_invalid:
                self.set_pause_hold(
                    config,
                    AUTO_PAUSE_REASON_KEY,
                    active=key_invalid,
                    scope_upstream_id=upstream_id,
                    recovery_mode="upstream_healthy",
                    now=now,
                    evidence={"key_status": transition.new_key_status},
                )
        if transition.group_observed:
            group_invalid = transition.new_group_status in INVALID_UPSTREAM_GROUP_STATUSES
            if confirmed or not group_invalid:
                self.set_pause_hold(
                    config,
                    AUTO_PAUSE_REASON_GROUP,
                    active=group_invalid,
                    scope_upstream_id=upstream_id,
                    recovery_mode="upstream_healthy",
                    now=now,
                    evidence={"group_status": transition.new_group_status},
                )

    async def _automatic_pause_readback(
        self,
        config: ApiAccount,
    ) -> dict[str, Any]:
        try:
            remote = await self.sub2api.get_account_by_id(config.management_account_id)
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ApiAccountServiceError(
                "Unable to read the sub2api API key account."
            ) from None
        if remote is None:
            raise ApiAccountServiceError(
                "The sub2api API key account was not found.",
                status_code=404,
            )
        self._require_config_binding(remote, config)
        return remote

    async def reconcile_automatic_pause(
        self,
        db: AsyncSession,
        remote: dict[str, Any],
        config: ApiAccount,
        *,
        upstream_id: int | None,
        upstream_name: str | None = None,
        pause_action_reason: str | None = None,
        mutations_allowed: bool,
    ) -> tuple[dict[str, Any], bool | None, bool | None, str | None, str | None]:
        old_schedulable = self.sub2api.account_schedulable(remote)
        holds = self.active_pause_holds(config)
        active_reasons = [hold.reason for hold in holds]
        primary_reason = holds[0].reason if holds else pause_action_reason
        primary_evidence = (
            holds[0].evidence_json
            if holds and isinstance(holds[0].evidence_json, dict)
            else None
        )
        pause_episode_reasons = self.pause_episode_reasons(config)
        if pause_action_reason and pause_action_reason not in pause_episode_reasons:
            pause_episode_reasons.append(pause_action_reason)
        self.sync_pause_compatibility_fields(config)
        if not mutations_allowed:
            return remote, old_schedulable, old_schedulable, None, None

        if holds:
            if old_schedulable is False:
                if config.pause_owned_by_plugin:
                    config.pause_operation = "paused"
                    self.sync_pause_compatibility_fields(config)
                return remote, old_schedulable, old_schedulable, None, None
            if old_schedulable is not True:
                return remote, old_schedulable, old_schedulable, None, None

            if not config.pause_owned_by_plugin:
                config.auto_pause_episode_id = uuid4().hex
                config.pause_owned_by_plugin = True
                config.auto_pause_upstream_id = upstream_id
                config.auto_paused_at = _utcnow()
            config.pause_operation = "pause_pending"
            self.sync_pause_compatibility_fields(config)
            # Persist ownership before mutating the remote account. A later
            # probe can reconcile pause_pending after a process interruption.
            await db.flush()
            await db.commit()
            try:
                self._require_config_binding(remote, config)
                await self.sub2api.set_account_schedulable(
                    config.management_account_id,
                    False,
                )
                readback = await self._automatic_pause_readback(config)
                if self.sub2api.account_schedulable(readback) is not False:
                    raise ValueError("schedulable readback mismatch")
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                db.add(
                    AccountSchedulingChangeLog(
                        management_account_id=config.management_account_id,
                        account_name=(config.remote_name or "")[:200] or None,
                        upstream_id=upstream_id,
                        upstream_name=(upstream_name or "")[:200] or None,
                        event_type="pause_failed",
                        reason=primary_reason,
                        active_reasons=active_reasons,
                        evidence=primary_evidence,
                        old_schedulable=old_schedulable,
                        new_schedulable=self.sub2api.account_schedulable(remote),
                        status="failed",
                        safe_error="Unable to disable/pause and verify the API key account for an active automatic policy.",
                    )
                )
                return (
                    remote,
                    old_schedulable,
                    self.sub2api.account_schedulable(remote),
                    "disable_failed",
                    "Unable to disable/pause and verify the API key account for an active automatic policy.",
                )
            config.pause_operation = "paused"
            self.sync_pause_compatibility_fields(config)
            await enqueue_api_key_account_state_changed(
                db,
                enabled=False,
                account_id=config.management_account_id,
                account_name=config.remote_name,
                upstream_id=upstream_id,
                upstream_name=upstream_name,
                reason=holds[0].reason,
                reason_details=primary_evidence,
                observed_at=config.auto_paused_at,
            )
            db.add(
                AccountSchedulingChangeLog(
                    management_account_id=config.management_account_id,
                    account_name=(config.remote_name or "")[:200] or None,
                    upstream_id=upstream_id,
                    upstream_name=(upstream_name or "")[:200] or None,
                    event_type="paused",
                    reason=primary_reason,
                    active_reasons=active_reasons,
                    evidence=primary_evidence,
                    old_schedulable=old_schedulable,
                    new_schedulable=False,
                    status="success",
                )
            )
            return readback, old_schedulable, False, "account_disabled", None

        if not config.pause_owned_by_plugin:
            self.sync_pause_compatibility_fields(config)
            return remote, old_schedulable, old_schedulable, None, None
        if old_schedulable not in {True, False}:
            return remote, old_schedulable, old_schedulable, None, None

        config.pause_operation = "restore_pending"
        self.sync_pause_compatibility_fields(config)
        await db.flush()
        await db.commit()
        readback = remote
        try:
            if old_schedulable is False:
                self._require_config_binding(remote, config)
                await self.sub2api.set_account_schedulable(
                    config.management_account_id,
                    True,
                )
                readback = await self._automatic_pause_readback(config)
                if self.sub2api.account_schedulable(readback) is not True:
                    raise ValueError("schedulable readback mismatch")
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            db.add(
                AccountSchedulingChangeLog(
                    management_account_id=config.management_account_id,
                    account_name=(config.remote_name or "")[:200] or None,
                    upstream_id=upstream_id,
                    upstream_name=(upstream_name or "")[:200] or None,
                    event_type="restore_failed",
                    reason=primary_reason,
                    active_reasons=[],
                    old_schedulable=old_schedulable,
                    new_schedulable=self.sub2api.account_schedulable(remote),
                    status="failed",
                    safe_error="Unable to restore and verify the API key account after all automatic holds cleared.",
                )
            )
            return (
                remote,
                old_schedulable,
                self.sub2api.account_schedulable(remote),
                "restore_failed",
                "Unable to restore and verify the API key account after all automatic holds cleared.",
            )
        restored_at = _utcnow()
        self.clear_pause_ownership(config)
        if old_schedulable is False:
            await enqueue_api_key_account_state_changed(
                db,
                enabled=True,
                account_id=config.management_account_id,
                account_name=config.remote_name,
                upstream_id=upstream_id,
                upstream_name=upstream_name,
                reason="All automatic pause conditions cleared.",
                reason_details={"previous_pause_reasons": pause_episode_reasons},
                observed_at=restored_at,
            )
            db.add(
                AccountSchedulingChangeLog(
                    management_account_id=config.management_account_id,
                    account_name=(config.remote_name or "")[:200] or None,
                    upstream_id=upstream_id,
                    upstream_name=(upstream_name or "")[:200] or None,
                    event_type="restored",
                    reason=primary_reason,
                    active_reasons=[],
                    old_schedulable=False,
                    new_schedulable=True,
                    status="success",
                    created_at=restored_at,
                )
            )
        return (
            readback,
            old_schedulable,
            self.sub2api.account_schedulable(readback),
            "account_restored" if old_schedulable is False else None,
            None,
        )

    def record_upstream_health_change(
        self,
        db: AsyncSession,
        config: ApiAccount,
        channel: Upstream | None,
        transition: UpstreamHealthTransition,
        *,
        old_remote_schedulable: bool | None,
        new_remote_schedulable: bool | None,
        action_status: str | None,
        safe_error: str | None,
        previous_group_multiplier: float | None,
        previous_recharge_multiplier: float | None,
        previous_expected_management_billing_multiplier: float | None,
        previous_management_billing_multiplier: float | None,
        pause_action_reason: str | None = None,
    ) -> None:
        key_changed = transition.old_key_status != transition.new_key_status
        group_changed = transition.old_group_status != transition.new_group_status
        schedulable_changed = old_remote_schedulable != new_remote_schedulable
        currently_invalid = bool(
            transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES
            or transition.new_group_status in INVALID_UPSTREAM_GROUP_STATUSES
        )
        initial_non_invalid = bool(
            transition.old_key_status == "not_checked"
            and transition.old_group_status == "not_checked"
            and not currently_invalid
        )
        if not (
            ((key_changed or group_changed) and not initial_non_invalid)
            or schedulable_changed
            or action_status is not None
        ):
            return

        reason = (
            f"{pause_action_reason}_recovered"
            if action_status == "account_restored" and pause_action_reason
            else pause_action_reason
            if action_status is not None and pause_action_reason
            else "automatic_pause_restored"
            if action_status == "account_restored"
            else "upstream_auto_disable"
            if action_status is not None
            else "upstream_key_recovered"
            if key_changed
            and transition.new_key_status == "active"
            and transition.old_key_status in INVALID_UPSTREAM_KEY_STATUSES
            else "upstream_key_status_change"
            if key_changed
            else "upstream_group_recovered"
            if group_changed
            and transition.new_group_status == "available"
            and transition.old_group_status in INVALID_UPSTREAM_GROUP_STATUSES
            else "upstream_group_status_change"
        )
        known_secrets = (
            *self._known_local_secrets(config),
            *self._known_channel_secrets(channel),
        )
        db.add(
            UpstreamRateChangeLog(
                management_account_id=config.management_account_id,
                account_name=_safe_text(
                    config.remote_name,
                    secrets=known_secrets,
                    limit=200,
                ),
                upstream_id=channel.id if channel is not None else config.upstream_id,
                upstream_name=_safe_text(
                    channel.display_name if channel is not None else None,
                    secrets=known_secrets,
                    limit=200,
                ),
                group_id=_safe_text(
                    config.selected_group_id,
                    secrets=known_secrets,
                    limit=128,
                ),
                group_name=_safe_text(
                    config.selected_group_name,
                    secrets=known_secrets,
                    limit=200,
                ),
                old_group_multiplier=previous_group_multiplier,
                new_group_multiplier=config.upstream_group_multiplier,
                old_upstream_multiplier=_calculate_upstream_actual_multiplier(
                    previous_group_multiplier,
                    previous_recharge_multiplier,
                ),
                new_upstream_multiplier=_calculate_upstream_actual_multiplier(
                    config.upstream_group_multiplier,
                    config.upstream_recharge_multiplier,
                ),
                old_upstream_recharge_multiplier=previous_recharge_multiplier,
                new_upstream_recharge_multiplier=config.upstream_recharge_multiplier,
                upstream_recharge_multiplier=config.upstream_recharge_multiplier,
                management_recharge_multiplier=config.management_recharge_multiplier,
                old_expected_management_billing_multiplier=previous_expected_management_billing_multiplier,
                new_expected_management_billing_multiplier=config.expected_management_billing_multiplier,
                old_management_billing_multiplier=previous_management_billing_multiplier,
                new_management_billing_multiplier=config.management_billing_multiplier,
                old_upstream_key_status=transition.old_key_status,
                new_upstream_key_status=transition.new_key_status,
                old_upstream_group_status=transition.old_group_status,
                new_upstream_group_status=transition.new_group_status,
                old_remote_schedulable=old_remote_schedulable,
                new_remote_schedulable=new_remote_schedulable,
                reason=reason,
                status=action_status or "observed",
                safe_error=safe_error,
            )
        )

    async def _lock_for(self, account_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(account_id, asyncio.Lock())

    def _new_config(
        self,
        remote: dict[str, Any],
        account_id: int,
        *,
        secrets: tuple[str | None, ...] = (),
    ) -> ApiAccount:
        snapshot = self._safe_remote_snapshot(remote, secrets=secrets)
        return ApiAccount(
            management_account_id=account_id,
            remote_identity_fingerprint=self._remote_binding_fingerprint(remote),
            remote_name=_safe_text(
                self._remote_name(remote, account_id, secrets=secrets),
                secrets=secrets,
                limit=200,
            ),
            remote_platform=_safe_text(
                self.sub2api.account_platform(remote),
                secrets=secrets,
                limit=64,
            ),
            remote_account_type=_safe_text(
                self.sub2api.account_type(remote),
                secrets=secrets,
                limit=32,
            ),
            remote_status=_safe_text(self.sub2api.account_status(remote), limit=64),
            remote_schedulable=self.sub2api.account_schedulable(remote),
            remote_priority=self.sub2api.account_priority(remote),
            remote_snapshot=snapshot,
            remote_snapshot_updated_at=_utcnow(),
            remote_present=True,
            api_endpoint_url=_safe_text(
                _remote_base_url(remote),
                secrets=secrets,
                limit=500,
            ),
            platform_type="auto",
            group_multiplier_status="not_discovered",
            recharge_multiplier_status="not_discovered",
            management_recharge_status="not_checked",
            balance_status="not_checked",
            availability_check_mode="upstream_monitor",
            availability_status="not_configured",
            management_billing_multiplier=self._remote_management_billing_multiplier(remote),
        )

    def _safe_remote_snapshot(
        self,
        remote: dict[str, Any],
        *,
        secrets: tuple[str | None, ...] = (),
    ) -> dict[str, Any]:
        account_id = self._numeric_remote_id(remote)
        if account_id is None:
            raise ApiAccountServiceError("sub2api returned an invalid API key account id.")
        created_at = _safe_text(_value(remote, "created_at", "createdAt"), limit=80)
        return {
            "id": account_id,
            "name": _safe_text(self.sub2api.account_name(remote), secrets=secrets, limit=200) or f"Account #{account_id}",
            "platform": _safe_text(self.sub2api.account_platform(remote), secrets=secrets, limit=64),
            "type": _safe_text(self.sub2api.account_type(remote), secrets=secrets, limit=32),
            "status": _safe_text(self.sub2api.account_status(remote), secrets=secrets, limit=64),
            "schedulable": self.sub2api.account_schedulable(remote),
            "priority": self.sub2api.account_priority(remote),
            "rate_multiplier": self._remote_management_billing_multiplier(remote),
            "created_at": created_at,
            # Keep the upstream-owned timestamp in the local display snapshot.
            # The account list endpoint intentionally reads this snapshot to
            # avoid a remote request on every page load.
            "last_used_at": _datetime_text(self.sub2api.account_last_used_at(remote)),
            "base_url": _safe_text(_remote_base_url(remote), secrets=secrets, limit=500),
            "_cached": True,
            "_identity_fingerprint": self._remote_identity_fingerprint(remote),
            "_binding_fingerprint": self._remote_binding_fingerprint(remote),
        }

    def apply_remote_snapshot(
        self,
        config: ApiAccount,
        remote: dict[str, Any],
        *,
        now: datetime | None = None,
        secrets: tuple[str | None, ...] = (),
        include_stored_secrets: bool = True,
    ) -> None:
        snapshot = self._safe_remote_snapshot(
            remote,
            secrets=(
                (*self._known_local_secrets(config), *secrets)
                if include_stored_secrets
                else secrets
            ),
        )
        config.remote_snapshot = snapshot
        config.remote_snapshot_updated_at = now or _utcnow()
        config.remote_name = snapshot["name"]
        config.remote_platform = snapshot["platform"]
        config.remote_account_type = snapshot["type"]
        config.remote_status = snapshot["status"]
        config.remote_schedulable = snapshot["schedulable"]
        config.remote_priority = snapshot["priority"]
        if snapshot["rate_multiplier"] is not None:
            config.management_billing_multiplier = snapshot["rate_multiplier"]

    async def cached_remote_accounts(self, db: AsyncSession) -> list[dict[str, Any]]:
        result = await db.execute(
            select(ApiAccount)
            .where(ApiAccount.remote_present.is_(True))
            .order_by(ApiAccount.management_account_id)
        )
        accounts: list[dict[str, Any]] = []
        for config in result.scalars().all():
            snapshot = config.remote_snapshot
            if not isinstance(snapshot, dict):
                snapshot = {
                    "id": config.management_account_id,
                    "name": config.remote_name or f"Account #{config.management_account_id}",
                    "platform": config.remote_platform,
                    "type": config.remote_account_type,
                    "status": config.remote_status,
                    "schedulable": config.remote_schedulable,
                    "priority": config.remote_priority,
                    "rate_multiplier": config.management_billing_multiplier,
                    "created_at": (
                        config.created_at.isoformat() if config.created_at is not None else None
                    ),
                    "last_used_at": None,
                    "api_endpoint_url": config.api_endpoint_url,
                    "_cached": True,
                    "_identity_fingerprint": hashlib.sha256(
                        f"cached:{config.management_account_id}:{config.remote_identity_fingerprint or ''}".encode("utf-8")
                    ).hexdigest(),
                    "_binding_fingerprint": config.remote_identity_fingerprint,
                }
            accounts.append(dict(snapshot))
        return accounts

    @staticmethod
    def _invalidate_preview(
        config: ApiAccount,
        *,
        clear_upstream_identity: bool = False,
    ) -> None:
        if clear_upstream_identity:
            config.remote_upstream_api_key_id = None
            config.upstream_api_key_id = None
            config.upstream_identity_rebind_required = False
            config.resolved_platform_type = None
        config.group_options = []
        config.discovered_upstream_group_multiplier = None
        config.upstream_group_multiplier = None
        config.group_multiplier_source = None
        config.group_multiplier_status = "not_discovered"
        config.discovered_upstream_recharge_multiplier = None
        config.upstream_recharge_multiplier = None
        config.recharge_multiplier_source = None
        config.recharge_multiplier_status = "not_discovered"
        config.expected_management_billing_multiplier = None
        config.last_discovered_at = None
        config.last_error = None
        config.upstream_key_status = "not_checked"
        config.upstream_group_status = "not_checked"
        config.upstream_health_invalid_count = 0
        config.upstream_key_checked_at = None
        config.upstream_group_checked_at = None
        if config.priority_interval_id is not None:
            config.desired_priority = None
            config.priority_sync_status = "multiplier_unavailable"
            config.priority_sync_error = None

    @staticmethod
    def _upstream_record_id(state: Any) -> int | None:
        raw_id = _value(state, "key_record_id")
        if isinstance(raw_id, bool):
            return None
        try:
            parsed = int(raw_id) if raw_id is not None else 0
        except (TypeError, ValueError):
            return None
        return parsed if 0 < parsed <= SIGNED_BIGINT_MAX else None

    async def apply_upstream_record_identity(
        self,
        db: AsyncSession,
        config: ApiAccount,
        state: Any,
        *,
        observed_upstream_type: str | None = None,
        record_owners: dict[int, int] | None = None,
        ambiguous_unbound_record_ids: set[int] | None = None,
    ) -> UpstreamRecordIdentityStatus:
        """Bind a verified upstream key row without silently replacing it."""
        observed_id = self._upstream_record_id(state)
        normalized_observed_type = str(observed_upstream_type or "").strip().lower()
        if normalized_observed_type not in {"newapi", "sub2api"}:
            normalized_observed_type = ""
        stored_id = config.remote_upstream_api_key_id
        stored_upstream_type = str(
            config.resolved_platform_type
            or (config.platform_type if config.platform_type in {"newapi", "sub2api"} else "")
        ).strip().lower()
        if (
            stored_id is not None
            and stored_upstream_type
            and normalized_observed_type
            and stored_upstream_type != normalized_observed_type
        ):
            config.upstream_identity_rebind_required = True
            config.last_error = (
                f"The upstream platform changed from {stored_upstream_type} to "
                f"{normalized_observed_type}; explicit rebind is required before "
                "accepting an API key record from the new platform."
            )
            return UpstreamRecordIdentityStatus.REBIND_REQUIRED
        if observed_id is None:
            if config.upstream_identity_rebind_required:
                return UpstreamRecordIdentityStatus.REBIND_REQUIRED
            if stored_id is not None:
                return UpstreamRecordIdentityStatus.INCONCLUSIVE
            if normalized_observed_type:
                config.resolved_platform_type = normalized_observed_type
            return UpstreamRecordIdentityStatus.VERIFIED

        if stored_id is None and config.upstream_identity_rebind_required:
            return UpstreamRecordIdentityStatus.REBIND_REQUIRED

        owner_id: int | None = None
        if record_owners is not None:
            owner_id = record_owners.get(observed_id)
        if owner_id is None and config.upstream_id is not None:
            with db.no_autoflush:
                owner_id = await db.scalar(
                    select(ApiAccount.id)
                    .where(
                        ApiAccount.upstream_id == config.upstream_id,
                        ApiAccount.remote_upstream_api_key_id == observed_id,
                        ApiAccount.id != config.id,
                    )
                    .limit(1)
                )

        ambiguous = bool(
            ambiguous_unbound_record_ids
            and observed_id in ambiguous_unbound_record_ids
            and stored_id is None
        )
        if (owner_id is not None and owner_id != config.id) or ambiguous:
            config.upstream_identity_rebind_required = True
            config.last_error = (
                f"Upstream API key record #{observed_id} matches more than one local "
                "account on this channel; explicit rebind is required."
            )
            return UpstreamRecordIdentityStatus.REBIND_REQUIRED
        if stored_id is not None and stored_id != observed_id:
            config.upstream_identity_rebind_required = True
            config.last_error = (
                f"The upstream API key record ID changed from #{stored_id} to "
                f"#{observed_id}; explicit rebind is required."
            )
            return UpstreamRecordIdentityStatus.REBIND_REQUIRED

        config.remote_upstream_api_key_id = observed_id
        config.upstream_identity_rebind_required = False
        if normalized_observed_type:
            config.resolved_platform_type = normalized_observed_type
        if record_owners is not None:
            record_owners[observed_id] = config.id
        return UpstreamRecordIdentityStatus.VERIFIED

    def _numeric_remote_id(self, account: dict[str, Any]) -> int | None:
        raw_id = self.sub2api.account_id(account)
        try:
            parsed = int(raw_id) if raw_id is not None else 0
        except (TypeError, ValueError):
            return None
        return parsed if 0 < parsed <= JS_SAFE_INTEGER_MAX else None

    async def _remote_accounts(self) -> list[dict[str, Any]]:
        try:
            accounts = await self.sub2api.list_api_key_accounts()
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ApiAccountServiceError("Unable to read API key accounts from sub2api.") from None

        seen_ids: set[int] = set()
        for account in accounts:
            account_id = self._numeric_remote_id(account)
            if account_id is None:
                continue
            if account_id in seen_ids:
                raise ApiAccountServiceError("sub2api returned duplicate API key account ids.")
            seen_ids.add(account_id)
        return accounts

    async def sync_available_models(
        self,
        db: AsyncSession,
        configs: dict[int, ApiAccount],
        remote_by_id: dict[int, dict[str, Any]],
        *,
        force: bool | None = None,
    ) -> int:
        if force is None:
            # Full whitelist refreshes are handled by the independent model
            # whitelist scheduler. Inventory sync only repairs missing data.
            force = False
        targets = [
            (
                account_id,
                remote_by_id[account_id],
                config.available_models_checked_at,
            )
            for account_id, config in configs.items()
            if account_id in remote_by_id
            and (force or config.available_models is None)
        ]
        if not targets:
            return 0

        # Inventory queries may have opened a new read transaction after their
        # write commit. Close it before requesting model lists from sub2api.
        await db.commit()
        semaphore = asyncio.Semaphore(8)

        async def fetch(
            account_id: int,
            remote: dict[str, Any],
            baseline_checked_at: datetime | None,
        ) -> tuple[int, datetime | None, list[dict[str, str]] | None, datetime]:
            async with semaphore:
                try:
                    models = _normalize_available_models(
                        await self.sub2api.get_account_models(remote)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return account_id, baseline_checked_at, None, _utcnow()
            return account_id, baseline_checked_at, models, _utcnow()

        results = await asyncio.gather(
            *(fetch(*target) for target in targets)
        )
        current_result = await db.execute(
            select(ApiAccount)
            .where(
                ApiAccount.management_account_id.in_(
                    [result[0] for result in results]
                )
            )
            .execution_options(populate_existing=True)
        )
        current_by_account_id = {
            config.management_account_id: config
            for config in current_result.scalars().all()
        }
        synchronized = 0
        for account_id, baseline_checked_at, models, checked_at in results:
            config = current_by_account_id.get(account_id)
            if (
                config is None
                or _model_check_version(config.available_models_checked_at)
                != _model_check_version(baseline_checked_at)
                or (not force and config.available_models is not None)
            ):
                continue
            config.available_models_status = "ok" if models is not None else "error"
            config.available_models_checked_at = checked_at
            if models is not None:
                config.available_models = models
                synchronized += 1
        await db.commit()
        return synchronized

    async def _remote_account(
        self,
        account_id: int,
        expected_identity_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        for account in await self._remote_accounts():
            if self._numeric_remote_id(account) == account_id:
                if (
                    expected_identity_fingerprint is not None
                    and self._remote_identity_fingerprint(account) != expected_identity_fingerprint
                ):
                    raise ApiAccountServiceError(
                        "The sub2api account identity changed; refresh the account list and try again.",
                        status_code=409,
                    )
                return account
        raise ApiAccountServiceError("The sub2api API key account was not found.", status_code=404)

    def _remote_identity_fingerprint(self, account: dict[str, Any]) -> str:
        cached = str(account.get("_identity_fingerprint") or "").strip().lower()
        if account.get("_cached") is True and len(cached) == 64:
            return cached
        fingerprint = self._remote_fingerprint(account, include_endpoint=True)
        if fingerprint is None:
            raise ApiAccountServiceError("sub2api returned an invalid API key account identity.")
        return fingerprint

    def _remote_binding_fingerprint(self, account: dict[str, Any]) -> str | None:
        # Persistent ownership follows the immutable Sub2API account row, not
        # mutable presentation fields such as its name or API key.
        return self._remote_fingerprint(
            account,
            include_endpoint=False,
            include_name=False,
        )

    def _legacy_remote_binding_fingerprint(self, account: dict[str, Any]) -> str | None:
        return self._remote_fingerprint(
            account,
            include_endpoint=False,
            include_name=True,
        )

    def _known_local_secrets(
        self,
        config: ApiAccount | None,
    ) -> tuple[str | None, str | None]:
        if config is None:
            return None, None
        return (
            decrypt_text(config.encrypted_api_key),
            decrypt_text(config.encrypted_access_token),
        )

    @staticmethod
    def _known_channel_secrets(
        channel: Upstream | None,
    ) -> tuple[str | None, str | None]:
        if channel is None:
            return None, None
        return (
            decrypt_text(channel.encrypted_access_token),
            decrypt_text(channel.encrypted_refresh_token),
        )

    @staticmethod
    async def _channel_for_config(
        db: AsyncSession,
        config: ApiAccount | None,
    ) -> Upstream | None:
        if config is None or config.upstream_id is None:
            return None
        return await db.get(Upstream, config.upstream_id)

    def _remote_rename_guard_fingerprint(self, account: dict[str, Any]) -> str:
        fingerprint = self._remote_fingerprint(
            account,
            include_endpoint=True,
            include_name=False,
            require_created_at=True,
        )
        if fingerprint is None:
            raise ApiAccountServiceError(
                "sub2api did not provide the immutable account creation timestamp required "
                "to rename this API key account.",
                status_code=409,
            )
        return fingerprint

    def _require_remote_binding_fingerprint(self, account: dict[str, Any]) -> str:
        fingerprint = self._remote_binding_fingerprint(account)
        if fingerprint is None:
            raise ApiAccountServiceError(
                "sub2api did not provide the immutable account creation timestamp required "
                "to confirm this API key account identity.",
                status_code=409,
            )
        return fingerprint

    def _remote_fingerprint(
        self,
        account: dict[str, Any],
        *,
        include_endpoint: bool,
        include_name: bool = True,
        require_created_at: bool = False,
    ) -> str | None:
        account_id = self._numeric_remote_id(account)
        if account_id is None:
            raise ApiAccountServiceError("sub2api returned an invalid API key account id.")

        def normalized_text(value: Any, *, casefold: bool = False) -> str:
            text = unicodedata.normalize("NFKC", str(value or "").strip())
            return text.casefold() if casefold else text

        account_type = normalized_text(self.sub2api.account_type(account), casefold=True)
        account_type = account_type.replace("-", "_").replace(" ", "_")
        created_at = normalized_text(_value(account, "created_at", "createdAt"))
        if (not include_endpoint or require_created_at) and not created_at:
            return None
        identity = {
            "id": account_id,
            "platform": normalized_text(self.sub2api.account_platform(account), casefold=True),
            "type": account_type,
            "created_at": created_at,
        }
        if include_name:
            identity["name"] = normalized_text(self.sub2api.account_name(account))
        if include_endpoint:
            identity["base_url"] = _remote_base_url(account) or ""
        encoded = json.dumps(
            identity,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _config_binding_status(
        self,
        account: dict[str, Any],
        config: ApiAccount | None,
    ) -> str:
        if config is None:
            return "unmanaged"
        stored = (config.remote_identity_fingerprint or "").strip().lower()
        if not stored:
            return "unbound"
        cached_binding = str(account.get("_binding_fingerprint") or "").strip().lower()
        if account.get("_cached") is True and len(cached_binding) == 64:
            return "bound" if stored == cached_binding else "mismatch"
        current = self._remote_binding_fingerprint(account)
        if current is None:
            return "mismatch"
        if stored == current:
            return "bound"
        legacy = self._legacy_remote_binding_fingerprint(account)
        return "bound" if legacy is not None and stored == legacy else "mismatch"

    def _config_binding_evidence_incomplete(
        self,
        account: dict[str, Any],
        config: ApiAccount | None,
    ) -> bool:
        if config is None or not str(config.remote_identity_fingerprint or "").strip():
            return False
        if account.get("_cached") is True:
            return False
        return self._remote_binding_fingerprint(account) is None

    def _config_binding_differs_only_by_remote_name(
        self,
        account: dict[str, Any],
        config: ApiAccount,
        *,
        extra_secrets: tuple[str | None, ...] = (),
    ) -> bool:
        stored_fingerprint = str(config.remote_identity_fingerprint or "").strip().lower()
        stored_name = _safe_text(config.remote_name, limit=200)
        current_name = _safe_text(self.sub2api.account_name(account), limit=200)
        if not stored_fingerprint or not current_name:
            return False
        configured_endpoint = _normalize_base_url(config.api_endpoint_url)
        remote_endpoint = _remote_base_url(account)
        if (
            configured_endpoint is not None
            and remote_endpoint is not None
            and configured_endpoint != remote_endpoint
        ):
            return False
        previous_names: list[str | None] = [stored_name]
        for secret in (*self._known_local_secrets(config), *extra_secrets):
            previous_names.append(secret)
            cleaned = str(secret or "").strip()
            if cleaned.lower().startswith("bearer ") and cleaned[7:].strip():
                previous_names.append(cleaned[7:].strip())
        for previous_name in dict.fromkeys(previous_names):
            if not previous_name or previous_name == current_name:
                continue
            previous_name_view = dict(account)
            previous_name_view["name"] = previous_name
            if self._legacy_remote_binding_fingerprint(previous_name_view) == stored_fingerprint:
                return True
        return False

    def _require_config_binding(
        self,
        account: dict[str, Any],
        config: ApiAccount | None,
    ) -> None:
        status = self._config_binding_status(account, config)
        if status in {"unbound", "mismatch"}:
            raise ApiAccountServiceError(
                "The local upstream configuration is bound to a different or unconfirmed "
                "sub2api account identity; explicitly rebind it before continuing.",
                status_code=409,
            )

    def _remote_management_billing_multiplier(self, account: dict[str, Any]) -> float | None:
        return _float_multiplier(account.get("rate_multiplier"), allow_zero=True)

    def _remote_name(
        self,
        account: dict[str, Any],
        account_id: int,
        *,
        secrets: tuple[str | None, ...] = (),
    ) -> str:
        name = self.sub2api.account_name(account)
        return _safe_text(name, secrets=secrets, limit=200) or f"Account #{account_id}"

    def _group_options_out(
        self,
        config: ApiAccount | None,
        *,
        secrets: tuple[str | None, ...] = (),
    ) -> list[UpstreamGroupOptionOut]:
        if config is None:
            return []
        return [
            UpstreamGroupOptionOut(**item)
            for item in _sanitize_group_options(config.group_options, secrets=secrets)
        ]

    def _build_out(
        self,
        account: dict[str, Any],
        config: ApiAccount | None,
        *,
        extra_secrets: tuple[str | None, ...] = (),
        upstream_name: str | None = None,
        upstream_id: str | None = None,
        priority_interval_name: str | None = None,
        priority_interval: UpstreamPriorityInterval | None = None,
    ) -> ApiAccountOut:
        account_id = self._numeric_remote_id(account)
        if account_id is None:
            raise ApiAccountServiceError("sub2api returned an invalid API key account id.")
        secrets = (*self._known_local_secrets(config), *extra_secrets)
        binding_status = self._config_binding_status(account, config)
        identity_rebind_required = binding_status in {"unbound", "mismatch"}
        api_key_origin_rebind_required = bool(
            config is not None and config.api_key_origin_rebind_required
        )
        upstream_identity_rebind_required = bool(
            config is not None and config.upstream_identity_rebind_required
        )
        if binding_status != "bound":
            config = None
        remote_management_billing_multiplier = self._remote_management_billing_multiplier(account)
        management_billing_multiplier = remote_management_billing_multiplier
        if management_billing_multiplier is None and config is not None:
            management_billing_multiplier = config.management_billing_multiplier
        expected_management_billing_multiplier = config.expected_management_billing_multiplier if config is not None else None
        would_change: bool | None = None
        if management_billing_multiplier is not None and expected_management_billing_multiplier is not None:
            current = _decimal_multiplier(management_billing_multiplier, allow_zero=True)
            target = _decimal_multiplier(expected_management_billing_multiplier, allow_zero=True)
            if current is not None and target is not None:
                would_change = _quantize_rate(current) != _quantize_rate(target)

        recharge_source = config.recharge_multiplier_source if config is not None else None
        discovered_recharge = config.discovered_upstream_recharge_multiplier if config is not None else None
        if recharge_source == "default":
            discovered_recharge = None
        active_pause_holds = self.active_pause_holds(config) if config is not None else []
        rate_pause_policy = resolve_rate_pause_policy(config, priority_interval)
        today_upstream_cost_cny = _cny_amount(
            config.today_upstream_wallet_cost_usd if config else None,
            config.upstream_recharge_multiplier if config else None,
        )
        today_management_account_cost_cny = _cny_amount(
            config.today_management_account_cost_usd if config else None,
            config.management_recharge_multiplier if config else None,
        )
        today_income_cny = _cny_amount(
            config.today_management_user_charge_usd if config else None,
            config.management_recharge_multiplier if config else None,
        )
        today_consumption_cny = _maximum_known_cost(
            today_upstream_cost_cny,
            today_management_account_cost_cny,
        )
        today_profit_cny = (
            today_income_cny - today_consumption_cny
            if today_income_cny is not None and today_consumption_cny is not None
            else None
        )

        return ApiAccountOut(
            management_account_id=account_id,
            identity_fingerprint=self._remote_identity_fingerprint(account),
            identity_binding_status=binding_status,
            identity_rebind_required=identity_rebind_required,
            api_key_origin_rebind_required=api_key_origin_rebind_required,
            upstream_identity_rebind_required=upstream_identity_rebind_required,
            remote_key_id=(
                str(config.remote_upstream_api_key_id)
                if config is not None and config.remote_upstream_api_key_id is not None
                else None
            ),
            upstream_api_key_id=(config.upstream_api_key_id if config is not None else None),
            remote_name=(
                _safe_text(
                    self._remote_name(account, account_id, secrets=secrets),
                    secrets=secrets,
                    limit=200,
                )
                or f"Account #{account_id}"
            ),
            remote_platform=(
                _safe_text(self.sub2api.account_platform(account), secrets=secrets, limit=64)
                or _safe_text(config.remote_platform if config else None, secrets=secrets, limit=64)
            ),
            remote_account_type=(
                _safe_text(self.sub2api.account_type(account), secrets=secrets, limit=32)
                or _safe_text(config.remote_account_type if config else None, secrets=secrets, limit=32)
            ),
            remote_status=_safe_text(self.sub2api.account_status(account), secrets=secrets, limit=32),
            remote_schedulable=self.sub2api.account_schedulable(account),
            priority=self.sub2api.account_priority(account),
            remote_present=bool(config.remote_present) if config is not None else True,
            remote_snapshot_updated_at=(
                config.remote_snapshot_updated_at if config is not None else None
            ),
            remote_missing_at=config.remote_missing_at if config is not None else None,
            desired_priority=config.desired_priority if config is not None else None,
            priority_interval_id=config.priority_interval_id if config is not None else None,
            priority_interval_name=(
                _safe_text(priority_interval_name, secrets=secrets, limit=100)
                if config is not None
                else None
            ),
            priority_sync_status=(
                config.priority_sync_status if config is not None else "unassigned"
            ),
            priority_sync_error=(
                _safe_text(config.priority_sync_error, secrets=secrets, limit=300)
                if config is not None
                else None
            ),
            priority_tiebreak_order=(
                config.priority_tiebreak_order if config is not None else None
            ),
            priority_tiebreak_multiplier=(
                config.priority_tiebreak_multiplier if config is not None else None
            ),
            priority_assignment_when_disabled=(
                config.priority_assignment_when_disabled if config is not None else None
            ),
            rate_pause_policy=(config.rate_pause_policy if config is not None else "inherit"),
            rate_pause_effective_enabled=bool(rate_pause_policy["enabled"]),
            rate_pause_effective_source=rate_pause_policy["source"],
            rate_absolute_threshold=rate_pause_policy["absolute_threshold"],
            upstream_actual_multiplier=(
                _calculate_upstream_actual_multiplier(
                    config.upstream_group_multiplier,
                    config.upstream_recharge_multiplier,
                )
                if config is not None
                else None
            ),
            managed=config is not None,
            upstream_id=upstream_id if config is not None else None,
            upstream_name=(
                _safe_text(upstream_name, secrets=secrets, limit=200)
                if config is not None
                else None
            ),
            api_endpoint_url=_safe_text(
                config.api_endpoint_url if config is not None else _remote_base_url(account),
                secrets=secrets,
                limit=500,
            ),
            platform_type=(config.platform_type if config and config.platform_type in {"auto", "newapi", "sub2api"} else "auto"),
            resolved_platform_type=(
                config.resolved_platform_type
                if config and config.resolved_platform_type in {"newapi", "sub2api"}
                else None
            ),
            upstream_user_id=_safe_text(config.upstream_user_id if config else None, secrets=secrets, limit=128),
            selected_group_id=_safe_text(config.selected_group_id if config else None, secrets=secrets, limit=128),
            selected_group_name=_safe_text(config.selected_group_name if config else None, secrets=secrets, limit=200),
            upstream_key_status=(config.upstream_key_status or "not_checked") if config else "not_checked",
            upstream_group_status=(config.upstream_group_status or "not_checked") if config else "not_checked",
            upstream_health_invalid_count=(
                min(2, max(0, int(config.upstream_health_invalid_count or 0)))
                if config
                else 0
            ),
            upstream_key_checked_at=config.upstream_key_checked_at if config else None,
            upstream_group_checked_at=config.upstream_group_checked_at if config else None,
            availability_check_mode=(
                config.availability_check_mode
                if config and config.availability_check_mode in {
                    "upstream_monitor",
                    "independent_model",
                    "disabled",
                }
                else "disabled"
            ),
            availability_monitor_id=config.availability_monitor_id if config else None,
            availability_test_model=(
                _safe_text(config.availability_test_model, secrets=secrets, limit=160)
                if config
                else None
            ),
            available_models=(
                _normalize_available_models(config.available_models)
                if config
                else []
            ),
            available_models_status=(
                config.available_models_status or "not_checked"
                if config
                else "not_checked"
            ),
            available_models_checked_at=(
                config.available_models_checked_at if config else None
            ),
            availability_status=(config.availability_status or "not_checked") if config else "not_checked",
            availability_unavailable_count=(
                min(100, max(0, int(config.availability_unavailable_count or 0)))
                if config
                else 0
            ),
            availability_recovery_count=(
                min(100, max(0, int(config.availability_recovery_count or 0)))
                if config
                else 0
            ),
            availability_checked_at=config.availability_checked_at if config else None,
            availability_source=config.availability_source if config else None,
            availability_message=(
                _safe_text(config.availability_message, secrets=secrets, limit=300)
                if config
                else None
            ),
            auto_disabled_reason=config.auto_disabled_reason if config else None,
            last_auto_disabled_at=config.last_auto_disabled_at if config else None,
            active_pause_holds=[
                {
                    "reason": hold.reason,
                    "triggered_at": hold.triggered_at,
                    "recovery_mode": hold.recovery_mode,
                    "scope_upstream_id": (
                        upstream_id
                        if config is not None
                        and hold.scope_upstream_id == config.upstream_id
                        else None
                    ),
                    "evidence": _safe_pause_evidence(hold.evidence_json),
                }
                for hold in active_pause_holds
            ],
            pause_owned_by_plugin=bool(config and config.pause_owned_by_plugin),
            auto_restore_eligible=bool(config and config.pause_owned_by_plugin),
            auto_pause_episode_id=config.auto_pause_episode_id if config else None,
            auto_pause_upstream_id=(
                upstream_id
                if config is not None
                and config.auto_pause_upstream_id == config.upstream_id
                else None
            ),
            auto_paused_at=config.auto_paused_at if config else None,
            balance_guard_restore_eligible=bool(
                config and config.balance_guard_restore_eligible
            ),
            balance_guard_upstream_id=(
                upstream_id
                if config is not None
                and config.balance_guard_upstream_id == config.upstream_id
                else None
            ),
            balance_guard_paused_at=config.balance_guard_paused_at if config else None,
            api_key_set=bool(
                not api_key_origin_rebind_required
                and (
                    (config and config.encrypted_api_key)
                    or self.sub2api.account_has_api_key(account)
                )
            ),
            api_key_hint=None,
            access_token_set=bool(config and config.encrypted_access_token),
            upstream_group_multiplier_override=config.upstream_group_multiplier_override if config else None,
            upstream_recharge_multiplier_override=config.upstream_recharge_multiplier_override if config else None,
            group_options=self._group_options_out(config, secrets=secrets),
            discovered_upstream_group_multiplier=config.discovered_upstream_group_multiplier if config else None,
            upstream_group_multiplier=config.upstream_group_multiplier if config else None,
            group_multiplier_source=config.group_multiplier_source if config else None,
            group_multiplier_status=config.group_multiplier_status if config else "unmanaged",
            discovered_upstream_recharge_multiplier=discovered_recharge,
            upstream_recharge_multiplier=config.upstream_recharge_multiplier if config else None,
            recharge_multiplier_source=recharge_source,
            recharge_multiplier_status=config.recharge_multiplier_status if config else "unmanaged",
            management_recharge_multiplier=config.management_recharge_multiplier if config else None,
            management_recharge_source=config.management_recharge_source if config else None,
            management_recharge_status=config.management_recharge_status if config else "not_checked",
            management_billing_multiplier=management_billing_multiplier,
            expected_management_billing_multiplier=expected_management_billing_multiplier,
            would_change=would_change,
            wallet_balance_usd=config.wallet_balance_usd if config else None,
            wallet_total_usd=config.wallet_total_usd if config else None,
            wallet_used_usd=config.wallet_used_usd if config else None,
            balance_unit=_safe_text(config.balance_unit if config else None, secrets=secrets, limit=32),
            balance_status=config.balance_status if config else "not_checked",
            balance_source=config.balance_source if config else None,
            balance_message=_safe_text(config.balance_message if config else None, secrets=secrets, limit=300),
            balance_checked_at=config.balance_checked_at if config else None,
            upstream_wallet_cost_usd=config.upstream_wallet_cost_usd if config else None,
            upstream_usage_unit=_safe_text(
                config.upstream_usage_unit if config else None,
                secrets=secrets,
                limit=32,
            ),
            upstream_usage_checked_at=config.upstream_usage_checked_at if config else None,
            today_upstream_wallet_cost_usd=(
                config.today_upstream_wallet_cost_usd if config else None
            ),
            today_upstream_usage_unit=_safe_text(
                config.today_upstream_usage_unit if config else None,
                secrets=secrets,
                limit=32,
            ),
            today_upstream_usage_status=(
                config.today_upstream_usage_status if config else "not_checked"
            ),
            today_upstream_usage_source=_safe_text(
                config.today_upstream_usage_source if config else None,
                secrets=secrets,
                limit=64,
            ),
            today_upstream_usage_checked_at=(
                config.today_upstream_usage_checked_at if config else None
            ),
            today_upstream_actual_cost_cny=today_upstream_cost_cny,
            today_management_account_cost_cny=today_management_account_cost_cny,
            today_actual_income_cny=today_income_cny,
            today_consumption_cny=today_consumption_cny,
            today_profit_cny=today_profit_cny,
            today_management_site_stats_status=(
                config.today_management_site_stats_status if config else "not_checked"
            ),
            today_management_site_stats_checked_at=(
                config.today_management_site_stats_checked_at if config else None
            ),
            # Read live from the upstream payload: sub2api owns this timestamp,
            # so there is nothing to persist or infer locally.
            last_used_at=self.sub2api.account_last_used_at(account),
            last_error=(
                config.last_error
                if config
                else "Local upstream configuration requires explicit identity rebind."
                if identity_rebind_required
                else None
            ),
            last_discovered_at=config.last_discovered_at if config else None,
            last_applied_at=config.last_applied_at if config else None,
            created_at=config.created_at if config else None,
            updated_at=config.updated_at if config else None,
        )

    async def list_accounts(
        self,
        db: AsyncSession,
        *,
        use_cache: bool = False,
    ) -> list[ApiAccountOut]:
        remote_accounts = (
            await self.cached_remote_accounts(db)
            if use_cache
            else await self._remote_accounts()
        )
        remote_by_id = {
            account_id: account
            for account in remote_accounts
            if (account_id := self._numeric_remote_id(account)) is not None
        }
        configs: dict[int, ApiAccount] = {}
        if remote_by_id:
            result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id.in_(sorted(remote_by_id))
                )
            )
            configs = {item.management_account_id: item for item in result.scalars().all()}
        restored = False
        restored_at = _utcnow()
        for account_id, config in configs.items():
            if config.deleted_at is None:
                continue
            remote = remote_by_id[account_id]
            binding_status = self._config_binding_status(remote, config)
            name_only_change = (
                binding_status == "mismatch"
                and self._config_binding_differs_only_by_remote_name(remote, config)
            )
            if binding_status != "bound" and not name_only_change:
                continue
            if name_only_change:
                config.remote_identity_fingerprint = (
                    self._require_remote_binding_fingerprint(remote)
                )
            config.deleted_at = None
            config.remote_present = True
            config.remote_missing_at = None
            config.last_seen_at = restored_at
            self.apply_remote_snapshot(config, remote)
            restored = True
        if restored:
            await db.commit()
        upstream_ids = sorted(
            {
                config.upstream_id
                for config in configs.values()
                if config.upstream_id is not None
            }
        )
        channels: dict[int, Upstream] = {}
        if upstream_ids:
            result = await db.execute(
                select(Upstream).where(Upstream.id.in_(upstream_ids))
            )
            channels = {item.id: item for item in result.scalars().all()}
        priority_interval_ids = sorted(
            {
                config.priority_interval_id
                for config in configs.values()
                if config.priority_interval_id is not None
            }
        )
        priority_intervals: dict[int, UpstreamPriorityInterval] = {}
        if priority_interval_ids:
            result = await db.execute(
                select(UpstreamPriorityInterval).where(
                    UpstreamPriorityInterval.id.in_(priority_interval_ids)
                )
            )
            priority_intervals = {item.id: item for item in result.scalars().all()}
        assign_disabled_globally = False
        try:
            assign_disabled_globally = bool(
                await get_runtime_config_service().get_priority_assign_disabled_api_key_accounts()
            )
        except Exception:
            pass
        accounts = [
            self._build_out(
                remote_by_id[account_id],
                configs.get(account_id),
                extra_secrets=self._known_channel_secrets(
                    channels.get(configs[account_id].upstream_id)
                    if account_id in configs and configs[account_id].upstream_id is not None
                    else None
                ),
                upstream_name=(
                    channels[configs[account_id].upstream_id].display_name
                    if account_id in configs
                    and configs[account_id].upstream_id in channels
                    else None
                ),
                upstream_id=(
                    channels[configs[account_id].upstream_id].id
                    if account_id in configs
                    and configs[account_id].upstream_id in channels
                    else None
                ),
                priority_interval_name=(
                    priority_intervals[configs[account_id].priority_interval_id].name
                    if account_id in configs
                    and configs[account_id].priority_interval_id in priority_intervals
                    else None
                ),
                priority_interval=(
                    priority_intervals[configs[account_id].priority_interval_id]
                    if account_id in configs
                    and configs[account_id].priority_interval_id in priority_intervals
                    else None
                ),
            ).model_copy(
                update={
                    "priority_assignment_when_disabled_effective": (
                        configs[account_id].priority_assignment_when_disabled
                        if account_id in configs
                        and configs[account_id].priority_assignment_when_disabled is not None
                        else assign_disabled_globally
                    )
                }
            )
            for account_id in sorted(remote_by_id)
        ]
        return sorted(
            accounts,
            key=lambda item: (
                item.upstream_actual_multiplier is None,
                item.upstream_actual_multiplier or 0,
                item.management_account_id,
            ),
        )

    async def bind_legacy_identities(
        self,
        db: AsyncSession,
        expected_fingerprints: dict[int, str],
    ) -> int:
        if not expected_fingerprints:
            return 0

        remote_by_id = {
            account_id: account
            for account in await self._remote_accounts()
            if (account_id := self._numeric_remote_id(account)) is not None
        }
        requested_ids = sorted(expected_fingerprints)
        result = await db.execute(
            select(ApiAccount).where(
                ApiAccount.management_account_id.in_(requested_ids)
            )
        )
        configs = {
            config.management_account_id: config
            for config in result.scalars().all()
        }

        pending_bindings: dict[int, str] = {}
        pending_origin_rebinds: dict[int, str] = {}
        for account_id in requested_ids:
            remote = remote_by_id.get(account_id)
            if (
                remote is None
                or self._remote_identity_fingerprint(remote)
                != expected_fingerprints[account_id]
            ):
                raise ApiAccountServiceError(
                    "The legacy identity confirmation is stale; refresh the API key account list.",
                    status_code=409,
                )
            binding_fingerprint = self._require_remote_binding_fingerprint(remote)
            config = configs.get(account_id)
            if config is None:
                continue
            binding_status = self._config_binding_status(remote, config)
            if binding_status == "unbound":
                pending_bindings[account_id] = binding_fingerprint
            elif binding_status == "bound" and config.api_key_origin_rebind_required:
                pending_origin_rebinds[account_id] = binding_fingerprint
            elif binding_status != "bound":
                raise ApiAccountServiceError(
                    "A sub2api account identity changed and cannot be bulk rebound.",
                    status_code=409,
                )

        if not pending_bindings and not pending_origin_rebinds:
            return 0

        # Revalidate the complete confirmation immediately before the conditional
        # updates so a replaced remote id cannot inherit a legacy configuration.
        current_by_id = {
            account_id: account
            for account in await self._remote_accounts()
            if (account_id := self._numeric_remote_id(account)) is not None
        }
        for account_id in requested_ids:
            remote = current_by_id.get(account_id)
            if (
                remote is None
                or self._remote_identity_fingerprint(remote)
                != expected_fingerprints[account_id]
            ):
                raise ApiAccountServiceError(
                    "The legacy identity confirmation is stale; refresh the API key account list.",
                    status_code=409,
                )
            expected_binding = (
                pending_bindings.get(account_id)
                or pending_origin_rebinds.get(account_id)
            )
            if (
                expected_binding is not None
                and self._require_remote_binding_fingerprint(remote) != expected_binding
            ):
                raise ApiAccountServiceError(
                    "The legacy identity confirmation is stale; refresh the API key account list.",
                    status_code=409,
                )

        bound = 0
        for account_id, fingerprint in pending_bindings.items():
            result = await db.execute(
                update(ApiAccount)
                .where(
                    ApiAccount.management_account_id == account_id,
                    ApiAccount.remote_identity_fingerprint.is_(None),
                )
                .values(
                    remote_identity_fingerprint=fingerprint,
                    api_key_origin_rebind_required=False,
                )
                .execution_options(synchronize_session=False)
            )
            bound += int(result.rowcount or 0)
        for account_id, fingerprint in pending_origin_rebinds.items():
            result = await db.execute(
                update(ApiAccount)
                .where(
                    ApiAccount.management_account_id == account_id,
                    ApiAccount.remote_identity_fingerprint == fingerprint,
                    ApiAccount.api_key_origin_rebind_required.is_(True),
                )
                .values(api_key_origin_rebind_required=False)
                .execution_options(synchronize_session=False)
            )
            bound += int(result.rowcount or 0)
        await db.commit()
        db.expire_all()
        return bound

    def _ensure_secret_storage_ready(self, payload: ApiAccountUpdate) -> None:
        if payload.api_key and len(payload.api_key) > 4096:
            raise ApiAccountServiceError("The API key is too long.", status_code=422)
        if payload.access_token and len(payload.access_token) > 8192:
            raise ApiAccountServiceError("The access token is too long.", status_code=422)
        if payload.clear_access_token and payload.access_token:
            raise ApiAccountServiceError(
                "An access token cannot be set and cleared in the same request.",
                status_code=422,
            )
        adds_secret = bool(payload.api_key or payload.access_token)
        settings = get_settings()
        if (
            adds_secret
            and settings.app_env == "production"
            and settings.app_encryption_key.strip() == DEFAULT_ENCRYPTION_KEY
        ):
            raise ApiAccountServiceError(
                "Configure a non-default application encryption key before saving credentials.",
                status_code=503,
            )

    async def _upsert_account(
        self,
        db: AsyncSession,
        account_id: int,
        payload: ApiAccountUpdate,
    ) -> ApiAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            self._ensure_secret_storage_ready(payload)
            remote = await self._remote_account(
                account_id,
                payload.expected_identity_fingerprint,
            )
            result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            is_new = config is None
            binding_rebound = False
            if config is None:
                self._require_remote_binding_fingerprint(remote)
                config = self._new_config(
                    remote,
                    account_id,
                    secrets=(payload.api_key, payload.access_token),
                )
                db.add(config)
            else:
                binding_status = self._config_binding_status(remote, config)
                if binding_status == "mismatch" and not payload.confirm_identity_rebind:
                    self._require_config_binding(remote, config)
                if binding_status in {"unbound", "mismatch"}:
                    self._require_remote_binding_fingerprint(remote)
                    binding_rebound = True

            fields = payload.model_fields_set
            if (
                config.upstream_identity_rebind_required
                and not payload.confirm_upstream_identity_rebind
            ):
                raise ApiAccountServiceError(
                    "The upstream API key record identity changed or conflicts "
                    "with another account; explicitly rebind it before saving.",
                    status_code=409,
                )
            preview_fields = {
                "upstream_id",
                "api_endpoint_url",
                "platform_type",
                "upstream_user_id",
                "selected_group_id",
                "selected_group_name",
                "upstream_group_multiplier_override",
                "upstream_recharge_multiplier_override",
            }
            current_api_key = (
                decrypt_text(config.encrypted_api_key)
                if not config.api_key_origin_rebind_required
                else None
            )
            current_access_token = decrypt_text(config.encrypted_access_token)
            previous_origin = upstream_url_origin(config.api_endpoint_url)
            current_channel = await self._channel_for_config(db, config)
            selected_channel = current_channel
            if "upstream_id" in fields:
                selected_channel = (
                    await db.get(Upstream, payload.upstream_id)
                    if payload.upstream_id is not None
                    else None
                )
            if "upstream_id" in fields and payload.upstream_id is not None:
                if selected_channel is None:
                    raise ApiAccountServiceError(
                        "The upstream channel was not found.", status_code=404
                    )
            remote_base_url_target: str | None = None
            if "api_endpoint_url" in fields and payload.api_endpoint_url is not None:
                remote_base_url_target = payload.api_endpoint_url
            elif (
                "upstream_id" in fields
                and selected_channel is not None
                and payload.upstream_id != config.upstream_id
            ):
                remote_base_url_target = selected_channel.api_endpoint_url
            next_origin = upstream_url_origin(remote_base_url_target or config.api_endpoint_url)
            if (
                previous_origin is not None
                and next_origin is not None
                and next_origin != previous_origin
                and not payload.confirm_credential_rebind
            ):
                raise ApiAccountServiceError(
                    "Changing the upstream origin requires explicit credential rebind confirmation.",
                    status_code=409,
                )
            if (
                remote_base_url_target is not None
                and _remote_base_url(remote) != remote_base_url_target
            ):
                endpoint_guard = self._remote_rename_guard_fingerprint(remote)

                def validate_endpoint_candidate(candidate: dict[str, Any]) -> None:
                    if self._remote_rename_guard_fingerprint(candidate) != endpoint_guard:
                        raise ApiAccountServiceError(
                            "The sub2api account identity or upstream address changed before "
                            "the new channel could be applied; refresh the account list and try again.",
                            status_code=409,
                        )

                try:
                    remote = await self.sub2api.update_account_base_url(
                        account_id,
                        remote_base_url_target,
                        validate_current=validate_endpoint_candidate,
                    )
                except ApiAccountServiceError:
                    raise
                except Exception as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ApiAccountServiceError(
                        "Unable to update the sub2api API key account upstream address."
                    ) from None
            preview_invalidated = any(
                field in fields
                and getattr(payload, field)
                != getattr(
                    config,
                    {
                        "api_endpoint_url": "api_endpoint_url",
                        "platform_type": "platform_type",
                        "upstream_group_multiplier_override": "upstream_group_multiplier_override",
                        "upstream_recharge_multiplier_override": "upstream_recharge_multiplier_override",
                    }.get(field, field),
                )
                for field in preview_fields
            ) or bool(
                (payload.api_key and payload.api_key != current_api_key)
                or (payload.access_token and payload.access_token != current_access_token)
                or (payload.clear_access_token and current_access_token is not None)
                or binding_rebound
                or payload.confirm_upstream_identity_rebind
            )
            stored_record_upstream_type = str(
                config.resolved_platform_type
                or (
                    config.platform_type
                    if config.platform_type in {"newapi", "sub2api"}
                    else ""
                )
            ).strip().lower()
            selected_record_upstream_type = str(
                (
                    selected_channel.resolved_platform_type
                    or (
                        selected_channel.platform_type
                        if selected_channel.platform_type in {"newapi", "sub2api"}
                        else ""
                    )
                )
                if selected_channel is not None
                else ""
            ).strip().lower()
            channel_platform_changed = bool(
                config.remote_upstream_api_key_id is not None
                and stored_record_upstream_type
                and selected_record_upstream_type
                and stored_record_upstream_type != selected_record_upstream_type
            )
            identity_changed = (
                binding_rebound
                or
                ("upstream_id" in fields and payload.upstream_id != config.upstream_id)
                or
                (
                    "api_endpoint_url" in fields
                    and payload.api_endpoint_url != config.api_endpoint_url
                )
                or ("platform_type" in fields and payload.platform_type != config.platform_type)
                or ("upstream_user_id" in fields and payload.upstream_user_id != config.upstream_user_id)
                or (bool(payload.api_key) and payload.api_key != current_api_key)
                or (bool(payload.access_token) and payload.access_token != current_access_token)
                or (payload.clear_access_token and current_access_token is not None)
                or payload.confirm_upstream_identity_rebind
            )
            if preview_invalidated and not is_new:
                self.archive_data_before_invalidation(
                    db,
                    config,
                    reason=(
                        "account_identity_changed"
                        if identity_changed
                        else "account_discovery_context_changed"
                    ),
                    channel=current_channel,
                )
            known_secrets = (
                payload.api_key,
                current_api_key,
                payload.access_token,
                current_access_token,
                *self._known_channel_secrets(current_channel),
                *self._known_channel_secrets(selected_channel),
            )
            config.remote_platform = _safe_text(
                self.sub2api.account_platform(remote),
                secrets=known_secrets,
                limit=64,
            )
            config.remote_account_type = _safe_text(
                self.sub2api.account_type(remote),
                secrets=known_secrets,
                limit=32,
            )
            self.apply_remote_snapshot(
                config,
                remote,
                secrets=known_secrets,
                include_stored_secrets=False,
            )
            if "upstream_id" in fields:
                channel_changed = payload.upstream_id != config.upstream_id
                config.upstream_id = payload.upstream_id
                if channel_changed:
                    config.upstream_auto_assign_disabled = True
                    self.resolve_all_pause_holds(
                        config,
                        now=_utcnow(),
                        clear_ownership=binding_rebound,
                    )
                if selected_channel is not None:
                    config.api_endpoint_url = selected_channel.api_endpoint_url
                    config.platform_type = selected_channel.platform_type
                    if (
                        config.remote_upstream_api_key_id is None
                        or payload.confirm_upstream_identity_rebind
                    ):
                        config.resolved_platform_type = selected_channel.resolved_platform_type
                    elif config.resolved_platform_type is None and stored_record_upstream_type:
                        # Preserve the platform component of an existing record
                        # binding. A normal save cannot prove a record belongs
                        # to the channel's currently detected platform.
                        config.resolved_platform_type = stored_record_upstream_type
                    config.upstream_user_id = selected_channel.upstream_user_id
                    config.encrypted_access_token = selected_channel.encrypted_access_token
                    config.upstream_recharge_multiplier_override = selected_channel.upstream_recharge_multiplier_override
            if identity_changed:
                config.selected_group_id = None
                config.selected_group_name = None
            if "api_endpoint_url" in fields:
                config.api_endpoint_url = payload.api_endpoint_url
            if "platform_type" in fields or is_new:
                config.platform_type = payload.platform_type
            for field in (
                "upstream_user_id",
                "selected_group_id",
                "selected_group_name",
                "rate_pause_policy",
                "rate_absolute_threshold",
            ):
                if field in fields:
                    value = getattr(payload, field)
                    if field in {"upstream_user_id", "selected_group_id", "selected_group_name"}:
                        value = _safe_text(
                            value,
                            secrets=known_secrets,
                            limit=200 if field == "selected_group_name" else 128,
                        )
                    setattr(config, field, value)
            if "upstream_group_multiplier_override" in fields:
                config.upstream_group_multiplier_override = payload.upstream_group_multiplier_override
            if "upstream_recharge_multiplier_override" in fields:
                config.upstream_recharge_multiplier_override = payload.upstream_recharge_multiplier_override
            if "rate_pause_policy" in fields and config.rate_pause_policy == "custom":
                config.rate_absolute_threshold = config.rate_absolute_threshold or 1.0
            elif "rate_pause_policy" in fields and config.rate_pause_policy != "custom":
                config.rate_absolute_threshold = None
            if "priority_assignment_when_disabled" in fields:
                config.priority_assignment_when_disabled = payload.priority_assignment_when_disabled
            if "upstream_id" in fields and "availability_monitor_id" not in fields:
                config.availability_monitor_id = None
            if "availability_check_mode" in fields:
                config.availability_check_mode = payload.availability_check_mode
            if "availability_monitor_id" in fields:
                config.availability_monitor_id = payload.availability_monitor_id
            if "availability_test_model" in fields:
                normalized_test_model = _safe_text(
                    payload.availability_test_model,
                    secrets=known_secrets,
                    limit=160,
                )
                if normalized_test_model and config.available_models is None:
                    raise ApiAccountServiceError(
                        "Synchronize this API Key account's available model whitelist before selecting a test model.",
                        status_code=409,
                    )
                if normalized_test_model and isinstance(config.available_models, list):
                    allowed_models = {
                        str(item.get("id") or "").strip()
                        for item in config.available_models
                        if isinstance(item, dict)
                    }
                    if normalized_test_model not in allowed_models:
                        raise ApiAccountServiceError(
                            "The selected test model is not in this API Key account's available model whitelist.",
                            status_code=422,
                        )
                config.availability_test_model = normalized_test_model
            availability_fields = {
                "availability_check_mode",
                "availability_monitor_id",
                "availability_test_model",
            }
            availability_changed = bool(fields & availability_fields) or (
                "upstream_id" in fields
                and config.availability_check_mode == "upstream_monitor"
                and config.availability_monitor_id is None
            )
            if availability_changed:
                if config.availability_check_mode == "disabled":
                    config.availability_monitor_id = None
                    config.availability_test_model = None
                elif config.availability_check_mode == "independent_model":
                    config.availability_monitor_id = None
                config.availability_unavailable_count = 0
                config.availability_recovery_count = 0
                config.availability_checked_at = None
                config.availability_source = None
                config.availability_message = None
                if config.availability_check_mode == "disabled":
                    config.availability_status = "disabled"
                else:
                    config.availability_status = "pending"

            if payload.api_key:
                config.encrypted_api_key = encrypt_text(payload.api_key)
            if payload.api_key or payload.confirm_credential_rebind:
                config.api_key_origin_rebind_required = False
            if payload.clear_access_token:
                config.encrypted_access_token = None
            elif payload.access_token:
                config.encrypted_access_token = encrypt_text(payload.access_token)

            if "remote_name" in fields and payload.remote_name:
                requested_name = payload.remote_name
                if requested_name != self._remote_name(remote, account_id):
                    rename_guard = self._remote_rename_guard_fingerprint(remote)

                    def validate_rename_candidate(candidate: dict[str, Any]) -> None:
                        if self._remote_rename_guard_fingerprint(candidate) != rename_guard:
                            raise ApiAccountServiceError(
                                "The sub2api account identity changed before its name could be updated; "
                                "refresh the account list and try again.",
                                status_code=409,
                            )

                    try:
                        renamed_remote = await self.sub2api.update_account_name(
                            account_id,
                            requested_name,
                            validate_current=validate_rename_candidate,
                        )
                    except ApiAccountServiceError:
                        raise
                    except Exception as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        raise ApiAccountServiceError(
                            "Unable to rename the sub2api API key account."
                        ) from None
                    if self._remote_rename_guard_fingerprint(renamed_remote) != rename_guard:
                        raise ApiAccountServiceError(
                            "The sub2api account identity changed while its name was being updated; "
                            "refresh the account list and try again.",
                            status_code=409,
                        )
                    remote = renamed_remote
                config.remote_identity_fingerprint = self._require_remote_binding_fingerprint(
                    remote
                )

            if preview_invalidated and not is_new:
                self._invalidate_preview(
                    config,
                    clear_upstream_identity=identity_changed,
                )
                if identity_changed:
                    self.clear_upstream_usage_state(config)
                    self.resolve_all_pause_holds(
                        config,
                        now=_utcnow(),
                        clear_ownership=binding_rebound,
                    )

            if channel_platform_changed and not identity_changed:
                config.upstream_identity_rebind_required = True
                config.last_error = (
                    f"The upstream platform changed from {stored_record_upstream_type} to "
                    f"{selected_record_upstream_type}; explicit rebind is required before "
                    "accepting an API key record from the new platform."
                )

            if binding_rebound:
                config.remote_identity_fingerprint = self._require_remote_binding_fingerprint(
                    remote
                )

            config.remote_name = (
                _safe_text(
                    self._remote_name(remote, account_id, secrets=known_secrets),
                    secrets=known_secrets,
                    limit=200,
                )
                or f"Account #{account_id}"
            )
            self.apply_remote_snapshot(
                config,
                remote,
                secrets=known_secrets,
                include_stored_secrets=False,
            )

            remote_rate = self._remote_management_billing_multiplier(remote)
            if remote_rate is not None:
                config.management_billing_multiplier = remote_rate
            await db.commit()
            await db.refresh(config)
            return self._build_out(remote, config, extra_secrets=known_secrets)

    async def upsert_account(
        self,
        db: AsyncSession,
        account_id: int,
        payload: ApiAccountUpdate,
        *,
        defer_priority_rebalance: bool = False,
    ) -> ApiAccountOut:
        account = await self._upsert_account(db, account_id, payload)
        if defer_priority_rebalance:
            return account
        await self._rebalance_priorities_best_effort(db)
        refreshed = await self.list_accounts(db)
        return next(
            (item for item in refreshed if item.management_account_id == account_id),
            account,
        )

    async def delete_account(
        self,
        db: AsyncSession,
        account_id: int,
        expected_identity_fingerprint: str,
    ) -> bool:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                return False
            self._require_config_binding(remote, config)
            config.deleted_at = _utcnow()
            config.remote_present = False
            config.remote_missing_at = config.remote_missing_at or config.deleted_at
            await db.commit()
        await self._rebalance_priorities_best_effort(db)
        return True

    async def set_account_enabled(
        self,
        db: AsyncSession,
        account_id: int,
        enabled: bool,
        expected_identity_fingerprint: str,
    ) -> ApiAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            old_schedulable = self.sub2api.account_schedulable(remote)
            old_enabled = (
                old_schedulable
                if old_schedulable in {True, False}
                else self.sub2api.account_looks_healthy(remote)
            )
            result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            self._require_config_binding(remote, config)
            try:
                await self.sub2api.set_account_schedulable(account_id, enabled)
                readback = await self._remote_account(account_id, expected_identity_fingerprint)
            except ApiAccountServiceError:
                raise
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ApiAccountServiceError(
                    "Unable to update and verify the sub2api account state."
                ) from None
            if readback is None or self.sub2api.account_schedulable(readback) is not enabled:
                raise ApiAccountServiceError(
                    "sub2api account state readback did not match the requested state."
                )
            if config is not None:
                self.resolve_all_pause_holds(config, now=_utcnow())
                self.apply_remote_snapshot(config, readback)
            if old_enabled != enabled:
                await enqueue_api_key_account_state_changed(
                    db,
                    enabled=enabled,
                    account_id=account_id,
                    account_name=(
                        config.remote_name
                        if config is not None
                        else self.sub2api.account_name(readback)
                    ),
                    upstream_id=config.upstream_id if config is not None else None,
                    reason="Account state changed manually.",
                )
            if config is not None:
                await db.commit()
                await db.refresh(config)
            channel = await self._channel_for_config(db, config)
            return self._build_out(
                readback,
                config,
                extra_secrets=self._known_channel_secrets(channel),
            )

    async def delete_remote_account(
        self,
        db: AsyncSession,
        account_id: int,
        expected_identity_fingerprint: str,
    ) -> bool:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            self._require_config_binding(remote, config)
            try:
                deleted = await self.sub2api.delete_account(remote)
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ApiAccountServiceError(
                    "Unable to delete the sub2api API key account."
                ) from None
            if not deleted:
                raise ApiAccountServiceError(
                    "The sub2api API key account was not found.",
                    status_code=404,
                )

            if config is not None:
                deleted_at = _utcnow()
                config.deleted_at = deleted_at
                config.remote_present = False
                config.remote_missing_at = config.remote_missing_at or deleted_at
            await db.commit()
        await self._rebalance_priorities_best_effort(db)
        return True

    @staticmethod
    def _upstream_discovery_base_url(
        config: ApiAccount,
        channel: Upstream | None,
    ) -> str:
        if channel is not None and channel.management_url:
            return channel.management_url
        return config.api_endpoint_url or (
            channel.api_endpoint_url if channel is not None else ""
        )

    async def _call_upstream_discovery(
        self,
        config: ApiAccount,
        channel: Upstream | None,
        *,
        today_timezone: str,
    ) -> Any:
        api_key = (
            decrypt_text(config.encrypted_api_key)
            if not config.api_key_origin_rebind_required
            else None
        )
        result = discover_upstream(
            base_url=self._upstream_discovery_base_url(config, channel),
            upstream_type=config.platform_type,
            api_key=api_key,
            access_token=decrypt_text(config.encrypted_access_token),
            new_api_user=config.upstream_user_id,
            selected_group_id=config.selected_group_id,
            selected_group_name=config.selected_group_name,
            account_api_keys=(
                {config.management_account_id: api_key}
                if api_key is not None
                else None
            ),
            account_api_key_record_ids=(
                {
                    config.management_account_id: config.remote_upstream_api_key_id
                }
                if config.remote_upstream_api_key_id is not None
                else None
            ),
            today_timezone=today_timezone,
        )
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    async def _runtime_display_timezone() -> str:
        try:
            public_settings = await get_runtime_config_service().get_public_settings()
        except Exception:
            return DEFAULT_TODAY_TIME_ZONE
        configured = (
            public_settings.get("display_timezone")
            if isinstance(public_settings, dict)
            else None
        )
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return DEFAULT_TODAY_TIME_ZONE

    def _apply_balance_result(
        self,
        config: ApiAccount,
        result: Any,
        *,
        api_key: str | None,
        access_token: str | None,
        now: datetime,
    ) -> bool:
        if isinstance(result, BaseException):
            config.balance_status = "error"
            config.balance_message = "Unable to read the upstream balance through sub2api."
            return False
        if not isinstance(result, dict):
            config.balance_status = "error"
            config.balance_message = "sub2api returned an invalid upstream balance result."
            return False

        secrets = (api_key, access_token)
        status = (_safe_text(result.get("status"), secrets=secrets, limit=32) or "unknown").lower()
        config.balance_status = status
        config.balance_message = _safe_text(
            result.get("message"),
            secrets=secrets,
            limit=300,
        )
        successful = status in {"ok", "success", "available"}
        if successful:
            parsed_numbers: dict[str, float] = {}
            for field, key in (
                ("wallet_balance_usd", "remaining"),
                ("wallet_total_usd", "total"),
                ("wallet_used_usd", "used"),
            ):
                if key not in result:
                    continue
                number = _balance_number(result[key])
                if number is None:
                    config.balance_status = "error"
                    config.balance_message = "sub2api returned invalid upstream balance numeric data."
                    return False
                parsed_numbers[field] = number
            unit = _safe_text(result.get("unit"), secrets=secrets, limit=32)
            for field, number in parsed_numbers.items():
                setattr(config, field, number)
            if unit is not None:
                config.balance_unit = unit
            config.balance_checked_at = _parse_datetime(result.get("checked_at")) or now
        return successful

    async def _discover_locked(
        self,
        db: AsyncSession,
        remote: dict[str, Any],
        config: ApiAccount,
    ) -> ApiAccountOut:
        self._require_config_binding(remote, config)
        today_timezone = await self._runtime_display_timezone()
        now = _utcnow()
        channel = await self._channel_for_config(db, config)
        channel_secrets = self._known_channel_secrets(channel)
        stored_api_key, access_token = self._known_local_secrets(config)
        api_key = None if config.api_key_origin_rebind_required else stored_api_key
        known_secrets = (stored_api_key, access_token, *channel_secrets)
        has_credentials = bool(api_key or access_token)
        discovery_base_url = self._upstream_discovery_base_url(config, channel)
        has_base_url = bool(discovery_base_url)
        previous_group_multiplier = config.upstream_group_multiplier
        previous_recharge_multiplier = config.upstream_recharge_multiplier
        previous_expected_management_billing_multiplier = config.expected_management_billing_multiplier
        previous_management_billing_multiplier = config.management_billing_multiplier
        upstream_state: Any = None

        balance_task = self.sub2api.get_account_balance(config.management_account_id)
        management_recharge_task = self.sub2api.get_payment_balance_recharge_multiplier_info()
        today_stats_task = self.get_sub2api_today_stats([config.management_account_id])
        tasks: list[Any] = [balance_task, management_recharge_task, today_stats_task]
        upstream_attempted = has_credentials and has_base_url
        if upstream_attempted:
            tasks.append(
                self._call_upstream_discovery(
                    config,
                    channel,
                    today_timezone=today_timezone,
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        balance_result, management_recharge_result = results[0], results[1]
        today_stats_result = results[2]
        upstream_result = results[3] if upstream_attempted else None

        errors: list[str] = []
        if not self._apply_balance_result(
            config,
            balance_result,
            api_key=api_key,
            access_token=access_token,
            now=now,
        ):
            if isinstance(balance_result, BaseException):
                errors.append("Balance check failed.")

        fresh_local: Decimal | None = None
        if isinstance(management_recharge_result, BaseException):
            config.management_recharge_status = "error"
            errors.append("Target recharge multiplier check failed.")
        elif (
            isinstance(management_recharge_result, tuple)
            and len(management_recharge_result) == 2
        ):
            local_credit_per_cny = _decimal_multiplier(management_recharge_result[0])
            if local_credit_per_cny is None:
                config.management_recharge_status = "error"
                errors.append("Target recharge multiplier is invalid.")
            else:
                try:
                    fresh_local = _decimal_multiplier(Decimal("1") / local_credit_per_cny)
                except DecimalException:
                    fresh_local = None
                if fresh_local is None:
                    config.management_recharge_status = "error"
                    errors.append("Target recharge multiplier is outside the supported range.")
                else:
                    field_present = bool(management_recharge_result[1])
                    config.management_recharge_multiplier = float(fresh_local)
                    config.management_recharge_source = "sub2api_settings" if field_present else "default"
                    config.management_recharge_status = "ok" if field_present else "default_missing"
        else:
            config.management_recharge_status = "error"
            errors.append("Target recharge multiplier result is invalid.")
        today_stats = (
            today_stats_result
            if isinstance(today_stats_result, dict)
            else {}
        )
        self.apply_sub2api_today_stats(
            config,
            today_stats.get(config.management_account_id),
            now=now,
            time_zone=today_timezone,
        )

        fresh_group: Decimal | None = None
        fresh_recharge: Decimal | None = None
        fresh_group_source: str | None = None
        fresh_recharge_source: str | None = None
        discovery_failed = False
        discovery_succeeded = False
        upstream_identity_mismatch = False
        recharge_failed = False
        recharge_invalid = False
        if upstream_attempted:
            if isinstance(upstream_result, BaseException):
                discovery_failed = True
                errors.append("Upstream discovery failed.")
            else:
                discovery_status = str(_value(upstream_result, "status") or "error").strip().lower()
                if discovery_status == "insecure_url":
                    raise ApiAccountServiceError(
                        "Credentials may only be sent to an HTTPS upstream URL.",
                        status_code=422,
                    )
                if discovery_status != "ok":
                    discovery_failed = True
                    errors.append("Upstream discovery failed.")
                else:
                    discovery_succeeded = True
                    account_states = _value(
                        upstream_result,
                        "account_upstream_states",
                    )
                    upstream_state = (
                        account_states.get(config.management_account_id)
                        if isinstance(account_states, dict)
                        else None
                    ) or _value(upstream_result, "matched_account_state")
                    resolved_type = str(
                        _value(upstream_result, "platform_type") or ""
                    ).strip().lower()
                    identity_status = await self.apply_upstream_record_identity(
                        db,
                        config,
                        upstream_state,
                        observed_upstream_type=resolved_type,
                    )
                    if identity_status is not UpstreamRecordIdentityStatus.VERIFIED:
                        upstream_identity_mismatch = True
                        discovery_succeeded = False
                        upstream_state = None
                    if upstream_identity_mismatch:
                        config.last_discovered_at = now
                    else:
                        self.apply_upstream_usage_state(
                            config,
                            upstream_state,
                            now=now,
                            time_zone=today_timezone,
                            secrets=known_secrets,
                        )
                    options_value = _value(
                        upstream_result,
                        "groups",
                        "group_options",
                        "available_groups",
                    )
                    if options_value is not None and not upstream_identity_mismatch:
                        config.group_options = _sanitize_group_options(
                            options_value,
                            secrets=known_secrets,
                        )
                    raw_group = _value(
                        upstream_result,
                        "discovered_upstream_group_multiplier",
                        "group_multiplier",
                        "selected_group_multiplier",
                    )
                    fresh_group = (
                        None
                        if upstream_identity_mismatch
                        else _decimal_multiplier(raw_group)
                    )
                    raw_recharge = _value(
                        upstream_result,
                        "discovered_upstream_recharge_multiplier",
                        "recharge_multiplier",
                        "balance_recharge_multiplier",
                    )
                    fresh_recharge = (
                        None
                        if upstream_identity_mismatch
                        else _decimal_multiplier(raw_recharge)
                    )
                    recharge_probe_status = str(
                        _value(upstream_result, "recharge_discovery_status") or "unknown"
                    ).strip().lower()
                    if recharge_probe_status not in {"ok", "missing"}:
                        recharge_failed = True
                        fresh_recharge = None
                        errors.append("Upstream recharge multiplier discovery failed.")
                    elif (
                        (raw_recharge is not None and fresh_recharge is None)
                        or (recharge_probe_status == "ok" and fresh_recharge is None)
                    ):
                        recharge_invalid = True
                        errors.append("Upstream recharge multiplier is invalid.")
                    fresh_group_source = _safe_text(
                        _value(
                            upstream_result,
                            "discovered_upstream_group_multiplier_source",
                        ),
                        secrets=known_secrets,
                        limit=128,
                    )
                    fresh_recharge_source = _safe_text(
                        _value(
                            upstream_result,
                            "discovered_upstream_recharge_multiplier_source",
                        ),
                        secrets=known_secrets,
                        limit=128,
                    )
                    matched_group = (
                        None
                        if upstream_identity_mismatch
                        else _value(upstream_result, "matched_group")
                    )
                    selected_id = _value(matched_group, "id", "group_id")
                    selected_name = _value(matched_group, "name", "group_name")
                    if selected_id is not None:
                        config.selected_group_id = (
                            _safe_text(
                                selected_id,
                                secrets=known_secrets,
                                limit=128,
                            )
                            or config.selected_group_id
                        )
                    if selected_name is not None:
                        config.selected_group_name = (
                            _safe_text(
                                selected_name,
                                secrets=known_secrets,
                                limit=200,
                            )
                            or config.selected_group_name
                        )
                    if fresh_group is not None:
                        config.discovered_upstream_group_multiplier = float(fresh_group)
                    if fresh_recharge is not None:
                        config.discovered_upstream_recharge_multiplier = float(fresh_recharge)

        if (
            upstream_attempted
            and not discovery_succeeded
            and not upstream_identity_mismatch
        ):
            self.apply_upstream_usage_state(
                config,
                None,
                now=now,
                time_zone=today_timezone,
                secrets=known_secrets,
            )

        if upstream_identity_mismatch:
            config.remote_platform = _safe_text(
                self.sub2api.account_platform(remote),
                secrets=known_secrets,
                limit=64,
            )
            config.remote_account_type = _safe_text(
                self.sub2api.account_type(remote),
                secrets=known_secrets,
                limit=32,
            )
            config.remote_name = (
                _safe_text(
                    self._remote_name(
                        remote,
                        config.management_account_id,
                        secrets=known_secrets,
                    ),
                    secrets=known_secrets,
                    limit=200,
                )
                or f"Account #{config.management_account_id}"
            )
            await db.commit()
            await db.refresh(config)
            return self._build_out(remote, config, extra_secrets=channel_secrets)

        health_transition = self.apply_authoritative_upstream_state(
            config,
            upstream_state,
            now=now,
        )
        if (
            health_transition.observed_group_id is not None
            or health_transition.observed_group_name is not None
        ):
            config.selected_group_id = health_transition.observed_group_id
            config.selected_group_name = health_transition.observed_group_name

        manual_group = _decimal_multiplier(config.upstream_group_multiplier_override)
        if fresh_group is not None:
            effective_group = fresh_group
            config.group_multiplier_source = fresh_group_source or "auto"
            config.group_multiplier_status = "ok"
        elif manual_group is not None:
            effective_group = manual_group
            config.group_multiplier_source = "manual"
            config.group_multiplier_status = "fallback_manual" if upstream_attempted else "manual"
        else:
            effective_group = None
            config.group_multiplier_source = None
            if not has_credentials:
                config.group_multiplier_status = "credentials_missing"
            elif not has_base_url:
                config.group_multiplier_status = "base_url_missing"
            elif discovery_failed:
                config.group_multiplier_status = "discovery_failed"
            else:
                config.group_multiplier_status = "unavailable"
        config.upstream_group_multiplier = float(effective_group) if effective_group is not None else None

        manual_recharge = _decimal_multiplier(config.upstream_recharge_multiplier_override)
        if fresh_recharge is not None:
            effective_recharge = fresh_recharge
            config.recharge_multiplier_source = fresh_recharge_source or "auto"
            config.recharge_multiplier_status = "ok"
        elif manual_recharge is not None:
            effective_recharge = manual_recharge
            config.recharge_multiplier_source = "manual"
            config.recharge_multiplier_status = "fallback_manual" if upstream_attempted else "manual"
        elif discovery_succeeded and not recharge_invalid and not recharge_failed:
            effective_recharge = Decimal("1")
            config.recharge_multiplier_source = "default"
            config.recharge_multiplier_status = "default_missing"
        else:
            effective_recharge = None
            config.recharge_multiplier_source = None
            if recharge_invalid:
                config.recharge_multiplier_status = "invalid"
            elif discovery_failed or recharge_failed:
                config.recharge_multiplier_status = "discovery_failed"
            elif not has_credentials:
                config.recharge_multiplier_status = "credentials_missing"
            elif not has_base_url:
                config.recharge_multiplier_status = "base_url_missing"
            else:
                config.recharge_multiplier_status = "unavailable"
        config.upstream_recharge_multiplier = (
            float(effective_recharge) if effective_recharge is not None else None
        )

        management_billing_multiplier = self._remote_management_billing_multiplier(remote)
        if management_billing_multiplier is not None:
            config.management_billing_multiplier = management_billing_multiplier

        if config.today_upstream_usage_status != "ok":
            self.apply_local_today_usage_fallback(
                config,
                _value(today_stats.get(config.management_account_id), "cost"),
                management_billing_multiplier,
                now=now,
            )

        if (
            effective_group is not None
            and effective_recharge is not None
            and fresh_local is not None
        ):
            target = _calculate_expected_management_billing_multiplier(effective_group, effective_recharge, fresh_local)
            if target is None:
                config.expected_management_billing_multiplier = None
                errors.append("Calculated target rate is outside the supported range.")
            else:
                config.expected_management_billing_multiplier = float(target)
        else:
            config.expected_management_billing_multiplier = None

        if health_transition.new_group_status in INVALID_UPSTREAM_GROUP_STATUSES:
            config.expected_management_billing_multiplier = None
            if health_transition.new_group_status == "deleted":
                config.group_multiplier_status = "group_deleted"
                errors.append("The synchronized upstream group was deleted.")
            else:
                config.group_multiplier_status = "group_unavailable"
                errors.append("The synchronized upstream group is unavailable.")
        elif health_transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES:
            config.expected_management_billing_multiplier = None
            errors.append("The synchronized upstream API key is unavailable.")

        config.remote_platform = _safe_text(
            self.sub2api.account_platform(remote),
            secrets=known_secrets,
            limit=64,
        )
        config.remote_account_type = _safe_text(
            self.sub2api.account_type(remote),
            secrets=known_secrets,
            limit=32,
        )
        config.remote_name = (
            _safe_text(
                self._remote_name(
                    remote,
                    config.management_account_id,
                    secrets=known_secrets,
                ),
                secrets=known_secrets,
                limit=200,
            )
            or f"Account #{config.management_account_id}"
        )
        config.last_error = " ".join(dict.fromkeys(errors)) or None
        config.last_discovered_at = now
        current_remote = await self._remote_account(config.management_account_id)
        self._require_config_binding(current_remote, config)
        runtime_config = get_runtime_config_service()
        try:
            automation_paused = bool(await runtime_config.get_automation_paused())
        except Exception:
            automation_paused = True
        try:
            upstream_health_pause_enabled = bool(
                await runtime_config.get_api_key_auto_disable_on_upstream_unavailable()
            )
        except Exception:
            upstream_health_pause_enabled = None
        previous_pause_holds = self.active_pause_holds(config)
        previous_pause_reason = (
            config.auto_disabled_reason
            or (previous_pause_holds[0].reason if previous_pause_holds else None)
        )
        self.update_upstream_health_pause_holds(
            config,
            health_transition,
            enabled=upstream_health_pause_enabled,
            automation_paused=automation_paused,
            upstream_id=channel.id if channel is not None else config.upstream_id,
            now=now,
        )
        (
            current_remote,
            old_remote_schedulable,
            new_remote_schedulable,
            health_action_status,
            health_safe_error,
        ) = await self.reconcile_automatic_pause(
            db,
            current_remote,
            config,
            upstream_id=channel.id if channel is not None else config.upstream_id,
            upstream_name=channel.display_name if channel is not None else None,
            pause_action_reason=(
                self.active_pause_holds(config)[0].reason
                if self.active_pause_holds(config)
                else previous_pause_reason
            ),
            mutations_allowed=not automation_paused,
        )
        if health_safe_error is not None:
            config.last_error = health_safe_error
        self.record_upstream_health_change(
            db,
            config,
            channel,
            health_transition,
            old_remote_schedulable=old_remote_schedulable,
            new_remote_schedulable=new_remote_schedulable,
            action_status=health_action_status,
            safe_error=health_safe_error,
            previous_group_multiplier=previous_group_multiplier,
            previous_recharge_multiplier=previous_recharge_multiplier,
            previous_expected_management_billing_multiplier=previous_expected_management_billing_multiplier,
            previous_management_billing_multiplier=previous_management_billing_multiplier,
            pause_action_reason=(
                previous_pause_reason
                if health_action_status == "account_restored"
                else (
                    self.active_pause_holds(config)[0].reason
                    if self.active_pause_holds(config)
                    else previous_pause_reason
                )
            ),
        )
        await db.commit()
        await db.refresh(config)
        return self._build_out(current_remote, config, extra_secrets=channel_secrets)

    async def discover_account(
        self,
        db: AsyncSession,
        account_id: int,
        expected_identity_fingerprint: str,
    ) -> ApiAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                config = self._new_config(remote, account_id)
                db.add(config)
            else:
                self._require_config_binding(remote, config)
            account = await self._discover_locked(db, remote, config)
        await self._rebalance_priorities_after_discovery(db)
        refreshed = await self.list_accounts(db)
        return next(
            (item for item in refreshed if item.management_account_id == account_id),
            account,
        )

    async def discover_all(self, db: AsyncSession) -> ApiAccountDiscoverAllOut:
        remote_by_id = {
            account_id: account
            for account in await self._remote_accounts()
            if (account_id := self._numeric_remote_id(account)) is not None
        }
        if not remote_by_id:
            return ApiAccountDiscoverAllOut(total=0, succeeded=0, failed=0, accounts=[])
        accounts: list[ApiAccountOut] = []
        succeeded = 0
        failed = 0
        for account_id in sorted(remote_by_id):
            remote = remote_by_id[account_id]
            lock = await self._lock_for(account_id)
            async with lock:
                result = await db.execute(
                    select(ApiAccount)
                    .where(
                        ApiAccount.management_account_id == account_id
                    )
                    .execution_options(populate_existing=True)
                )
                config = result.scalar_one_or_none()
                if config is None:
                    config = self._new_config(remote, account_id)
                    db.add(config)
                elif self._config_binding_status(remote, config) != "bound":
                    channel = await self._channel_for_config(db, config)
                    accounts.append(
                        self._build_out(
                            remote,
                            config,
                            extra_secrets=self._known_channel_secrets(channel),
                        )
                    )
                    failed += 1
                    continue
                account = await self._discover_locked(db, remote, config)
            accounts.append(account)
            discovery_failed = "Upstream discovery failed." in (account.last_error or "")
            if (
                not discovery_failed
                and (
                    account.expected_management_billing_multiplier is not None
                    or account.balance_status in {"ok", "success", "available"}
                )
            ):
                succeeded += 1
            else:
                failed += 1
        await self._rebalance_priorities_after_discovery(db)
        accounts = await self.list_accounts(db)
        return ApiAccountDiscoverAllOut(
            total=len(remote_by_id),
            succeeded=succeeded,
            failed=failed,
            accounts=accounts,
        )

    async def _apply_account(
        self,
        db: AsyncSession,
        account_id: int,
        confirmed_expected_management_billing_multiplier: float,
        expected_identity_fingerprint: str,
    ) -> ApiAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                raise ApiAccountServiceError(
                    "Save the upstream account configuration before applying a rate.",
                    status_code=404,
                )
            self._require_config_binding(remote, config)

            discovered = await self._discover_locked(db, remote, config)
            target = _decimal_multiplier(discovered.expected_management_billing_multiplier, allow_zero=True)
            confirmed = _decimal_multiplier(confirmed_expected_management_billing_multiplier, allow_zero=True)
            if target is None or confirmed is None:
                raise ApiAccountServiceError(
                    "A valid target rate could not be calculated.",
                    status_code=409,
                )
            target = _quantize_rate(target)
            confirmed = _quantize_rate(confirmed)
            if target != confirmed:
                raise ApiAccountServiceError(
                    "The confirmed target rate is stale; discover the account again.",
                    status_code=409,
                )

            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            self._require_config_binding(remote, config)
            old_management_billing_multiplier = self._remote_management_billing_multiplier(remote)
            try:
                await self.sub2api.update_account_rate_multiplier(account_id, float(target))
                readback = await self.sub2api.get_account_management_billing_multiplier_multiplier(account_id)
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                config.last_error = "Unable to update and verify the sub2api account rate."
                await db.commit()
                raise ApiAccountServiceError(
                    "Unable to update and verify the sub2api account rate."
                ) from None

            readback_decimal = _decimal_multiplier(readback, allow_zero=True)
            config.management_billing_multiplier = readback
            if readback_decimal is None or _quantize_rate(readback_decimal) != target:
                config.last_error = "sub2api rate readback did not match the requested target."
                await db.commit()
                raise ApiAccountServiceError(
                    "sub2api rate readback did not match the requested target."
                )

            config.last_applied_at = _utcnow()
            config.last_error = None
            old_current_decimal = _decimal_multiplier(
                old_management_billing_multiplier,
                allow_zero=True,
            )
            if (
                old_current_decimal is not None
                and readback_decimal is not None
                and _quantize_rate(old_current_decimal) != _quantize_rate(readback_decimal)
            ):
                channel = await self._channel_for_config(db, config)
                await enqueue_api_key_rate_changed(
                    db,
                    account_id=config.management_account_id,
                    account_name=config.remote_name,
                    old_rate=old_management_billing_multiplier,
                    new_rate=readback,
                    observed_at=config.last_applied_at,
                    reason="manual_apply",
                    upstream_id=channel.id if channel is not None else None,
                    upstream_name=channel.display_name if channel is not None else None,
                )
            await db.commit()
            await db.refresh(config)
            remote["rate_multiplier"] = readback
            channel = await self._channel_for_config(db, config)
            return self._build_out(
                remote,
                config,
                extra_secrets=self._known_channel_secrets(channel),
            )

    async def apply_account(
        self,
        db: AsyncSession,
        account_id: int,
        confirmed_expected_management_billing_multiplier: float,
        expected_identity_fingerprint: str,
    ) -> ApiAccountOut:
        try:
            account = await self._apply_account(
                db,
                account_id,
                confirmed_expected_management_billing_multiplier,
                expected_identity_fingerprint,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._rebalance_priorities_best_effort(db)
            raise
        await self._rebalance_priorities_best_effort(db)
        refreshed = await self.list_accounts(db)
        return next(
            (item for item in refreshed if item.management_account_id == account_id),
            account,
        )


_service: ApiAccountService | None = None


def get_api_account_service() -> ApiAccountService:
    global _service
    if _service is None:
        _service = ApiAccountService()
    return _service
