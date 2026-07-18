from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import AsyncExitStack
from decimal import Decimal, DecimalException
from datetime import timezone
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import AsyncSessionLocal
from app.core.upstream_urls import canonicalize_upstream_url, upstream_url_origin
from app.models import UpstreamAccountConfig, UpstreamChannel, UpstreamRateChangeLog
from app.schemas import (
    UpstreamAccountOut,
    UpstreamChannelDiscoverAllOut,
    UpstreamChannelOut,
    UpstreamChannelUpdate,
    UpstreamGroupOptionOut,
    UpstreamOverviewOut,
)
from app.services.sub2api import Sub2ApiClient
from app.services.events import elapsed_ms
from app.services.runtime_config import get_runtime_config_service
from app.services.upstream_accounts import (
    DEFAULT_ENCRYPTION_KEY,
    INVALID_UPSTREAM_GROUP_STATUSES,
    INVALID_UPSTREAM_KEY_STATUSES,
    UpstreamAccountService,
    UpstreamAccountServiceError,
    UpstreamHealthTransition,
    get_upstream_account_service,
    _calculate_target_rate,
    _balance_number,
    _decimal_multiplier,
    _remote_base_url,
    _quantize_rate,
    _safe_text,
    _sanitize_group_options,
    _utcnow,
    _value,
)
from app.services.upstream_client import (
    DEFAULT_TODAY_TIME_ZONE,
    MAX_UPSTREAM_TOKEN_LENGTH,
    discover_upstream,
    refresh_sub2api_tokens,
)
from app.services.workflow_coordination import get_workflow_coordinator


API_KEY_EXPORT_BATCH_SIZE = 200
logger = logging.getLogger(__name__)


