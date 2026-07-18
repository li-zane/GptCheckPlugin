from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.upstream_urls import canonicalize_upstream_url, upstream_url_origin
from app.models import (
    UpstreamAccountConfig,
    UpstreamChannel,
    UpstreamPriorityInterval,
    UpstreamRateChangeLog,
)
from app.schemas import (
    UpstreamAccountOut,
    UpstreamAccountUpdate,
    UpstreamDiscoverAllOut,
    UpstreamGroupOptionOut,
)
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError
from app.services.runtime_config import get_runtime_config_service
from app.services.upstream_client import discover_upstream


RATE_QUANTUM = Decimal("0.0001")
MAX_MULTIPLIER = Decimal("1000")
DEFAULT_ENCRYPTION_KEY = "change-me-encryption-key"
JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991
INVALID_UPSTREAM_KEY_STATUSES = frozenset({"disabled", "expired", "quota_exhausted"})
INVALID_UPSTREAM_GROUP_STATUSES = frozenset({"unavailable", "unassigned"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UpstreamHealthTransition:
    old_key_status: str
    new_key_status: str
    old_group_status: str
    new_group_status: str
    previous_invalid_count: int
    confirmed_invalid: bool
    observed_group_id: str | None = None
    observed_group_name: str | None = None


class UpstreamAccountServiceError(RuntimeError):
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


def _quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _calculate_target_rate(
    group_multiplier: Decimal,
    upstream_recharge_multiplier: Decimal,
    local_recharge_multiplier: Decimal,
) -> Decimal | None:
    try:
        if (
            not group_multiplier.is_finite()
            or not upstream_recharge_multiplier.is_finite()
            or not local_recharge_multiplier.is_finite()
            or group_multiplier <= 0
            or upstream_recharge_multiplier <= 0
            or local_recharge_multiplier <= 0
        ):
            return None
        raw_target = group_multiplier * upstream_recharge_multiplier / local_recharge_multiplier
        if not raw_target.is_finite() or raw_target <= 0 or raw_target > MAX_MULTIPLIER:
            return None
        target = _quantize_rate(raw_target)
    except (DecimalException, ValueError):
        return None
    return target if target.is_finite() and Decimal("0") < target <= MAX_MULTIPLIER else None


def _calculate_composite_multiplier(
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


class UpstreamAccountService:
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
        config: UpstreamAccountConfig,
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
            observed_group_id=observed_group_id,
            observed_group_name=observed_group_name,
        )

    @staticmethod
    def clear_upstream_usage_state(config: UpstreamAccountConfig) -> None:
        config.upstream_usage_amount = None
        config.upstream_usage_unit = None
        config.upstream_usage_checked_at = None

    @staticmethod
    def apply_upstream_usage_state(
        config: UpstreamAccountConfig,
        state: Any,
        *,
        now: datetime,
        secrets: tuple[str | None, ...] = (),
    ) -> None:
        if state is None:
            UpstreamAccountService.clear_upstream_usage_state(config)
            return
        usage_amount = _balance_number(_value(state, "usage_amount"))
        if usage_amount is None or usage_amount < 0:
            UpstreamAccountService.clear_upstream_usage_state(config)
            return
        config.upstream_usage_amount = usage_amount
        config.upstream_usage_unit = (
            _safe_text(_value(state, "usage_unit"), secrets=secrets, limit=32) or "USD"
        )
        config.upstream_usage_checked_at = now

    @staticmethod
    async def automatic_upstream_disable_allowed() -> bool:
        try:
            runtime = get_runtime_config_service()
            return bool(
                await runtime.get_api_key_auto_disable_on_upstream_unavailable()
                and not await runtime.get_automation_paused()
            )
        except Exception:
            return False

    async def maybe_disable_for_upstream_state(
        self,
        remote: dict[str, Any],
        config: UpstreamAccountConfig,
        transition: UpstreamHealthTransition,
        *,
        allowed: bool,
    ) -> tuple[dict[str, Any], bool | None, bool | None, str | None, str | None]:
        old_schedulable = self.sub2api.account_schedulable(remote)
        if (
            not allowed
            or not transition.confirmed_invalid
            or config.upstream_health_invalid_count < 2
        ):
            return remote, old_schedulable, old_schedulable, None, None
        if old_schedulable is False:
            status = "already_disabled" if transition.previous_invalid_count < 2 else None
            return remote, old_schedulable, old_schedulable, status, None
        if old_schedulable is not True:
            return remote, old_schedulable, old_schedulable, None, None

        try:
            self._require_config_binding(remote, config)
            await self.sub2api.set_account_schedulable(
                config.sub2api_account_id,
                False,
            )
            readback = await self._remote_account(config.sub2api_account_id)
            self._require_config_binding(readback, config)
            new_schedulable = self.sub2api.account_schedulable(readback)
            if new_schedulable is not False:
                raise ValueError("schedulable readback mismatch")
            config.auto_disabled_reason = (
                "upstream_key_unavailable"
                if transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES
                else "upstream_group_unavailable"
            )
            config.last_auto_disabled_at = _utcnow()
            return readback, old_schedulable, new_schedulable, "account_disabled", None
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            return (
                remote,
                old_schedulable,
                self.sub2api.account_schedulable(remote),
                "disable_failed",
                "Unable to disable and verify the sub2api account after confirmed upstream unavailability.",
            )

    def record_upstream_health_change(
        self,
        db: AsyncSession,
        config: UpstreamAccountConfig,
        channel: UpstreamChannel | None,
        transition: UpstreamHealthTransition,
        *,
        old_remote_schedulable: bool | None,
        new_remote_schedulable: bool | None,
        action_status: str | None,
        safe_error: str | None,
        previous_group_multiplier: float | None,
        previous_recharge_multiplier: float | None,
        previous_target_rate: float | None,
        previous_current_rate: float | None,
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
            "upstream_auto_disable"
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
                sub2api_account_id=config.sub2api_account_id,
                account_name=_safe_text(
                    config.remote_name,
                    secrets=known_secrets,
                    limit=200,
                ),
                channel_id=channel.id if channel is not None else config.channel_id,
                channel_name=_safe_text(
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
                new_group_multiplier=config.effective_group_multiplier,
                old_upstream_multiplier=_calculate_composite_multiplier(
                    previous_group_multiplier,
                    previous_recharge_multiplier,
                ),
                new_upstream_multiplier=_calculate_composite_multiplier(
                    config.effective_group_multiplier,
                    config.effective_recharge_multiplier,
                ),
                old_upstream_recharge_multiplier=previous_recharge_multiplier,
                new_upstream_recharge_multiplier=config.effective_recharge_multiplier,
                upstream_recharge_multiplier=config.effective_recharge_multiplier,
                local_recharge_multiplier=config.local_recharge_multiplier,
                old_target_rate=previous_target_rate,
                new_target_rate=config.target_rate,
                old_current_rate=previous_current_rate,
                new_current_rate=config.current_rate,
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
    ) -> UpstreamAccountConfig:
        return UpstreamAccountConfig(
            sub2api_account_id=account_id,
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
            base_url=_safe_text(
                _remote_base_url(remote),
                secrets=secrets,
                limit=500,
            ),
            upstream_type="auto",
            group_multiplier_status="not_discovered",
            recharge_multiplier_status="not_discovered",
            local_recharge_status="not_checked",
            balance_status="not_checked",
            current_rate=self._remote_current_rate(remote),
        )

    @staticmethod
    def _invalidate_preview(config: UpstreamAccountConfig) -> None:
        config.resolved_upstream_type = None
        config.group_options = []
        config.discovered_group_multiplier = None
        config.effective_group_multiplier = None
        config.group_multiplier_source = None
        config.group_multiplier_status = "not_discovered"
        config.discovered_recharge_multiplier = None
        config.effective_recharge_multiplier = None
        config.recharge_multiplier_source = None
        config.recharge_multiplier_status = "not_discovered"
        config.target_rate = None
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
            raise UpstreamAccountServiceError("Unable to read API key accounts from sub2api.") from None

        seen_ids: set[int] = set()
        for account in accounts:
            account_id = self._numeric_remote_id(account)
            if account_id is None:
                continue
            if account_id in seen_ids:
                raise UpstreamAccountServiceError("sub2api returned duplicate API key account ids.")
            seen_ids.add(account_id)
        return accounts

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
                    raise UpstreamAccountServiceError(
                        "The sub2api account identity changed; refresh the account list and try again.",
                        status_code=409,
                    )
                return account
        raise UpstreamAccountServiceError("The sub2api API key account was not found.", status_code=404)

    def _remote_identity_fingerprint(self, account: dict[str, Any]) -> str:
        fingerprint = self._remote_fingerprint(account, include_endpoint=True)
        if fingerprint is None:
            raise UpstreamAccountServiceError("sub2api returned an invalid API key account identity.")
        return fingerprint

    def _remote_binding_fingerprint(self, account: dict[str, Any]) -> str | None:
        return self._remote_fingerprint(account, include_endpoint=False)

    def _known_local_secrets(
        self,
        config: UpstreamAccountConfig | None,
    ) -> tuple[str | None, str | None]:
        if config is None:
            return None, None
        return (
            decrypt_text(config.encrypted_api_key),
            decrypt_text(config.encrypted_access_token),
        )

    @staticmethod
    def _known_channel_secrets(
        channel: UpstreamChannel | None,
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
        config: UpstreamAccountConfig | None,
    ) -> UpstreamChannel | None:
        if config is None or config.channel_id is None:
            return None
        return await db.get(UpstreamChannel, config.channel_id)

    def _remote_rename_guard_fingerprint(self, account: dict[str, Any]) -> str:
        fingerprint = self._remote_fingerprint(
            account,
            include_endpoint=True,
            include_name=False,
            require_created_at=True,
        )
        if fingerprint is None:
            raise UpstreamAccountServiceError(
                "sub2api did not provide the immutable account creation timestamp required "
                "to rename this API key account.",
                status_code=409,
            )
        return fingerprint

    def _require_remote_binding_fingerprint(self, account: dict[str, Any]) -> str:
        fingerprint = self._remote_binding_fingerprint(account)
        if fingerprint is None:
            raise UpstreamAccountServiceError(
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
            raise UpstreamAccountServiceError("sub2api returned an invalid API key account id.")

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
        config: UpstreamAccountConfig | None,
    ) -> str:
        if config is None:
            return "unmanaged"
        stored = (config.remote_identity_fingerprint or "").strip().lower()
        if not stored:
            return "unbound"
        current = self._remote_binding_fingerprint(account)
        if current is None:
            return "mismatch"
        return "bound" if stored == current else "mismatch"

    def _config_binding_differs_only_by_remote_name(
        self,
        account: dict[str, Any],
        config: UpstreamAccountConfig,
        *,
        extra_secrets: tuple[str | None, ...] = (),
    ) -> bool:
        stored_fingerprint = str(config.remote_identity_fingerprint or "").strip().lower()
        stored_name = _safe_text(config.remote_name, limit=200)
        current_name = _safe_text(self.sub2api.account_name(account), limit=200)
        if not stored_fingerprint or not current_name:
            return False
        configured_endpoint = _normalize_base_url(config.base_url)
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
            if self._remote_binding_fingerprint(previous_name_view) == stored_fingerprint:
                return True
        return False

    def _require_config_binding(
        self,
        account: dict[str, Any],
        config: UpstreamAccountConfig | None,
    ) -> None:
        status = self._config_binding_status(account, config)
        if status in {"unbound", "mismatch"}:
            raise UpstreamAccountServiceError(
                "The local upstream configuration is bound to a different or unconfirmed "
                "sub2api account identity; explicitly rebind it before continuing.",
                status_code=409,
            )

    def _remote_current_rate(self, account: dict[str, Any]) -> float | None:
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
        config: UpstreamAccountConfig | None,
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
        config: UpstreamAccountConfig | None,
        *,
        extra_secrets: tuple[str | None, ...] = (),
        channel_name: str | None = None,
        priority_interval_name: str | None = None,
    ) -> UpstreamAccountOut:
        account_id = self._numeric_remote_id(account)
        if account_id is None:
            raise UpstreamAccountServiceError("sub2api returned an invalid API key account id.")
        secrets = (*self._known_local_secrets(config), *extra_secrets)
        binding_status = self._config_binding_status(account, config)
        identity_rebind_required = binding_status in {"unbound", "mismatch"}
        api_key_origin_rebind_required = bool(
            config is not None and config.api_key_origin_rebind_required
        )
        if binding_status != "bound":
            config = None
        remote_current_rate = self._remote_current_rate(account)
        current_rate = remote_current_rate
        if current_rate is None and config is not None:
            current_rate = config.current_rate
        target_rate = config.target_rate if config is not None else None
        would_change: bool | None = None
        if current_rate is not None and target_rate is not None:
            current = _decimal_multiplier(current_rate, allow_zero=True)
            target = _decimal_multiplier(target_rate, allow_zero=True)
            if current is not None and target is not None:
                would_change = _quantize_rate(current) != _quantize_rate(target)

        recharge_source = config.recharge_multiplier_source if config is not None else None
        discovered_recharge = config.discovered_recharge_multiplier if config is not None else None
        if recharge_source == "default":
            discovered_recharge = None

        return UpstreamAccountOut(
            sub2api_account_id=account_id,
            identity_fingerprint=self._remote_identity_fingerprint(account),
            identity_binding_status=binding_status,
            identity_rebind_required=identity_rebind_required,
            api_key_origin_rebind_required=api_key_origin_rebind_required,
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
            composite_multiplier=(
                _calculate_composite_multiplier(
                    config.effective_group_multiplier,
                    config.effective_recharge_multiplier,
                )
                if config is not None
                else None
            ),
            managed=config is not None,
            channel_id=config.channel_id if config is not None else None,
            channel_name=(
                _safe_text(channel_name, secrets=secrets, limit=200)
                if config is not None
                else None
            ),
            base_url=_safe_text(
                config.base_url if config is not None else _remote_base_url(account),
                secrets=secrets,
                limit=500,
            ),
            upstream_type=(config.upstream_type if config and config.upstream_type in {"auto", "newapi", "sub2api"} else "auto"),
            resolved_upstream_type=(
                config.resolved_upstream_type
                if config and config.resolved_upstream_type in {"newapi", "sub2api"}
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
            auto_disabled_reason=config.auto_disabled_reason if config else None,
            last_auto_disabled_at=config.last_auto_disabled_at if config else None,
            api_key_set=bool(
                not api_key_origin_rebind_required
                and (
                    (config and config.encrypted_api_key)
                    or self.sub2api.account_has_api_key(account)
                )
            ),
            api_key_hint=None,
            access_token_set=bool(config and config.encrypted_access_token),
            manual_group_multiplier=config.manual_group_multiplier if config else None,
            manual_recharge_multiplier=config.manual_recharge_multiplier if config else None,
            group_options=self._group_options_out(config, secrets=secrets),
            discovered_group_multiplier=config.discovered_group_multiplier if config else None,
            effective_group_multiplier=config.effective_group_multiplier if config else None,
            group_multiplier_source=config.group_multiplier_source if config else None,
            group_multiplier_status=config.group_multiplier_status if config else "unmanaged",
            discovered_recharge_multiplier=discovered_recharge,
            effective_recharge_multiplier=config.effective_recharge_multiplier if config else None,
            recharge_multiplier_source=recharge_source,
            recharge_multiplier_status=config.recharge_multiplier_status if config else "unmanaged",
            local_recharge_multiplier=config.local_recharge_multiplier if config else None,
            local_recharge_source=config.local_recharge_source if config else None,
            local_recharge_status=config.local_recharge_status if config else "not_checked",
            current_rate=current_rate,
            target_rate=target_rate,
            would_change=would_change,
            balance_remaining=config.balance_remaining if config else None,
            balance_total=config.balance_total if config else None,
            balance_used=config.balance_used if config else None,
            balance_unit=_safe_text(config.balance_unit if config else None, secrets=secrets, limit=32),
            balance_status=config.balance_status if config else "not_checked",
            balance_message=_safe_text(config.balance_message if config else None, secrets=secrets, limit=300),
            balance_checked_at=config.balance_checked_at if config else None,
            upstream_usage_amount=config.upstream_usage_amount if config else None,
            upstream_usage_unit=_safe_text(
                config.upstream_usage_unit if config else None,
                secrets=secrets,
                limit=32,
            ),
            upstream_usage_checked_at=config.upstream_usage_checked_at if config else None,
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

    async def list_accounts(self, db: AsyncSession) -> list[UpstreamAccountOut]:
        remote_accounts = await self._remote_accounts()
        remote_by_id = {
            account_id: account
            for account in remote_accounts
            if (account_id := self._numeric_remote_id(account)) is not None
        }
        configs: dict[int, UpstreamAccountConfig] = {}
        if remote_by_id:
            result = await db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id.in_(sorted(remote_by_id))
                )
            )
            configs = {item.sub2api_account_id: item for item in result.scalars().all()}
        channel_ids = sorted(
            {
                config.channel_id
                for config in configs.values()
                if config.channel_id is not None
            }
        )
        channels: dict[int, UpstreamChannel] = {}
        if channel_ids:
            result = await db.execute(
                select(UpstreamChannel).where(UpstreamChannel.id.in_(channel_ids))
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
        accounts = [
            self._build_out(
                remote_by_id[account_id],
                configs.get(account_id),
                extra_secrets=self._known_channel_secrets(
                    channels.get(configs[account_id].channel_id)
                    if account_id in configs and configs[account_id].channel_id is not None
                    else None
                ),
                channel_name=(
                    channels[configs[account_id].channel_id].display_name
                    if account_id in configs
                    and configs[account_id].channel_id in channels
                    else None
                ),
                priority_interval_name=(
                    priority_intervals[configs[account_id].priority_interval_id].name
                    if account_id in configs
                    and configs[account_id].priority_interval_id in priority_intervals
                    else None
                ),
            )
            for account_id in sorted(remote_by_id)
        ]
        return sorted(
            accounts,
            key=lambda item: (
                item.composite_multiplier is None,
                item.composite_multiplier or 0,
                item.sub2api_account_id,
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
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.sub2api_account_id.in_(requested_ids)
            )
        )
        configs = {
            config.sub2api_account_id: config
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
                raise UpstreamAccountServiceError(
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
                raise UpstreamAccountServiceError(
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
                raise UpstreamAccountServiceError(
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
                raise UpstreamAccountServiceError(
                    "The legacy identity confirmation is stale; refresh the API key account list.",
                    status_code=409,
                )

        bound = 0
        for account_id, fingerprint in pending_bindings.items():
            result = await db.execute(
                update(UpstreamAccountConfig)
                .where(
                    UpstreamAccountConfig.sub2api_account_id == account_id,
                    UpstreamAccountConfig.remote_identity_fingerprint.is_(None),
                )
                .values(remote_identity_fingerprint=fingerprint)
                .execution_options(synchronize_session=False)
            )
            bound += int(result.rowcount or 0)
        for account_id, fingerprint in pending_origin_rebinds.items():
            result = await db.execute(
                update(UpstreamAccountConfig)
                .where(
                    UpstreamAccountConfig.sub2api_account_id == account_id,
                    UpstreamAccountConfig.remote_identity_fingerprint == fingerprint,
                    UpstreamAccountConfig.api_key_origin_rebind_required.is_(True),
                )
                .values(api_key_origin_rebind_required=False)
                .execution_options(synchronize_session=False)
            )
            bound += int(result.rowcount or 0)
        await db.commit()
        db.expire_all()
        return bound

    def _ensure_secret_storage_ready(self, payload: UpstreamAccountUpdate) -> None:
        if payload.api_key and len(payload.api_key) > 4096:
            raise UpstreamAccountServiceError("The API key is too long.", status_code=422)
        if payload.access_token and len(payload.access_token) > 8192:
            raise UpstreamAccountServiceError("The access token is too long.", status_code=422)
        if payload.clear_access_token and payload.access_token:
            raise UpstreamAccountServiceError(
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
            raise UpstreamAccountServiceError(
                "Configure a non-default application encryption key before saving credentials.",
                status_code=503,
            )

    async def _upsert_account(
        self,
        db: AsyncSession,
        account_id: int,
        payload: UpstreamAccountUpdate,
    ) -> UpstreamAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            self._ensure_secret_storage_ready(payload)
            remote = await self._remote_account(
                account_id,
                payload.expected_identity_fingerprint,
            )
            result = await db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == account_id
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
            preview_fields = {
                "channel_id",
                "base_url",
                "upstream_type",
                "upstream_user_id",
                "selected_group_id",
                "selected_group_name",
                "manual_group_multiplier",
                "manual_recharge_multiplier",
            }
            preview_invalidated = bool(fields & preview_fields) or bool(
                payload.api_key or payload.access_token or payload.clear_access_token
            ) or binding_rebound
            current_api_key = (
                decrypt_text(config.encrypted_api_key)
                if not config.api_key_origin_rebind_required
                else None
            )
            current_access_token = decrypt_text(config.encrypted_access_token)
            previous_origin = upstream_url_origin(config.base_url)
            current_channel = await self._channel_for_config(db, config)
            selected_channel = current_channel
            if "channel_id" in fields:
                selected_channel = (
                    await db.get(UpstreamChannel, payload.channel_id)
                    if payload.channel_id is not None
                    else None
                )
            if "channel_id" in fields and payload.channel_id is not None:
                if selected_channel is None:
                    raise UpstreamAccountServiceError(
                        "The upstream channel was not found.", status_code=404
                    )
            identity_changed = (
                binding_rebound
                or
                ("channel_id" in fields and payload.channel_id != config.channel_id)
                or
                ("base_url" in fields and payload.base_url != config.base_url)
                or ("upstream_type" in fields and payload.upstream_type != config.upstream_type)
                or ("upstream_user_id" in fields and payload.upstream_user_id != config.upstream_user_id)
                or (bool(payload.api_key) and payload.api_key != current_api_key)
                or (bool(payload.access_token) and payload.access_token != current_access_token)
                or (payload.clear_access_token and current_access_token is not None)
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
            if "channel_id" in fields:
                channel_changed = payload.channel_id != config.channel_id
                config.channel_id = payload.channel_id
                if channel_changed:
                    config.channel_auto_assign_disabled = True
                if selected_channel is not None:
                    config.base_url = selected_channel.canonical_base_url
                    config.upstream_type = selected_channel.upstream_type
                    config.resolved_upstream_type = selected_channel.resolved_upstream_type
                    config.upstream_user_id = selected_channel.upstream_user_id
                    config.encrypted_access_token = selected_channel.encrypted_access_token
                    config.manual_recharge_multiplier = selected_channel.manual_recharge_multiplier
            if identity_changed:
                config.selected_group_id = None
                config.selected_group_name = None
            if "base_url" in fields:
                config.base_url = payload.base_url
            if "upstream_type" in fields or is_new:
                config.upstream_type = payload.upstream_type
            for field in (
                "upstream_user_id",
                "selected_group_id",
                "selected_group_name",
                "manual_group_multiplier",
                "manual_recharge_multiplier",
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

            next_origin = upstream_url_origin(config.base_url)
            if (
                previous_origin is not None
                and next_origin is not None
                and next_origin != previous_origin
                and not payload.confirm_credential_rebind
            ):
                raise UpstreamAccountServiceError(
                    "Changing the upstream origin requires explicit credential rebind confirmation.",
                    status_code=409,
                )

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
                            raise UpstreamAccountServiceError(
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
                    except UpstreamAccountServiceError:
                        raise
                    except Exception as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        raise UpstreamAccountServiceError(
                            "Unable to rename the sub2api API key account."
                        ) from None
                    if self._remote_rename_guard_fingerprint(renamed_remote) != rename_guard:
                        raise UpstreamAccountServiceError(
                            "The sub2api account identity changed while its name was being updated; "
                            "refresh the account list and try again.",
                            status_code=409,
                        )
                    remote = renamed_remote
                config.remote_identity_fingerprint = self._require_remote_binding_fingerprint(
                    remote
                )

            if preview_invalidated and not is_new:
                self._invalidate_preview(config)
                if identity_changed:
                    self.clear_upstream_usage_state(config)

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

            remote_rate = self._remote_current_rate(remote)
            if remote_rate is not None:
                config.current_rate = remote_rate
            await db.commit()
            await db.refresh(config)
            return self._build_out(remote, config, extra_secrets=known_secrets)

    async def upsert_account(
        self,
        db: AsyncSession,
        account_id: int,
        payload: UpstreamAccountUpdate,
        *,
        defer_priority_rebalance: bool = False,
    ) -> UpstreamAccountOut:
        account = await self._upsert_account(db, account_id, payload)
        if defer_priority_rebalance:
            return account
        await self._rebalance_priorities_best_effort(db)
        refreshed = await self.list_accounts(db)
        return next(
            (item for item in refreshed if item.sub2api_account_id == account_id),
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
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                return False
            self._require_config_binding(remote, config)
            await db.delete(config)
            await db.commit()
        await self._rebalance_priorities_best_effort(db)
        return True

    async def set_account_enabled(
        self,
        db: AsyncSession,
        account_id: int,
        enabled: bool,
        expected_identity_fingerprint: str,
    ) -> UpstreamAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            result = await db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            self._require_config_binding(remote, config)
            try:
                await self.sub2api.set_account_schedulable(account_id, enabled)
                readback = await self._remote_account(account_id, expected_identity_fingerprint)
            except UpstreamAccountServiceError:
                raise
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise UpstreamAccountServiceError(
                    "Unable to update and verify the sub2api account state."
                ) from None
            if readback is None or self.sub2api.account_schedulable(readback) is not enabled:
                raise UpstreamAccountServiceError(
                    "sub2api account state readback did not match the requested state."
                )
            if enabled and config is not None:
                config.auto_disabled_reason = None
                config.last_auto_disabled_at = None
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
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            self._require_config_binding(remote, config)
            try:
                deleted = await self.sub2api.delete_account(remote)
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise UpstreamAccountServiceError(
                    "Unable to delete the sub2api API key account."
                ) from None
            if not deleted:
                raise UpstreamAccountServiceError(
                    "The sub2api API key account was not found.",
                    status_code=404,
                )

            channel_id = config.channel_id if config is not None else None
            if config is not None:
                await db.delete(config)
                await db.flush()
            if channel_id is not None:
                remaining = await db.execute(
                    select(UpstreamAccountConfig.id)
                    .where(UpstreamAccountConfig.channel_id == channel_id)
                    .limit(1)
                )
                if remaining.scalar_one_or_none() is None:
                    channel = await db.get(UpstreamChannel, channel_id)
                    if channel is not None:
                        await db.delete(channel)
            await db.commit()
        await self._rebalance_priorities_best_effort(db)
        return True

    async def _call_upstream_discovery(self, config: UpstreamAccountConfig) -> Any:
        result = discover_upstream(
            base_url=config.base_url or "",
            upstream_type=config.upstream_type,
            api_key=(
                decrypt_text(config.encrypted_api_key)
                if not config.api_key_origin_rebind_required
                else None
            ),
            access_token=decrypt_text(config.encrypted_access_token),
            new_api_user=config.upstream_user_id,
            selected_group_id=config.selected_group_id,
            selected_group_name=config.selected_group_name,
        )
        return await result if inspect.isawaitable(result) else result

    def _apply_balance_result(
        self,
        config: UpstreamAccountConfig,
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
                ("balance_remaining", "remaining"),
                ("balance_total", "total"),
                ("balance_used", "used"),
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
        config: UpstreamAccountConfig,
    ) -> UpstreamAccountOut:
        self._require_config_binding(remote, config)
        now = _utcnow()
        channel = await self._channel_for_config(db, config)
        channel_secrets = self._known_channel_secrets(channel)
        stored_api_key, access_token = self._known_local_secrets(config)
        api_key = None if config.api_key_origin_rebind_required else stored_api_key
        known_secrets = (stored_api_key, access_token, *channel_secrets)
        has_credentials = bool(api_key or access_token)
        has_base_url = bool(config.base_url)
        previous_group_multiplier = config.effective_group_multiplier
        previous_recharge_multiplier = config.effective_recharge_multiplier
        previous_target_rate = config.target_rate
        previous_current_rate = config.current_rate
        upstream_state: Any = None

        balance_task = self.sub2api.get_account_balance(config.sub2api_account_id)
        local_recharge_task = self.sub2api.get_payment_balance_recharge_multiplier_info()
        tasks: list[Any] = [balance_task, local_recharge_task]
        upstream_attempted = has_credentials and has_base_url
        if upstream_attempted:
            tasks.append(self._call_upstream_discovery(config))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        balance_result, local_recharge_result = results[0], results[1]
        upstream_result = results[2] if upstream_attempted else None

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
        if isinstance(local_recharge_result, BaseException):
            config.local_recharge_status = "error"
            errors.append("Target recharge multiplier check failed.")
        elif (
            isinstance(local_recharge_result, tuple)
            and len(local_recharge_result) == 2
        ):
            local_credit_per_cny = _decimal_multiplier(local_recharge_result[0])
            if local_credit_per_cny is None:
                config.local_recharge_status = "error"
                errors.append("Target recharge multiplier is invalid.")
            else:
                try:
                    fresh_local = _decimal_multiplier(Decimal("1") / local_credit_per_cny)
                except DecimalException:
                    fresh_local = None
                if fresh_local is None:
                    config.local_recharge_status = "error"
                    errors.append("Target recharge multiplier is outside the supported range.")
                else:
                    field_present = bool(local_recharge_result[1])
                    config.local_recharge_multiplier = float(fresh_local)
                    config.local_recharge_source = "sub2api_settings" if field_present else "default"
                    config.local_recharge_status = "ok" if field_present else "default_missing"
        else:
            config.local_recharge_status = "error"
            errors.append("Target recharge multiplier result is invalid.")

        fresh_group: Decimal | None = None
        fresh_recharge: Decimal | None = None
        fresh_group_source: str | None = None
        fresh_recharge_source: str | None = None
        discovery_failed = False
        discovery_succeeded = False
        recharge_failed = False
        recharge_invalid = False
        if upstream_attempted:
            if isinstance(upstream_result, BaseException):
                discovery_failed = True
                errors.append("Upstream discovery failed.")
            else:
                discovery_status = str(_value(upstream_result, "status") or "error").strip().lower()
                if discovery_status == "insecure_url":
                    raise UpstreamAccountServiceError(
                        "Credentials may only be sent to an HTTPS upstream URL.",
                        status_code=422,
                    )
                if discovery_status != "ok":
                    discovery_failed = True
                    errors.append("Upstream discovery failed.")
                else:
                    discovery_succeeded = True
                    upstream_state = _value(upstream_result, "matched_account_state")
                    self.apply_upstream_usage_state(
                        config,
                        upstream_state,
                        now=now,
                        secrets=known_secrets,
                    )
                    resolved_type = str(_value(upstream_result, "upstream_type") or "").strip().lower()
                    if resolved_type in {"newapi", "sub2api"}:
                        config.resolved_upstream_type = resolved_type
                    options_value = _value(upstream_result, "groups", "group_options", "available_groups")
                    if options_value is not None:
                        config.group_options = _sanitize_group_options(
                            options_value,
                            secrets=known_secrets,
                        )
                    raw_group = _value(
                        upstream_result,
                        "discovered_group_multiplier",
                        "group_multiplier",
                        "selected_group_multiplier",
                    )
                    fresh_group = _decimal_multiplier(raw_group)
                    raw_recharge = _value(
                        upstream_result,
                        "discovered_recharge_multiplier",
                        "recharge_multiplier",
                        "balance_recharge_multiplier",
                    )
                    fresh_recharge = _decimal_multiplier(raw_recharge)
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
                        _value(upstream_result, "discovered_group_multiplier_source"),
                        secrets=known_secrets,
                        limit=128,
                    )
                    fresh_recharge_source = _safe_text(
                        _value(upstream_result, "discovered_recharge_multiplier_source"),
                        secrets=known_secrets,
                        limit=128,
                    )
                    matched_group = _value(upstream_result, "matched_group")
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
                        config.discovered_group_multiplier = float(fresh_group)
                    if fresh_recharge is not None:
                        config.discovered_recharge_multiplier = float(fresh_recharge)

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

        manual_group = _decimal_multiplier(config.manual_group_multiplier)
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
        config.effective_group_multiplier = float(effective_group) if effective_group is not None else None

        manual_recharge = _decimal_multiplier(config.manual_recharge_multiplier)
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
        config.effective_recharge_multiplier = (
            float(effective_recharge) if effective_recharge is not None else None
        )

        current_rate = self._remote_current_rate(remote)
        if current_rate is not None:
            config.current_rate = current_rate

        if (
            effective_group is not None
            and effective_recharge is not None
            and fresh_local is not None
        ):
            target = _calculate_target_rate(effective_group, effective_recharge, fresh_local)
            if target is None:
                config.target_rate = None
                errors.append("Calculated target rate is outside the supported range.")
            else:
                config.target_rate = float(target)
        else:
            config.target_rate = None

        if health_transition.new_group_status in INVALID_UPSTREAM_GROUP_STATUSES:
            config.target_rate = None
            config.group_multiplier_status = "group_unavailable"
            errors.append("The synchronized upstream group is unavailable.")
        elif health_transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES:
            config.target_rate = None
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
                    config.sub2api_account_id,
                    secrets=known_secrets,
                ),
                secrets=known_secrets,
                limit=200,
            )
            or f"Account #{config.sub2api_account_id}"
        )
        config.last_error = " ".join(dict.fromkeys(errors)) or None
        config.last_discovered_at = now
        current_remote = await self._remote_account(config.sub2api_account_id)
        self._require_config_binding(current_remote, config)
        auto_disable_allowed = await self.automatic_upstream_disable_allowed()
        (
            current_remote,
            old_remote_schedulable,
            new_remote_schedulable,
            health_action_status,
            health_safe_error,
        ) = await self.maybe_disable_for_upstream_state(
            current_remote,
            config,
            health_transition,
            allowed=auto_disable_allowed,
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
            previous_target_rate=previous_target_rate,
            previous_current_rate=previous_current_rate,
        )
        await db.commit()
        await db.refresh(config)
        return self._build_out(current_remote, config, extra_secrets=channel_secrets)

    async def discover_account(
        self,
        db: AsyncSession,
        account_id: int,
        expected_identity_fingerprint: str,
    ) -> UpstreamAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            result = await db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == account_id
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
            (item for item in refreshed if item.sub2api_account_id == account_id),
            account,
        )

    async def discover_all(self, db: AsyncSession) -> UpstreamDiscoverAllOut:
        remote_by_id = {
            account_id: account
            for account in await self._remote_accounts()
            if (account_id := self._numeric_remote_id(account)) is not None
        }
        if not remote_by_id:
            return UpstreamDiscoverAllOut(total=0, succeeded=0, failed=0, accounts=[])
        accounts: list[UpstreamAccountOut] = []
        succeeded = 0
        failed = 0
        for account_id in sorted(remote_by_id):
            remote = remote_by_id[account_id]
            lock = await self._lock_for(account_id)
            async with lock:
                result = await db.execute(
                    select(UpstreamAccountConfig)
                    .where(
                        UpstreamAccountConfig.sub2api_account_id == account_id
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
                    account.target_rate is not None
                    or account.balance_status in {"ok", "success", "available"}
                )
            ):
                succeeded += 1
            else:
                failed += 1
        await self._rebalance_priorities_after_discovery(db)
        accounts = await self.list_accounts(db)
        return UpstreamDiscoverAllOut(
            total=len(remote_by_id),
            succeeded=succeeded,
            failed=failed,
            accounts=accounts,
        )

    async def _apply_account(
        self,
        db: AsyncSession,
        account_id: int,
        confirmed_target_rate: float,
        expected_identity_fingerprint: str,
    ) -> UpstreamAccountOut:
        lock = await self._lock_for(account_id)
        async with lock:
            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            result = await db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == account_id
                )
            )
            config = result.scalar_one_or_none()
            if config is None:
                raise UpstreamAccountServiceError(
                    "Save the upstream account configuration before applying a rate.",
                    status_code=404,
                )
            self._require_config_binding(remote, config)

            discovered = await self._discover_locked(db, remote, config)
            target = _decimal_multiplier(discovered.target_rate, allow_zero=True)
            confirmed = _decimal_multiplier(confirmed_target_rate, allow_zero=True)
            if target is None or confirmed is None:
                raise UpstreamAccountServiceError(
                    "A valid target rate could not be calculated.",
                    status_code=409,
                )
            target = _quantize_rate(target)
            confirmed = _quantize_rate(confirmed)
            if target != confirmed:
                raise UpstreamAccountServiceError(
                    "The confirmed target rate is stale; discover the account again.",
                    status_code=409,
                )

            remote = await self._remote_account(account_id, expected_identity_fingerprint)
            self._require_config_binding(remote, config)
            try:
                await self.sub2api.update_account_rate_multiplier(account_id, float(target))
                readback = await self.sub2api.get_account_current_rate_multiplier(account_id)
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                config.last_error = "Unable to update and verify the sub2api account rate."
                await db.commit()
                raise UpstreamAccountServiceError(
                    "Unable to update and verify the sub2api account rate."
                ) from None

            readback_decimal = _decimal_multiplier(readback, allow_zero=True)
            config.current_rate = readback
            if readback_decimal is None or _quantize_rate(readback_decimal) != target:
                config.last_error = "sub2api rate readback did not match the requested target."
                await db.commit()
                raise UpstreamAccountServiceError(
                    "sub2api rate readback did not match the requested target."
                )

            config.last_applied_at = _utcnow()
            config.last_error = None
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
        confirmed_target_rate: float,
        expected_identity_fingerprint: str,
    ) -> UpstreamAccountOut:
        try:
            account = await self._apply_account(
                db,
                account_id,
                confirmed_target_rate,
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
            (item for item in refreshed if item.sub2api_account_id == account_id),
            account,
        )


_service: UpstreamAccountService | None = None


def get_upstream_account_service() -> UpstreamAccountService:
    global _service
    if _service is None:
        _service = UpstreamAccountService()
    return _service
