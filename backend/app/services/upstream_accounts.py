from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.upstream_urls import canonicalize_upstream_url, upstream_url_origin
from app.models import UpstreamAccountConfig, UpstreamChannel
from app.schemas import (
    UpstreamAccountOut,
    UpstreamAccountUpdate,
    UpstreamDiscoverAllOut,
    UpstreamGroupOptionOut,
)
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError
from app.services.upstream_client import discover_upstream


RATE_QUANTUM = Decimal("0.0001")
MAX_MULTIPLIER = Decimal("1000")
DEFAULT_ENCRYPTION_KEY = "change-me-encryption-key"
JS_SAFE_INTEGER_MAX = 9_007_199_254_740_991


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
    def __init__(self, sub2api: Sub2ApiClient | None = None) -> None:
        self.sub2api = sub2api or Sub2ApiClient()
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

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
                self._remote_name(remote, account_id),
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
        if not include_endpoint and not created_at:
            return None
        identity = {
            "id": account_id,
            "name": normalized_text(self.sub2api.account_name(account)),
            "platform": normalized_text(self.sub2api.account_platform(account), casefold=True),
            "type": account_type,
            "created_at": created_at,
        }
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

    def _remote_name(self, account: dict[str, Any], account_id: int) -> str:
        name = self.sub2api.account_name(account)
        return _safe_text(name, limit=200) or f"Account #{account_id}"

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
    ) -> UpstreamAccountOut:
        account_id = self._numeric_remote_id(account)
        if account_id is None:
            raise UpstreamAccountServiceError("sub2api returned an invalid API key account id.")
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

        api_key = (
            decrypt_text(config.encrypted_api_key)
            if config is not None and not api_key_origin_rebind_required
            else None
        )
        access_token = decrypt_text(config.encrypted_access_token) if config is not None else None
        secrets = (api_key, access_token)
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
                    config.remote_name if config and config.remote_name else self._remote_name(account, account_id),
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
            managed=config is not None,
            channel_id=config.channel_id if config is not None else None,
            channel_name=None,
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
        return [
            self._build_out(remote_by_id[account_id], configs.get(account_id))
            for account_id in sorted(remote_by_id)
        ]

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

    async def upsert_account(
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
            binding_fingerprint = self._remote_binding_fingerprint(remote)
            if config is None:
                binding_fingerprint = self._require_remote_binding_fingerprint(remote)
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
                    binding_fingerprint = self._require_remote_binding_fingerprint(remote)
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
            selected_channel: UpstreamChannel | None = None
            if "channel_id" in fields and payload.channel_id is not None:
                selected_channel = await db.get(UpstreamChannel, payload.channel_id)
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
                or (bool(payload.api_key) and payload.api_key != current_api_key)
                or (bool(payload.access_token) and payload.access_token != current_access_token)
                or (payload.clear_access_token and current_access_token is not None)
            )
            known_secrets = (
                payload.api_key or current_api_key,
                payload.access_token or current_access_token,
            )
            remote_name = _safe_text(
                self._remote_name(remote, account_id),
                secrets=known_secrets,
                limit=200,
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
            if "remote_name" in fields and payload.remote_name:
                config.remote_name = _safe_text(
                    payload.remote_name,
                    secrets=known_secrets,
                    limit=200,
                )
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

            if preview_invalidated and not is_new:
                self._invalidate_preview(config)

            if binding_rebound:
                config.remote_identity_fingerprint = binding_fingerprint

            remote_rate = self._remote_current_rate(remote)
            if remote_rate is not None:
                config.current_rate = remote_rate
            await db.commit()
            await db.refresh(config)
            return self._build_out(remote, config)

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
            return self._build_out(readback, config)

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
        api_key = (
            decrypt_text(config.encrypted_api_key)
            if not config.api_key_origin_rebind_required
            else None
        )
        access_token = decrypt_text(config.encrypted_access_token)
        has_credentials = bool(api_key or access_token)
        has_base_url = bool(config.base_url)

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
                    resolved_type = str(_value(upstream_result, "upstream_type") or "").strip().lower()
                    if resolved_type in {"newapi", "sub2api"}:
                        config.resolved_upstream_type = resolved_type
                    options_value = _value(upstream_result, "groups", "group_options", "available_groups")
                    if options_value is not None:
                        config.group_options = _sanitize_group_options(
                            options_value,
                            secrets=(api_key, access_token),
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
                        secrets=(api_key, access_token),
                        limit=128,
                    )
                    fresh_recharge_source = _safe_text(
                        _value(upstream_result, "discovered_recharge_multiplier_source"),
                        secrets=(api_key, access_token),
                        limit=128,
                    )
                    matched_group = _value(upstream_result, "matched_group")
                    selected_id = _value(matched_group, "id", "group_id")
                    selected_name = _value(matched_group, "name", "group_name")
                    if selected_id is not None:
                        config.selected_group_id = (
                            _safe_text(
                                selected_id,
                                secrets=(api_key, access_token),
                                limit=128,
                            )
                            or config.selected_group_id
                        )
                    if selected_name is not None:
                        config.selected_group_name = (
                            _safe_text(
                                selected_name,
                                secrets=(api_key, access_token),
                                limit=200,
                            )
                            or config.selected_group_name
                        )
                    if fresh_group is not None:
                        config.discovered_group_multiplier = float(fresh_group)
                    if fresh_recharge is not None:
                        config.discovered_recharge_multiplier = float(fresh_recharge)

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

        config.remote_platform = _safe_text(self.sub2api.account_platform(remote), limit=64)
        config.remote_account_type = _safe_text(self.sub2api.account_type(remote), limit=32)
        if not config.remote_name:
            config.remote_name = self._remote_name(remote, config.sub2api_account_id)
        config.last_error = " ".join(dict.fromkeys(errors)) or None
        config.last_discovered_at = now
        current_remote = await self._remote_account(config.sub2api_account_id)
        self._require_config_binding(current_remote, config)
        await db.commit()
        await db.refresh(config)
        return self._build_out(remote, config)

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
            return await self._discover_locked(db, remote, config)

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
                    accounts.append(self._build_out(remote, config))
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
        return UpstreamDiscoverAllOut(
            total=len(remote_by_id),
            succeeded=succeeded,
            failed=failed,
            accounts=accounts,
        )

    async def apply_account(
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
            return self._build_out(remote, config)


_service: UpstreamAccountService | None = None


def get_upstream_account_service() -> UpstreamAccountService:
    global _service
    if _service is None:
        _service = UpstreamAccountService()
    return _service