class UpstreamChannelService:
    """Manage shared upstream-site state independently from API-key accounts."""

    def __init__(
        self,
        sub2api: Sub2ApiClient | None = None,
        accounts: UpstreamAccountService | None = None,
        priorities: Any | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.accounts = accounts or UpstreamAccountService(sub2api or Sub2ApiClient())
        self.sub2api = self.accounts.sub2api
        self._priorities = priorities
        self._session_factory = session_factory
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._inventory_lock = asyncio.Lock()
        self._discover_all_lock = asyncio.Lock()

    def _priority_service(self):
        if self._priorities is None:
            from app.services.upstream_priorities import UpstreamPriorityService

            self._priorities = UpstreamPriorityService(accounts=self.accounts)
        return self._priorities

    async def _rebalance_priorities_best_effort(
        self,
        db: AsyncSession,
        *,
        account_ids: set[int] | None = None,
    ) -> None:
        try:
            runtime = get_runtime_config_service()
            getter = getattr(runtime, "get_upstream_priority_sync_enabled", None)
            if getter is not None and not bool(await getter()):
                return
            if account_ids is not None:
                if not account_ids:
                    return
                await self._priority_service().rebalance(
                    db,
                    account_ids=account_ids,
                )
            else:
                await self._priority_service().rebalance(db)
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            await db.rollback()
            logger.exception("Priority rebalance failed after an upstream channel mutation.")

    async def _lock_for(self, channel_id: int) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(channel_id, asyncio.Lock())

    @staticmethod
    def _channel_cache_is_fresh(
        channel: UpstreamChannelOut,
        max_age_seconds: int | None,
    ) -> bool:
        if (
            channel.last_discovered_at is None
            or channel.last_error == "Upstream channel discovery failed."
        ):
            return False
        if max_age_seconds is not None:
            if max_age_seconds <= 0:
                return False
            observed_at = channel.last_discovered_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            age_seconds = (_utcnow() - observed_at).total_seconds()
            if age_seconds < 0 or age_seconds > max_age_seconds:
                return False
        return all(account.last_discovered_at is not None for account in channel.accounts)

    @staticmethod
    def _default_name(base_url: str) -> str:
        parsed = urlsplit(base_url)
        return parsed.hostname or "Upstream channel"

    @staticmethod
    def _invalidate_account(config: UpstreamAccountConfig) -> None:
        config.discovered_group_multiplier = None
        config.effective_group_multiplier = None
        config.group_multiplier_source = None
        config.group_multiplier_status = "not_discovered"
        config.target_rate = None
        config.last_discovered_at = None
        config.upstream_key_status = "not_checked"
        config.upstream_group_status = "not_checked"
        config.upstream_health_invalid_count = 0
        config.upstream_key_checked_at = None
        config.upstream_group_checked_at = None
        UpstreamAccountService.clear_upstream_usage_state(config)
        if config.priority_interval_id is not None:
            config.desired_priority = None
            config.priority_sync_status = "multiplier_unavailable"
            config.priority_sync_error = None

    @staticmethod
    def _invalidate_channel(channel: UpstreamChannel) -> None:
        channel.resolved_upstream_type = None
        channel.group_options = []
        channel.discovered_recharge_multiplier = None
        channel.effective_recharge_multiplier = None
        channel.recharge_multiplier_source = None
        channel.recharge_multiplier_status = "not_discovered"
        channel.balance_remaining = None
        channel.balance_total = None
        channel.balance_used = None
        channel.balance_unit = None
        channel.balance_status = "not_checked"
        channel.balance_message = None
        channel.balance_checked_at = None
        channel.today_balance_used = None
        channel.today_balance_unit = None
        channel.today_balance_status = "not_checked"
        channel.today_balance_checked_at = None
        channel.last_error = None
        channel.last_discovered_at = None

    def _known_secrets(
        self,
        config: UpstreamAccountConfig | None,
        channel: UpstreamChannel | None = None,
    ) -> tuple[str | None, ...]:
        return (
            *self.accounts._known_local_secrets(config),
            decrypt_text(channel.encrypted_access_token) if channel is not None else None,
            decrypt_text(channel.encrypted_refresh_token) if channel is not None else None,
        )

    def _ensure_secret_storage_ready(self, payload: UpstreamChannelUpdate) -> None:
        if payload.access_token and len(payload.access_token) > MAX_UPSTREAM_TOKEN_LENGTH:
            raise UpstreamAccountServiceError("The access token is too long.", status_code=422)
        if payload.refresh_token and len(payload.refresh_token) > MAX_UPSTREAM_TOKEN_LENGTH:
            raise UpstreamAccountServiceError("The refresh token is too long.", status_code=422)
        if payload.clear_access_token and payload.access_token:
            raise UpstreamAccountServiceError(
                "An access token cannot be set and cleared in the same request.",
                status_code=422,
            )
        if payload.clear_refresh_token and payload.refresh_token:
            raise UpstreamAccountServiceError(
                "A refresh token cannot be set and cleared in the same request.",
                status_code=422,
            )
        settings = get_settings()
        if (
            (payload.access_token or payload.refresh_token)
            and settings.app_env == "production"
            and settings.app_encryption_key.strip() == DEFAULT_ENCRYPTION_KEY
        ):
            raise UpstreamAccountServiceError(
                "Configure a non-default application encryption key before saving credentials.",
                status_code=503,
            )

    async def _load_channel(self, db: AsyncSession, channel_id: int) -> UpstreamChannel:
        channel = await db.get(UpstreamChannel, channel_id)
        if channel is None:
            raise UpstreamAccountServiceError("The upstream channel was not found.", status_code=404)
        return channel

    async def _bound_configs(
        self,
        db: AsyncSession,
        channel_id: int,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> list[UpstreamAccountConfig]:
        result = await db.execute(
            select(UpstreamAccountConfig).where(UpstreamAccountConfig.channel_id == channel_id)
        )
        return await self._filter_current_bindings(
            list(result.scalars().all()),
            remote_by_id=remote_by_id,
        )

    async def _filter_current_bindings(
        self,
        configs: list[UpstreamAccountConfig],
        *,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> list[UpstreamAccountConfig]:
        if not configs:
            return []
        if remote_by_id is None:
            remote_by_id = {
                account_id: account
                for account in await self.accounts._remote_accounts()
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        return [
            config
            for config in configs
            if (
                (remote := remote_by_id.get(config.sub2api_account_id)) is not None
                and self.accounts._config_binding_status(remote, config) == "bound"
            )
        ]

    async def _account_api_keys(
        self,
        db: AsyncSession,
        configs: list[UpstreamAccountConfig],
        channel_id: int,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> dict[int, str]:
        channel = await self._load_channel(db, channel_id)
        try:
            channel_base_url = canonicalize_upstream_url(channel.canonical_base_url)
        except ValueError:
            configs.clear()
            return {}

        async def live_strict_fingerprints(
            candidates: list[UpstreamAccountConfig],
        ) -> dict[int, str]:
            live_remote_by_id = remote_by_id
            if live_remote_by_id is None:
                live_remote_by_id = {
                    account_id: account
                    for account in await self.accounts._remote_accounts()
                    if (account_id := self.accounts._numeric_remote_id(account)) is not None
                }
            fingerprints: dict[int, str] = {}
            for config in candidates:
                remote = live_remote_by_id.get(config.sub2api_account_id)
                if remote is None or self.accounts._config_binding_status(remote, config) != "bound":
                    continue
                has_stored_key = bool(decrypt_text(config.encrypted_api_key))
                requires_remote_origin = (
                    not config.channel_auto_assign_disabled or not has_stored_key
                )
                if requires_remote_origin:
                    if _remote_base_url(remote) != channel_base_url:
                        continue
                    fingerprint = self.accounts._remote_identity_fingerprint(remote)
                else:
                    # A manually pinned channel with an explicitly retained or
                    # supplied key is bound to the local channel origin, not the
                    # remote metadata endpoint. Stable account identity still
                    # has to match.
                    fingerprint = self.accounts._require_remote_binding_fingerprint(remote)
                fingerprints[config.sub2api_account_id] = fingerprint
            return fingerprints

        async def reload_current(
            candidates: list[UpstreamAccountConfig],
            *,
            expected_strict_fingerprints: dict[int, str] | None = None,
        ) -> tuple[list[UpstreamAccountConfig], dict[int, str]]:
            candidate_ids = [config.id for config in candidates]
            if not candidate_ids:
                return [], {}
            current_result = await db.execute(
                select(UpstreamAccountConfig)
                .where(UpstreamAccountConfig.id.in_(candidate_ids))
                .execution_options(populate_existing=True)
            )
            current = [
                config
                for config in current_result.scalars().all()
                if (
                    config.channel_id == channel_id
                    and not config.api_key_origin_rebind_required
                )
            ]
            strict_fingerprints = await live_strict_fingerprints(current)
            if expected_strict_fingerprints is not None:
                strict_fingerprints = {
                    account_id: fingerprint
                    for account_id, fingerprint in strict_fingerprints.items()
                    if fingerprint == expected_strict_fingerprints.get(account_id)
                }
            current = [
                config
                for config in current
                if config.sub2api_account_id in strict_fingerprints
            ]
            return current, strict_fingerprints

        configs[:], export_started_strict_fingerprints = await reload_current(configs)
        missing_account_ids: list[int] = []
        for config in configs:
            api_key = decrypt_text(config.encrypted_api_key)
            if not api_key:
                missing_account_ids.append(config.sub2api_account_id)
        if not missing_account_ids:
            configs[:], _strict_fingerprints = await reload_current(
                configs,
                expected_strict_fingerprints=export_started_strict_fingerprints,
            )
            return {
                config.sub2api_account_id: api_key
                for config in configs
                if (
                    not config.api_key_origin_rebind_required
                    and (api_key := decrypt_text(config.encrypted_api_key))
                )
            }
        export_started_fingerprints = {
            config.sub2api_account_id: config.remote_identity_fingerprint.strip().lower()
            for config in configs
            if config.remote_identity_fingerprint
        }
        try:
            exported: dict[int, str] = {}
            for start in range(0, len(missing_account_ids), API_KEY_EXPORT_BATCH_SIZE):
                batch = missing_account_ids[start : start + API_KEY_EXPORT_BATCH_SIZE]
                exported.update(await self.sub2api.export_api_key_secrets(batch))
        except Exception:
            configs[:], _strict_fingerprints = await reload_current(
                configs,
                expected_strict_fingerprints=export_started_strict_fingerprints,
            )
            return {
                config.sub2api_account_id: api_key
                for config in configs
                if (
                    not config.api_key_origin_rebind_required
                    and (api_key := decrypt_text(config.encrypted_api_key))
                )
            }
        configs[:], checked_strict_fingerprints = await reload_current(
            configs,
            expected_strict_fingerprints=export_started_strict_fingerprints,
        )
        checked_fingerprints = {
            config.sub2api_account_id: config.remote_identity_fingerprint.strip().lower()
            for config in configs
            if (
                config.remote_identity_fingerprint
                and config.remote_identity_fingerprint.strip().lower()
                == export_started_fingerprints.get(config.sub2api_account_id)
            )
        }
        currently_bound_ids = set(checked_fingerprints)
        missing_account_ids = [
            account_id for account_id in missing_account_ids if account_id in currently_bound_ids
        ]
        exported = {
            account_id: api_key
            for account_id, api_key in exported.items()
            if account_id in currently_bound_ids
        }
        settings = get_settings()
        can_persist = not (
            settings.app_env == "production"
            and settings.app_encryption_key.strip() == DEFAULT_ENCRYPTION_KEY
        )
        persisted = False
        if can_persist:
            try:
                for account_id in missing_account_ids:
                    api_key = exported.get(account_id)
                    if not api_key:
                        continue
                    result = await db.execute(
                        update(UpstreamAccountConfig)
                        .where(
                            UpstreamAccountConfig.sub2api_account_id == account_id,
                            UpstreamAccountConfig.channel_id == channel_id,
                            UpstreamAccountConfig.encrypted_api_key.is_(None),
                            UpstreamAccountConfig.remote_identity_fingerprint
                            == checked_fingerprints[account_id],
                            UpstreamAccountConfig.api_key_origin_rebind_required.is_(False),
                        )
                        .values(encrypted_api_key=encrypt_text(api_key))
                        .execution_options(synchronize_session=False)
                    )
                    persisted = persisted or bool(result.rowcount)
                if persisted:
                    await db.commit()
            except Exception:
                await db.rollback()
                raise UpstreamAccountServiceError(
                    "Could not securely save imported API key credentials.",
                    status_code=503,
                ) from None

        # Re-read both the persistent identity binding and the live remote
        # identity. An export can only be used for the fingerprint checked after
        # that export; a concurrent identity or origin rebind wins.
        configs[:], _final_strict_fingerprints = await reload_current(
            configs,
            expected_strict_fingerprints=checked_strict_fingerprints,
        )
        account_api_keys = {}
        for config in configs:
            checked_fingerprint = checked_fingerprints.get(config.sub2api_account_id)
            current_fingerprint = (
                config.remote_identity_fingerprint or ""
            ).strip().lower()
            if (
                config.api_key_origin_rebind_required
                or not checked_fingerprint
                or current_fingerprint != checked_fingerprint
            ):
                continue
            api_key = decrypt_text(config.encrypted_api_key)
            if not api_key:
                api_key = exported.get(config.sub2api_account_id)
            if api_key:
                account_api_keys[config.sub2api_account_id] = api_key
        return account_api_keys

    @staticmethod
    def _account_rate_input_snapshot(config: UpstreamAccountConfig) -> tuple[Any, ...]:
        return (
            config.channel_id,
            config.remote_identity_fingerprint,
            config.api_key_origin_rebind_required,
            config.encrypted_api_key,
            config.selected_group_id,
            config.selected_group_name,
            config.manual_group_multiplier,
            config.manual_recharge_multiplier,
        )

    async def _sync_inventory(
        self,
        db: AsyncSession,
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, UpstreamAccountConfig],
        dict[int, UpstreamChannel],
        bool,
    ]:
        # Production runs one backend process. Serialize the read-create-commit
        # inventory transaction so overlapping page loads cannot race unique
        # channel/account inserts and escape as an unhandled IntegrityError.
        async with self._inventory_lock:
            return await self._sync_inventory_unlocked(db)

    async def sync_inventory(
        self,
        db: AsyncSession,
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, UpstreamAccountConfig],
        dict[int, UpstreamChannel],
        bool,
    ]:
        """Synchronize the local API-key/channel inventory without remote writes."""

        return await self._sync_inventory(db)

    async def _sync_inventory_unlocked(
        self,
        db: AsyncSession,
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, UpstreamAccountConfig],
        dict[int, UpstreamChannel],
        bool,
    ]:
        remote_by_id = {
            account_id: account
            for account in await self.accounts._remote_accounts()
            if (account_id := self.accounts._numeric_remote_id(account)) is not None
        }
        config_ids_result = await db.execute(
            select(UpstreamAccountConfig.sub2api_account_id)
        )
        lock_account_ids = sorted(
            set(remote_by_id).union(int(value) for value in config_ids_result.scalars().all())
        )
        await db.rollback()
        db.expire_all()
        account_locks: list[asyncio.Lock] = []
        try:
            for account_id in lock_account_ids:
                lock = await self.accounts._lock_for(account_id)
                await lock.acquire()
                account_locks.append(lock)
            return await self._sync_inventory_rows(db, remote_by_id)
        finally:
            for lock in reversed(account_locks):
                lock.release()

    async def _sync_inventory_rows(
        self,
        db: AsyncSession,
        remote_by_id: dict[int, dict[str, Any]],
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, UpstreamAccountConfig],
        dict[int, UpstreamChannel],
        bool,
    ]:
        channel_result = await db.execute(select(UpstreamChannel))
        channels = list(channel_result.scalars().all())
        channels_by_url = {item.canonical_base_url: item for item in channels}

        config_result = await db.execute(select(UpstreamAccountConfig))
        configs = {item.sub2api_account_id: item for item in config_result.scalars().all()}
        changed = False
        priority_membership_changed = False

        for account_id, config in configs.items():
            if account_id not in remote_by_id and config.priority_interval_id is not None:
                config.priority_interval_id = None
                config.desired_priority = None
                config.priority_sync_status = "unassigned"
                config.priority_sync_error = None
                changed = True
                priority_membership_changed = True

        for account_id in sorted(remote_by_id):
            remote = remote_by_id[account_id]
            config = configs.get(account_id)
            stored_channel = (
                next((item for item in channels if item.id == config.channel_id), None)
                if config is not None and config.channel_id is not None
                else None
            )
            name_only_binding_change = bool(
                config is not None
                and self.accounts._config_binding_status(remote, config) == "mismatch"
                and self.accounts._config_binding_differs_only_by_remote_name(
                    remote,
                    config,
                    extra_secrets=self._known_secrets(config, stored_channel),
                )
            )
            if (
                config is not None
                and self.accounts._config_binding_status(remote, config) != "bound"
                and not name_only_binding_change
            ):
                if any(
                    value is not None
                    for value in (
                        config.upstream_usage_amount,
                        config.upstream_usage_unit,
                        config.upstream_usage_checked_at,
                    )
                ):
                    self.accounts.clear_upstream_usage_state(config)
                    changed = True
                if config.priority_interval_id is not None:
                    config.priority_interval_id = None
                    config.desired_priority = None
                    config.priority_sync_status = "unassigned"
                    config.priority_sync_error = None
                    changed = True
                    priority_membership_changed = True
                continue
            remote_url = _remote_base_url(remote)
            configured_url: str | None = None
            if config is not None and config.base_url:
                try:
                    configured_url = canonicalize_upstream_url(config.base_url)
                except ValueError:
                    configured_url = None
            auto_assign_allowed = config is None or not config.channel_auto_assign_disabled
            raw_url = remote_url if auto_assign_allowed and remote_url else configured_url or remote_url
            canonical_url: str | None = None
            if raw_url:
                try:
                    canonical_url = canonicalize_upstream_url(raw_url)
                except ValueError:
                    canonical_url = None

            channel = stored_channel
            endpoint_changed = bool(
                config is not None
                and auto_assign_allowed
                and remote_url
                and remote_url != configured_url
            )
            if endpoint_changed:
                channel = None
            if channel is None and canonical_url is not None and auto_assign_allowed:
                channel = channels_by_url.get(canonical_url)
                if channel is None:
                    channel = UpstreamChannel(
                        display_name=self._default_name(canonical_url),
                        canonical_base_url=canonical_url,
                        upstream_type="auto",
                        group_options=[],
                        recharge_multiplier_status="not_discovered",
                        balance_status="not_checked",
                    )
                    db.add(channel)
                    await db.flush()
                    channels.append(channel)
                    channels_by_url[canonical_url] = channel
                    changed = True

            if config is None:
                config = self.accounts._new_config(
                    remote,
                    account_id,
                    secrets=self._known_secrets(None, channel),
                )
                config.base_url = canonical_url or config.base_url
                config.channel_id = channel.id if channel is not None else None
                db.add(config)
                configs[account_id] = config
                changed = True
            else:
                known_secrets = self._known_secrets(config, channel)
                remote_name = (
                    _safe_text(
                        self.accounts._remote_name(
                            remote,
                            account_id,
                            secrets=known_secrets,
                        ),
                        secrets=known_secrets,
                        limit=200,
                    )
                    or f"Account #{account_id}"
                )
                if name_only_binding_change:
                    config.remote_identity_fingerprint = (
                        self.accounts._require_remote_binding_fingerprint(remote)
                    )
                    changed = True
                if config.remote_name != remote_name:
                    config.remote_name = remote_name
                    changed = True
                if endpoint_changed and channel is not None:
                    priority_membership_changed = (
                        priority_membership_changed
                        or config.priority_interval_id is not None
                    )
                    config.channel_id = channel.id
                    config.base_url = canonical_url
                    config.upstream_type = channel.upstream_type
                    config.resolved_upstream_type = channel.resolved_upstream_type
                    config.upstream_user_id = channel.upstream_user_id
                    config.encrypted_access_token = None
                    config.encrypted_api_key = None
                    config.api_key_origin_rebind_required = True
                    config.selected_group_id = None
                    config.selected_group_name = None
                    config.manual_group_multiplier = None
                    config.manual_recharge_multiplier = channel.manual_recharge_multiplier
                    config.group_options = channel.group_options
                    config.last_applied_at = None
                    self._invalidate_account(config)
                    config.last_error = "The upstream endpoint changed; rediscovery is required."
                    changed = True
                elif (
                    config.channel_id is None
                    and channel is not None
                    and not config.channel_auto_assign_disabled
                ):
                    config.channel_id = channel.id
                    changed = True
                config.remote_platform = _safe_text(self.sub2api.account_platform(remote), limit=64)
                config.remote_account_type = _safe_text(self.sub2api.account_type(remote), limit=32)
                current_rate = self.accounts._remote_current_rate(remote)
                if current_rate is not None:
                    config.current_rate = current_rate

        await db.commit()

        channel_result = await db.execute(select(UpstreamChannel))
        channels_by_id = {item.id: item for item in channel_result.scalars().all()}
        config_result = await db.execute(select(UpstreamAccountConfig))
        configs = {item.sub2api_account_id: item for item in config_result.scalars().all()}
        return remote_by_id, configs, channels_by_id, priority_membership_changed

    async def _local_recharge(self) -> tuple[float | None, str | None, str]:
        try:
            credit_per_cny, field_present = await self.sub2api.get_payment_balance_recharge_multiplier_info()
            parsed = _decimal_multiplier(credit_per_cny)
            if parsed is None:
                return None, None, "error"
            try:
                cost_per_usd = _decimal_multiplier(Decimal("1") / parsed)
            except DecimalException:
                cost_per_usd = None
            if cost_per_usd is None:
                return None, None, "error"
            return (
                float(cost_per_usd),
                "sub2api_settings" if field_present else "default",
                "ok" if field_present else "default_missing",
            )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            return None, None, "error"

    def _project_account(
        self,
        remote: dict[str, Any],
        config: UpstreamAccountConfig,
        channel: UpstreamChannel | None,
        *,
        local_recharge: float | None,
        local_source: str | None,
        local_status: str,
        priority_interval_name: str | None = None,
    ) -> UpstreamAccountOut:
        projected_target_rate = config.target_rate
        if channel is not None:
            group = _decimal_multiplier(config.effective_group_multiplier)
            recharge = _decimal_multiplier(channel.effective_recharge_multiplier)
            local = _decimal_multiplier(local_recharge)
            target = (
                _calculate_target_rate(group, recharge, local)
                if group and recharge and local
                else None
            )
            projected_target_rate = float(target) if target is not None else None
        base = self.accounts._build_out(
            remote,
            config,
            extra_secrets=self._known_secrets(config, channel),
            channel_name=channel.display_name if channel is not None else None,
            priority_interval_name=priority_interval_name,
        )
        projected_would_change: bool | None = None
        if base.current_rate is not None and projected_target_rate is not None:
            current = _decimal_multiplier(base.current_rate, allow_zero=True)
            target = _decimal_multiplier(projected_target_rate, allow_zero=True)
            if current is not None and target is not None:
                projected_would_change = _quantize_rate(current) != _quantize_rate(target)
        health_update = {
            "upstream_key_status": config.upstream_key_status or "not_checked",
            "upstream_group_status": config.upstream_group_status or "not_checked",
            "upstream_health_invalid_count": min(
                2,
                max(0, int(config.upstream_health_invalid_count or 0)),
            ),
            "upstream_key_checked_at": config.upstream_key_checked_at,
            "upstream_group_checked_at": config.upstream_group_checked_at,
        }
        if channel is None:
            return base.model_copy(
                update={
                    **health_update,
                    "channel_id": None,
                    "channel_name": None,
                    "local_recharge_multiplier": local_recharge,
                    "local_recharge_source": local_source,
                    "local_recharge_status": local_status,
                }
            )
        return base.model_copy(
            update={
                **health_update,
                "channel_id": channel.id,
                "channel_name": channel.display_name,
                "base_url": channel.canonical_base_url,
                "upstream_type": channel.upstream_type,
                "resolved_upstream_type": channel.resolved_upstream_type,
                "upstream_user_id": channel.upstream_user_id,
                "access_token_set": bool(channel.encrypted_access_token),
                "manual_recharge_multiplier": channel.manual_recharge_multiplier,
                "group_options": self._group_options_out(channel),
                "discovered_recharge_multiplier": channel.discovered_recharge_multiplier,
                "effective_recharge_multiplier": channel.effective_recharge_multiplier,
                "recharge_multiplier_source": channel.recharge_multiplier_source,
                "recharge_multiplier_status": channel.recharge_multiplier_status,
                "local_recharge_multiplier": local_recharge,
                "local_recharge_source": local_source,
                "local_recharge_status": local_status,
                "target_rate": projected_target_rate,
                "would_change": projected_would_change,
            }
        )

    async def _inventory_snapshot(
        self,
        db: AsyncSession,
        *,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, UpstreamAccountConfig],
        dict[int, UpstreamChannel],
    ]:
        if remote_by_id is None:
            remote_by_id = {
                account_id: account
                for account in await self.accounts._remote_accounts()
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        with db.no_autoflush:
            config_result = await db.execute(select(UpstreamAccountConfig))
            configs = {
                item.sub2api_account_id: item for item in config_result.scalars().all()
            }
            channel_result = await db.execute(select(UpstreamChannel))
            channels_by_id = {item.id: item for item in channel_result.scalars().all()}
        return (
            {
                account_id: remote
                for account_id, remote in remote_by_id.items()
                if account_id in configs
            },
            configs,
            channels_by_id,
        )

    @staticmethod
    def _group_options_out(channel: UpstreamChannel) -> list[UpstreamGroupOptionOut]:
        return [UpstreamGroupOptionOut(**item) for item in _sanitize_group_options(channel.group_options)]

    def _channel_out(
        self,
        channel: UpstreamChannel,
        accounts: list[UpstreamAccountOut],
    ) -> UpstreamChannelOut:
        sorted_accounts = sorted(
            accounts,
            key=lambda item: (
                item.composite_multiplier is None,
                item.composite_multiplier or 0,
                item.sub2api_account_id,
            ),
        )
        return UpstreamChannelOut(
            id=channel.id,
            display_name=channel.display_name,
            base_url=channel.canonical_base_url,
            canonical_base_url=channel.canonical_base_url,
            management_base_url=channel.management_base_url,
            upstream_type=channel.upstream_type if channel.upstream_type in {"auto", "newapi", "sub2api"} else "auto",
            probe_enabled=bool(channel.probe_enabled),
            resolved_upstream_type=(
                channel.resolved_upstream_type
                if channel.resolved_upstream_type in {"newapi", "sub2api"}
                else None
            ),
            upstream_user_id=channel.upstream_user_id,
            access_token_set=bool(channel.encrypted_access_token),
            refresh_token_set=bool(channel.encrypted_refresh_token),
            manual_recharge_multiplier=channel.manual_recharge_multiplier,
            group_options=self._group_options_out(channel),
            discovered_recharge_multiplier=channel.discovered_recharge_multiplier,
            effective_recharge_multiplier=channel.effective_recharge_multiplier,
            recharge_multiplier_source=channel.recharge_multiplier_source,
            recharge_multiplier_status=channel.recharge_multiplier_status,
            balance_remaining=channel.balance_remaining,
            balance_total=channel.balance_total,
            balance_used=channel.balance_used,
            balance_unit=channel.balance_unit,
            balance_status=channel.balance_status,
            balance_message=channel.balance_message,
            balance_checked_at=channel.balance_checked_at,
            today_balance_used=channel.today_balance_used,
            today_balance_unit=channel.today_balance_unit,
            today_balance_status=channel.today_balance_status,
            today_balance_checked_at=channel.today_balance_checked_at,
            last_error=channel.last_error,
            last_discovered_at=channel.last_discovered_at,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
            account_count=len(sorted_accounts),
            accounts=sorted_accounts,
        )

    async def overview(
        self,
        db: AsyncSession,
        *,
        sync_inventory: bool = True,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> UpstreamOverviewOut:
        if sync_inventory:
            (
                remote_by_id,
                configs,
                channels_by_id,
                priority_membership_changed,
            ) = await self.sync_inventory(db)
            if priority_membership_changed:
                await self._rebalance_priorities_best_effort(db)
        else:
            remote_by_id, configs, channels_by_id = await self._inventory_snapshot(
                db,
                remote_by_id=remote_by_id,
            )
        local_recharge, local_source, local_status = await self._local_recharge()
        priority_intervals = await self._priority_service().list_intervals(db)
        priority_interval_names = {item.id: item.name for item in priority_intervals}
        grouped: dict[int, list[UpstreamAccountOut]] = {channel_id: [] for channel_id in channels_by_id}
        unassigned: list[UpstreamAccountOut] = []
        for account_id in sorted(remote_by_id):
            config = configs[account_id]
            if self.accounts._config_binding_status(remote_by_id[account_id], config) != "bound":
                channel = channels_by_id.get(config.channel_id) if config.channel_id is not None else None
                account = self.accounts._build_out(
                    remote_by_id[account_id],
                    config,
                    extra_secrets=self._known_secrets(config, channel),
                    channel_name=channel.display_name if channel is not None else None,
                    priority_interval_name=priority_interval_names.get(
                        config.priority_interval_id or 0
                    ),
                )
                if channel is None:
                    unassigned.append(account)
                else:
                    grouped[channel.id].append(
                        account.model_copy(
                            update={
                                "channel_id": channel.id,
                                "channel_name": channel.display_name,
                            }
                        )
                    )
                continue
            channel = channels_by_id.get(config.channel_id) if config.channel_id is not None else None
            account = self._project_account(
                remote_by_id[account_id],
                config,
                channel,
                local_recharge=local_recharge,
                local_source=local_source,
                local_status=local_status,
                priority_interval_name=priority_interval_names.get(
                    config.priority_interval_id or 0
                ),
            )
            if channel is None:
                unassigned.append(account)
            else:
                grouped[channel.id].append(account)
        channels = [
            self._channel_out(channel, grouped.get(channel.id, []))
            for channel in sorted(channels_by_id.values(), key=lambda item: (item.display_name.casefold(), item.id))
            if grouped.get(channel.id)
        ]
        return UpstreamOverviewOut(
            local_recharge_multiplier=local_recharge,
            local_recharge_source=local_source,
            local_recharge_status=local_status,
            channels=channels,
            unassigned_accounts=sorted(
                unassigned,
                key=lambda item: (
                    item.composite_multiplier is None,
                    item.composite_multiplier or 0,
                    item.sub2api_account_id,
                ),
            ),
            priority_intervals=priority_intervals,
        )

    async def update_channel(
        self,
        db: AsyncSession,
        channel_id: int,
        payload: UpstreamChannelUpdate,
    ) -> UpstreamChannelOut:
        # Channel URL changes and inventory auto-creation share the same unique
        # canonical URL namespace, so their check-and-commit sections must not
        # overlap.
        async with self._inventory_lock:
            await self._update_channel_locked(db, channel_id, payload)
        rate_fields = {
            "base_url",
            "management_base_url",
            "upstream_type",
            "upstream_user_id",
            "access_token",
            "clear_access_token",
            "refresh_token",
            "clear_refresh_token",
            "manual_recharge_multiplier",
        }
        if payload.model_fields_set & rate_fields:
            await self._rebalance_priorities_best_effort(db)
        overview = await self.overview(db)
        result = next((item for item in overview.channels if item.id == channel_id), None)
        if result is None:
            raise UpstreamAccountServiceError(
                "The upstream channel has no API key accounts.",
                status_code=409,
            )
        return result

    async def _update_channel_locked(
        self,
        db: AsyncSession,
        channel_id: int,
        payload: UpstreamChannelUpdate,
    ) -> None:
        lock = await self._lock_for(channel_id)
        async with lock:
            self._ensure_secret_storage_ready(payload)
            channel = await self._load_channel(db, channel_id)
            fields = payload.model_fields_set
            current_token = decrypt_text(channel.encrypted_access_token)
            current_refresh_token = decrypt_text(channel.encrypted_refresh_token)
            previous_canonical_origin = upstream_url_origin(channel.canonical_base_url)
            previous_management_origin = upstream_url_origin(
                channel.management_base_url or channel.canonical_base_url
            )
            identity_changed = False
            base_url_changed = False

            if "base_url" in fields:
                if payload.base_url is None:
                    raise UpstreamAccountServiceError(
                        "The upstream channel URL is required.", status_code=422
                    )
                canonical_url = canonicalize_upstream_url(payload.base_url)
                collision = await db.execute(
                    select(UpstreamChannel).where(
                        UpstreamChannel.canonical_base_url == canonical_url,
                        UpstreamChannel.id != channel_id,
                    )
                )
                if collision.scalar_one_or_none() is not None:
                    raise UpstreamAccountServiceError(
                        "Another upstream channel already uses this URL.", status_code=409
                    )
                base_url_changed = canonical_url != channel.canonical_base_url
                identity_changed = base_url_changed
                channel.canonical_base_url = canonical_url
            if "management_base_url" in fields:
                management_base_url = payload.management_base_url
                identity_changed = identity_changed or management_base_url != channel.management_base_url
                channel.management_base_url = management_base_url
            if "display_name" in fields:
                channel.display_name = payload.display_name or self._default_name(channel.canonical_base_url)
            if "upstream_type" in fields:
                identity_changed = identity_changed or payload.upstream_type != channel.upstream_type
                channel.upstream_type = payload.upstream_type
            if "probe_enabled" in fields and payload.probe_enabled is not None:
                channel.probe_enabled = payload.probe_enabled
            if "upstream_user_id" in fields:
                identity_changed = identity_changed or payload.upstream_user_id != channel.upstream_user_id
                channel.upstream_user_id = payload.upstream_user_id
            if payload.clear_access_token:
                identity_changed = identity_changed or current_token is not None
                channel.encrypted_access_token = None
            elif payload.access_token:
                identity_changed = identity_changed or payload.access_token != current_token
                channel.encrypted_access_token = encrypt_text(payload.access_token)
            if payload.clear_refresh_token:
                identity_changed = identity_changed or current_refresh_token is not None
                channel.encrypted_refresh_token = None
            elif payload.refresh_token:
                identity_changed = identity_changed or payload.refresh_token != current_refresh_token
                channel.encrypted_refresh_token = encrypt_text(payload.refresh_token)
            if "manual_recharge_multiplier" in fields:
                channel.manual_recharge_multiplier = payload.manual_recharge_multiplier

            next_canonical_origin = upstream_url_origin(channel.canonical_base_url)
            next_management_origin = upstream_url_origin(
                channel.management_base_url or channel.canonical_base_url
            )
            canonical_origin_changed = next_canonical_origin != previous_canonical_origin
            if (
                (
                    canonical_origin_changed
                    or next_management_origin != previous_management_origin
                )
                and not payload.confirm_credential_rebind
            ):
                raise UpstreamAccountServiceError(
                    "Changing the upstream origin requires explicit credential rebind confirmation.",
                    status_code=409,
                )

            if identity_changed:
                self._invalidate_channel(channel)
            account_values: dict[str, Any] = {
                "base_url": channel.canonical_base_url,
                "upstream_type": channel.upstream_type,
                "upstream_user_id": channel.upstream_user_id,
                "encrypted_access_token": channel.encrypted_access_token,
                "manual_recharge_multiplier": channel.manual_recharge_multiplier,
            }
            if identity_changed or "manual_recharge_multiplier" in fields:
                account_values.update(
                    discovered_group_multiplier=None,
                    effective_group_multiplier=None,
                    group_multiplier_source=None,
                    group_multiplier_status="not_discovered",
                    target_rate=None,
                    desired_priority=None,
                    last_discovered_at=None,
                )
            if identity_changed:
                account_values.update(
                    upstream_usage_amount=None,
                    upstream_usage_unit=None,
                    upstream_usage_checked_at=None,
                )
            if base_url_changed:
                account_values["channel_auto_assign_disabled"] = True
            if canonical_origin_changed and payload.confirm_credential_rebind:
                account_values["api_key_origin_rebind_required"] = False
            bound_configs = await self._bound_configs(db, channel_id)
            if bound_configs:
                await db.execute(
                    update(UpstreamAccountConfig)
                    .where(
                        UpstreamAccountConfig.id.in_(
                            [config.id for config in bound_configs]
                        )
                    )
                    .values(**account_values)
                    .execution_options(synchronize_session=False)
                )
                if identity_changed or "manual_recharge_multiplier" in fields:
                    await db.execute(
                        update(UpstreamAccountConfig)
                        .where(
                            UpstreamAccountConfig.id.in_(
                                [config.id for config in bound_configs]
                            ),
                            UpstreamAccountConfig.priority_interval_id.is_not(None),
                        )
                        .values(
                            desired_priority=None,
                            priority_sync_status="multiplier_unavailable",
                            priority_sync_error=None,
                        )
                        .execution_options(synchronize_session=False)
                    )

            await db.commit()
            db.expire_all()

    @staticmethod
    def _selected_group(config: UpstreamAccountConfig, groups: list[dict[str, Any]]) -> dict[str, Any] | None:
        selected_id = (config.selected_group_id or "").strip().casefold()
        selected_name = (config.selected_group_name or "").strip().casefold()
        for group in groups:
            group_id = str(group.get("id") or "").strip().casefold()
            group_name = str(group.get("name") or "").strip().casefold()
            if selected_id and group_id == selected_id:
                return group
            if selected_name and group_name == selected_name:
                return group
        return None

    def _apply_discovery_to_channel(self, channel: UpstreamChannel, result: Any) -> bool:
        now = _utcnow()
        status = str(_value(result, "status") or "error").strip().lower()
        if status == "insecure_url":
            raise UpstreamAccountServiceError(
                "Credentials may only be sent to an HTTPS upstream URL.", status_code=422
            )
        if status != "ok":
            channel.discovered_recharge_multiplier = None
            manual_recharge = _decimal_multiplier(channel.manual_recharge_multiplier)
            if manual_recharge is not None:
                channel.effective_recharge_multiplier = float(manual_recharge)
                channel.recharge_multiplier_source = "manual"
                channel.recharge_multiplier_status = "fallback_manual"
            else:
                channel.effective_recharge_multiplier = None
                channel.recharge_multiplier_source = None
                channel.recharge_multiplier_status = "discovery_failed"
            channel.balance_status = str(_value(result, "balance_status") or "error").strip().lower()
            channel.balance_message = _safe_text(
                _value(result, "balance_message"), limit=300
            ) or "Unable to read the upstream channel."
            channel.today_balance_status = "error"
            channel.last_error = "Upstream channel discovery failed."
            channel.last_discovered_at = now
            return False

        resolved = str(_value(result, "upstream_type") or "").strip().lower()
        if resolved in {"newapi", "sub2api"}:
            channel.resolved_upstream_type = resolved
        channel.group_options = _sanitize_group_options(_value(result, "groups") or [])

        discovered_recharge = _decimal_multiplier(_value(result, "discovered_recharge_multiplier"))
        recharge_probe_status = str(_value(result, "recharge_discovery_status") or "unknown").lower()
        manual_recharge = _decimal_multiplier(channel.manual_recharge_multiplier)
        if discovered_recharge is not None:
            channel.discovered_recharge_multiplier = float(discovered_recharge)
            channel.effective_recharge_multiplier = float(discovered_recharge)
            channel.recharge_multiplier_source = _safe_text(
                _value(result, "discovered_recharge_multiplier_source"), limit=128
            ) or "auto"
            channel.recharge_multiplier_status = "ok"
        elif manual_recharge is not None:
            channel.discovered_recharge_multiplier = None
            channel.effective_recharge_multiplier = float(manual_recharge)
            channel.recharge_multiplier_source = "manual"
            channel.recharge_multiplier_status = "fallback_manual"
        elif recharge_probe_status == "missing":
            channel.discovered_recharge_multiplier = None
            channel.effective_recharge_multiplier = 1.0
            channel.recharge_multiplier_source = "default"
            channel.recharge_multiplier_status = "default_missing"
        else:
            channel.discovered_recharge_multiplier = None
            channel.effective_recharge_multiplier = None
            channel.recharge_multiplier_source = None
            channel.recharge_multiplier_status = "discovery_failed"

        balance_status = str(_value(result, "balance_status") or "not_checked").strip().lower()
        channel.balance_status = balance_status
        channel.balance_message = _safe_text(_value(result, "balance_message"), limit=300)
        if balance_status in {"ok", "success", "available"}:
            for field in ("balance_remaining", "balance_total", "balance_used"):
                value = _value(result, field)
                if value is not None:
                    parsed = _balance_number(value)
                    if parsed is not None:
                        setattr(channel, field, parsed)
            channel.balance_unit = _safe_text(_value(result, "balance_unit"), limit=32) or "USD"
            channel.balance_checked_at = now
        today_status = str(_value(result, "today_balance_status") or "unsupported").strip().lower()
        channel.today_balance_status = today_status
        channel.today_balance_used = None
        channel.today_balance_unit = None
        channel.today_balance_checked_at = None
        if today_status == "ok":
            today_used = _balance_number(_value(result, "today_balance_used"))
            if today_used is not None and today_used >= 0:
                channel.today_balance_used = today_used
                channel.today_balance_unit = _safe_text(
                    _value(result, "today_balance_unit"), limit=32
                ) or "USD"
                channel.today_balance_checked_at = now
        channel.last_error = None if balance_status in {"ok", "success", "available"} else channel.balance_message
        channel.last_discovered_at = now
        return True

    async def _apply_api_key_balance_fallback(
        self,
        channel: UpstreamChannel,
        configs: list[UpstreamAccountConfig],
    ) -> bool:
        if channel.balance_status in {"ok", "success", "available"}:
            return True
        for config in sorted(configs, key=lambda item: item.sub2api_account_id)[:10]:
            try:
                result = await self.sub2api.get_account_balance(config.sub2api_account_id)
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                continue
            if not isinstance(result, dict):
                continue
            status = str(result.get("status") or "").strip().lower()
            if status not in {"ok", "success", "available"}:
                continue
            remaining = _balance_number(
                result.get("remaining", result.get("balance_remaining", result.get("balance")))
            )
            if remaining is None:
                continue
            channel.balance_remaining = remaining
            channel.balance_total = _balance_number(
                result.get("total", result.get("balance_total"))
            )
            channel.balance_used = _balance_number(
                result.get("used", result.get("balance_used"))
            )
            channel.balance_unit = _safe_text(result.get("unit"), limit=32) or "USD"
            channel.balance_status = "ok"
            channel.balance_message = "API Key balance reported through the local sub2api account."
            channel.balance_checked_at = _utcnow()
            return True
        return False

    def _derive_account(
        self,
        config: UpstreamAccountConfig,
        channel: UpstreamChannel,
        *,
        local_recharge: float | None,
        local_source: str | None,
        local_status: str,
    ) -> None:
        groups = _sanitize_group_options(channel.group_options)
        matched = self._selected_group(config, groups)
        manual_group = _decimal_multiplier(config.manual_group_multiplier)
        if matched is not None:
            group = _decimal_multiplier(matched.get("multiplier"))
            config.discovered_group_multiplier = float(group) if group is not None else None
            config.effective_group_multiplier = float(group) if group is not None else None
            config.group_multiplier_source = _safe_text(matched.get("source"), limit=128) or "upstream"
            config.group_multiplier_status = "ok" if group is not None else "invalid"
            config.selected_group_id = str(matched.get("id"))
            config.selected_group_name = str(matched.get("name"))
        elif manual_group is not None:
            config.discovered_group_multiplier = None
            config.effective_group_multiplier = float(manual_group)
            config.group_multiplier_source = "manual"
            config.group_multiplier_status = "fallback_manual"
        else:
            config.discovered_group_multiplier = None
            config.effective_group_multiplier = None
            config.group_multiplier_source = None
            config.group_multiplier_status = "group_selection_missing" if groups else "unavailable"

        group = _decimal_multiplier(config.effective_group_multiplier)
        recharge = _decimal_multiplier(channel.effective_recharge_multiplier)
        local = _decimal_multiplier(local_recharge)
        target = (
            _calculate_target_rate(group, recharge, local)
            if group and recharge and local
            else None
        )
        config.target_rate = float(target) if target is not None else None
        config.local_recharge_multiplier = local_recharge
        config.local_recharge_source = local_source
        config.local_recharge_status = local_status
        config.last_discovered_at = channel.last_discovered_at
        config.last_error = None if target is not None else "Select an upstream group before applying a billing rate."

        # Keep the compatibility projection populated during the transition.
        config.base_url = channel.canonical_base_url
        config.upstream_type = channel.upstream_type
        config.resolved_upstream_type = channel.resolved_upstream_type
        config.upstream_user_id = channel.upstream_user_id
        config.encrypted_access_token = channel.encrypted_access_token
        config.manual_recharge_multiplier = channel.manual_recharge_multiplier
        config.group_options = channel.group_options
        config.discovered_recharge_multiplier = channel.discovered_recharge_multiplier
        config.effective_recharge_multiplier = channel.effective_recharge_multiplier
        config.recharge_multiplier_source = channel.recharge_multiplier_source
        config.recharge_multiplier_status = channel.recharge_multiplier_status
        config.balance_remaining = channel.balance_remaining
        config.balance_total = channel.balance_total
        config.balance_used = channel.balance_used
        config.balance_unit = channel.balance_unit
        config.balance_status = channel.balance_status
        config.balance_message = channel.balance_message
        config.balance_checked_at = channel.balance_checked_at

    @staticmethod
    def _same_multiplier(left: Any, right: Any) -> bool:
        left_value = _decimal_multiplier(left, allow_zero=True)
        right_value = _decimal_multiplier(right, allow_zero=True)
        if left_value is None or right_value is None:
            return left_value is None and right_value is None
        return _quantize_rate(left_value) == _quantize_rate(right_value)

    @staticmethod
    def _upstream_multiplier(
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
        if not value.is_finite() or value <= 0:
            return None
        return float(value)

    @staticmethod
    async def _automatic_rate_write_allowed() -> bool:
        try:
            runtime = get_runtime_config_service()
            return bool(
                await runtime.get_upstream_rate_sync_enabled()
                and not await runtime.get_automation_paused()
            )
        except Exception:
            return False

    @staticmethod
    def _synchronized_upstream_state(result: Any, account_id: int) -> Any | None:
        states = _value(result, "account_upstream_states")
        if not isinstance(states, dict):
            return None
        state = states.get(account_id)
        return state if state is not None else states.get(str(account_id))

    async def _reconcile_account_rate(
        self,
        db: AsyncSession,
        config: UpstreamAccountConfig,
        channel: UpstreamChannel,
        *,
        previous_group_multiplier: float | None,
        previous_upstream_recharge_multiplier: float | None,
        previous_local_recharge_multiplier: float | None,
        previous_target_rate: float | None,
        sync_enabled: bool,
        health_transition: UpstreamHealthTransition,
        old_remote_schedulable: bool | None,
        new_remote_schedulable: bool | None,
        health_action_status: str | None,
        health_safe_error: str | None,
        current_remote: dict[str, Any],
    ) -> None:
        group_changed = not self._same_multiplier(
            previous_group_multiplier,
            config.effective_group_multiplier,
        )
        upstream_recharge_changed = not self._same_multiplier(
            previous_upstream_recharge_multiplier,
            channel.effective_recharge_multiplier,
        )
        local_recharge_changed = not self._same_multiplier(
            previous_local_recharge_multiplier,
            config.local_recharge_multiplier,
        )
        target_changed = not self._same_multiplier(previous_target_rate, config.target_rate)
        rate_inputs_changed = (
            group_changed or upstream_recharge_changed or local_recharge_changed
        )
        key_status_changed = (
            health_transition.old_key_status != health_transition.new_key_status
        )
        group_status_changed = (
            health_transition.old_group_status != health_transition.new_group_status
        )
        schedulable_changed = old_remote_schedulable != new_remote_schedulable
        upstream_health_invalid = bool(
            health_transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES
            or health_transition.new_group_status in INVALID_UPSTREAM_GROUP_STATUSES
        )
        initial_non_invalid_health = bool(
            health_transition.old_key_status == "not_checked"
            and health_transition.old_group_status == "not_checked"
            and not upstream_health_invalid
        )
        health_change_should_log = bool(
            (key_status_changed or group_status_changed)
            and not initial_non_invalid_health
        )
        old_upstream_multiplier = self._upstream_multiplier(
            previous_group_multiplier,
            previous_upstream_recharge_multiplier,
        )
        new_upstream_multiplier = self._upstream_multiplier(
            config.effective_group_multiplier,
            channel.effective_recharge_multiplier,
        )
        old_current = config.current_rate
        new_current = old_current
        status = health_action_status or "observed"
        safe_error: str | None = health_safe_error
        reason = (
            "upstream_auto_disable"
            if health_action_status is not None
            else "upstream_key_recovered"
            if key_status_changed
            and health_transition.new_key_status == "active"
            and health_transition.old_key_status in INVALID_UPSTREAM_KEY_STATUSES
            else "upstream_key_status_change"
            if key_status_changed
            else "upstream_group_recovered"
            if group_status_changed
            and health_transition.new_group_status == "available"
            and health_transition.old_group_status in INVALID_UPSTREAM_GROUP_STATUSES
            else "upstream_group_status_change"
            if group_status_changed
            else "upstream_group_change"
            if group_changed
            else "upstream_recharge_change"
            if upstream_recharge_changed
            else "local_recharge_change"
            if local_recharge_changed
            else "target_recalculated"
            if target_changed
            else "rate_drift"
        )

        target = _decimal_multiplier(config.target_rate)
        source_is_safe = config.group_multiplier_source in {"upstream_key", "manual"}
        attempted = (
            sync_enabled
            and target is not None
            and source_is_safe
            and not upstream_health_invalid
            and health_action_status is None
        )
        if attempted:
            try:
                if not await self._automatic_rate_write_allowed():
                    attempted = False
                    status = "skipped"
                else:
                    current = self.accounts._remote_current_rate(current_remote)
                    config.current_rate = current
                    old_current = current
                    new_current = current
                    current_decimal = _decimal_multiplier(current, allow_zero=True)
                    if current_decimal is None:
                        raise ValueError("invalid current rate")
                    if _quantize_rate(current_decimal) != _quantize_rate(target):
                        if not await self._automatic_rate_write_allowed():
                            status = "skipped"
                        else:
                            self.accounts._require_config_binding(current_remote, config)
                            await self.sub2api.update_account_rate_multiplier(
                                config.sub2api_account_id,
                                float(_quantize_rate(target)),
                            )
                            readback = await self.sub2api.get_account_current_rate_multiplier(
                                config.sub2api_account_id
                            )
                            readback_decimal = _decimal_multiplier(readback, allow_zero=True)
                            if (
                                readback_decimal is None
                                or _quantize_rate(readback_decimal) != _quantize_rate(target)
                            ):
                                raise ValueError("rate readback mismatch")
                            config.current_rate = readback
                            config.last_applied_at = _utcnow()
                            new_current = readback
                            status = "applied"
                    elif rate_inputs_changed or target_changed:
                        status = "observed"
            except Exception:
                status = "apply_failed"
                safe_error = "Unable to update and verify the sub2api account rate."
                config.last_error = safe_error
        elif target is None and (rate_inputs_changed or target_changed):
            status = "skipped"

        if health_action_status is not None:
            status = health_action_status
            safe_error = health_safe_error

        current_drift = (
            target is not None
            and _decimal_multiplier(old_current, allow_zero=True) is not None
            and not self._same_multiplier(old_current, target)
        )
        if not (
            rate_inputs_changed
            or target_changed
            or (attempted and current_drift)
            or health_change_should_log
            or schedulable_changed
            or health_action_status is not None
        ):
            return
        if status not in {"apply_failed", "disable_failed"} and config.target_rate is not None and not upstream_health_invalid:
            config.last_error = None
        elif health_safe_error is not None:
            config.last_error = health_safe_error
        known_secrets = self._known_secrets(config, channel)
        db.add(
            UpstreamRateChangeLog(
                sub2api_account_id=config.sub2api_account_id,
                account_name=_safe_text(
                    config.remote_name,
                    secrets=known_secrets,
                    limit=200,
                ),
                channel_id=channel.id,
                channel_name=_safe_text(
                    channel.display_name,
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
                old_upstream_multiplier=old_upstream_multiplier,
                new_upstream_multiplier=new_upstream_multiplier,
                old_upstream_recharge_multiplier=previous_upstream_recharge_multiplier,
                new_upstream_recharge_multiplier=channel.effective_recharge_multiplier,
                upstream_recharge_multiplier=channel.effective_recharge_multiplier,
                local_recharge_multiplier=config.local_recharge_multiplier,
                old_target_rate=previous_target_rate,
                new_target_rate=config.target_rate,
                old_current_rate=old_current,
                new_current_rate=new_current,
                old_upstream_key_status=health_transition.old_key_status,
                new_upstream_key_status=health_transition.new_key_status,
                old_upstream_group_status=health_transition.old_group_status,
                new_upstream_group_status=health_transition.new_group_status,
                old_remote_schedulable=old_remote_schedulable,
                new_remote_schedulable=new_remote_schedulable,
                reason=reason,
                status=status,
                safe_error=safe_error,
            )
        )

    @staticmethod
    def _synchronized_group(result: Any, account_id: int) -> dict[str, Any] | None:
        matches = _value(result, "account_group_matches")
        if not isinstance(matches, dict):
            return None
        raw_group = matches.get(account_id)
        if raw_group is None:
            raw_group = matches.get(str(account_id))
        group_id = _safe_text(_value(raw_group, "id", "group_id"), limit=128)
        group_name = _safe_text(_value(raw_group, "name", "group_name"), limit=200)
        if not group_id or not group_name:
            return None
        multiplier = _decimal_multiplier(_value(raw_group, "multiplier"))
        return {
            "id": group_id,
            "name": group_name,
            "multiplier": float(multiplier) if multiplier is not None else None,
        }

    async def _discover_channel(
        self,
        db: AsyncSession,
        channel_id: int,
        *,
        sync_inventory: bool = True,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> UpstreamChannelOut:
        lock = await self._lock_for(channel_id)
        async with lock:
            channel = await self._load_channel(db, channel_id)
            configs = await self._bound_configs(
                db,
                channel_id,
                remote_by_id=remote_by_id,
            )
            if not configs:
                raise UpstreamAccountServiceError(
                    "The upstream channel has no identity-bound API key accounts.",
                    status_code=409,
                )
            access_token = decrypt_text(channel.encrypted_access_token)
            refresh_token = decrypt_text(channel.encrypted_refresh_token)
            account_api_keys = await self._account_api_keys(
                db,
                configs,
                channel.id,
                remote_by_id=remote_by_id,
            )
            account_input_snapshots = {
                config.sub2api_account_id: self._account_rate_input_snapshot(config)
                for config in configs
            }
            discovery_base_url = channel.management_base_url or channel.canonical_base_url
            preferred_type = (
                channel.resolved_upstream_type
                if channel.upstream_type == "auto"
                and channel.resolved_upstream_type in {"newapi", "sub2api"}
                else channel.upstream_type
            )
            runtime_config = get_runtime_config_service()
            today_timezone = DEFAULT_TODAY_TIME_ZONE
            try:
                public_settings = await runtime_config.get_public_settings()
                configured_timezone = (
                    public_settings.get("display_timezone")
                    if isinstance(public_settings, dict)
                    else None
                )
                if isinstance(configured_timezone, str) and configured_timezone.strip():
                    today_timezone = configured_timezone.strip()
            except Exception:
                # Isolated tests and early startup may not have runtime
                # settings available; keep the application's default zone.
                pass

            async def run_discovery(active_access_token: str | None, active_type: str) -> Any:
                discovery = discover_upstream(
                    base_url=discovery_base_url,
                    upstream_type=active_type,
                    access_token=active_access_token,
                    new_api_user=channel.upstream_user_id,
                    account_api_keys=account_api_keys,
                    optimized_endpoint_fallbacks=True,
                    today_timezone=today_timezone,
                )
                return await discovery if inspect.isawaitable(discovery) else discovery

            configured_refresh_supported = bool(
                channel.upstream_type == "sub2api"
                or channel.resolved_upstream_type == "sub2api"
                or (channel.upstream_type == "auto" and channel.encrypted_refresh_token)
            )
            result: Any | None = None
            # A refresh-only Sub2API channel has no credential with which to
            # produce a 401. Rotate it before discovery instead of skipping the
            # only credential that can restore management access.
            should_refresh = bool(
                refresh_token
                and not access_token
                and configured_refresh_supported
            )
            if not should_refresh:
                result = await run_discovery(access_token, preferred_type)
                if (
                    channel.upstream_type == "auto"
                    and preferred_type != "auto"
                    and str(_value(result, "status") or "error").strip().lower() != "ok"
                    and not bool(_value(result, "sub2api_auth_rejected"))
                ):
                    result = await run_discovery(access_token, "auto")
                result_type = str(_value(result, "upstream_type") or "").strip().lower()
                should_refresh = bool(
                    _value(result, "sub2api_auth_rejected")
                    and refresh_token
                    and (
                        configured_refresh_supported
                        or result_type == "sub2api"
                    )
                )
            if should_refresh:
                refreshed = refresh_sub2api_tokens(
                    discovery_base_url,
                    refresh_token or "",
                )
                token_pair = await refreshed if inspect.isawaitable(refreshed) else refreshed
                if token_pair is not None:
                    channel.encrypted_access_token = encrypt_text(token_pair.access_token)
                    channel.encrypted_refresh_token = encrypt_text(token_pair.refresh_token)
                    try:
                        await db.execute(
                            update(UpstreamAccountConfig)
                            .where(UpstreamAccountConfig.channel_id == channel.id)
                            .values(encrypted_access_token=channel.encrypted_access_token)
                            .execution_options(synchronize_session=False)
                        )
                        # Sub2API invalidates the old RT before returning the new
                        # pair, so persist the pair before any retry can fail.
                        await db.commit()
                        await db.refresh(channel)
                    except Exception as exc:
                        await db.rollback()
                        raise UpstreamAccountServiceError(
                            "Could not securely save refreshed upstream credentials.",
                            status_code=503,
                        ) from exc

                    current_configs = await db.execute(
                        select(UpstreamAccountConfig)
                        .where(
                            UpstreamAccountConfig.sub2api_account_id.in_(
                                account_input_snapshots
                            )
                        )
                        .execution_options(populate_existing=True)
                    )
                    configs = [
                        config
                        for config in current_configs.scalars().all()
                        if config.channel_id == channel.id
                    ]
                    configs = await self._filter_current_bindings(
                        configs,
                        remote_by_id=remote_by_id,
                    )
                    account_api_keys = await self._account_api_keys(
                        db,
                        configs,
                        channel.id,
                        remote_by_id=remote_by_id,
                    )

                    access_token = token_pair.access_token
                    result = await run_discovery(access_token, preferred_type)
                    if (
                        channel.upstream_type == "auto"
                        and preferred_type != "auto"
                        and str(_value(result, "status") or "error").strip().lower() != "ok"
                    ):
                        result = await run_discovery(access_token, "auto")
            if result is None:
                result = await run_discovery(access_token, preferred_type)
                if (
                    channel.upstream_type == "auto"
                    and preferred_type != "auto"
                    and str(_value(result, "status") or "error").strip().lower() != "ok"
                    and not bool(_value(result, "sub2api_auth_rejected"))
                ):
                    result = await run_discovery(access_token, "auto")
            previous_channel_recharge = channel.effective_recharge_multiplier
            discovery_succeeded = self._apply_discovery_to_channel(channel, result)
            await self._apply_api_key_balance_fallback(channel, configs)
            local_recharge, local_source, local_status = await self._local_recharge()
            try:
                sync_enabled = await runtime_config.get_upstream_rate_sync_enabled()
            except Exception:
                # Discovery remains read-only if runtime settings are not yet
                # available during startup or isolated service tests.
                sync_enabled = False
            auto_disable_allowed = await self.accounts.automatic_upstream_disable_allowed()
            async with AsyncExitStack() as account_locks:
                for config in sorted(
                    configs,
                    key=lambda item: item.sub2api_account_id,
                ):
                    account_lock = await self.accounts._lock_for(
                        config.sub2api_account_id
                    )
                    await account_locks.enter_async_context(account_lock)

                with db.no_autoflush:
                    current_config_result = await db.execute(
                        select(UpstreamAccountConfig)
                        .where(
                            UpstreamAccountConfig.id.in_(
                                [config.id for config in configs]
                            )
                        )
                        .execution_options(populate_existing=True)
                    )
                current_configs = {
                    config.id: config
                    for config in current_config_result.scalars().all()
                }
                for pending_config in configs:
                    expected_inputs = account_input_snapshots.get(
                        pending_config.sub2api_account_id
                    )
                    if expected_inputs is None:
                        continue
                    config = current_configs.get(pending_config.id)
                    if (
                        config is None
                        or config.channel_id != channel.id
                        or self._account_rate_input_snapshot(config) != expected_inputs
                    ):
                        continue
                    try:
                        current_remote = (
                            remote_by_id.get(config.sub2api_account_id)
                            if remote_by_id is not None
                            else await self.accounts._remote_account(
                                config.sub2api_account_id
                            )
                        )
                        if current_remote is None:
                            continue
                        self.accounts._require_config_binding(current_remote, config)
                    except UpstreamAccountServiceError:
                        continue
                    if not discovery_succeeded:
                        config.upstream_health_invalid_count = 0
                        config.target_rate = None
                        config.local_recharge_multiplier = local_recharge
                        config.local_recharge_source = local_source
                        config.local_recharge_status = local_status
                        config.group_multiplier_status = "discovery_failed"
                        config.last_discovered_at = channel.last_discovered_at
                        config.last_error = (
                            "Upstream channel discovery failed; cached rates were not applied."
                        )
                        continue
                    previous_group_multiplier = config.effective_group_multiplier
                    previous_local_recharge_multiplier = config.local_recharge_multiplier
                    previous_target_rate = config.target_rate
                    upstream_state = self._synchronized_upstream_state(
                        result,
                        config.sub2api_account_id,
                    )
                    self.accounts.apply_upstream_usage_state(
                        config,
                        upstream_state,
                        now=channel.last_discovered_at or _utcnow(),
                    )
                    health_transition = self.accounts.apply_authoritative_upstream_state(
                        config,
                        upstream_state,
                        now=channel.last_discovered_at or _utcnow(),
                    )
                    synchronized_group = self._synchronized_group(
                        result,
                        config.sub2api_account_id,
                    )
                    if synchronized_group is not None:
                        config.selected_group_id = str(synchronized_group["id"])
                        config.selected_group_name = str(synchronized_group["name"])
                    elif (
                        health_transition.observed_group_id is not None
                        or health_transition.observed_group_name is not None
                    ):
                        config.selected_group_id = health_transition.observed_group_id
                        config.selected_group_name = health_transition.observed_group_name
                    else:
                        # A successful channel response without an unambiguous key
                        # match must not reuse a historical group for billing.
                        config.selected_group_id = None
                        config.selected_group_name = None
                    self._derive_account(
                        config,
                        channel,
                        local_recharge=local_recharge,
                        local_source=local_source,
                        local_status=local_status,
                    )
                    if synchronized_group is not None:
                        if synchronized_group["multiplier"] is not None:
                            config.group_multiplier_source = "upstream_key"
                        elif config.effective_group_multiplier is None:
                            config.group_multiplier_status = "group_rate_unavailable"
                            config.last_error = (
                                "The synchronized upstream group does not expose a billing multiplier."
                            )
                    if health_transition.new_group_status in INVALID_UPSTREAM_GROUP_STATUSES:
                        config.target_rate = None
                        config.group_multiplier_status = "group_unavailable"
                        config.last_error = "The synchronized upstream group is unavailable."
                    elif health_transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES:
                        config.target_rate = None
                        config.last_error = "The synchronized upstream API key is unavailable."
                    (
                        current_remote,
                        old_remote_schedulable,
                        new_remote_schedulable,
                        health_action_status,
                        health_safe_error,
                    ) = await self.accounts.maybe_disable_for_upstream_state(
                        current_remote,
                        config,
                        health_transition,
                        allowed=auto_disable_allowed,
                    )
                    await self._reconcile_account_rate(
                        db,
                        config,
                        channel,
                        previous_group_multiplier=previous_group_multiplier,
                        previous_upstream_recharge_multiplier=previous_channel_recharge,
                        previous_local_recharge_multiplier=previous_local_recharge_multiplier,
                        previous_target_rate=previous_target_rate,
                        sync_enabled=sync_enabled,
                        health_transition=health_transition,
                        old_remote_schedulable=old_remote_schedulable,
                        new_remote_schedulable=new_remote_schedulable,
                        health_action_status=health_action_status,
                        health_safe_error=health_safe_error,
                        current_remote=current_remote,
                    )
                # Persist the complete channel batch before releasing account
                # locks so concurrent account edits cannot be overwritten.
                await db.flush()
            await db.commit()
            await db.refresh(channel)
        if not sync_inventory:
            return self._channel_out(channel, [])
        overview = await self.overview(db, sync_inventory=True)
        found = next((item for item in overview.channels if item.id == channel_id), None)
        if found is None:
            raise UpstreamAccountServiceError("The upstream channel has no API key accounts.", status_code=409)
        return found

    async def discover_channel(self, db: AsyncSession, channel_id: int) -> UpstreamChannelOut:
        async with self._discover_all_lock:
            result = await self._discover_channel(db, channel_id)
            await self._rebalance_priorities_best_effort(db)
            refreshed = await self.overview(db, sync_inventory=False)
            return next(
                (item for item in refreshed.channels if item.id == channel_id),
                result,
            )

    async def discover_all(
        self,
        db: AsyncSession,
        *,
        legacy_bindings: dict[int, str] | None = None,
        max_concurrency: int = 1,
        sync_inventory: bool = True,
        require_management_credentials: bool = False,
        force: bool = True,
        cache_max_age_seconds: int | None = 900,
    ) -> UpstreamChannelDiscoverAllOut:
        async with get_workflow_coordinator().upstream_batch():
            async with self._discover_all_lock:
                return await self._discover_all_locked(
                    db,
                    legacy_bindings=legacy_bindings,
                    max_concurrency=max_concurrency,
                    sync_inventory=sync_inventory,
                    require_management_credentials=require_management_credentials,
                    force=force,
                    cache_max_age_seconds=cache_max_age_seconds,
                )

    async def _discover_all_locked(
        self,
        db: AsyncSession,
        *,
        legacy_bindings: dict[int, str] | None = None,
        max_concurrency: int = 1,
        sync_inventory: bool = True,
        require_management_credentials: bool = False,
        force: bool = True,
        cache_max_age_seconds: int | None = 900,
    ) -> UpstreamChannelDiscoverAllOut:
        started_at = perf_counter()
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 0 <= max_concurrency <= 50
        ):
            raise ValueError("max_concurrency must be between 0 and 50.")
        if legacy_bindings is not None:
            await self.accounts.bind_legacy_identities(db, legacy_bindings)
        inventory_started_at = perf_counter()
        if sync_inventory:
            remote_by_id, _configs, _channels_by_id, _membership_changed = (
                await self.sync_inventory(db)
            )
        else:
            remote_by_id = {
                account_id: account
                for account in await self.accounts._remote_accounts()
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        inventory_duration_ms = elapsed_ms(inventory_started_at)
        overview = await self.overview(
            db,
            sync_inventory=False,
            remote_by_id=remote_by_id,
        )
        if legacy_bindings is not None:
            # The first confirmation can bind a legacy row whose live endpoint
            # differs from its stored channel. Inventory sync then creates the
            # origin-rebind tombstone; revalidate the same strict fingerprints
            # once more so this single informed confirmation covers both steps.
            await self.accounts.bind_legacy_identities(db, legacy_bindings)
        channels: list[UpstreamChannelOut] = []
        succeeded = 0
        failed = 0
        eligible_channels = [
            channel
            for channel in overview.channels
            if channel.probe_enabled
            and (
                not require_management_credentials
                or channel.access_token_set
                or (
                    channel.refresh_token_set
                    and (
                        channel.upstream_type in {"auto", "sub2api"}
                        or channel.resolved_upstream_type == "sub2api"
                    )
                )
            )
        ]
        cached_channels = (
            []
            if force
            else [
                channel
                for channel in eligible_channels
                if self._channel_cache_is_fresh(channel, cache_max_age_seconds)
            ]
        )
        cached_channel_ids = {channel.id for channel in cached_channels}
        channels_to_probe = [
            channel for channel in eligible_channels if channel.id not in cached_channel_ids
        ]
        skipped = len(overview.channels) - len(eligible_channels)
        effective_concurrency = (
            len(channels_to_probe) if max_concurrency == 0 else max_concurrency
        )
        probe_started_at = perf_counter()
        if effective_concurrency <= 1:
            results: list[UpstreamChannelOut | BaseException] = []
            for channel in channels_to_probe:
                try:
                    results.append(
                        await self._discover_channel(
                            db,
                            channel.id,
                            sync_inventory=False,
                            remote_by_id=remote_by_id,
                        )
                    )
                except UpstreamAccountServiceError as exc:
                    results.append(exc)
        else:
            # The caller's session may have an active read transaction from the
            # overview. Release it before worker sessions commit channel-local
            # changes, then expire the identity map before the final readback.
            await db.rollback()
            db.expire_all()
            session_factory = self._session_factory
            if session_factory is None:
                bind = db.bind
                session_factory = (
                    async_sessionmaker(bind, expire_on_commit=False)
                    if bind is not None
                    else AsyncSessionLocal
                )
            semaphore = asyncio.Semaphore(effective_concurrency)

            async def discover_with_session(channel_id: int) -> UpstreamChannelOut:
                async with semaphore:
                    async with session_factory() as worker_db:
                        return await self._discover_channel(
                            worker_db,
                            channel_id,
                            sync_inventory=False,
                            remote_by_id=remote_by_id,
                        )

            results = list(
                await asyncio.gather(
                    *(discover_with_session(channel.id) for channel in channels_to_probe),
                    return_exceptions=True,
                )
            )
        probe_duration_ms = elapsed_ms(probe_started_at)

        for result in results:
            if isinstance(result, UpstreamAccountServiceError):
                failed += 1
                continue
            if isinstance(result, BaseException):
                raise result
            discovered = result
            channels.append(discovered)
            discovery_failed = discovered.last_error == "Upstream channel discovery failed."
            if (
                not discovery_failed
                and discovered.recharge_multiplier_status in {"ok", "default_missing", "fallback_manual"}
            ):
                succeeded += 1
            else:
                failed += 1
        eligible_account_ids = {
            account.sub2api_account_id
            for channel in eligible_channels
            for account in channel.accounts
        }
        priority_started_at = perf_counter()
        await self._rebalance_priorities_best_effort(
            db,
            account_ids=eligible_account_ids,
        )
        priority_duration_ms = elapsed_ms(priority_started_at)
        try:
            remote_by_id = {
                account_id: account
                for account in await self.accounts._remote_accounts()
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        except UpstreamAccountServiceError:
            pass
        db.expire_all()
        refreshed = await self.overview(
            db,
            sync_inventory=False,
            remote_by_id=remote_by_id,
        )
        return UpstreamChannelDiscoverAllOut(
            total=len(overview.channels),
            succeeded=succeeded,
            failed=failed,
            cached=len(cached_channels),
            skipped=skipped,
            force=force,
            cache_max_age_seconds=None if force else cache_max_age_seconds,
            duration_ms=elapsed_ms(started_at),
            inventory_duration_ms=inventory_duration_ms,
            probe_duration_ms=probe_duration_ms,
            priority_duration_ms=priority_duration_ms,
            channels=refreshed.channels,
            overview=refreshed,
        )


_service: UpstreamChannelService | None = None


def get_upstream_channel_service() -> UpstreamChannelService:
    global _service
    if _service is None:
        from app.services.upstream_priorities import get_upstream_priority_service

        _service = UpstreamChannelService(
            accounts=get_upstream_account_service(),
            priorities=get_upstream_priority_service(),
        )
    return _service
