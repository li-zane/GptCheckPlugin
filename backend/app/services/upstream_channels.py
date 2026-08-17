from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from copy import deepcopy
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from decimal import Decimal, DecimalException
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Iterable
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import AsyncSessionLocal
from app.core.upstream_urls import canonicalize_upstream_url, upstream_url_origin
from app.models import (
    UpstreamApiKey,
    ApiAccount,
    Upstream,
    UpstreamChangeEvent,
    UpstreamGroup,
    UpstreamPriorityInterval,
    UpstreamRateChangeLog,
)
from app.schemas import (
    ApiAccountAvailabilityTestOut,
    ApiAccountConnectionTestOut,
    ApiAccountOut,
    UpstreamDiscoverAllOut,
    UpstreamMonitorsOut,
    UpstreamOut,
    UpstreamUpdate,
    UpstreamGroupOptionOut,
    UpstreamOverviewOut,
)
from app.services.sub2api import Sub2ApiClient
from app.services.change_logs import record_upstream_channel_changes
from app.services.daily_usage_policy import daily_usage_failure_decision
from app.services.events import elapsed_ms, record_event
from app.services.runtime_config import get_runtime_config_service
from app.services.notifications import (
    NotificationService,
    enqueue_api_key_rate_changed,
    enqueue_upstream_token_invalid,
    enqueue_upstream_group_changed,
)
from app.services.upstream_accounts import (
    AUTO_PAUSE_REASON_BALANCE,
    AUTO_PAUSE_REASON_GROUP,
    AUTO_PAUSE_REASON_KEY,
    AUTO_PAUSE_REASON_MONITOR,
    AUTO_PAUSE_REASON_RATE,
    DEFAULT_ENCRYPTION_KEY,
    INVALID_UPSTREAM_GROUP_STATUSES,
    INVALID_UPSTREAM_KEY_STATUSES,
    ApiAccountService,
    ApiAccountServiceError,
    UpstreamRecordIdentityStatus,
    UpstreamHealthTransition,
    resolve_rate_pause_policy,
    get_api_account_service,
    _calculate_expected_management_billing_multiplier,
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
    AccountGroupMatch,
    AccountUpstreamState,
    DEFAULT_TODAY_TIME_ZONE,
    MAX_UPSTREAM_TOKEN_LENGTH,
    discover_upstream,
    fetch_upstream_daily_usages,
    login_sub2api_tokens,
    refresh_sub2api_tokens,
)
from app.services.upstream_usage_history import (
    finalize_cached_yesterday_usage,
    finalize_elapsed_usage,
    finalize_yesterday_usage,
    hydrate_yesterday_usage,
    import_sub2api_daily_stats,
    missing_finalized_usage_dates,
    snapshot_today_usage,
    upsert_historical_upstream_usage,
    usage_day,
)
from app.services.workflow_coordination import get_workflow_coordinator


TOKEN_INVALID_STATUS = "token_invalid"
TOKEN_INVALID_ERROR = "Upstream channel token is invalid."
UPSTREAM_HISTORY_BACKFILL_TIMEOUT_SECONDS = 75.0


API_KEY_EXPORT_BATCH_SIZE = 200
logger = logging.getLogger(__name__)

_AVAILABILITY_STATE_FIELDS = (
    "availability_status",
    "availability_unavailable_count",
    "availability_recovery_count",
    "availability_checked_at",
    "availability_source",
    "availability_message",
)


@dataclass(frozen=True)
class _PreparedAccountAvailability:
    action: str | None
    evidence: dict[str, Any]
    values: dict[str, Any]


class _DiscoveryWritebackCoordinator:
    """Let probes finish concurrently, then admit one channel writer at a time."""

    def __init__(self, upstream_ids: list[str], max_probe_concurrency: int) -> None:
        self._upstream_ids = tuple(upstream_ids)
        self._ready_ids: set[str] = set()
        self._all_ready = asyncio.Event()
        self._completed = {upstream_id: asyncio.Event() for upstream_id in upstream_ids}
        self._probe_semaphore = asyncio.Semaphore(max_probe_concurrency)
        self._active_probe_ids: set[str] = set()

    def includes(self, upstream_id: str) -> bool:
        return upstream_id in self._completed

    def _mark_ready(self, upstream_id: str) -> None:
        if upstream_id not in self._completed:
            return
        self._ready_ids.add(upstream_id)
        if len(self._ready_ids) == len(self._upstream_ids):
            self._all_ready.set()

    async def start_probe(self, upstream_id: str) -> None:
        await self._probe_semaphore.acquire()
        self._active_probe_ids.add(upstream_id)

    def _finish_probe(self, upstream_id: str) -> None:
        if upstream_id in self._active_probe_ids:
            self._active_probe_ids.remove(upstream_id)
            self._probe_semaphore.release()

    async def wait_for_turn(self, upstream_id: str) -> None:
        self._finish_probe(upstream_id)
        self._mark_ready(upstream_id)
        await self._all_ready.wait()
        index = self._upstream_ids.index(upstream_id)
        for preceding_upstream_id in self._upstream_ids[:index]:
            await self._completed[preceding_upstream_id].wait()

    def task_done(self, upstream_id: str) -> None:
        self._finish_probe(upstream_id)
        self._mark_ready(upstream_id)
        completed = self._completed.get(upstream_id)
        if completed is not None:
            completed.set()


def _is_sqlite_locked_error(error: BaseException) -> bool:
    return isinstance(error, OperationalError) and "database is locked" in str(error).lower()


def _copy_mapped_row(row: Any) -> Any:
    values = {
        column.key: deepcopy(getattr(row, column.key))
        for column in row.__table__.columns
    }
    return type(row)(**values)


def _account_group_rate_change_reason(
    *,
    previous_group_id: str | None,
    current_group_id: str | None,
    previous_group_name: str | None,
    current_group_name: str | None,
    multiplier_changed: bool,
) -> str | None:
    if previous_group_id != current_group_id:
        return "upstream_group_assignment_change"
    if previous_group_name != current_group_name:
        return "upstream_group_name_change"
    if multiplier_changed:
        return "upstream_group_change"
    return None


def _group_removal_matches(
    change: dict[str, Any],
    *,
    group_id: str | None,
    group_name: str | None,
) -> bool:
    if change.get("change_type") != "removed":
        return False
    expected_id = str(group_id or "").strip().casefold()
    changed_id = str(change.get("group_id") or "").strip().casefold()
    if expected_id:
        return expected_id == changed_id
    expected_name = str(group_name or "").strip().casefold()
    changed_name = str(change.get("group_name") or "").strip().casefold()
    return bool(expected_name and expected_name == changed_name)


@dataclass(frozen=True)
class UpstreamDiscoveryOptions:
    """Optional task inclusion overrides for one upstream discovery run."""

    sync_rates: bool | None = None
    sync_priorities: bool | None = None
    evaluate_upstream_health: bool | None = None
    refresh_upstream_monitors: bool | None = None
    evaluate_account_availability: bool | None = None
    evaluate_balance_guard: bool | None = None
    evaluate_rate_pause: bool | None = None


class UpstreamService:
    """Manage shared upstream-site state independently from API-key accounts."""

    def __init__(
        self,
        sub2api: Sub2ApiClient | None = None,
        accounts: ApiAccountService | None = None,
        priorities: Any | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.accounts = accounts or ApiAccountService(sub2api or Sub2ApiClient())
        self.sub2api = self.accounts.sub2api
        self._priorities = priorities
        self._session_factory = session_factory
        self._locks: dict[int, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._inventory_lock = asyncio.Lock()
        self._discover_all_lock = asyncio.Lock()
        self._active_writeback_coordinator: _DiscoveryWritebackCoordinator | None = None
        self._background_discovery_tasks: set[asyncio.Task[None]] = set()
        self._background_discovery_counts: dict[int, int] = {}
        self._background_discovery_by_channel: dict[int, asyncio.Task[None]] = {}
        self._background_discovery_stopping = False

    def _priority_service(self):
        if self._priorities is None:
            from app.services.upstream_priorities import UpstreamPriorityService

            self._priorities = UpstreamPriorityService(accounts=self.accounts)
        return self._priorities

    def queue_discover_channel(self, upstream_id: str) -> None:
        if self._background_discovery_stopping:
            logger.info("Ignoring background upstream discovery during shutdown.")
            return
        existing = self._background_discovery_by_channel.get(upstream_id)
        if existing is not None and not existing.done():
            # Account edits can enqueue several refreshes in quick succession.
            # Keep one authoritative run per upstream so a stale run cannot
            # pause an account immediately before a newer run restores it.
            return
        self._background_discovery_counts[upstream_id] = (
            1
        )
        task = asyncio.create_task(self._discover_channel_in_background(upstream_id))
        self._background_discovery_tasks.add(task)
        self._background_discovery_by_channel[upstream_id] = task
        task.add_done_callback(
            lambda completed, queued_upstream_id=upstream_id: (
                self._consume_background_discovery_task(completed, queued_upstream_id)
            )
        )

    async def _discover_channel_in_background(self, upstream_id: str) -> None:
        session_factory = self._session_factory or AsyncSessionLocal
        async with session_factory() as db:
            await self.discover_channel(db, upstream_id)

    def _consume_background_discovery_task(
        self,
        task: asyncio.Task[None],
        upstream_id: str,
    ) -> None:
        self._background_discovery_tasks.discard(task)
        if self._background_discovery_by_channel.get(upstream_id) is task:
            self._background_discovery_by_channel.pop(upstream_id, None)
        self._background_discovery_counts.pop(upstream_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Background upstream channel discovery failed.")

    async def stop_background_tasks(self) -> None:
        self._background_discovery_stopping = True
        try:
            tasks = tuple(self._background_discovery_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._background_discovery_tasks.clear()
            self._background_discovery_counts.clear()
            self._background_discovery_by_channel.clear()
        finally:
            self._background_discovery_stopping = False

    async def _rebalance_priorities_best_effort(
        self,
        db: AsyncSession,
        *,
        account_ids: set[int] | None = None,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
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
                    remote_by_id=remote_by_id,
                )
            else:
                await self._priority_service().rebalance(
                    db,
                    remote_by_id=remote_by_id,
                )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            await db.rollback()
            logger.exception("Priority rebalance failed after an upstream channel mutation.")

    async def _lock_for(self, upstream_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(upstream_id, asyncio.Lock())

    @staticmethod
    def _credential_fingerprint(access_token: str | None) -> str:
        normalized = str(access_token or "").strip()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _channel_cache_is_fresh(
        channel: UpstreamOut,
        max_age_seconds: int | None,
    ) -> bool:
        if (
            channel.last_discovered_at is None
            or channel.last_error in {
                "Upstream channel discovery failed.",
                TOKEN_INVALID_ERROR,
            }
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
    def _invalidate_account(
        config: ApiAccount,
        *,
        preserve_pause_ownership: bool = False,
    ) -> None:
        config.remote_upstream_api_key_id = None
        config.upstream_api_key_id = None
        config.upstream_identity_rebind_required = False
        config.discovered_upstream_group_multiplier = None
        config.upstream_group_multiplier = None
        config.group_multiplier_source = None
        config.group_multiplier_status = "not_discovered"
        config.expected_management_billing_multiplier = None
        config.last_discovered_at = None
        config.upstream_key_status = "not_checked"
        config.upstream_group_status = "not_checked"
        config.upstream_health_invalid_count = 0
        config.upstream_key_checked_at = None
        config.upstream_group_checked_at = None
        ApiAccountService.clear_upstream_usage_state(config)
        ApiAccountService.resolve_all_pause_holds(
            config,
            now=_utcnow(),
            clear_ownership=not preserve_pause_ownership,
        )
        if config.priority_interval_id is not None:
            config.desired_priority = None
            config.priority_sync_status = "multiplier_unavailable"
            config.priority_sync_error = None

    @staticmethod
    def _invalidate_channel(channel: Upstream) -> None:
        channel.resolved_platform_type = None
        channel.group_options = []
        channel.discovered_upstream_recharge_multiplier = None
        channel.upstream_recharge_multiplier = None
        channel.recharge_multiplier_source = None
        channel.recharge_multiplier_status = "not_discovered"
        channel.wallet_balance_usd = None
        channel.wallet_total_usd = None
        channel.wallet_used_usd = None
        channel.balance_unit = None
        channel.balance_status = "not_checked"
        channel.balance_source = None
        channel.balance_message = None
        channel.balance_checked_at = None
        channel.today_upstream_wallet_cost_usd = None
        channel.today_balance_unit = None
        channel.today_balance_status = "not_checked"
        channel.today_balance_checked_at = None
        channel.yesterday_upstream_wallet_cost_usd = None
        channel.yesterday_balance_unit = None
        channel.yesterday_balance_status = "not_checked"
        channel.yesterday_balance_checked_at = None
        channel.balance_guard_state = "not_checked"
        channel.balance_guard_basis = None
        channel.balance_guard_value = None
        channel.balance_guard_checked_at = None
        channel.balance_guard_episode_id = None
        channel.balance_guard_paused_count = 0
        channel.upstream_monitors = []
        channel.upstream_monitor_count = 0
        channel.upstream_monitor_status = "not_checked"
        channel.upstream_monitor_message = None
        channel.upstream_monitor_checked_at = None
        channel.upstream_monitor_guard_state = "not_checked"
        channel.upstream_monitor_unavailable_count = 0
        channel.upstream_monitor_recovery_count = 0
        channel.upstream_monitor_guard_checked_at = None
        channel.last_error = None
        channel.last_discovered_at = None

    def _known_secrets(
        self,
        config: ApiAccount | None,
        channel: Upstream | None = None,
    ) -> tuple[str | None, ...]:
        return (
            *self.accounts._known_local_secrets(config),
            decrypt_text(channel.encrypted_access_token) if channel is not None else None,
            decrypt_text(channel.encrypted_refresh_token) if channel is not None else None,
            decrypt_text(channel.encrypted_login_username) if channel is not None else None,
            decrypt_text(channel.encrypted_login_password) if channel is not None else None,
        )

    def _ensure_secret_storage_ready(self, payload: UpstreamUpdate) -> None:
        if payload.access_token and len(payload.access_token) > MAX_UPSTREAM_TOKEN_LENGTH:
            raise ApiAccountServiceError("The access token is too long.", status_code=422)
        if payload.refresh_token and len(payload.refresh_token) > MAX_UPSTREAM_TOKEN_LENGTH:
            raise ApiAccountServiceError("The refresh token is too long.", status_code=422)
        if payload.login_username and len(payload.login_username) > 320:
            raise ApiAccountServiceError("The login username is too long.", status_code=422)
        if payload.login_password and len(payload.login_password) > MAX_UPSTREAM_TOKEN_LENGTH:
            raise ApiAccountServiceError("The login password is too long.", status_code=422)
        if payload.clear_access_token and payload.access_token:
            raise ApiAccountServiceError(
                "An access token cannot be set and cleared in the same request.",
                status_code=422,
            )
        if payload.clear_refresh_token and payload.refresh_token:
            raise ApiAccountServiceError(
                "A refresh token cannot be set and cleared in the same request.",
                status_code=422,
            )
        if payload.clear_login_credentials and (
            payload.login_username is not None or payload.login_password is not None
        ):
            raise ApiAccountServiceError(
                "Login credentials cannot be set and cleared in the same request.",
                status_code=422,
            )
        if (payload.login_username is None) != (payload.login_password is None):
            raise ApiAccountServiceError(
                "Login username and password must be provided together.",
                status_code=422,
            )
        if payload.login_username is not None and not payload.login_username.strip():
            raise ApiAccountServiceError("The login username is required.", status_code=422)
        if payload.login_password is not None and not payload.login_password:
            raise ApiAccountServiceError("The login password is required.", status_code=422)
        settings = get_settings()
        if (
            (payload.access_token or payload.refresh_token or payload.login_username or payload.login_password)
            and settings.app_env == "production"
            and settings.app_encryption_key.strip() == DEFAULT_ENCRYPTION_KEY
        ):
            raise ApiAccountServiceError(
                "Configure a non-default application encryption key before saving credentials.",
                status_code=503,
            )

    async def _load_channel(self, db: AsyncSession, upstream_id: str) -> Upstream:
        channel = await db.get(Upstream, upstream_id)
        if channel is None or channel.deleted_at is not None:
            raise ApiAccountServiceError("The upstream channel was not found.", status_code=404)
        return channel

    async def _bound_configs(
        self,
        db: AsyncSession,
        upstream_id: str,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> list[ApiAccount]:
        result = await db.execute(
            select(ApiAccount).where(ApiAccount.upstream_id == upstream_id)
        )
        return await self._filter_current_bindings(
            list(result.scalars().all()),
            remote_by_id=remote_by_id,
        )

    async def _sync_linked_account_base_urls(
        self,
        configs: list[ApiAccount],
        target_base_url: str,
    ) -> None:
        """Keep each linked sub2api account on the channel's new endpoint."""

        async def verify_readback(
            account_id: int,
            expected_identity: str,
            expected_base_url: str,
        ) -> None:
            candidate = await self.sub2api.get_account_by_id(account_id)
            if candidate is None:
                raise ApiAccountServiceError(
                    "The management-site account was not found during upstream address readback.",
                    status_code=404,
                )
            if self.accounts._numeric_remote_id(candidate) != account_id:
                raise ApiAccountServiceError(
                    "The management-site account readback returned a mismatched account.",
                    status_code=409,
                )
            if self.accounts._require_remote_binding_fingerprint(candidate) != expected_identity:
                raise ApiAccountServiceError(
                    "The management-site account identity changed during upstream address readback.",
                    status_code=409,
                )
            if _remote_base_url(candidate) != expected_base_url:
                raise ApiAccountServiceError(
                    "The management-site account upstream address readback did not match.",
                    status_code=409,
                )

        remote_by_id = {
            account_id: account
            for account in await self.accounts._remote_accounts()
            if (account_id := self.accounts._numeric_remote_id(account)) is not None
        }
        updated: list[tuple[int, str, str]] = []
        try:
            for config in configs:
                account_id = int(config.management_account_id)
                remote = remote_by_id.get(account_id)
                previous_base_url = _remote_base_url(remote) if remote is not None else None
                if remote is None or previous_base_url == target_base_url:
                    continue
                expected_identity = self.accounts._require_remote_binding_fingerprint(remote)

                def validate_current(
                    candidate: dict[str, Any],
                    *,
                    expected: str = expected_identity,
                ) -> None:
                    if self.accounts._require_remote_binding_fingerprint(candidate) != expected:
                        raise ApiAccountServiceError(
                            "The management-site account identity changed before the upstream URL update completed.",
                            status_code=409,
                        )

                await self.sub2api.update_account_base_url(
                    account_id,
                    target_base_url,
                    validate_current=validate_current,
                )
                if previous_base_url is not None:
                    updated.append((account_id, previous_base_url, expected_identity))
                await verify_readback(
                    account_id,
                    expected_identity,
                    target_base_url,
                )
        except BaseException as exc:
            rollback_failed = False
            for account_id, previous_base_url, expected_identity in reversed(updated):
                try:
                    def validate_rollback(
                        candidate: dict[str, Any],
                        *,
                        expected: str = expected_identity,
                    ) -> None:
                        if self.accounts._require_remote_binding_fingerprint(candidate) != expected:
                            raise ApiAccountServiceError(
                                "The management-site account identity changed during URL rollback.",
                                status_code=409,
                            )

                    await self.sub2api.update_account_base_url(
                        account_id,
                        previous_base_url,
                        validate_current=validate_rollback,
                    )
                    await verify_readback(
                        account_id,
                        expected_identity,
                        previous_base_url,
                    )
                except BaseException as rollback_exc:
                    if isinstance(rollback_exc, asyncio.CancelledError):
                        raise
                    rollback_failed = True
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, ApiAccountServiceError) and not rollback_failed:
                raise
            detail = " Rollback was incomplete." if rollback_failed else ""
            raise ApiAccountServiceError(
                "Unable to update and verify linked management-site account upstream addresses."
                + detail
            ) from None

    async def _filter_current_bindings(
        self,
        configs: list[ApiAccount],
        *,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
    ) -> list[ApiAccount]:
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
                (remote := remote_by_id.get(config.management_account_id)) is not None
                and self.accounts._config_binding_status(remote, config) == "bound"
            )
        ]

    async def _account_api_keys(
        self,
        db: AsyncSession,
        configs: list[ApiAccount],
        upstream_id: str,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
        *,
        imported_api_keys: dict[int, str] | None = None,
        persist_imported: bool = True,
    ) -> dict[int, str]:
        refresh_remote_inventory = remote_by_id is None
        if refresh_remote_inventory:
            await db.commit()
            remote_by_id = {
                account_id: account
                for account in await self.accounts._remote_accounts()
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        channel = await self._load_channel(db, upstream_id)
        try:
            channel_base_url = canonicalize_upstream_url(channel.api_endpoint_url)
        except ValueError:
            configs.clear()
            return {}

        async def live_strict_fingerprints(
            candidates: list[ApiAccount],
        ) -> dict[int, str]:
            fingerprints: dict[int, str] = {}
            for config in candidates:
                remote = remote_by_id.get(config.management_account_id)
                if remote is None or self.accounts._config_binding_status(remote, config) != "bound":
                    continue
                has_stored_key = bool(decrypt_text(config.encrypted_api_key))
                requires_remote_origin = (
                    not config.upstream_auto_assign_disabled or not has_stored_key
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
                fingerprints[config.management_account_id] = fingerprint
            return fingerprints

        async def reload_current(
            candidates: list[ApiAccount],
            *,
            expected_strict_fingerprints: dict[int, str] | None = None,
        ) -> tuple[list[ApiAccount], dict[int, str]]:
            candidate_ids = [config.id for config in candidates]
            if not candidate_ids:
                return [], {}
            current_result = await db.execute(
                select(ApiAccount)
                .where(ApiAccount.id.in_(candidate_ids))
                .execution_options(populate_existing=True)
            )
            current = [
                config
                for config in current_result.scalars().all()
                if (
                    config.upstream_id == upstream_id
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
                if config.management_account_id in strict_fingerprints
            ]
            return current, strict_fingerprints

        configs[:], export_started_strict_fingerprints = await reload_current(configs)
        missing_account_ids: list[int] = []
        for config in configs:
            api_key = decrypt_text(config.encrypted_api_key)
            if not api_key:
                missing_account_ids.append(config.management_account_id)
        if not missing_account_ids:
            configs[:], _strict_fingerprints = await reload_current(
                configs,
                expected_strict_fingerprints=export_started_strict_fingerprints,
            )
            return {
                config.management_account_id: api_key
                for config in configs
                if (
                    not config.api_key_origin_rebind_required
                    and (api_key := decrypt_text(config.encrypted_api_key))
                )
            }
        export_started_fingerprints = {
            config.management_account_id: config.remote_identity_fingerprint.strip().lower()
            for config in configs
            if config.remote_identity_fingerprint
        }
        await db.commit()
        try:
            exported: dict[int, str] = {}
            for start in range(0, len(missing_account_ids), API_KEY_EXPORT_BATCH_SIZE):
                batch = missing_account_ids[start : start + API_KEY_EXPORT_BATCH_SIZE]
                exported.update(await self.sub2api.export_api_key_secrets(batch))
        except Exception:
            if refresh_remote_inventory:
                remote_by_id = {
                    account_id: account
                    for account in await self.accounts._remote_accounts()
                    if (account_id := self.accounts._numeric_remote_id(account)) is not None
                }
            configs[:], _strict_fingerprints = await reload_current(
                configs,
                expected_strict_fingerprints=export_started_strict_fingerprints,
            )
            return {
                config.management_account_id: api_key
                for config in configs
                if (
                    not config.api_key_origin_rebind_required
                    and (api_key := decrypt_text(config.encrypted_api_key))
                )
            }
        if refresh_remote_inventory:
            remote_by_id = {
                account_id: account
                for account in await self.accounts._remote_accounts()
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        configs[:], checked_strict_fingerprints = await reload_current(
            configs,
            expected_strict_fingerprints=export_started_strict_fingerprints,
        )
        checked_fingerprints = {
            config.management_account_id: config.remote_identity_fingerprint.strip().lower()
            for config in configs
            if (
                config.remote_identity_fingerprint
                and config.remote_identity_fingerprint.strip().lower()
                == export_started_fingerprints.get(config.management_account_id)
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
        if imported_api_keys is not None:
            imported_api_keys.update(exported)
        settings = get_settings()
        can_persist = not (
            settings.app_env == "production"
            and settings.app_encryption_key.strip() == DEFAULT_ENCRYPTION_KEY
        )
        persisted = False
        if can_persist and persist_imported:
            try:
                for account_id in missing_account_ids:
                    api_key = exported.get(account_id)
                    if not api_key:
                        continue
                    result = await db.execute(
                        update(ApiAccount)
                        .where(
                            ApiAccount.management_account_id == account_id,
                            ApiAccount.upstream_id == upstream_id,
                            ApiAccount.encrypted_api_key.is_(None),
                            ApiAccount.remote_identity_fingerprint
                            == checked_fingerprints[account_id],
                            ApiAccount.api_key_origin_rebind_required.is_(False),
                        )
                        .values(encrypted_api_key=encrypt_text(api_key))
                        .execution_options(synchronize_session=False)
                    )
                    persisted = persisted or bool(result.rowcount)
                if persisted:
                    await db.commit()
            except Exception:
                await db.rollback()
                raise ApiAccountServiceError(
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
            checked_fingerprint = checked_fingerprints.get(config.management_account_id)
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
                api_key = exported.get(config.management_account_id)
            if api_key:
                account_api_keys[config.management_account_id] = api_key
        return account_api_keys

    @staticmethod
    def _account_rate_input_snapshot(config: ApiAccount) -> tuple[Any, ...]:
        return (
            config.upstream_id,
            config.remote_identity_fingerprint,
            config.remote_upstream_api_key_id,
            config.upstream_identity_rebind_required,
            config.api_key_origin_rebind_required,
            config.encrypted_api_key,
            config.selected_group_id,
            config.selected_group_name,
            config.upstream_group_multiplier_override,
            config.upstream_recharge_multiplier_override,
            config.availability_check_mode,
            config.availability_monitor_id,
            config.availability_test_model,
            tuple(
                sorted(
                    (
                        str(item.get("id") or ""),
                        str(item.get("display_name") or ""),
                    )
                    for item in (config.available_models or [])
                    if isinstance(item, dict)
                )
            ),
        )

    async def _sync_inventory(
        self,
        db: AsyncSession,
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, ApiAccount],
        dict[str, Upstream],
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
        dict[int, ApiAccount],
        dict[str, Upstream],
        bool,
    ]:
        """Synchronize the local API-key/channel inventory without remote writes."""

        return await self._sync_inventory(db)

    async def _sync_inventory_unlocked(
        self,
        db: AsyncSession,
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, ApiAccount],
        dict[str, Upstream],
        bool,
    ]:
        remote_by_id = {
            account_id: account
            for account in await self.accounts._remote_accounts()
            if (account_id := self.accounts._numeric_remote_id(account)) is not None
        }
        config_ids_result = await db.execute(
            select(ApiAccount.management_account_id)
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
            result = None
            for attempt in range(2):
                try:
                    result = await self._sync_inventory_rows(db, remote_by_id)
                    break
                except IntegrityError as exc:
                    await db.rollback()
                    db.expire_all()
                    if attempt == 1:
                        raise ApiAccountServiceError(
                            "The upstream inventory changed concurrently; refresh and retry.",
                            status_code=409,
                        ) from exc
            if result is None:
                raise ApiAccountServiceError(
                    "The upstream inventory could not be synchronized.",
                    status_code=409,
                )
            await self.accounts.sync_available_models(
                db,
                result[1],
                result[0],
            )
            return result
        finally:
            for lock in reversed(account_locks):
                lock.release()

    async def _sync_inventory_rows(
        self,
        db: AsyncSession,
        remote_by_id: dict[int, dict[str, Any]],
    ) -> tuple[
        dict[int, dict[str, Any]],
        dict[int, ApiAccount],
        dict[str, Upstream],
        bool,
    ]:
        channel_result = await db.execute(
            select(Upstream).where(Upstream.deleted_at.is_(None))
        )
        channels = list(channel_result.scalars().all())
        channels_by_url = {item.api_endpoint_url: item for item in channels}

        config_result = await db.execute(select(ApiAccount))
        configs = {item.management_account_id: item for item in config_result.scalars().all()}
        changed = False
        priority_membership_changed = False
        inventory_checked_at = _utcnow()
        auto_assigned_config_ids: set[int] = set()

        for account_id, config in configs.items():
            if account_id not in remote_by_id:
                if config.remote_present:
                    config.remote_present = False
                    config.remote_missing_at = inventory_checked_at
                    changed = True
                if config.priority_interval_id is not None:
                    priority_membership_changed = True

        for account_id in sorted(remote_by_id):
            remote = remote_by_id[account_id]
            config = configs.get(account_id)
            if config is not None and config.deleted_at is not None:
                config.deleted_at = None
                config.last_seen_at = inventory_checked_at
                changed = True
            if config is not None and not config.remote_present:
                config.remote_present = True
                config.remote_missing_at = None
                changed = True
            stored_channel = (
                next((item for item in channels if item.id == config.upstream_id), None)
                if config is not None and config.upstream_id is not None
                else None
            )
            binding_status = (
                self.accounts._config_binding_status(remote, config)
                if config is not None
                else "unmanaged"
            )
            name_only_binding_change = bool(
                config is not None
                and binding_status == "mismatch"
                and self.accounts._config_binding_differs_only_by_remote_name(
                    remote,
                    config,
                    extra_secrets=self._known_secrets(config, stored_channel),
                )
            )
            if (
                config is not None
                and binding_status != "bound"
                and not name_only_binding_change
            ):
                if self.accounts._config_binding_evidence_incomplete(remote, config):
                    # Missing immutable identity fields are inconclusive. Keep
                    # the existing configuration intact while all automatic
                    # operations remain fail-closed on the non-bound status.
                    continue
                if binding_status == "mismatch":
                    if not config.api_key_origin_rebind_required:
                        self.accounts.archive_data_before_invalidation(
                            db,
                            config,
                            reason="remote_identity_mismatch",
                            channel=stored_channel,
                        )
                        config.api_key_origin_rebind_required = True
                        changed = True
                    if any(
                        value is not None
                        for value in (
                            config.upstream_wallet_cost_usd,
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
                # Legacy rows without a stored fingerprint remain visible for
                # explicit confirmation, but must not be mutated or auto-bound.
                continue
            remote_url = _remote_base_url(remote)
            configured_url: str | None = None
            if config is not None and config.api_endpoint_url:
                try:
                    configured_url = canonicalize_upstream_url(config.api_endpoint_url)
                except ValueError:
                    configured_url = None
            auto_assign_allowed = config is None or not config.upstream_auto_assign_disabled
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
                    channel = Upstream(
                        display_name=self._default_name(canonical_url),
                        api_endpoint_url=canonical_url,
                        platform_type="auto",
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
                config.api_endpoint_url = canonical_url or config.api_endpoint_url
                config.upstream_id = channel.id if channel is not None else None
                db.add(config)
                configs[account_id] = config
                changed = True
            else:
                known_secrets = self._known_secrets(config, channel)
                stable_binding_fingerprint = (
                    self.accounts._remote_binding_fingerprint(remote)
                )
                if (
                    stable_binding_fingerprint is not None
                    and config.remote_identity_fingerprint != stable_binding_fingerprint
                ):
                    # Upgrade legacy bindings that included the mutable account
                    # name. The numeric ID and creation timestamp remain stable.
                    config.remote_identity_fingerprint = stable_binding_fingerprint
                    changed = True
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
                    self.accounts.archive_data_before_invalidation(
                        db,
                        config,
                        reason="account_endpoint_changed",
                        channel=stored_channel,
                    )
                    priority_membership_changed = (
                        priority_membership_changed
                        or config.priority_interval_id is not None
                    )
                    config.upstream_id = channel.id
                    config.api_endpoint_url = canonical_url
                    config.platform_type = channel.platform_type
                    config.resolved_platform_type = channel.resolved_platform_type
                    config.upstream_user_id = channel.upstream_user_id
                    config.encrypted_access_token = None
                    config.encrypted_api_key = None
                    config.api_key_origin_rebind_required = True
                    config.selected_group_id = None
                    config.selected_group_name = None
                    config.upstream_group_multiplier_override = None
                    config.upstream_recharge_multiplier_override = channel.upstream_recharge_multiplier_override
                    config.group_options = channel.group_options
                    config.last_applied_at = None
                    self._invalidate_account(
                        config,
                        preserve_pause_ownership=True,
                    )
                    config.upstream_identity_rebind_required = True
                    config.last_error = "The upstream endpoint changed; rediscovery is required."
                    changed = True
                elif (
                    config.upstream_id is None
                    and channel is not None
                    and not config.upstream_auto_assign_disabled
                ):
                    config.upstream_id = channel.id
                    auto_assigned_config_ids.add(config.id)
                    changed = True
                config.remote_platform = _safe_text(self.sub2api.account_platform(remote), limit=64)
                config.remote_account_type = _safe_text(self.sub2api.account_type(remote), limit=32)
                previous_management_billing_multiplier = config.management_billing_multiplier
                self.accounts.apply_remote_snapshot(
                    config,
                    remote,
                    secrets=known_secrets,
                )
                if previous_management_billing_multiplier is not None:
                    # Inventory refreshes the display snapshot, while discovery
                    # owns the confirmed baseline used to detect rate changes.
                    config.management_billing_multiplier = previous_management_billing_multiplier

        target_record_bindings: dict[
            tuple[int, int], list[ApiAccount]
        ] = {}
        for config in configs.values():
            if (
                config.upstream_id is not None
                and config.remote_upstream_api_key_id is not None
            ):
                target_record_bindings.setdefault(
                    (config.upstream_id, config.remote_upstream_api_key_id),
                    [],
                ).append(config)
        for (_upstream_id, record_id), candidates in target_record_bindings.items():
            if len(candidates) < 2:
                continue
            for config in candidates:
                if config.id in auto_assigned_config_ids:
                    config.upstream_id = None
                    config.upstream_auto_assign_disabled = True
                config.remote_upstream_api_key_id = None
                config.upstream_identity_rebind_required = True
                config.last_error = (
                    f"Multiple local accounts have duplicate upstream API key record "
                    f"#{record_id} for the same channel; explicit rebind is required."
                )
            changed = True

        await db.commit()

        channel_result = await db.execute(
            select(Upstream).where(Upstream.deleted_at.is_(None))
        )
        channels_by_id = {item.id: item for item in channel_result.scalars().all()}
        config_result = await db.execute(select(ApiAccount))
        configs = {item.management_account_id: item for item in config_result.scalars().all()}
        return remote_by_id, configs, channels_by_id, priority_membership_changed

    async def _management_recharge(self) -> tuple[float | None, str | None, str]:
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
        config: ApiAccount,
        channel: Upstream | None,
        *,
        management_recharge: float | None,
        local_source: str | None,
        local_status: str,
        priority_interval_name: str | None = None,
        priority_interval: UpstreamPriorityInterval | None = None,
    ) -> ApiAccountOut:
        projected_expected_management_billing_multiplier = config.expected_management_billing_multiplier
        if channel is not None:
            group = _decimal_multiplier(config.upstream_group_multiplier)
            recharge = _decimal_multiplier(channel.upstream_recharge_multiplier)
            local = _decimal_multiplier(management_recharge)
            target = (
                _calculate_expected_management_billing_multiplier(group, recharge, local)
                if group and recharge and local
                else None
            )
            projected_expected_management_billing_multiplier = float(target) if target is not None else None
        base = self.accounts._build_out(
            remote,
            config,
            extra_secrets=self._known_secrets(config, channel),
            upstream_name=channel.display_name if channel is not None else None,
            priority_interval_name=priority_interval_name,
            priority_interval=priority_interval,
        )
        projected_would_change: bool | None = None
        if base.management_billing_multiplier is not None and projected_expected_management_billing_multiplier is not None:
            current = _decimal_multiplier(base.management_billing_multiplier, allow_zero=True)
            target = _decimal_multiplier(projected_expected_management_billing_multiplier, allow_zero=True)
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
                    "upstream_id": None,
                    "upstream_name": None,
                    "management_recharge_multiplier": management_recharge,
                    "management_recharge_source": local_source,
                    "management_recharge_status": local_status,
                }
            )
        return base.model_copy(
            update={
                **health_update,
                "upstream_id": channel.id,
                "upstream_name": channel.display_name,
                "api_endpoint_url": channel.api_endpoint_url,
                "platform_type": channel.platform_type,
                "resolved_platform_type": channel.resolved_platform_type,
                "upstream_user_id": channel.upstream_user_id,
                "access_token_set": bool(channel.encrypted_access_token),
                "upstream_recharge_multiplier_override": channel.upstream_recharge_multiplier_override,
                "group_options": self._group_options_out(channel),
                "discovered_upstream_recharge_multiplier": channel.discovered_upstream_recharge_multiplier,
                "upstream_recharge_multiplier": channel.upstream_recharge_multiplier,
                "recharge_multiplier_source": channel.recharge_multiplier_source,
                "recharge_multiplier_status": channel.recharge_multiplier_status,
                "management_recharge_multiplier": management_recharge,
                "management_recharge_source": local_source,
                "management_recharge_status": local_status,
                "expected_management_billing_multiplier": projected_expected_management_billing_multiplier,
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
        dict[int, ApiAccount],
        dict[str, Upstream],
    ]:
        if remote_by_id is None:
            remote_by_id = {
                account_id: account
                for account in await self.accounts.cached_remote_accounts(db)
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        with db.no_autoflush:
            config_result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.deleted_at.is_(None)
                )
            )
            configs = {
                item.management_account_id: item for item in config_result.scalars().all()
            }
            channel_result = await db.execute(
                select(Upstream).where(Upstream.deleted_at.is_(None))
            )
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
    def _group_options_out(channel: Upstream) -> list[UpstreamGroupOptionOut]:
        return [UpstreamGroupOptionOut(**item) for item in _sanitize_group_options(channel.group_options)]

    def _channel_out(
        self,
        channel: Upstream,
        accounts: list[ApiAccountOut],
    ) -> UpstreamOut:
        sorted_accounts = sorted(
            accounts,
            key=lambda item: (
                item.upstream_actual_multiplier is None,
                item.upstream_actual_multiplier or 0,
                item.management_account_id,
            ),
        )
        effective_recharge = _decimal_multiplier(channel.upstream_recharge_multiplier)
        using_cached_recharge = effective_recharge is None
        if effective_recharge is None:
            effective_recharge = _decimal_multiplier(channel.last_known_recharge_multiplier)
        effective_recharge_value = (
            float(effective_recharge) if effective_recharge is not None else None
        )
        actual_balance_cny = None
        if (
            channel.wallet_balance_usd is not None
            and effective_recharge_value is not None
        ):
            actual_balance_cny = (
                channel.wallet_balance_usd * effective_recharge_value
            )
        today_upstream_actual_cost_cny = None
        if (
            channel.today_upstream_wallet_cost_usd is not None
            and effective_recharge_value is not None
        ):
            today_upstream_actual_cost_cny = (
                channel.today_upstream_wallet_cost_usd * effective_recharge_value
            )
        yesterday_upstream_actual_cost_cny = None
        if (
            channel.yesterday_upstream_wallet_cost_usd is not None
            and effective_recharge_value is not None
        ):
            yesterday_upstream_actual_cost_cny = (
                channel.yesterday_upstream_wallet_cost_usd * effective_recharge_value
            )
        if not channel.id:
            channel.id = str(uuid4())
        monitors = channel.upstream_monitors if isinstance(channel.upstream_monitors, list) else []
        return UpstreamOut(
            upstream_id=channel.id,
            background_discovery_pending=bool(
                self._background_discovery_counts.get(channel.id, 0)
            ),
            display_name=channel.display_name,
            api_endpoint_url=channel.api_endpoint_url,
            management_url=channel.management_url,
            platform_type=channel.platform_type if channel.platform_type in {"auto", "newapi", "sub2api"} else "auto",
            probe_enabled=bool(channel.probe_enabled),
            resolved_platform_type=(
                channel.resolved_platform_type
                if channel.resolved_platform_type in {"newapi", "sub2api"}
                else None
            ),
            upstream_user_id=channel.upstream_user_id,
            access_token_set=bool(channel.encrypted_access_token),
            refresh_token_set=bool(channel.encrypted_refresh_token),
            login_credentials_set=bool(
                channel.encrypted_login_username and channel.encrypted_login_password
            ),
            upstream_recharge_multiplier_override=channel.upstream_recharge_multiplier_override,
            upstream_monitor_test_models=(
                dict(channel.upstream_monitor_test_models)
                if isinstance(channel.upstream_monitor_test_models, dict)
                else {}
            ),
            group_options=self._group_options_out(channel),
            discovered_upstream_recharge_multiplier=channel.discovered_upstream_recharge_multiplier,
            upstream_recharge_multiplier=effective_recharge_value,
            recharge_multiplier_source=(
                channel.recharge_multiplier_source
                if not using_cached_recharge
                else "cached" if effective_recharge_value is not None else None
            ),
            recharge_multiplier_status=(
                channel.recharge_multiplier_status
                if not using_cached_recharge
                else "stale" if effective_recharge_value is not None else channel.recharge_multiplier_status
            ),
            wallet_balance_usd=channel.wallet_balance_usd,
            wallet_total_usd=channel.wallet_total_usd,
            wallet_used_usd=channel.wallet_used_usd,
            balance_unit=channel.balance_unit,
            balance_status=channel.balance_status,
            balance_source=channel.balance_source,
            balance_message=channel.balance_message,
            balance_checked_at=channel.balance_checked_at,
            actual_balance_cny=actual_balance_cny,
            balance_guard_state=channel.balance_guard_state,
            balance_guard_basis=channel.balance_guard_basis,
            balance_guard_value=channel.balance_guard_value,
            balance_guard_checked_at=channel.balance_guard_checked_at,
            balance_guard_paused_count=channel.balance_guard_paused_count,
            today_upstream_wallet_cost_usd=channel.today_upstream_wallet_cost_usd,
            today_balance_unit=channel.today_balance_unit,
            today_balance_status=channel.today_balance_status,
            today_balance_checked_at=channel.today_balance_checked_at,
            today_upstream_actual_cost_cny=today_upstream_actual_cost_cny,
            yesterday_upstream_wallet_cost_usd=channel.yesterday_upstream_wallet_cost_usd,
            yesterday_upstream_actual_cost_cny=yesterday_upstream_actual_cost_cny,
            yesterday_balance_unit=channel.yesterday_balance_unit,
            yesterday_balance_status=channel.yesterday_balance_status,
            yesterday_balance_checked_at=channel.yesterday_balance_checked_at,
            upstream_monitors=monitors,
            upstream_monitor_count=max(channel.upstream_monitor_count, len(monitors)),
            upstream_monitor_status=channel.upstream_monitor_status,
            upstream_monitor_message=channel.upstream_monitor_message,
            upstream_monitor_checked_at=channel.upstream_monitor_checked_at,
            upstream_monitor_guard_state=channel.upstream_monitor_guard_state,
            upstream_monitor_unavailable_count=min(
                100,
                max(0, int(channel.upstream_monitor_unavailable_count or 0)),
            ),
            upstream_monitor_recovery_count=min(
                100,
                max(0, int(channel.upstream_monitor_recovery_count or 0)),
            ),
            upstream_monitor_guard_checked_at=channel.upstream_monitor_guard_checked_at,
            last_error=channel.last_error,
            last_discovered_at=channel.last_discovered_at,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
            account_count=len(sorted_accounts),
            accounts=sorted_accounts,
        )

    async def _sync_normalized_entities(
        self,
        db: AsyncSession,
        channels_by_id: dict[str, Upstream],
        configs: Iterable[ApiAccount],
    ) -> None:
        now = _utcnow()
        group_rows = list((await db.execute(select(UpstreamGroup))).scalars())
        groups_by_identity = {
            (item.upstream_id, item.remote_group_id): item for item in group_rows
        }
        visible_group_keys: set[tuple[str, str]] = set()
        for channel in channels_by_id.values():
            for option in _sanitize_group_options(channel.group_options):
                remote_group_id = str(option.get("id") or "").strip()
                name = str(option.get("name") or "").strip()
                if not remote_group_id or not name:
                    continue
                key = (channel.id, remote_group_id)
                visible_group_keys.add(key)
                row = groups_by_identity.get(key)
                if row is None:
                    row = UpstreamGroup(
                        upstream_id=channel.id,
                        remote_group_id=remote_group_id,
                        name=name,
                    )
                    db.add(row)
                    groups_by_identity[key] = row
                row.name = name
                multiplier = _decimal_multiplier(option.get("multiplier"))
                row.multiplier = float(multiplier) if multiplier is not None else None
                row.available = True
                row.last_seen_at = now
                row.deleted_at = None
        for key, row in groups_by_identity.items():
            if key[0] in channels_by_id and key not in visible_group_keys:
                row.available = False
                row.deleted_at = row.deleted_at or now
        await db.flush()

        key_rows = list((await db.execute(select(UpstreamApiKey))).scalars())
        keys_by_identity = {
            (item.upstream_id, item.remote_key_id): item for item in key_rows
        }
        visible_key_keys: set[tuple[str, int]] = set()
        for config in configs:
            if config.upstream_id not in channels_by_id or config.remote_upstream_api_key_id is None:
                continue
            identity = (config.upstream_id, int(config.remote_upstream_api_key_id))
            visible_key_keys.add(identity)
            row = keys_by_identity.get(identity)
            if row is None:
                row = UpstreamApiKey(
                    upstream_id=identity[0],
                    remote_key_id=identity[1],
                )
                db.add(row)
                keys_by_identity[identity] = row
            row.remote_name = config.remote_name
            row.status = config.upstream_key_status or "not_checked"
            row.last_seen_at = now
            row.deleted_at = None
            selected_group_id = str(config.selected_group_id or "").strip()
            group = groups_by_identity.get((identity[0], selected_group_id))
            row.group_id = group.id if group is not None else None
            await db.flush()
            config.upstream_api_key_id = row.id
            config.last_seen_at = now
        for key, row in keys_by_identity.items():
            if key[0] in channels_by_id and key not in visible_key_keys:
                row.deleted_at = row.deleted_at or now
        await db.commit()

    async def overview(
        self,
        db: AsyncSession,
        *,
        sync_inventory: bool = True,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
        management_recharge_snapshot: tuple[float | None, str | None, str] | None = None,
    ) -> UpstreamOverviewOut:
        cached_only = not sync_inventory and remote_by_id is None
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
        if not cached_only:
            management_recharge, local_source, local_status = (
                management_recharge_snapshot
                if management_recharge_snapshot is not None
                else await self._management_recharge()
            )
            if sync_inventory:
                local_cache_changed = False
                for config in configs.values():
                    if (
                        config.management_recharge_multiplier != management_recharge
                        or config.management_recharge_source != local_source
                        or config.management_recharge_status != local_status
                    ):
                        config.management_recharge_multiplier = management_recharge
                        config.management_recharge_source = local_source
                        config.management_recharge_status = local_status
                        local_cache_changed = True
                if local_cache_changed:
                    await db.commit()
        else:
            cached_local = next(
                (
                    config
                    for config in configs.values()
                    if config.management_recharge_multiplier is not None
                    or config.management_recharge_status not in {None, "not_checked"}
                ),
                None,
            )
            management_recharge = (
                cached_local.management_recharge_multiplier if cached_local is not None else None
            )
            local_source = cached_local.management_recharge_source if cached_local is not None else None
            local_status = (
                cached_local.management_recharge_status
                if cached_local is not None and cached_local.management_recharge_status
                else "not_checked"
            )
        priority_intervals = await self._priority_service().list_intervals(db)
        priority_interval_result = await db.execute(select(UpstreamPriorityInterval))
        priority_intervals_by_id = {
            interval.id: interval for interval in priority_interval_result.scalars().all()
        }
        priority_interval_names = {item.id: item.name for item in priority_intervals}
        assign_disabled_globally = False
        try:
            assign_disabled_globally = bool(
                await get_runtime_config_service().get_priority_assign_disabled_api_key_accounts()
            )
        except Exception:
            pass
        grouped: dict[str, list[ApiAccountOut]] = {upstream_id: [] for upstream_id in channels_by_id}
        unassigned: list[ApiAccountOut] = []
        for account_id in sorted(remote_by_id):
            config = configs[account_id]
            if self.accounts._config_binding_status(remote_by_id[account_id], config) != "bound":
                channel = channels_by_id.get(config.upstream_id) if config.upstream_id is not None else None
                account = self.accounts._build_out(
                    remote_by_id[account_id],
                    config,
                    extra_secrets=self._known_secrets(config, channel),
                    upstream_name=channel.display_name if channel is not None else None,
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
                                "upstream_id": channel.id,
                                "upstream_name": channel.display_name,
                            }
                        )
                    )
                continue
            channel = channels_by_id.get(config.upstream_id) if config.upstream_id is not None else None
            account = self._project_account(
                remote_by_id[account_id],
                config,
                channel,
                management_recharge=management_recharge,
                local_source=local_source,
                local_status=local_status,
                priority_interval_name=priority_interval_names.get(
                    config.priority_interval_id or 0
                ),
                priority_interval=priority_intervals_by_id.get(config.priority_interval_id),
            )
            account = account.model_copy(
                update={
                    "upstream_id": channel.id if channel is not None else None,
                    "priority_assignment_when_disabled_effective": (
                        config.priority_assignment_when_disabled
                        if config.priority_assignment_when_disabled is not None
                        else assign_disabled_globally
                    )
                }
            )
            if channel is None:
                unassigned.append(account)
            else:
                grouped[channel.id].append(account)
        channels = [
            self._channel_out(channel, grouped.get(channel.id, []))
            for channel in sorted(channels_by_id.values(), key=lambda item: (item.display_name.casefold(), item.id))
        ]
        if sync_inventory:
            await self._sync_normalized_entities(
                db,
                channels_by_id,
                configs.values(),
            )
        return UpstreamOverviewOut(
            management_recharge_multiplier=management_recharge,
            management_recharge_source=local_source,
            management_recharge_status=local_status,
            upstreams=channels,
            unassigned_accounts=sorted(
                unassigned,
                key=lambda item: (
                    item.upstream_actual_multiplier is None,
                    item.upstream_actual_multiplier or 0,
                    item.management_account_id,
                ),
            ),
            priority_intervals=priority_intervals,
        )

    async def delete_channel(self, db: AsyncSession, upstream_id: str) -> None:
        # Keep the same lock order as inventory updates so an account cannot be
        # assigned to the channel while its final emptiness check is running.
        async with self._inventory_lock:
            lock = await self._lock_for(upstream_id)
            async with lock:
                channel = await self._load_channel(db, upstream_id)
                remote_by_id = {
                    account_id: account
                    for account in await self.accounts._remote_accounts()
                    if (account_id := self.accounts._numeric_remote_id(account)) is not None
                }
                config_result = await db.execute(
                    select(ApiAccount).where(
                        ApiAccount.upstream_id == upstream_id
                    )
                )
                configs = list(config_result.scalars().all())
                has_current_config = any(
                    config.management_account_id in remote_by_id for config in configs
                )
                has_unsynced_origin_account = any(
                    _remote_base_url(account) == channel.api_endpoint_url
                    for account in remote_by_id.values()
                )
                if has_current_config or has_unsynced_origin_account:
                    raise ApiAccountServiceError(
                        "An upstream channel with API key accounts cannot be deleted.",
                        status_code=409,
                    )

                deleted_at = _utcnow()
                for config in configs:
                    config.deleted_at = deleted_at
                    config.remote_present = False
                    config.remote_missing_at = config.remote_missing_at or deleted_at
                group_rows = list(
                    (
                        await db.execute(
                            select(UpstreamGroup).where(
                                UpstreamGroup.upstream_id == upstream_id,
                                UpstreamGroup.deleted_at.is_(None),
                            )
                        )
                    ).scalars()
                )
                key_rows = list(
                    (
                        await db.execute(
                            select(UpstreamApiKey).where(
                                UpstreamApiKey.upstream_id == upstream_id,
                                UpstreamApiKey.deleted_at.is_(None),
                            )
                        )
                    ).scalars()
                )
                for row in (*group_rows, *key_rows):
                    row.deleted_at = deleted_at
                channel.archived_api_endpoint_url = channel.api_endpoint_url
                channel.api_endpoint_url = f"archived://deleted/{channel.id}"
                channel.deleted_at = deleted_at
                await db.commit()

    async def update_channel(
        self,
        db: AsyncSession,
        upstream_id: str,
        payload: UpstreamUpdate,
    ) -> UpstreamOut:
        # Channel URL changes and inventory auto-creation share the same unique
        # canonical URL namespace, so their check-and-commit sections must not
        # overlap.
        async with self._inventory_lock:
            await self._update_channel_locked(db, upstream_id, payload)
        rate_fields = {
            "api_endpoint_url",
            "management_url",
            "platform_type",
            "upstream_user_id",
            "access_token",
            "clear_access_token",
            "refresh_token",
            "clear_refresh_token",
            "login_username",
            "login_password",
            "clear_login_credentials",
            "upstream_recharge_multiplier_override",
        }
        if payload.model_fields_set & rate_fields:
            await self._rebalance_priorities_best_effort(db)
        overview = await self.overview(db)
        result = next(
            (item for item in overview.upstreams if item.upstream_id == upstream_id),
            None,
        )
        if result is None:
            raise ApiAccountServiceError(
                "The upstream channel has no API key accounts.",
                status_code=409,
            )
        return result

    async def _update_channel_locked(
        self,
        db: AsyncSession,
        upstream_id: str,
        payload: UpstreamUpdate,
    ) -> None:
        lock = await self._lock_for(upstream_id)
        async with lock:
            self._ensure_secret_storage_ready(payload)
            channel = await self._load_channel(db, upstream_id)
            fields = payload.model_fields_set
            current_token = decrypt_text(channel.encrypted_access_token)
            current_refresh_token = decrypt_text(channel.encrypted_refresh_token)
            current_login_username = decrypt_text(channel.encrypted_login_username)
            current_login_password = decrypt_text(channel.encrypted_login_password)
            previous_canonical_origin = upstream_url_origin(channel.api_endpoint_url)
            previous_management_origin = upstream_url_origin(
                channel.management_url or channel.api_endpoint_url
            )
            identity_changed = False
            identity_metadata_changed = False
            credential_changed = False
            manual_multiplier_changed = False
            base_url_changed = False

            if "api_endpoint_url" in fields:
                if payload.api_endpoint_url is None:
                    raise ApiAccountServiceError(
                        "The upstream channel URL is required.", status_code=422
                    )
                canonical_url = canonicalize_upstream_url(payload.api_endpoint_url)
                collision = await db.execute(
                    select(Upstream).where(
                        Upstream.api_endpoint_url == canonical_url,
                        Upstream.id != upstream_id,
                    )
                )
                if collision.scalar_one_or_none() is not None:
                    raise ApiAccountServiceError(
                        "Another upstream channel already uses this URL.", status_code=409
                    )
                base_url_changed = canonical_url != channel.api_endpoint_url
                identity_metadata_changed = base_url_changed
                channel.api_endpoint_url = canonical_url
            if "management_url" in fields:
                management_base_url = payload.management_url
                management_url_changed = management_base_url != channel.management_url
                identity_changed = identity_changed or management_url_changed
                identity_metadata_changed = identity_metadata_changed or management_url_changed
                channel.management_url = management_base_url
            if "display_name" in fields:
                channel.display_name = payload.display_name or self._default_name(channel.api_endpoint_url)
            if "platform_type" in fields:
                upstream_type_changed = payload.platform_type != channel.platform_type
                identity_changed = identity_changed or upstream_type_changed
                identity_metadata_changed = identity_metadata_changed or upstream_type_changed
                channel.platform_type = payload.platform_type
            if "probe_enabled" in fields and payload.probe_enabled is not None:
                channel.probe_enabled = payload.probe_enabled
            if "upstream_user_id" in fields:
                upstream_user_changed = payload.upstream_user_id != channel.upstream_user_id
                identity_changed = identity_changed or upstream_user_changed
                identity_metadata_changed = identity_metadata_changed or upstream_user_changed
                channel.upstream_user_id = payload.upstream_user_id
            if payload.clear_access_token:
                access_token_changed = current_token is not None
                identity_changed = identity_changed or access_token_changed
                credential_changed = credential_changed or access_token_changed
                channel.encrypted_access_token = None
            elif payload.access_token:
                access_token_changed = payload.access_token != current_token
                identity_changed = identity_changed or access_token_changed
                credential_changed = credential_changed or access_token_changed
                channel.encrypted_access_token = encrypt_text(payload.access_token)
            if payload.clear_refresh_token:
                refresh_token_changed = current_refresh_token is not None
                identity_changed = identity_changed or refresh_token_changed
                credential_changed = credential_changed or refresh_token_changed
                channel.encrypted_refresh_token = None
            elif payload.refresh_token:
                refresh_token_changed = payload.refresh_token != current_refresh_token
                identity_changed = identity_changed or refresh_token_changed
                credential_changed = credential_changed or refresh_token_changed
                channel.encrypted_refresh_token = encrypt_text(payload.refresh_token)
            if payload.clear_login_credentials:
                login_credentials_changed = bool(
                    current_login_username is not None or current_login_password is not None
                )
                credential_changed = credential_changed or login_credentials_changed
                channel.encrypted_login_username = None
                channel.encrypted_login_password = None
            elif payload.login_username is not None and payload.login_password is not None:
                login_credentials_changed = (
                    payload.login_username != current_login_username
                    or payload.login_password != current_login_password
                )
                credential_changed = credential_changed or login_credentials_changed
                channel.encrypted_login_username = encrypt_text(payload.login_username)
                channel.encrypted_login_password = encrypt_text(payload.login_password)
            if "upstream_recharge_multiplier_override" in fields:
                manual_multiplier_changed = (
                    payload.upstream_recharge_multiplier_override
                    != channel.upstream_recharge_multiplier_override
                )
                channel.upstream_recharge_multiplier_override = payload.upstream_recharge_multiplier_override
            if "upstream_monitor_test_models" in fields:
                channel.upstream_monitor_test_models = dict(
                    payload.upstream_monitor_test_models or {}
                )

            next_canonical_origin = upstream_url_origin(channel.api_endpoint_url)
            next_management_origin = upstream_url_origin(
                channel.management_url or channel.api_endpoint_url
            )
            canonical_origin_changed = next_canonical_origin != previous_canonical_origin
            if (
                (
                    canonical_origin_changed
                    or next_management_origin != previous_management_origin
                )
                and not payload.confirm_credential_rebind
            ):
                raise ApiAccountServiceError(
                    "Changing the upstream origin requires explicit credential rebind confirmation.",
                    status_code=409,
                )

            linked_configs_result = await db.execute(
                select(ApiAccount).where(
                    ApiAccount.upstream_id == upstream_id
                )
            )
            linked_configs = list(linked_configs_result.scalars().all())
            if base_url_changed and linked_configs:
                failed_upstream_id = channel.id
                failed_management_account_ids = [
                    config.management_account_id for config in linked_configs
                ]
                try:
                    await self._sync_linked_account_base_urls(
                        linked_configs,
                        channel.api_endpoint_url,
                    )
                except ApiAccountServiceError as exc:
                    await db.rollback()
                    try:
                        await record_event(
                            db,
                            "upstream_url_update_failed",
                            "Upstream URL update failed; local state was not changed.",
                            details={
                                "upstream_id": failed_upstream_id,
                                "management_account_ids": failed_management_account_ids,
                                "rollback_incomplete": "incomplete" in exc.public_message.lower(),
                            },
                        )
                    except Exception:
                        await db.rollback()
                        logger.warning(
                            "Could not persist the upstream URL update failure event.",
                            exc_info=True,
                        )
                    raise
            # Rotating credentials does not change the upstream identity. Keep
            # the last known channel metrics visible until the next discovery
            # finishes; URL/type/user changes still invalidate them immediately.
            credential_only_change = (
                credential_changed
                and not identity_metadata_changed
                and not manual_multiplier_changed
            )
            discovery_context_changed = identity_changed or base_url_changed
            if discovery_context_changed:
                for config in linked_configs:
                    self.accounts.archive_data_before_invalidation(
                        db,
                        config,
                        reason=(
                            "upstream_endpoint_changed"
                            if base_url_changed and not identity_changed
                            else "upstream_id_changed"
                        ),
                        channel=channel,
                    )
                if channel.balance_guard_episode_id:
                    await NotificationService(db).cancel_unsent(
                        dedupe_key=self._balance_guard_notification_key(channel)
                    )
                if not credential_only_change:
                    self._invalidate_channel(channel)
                else:
                    # The cached wallet remains useful after a token rotation,
                    # but an old guard episode must not carry over to the new
                    # credential and suppress a fresh evaluation.
                    channel.balance_guard_state = "not_checked"
                    channel.balance_guard_basis = None
                    channel.balance_guard_value = None
                    channel.balance_guard_checked_at = None
                    channel.balance_guard_episode_id = None
                    channel.balance_guard_paused_count = 0
            account_values: dict[str, Any] = {
                "api_endpoint_url": channel.api_endpoint_url,
                "platform_type": channel.platform_type,
                "upstream_user_id": channel.upstream_user_id,
                "encrypted_access_token": channel.encrypted_access_token,
                "upstream_recharge_multiplier_override": (
                    channel.upstream_recharge_multiplier_override
                ),
            }
            if identity_changed or "upstream_recharge_multiplier_override" in fields:
                account_values.update(
                    discovered_upstream_group_multiplier=None,
                    upstream_group_multiplier=None,
                    group_multiplier_source=None,
                    group_multiplier_status="not_discovered",
                    expected_management_billing_multiplier=None,
                    desired_priority=None,
                    last_discovered_at=None,
                )
            if discovery_context_changed:
                account_values.update(
                    upstream_wallet_cost_usd=None,
                    upstream_usage_unit=None,
                    upstream_usage_checked_at=None,
                )
            if base_url_changed:
                account_values["upstream_auto_assign_disabled"] = True
            if canonical_origin_changed and payload.confirm_credential_rebind:
                account_values["api_key_origin_rebind_required"] = False
            if identity_changed:
                # The old upstream identity no longer proves that any existing
                # automatic pause reason is valid. Resolve those reasons while
                # retaining ownership long enough for a healthy probe to restore
                # accounts that the plugin had already paused.
                for config in linked_configs:
                    self._invalidate_account(
                        config,
                        preserve_pause_ownership=True,
                    )
            # A URL change is the operation that repairs stale endpoint
            # bindings, so every local config linked to this channel must be
            # projected even when its remote endpoint still has the old URL.
            bound_configs = (
                linked_configs
                if base_url_changed
                else await self._bound_configs(db, upstream_id)
            )
            if bound_configs:
                await db.execute(
                    update(ApiAccount)
                    .where(
                        ApiAccount.id.in_(
                            [config.id for config in bound_configs]
                        )
                    )
                    .values(**account_values)
                    .execution_options(synchronize_session=False)
                )
                if identity_changed or "upstream_recharge_multiplier_override" in fields:
                    await db.execute(
                        update(ApiAccount)
                        .where(
                            ApiAccount.id.in_(
                                [config.id for config in bound_configs]
                            ),
                            ApiAccount.priority_interval_id.is_not(None),
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
    def _selected_group(config: ApiAccount, groups: list[dict[str, Any]]) -> dict[str, Any] | None:
        selected_id = (config.selected_group_id or "").strip().casefold()
        selected_name = (config.selected_group_name or "").strip().casefold()
        if selected_id:
            return next(
                (
                    group
                    for group in groups
                    if str(group.get("id") or "").strip().casefold() == selected_id
                ),
                None,
            )
        if selected_name:
            return next(
                (
                    group
                    for group in groups
                    if str(group.get("name") or "").strip().casefold() == selected_name
                ),
                None,
            )
        return None

    def _apply_daily_usage_failure(
        self,
        channel: Upstream,
        period: str,
        *,
        status: str,
        reason: str,
        time_zone: str,
        now: datetime,
        log_failure: bool = True,
    ) -> None:
        amount_field = (
            "today_upstream_wallet_cost_usd"
            if period == "today"
            else "yesterday_upstream_wallet_cost_usd"
        )
        decision = daily_usage_failure_decision(
            status=status,
            reason=reason,
            cached_amount=_balance_number(getattr(channel, amount_field, None)),
            checked_at=getattr(channel, f"{period}_balance_checked_at", None),
            time_zone=time_zone,
            fallback_time_zone=DEFAULT_TODAY_TIME_ZONE,
            now=now,
        )
        retained = decision.retain_cached_value
        setattr(
            channel,
            f"{period}_balance_status",
            "stale" if retained else status,
        )
        if not retained:
            setattr(channel, amount_field, None)
            setattr(channel, f"{period}_balance_unit", None)
            setattr(channel, f"{period}_balance_checked_at", None)
        if log_failure and decision.should_log:
            logger.warning(
                "Upstream daily usage probe unavailable: upstream_id=%s "
                "period=%s status=%s reason=%s retained=%s",
                channel.id,
                period,
                status,
                decision.normalized_reason or "unknown",
                retained,
            )

    def _apply_discovery_to_channel(
        self,
        channel: Upstream,
        result: Any,
        *,
        refresh_upstream_monitors: bool = True,
        time_zone: str = DEFAULT_TODAY_TIME_ZONE,
        log_usage_failures: bool = True,
    ) -> bool:
        now = _utcnow()
        status = str(_value(result, "status") or "error").strip().lower()
        token_invalid = bool(_value(result, "sub2api_auth_rejected"))
        if status == "insecure_url":
            raise ApiAccountServiceError(
                "Credentials may only be sent to an HTTPS upstream URL.", status_code=422
            )
        if status != "ok":
            cached_recharge = _decimal_multiplier(channel.upstream_recharge_multiplier)
            if cached_recharge is None:
                cached_recharge = _decimal_multiplier(channel.last_known_recharge_multiplier)
                if cached_recharge is not None:
                    channel.upstream_recharge_multiplier = float(cached_recharge)
                    channel.recharge_multiplier_source = channel.recharge_multiplier_source or "cached"
                    channel.recharge_multiplier_status = "stale"
            if cached_recharge is None:
                manual_recharge = _decimal_multiplier(channel.upstream_recharge_multiplier_override)
                if manual_recharge is not None:
                    channel.upstream_recharge_multiplier = float(manual_recharge)
                    channel.recharge_multiplier_source = "manual"
                    channel.recharge_multiplier_status = "fallback_manual"
                else:
                    channel.recharge_multiplier_status = "discovery_failed"
            channel.balance_status = (
                TOKEN_INVALID_STATUS
                if token_invalid
                else str(_value(result, "balance_status") or "error").strip().lower()
            )
            channel.balance_message = None if token_invalid else (
                _safe_text(_value(result, "balance_message"), limit=300)
                or "Unable to read the upstream channel."
            )
            for period in ("today", "yesterday"):
                self._apply_daily_usage_failure(
                    channel,
                    period,
                    status="error",
                    reason=(
                        _safe_text(
                            _value(result, f"{period}_balance_error"),
                            limit=80,
                        )
                        or f"overall_discovery_{status}"
                    ),
                    time_zone=time_zone,
                    now=now,
                    log_failure=log_usage_failures,
                )
            if refresh_upstream_monitors:
                self._apply_upstream_monitor_discovery(channel, result, now=now)
            channel.last_error = (
                TOKEN_INVALID_ERROR
                if token_invalid
                else "Upstream channel discovery failed."
            )
            channel.last_discovered_at = now
            return False

        resolved = str(_value(result, "platform_type") or "").strip().lower()
        if resolved in {"newapi", "sub2api"}:
            channel.resolved_platform_type = resolved
        channel.group_options = _sanitize_group_options(_value(result, "groups") or [])
        if refresh_upstream_monitors:
            self._apply_upstream_monitor_discovery(channel, result, now=now)

        discovered_recharge = _decimal_multiplier(
            _value(result, "discovered_upstream_recharge_multiplier")
        )
        recharge_probe_status = str(_value(result, "recharge_discovery_status") or "unknown").lower()
        manual_recharge = _decimal_multiplier(channel.upstream_recharge_multiplier_override)
        if discovered_recharge is not None:
            channel.discovered_upstream_recharge_multiplier = float(discovered_recharge)
            channel.upstream_recharge_multiplier = float(discovered_recharge)
            channel.recharge_multiplier_source = _safe_text(
                _value(result, "discovered_upstream_recharge_multiplier_source"),
                limit=128,
            ) or "auto"
            channel.recharge_multiplier_status = "ok"
        elif manual_recharge is not None:
            channel.discovered_upstream_recharge_multiplier = None
            channel.upstream_recharge_multiplier = float(manual_recharge)
            channel.recharge_multiplier_source = "manual"
            channel.recharge_multiplier_status = "fallback_manual"
        elif recharge_probe_status == "missing":
            channel.discovered_upstream_recharge_multiplier = None
            channel.upstream_recharge_multiplier = 1.0
            channel.recharge_multiplier_source = "default"
            channel.recharge_multiplier_status = "default_missing"
        else:
            channel.discovered_upstream_recharge_multiplier = None
            channel.upstream_recharge_multiplier = None
            channel.recharge_multiplier_source = None
            channel.recharge_multiplier_status = "discovery_failed"

        self._remember_channel_recharge_multiplier(channel)

        balance_status = str(_value(result, "balance_status") or "not_checked").strip().lower()
        channel.balance_status = balance_status
        channel.balance_message = _safe_text(_value(result, "balance_message"), limit=300)
        if balance_status in {"ok", "success", "available"}:
            for field in ("wallet_balance_usd", "wallet_total_usd", "wallet_used_usd"):
                value = _value(result, field)
                if value is not None:
                    parsed = _balance_number(value)
                    if parsed is not None:
                        setattr(channel, field, parsed)
            channel.balance_unit = _safe_text(_value(result, "balance_unit"), limit=32) or "USD"
            channel.balance_checked_at = now
            channel.balance_source = "upstream_wallet"
        for period in ("today", "yesterday"):
            amount_field = (
                "today_upstream_wallet_cost_usd"
                if period == "today"
                else "yesterday_upstream_wallet_cost_usd"
            )
            status = str(
                _value(result, f"{period}_balance_status") or "unsupported"
            ).strip().lower()
            if status != "ok":
                self._apply_daily_usage_failure(
                    channel,
                    period,
                    status=status,
                    reason=(
                        _safe_text(
                            _value(result, f"{period}_balance_error"),
                            limit=80,
                        )
                        or status
                    ),
                    time_zone=time_zone,
                    now=now,
                    log_failure=log_usage_failures,
                )
                continue
            used = _balance_number(_value(result, amount_field))
            if used is None or used < 0:
                self._apply_daily_usage_failure(
                    channel,
                    period,
                    status="error",
                    reason="invalid_amount",
                    time_zone=time_zone,
                    now=now,
                    log_failure=log_usage_failures,
                )
                continue
            setattr(channel, f"{period}_balance_status", "ok")
            setattr(channel, amount_field, used)
            setattr(
                channel,
                f"{period}_balance_unit",
                _safe_text(_value(result, f"{period}_balance_unit"), limit=32) or "USD",
            )
            setattr(channel, f"{period}_balance_checked_at", now)
        channel.last_error = None if balance_status in {"ok", "success", "available"} else channel.balance_message
        channel.last_discovered_at = now
        return True

    @staticmethod
    def _remember_channel_recharge_multiplier(channel: Upstream) -> float | None:
        """Return the latest reliable multiplier without treating a failed probe as a reset."""
        current = _decimal_multiplier(channel.upstream_recharge_multiplier)
        if current is not None:
            channel.last_known_recharge_multiplier = float(current)
            return float(current)
        remembered = _decimal_multiplier(channel.last_known_recharge_multiplier)
        return float(remembered) if remembered is not None else None

    @staticmethod
    def _set_discovery_value(result: Any, field: str, value: Any) -> None:
        if isinstance(result, dict):
            result[field] = value
        else:
            setattr(result, field, value)

    @staticmethod
    def _group_maps(
        groups: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        ambiguous_names: set[str] = set()
        for group in groups:
            group_id = str(group.get("id") or "").strip()
            group_name = str(group.get("name") or "").strip()
            if group_id:
                by_id[group_id.casefold()] = group
            if group_name:
                normalized_name = group_name.casefold()
                if normalized_name in by_name:
                    by_name.pop(normalized_name, None)
                    ambiguous_names.add(normalized_name)
                elif normalized_name not in ambiguous_names:
                    by_name[normalized_name] = group
        return by_id, by_name

    @staticmethod
    def _account_mapping_has(mapping: dict[Any, Any], account_id: Any) -> bool:
        if account_id in mapping or str(account_id) in mapping:
            return True
        try:
            numeric_id = int(account_id)
        except (TypeError, ValueError, OverflowError):
            return False
        return numeric_id in mapping

    def _stabilize_discovery_groups(
        self,
        channel: Upstream,
        result: Any,
        *,
        now: datetime,
    ) -> None:
        """Require two matching snapshots before accepting group removals."""

        if str(_value(result, "status") or "error").strip().lower() != "ok":
            return
        current_groups = _sanitize_group_options(_value(result, "groups") or [])
        previous_groups = _sanitize_group_options(channel.group_options)
        previous_by_id, _previous_by_name = self._group_maps(previous_groups)
        current_by_id, _current_by_name = self._group_maps(current_groups)
        missing_ids = frozenset(previous_by_id.keys() - current_by_id.keys())

        if not previous_groups or not missing_ids:
            channel.pending_group_options = None
            channel.pending_group_removal_count = 0
            channel.pending_group_removal_checked_at = None
            self._set_discovery_value(result, "groups", current_groups)
            return

        pending_groups = _sanitize_group_options(channel.pending_group_options)
        pending_by_id, _pending_by_name = self._group_maps(pending_groups)
        pending_missing_ids = frozenset(previous_by_id.keys() - pending_by_id.keys())
        pending_count = int(channel.pending_group_removal_count or 0)
        same_missing_set = pending_count >= 1 and pending_missing_ids == missing_ids
        if same_missing_set:
            channel.pending_group_options = None
            channel.pending_group_removal_count = 0
            channel.pending_group_removal_checked_at = None
            self._set_discovery_value(result, "groups", current_groups)
            return

        channel.pending_group_options = current_groups
        channel.pending_group_removal_count = 1
        channel.pending_group_removal_checked_at = now

        stable_groups = list(current_groups)
        stable_ids = {
            str(group.get("id") or "").strip().casefold() for group in stable_groups
        }
        protected_groups: list[dict[str, Any]] = []
        for group in previous_groups:
            group_id = str(group.get("id") or "").strip().casefold()
            if group_id and group_id not in stable_ids:
                protected_groups.append(group)
                stable_groups.append(group)
                stable_ids.add(group_id)
        self._set_discovery_value(result, "groups", stable_groups)

        protected_by_id, _protected_by_name = self._group_maps(protected_groups)
        _stable_by_id, stable_by_name = self._group_maps(stable_groups)
        protected_by_name = {
            group_name: group
            for group_name, group in stable_by_name.items()
            if str(group.get("id") or "").strip().casefold() in protected_by_id
        }
        raw_states = _value(result, "account_upstream_states")
        states = dict(raw_states) if isinstance(raw_states, dict) else {}
        raw_matches = _value(result, "account_group_matches")
        matches = dict(raw_matches) if isinstance(raw_matches, dict) else {}
        for account_id, state in list(states.items()):
            group_id = str(_value(state, "group_id") or "").strip().casefold()
            group_name = str(_value(state, "group_name") or "").strip().casefold()
            protected = (
                protected_by_id.get(group_id)
                if group_id
                else protected_by_name.get(group_name)
            )
            if protected is None:
                continue
            if isinstance(state, AccountUpstreamState):
                states[account_id] = replace(state, group_status="available")
            elif isinstance(state, dict):
                states[account_id] = {**state, "group_status": "available"}
            else:
                states[account_id] = AccountUpstreamState(
                    key_status=_value(state, "key_status"),
                    group_status="available",
                    group_id=_value(state, "group_id"),
                    group_name=_value(state, "group_name"),
                    usage_amount=_value(state, "usage_amount"),
                    usage_unit=_value(state, "usage_unit"),
                )
            if not self._account_mapping_has(matches, account_id):
                matches[account_id] = AccountGroupMatch(
                    id=str(protected["id"]),
                    name=str(protected["name"]),
                    multiplier=float(protected["multiplier"]),
                    source="stable_group_snapshot",
                )
        self._set_discovery_value(result, "account_upstream_states", states)
        self._set_discovery_value(result, "account_group_matches", matches)

    def _apply_stabilized_group_account_states(
        self,
        channel: Upstream,
        result: Any,
        configs: list[ApiAccount],
        *,
        previous_groups: list[dict[str, Any]],
    ) -> None:
        current_groups = _sanitize_group_options(_value(result, "groups") or [])
        previous_by_id, previous_by_name = self._group_maps(previous_groups)
        current_by_id, current_by_name = self._group_maps(current_groups)
        raw_states = _value(result, "account_upstream_states")
        states = dict(raw_states) if isinstance(raw_states, dict) else {}
        raw_matches = _value(result, "account_group_matches")
        matches = dict(raw_matches) if isinstance(raw_matches, dict) else {}

        if int(channel.pending_group_removal_count or 0) >= 1:
            pending_groups = _sanitize_group_options(channel.pending_group_options)
            pending_by_id, pending_by_name = self._group_maps(pending_groups)
            protected_by_id = {
                group_id: group
                for group_id, group in previous_by_id.items()
                if group_id not in pending_by_id
            }
            protected_by_name = {
                group_name: group
                for group_name, group in previous_by_name.items()
                if group_name not in pending_by_name
                and str(group.get("id") or "").strip().casefold() in protected_by_id
            }
            changed = False
            for config in configs:
                account_id = config.management_account_id
                if matches.get(account_id) is not None or matches.get(str(account_id)) is not None:
                    continue
                state_key: int | str = account_id if account_id in states else str(account_id)
                state = states.get(state_key)
                state_group_id = str(_value(state, "group_id") or "").strip().casefold()
                state_group_name = str(_value(state, "group_name") or "").strip().casefold()
                if state_group_id or state_group_name:
                    protected = (
                        protected_by_id.get(state_group_id)
                        if state_group_id
                        else protected_by_name.get(state_group_name)
                    )
                    if protected is None:
                        continue
                else:
                    selected_id = str(config.selected_group_id or "").strip().casefold()
                    selected_name = str(config.selected_group_name or "").strip().casefold()
                    protected = (
                        protected_by_id.get(selected_id)
                        if selected_id
                        else protected_by_name.get(selected_name)
                    )
                    if protected is None:
                        continue
                group_id = str(protected["id"])
                group_name = str(protected["name"])
                if isinstance(state, AccountUpstreamState):
                    states[state_key] = replace(state, group_status="available")
                elif isinstance(state, dict):
                    states[state_key] = {
                        **state,
                        "group_status": "available",
                        "group_id": group_id,
                        "group_name": group_name,
                    }
                else:
                    states[state_key] = AccountUpstreamState(
                        group_status="available",
                        group_id=group_id,
                        group_name=group_name,
                    )
                matches[state_key] = AccountGroupMatch(
                    id=group_id,
                    name=group_name,
                    multiplier=float(protected["multiplier"]),
                    source="stable_group_snapshot",
                )
                changed = True
            if changed:
                self._set_discovery_value(result, "account_upstream_states", states)
                self._set_discovery_value(result, "account_group_matches", matches)
            return

        removed_by_id = {
            group_id: group
            for group_id, group in previous_by_id.items()
            if group_id not in current_by_id
        }
        removed_by_name = {
            group_name: group
            for group_name, group in previous_by_name.items()
            if group_name not in current_by_name
            and str(group.get("id") or "").strip().casefold() in removed_by_id
        }
        if not removed_by_id and not any(
            config.upstream_group_status == "deleted" for config in configs
        ):
            return
        changed = False
        for config in configs:
            account_id = config.management_account_id
            match = matches.get(account_id) or matches.get(str(account_id))
            if match is not None:
                continue
            state_key: int | str = account_id if account_id in states else str(account_id)
            state = states.get(state_key)
            state_group_id = str(_value(state, "group_id") or "").strip().casefold()
            state_group_name = str(_value(state, "group_name") or "").strip().casefold()
            removed: dict[str, Any] | None = None
            if state_group_id or state_group_name:
                removed = (
                    removed_by_id.get(state_group_id)
                    if state_group_id
                    else removed_by_name.get(state_group_name)
                )
            else:
                selected_id = str(config.selected_group_id or "").strip().casefold()
                selected_name = str(config.selected_group_name or "").strip().casefold()
                removed = (
                    removed_by_id.get(selected_id)
                    if selected_id
                    else removed_by_name.get(selected_name)
                )

            if removed is None and config.upstream_group_status == "deleted":
                retained_group_id = str(
                    _value(state, "group_id") or config.selected_group_id or ""
                ).strip()
                retained_group_name = str(
                    _value(state, "group_name") or config.selected_group_name or ""
                ).strip()
                normalized_retained_id = retained_group_id.casefold()
                normalized_retained_name = retained_group_name.casefold()
                group_is_current = bool(
                    normalized_retained_id in current_by_id
                    if normalized_retained_id
                    else normalized_retained_name in current_by_name
                )
                if not group_is_current and (
                    retained_group_id or retained_group_name
                ):
                    removed = {
                        "id": retained_group_id,
                        "name": retained_group_name,
                    }
            if removed is None:
                continue

            group_id = str(_value(state, "group_id") or removed.get("id") or "").strip() or None
            group_name = str(
                _value(state, "group_name") or removed.get("name") or ""
            ).strip() or None
            if isinstance(state, AccountUpstreamState):
                states[state_key] = replace(
                    state,
                    group_status="deleted",
                    group_id=group_id,
                    group_name=group_name,
                )
            elif isinstance(state, dict):
                states[state_key] = {
                    **state,
                    "group_status": "deleted",
                    "group_id": group_id,
                    "group_name": group_name,
                }
            else:
                states[state_key] = AccountUpstreamState(
                    group_status="deleted",
                    group_id=group_id,
                    group_name=group_name,
                )
            changed = True
        if changed:
            self._set_discovery_value(result, "account_upstream_states", states)

    @staticmethod
    def _apply_upstream_monitor_discovery(
        channel: Upstream,
        result: Any,
        *,
        now: datetime,
        upstream_monitor_detail_ids: set[int] | None = None,
    ) -> None:
        status = str(_value(result, "status") or "error").strip().lower()
        if status != "ok":
            token_invalid = bool(_value(result, "sub2api_auth_rejected"))
            channel.upstream_monitor_status = (
                TOKEN_INVALID_STATUS if token_invalid else "error"
            )
            channel.upstream_monitor_message = None if token_invalid else (
                "Upstream discovery failed before channel monitors could be read."
            )
            channel.upstream_monitor_checked_at = now
            return
        raw_monitors = _value(result, "upstream_monitors")
        discovered_monitors = (
            [dict(item) for item in raw_monitors if isinstance(item, dict)]
            if isinstance(raw_monitors, list)
            else []
        )
        if upstream_monitor_detail_ids is not None:
            cached_by_id: dict[int, dict[str, Any]] = {}
            for cached in channel.upstream_monitors or []:
                if not isinstance(cached, dict):
                    continue
                try:
                    cached_id = int(cached.get("id") or 0)
                except (TypeError, ValueError):
                    continue
                if cached_id > 0:
                    cached_by_id[cached_id] = cached
            detail_fields = (
                "primary_status",
                "primary_latency_ms",
                "primary_ping_latency_ms",
                "availability_7d",
                "availability_window",
                "extra_models",
                "timeline",
            )
            merged_monitors: list[dict[str, Any]] = []
            for discovered in discovered_monitors:
                try:
                    monitor_id = int(discovered.get("id") or 0)
                except (TypeError, ValueError):
                    monitor_id = 0
                cached = cached_by_id.get(monitor_id)
                if cached is None or monitor_id in upstream_monitor_detail_ids:
                    merged_monitors.append(discovered)
                    continue
                merged = {**cached, **discovered}
                for field in detail_fields:
                    if field in cached:
                        merged[field] = cached[field]
                merged_monitors.append(merged)
            discovered_monitors = merged_monitors
        channel.upstream_monitors = discovered_monitors
        raw_monitor_total = _value(result, "upstream_monitors_total")
        try:
            parsed_monitor_total = int(raw_monitor_total)
        except (TypeError, ValueError):
            parsed_monitor_total = len(channel.upstream_monitors)
        channel.upstream_monitor_count = max(
            len(channel.upstream_monitors),
            min(max(parsed_monitor_total, 0), 1_000_000),
        )
        channel.upstream_monitor_status = str(
            _value(result, "upstream_monitors_status") or "not_checked"
        ).strip().lower()
        channel.upstream_monitor_message = _safe_text(
            _value(result, "upstream_monitors_message"), limit=300
        )
        channel.upstream_monitor_checked_at = now

    @staticmethod
    def _monitor_timeline_timestamp(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @classmethod
    def _current_monitor_status(cls, monitor: dict[str, Any]) -> str:
        latest_status = ""
        latest_at: datetime | None = None
        timeline = monitor.get("timeline")
        if isinstance(timeline, list):
            for point in timeline:
                if not isinstance(point, dict):
                    continue
                checked_at = cls._monitor_timeline_timestamp(
                    point.get("checked_at", point.get("time"))
                )
                status = str(point.get("status") or "").strip().lower()
                if checked_at is None or not status:
                    continue
                if latest_at is None or checked_at >= latest_at:
                    latest_at = checked_at
                    latest_status = status
        if latest_status:
            return latest_status
        return str(monitor.get("primary_status") or "").strip().lower() or "unknown"

    async def _test_account_connection_candidates(
        self,
        configs: list[ApiAccount],
        model: str,
        attempts: int,
        *,
        attempt_interval_seconds: float = 0,
        require_all_success: bool = False,
    ) -> tuple[bool | None, str | None, int | None, int]:
        candidates = sorted(
            {
                config.management_account_id: config
                for config in configs
                if config.remote_schedulable is not False
            }.values(),
            key=lambda item: item.management_account_id,
        )
        if not candidates:
            candidates = sorted(configs, key=lambda item: item.management_account_id)
        if not candidates or not model:
            return None, "No API Key account or test model is configured.", None, 0

        tester = getattr(self.sub2api, "test_account_connection", None)
        if not callable(tester):
            return None, "The local Sub2API client does not support connection tests.", None, 0

        try:
            bounded_interval = float(attempt_interval_seconds)
        except (TypeError, ValueError):
            bounded_interval = 0.0
        if not 0 < bounded_interval <= 300:
            bounded_interval = 0.0
        bounded_attempts = max(1, min(5, attempts))
        last_error: str | None = None
        last_account_id: int | None = None
        completed_attempts = 0
        for index in range(bounded_attempts):
            config = candidates[index % len(candidates)]
            last_account_id = config.management_account_id
            completed_attempts += 1
            try:
                async with asyncio.timeout(30.0):
                    success, error = await tester(
                        str(config.management_account_id),
                        model,
                    )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                success, error = False, "Connection test timed out."
            except Exception as exc:
                success = False
                redactor = getattr(self.sub2api, "redact_error_text", None)
                error = redactor(exc) if callable(redactor) else None
            if success and not require_all_success:
                return True, None, last_account_id, completed_attempts
            if not success:
                last_error = _safe_text(error, limit=300) or "Connection test failed."
            if bounded_interval and index + 1 < bounded_attempts:
                await asyncio.sleep(bounded_interval)
        if require_all_success and last_error is None:
            return True, None, last_account_id, completed_attempts
        return False, last_error, last_account_id, completed_attempts

    async def _prepare_upstream_monitor_guard(
        self,
        channel: Upstream,
        runtime_config: Any,
        *,
        monitor_probe_fresh: bool = True,
    ) -> str | None:
        try:
            configured = bool(
                await runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled()
            )
            automation_paused = bool(await runtime_config.get_automation_paused())
        except Exception:
            channel.upstream_monitor_guard_state = "unknown"
            channel.upstream_monitor_guard_checked_at = channel.upstream_monitor_checked_at
            return None

        channel.upstream_monitor_guard_checked_at = channel.upstream_monitor_checked_at
        if automation_paused:
            channel.upstream_monitor_guard_state = "automation_paused"
            return None
        if not configured:
            channel.upstream_monitor_guard_state = "disabled"
            channel.upstream_monitor_unavailable_count = 0
            channel.upstream_monitor_recovery_count = 0
            return "clear"

        channel.upstream_monitor_unavailable_count = 0
        channel.upstream_monitor_recovery_count = 0
        # Monitor rows represent separate upstream groups/model routes. They
        # cannot be combined into a site-wide availability result.
        channel.upstream_monitor_guard_state = "account_scoped"
        return None

    @staticmethod
    def _available_model_ids(config: ApiAccount) -> set[str]:
        return {
            str(item.get("id") or "").strip()
            for item in (config.available_models or [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }

    def _availability_test_blocker(
        self,
        config: ApiAccount,
        remote: dict[str, Any] | None,
    ) -> str | None:
        for hold in self.accounts.active_pause_holds(config):
            if hold.reason != AUTO_PAUSE_REASON_MONITOR:
                return hold.reason
        if (
            remote is not None
            and self.sub2api.account_schedulable(remote) is False
            and not config.pause_owned_by_plugin
        ):
            return "manual_disabled"
        return None

    async def _account_availability_test_model(
        self,
        config: ApiAccount,
        runtime_config: Any,
    ) -> tuple[str, str | None]:
        configured_model = str(config.availability_test_model or "").strip()[:160]
        if config.available_models is None:
            return "", "The API Key account model whitelist has not been synchronized."
        available_model_ids = self._available_model_ids(config)
        if configured_model:
            if configured_model in available_model_ids:
                return configured_model, None
            return "", "The selected test model is not in this API Key account's available model whitelist."
        get_models = getattr(runtime_config, "get_upstream_monitor_fallback_test_models", None)
        if callable(get_models):
            configured_models = list(await get_models() or [])
        else:
            configured_models = [
                str(await runtime_config.get_upstream_monitor_fallback_test_model() or "")
            ]
        configured_models = [
            str(item or "").strip()[:160]
            for item in configured_models
            if str(item or "").strip()
        ]
        if not configured_models:
            return "", "No fallback test model chain is configured."
        for model in configured_models:
            if model in available_model_ids:
                return model, None
        return "", "None of the fallback test models are in this API Key account's available model whitelist."

    async def _prepare_account_monitor_guard(
        self,
        config: ApiAccount,
        channel: Upstream,
        runtime_config: Any,
        *,
        automation_paused: bool,
        blocking_pause_reason: str | None = None,
        monitor_probe_fresh: bool = True,
        force_unbound_fallback: bool = False,
    ) -> tuple[str | None, dict[str, Any]]:
        recovering = any(
            hold.reason == AUTO_PAUSE_REASON_MONITOR
            for hold in self.accounts.active_pause_holds(config)
        )
        config.availability_unavailable_count = 0
        config.availability_recovery_count = 0
        try:
            enabled = bool(
                await runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled()
            )
            attempts = max(
                1,
                min(
                    5,
                    int(
                        await (
                            runtime_config.get_upstream_monitor_recovery_test_attempts()
                            if recovering
                            else runtime_config.get_upstream_monitor_fallback_test_attempts()
                        )
                    ),
                ),
            )
            get_attempt_interval = getattr(
                runtime_config,
                "get_upstream_monitor_test_attempt_interval_seconds",
                None,
            )
            raw_attempt_interval = (
                await get_attempt_interval()
                if callable(get_attempt_interval)
                else 0
            )
            try:
                attempt_interval_seconds = float(raw_attempt_interval)
            except (TypeError, ValueError):
                attempt_interval_seconds = 0.0
            if not 0 <= attempt_interval_seconds <= 300:
                attempt_interval_seconds = 0.0
            get_require_all_success = getattr(
                runtime_config,
                "get_api_key_availability_all_tests_must_succeed",
                None,
            )
            require_all_success = (
                bool(await get_require_all_success())
                if callable(get_require_all_success)
                else False
            )
        except Exception:
            config.availability_status = "unknown"
            config.availability_source = None
            config.availability_message = "Availability settings could not be read."
            return None, {"mode": config.availability_check_mode, "status": "unknown"}

        if not enabled and not force_unbound_fallback:
            config.availability_status = "disabled"
            config.availability_source = None
            config.availability_unavailable_count = 0
            config.availability_recovery_count = 0
            config.availability_message = None
            return "clear", {"mode": config.availability_check_mode, "status": "disabled"}
        mode = (
            config.availability_check_mode
            if config.availability_check_mode in {
                "upstream_monitor",
                "independent_model",
                "disabled",
            }
            else "disabled"
        )
        if mode == "disabled":
            config.availability_status = "disabled"
            config.availability_source = None
            config.availability_unavailable_count = 0
            config.availability_recovery_count = 0
            config.availability_message = None
            return "clear", {"mode": mode, "status": "disabled"}
        if automation_paused:
            config.availability_status = "automation_paused"
            return None, {"mode": config.availability_check_mode, "status": "automation_paused"}
        evidence: dict[str, Any] = {
            "mode": mode,
            "test_purpose": "recovery" if recovering else "pause",
            "max_test_attempts": attempts,
            "test_attempt_interval_seconds": attempt_interval_seconds,
            "test_success_policy": "all" if require_all_success else "any",
        }

        def paused_by_other_reason() -> tuple[None, dict[str, Any]]:
            # An unrelated automatic hold pauses only scheduled connection tests.
            # Keep the last observed result available for the UI and manual tests.
            evidence.update(status="automatic_test_paused", blocked_by=blocking_pause_reason)
            return None, evidence

        def action_for_result(status: str) -> str | None:
            if not enabled:
                return "clear"
            return "clear" if status == "available" else "hold"

        if mode == "independent_model":
            if blocking_pause_reason:
                return paused_by_other_reason()
            try:
                model, configuration_error = await self._account_availability_test_model(
                    config,
                    runtime_config,
                )
            except Exception:
                model, configuration_error = "", "Availability settings could not be read."
            now = _utcnow()
            config.availability_checked_at = now
            config.availability_source = "independent_model"
            if configuration_error:
                config.availability_status = "not_configured"
                config.availability_message = configuration_error
                evidence.update(status="not_configured", model=None)
                return None, evidence
            success, error, account_id, completed = await self._test_account_connection_candidates(
                [config],
                model,
                attempts,
                attempt_interval_seconds=attempt_interval_seconds,
                require_all_success=require_all_success,
            )
            evidence.update(
                model=model or None,
                test_attempts=completed,
                test_account_id=account_id,
                test_status=(
                    "available" if success is True else "unavailable" if success is False else "unknown"
                ),
            )
            if success is True:
                config.availability_status = "available"
                config.availability_message = None
                evidence["status"] = "available"
                return action_for_result("available"), evidence
            if success is False:
                config.availability_status = "unavailable"
                config.availability_message = _safe_text(error, limit=300)
                evidence["status"] = "unavailable"
                return action_for_result("unavailable"), evidence
            config.availability_status = "unknown"
            config.availability_message = _safe_text(error, limit=300)
            evidence["status"] = "unknown"
            return None, evidence

        if not monitor_probe_fresh:
            evidence["status"] = "cached"
            return None, evidence

        now = channel.upstream_monitor_checked_at or _utcnow()
        config.availability_checked_at = now
        config.availability_source = "upstream_monitor"
        monitors = [
            item
            for item in (channel.upstream_monitors or [])
            if isinstance(item, dict)
        ]
        selected_monitor: dict[str, Any] | None = None
        fallback_reason: str
        if config.availability_monitor_id is None:
            fallback_reason = "No concrete upstream monitor panel is bound."
            evidence.update(
                monitor_id=None,
                monitor_status="not_configured",
                monitor_source="upstream_monitor",
            )
            get_unbound_fallback = getattr(
                runtime_config,
                "get_upstream_monitor_fallback_without_monitor_enabled",
                None,
            )
            allow_unbound_fallback = force_unbound_fallback or (
                bool(await get_unbound_fallback())
                if callable(get_unbound_fallback)
                else False
            )
            evidence["fallback_without_monitor_enabled"] = allow_unbound_fallback
            if not allow_unbound_fallback:
                config.availability_status = "not_configured"
                config.availability_message = (
                    f"{fallback_reason} Fallback testing for unbound accounts is disabled."
                )
                evidence.update(status="not_configured", model=None)
                return None, evidence
        elif channel.upstream_monitor_status not in {"ok", "degraded"}:
            fallback_reason = (
                _safe_text(channel.upstream_monitor_message, limit=300)
                or "The upstream channel monitor result is unavailable."
            )
            evidence.update(
                monitor_id=config.availability_monitor_id,
                monitor_status="unknown",
                monitor_source="upstream_monitor",
            )
        else:
            selected_monitor = next(
                (
                    item
                    for item in monitors
                    if int(item.get("id") or 0) == config.availability_monitor_id
                ),
                None,
            )
            if selected_monitor is None:
                fallback_reason = "The configured upstream monitor no longer exists."
                evidence.update(
                    monitor_id=config.availability_monitor_id,
                    monitor_status="not_configured",
                    monitor_source="upstream_monitor",
                )
            else:
                monitor_status = self._current_monitor_status(selected_monitor)
                monitor_name = _safe_text(selected_monitor.get("name"), limit=160)
                evidence.update(
                    monitor_id=config.availability_monitor_id,
                    monitor_name=monitor_name,
                    monitor_status=monitor_status,
                    monitor_source="upstream_monitor",
                )
                if monitor_status in {
                    "available",
                    "degraded",
                    "healthy",
                    "operational",
                    "ok",
                    "success",
                }:
                    config.availability_status = "available"
                    config.availability_message = None
                    evidence["status"] = "available"
                    return action_for_result("available"), evidence
                fallback_reason = (
                    "The configured upstream monitor reported an unavailable status."
                    if monitor_status in {
                        "unavailable",
                        "error",
                        "failed",
                        "timeout",
                    }
                    else "The configured upstream monitor has no usable latest status."
                )

        evidence["fallback_reason"] = fallback_reason

        if blocking_pause_reason:
            return paused_by_other_reason()

        try:
            model, configuration_error = await self._account_availability_test_model(
                config,
                runtime_config,
            )
        except Exception:
            model, configuration_error = "", "Availability settings could not be read."
        if configuration_error:
            config.availability_status = "not_configured"
            config.availability_message = f"{fallback_reason} {configuration_error}"
            evidence.update(status="not_configured", model=None)
            return None, evidence

        success, error, account_id, completed = await self._test_account_connection_candidates(
            [config],
            model,
            attempts,
            attempt_interval_seconds=attempt_interval_seconds,
            require_all_success=require_all_success,
        )
        config.availability_checked_at = _utcnow()
        config.availability_source = "upstream_monitor_fallback"
        evidence.update(
            model=model,
            test_attempts=completed,
            test_account_id=account_id,
            test_status=(
                "available" if success is True else "unavailable" if success is False else "unknown"
            ),
        )
        if success is True:
            config.availability_status = "available"
            if require_all_success:
                config.availability_message = (
                    f"{fallback_reason} All {completed} fallback connection tests "
                    f"succeeded with model {model}."
                )
            else:
                config.availability_message = (
                    f"{fallback_reason} Fallback connection test succeeded with model "
                    f"{model} after {completed} attempt(s)."
                )
            evidence["status"] = "available"
            return action_for_result("available"), evidence
        if success is False:
            config.availability_status = "unavailable"
            config.availability_message = _safe_text(error, limit=300)
            evidence["status"] = "unavailable"
            return action_for_result("unavailable"), evidence
        config.availability_status = "unknown"
        config.availability_message = _safe_text(error, limit=300)
        evidence["status"] = "unknown"
        return None, evidence

    async def test_account_availability(
        self,
        db: AsyncSession,
        account_id: int,
        expected_identity_fingerprint: str,
    ) -> ApiAccountAvailabilityTestOut:
        """Run one account availability round with the automatic policy semantics."""

        remote = await self.accounts._remote_account(
            account_id,
            expected_identity_fingerprint,
        )
        config = await db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == account_id
            )
        )
        self.accounts._require_config_binding(remote, config)
        if config is None or config.upstream_id is None:
            raise ApiAccountServiceError(
                "The API Key account is not assigned to an upstream.",
                status_code=409,
            )
        upstream_id = config.upstream_id
        monitor_refresh_status = "not_required"
        if (
            config.availability_check_mode == "upstream_monitor"
            and config.availability_monitor_id is not None
        ):
            try:
                async with self._discover_all_lock:
                    await self._discover_channel(
                        db,
                        upstream_id,
                        sync_inventory=False,
                        remote_by_id={account_id: remote},
                        include_upstream_monitor_details=True,
                        upstream_monitor_detail_ids={config.availability_monitor_id},
                        monitor_details_only=True,
                    )
                monitor_refresh_status = "refreshed"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                monitor_refresh_status = "failed"
                await db.rollback()
                channel = await self._load_channel(db, upstream_id)
                channel.upstream_monitor_status = "error"
                channel.upstream_monitor_message = (
                    exc.public_message
                    if isinstance(exc, ApiAccountServiceError)
                    else "Unable to refresh the upstream channel monitor."
                )
                channel.upstream_monitor_checked_at = _utcnow()
                await db.commit()

        channel_lock = await self._lock_for(upstream_id)
        account_lock = await self.accounts._lock_for(account_id)
        async with channel_lock:
            async with account_lock:
                remote = await self.accounts._remote_account(
                    account_id,
                    expected_identity_fingerprint,
                )
                config = await db.scalar(
                    select(ApiAccount)
                    .where(ApiAccount.management_account_id == account_id)
                    .execution_options(populate_existing=True)
                )
                self.accounts._require_config_binding(remote, config)
                if config is None or config.upstream_id != upstream_id:
                    raise ApiAccountServiceError(
                        "The API Key account upstream changed during the test.",
                        status_code=409,
                    )
                channel = await self._load_channel(db, upstream_id)
                runtime_config = get_runtime_config_service()
                try:
                    automation_paused = bool(
                        await runtime_config.get_automation_paused()
                    )
                except Exception:
                    automation_paused = True

                previous_holds = self.accounts.active_pause_holds(config)
                previous_pause_reason = (
                    config.auto_disabled_reason
                    or (previous_holds[0].reason if previous_holds else None)
                )
                policy_action, evidence = await self._prepare_account_monitor_guard(
                    config,
                    channel,
                    runtime_config,
                    # Global/other policy pauses apply only to scheduled tests.
                    # A user-triggered test must still report the current result.
                    automation_paused=False,
                    blocking_pause_reason=None,
                    monitor_probe_fresh=True,
                    force_unbound_fallback=True,
                )
                evidence["manual_test"] = True
                evidence["monitor_refresh_status"] = monitor_refresh_status
                now = config.availability_checked_at or _utcnow()
                if policy_action is not None:
                    self.accounts.set_pause_hold(
                        config,
                        AUTO_PAUSE_REASON_MONITOR,
                        active=policy_action == "hold",
                        scope_upstream_id=channel.id,
                        recovery_mode="account_availability_healthy",
                        now=now,
                        evidence=evidence,
                    )

                active_holds = self.accounts.active_pause_holds(config)
                pause_action_reason = (
                    active_holds[0].reason
                    if active_holds
                    else previous_pause_reason
                )
                (
                    remote,
                    _old_remote_schedulable,
                    _new_remote_schedulable,
                    policy_status,
                    policy_error,
                ) = await self.accounts.reconcile_automatic_pause(
                    db,
                    remote,
                    config,
                    upstream_id=channel.id,
                    upstream_name=channel.display_name,
                    pause_action_reason=pause_action_reason,
                    mutations_allowed=not automation_paused,
                )
                if policy_error is not None:
                    evidence["policy_error"] = policy_error
                self.accounts.apply_remote_snapshot(
                    config,
                    remote,
                    secrets=self._known_secrets(config, channel),
                )
                await db.commit()
                await db.refresh(config)

                priority_interval = (
                    await db.get(UpstreamPriorityInterval, config.priority_interval_id)
                    if config.priority_interval_id is not None
                    else None
                )
                account = self._project_account(
                    remote,
                    config,
                    channel,
                    management_recharge=config.management_recharge_multiplier,
                    local_source=config.management_recharge_source,
                    local_status=config.management_recharge_status or "not_checked",
                    priority_interval_name=(
                        priority_interval.name if priority_interval is not None else None
                    ),
                    priority_interval=priority_interval,
                )
        await self._rebalance_priorities_best_effort(
            db,
            account_ids={account_id},
        )
        return ApiAccountAvailabilityTestOut(
            account=account,
            policy_action=policy_action,
            policy_status=policy_status,
            policy_error=policy_error,
            evidence=evidence,
        )

    async def test_account_connection(
        self,
        db: AsyncSession,
        account_id: int,
        expected_identity_fingerprint: str,
    ) -> ApiAccountConnectionTestOut:
        """Test one account through Sub2API without changing any automation state."""

        remote = await self.accounts._remote_account(
            account_id,
            expected_identity_fingerprint,
        )
        config = await db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == account_id
            )
        )
        self.accounts._require_config_binding(remote, config)
        if config is None:
            raise ApiAccountServiceError(
                "The API Key account is not managed by this service.",
                status_code=409,
            )

        runtime_config = get_runtime_config_service()
        model, model_error = await self._account_availability_test_model(
            config,
            runtime_config,
        )
        if model_error is not None:
            raise ApiAccountServiceError(model_error, status_code=409)

        tester = getattr(self.sub2api, "test_account_connection", None)
        if not callable(tester):
            raise ApiAccountServiceError(
                "The local Sub2API client does not support connection tests.",
                status_code=501,
            )
        try:
            async with asyncio.timeout(30.0):
                success, error = await tester(str(account_id), model)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            success, error = False, "Connection test timed out."
        except Exception as exc:
            success = False
            redactor = getattr(self.sub2api, "redact_error_text", None)
            error = redactor(exc) if callable(redactor) else None

        return ApiAccountConnectionTestOut(
            account_id=account_id,
            success=bool(success),
            model=model,
            error=None if success else _safe_text(error, limit=300) or "Connection test failed.",
        )

    @staticmethod
    def _authoritative_upstream_multiplier(
        group_multiplier: Any,
        group_status: Any,
        group_source: Any,
        recharge_multiplier: Any,
        recharge_status: Any,
        recharge_source: Any,
    ) -> float | None:
        if str(group_status or "").strip().lower() != "ok":
            return None
        if str(recharge_status or "").strip().lower() != "ok":
            return None
        group_source_value = str(group_source or "").strip().lower()
        recharge_source_value = str(recharge_source or "").strip().lower()
        if not group_source_value or not recharge_source_value:
            return None
        if "manual" in group_source_value or "manual" in recharge_source_value:
            return None
        if group_source_value == "default" or recharge_source_value == "default":
            return None
        return UpstreamService._upstream_multiplier(
            group_multiplier,
            recharge_multiplier,
        )

    def _update_rate_pause_hold(
        self,
        config: ApiAccount,
        channel: Upstream,
        *,
        enabled: bool | None,
        automation_paused: bool,
        absolute_threshold: float,
        current_multiplier: float | None,
        now: datetime,
    ) -> None:
        if automation_paused or enabled is None:
            return
        if not enabled:
            self.accounts.set_pause_hold(
                config,
                AUTO_PAUSE_REASON_RATE,
                active=False,
                scope_upstream_id=channel.id,
                recovery_mode="rate_within_threshold",
                now=now,
            )
            return
        if current_multiplier is None:
            return

        current = _decimal_multiplier(current_multiplier)
        if current is None:
            return
        boundary = Decimal(str(absolute_threshold))
        self.accounts.set_pause_hold(
            config,
            AUTO_PAUSE_REASON_RATE,
            active=current > boundary,
            scope_upstream_id=channel.id,
            recovery_mode="rate_at_or_below_absolute_threshold",
            now=now,
            evidence={
                "mode": "absolute_multiplier",
                "observed_multiplier": float(current),
                "absolute_threshold": absolute_threshold,
            },
        )

    def _clear_disabled_policy_holds(
        self,
        config: ApiAccount,
        channel: Upstream,
        *,
        balance_guard_action: str | None,
        monitor_guard_action: str | None,
        upstream_health_pause_enabled: bool | None,
        rate_pause_enabled: bool | None,
        automation_paused: bool,
        now: datetime,
    ) -> bool:
        if automation_paused:
            return False
        reasons: list[tuple[str, str]] = []
        if balance_guard_action == "clear":
            reasons.append((AUTO_PAUSE_REASON_BALANCE, "balance_at_or_above_threshold"))
        if monitor_guard_action == "clear":
            reasons.append((AUTO_PAUSE_REASON_MONITOR, "upstream_monitor_healthy"))
        if upstream_health_pause_enabled is False:
            reasons.extend(
                (
                    (AUTO_PAUSE_REASON_KEY, "upstream_healthy"),
                    (AUTO_PAUSE_REASON_GROUP, "upstream_healthy"),
                )
            )
        if rate_pause_enabled is False:
            reasons.append((AUTO_PAUSE_REASON_RATE, "rate_within_threshold"))

        changed = False
        for reason, recovery_mode in reasons:
            changed = self.accounts.set_pause_hold(
                config,
                reason,
                active=False,
                scope_upstream_id=channel.id,
                recovery_mode=recovery_mode,
                now=now,
            ) or changed
        if changed:
            self.accounts.sync_pause_compatibility_fields(config)
        return changed

    async def _fetch_api_key_balance_fallback(
        self,
        configs: list[ApiAccount],
    ) -> dict[str, Any] | None:
        for config in sorted(configs, key=lambda item: item.management_account_id)[:10]:
            try:
                result = await self.sub2api.get_account_balance(config.management_account_id)
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
                result.get("remaining", result.get("wallet_balance_usd", result.get("balance")))
            )
            if remaining is None:
                continue
            return dict(result)
        return None

    @staticmethod
    def _apply_api_key_balance_fallback(
        channel: Upstream,
        result: dict[str, Any] | None,
    ) -> bool:
        if channel.balance_status in {"ok", "success", "available"}:
            return True
        if not isinstance(result, dict):
            return False
        remaining = _balance_number(
            result.get("remaining", result.get("wallet_balance_usd", result.get("balance")))
        )
        if remaining is None:
            return False
        channel.wallet_balance_usd = remaining
        channel.wallet_total_usd = _balance_number(
            result.get("total", result.get("wallet_total_usd"))
        )
        channel.wallet_used_usd = _balance_number(
            result.get("used", result.get("wallet_used_usd"))
        )
        channel.balance_unit = _safe_text(result.get("unit"), limit=32) or "USD"
        channel.balance_status = "ok"
        channel.balance_source = "local_api_key"
        channel.balance_message = "API Key balance reported through the local sub2api account."
        channel.balance_checked_at = _utcnow()
        return True

    def _derive_account(
        self,
        config: ApiAccount,
        channel: Upstream,
        *,
        management_recharge: float | None,
        local_source: str | None,
        local_status: str,
    ) -> None:
        groups = _sanitize_group_options(channel.group_options)
        matched = self._selected_group(config, groups)
        manual_group = _decimal_multiplier(config.upstream_group_multiplier_override)
        if matched is not None:
            group = _decimal_multiplier(matched.get("multiplier"))
            config.discovered_upstream_group_multiplier = float(group) if group is not None else None
            config.upstream_group_multiplier = float(group) if group is not None else None
            config.group_multiplier_source = _safe_text(matched.get("source"), limit=128) or "upstream"
            config.group_multiplier_status = "ok" if group is not None else "invalid"
            config.selected_group_id = str(matched.get("id"))
            config.selected_group_name = str(matched.get("name"))
        elif manual_group is not None:
            config.discovered_upstream_group_multiplier = None
            config.upstream_group_multiplier = float(manual_group)
            config.group_multiplier_source = "manual"
            config.group_multiplier_status = "fallback_manual"
        else:
            config.discovered_upstream_group_multiplier = None
            config.upstream_group_multiplier = None
            config.group_multiplier_source = None
            config.group_multiplier_status = "group_selection_missing" if groups else "unavailable"

        group = _decimal_multiplier(config.upstream_group_multiplier)
        recharge = _decimal_multiplier(channel.upstream_recharge_multiplier)
        local = _decimal_multiplier(management_recharge)
        target = (
            _calculate_expected_management_billing_multiplier(group, recharge, local)
            if group and recharge and local
            else None
        )
        config.expected_management_billing_multiplier = float(target) if target is not None else None
        config.management_recharge_multiplier = management_recharge
        config.management_recharge_source = local_source
        config.management_recharge_status = local_status
        config.last_discovered_at = channel.last_discovered_at
        config.last_error = None if target is not None else "Select an upstream group before applying a billing rate."

        # Keep the compatibility projection populated during the transition.
        config.api_endpoint_url = channel.api_endpoint_url
        config.platform_type = channel.platform_type
        config.resolved_platform_type = channel.resolved_platform_type
        config.upstream_user_id = channel.upstream_user_id
        config.encrypted_access_token = channel.encrypted_access_token
        config.upstream_recharge_multiplier_override = channel.upstream_recharge_multiplier_override
        config.group_options = channel.group_options
        config.discovered_upstream_recharge_multiplier = channel.discovered_upstream_recharge_multiplier
        config.upstream_recharge_multiplier = channel.upstream_recharge_multiplier
        config.recharge_multiplier_source = channel.recharge_multiplier_source
        config.recharge_multiplier_status = channel.recharge_multiplier_status
        config.wallet_balance_usd = channel.wallet_balance_usd
        config.wallet_total_usd = channel.wallet_total_usd
        config.wallet_used_usd = channel.wallet_used_usd
        config.balance_unit = channel.balance_unit
        config.balance_status = channel.balance_status
        config.balance_source = channel.balance_source
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

    async def _prepare_balance_guard(
        self,
        db: AsyncSession | None,
        channel: Upstream,
        runtime_config: Any,
        *,
        persist_notifications: bool = True,
    ) -> str | None:
        try:
            configured = bool(
                await runtime_config.get_api_key_auto_pause_on_negative_balance_enabled()
            )
            automation_paused = bool(await runtime_config.get_automation_paused())
            basis = await runtime_config.get_upstream_negative_balance_basis()
            threshold_getter = getattr(
                runtime_config,
                "get_upstream_balance_pause_threshold",
                None,
            )
            threshold = float(await threshold_getter()) if threshold_getter else 0.0
        except Exception:
            channel.balance_guard_state = "unavailable"
            channel.balance_guard_value = None
            channel.balance_guard_checked_at = _utcnow()
            return None

        channel.balance_guard_basis = basis
        channel.balance_guard_checked_at = _utcnow()
        if automation_paused:
            channel.balance_guard_state = "automation_paused"
            channel.balance_guard_value = None
            return None
        if not configured:
            if channel.balance_guard_episode_id:
                if persist_notifications and db is not None:
                    await NotificationService(db, runtime_config).cancel_unsent(
                        dedupe_key=self._balance_guard_notification_key(channel)
                    )
                channel.balance_guard_episode_id = None
            channel.balance_guard_state = "disabled"
            channel.balance_guard_value = None
            return "clear"
        if (
            channel.balance_source != "upstream_wallet"
            or channel.balance_status not in {"ok", "success", "available"}
            or channel.wallet_balance_usd is None
        ):
            channel.balance_guard_state = "unavailable"
            channel.balance_guard_value = None
            return None

        value = channel.wallet_balance_usd
        guard_unit = channel.balance_unit or "USD"
        if basis == "recharge_adjusted":
            multiplier = channel.upstream_recharge_multiplier
            if multiplier is None:
                channel.balance_guard_state = "unavailable"
                channel.balance_guard_value = None
                return None
            value *= multiplier
            guard_unit = "CNY"
        channel.balance_guard_value = value
        channel.balance_guard_checked_at = channel.balance_checked_at or _utcnow()
        if value < threshold:
            channel.balance_guard_state = "insufficient"
            if not channel.balance_guard_episode_id:
                channel.balance_guard_episode_id = uuid4().hex
            if persist_notifications and db is not None:
                await NotificationService(db, runtime_config).enqueue_if_enabled(
                    "upstream_balance_low",
                    self._balance_guard_notification_key(channel),
                    "Upstream channel balance is below the configured threshold",
                    (
                        f"{channel.display_name} balance is {value:.2f} "
                        f"{guard_unit}; threshold is {threshold:.2f}."
                    ),
                    {
                        "upstream_id": channel.id,
                        "upstream_name": channel.display_name,
                        "balance": value,
                        "basis": basis,
                        "threshold": threshold,
                        "unit": guard_unit,
                    },
                )
            return "hold"
        if value >= threshold:
            if channel.balance_guard_episode_id:
                if persist_notifications and db is not None:
                    await NotificationService(db, runtime_config).cancel_unsent(
                        dedupe_key=self._balance_guard_notification_key(channel)
                    )
                channel.balance_guard_episode_id = None
            channel.balance_guard_state = "healthy"
            return "clear"
        return None

    @staticmethod
    def _balance_guard_notification_key(channel: Upstream) -> str:
        return f"upstream-balance-low:{channel.id}:{channel.balance_guard_episode_id}"

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
        config: ApiAccount,
        channel: Upstream,
        *,
        previous_group_multiplier: float | None,
        previous_group_id: str | None,
        previous_group_name: str | None,
        previous_upstream_recharge_multiplier: float | None,
        previous_management_recharge_multiplier: float | None,
        previous_expected_management_billing_multiplier: float | None,
        sync_enabled: bool,
        health_transition: UpstreamHealthTransition,
        old_remote_schedulable: bool | None,
        new_remote_schedulable: bool | None,
        health_action_status: str | None,
        health_safe_error: str | None,
        pause_action_reason: str | None,
        current_remote: dict[str, Any],
        runtime_config: Any,
        rate_notification_events: list[dict[str, Any]],
        group_removed_in_discovery: bool = False,
    ) -> None:
        group_changed = not self._same_multiplier(
            previous_group_multiplier,
            config.upstream_group_multiplier,
        )
        group_identity_changed = bool(
            previous_group_id != config.selected_group_id
            or previous_group_name != config.selected_group_name
        )
        upstream_recharge_changed = not self._same_multiplier(
            previous_upstream_recharge_multiplier,
            channel.upstream_recharge_multiplier,
        )
        management_recharge_changed = not self._same_multiplier(
            previous_management_recharge_multiplier,
            config.management_recharge_multiplier,
        )
        target_changed = not self._same_multiplier(previous_expected_management_billing_multiplier, config.expected_management_billing_multiplier)
        rate_inputs_changed = (
            group_changed
            or group_identity_changed
            or upstream_recharge_changed
            or management_recharge_changed
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
        group_rate_change_reason = _account_group_rate_change_reason(
            previous_group_id=previous_group_id,
            current_group_id=config.selected_group_id,
            previous_group_name=previous_group_name,
            current_group_name=config.selected_group_name,
            multiplier_changed=group_changed,
        )
        old_upstream_multiplier = self._upstream_multiplier(
            previous_group_multiplier,
            previous_upstream_recharge_multiplier,
        )
        new_upstream_multiplier = self._upstream_multiplier(
            config.upstream_group_multiplier,
            channel.upstream_recharge_multiplier,
        )
        old_current = config.management_billing_multiplier
        observed_current = self.accounts._remote_management_billing_multiplier(current_remote)
        new_current = observed_current if observed_current is not None else old_current
        old_current_decimal = _decimal_multiplier(old_current, allow_zero=True)
        observed_current_decimal = _decimal_multiplier(observed_current, allow_zero=True)
        externally_changed = bool(
            old_current_decimal is not None
            and observed_current_decimal is not None
            and _quantize_rate(old_current_decimal) != _quantize_rate(observed_current_decimal)
        )
        status = health_action_status or "observed"
        safe_error: str | None = health_safe_error
        reason = (
            f"{pause_action_reason}_recovered"
            if health_action_status == "account_restored" and pause_action_reason
            else pause_action_reason
            if health_action_status is not None and pause_action_reason
            else "automatic_pause_restored"
            if health_action_status == "account_restored"
            else "upstream_auto_disable"
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
            else group_rate_change_reason
            if group_rate_change_reason is not None
            else "upstream_recharge_change"
            if upstream_recharge_changed
            else "management_recharge_change"
            if management_recharge_changed
            else "expected_management_billing_multiplier_recalculated"
            if target_changed
            else "rate_drift"
        )

        target = _decimal_multiplier(config.expected_management_billing_multiplier)
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
                    current = observed_current
                    config.management_billing_multiplier = current
                    new_current = current
                    current_decimal = _decimal_multiplier(current, allow_zero=True)
                    if current_decimal is None:
                        raise ValueError("invalid current rate")
                    if _quantize_rate(current_decimal) != _quantize_rate(target):
                        if not await self._automatic_rate_write_allowed():
                            status = "skipped"
                        else:
                            self.accounts._require_config_binding(current_remote, config)
                            # Release the SQLite writer before waiting for the
                            # remote mutation and verification round trip.
                            await db.flush()
                            await db.commit()
                            await self.sub2api.update_account_rate_multiplier(
                                config.management_account_id,
                                float(_quantize_rate(target)),
                            )
                            readback = await self.sub2api.get_account_management_billing_multiplier_multiplier(
                                config.management_account_id
                            )
                            readback_decimal = _decimal_multiplier(readback, allow_zero=True)
                            if (
                                readback_decimal is None
                                or _quantize_rate(readback_decimal) != _quantize_rate(target)
                            ):
                                raise ValueError("rate readback mismatch")
                            config.management_billing_multiplier = readback
                            config.last_applied_at = _utcnow()
                            new_current = readback
                            current_remote["rate_multiplier"] = readback
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
            and observed_current_decimal is not None
            and not self._same_multiplier(observed_current, target)
        )
        if not (
            rate_inputs_changed
            or target_changed
            or externally_changed
            or (attempted and current_drift)
            or health_change_should_log
            or schedulable_changed
            or health_action_status is not None
        ):
            return
        if status not in {"apply_failed", "disable_failed", "restore_failed"} and config.expected_management_billing_multiplier is not None and not upstream_health_invalid:
            config.last_error = None
        elif health_safe_error is not None:
            config.last_error = health_safe_error
        known_secrets = self._known_secrets(config, channel)
        db.add(
            UpstreamRateChangeLog(
                management_account_id=config.management_account_id,
                account_name=_safe_text(
                    config.remote_name,
                    secrets=known_secrets,
                    limit=200,
                ),
                upstream_id=channel.id,
                upstream_name=_safe_text(
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
                old_group_id=_safe_text(previous_group_id, secrets=known_secrets, limit=128),
                new_group_id=_safe_text(config.selected_group_id, secrets=known_secrets, limit=128),
                old_group_name=_safe_text(previous_group_name, secrets=known_secrets, limit=200),
                new_group_name=_safe_text(config.selected_group_name, secrets=known_secrets, limit=200),
                old_group_multiplier=previous_group_multiplier,
                new_group_multiplier=config.upstream_group_multiplier,
                old_upstream_multiplier=old_upstream_multiplier,
                new_upstream_multiplier=new_upstream_multiplier,
                old_upstream_recharge_multiplier=previous_upstream_recharge_multiplier,
                new_upstream_recharge_multiplier=channel.upstream_recharge_multiplier,
                upstream_recharge_multiplier=channel.upstream_recharge_multiplier,
                management_recharge_multiplier=config.management_recharge_multiplier,
                old_expected_management_billing_multiplier=previous_expected_management_billing_multiplier,
                new_expected_management_billing_multiplier=config.expected_management_billing_multiplier,
                old_management_billing_multiplier=old_current,
                new_management_billing_multiplier=new_current,
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
        change_observed_at = channel.last_discovered_at or _utcnow()
        change_details = {
            "account_id": config.management_account_id,
            "account_name": _safe_text(
                config.remote_name,
                secrets=known_secrets,
                limit=200,
            ),
        }
        new_current_event = _decimal_multiplier(new_current, allow_zero=True)
        management_billing_multiplier_transitions: list[tuple[Decimal, Decimal, str]] = []
        if externally_changed and old_current_decimal is not None and observed_current_decimal is not None:
            management_billing_multiplier_transitions.append(
                (old_current_decimal, observed_current_decimal, "external_observed")
            )
        if (
            status == "applied"
            and observed_current_decimal is not None
            and new_current_event is not None
            and _quantize_rate(observed_current_decimal) != _quantize_rate(new_current_event)
        ):
            management_billing_multiplier_transitions.append(
                (observed_current_decimal, new_current_event, "automatic_apply")
            )
        for transition_old, transition_new, transition_reason in management_billing_multiplier_transitions:
            db.add(
                UpstreamChangeEvent(
                    upstream_id=channel.id,
                    upstream_name=_safe_text(
                        channel.display_name,
                        secrets=known_secrets,
                        limit=200,
                    ),
                    event_type="account_rate_changed",
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
                    old_value=float(transition_old),
                    new_value=float(transition_new),
                    details={
                        **change_details,
                        "reason": reason,
                        "transition_reason": transition_reason,
                    },
                    created_at=change_observed_at,
                )
            )
        for event_type, old_health, new_health in (
            (
                "upstream_key_status_changed",
                health_transition.old_key_status,
                health_transition.new_key_status,
            ),
            (
                "upstream_group_status_changed",
                health_transition.old_group_status,
                health_transition.new_group_status,
            ),
        ):
            if old_health == new_health:
                continue
            if (
                event_type == "upstream_group_status_changed"
                and new_health == "deleted"
                and group_removed_in_discovery
            ):
                # The channel-level group_removed event is the canonical deletion
                # record for this observation. Emitting the account health view as
                # well renders the same deletion twice in the shared change ledger.
                continue
            invalid_statuses = (
                INVALID_UPSTREAM_KEY_STATUSES
                if event_type == "upstream_key_status_changed"
                else INVALID_UPSTREAM_GROUP_STATUSES
            )
            if old_health == "not_checked" and new_health not in invalid_statuses:
                continue
            db.add(
                UpstreamChangeEvent(
                    upstream_id=channel.id,
                    upstream_name=_safe_text(
                        channel.display_name,
                        secrets=known_secrets,
                        limit=200,
                    ),
                    event_type=event_type,
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
                    old_status=old_health,
                    new_status=new_health,
                    details=change_details,
                    created_at=change_observed_at,
                )
            )
        for transition_old, transition_new, transition_reason in management_billing_multiplier_transitions:
            rate_notification_events.append(
                {
                    "account_id": config.management_account_id,
                    "account_name": config.remote_name,
                    "upstream_id": channel.id,
                    "upstream_name": channel.display_name,
                    "group_id": config.selected_group_id,
                    "group_name": config.selected_group_name,
                    "old_rate": float(transition_old),
                    "new_rate": float(transition_new),
                    "observed_at": (
                    config.last_applied_at
                    if transition_reason == "automatic_apply" and config.last_applied_at is not None
                    else change_observed_at
                    ),
                    "reason": transition_reason,
                    "change_cause": reason,
                    "status": status,
                }
            )

    async def _enqueue_discovery_change_notifications(
        self,
        db: AsyncSession,
        *,
        channel: Upstream,
        group_changes: list[dict[str, Any]],
        rate_events: list[dict[str, Any]],
        runtime_config: Any,
    ) -> None:
        grouped_rate_event_indexes: set[int] = set()
        for group_change in group_changes:
            change_type = str(group_change.get("change_type") or "changed")
            details = dict(group_change.get("details") or {})
            notification_event_type = "upstream_group_changed"
            if change_type == "multiplier":
                notification_event_type = "upstream_group_multiplier_changed"
                group_id = str(group_change.get("group_id") or "").strip().casefold()
                group_name = str(group_change.get("group_name") or "").strip().casefold()
                affected_accounts: list[dict[str, Any]] = []
                for index, rate_event in enumerate(rate_events):
                    if rate_event.get("change_cause") != "upstream_group_change":
                        continue
                    event_group_id = str(rate_event.get("group_id") or "").strip().casefold()
                    event_group_name = str(rate_event.get("group_name") or "").strip().casefold()
                    same_group = bool(group_id and event_group_id == group_id) or bool(
                        not group_id and group_name and event_group_name == group_name
                    )
                    if not same_group:
                        continue
                    grouped_rate_event_indexes.add(index)
                    affected_accounts.append(
                        {
                            "account_id": rate_event.get("account_id"),
                            "account_name": rate_event.get("account_name"),
                            "old_rate": rate_event.get("old_rate"),
                            "new_rate": rate_event.get("new_rate"),
                            "status": rate_event.get("status"),
                        }
                    )
                if affected_accounts:
                    details["affected_accounts"] = affected_accounts

            await enqueue_upstream_group_changed(
                db,
                upstream_id=channel.id,
                upstream_name=channel.display_name,
                group_id=group_change.get("group_id"),
                group_name=group_change.get("group_name"),
                change_type=change_type,
                old_multiplier=group_change.get("old_multiplier"),
                new_multiplier=group_change.get("new_multiplier"),
                old_status=group_change.get("old_status"),
                new_status=group_change.get("new_status"),
                details=details,
                notification_event_type=notification_event_type,
                observed_at=channel.last_discovered_at or _utcnow(),
                runtime_config=runtime_config,
            )

        for index, rate_event in enumerate(rate_events):
            if index in grouped_rate_event_indexes:
                continue
            await enqueue_api_key_rate_changed(
                db,
                account_id=int(rate_event["account_id"]),
                account_name=rate_event.get("account_name"),
                old_rate=float(rate_event["old_rate"]),
                new_rate=float(rate_event["new_rate"]),
                observed_at=rate_event["observed_at"],
                reason=str(rate_event.get("reason") or "changed"),
                upstream_id=rate_event.get("upstream_id"),
                upstream_name=rate_event.get("upstream_name"),
                runtime_config=runtime_config,
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
        upstream_id: str,
        *,
        sync_inventory: bool = True,
        remote_by_id: dict[int, dict[str, Any]] | None = None,
        include_upstream_monitor_details: bool = False,
        upstream_monitor_detail_ids: set[int] | None = None,
        monitor_details_only: bool = False,
        options: UpstreamDiscoveryOptions | None = None,
        management_recharge_snapshot: tuple[float | None, str | None, str] | None = None,
    ) -> UpstreamOut:
        lock = await self._lock_for(upstream_id)
        async with lock:
            if remote_by_id is None:
                # Materialize the Sub2API inventory before opening the channel
                # read transaction. Public callers usually provide this batch
                # snapshot, while direct/background calls use this fallback.
                await db.commit()
                remote_by_id = {
                    account_id: account
                    for account in await self.accounts._remote_accounts()
                    if (account_id := self.accounts._numeric_remote_id(account)) is not None
                }
            channel = await self._load_channel(db, upstream_id)
            configs = await self._bound_configs(
                db,
                upstream_id,
                remote_by_id=remote_by_id,
            )
            if not configs and not monitor_details_only:
                raise ApiAccountServiceError(
                    "The upstream channel has no identity-bound API key accounts.",
                    status_code=409,
                )
            access_token = decrypt_text(channel.encrypted_access_token)
            refresh_token = decrypt_text(channel.encrypted_refresh_token)
            login_username = decrypt_text(channel.encrypted_login_username)
            login_password = decrypt_text(channel.encrypted_login_password)
            imported_api_keys: dict[int, str] = {}
            account_api_keys = (
                {}
                if monitor_details_only
                else await self._account_api_keys(
                    db,
                    configs,
                    channel.id,
                    remote_by_id=remote_by_id,
                    imported_api_keys=imported_api_keys,
                    persist_imported=False,
                )
            )
            account_api_key_record_ids = {
                config.management_account_id: config.remote_upstream_api_key_id
                for config in configs
                if config.remote_upstream_api_key_id is not None
            }
            account_input_snapshots = {
                config.management_account_id: self._account_rate_input_snapshot(config)
                for config in configs
            }
            priority_interval_ids = sorted(
                {
                    config.priority_interval_id
                    for config in configs
                    if config.priority_interval_id is not None
                }
            )
            priority_intervals: dict[int, Any] = {}
            if priority_interval_ids:
                interval_result = await db.execute(
                    select(UpstreamPriorityInterval).where(
                        UpstreamPriorityInterval.id.in_(priority_interval_ids)
                    )
                )
                priority_intervals = {
                    item.id: item for item in interval_result.scalars().all()
                }
            discovery_base_url = channel.management_url or channel.api_endpoint_url
            # Auto channels must re-establish platform identity every time.
            # Several management endpoints are shared by NewAPI and Sub2API,
            # so a cached forced type can otherwise report a false success
            # after the service behind the URL has changed platforms.
            preferred_type = channel.platform_type
            runtime_config = get_runtime_config_service()
            rate_pause_policies = {
                config.id: resolve_rate_pause_policy(
                    config,
                    priority_intervals.get(config.priority_interval_id),
                )
                for config in configs
            }
            today_timezone = DEFAULT_TODAY_TIME_ZONE
            if options is not None and options.refresh_upstream_monitors is False:
                auto_probe_upstream_monitors = False
            else:
                try:
                    auto_probe_upstream_monitors = bool(
                        await runtime_config.get_upstream_monitor_auto_probe_enabled()
                    )
                except Exception:
                    auto_probe_upstream_monitors = True
            refresh_upstream_monitors = bool(
                monitor_details_only
                or include_upstream_monitor_details
                or auto_probe_upstream_monitors
            )
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
            usage_snapshot_now = _utcnow()
            try:
                include_yesterday_usage = False
                if not monitor_details_only:
                    cached_yesterday = await finalize_cached_yesterday_usage(
                        db,
                        upstream=channel,
                        now=usage_snapshot_now,
                        time_zone=today_timezone,
                    )
                    include_yesterday_usage = not cached_yesterday
            except Exception:
                # Keep the existing upstream read behavior if an early upgrade
                # has not created the local daily-usage tables yet.
                include_yesterday_usage = not monitor_details_only

            # Discovery input is fully materialized. End the read phase before
            # upstream HTTP so a slow probe cannot retain a SQLite transaction.
            # expire_on_commit=False keeps these snapshots available until the
            # existing writeback revalidation below.
            await db.commit()

            async def run_discovery(active_access_token: str | None, active_type: str) -> Any:
                discovery_kwargs: dict[str, Any] = {
                    "base_url": discovery_base_url,
                    "upstream_type": active_type,
                    "access_token": active_access_token,
                    "new_api_user": channel.upstream_user_id,
                    "account_api_keys": account_api_keys,
                    "account_api_key_record_ids": account_api_key_record_ids,
                    "optimized_endpoint_fallbacks": True,
                    "include_upstream_monitors": refresh_upstream_monitors,
                    "include_upstream_monitor_details": include_upstream_monitor_details,
                    "upstream_monitor_detail_ids": upstream_monitor_detail_ids,
                    "monitor_only": monitor_details_only,
                    "today_timezone": today_timezone,
                }
                if not include_yesterday_usage:
                    discovery_kwargs["include_yesterday_usage"] = False
                discovery = discover_upstream(
                    **discovery_kwargs,
                )
                return await discovery if inspect.isawaitable(discovery) else discovery

            configured_refresh_supported = bool(
                channel.platform_type == "sub2api"
                or channel.resolved_platform_type == "sub2api"
                or (channel.platform_type == "auto" and channel.encrypted_refresh_token)
            )
            result: Any | None = None
            refresh_attempted = False
            refresh_succeeded = False
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
                    channel.platform_type == "auto"
                    and preferred_type != "auto"
                    and str(_value(result, "status") or "error").strip().lower() != "ok"
                    and not bool(_value(result, "sub2api_auth_rejected"))
                ):
                    result = await run_discovery(access_token, "auto")
                result_type = str(_value(result, "platform_type") or "").strip().lower()
                should_refresh = bool(
                    _value(result, "sub2api_auth_rejected")
                    and refresh_token
                    and (
                        configured_refresh_supported
                        or result_type == "sub2api"
                    )
                )
            if should_refresh:
                refresh_attempted = True
                refreshed = refresh_sub2api_tokens(
                    discovery_base_url,
                    refresh_token or "",
                )
                token_pair = await refreshed if inspect.isawaitable(refreshed) else refreshed
                if token_pair is not None:
                    refresh_succeeded = True
                    channel.encrypted_access_token = encrypt_text(token_pair.access_token)
                    channel.encrypted_refresh_token = encrypt_text(token_pair.refresh_token)
                    try:
                        await db.execute(
                            update(ApiAccount)
                            .where(ApiAccount.upstream_id == channel.id)
                            .values(encrypted_access_token=channel.encrypted_access_token)
                            .execution_options(synchronize_session=False)
                        )
                        # Sub2API invalidates the old RT before returning the new
                        # pair, so persist the pair before any retry can fail.
                        await db.commit()
                        await db.refresh(channel)
                    except Exception as exc:
                        await db.rollback()
                        raise ApiAccountServiceError(
                            "Could not securely save refreshed upstream credentials.",
                            status_code=503,
                        ) from exc

                    current_configs = await db.execute(
                        select(ApiAccount)
                        .where(
                            ApiAccount.management_account_id.in_(
                                account_input_snapshots
                            )
                        )
                        .execution_options(populate_existing=True)
                    )
                    configs = [
                        config
                        for config in current_configs.scalars().all()
                        if config.upstream_id == channel.id
                    ]
                    configs = await self._filter_current_bindings(
                        configs,
                        remote_by_id=remote_by_id,
                    )
                    account_api_keys = (
                        {}
                        if monitor_details_only
                        else await self._account_api_keys(
                            db,
                            configs,
                            channel.id,
                            remote_by_id=remote_by_id,
                            imported_api_keys=imported_api_keys,
                            persist_imported=False,
                        )
                    )
                    await db.commit()

                    access_token = token_pair.access_token
                    result = await run_discovery(access_token, preferred_type)
                    if (
                        channel.platform_type == "auto"
                        and preferred_type != "auto"
                        and str(_value(result, "status") or "error").strip().lower() != "ok"
                    ):
                        result = await run_discovery(access_token, "auto")
            login_supported = bool(
                login_username
                and login_password
                and (
                    channel.platform_type in {"auto", "sub2api"}
                    or channel.resolved_platform_type == "sub2api"
                )
            )
            # Some Xingchen endpoints are public, so a no-token probe can look
            # healthy even though the channel still needs a fresh AT.
            login_needed = bool(
                login_supported
                and (
                    not access_token
                    or result is None
                    or str(_value(result, "status") or "error").strip().lower() != "ok"
                    or bool(_value(result, "sub2api_auth_rejected"))
                )
            )
            if login_needed:
                logged_in = login_sub2api_tokens(
                    discovery_base_url,
                    login_username or "",
                    login_password or "",
                )
                login_pair = await logged_in if inspect.isawaitable(logged_in) else logged_in
                if login_pair is not None:
                    channel.encrypted_access_token = encrypt_text(login_pair.access_token)
                    if login_pair.refresh_token:
                        channel.encrypted_refresh_token = encrypt_text(login_pair.refresh_token)
                    elif refresh_attempted and not refresh_succeeded:
                        # A successful password login without an RT confirms that
                        # the previously attempted rotation did not yield a
                        # usable RT. Do not retain that stale token for future probes.
                        channel.encrypted_refresh_token = None
                    try:
                        await db.execute(
                            update(ApiAccount)
                            .where(ApiAccount.upstream_id == channel.id)
                            .values(encrypted_access_token=channel.encrypted_access_token)
                            .execution_options(synchronize_session=False)
                        )
                        await db.commit()
                        await db.refresh(channel)
                    except Exception as exc:
                        await db.rollback()
                        raise ApiAccountServiceError(
                            "Could not securely save logged-in upstream credentials.",
                            status_code=503,
                        ) from exc
                    access_token = login_pair.access_token
                    refresh_token = login_pair.refresh_token
                    result = await run_discovery(access_token, preferred_type)
                    if (
                        channel.platform_type == "auto"
                        and preferred_type != "auto"
                        and str(_value(result, "status") or "error").strip().lower() != "ok"
                    ):
                        result = await run_discovery(access_token, "auto")
            if result is None:
                result = await run_discovery(access_token, preferred_type)
                if (
                    channel.platform_type == "auto"
                    and preferred_type != "auto"
                    and str(_value(result, "status") or "error").strip().lower() != "ok"
                    and not bool(_value(result, "sub2api_auth_rejected"))
                ):
                    result = await run_discovery(access_token, "auto")
            if monitor_details_only:
                status = str(_value(result, "status") or "error").strip().lower()
                token_invalid = bool(_value(result, "sub2api_auth_rejected"))
                observed_at = _utcnow()
                if status == "insecure_url":
                    raise ApiAccountServiceError(
                        "Credentials may only be sent to an HTTPS upstream URL.",
                        status_code=422,
                    )
                resolved = str(_value(result, "platform_type") or "").strip().lower()
                if status == "ok" and resolved in {"newapi", "sub2api"}:
                    channel.resolved_platform_type = resolved
                self._apply_upstream_monitor_discovery(
                    channel,
                    result,
                    now=observed_at,
                    upstream_monitor_detail_ids=upstream_monitor_detail_ids,
                )
                if token_invalid:
                    channel.balance_status = TOKEN_INVALID_STATUS
                    channel.balance_message = None
                    channel.last_error = TOKEN_INVALID_ERROR
                    await enqueue_upstream_token_invalid(
                        db,
                        upstream_id=channel.id,
                        upstream_name=channel.display_name,
                        credential_fingerprint=self._credential_fingerprint(access_token),
                        observed_at=observed_at,
                        runtime_config=runtime_config,
                    )
                elif status == "ok" and channel.last_error == TOKEN_INVALID_ERROR:
                    channel.last_error = None
                    if channel.balance_status == TOKEN_INVALID_STATUS:
                        channel.balance_status = "not_checked"
                await db.commit()
                await db.refresh(channel)
                return self._channel_out(channel, [])

            # The remote discovery result is only an observation at this point.
            # Collect every remaining read-only Sub2API input before mutating
            # persistent ORM objects so SQLite has no transaction to retain
            # while these requests are in flight.
            await db.commit()
            token_invalid = bool(_value(result, "sub2api_auth_rejected"))
            linked_account_ids = [config.management_account_id for config in configs]

            async def fetch_local_today_stats() -> dict[int, dict[str, float | None]]:
                try:
                    return await self.accounts.get_sub2api_today_stats(linked_account_ids)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return {}

            async def fetch_yesterday_stats() -> dict[int, dict[str, float | None]]:
                if not include_yesterday_usage:
                    return {}
                try:
                    daily_stats = await self.accounts.get_sub2api_daily_stats(
                        linked_account_ids,
                        days=2,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return {}
                yesterday = usage_day(usage_snapshot_now, today_timezone) - timedelta(days=1)
                return {
                    account_id: stats[yesterday]
                    for account_id, stats in daily_stats.items()
                    if isinstance(stats, dict) and yesterday in stats
                }

            async def fetch_balance_fallback() -> dict[str, Any] | None:
                if token_invalid:
                    return None
                balance_status = str(
                    _value(result, "balance_status") or "not_checked"
                ).strip().lower()
                if balance_status in {"ok", "success", "available"}:
                    return None
                return await self._fetch_api_key_balance_fallback(configs)

            async def fetch_management_recharge() -> tuple[float | None, str | None, str]:
                if management_recharge_snapshot is not None:
                    return management_recharge_snapshot
                return await self._management_recharge()

            (
                local_today_stats,
                yesterday_stats_by_account,
                balance_fallback_result,
                management_recharge_result,
            ) = await asyncio.gather(
                fetch_local_today_stats(),
                fetch_yesterday_stats(),
                fetch_balance_fallback(),
                fetch_management_recharge(),
            )
            management_recharge, local_source, local_status = management_recharge_result

            try:
                threshold_getter = getattr(
                    runtime_config,
                    "get_upstream_balance_pause_threshold",
                    None,
                )
                balance_pause_threshold = (
                    float(await threshold_getter()) if threshold_getter else 0.0
                )
            except Exception:
                balance_pause_threshold = 0.0
            if options is not None and options.sync_rates is False:
                sync_enabled = False
            else:
                try:
                    sync_enabled = await runtime_config.get_upstream_rate_sync_enabled()
                except Exception:
                    sync_enabled = False
            try:
                automation_paused = bool(await runtime_config.get_automation_paused())
            except Exception:
                automation_paused = True
            if options is not None and options.evaluate_upstream_health is False:
                upstream_health_pause_enabled = None
            else:
                try:
                    upstream_health_pause_enabled = bool(
                        await runtime_config.get_api_key_auto_disable_on_upstream_unavailable()
                    )
                except Exception:
                    upstream_health_pause_enabled = None

            # Build the availability plan against detached ORM-shaped copies.
            # Existing policy helpers can therefore calculate and probe without
            # placing the real Session into an autoflush-capable state.
            planned_channel = _copy_mapped_row(channel)
            planned_configs = {
                config.id: _copy_mapped_row(config)
                for config in configs
            }
            planning_result = deepcopy(result)
            planned_previous_groups = _sanitize_group_options(planned_channel.group_options)
            self._stabilize_discovery_groups(
                planned_channel,
                planning_result,
                now=_utcnow(),
            )
            self._apply_stabilized_group_account_states(
                planned_channel,
                planning_result,
                list(planned_configs.values()),
                previous_groups=planned_previous_groups,
            )
            planned_discovery_succeeded = self._apply_discovery_to_channel(
                planned_channel,
                planning_result,
                refresh_upstream_monitors=refresh_upstream_monitors,
                time_zone=today_timezone,
                log_usage_failures=False,
            )
            if not token_invalid:
                self._apply_api_key_balance_fallback(
                    planned_channel,
                    balance_fallback_result,
                )
            planned_balance_guard_action = (
                None
                if options is not None and options.evaluate_balance_guard is False
                else await self._prepare_balance_guard(
                    None,
                    planned_channel,
                    runtime_config,
                    persist_notifications=False,
                )
            )
            await self._prepare_upstream_monitor_guard(
                planned_channel,
                runtime_config,
                monitor_probe_fresh=refresh_upstream_monitors,
            )

            planned_record_owners = {
                config.remote_upstream_api_key_id: config.id
                for config in planned_configs.values()
                if config.remote_upstream_api_key_id is not None
            }
            planned_unbound_counts: dict[int, int] = {}
            if planned_discovery_succeeded:
                for config in planned_configs.values():
                    if config.remote_upstream_api_key_id is not None:
                        continue
                    observed_id = self.accounts._upstream_record_id(
                        self._synchronized_upstream_state(
                            planning_result,
                            config.management_account_id,
                        )
                    )
                    if observed_id is not None:
                        planned_unbound_counts[observed_id] = (
                            planned_unbound_counts.get(observed_id, 0) + 1
                        )
            planned_ambiguous_ids = {
                record_id
                for record_id, count in planned_unbound_counts.items()
                if count > 1
            }
            planned_identity_status: dict[int, UpstreamRecordIdentityStatus] = {}
            for config in planned_configs.values():
                if planned_discovery_succeeded:
                    planned_identity_status[config.id] = (
                        await self.accounts.apply_upstream_record_identity(
                            db,
                            config,
                            self._synchronized_upstream_state(
                                planning_result,
                                config.management_account_id,
                            ),
                            observed_upstream_type=str(
                                _value(planning_result, "platform_type") or ""
                            ).strip().lower(),
                            record_owners=planned_record_owners,
                            ambiguous_unbound_record_ids=planned_ambiguous_ids,
                        )
                    )
                else:
                    planned_identity_status[config.id] = (
                        UpstreamRecordIdentityStatus.REBIND_REQUIRED
                        if config.upstream_identity_rebind_required
                        else UpstreamRecordIdentityStatus.VERIFIED
                    )

            # Identity validation may perform a read query. End it before the
            # connection tests below, which can each take tens of seconds.
            await db.commit()
            availability_semaphore = asyncio.Semaphore(4)

            async def prepare_availability(
                config: ApiAccount,
            ) -> tuple[int, _PreparedAccountAvailability | None]:
                if planned_identity_status.get(
                    config.id,
                    UpstreamRecordIdentityStatus.INCONCLUSIVE,
                ) is not UpstreamRecordIdentityStatus.VERIFIED:
                    return config.id, None
                if options is not None and options.evaluate_account_availability is False:
                    return config.id, None
                current_remote = remote_by_id.get(config.management_account_id)
                if current_remote is None:
                    return config.id, None
                blocking_pause_reason = self._availability_test_blocker(
                    config,
                    current_remote,
                )
                if blocking_pause_reason is None and planned_balance_guard_action == "hold":
                    blocking_pause_reason = AUTO_PAUSE_REASON_BALANCE
                if (
                    blocking_pause_reason is None
                    and upstream_health_pause_enabled is True
                    and int(config.upstream_health_invalid_count or 0) >= 1
                ):
                    upstream_state = self._synchronized_upstream_state(
                        planning_result,
                        config.management_account_id,
                    )
                    key_status = str(_value(upstream_state, "key_status") or "").strip().lower()
                    group_status = str(
                        _value(upstream_state, "group_status") or ""
                    ).strip().lower()
                    if key_status in INVALID_UPSTREAM_KEY_STATUSES:
                        blocking_pause_reason = AUTO_PAUSE_REASON_KEY
                    elif group_status in INVALID_UPSTREAM_GROUP_STATUSES:
                        blocking_pause_reason = AUTO_PAUSE_REASON_GROUP
                async with availability_semaphore:
                    action, evidence = await self._prepare_account_monitor_guard(
                        config,
                        planned_channel,
                        runtime_config,
                        automation_paused=automation_paused,
                        blocking_pause_reason=blocking_pause_reason,
                        monitor_probe_fresh=refresh_upstream_monitors,
                    )
                return config.id, _PreparedAccountAvailability(
                    action=action,
                    evidence=evidence,
                    values={
                        field: deepcopy(getattr(config, field))
                        for field in _AVAILABILITY_STATE_FIELDS
                    },
                )

            availability_observations = {
                config_id: prepared
                for config_id, prepared in await asyncio.gather(
                    *(prepare_availability(config) for config in planned_configs.values())
                )
                if prepared is not None
            }

            writeback_coordinator = self._active_writeback_coordinator
            if (
                writeback_coordinator is not None
                and writeback_coordinator.includes(upstream_id)
            ):
                await writeback_coordinator.wait_for_turn(upstream_id)

            had_previous_discovery = channel.last_discovered_at is not None
            previous_channel_groups = _sanitize_group_options(channel.group_options)
            group_changes: list[dict[str, Any]] = []
            previous_channel_recharge = self._remember_channel_recharge_multiplier(channel)
            previous_channel_recharge_source = channel.recharge_multiplier_source
            previous_channel_recharge_status = channel.recharge_multiplier_status
            self._stabilize_discovery_groups(channel, result, now=_utcnow())
            self._apply_stabilized_group_account_states(
                channel,
                result,
                configs,
                previous_groups=previous_channel_groups,
            )
            discovery_succeeded = self._apply_discovery_to_channel(
                channel,
                result,
                refresh_upstream_monitors=refresh_upstream_monitors,
                time_zone=today_timezone,
            )
            if not include_yesterday_usage:
                await hydrate_yesterday_usage(
                    db,
                    upstream=channel,
                    now=usage_snapshot_now,
                    time_zone=today_timezone,
                )
            if token_invalid:
                await enqueue_upstream_token_invalid(
                    db,
                    upstream_id=channel.id,
                    upstream_name=channel.display_name,
                    credential_fingerprint=self._credential_fingerprint(access_token),
                    observed_at=channel.last_discovered_at or _utcnow(),
                    runtime_config=runtime_config,
                )
            if discovery_succeeded and had_previous_discovery:
                group_changes = record_upstream_channel_changes(
                    db,
                    upstream_id=channel.id,
                    upstream_name=channel.display_name,
                    previous_recharge_multiplier=previous_channel_recharge,
                    current_recharge_multiplier=channel.upstream_recharge_multiplier,
                    previous_groups=previous_channel_groups,
                    current_groups=_sanitize_group_options(channel.group_options),
                    observed_at=channel.last_discovered_at or _utcnow(),
                )
            if not token_invalid:
                self._apply_api_key_balance_fallback(
                    channel,
                    balance_fallback_result,
                )
            balance_guard_action = (
                None
                if options is not None and options.evaluate_balance_guard is False
                else await self._prepare_balance_guard(db, channel, runtime_config)
            )
            await self._prepare_upstream_monitor_guard(
                channel,
                runtime_config,
                monitor_probe_fresh=refresh_upstream_monitors,
            )
            async with AsyncExitStack() as account_locks:
                for config in sorted(
                    configs,
                    key=lambda item: item.management_account_id,
                ):
                    account_lock = await self.accounts._lock_for(
                        config.management_account_id
                    )
                    await account_locks.enter_async_context(account_lock)

                with db.no_autoflush:
                    current_config_result = await db.execute(
                        select(ApiAccount)
                        .where(
                            ApiAccount.id.in_(
                                [config.id for config in configs]
                            )
                        )
                        .execution_options(populate_existing=True)
                    )
                current_configs = {
                    config.id: config
                    for config in current_config_result.scalars().all()
                }
                account_work_items: list[
                    tuple[ApiAccount, dict[str, Any]]
                ] = []
                settings = get_settings()
                can_persist_imported_keys = not (
                    settings.app_env == "production"
                    and settings.app_encryption_key.strip() == DEFAULT_ENCRYPTION_KEY
                )
                for pending_config in configs:
                    expected_inputs = account_input_snapshots.get(
                        pending_config.management_account_id
                    )
                    if expected_inputs is None:
                        continue
                    config = current_configs.get(pending_config.id)
                    if (
                        config is None
                        or config.upstream_id != channel.id
                        or self._account_rate_input_snapshot(config) != expected_inputs
                    ):
                        continue
                    try:
                        current_remote = (
                            remote_by_id.get(config.management_account_id)
                            if remote_by_id is not None
                            else await self.accounts._remote_account(
                                config.management_account_id
                            )
                        )
                        if current_remote is None:
                            continue
                        self.accounts._require_config_binding(current_remote, config)
                    except ApiAccountServiceError:
                        continue
                    imported_api_key = imported_api_keys.get(config.management_account_id)
                    if (
                        can_persist_imported_keys
                        and imported_api_key
                        and not config.encrypted_api_key
                    ):
                        config.encrypted_api_key = encrypt_text(imported_api_key)
                    account_work_items.append((config, current_remote))

                record_owners = {
                    config.remote_upstream_api_key_id: config.id
                    for config, _current_remote in account_work_items
                    if config.remote_upstream_api_key_id is not None
                }
                unbound_observation_counts: dict[int, int] = {}
                if discovery_succeeded:
                    for config, _current_remote in account_work_items:
                        if config.remote_upstream_api_key_id is not None:
                            continue
                        observed_id = self.accounts._upstream_record_id(
                            self._synchronized_upstream_state(
                                result,
                                config.management_account_id,
                            )
                        )
                        if observed_id is not None:
                            unbound_observation_counts[observed_id] = (
                                unbound_observation_counts.get(observed_id, 0) + 1
                            )
                ambiguous_unbound_record_ids = {
                    record_id
                    for record_id, count in unbound_observation_counts.items()
                    if count > 1
                }
                identity_status_by_config: dict[int, UpstreamRecordIdentityStatus] = {}
                for config, _current_remote in account_work_items:
                    if discovery_succeeded:
                        identity_status_by_config[config.id] = (
                            await self.accounts.apply_upstream_record_identity(
                                db,
                                config,
                                self._synchronized_upstream_state(
                                    result,
                                    config.management_account_id,
                                ),
                                observed_upstream_type=str(
                                    _value(result, "platform_type") or ""
                                ).strip().lower(),
                                record_owners=record_owners,
                                ambiguous_unbound_record_ids=(
                                    ambiguous_unbound_record_ids
                                ),
                            )
                        )
                    else:
                        identity_status_by_config[config.id] = (
                            UpstreamRecordIdentityStatus.REBIND_REQUIRED
                            if config.upstream_identity_rebind_required
                            else UpstreamRecordIdentityStatus.VERIFIED
                        )

                account_monitor_results: dict[int, tuple[str | None, dict[str, Any]]] = {}
                for config, _current_remote in account_work_items:
                    if identity_status_by_config.get(
                        config.id,
                        UpstreamRecordIdentityStatus.INCONCLUSIVE,
                    ) is not UpstreamRecordIdentityStatus.VERIFIED:
                        account_monitor_results[config.id] = (None, {
                            "mode": config.availability_check_mode,
                            "status": "upstream_identity_rebind_required",
                        })
                        continue
                    if (
                        options is not None
                        and options.evaluate_account_availability is False
                    ):
                        account_monitor_results[config.id] = (None, {
                            "mode": config.availability_check_mode,
                            "status": "manual_sync_skipped",
                        })
                        continue
                    prepared = availability_observations.get(config.id)
                    if prepared is None:
                        config.availability_status = "unknown"
                        config.availability_source = None
                        config.availability_message = "Availability observation was not prepared."
                        account_monitor_results[config.id] = (None, {
                            "mode": config.availability_check_mode,
                            "status": "unknown",
                        })
                        continue
                    for field, value in prepared.values.items():
                        setattr(config, field, deepcopy(value))
                    account_monitor_results[config.id] = (
                        prepared.action,
                        deepcopy(prepared.evidence),
                    )

                rate_notification_events: list[dict[str, Any]] = []
                for config, current_remote in account_work_items:
                    account_monitor_action, account_monitor_evidence = (
                        account_monitor_results.get(
                            config.id,
                            (None, {"mode": config.availability_check_mode, "status": "unknown"}),
                        )
                    )
                    if identity_status_by_config.get(
                        config.id,
                        UpstreamRecordIdentityStatus.INCONCLUSIVE,
                    ) is not UpstreamRecordIdentityStatus.VERIFIED:
                        config.last_discovered_at = channel.last_discovered_at
                        self.accounts.apply_remote_snapshot(
                            config,
                            current_remote,
                            secrets=self._known_secrets(config, channel),
                        )
                        if remote_by_id is not None:
                            remote_by_id[config.management_account_id] = current_remote
                        continue
                    if not discovery_succeeded:
                        config.upstream_health_invalid_count = 0
                        config.expected_management_billing_multiplier = None
                        config.management_recharge_multiplier = management_recharge
                        config.management_recharge_source = local_source
                        config.management_recharge_status = local_status
                        self.accounts.apply_sub2api_today_stats(
                            config,
                            local_today_stats.get(config.management_account_id),
                            now=channel.last_discovered_at or _utcnow(),
                            time_zone=today_timezone,
                        )
                        config.group_multiplier_status = "discovery_failed"
                        config.last_discovered_at = channel.last_discovered_at
                        config.last_error = (
                            "Upstream channel discovery failed; cached rates were not applied."
                        )
                        policy_now = channel.last_discovered_at or _utcnow()
                        failed_pause_reason = config.auto_disabled_reason or next(
                            (
                                hold.reason
                                for hold in self.accounts.active_pause_holds(config)
                            ),
                            None,
                        )
                        self._clear_disabled_policy_holds(
                            config,
                            channel,
                            balance_guard_action=balance_guard_action,
                            monitor_guard_action=account_monitor_action,
                            upstream_health_pause_enabled=upstream_health_pause_enabled,
                            rate_pause_enabled=rate_pause_policies.get(config.id, {}).get(
                                "enabled"
                            ),
                            automation_paused=automation_paused,
                            now=policy_now,
                        )
                        if account_monitor_action == "hold" and not automation_paused:
                            self.accounts.set_pause_hold(
                                config,
                                AUTO_PAUSE_REASON_MONITOR,
                                active=True,
                                scope_upstream_id=channel.id,
                                recovery_mode="account_availability_healthy",
                                now=policy_now,
                                evidence=account_monitor_evidence,
                            )
                        if (
                            self.accounts.active_pause_holds(config)
                            or config.pause_owned_by_plugin
                        ):
                            (
                                current_remote,
                                _old_remote_schedulable,
                                _new_remote_schedulable,
                                _policy_action_status,
                                policy_safe_error,
                            ) = await self.accounts.reconcile_automatic_pause(
                                db,
                                current_remote,
                                config,
                                upstream_id=channel.id,
                                upstream_name=channel.display_name,
                                pause_action_reason=failed_pause_reason,
                                mutations_allowed=not automation_paused,
                            )
                            if policy_safe_error is not None:
                                config.last_error = (
                                    f"{config.last_error} {policy_safe_error}"
                                )
                            self.accounts.apply_remote_snapshot(
                                config,
                                current_remote,
                                secrets=self._known_secrets(config, channel),
                            )
                        if remote_by_id is not None:
                            remote_by_id[config.management_account_id] = current_remote
                        continue
                    previous_group_multiplier = config.upstream_group_multiplier
                    previous_group_multiplier_source = config.group_multiplier_source
                    previous_group_multiplier_status = config.group_multiplier_status
                    previous_group_id = config.selected_group_id
                    previous_group_name = config.selected_group_name
                    previous_management_recharge_multiplier = config.management_recharge_multiplier
                    previous_expected_management_billing_multiplier = config.expected_management_billing_multiplier
                    upstream_state = self._synchronized_upstream_state(
                        result,
                        config.management_account_id,
                    )
                    self.accounts.apply_upstream_usage_state(
                        config,
                        upstream_state,
                        now=channel.last_discovered_at or _utcnow(),
                        time_zone=today_timezone,
                    )
                    health_transition = self.accounts.apply_authoritative_upstream_state(
                        config,
                        upstream_state,
                        now=channel.last_discovered_at or _utcnow(),
                    )
                    synchronized_group = self._synchronized_group(
                        result,
                        config.management_account_id,
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
                        management_recharge=management_recharge,
                        local_source=local_source,
                        local_status=local_status,
                    )
                    self.accounts.apply_sub2api_today_stats(
                        config,
                        local_today_stats.get(config.management_account_id),
                        now=channel.last_discovered_at or _utcnow(),
                        time_zone=today_timezone,
                    )
                    self.accounts.apply_local_today_usage_fallback(
                        config,
                        _value(
                            local_today_stats.get(config.management_account_id),
                            "cost",
                        ),
                        self.accounts._remote_management_billing_multiplier(current_remote),
                        now=channel.last_discovered_at or _utcnow(),
                    )
                    if synchronized_group is not None:
                        if synchronized_group["multiplier"] is not None:
                            config.group_multiplier_source = "upstream_key"
                        elif config.upstream_group_multiplier is None:
                            config.group_multiplier_status = "group_rate_unavailable"
                            config.last_error = (
                                "The synchronized upstream group does not expose a billing multiplier."
                            )
                    if health_transition.new_group_status in INVALID_UPSTREAM_GROUP_STATUSES:
                        config.expected_management_billing_multiplier = None
                        if health_transition.new_group_status == "deleted":
                            config.group_multiplier_status = "group_deleted"
                            config.last_error = "The synchronized upstream group was deleted."
                        else:
                            config.group_multiplier_status = "group_unavailable"
                            config.last_error = "The synchronized upstream group is unavailable."
                    elif health_transition.new_key_status in INVALID_UPSTREAM_KEY_STATUSES:
                        config.expected_management_billing_multiplier = None
                        config.last_error = "The synchronized upstream API key is unavailable."
                    policy_now = channel.last_discovered_at or _utcnow()
                    previous_pause_holds = self.accounts.active_pause_holds(config)
                    previous_pause_reason = (
                        config.auto_disabled_reason
                        or (previous_pause_holds[0].reason if previous_pause_holds else None)
                    )
                    if balance_guard_action is not None:
                        self.accounts.set_pause_hold(
                            config,
                            AUTO_PAUSE_REASON_BALANCE,
                            active=balance_guard_action == "hold",
                            scope_upstream_id=channel.id,
                            recovery_mode="balance_at_or_above_threshold",
                            now=policy_now,
                            evidence={
                                "balance": channel.balance_guard_value,
                                "basis": channel.balance_guard_basis,
                                "threshold": balance_pause_threshold,
                                "unit": (
                                    "CNY"
                                    if channel.balance_guard_basis == "recharge_adjusted"
                                    else channel.balance_unit or "USD"
                                ),
                            },
                        )
                    if account_monitor_action is not None:
                        self.accounts.set_pause_hold(
                            config,
                            AUTO_PAUSE_REASON_MONITOR,
                            active=account_monitor_action == "hold",
                            scope_upstream_id=channel.id,
                            recovery_mode="account_availability_healthy",
                            now=policy_now,
                            evidence=account_monitor_evidence,
                        )
                    self.accounts.update_upstream_health_pause_holds(
                        config,
                        health_transition,
                        enabled=upstream_health_pause_enabled,
                        automation_paused=automation_paused,
                        upstream_id=channel.id,
                        now=policy_now,
                    )
                    current_authoritative_multiplier = self._authoritative_upstream_multiplier(
                        config.upstream_group_multiplier,
                        config.group_multiplier_status,
                        config.group_multiplier_source,
                        channel.upstream_recharge_multiplier,
                        channel.recharge_multiplier_status,
                        channel.recharge_multiplier_source,
                    )
                    rate_pause_policy = rate_pause_policies.get(config.id, {})
                    self._update_rate_pause_hold(
                        config,
                        channel,
                        enabled=(
                            None
                            if options is not None and options.evaluate_rate_pause is False
                            else rate_pause_policy.get("enabled")
                        ),
                        automation_paused=automation_paused,
                        absolute_threshold=float(
                            rate_pause_policy.get("absolute_threshold") or 1.0
                        ),
                        current_multiplier=current_authoritative_multiplier,
                        now=policy_now,
                    )
                    active_pause_holds = self.accounts.active_pause_holds(config)
                    pause_action_reason = (
                        active_pause_holds[0].reason
                        if active_pause_holds
                        else previous_pause_reason
                    )
                    (
                        current_remote,
                        old_remote_schedulable,
                        new_remote_schedulable,
                        health_action_status,
                        health_safe_error,
                    ) = await self.accounts.reconcile_automatic_pause(
                        db,
                        current_remote,
                        config,
                        upstream_id=channel.id,
                        upstream_name=channel.display_name,
                        pause_action_reason=pause_action_reason,
                        mutations_allowed=not automation_paused,
                    )
                    await self._reconcile_account_rate(
                        db,
                        config,
                        channel,
                        previous_group_multiplier=previous_group_multiplier,
                        previous_group_id=previous_group_id,
                        previous_group_name=previous_group_name,
                        previous_upstream_recharge_multiplier=previous_channel_recharge,
                        previous_management_recharge_multiplier=previous_management_recharge_multiplier,
                        previous_expected_management_billing_multiplier=previous_expected_management_billing_multiplier,
                        sync_enabled=sync_enabled,
                        health_transition=health_transition,
                        old_remote_schedulable=old_remote_schedulable,
                        new_remote_schedulable=new_remote_schedulable,
                        health_action_status=health_action_status,
                        health_safe_error=health_safe_error,
                        pause_action_reason=pause_action_reason,
                        current_remote=current_remote,
                        runtime_config=runtime_config,
                        rate_notification_events=rate_notification_events,
                        group_removed_in_discovery=any(
                            _group_removal_matches(
                                change,
                                group_id=previous_group_id,
                                group_name=previous_group_name,
                            )
                            for change in group_changes
                        ),
                    )
                    self.accounts.apply_remote_snapshot(
                        config,
                        current_remote,
                        secrets=self._known_secrets(config, channel),
                    )
                    if remote_by_id is not None:
                        remote_by_id[config.management_account_id] = current_remote
                await self._enqueue_discovery_change_notifications(
                    db,
                    channel=channel,
                    group_changes=group_changes,
                    rate_events=rate_notification_events,
                    runtime_config=runtime_config,
                )
                # Persist the complete channel batch before releasing account
                # locks so concurrent account edits cannot be overwritten.
                channel.balance_guard_paused_count = sum(
                    1
                    for config in current_configs.values()
                    if config.balance_guard_operation == "paused"
                    and config.balance_guard_upstream_id == channel.id
                )
                history_now = channel.last_discovered_at or _utcnow()
                history_configs = [
                    config
                    for config in current_configs.values()
                    if config.upstream_id == channel.id
                ]
                if discovery_succeeded:
                    await snapshot_today_usage(
                        db,
                        upstream=channel,
                        configs=history_configs,
                        management_stats_by_account=local_today_stats,
                        management_recharge_multiplier=management_recharge,
                        now=history_now,
                        time_zone=today_timezone,
                    )
                    await finalize_yesterday_usage(
                        db,
                        upstream=channel,
                        management_stats_by_account=yesterday_stats_by_account,
                        management_recharge_multiplier=management_recharge,
                        now=history_now,
                        time_zone=today_timezone,
                    )
                else:
                    await hydrate_yesterday_usage(
                        db,
                        upstream=channel,
                        now=history_now,
                        time_zone=today_timezone,
                    )
                await db.flush()
            await db.commit()
            await db.refresh(channel)
        if not sync_inventory:
            return self._channel_out(channel, [])
        overview = await self.overview(db, sync_inventory=True)
        found = next(
            (item for item in overview.upstreams if item.upstream_id == upstream_id),
            None,
        )
        if found is None:
            raise ApiAccountServiceError("The upstream channel has no API key accounts.", status_code=409)
        return found

    async def backfill_missing_usage_history(
        self,
        db: AsyncSession,
        upstream_id: str,
        *,
        start_date: date,
        end_date: date,
        time_zone: str,
    ) -> int:
        """Fetch and persist only missing finalized channel-usage days."""

        if end_date < start_date:
            return 0
        lock = await self._lock_for(upstream_id)
        async with lock:
            channel = await self._load_channel(db, upstream_id)
            if not channel.probe_enabled:
                return 0
            missing_dates = await missing_finalized_usage_dates(
                db,
                upstream=channel,
                start_date=start_date,
                end_date=end_date,
            )
            if not missing_dates:
                return 0
            resolved_type = str(channel.resolved_platform_type or "").strip().lower()
            configured_type = str(channel.platform_type or "").strip().lower()
            upstream_type = (
                resolved_type
                if resolved_type in {"newapi", "sub2api"}
                else configured_type
            )
            if upstream_type not in {"newapi", "sub2api"}:
                return 0
            results = await asyncio.wait_for(
                fetch_upstream_daily_usages(
                    channel.management_url or channel.api_endpoint_url,
                    missing_dates,
                    upstream_type=upstream_type,
                    access_token=decrypt_text(channel.encrypted_access_token),
                    new_api_user=channel.upstream_user_id,
                    time_zone=time_zone,
                ),
                timeout=UPSTREAM_HISTORY_BACKFILL_TIMEOUT_SECONDS,
            )
            stored = 0
            for usage_date in missing_dates:
                result = results.get(usage_date)
                amount = _balance_number(getattr(result, "amount", None))
                if (
                    result is None
                    or str(result.status or "").strip().lower() != "ok"
                    or amount is None
                    or amount < 0
                ):
                    continue
                await upsert_historical_upstream_usage(
                    db,
                    upstream=channel,
                    usage_date=usage_date,
                    upstream_wallet_cost_usd=amount,
                    balance_unit=result.unit or "USD",
                    upstream_recharge_multiplier=channel.upstream_recharge_multiplier,
                    observed_at=_utcnow(),
                )
                stored += 1
            if stored:
                await db.commit()
            return stored

    async def import_sub2api_usage_history(
        self,
        db: AsyncSession,
        upstream_id: str,
        *,
        retention_days: int,
        time_zone: str,
    ) -> int:
        """Import retained linked-account history without reading upstream data."""

        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650.")
        lock = await self._lock_for(upstream_id)
        async with lock:
            channel = await self._load_channel(db, upstream_id)
            configs = list(
                (
                    await db.execute(
                        select(ApiAccount).where(
                            ApiAccount.upstream_id == channel.id,
                        )
                    )
                ).scalars()
            )
            account_ids = [int(config.management_account_id) for config in configs]
            if not account_ids:
                return 0
            now = _utcnow()
            current_day = usage_day(now, time_zone)
            start_day = current_day - timedelta(days=retention_days - 1)
            request_days = min(retention_days, 366)
            stats_by_account = await self.accounts.get_sub2api_daily_stats(
                account_ids,
                days=request_days,
            )
            filtered_stats = {
                account_id: {
                    usage_date: stats
                    for usage_date, stats in account_stats.items()
                    if start_day <= usage_date <= current_day
                }
                for account_id, account_stats in stats_by_account.items()
            }
            management_recharge, _source, _status = await self._management_recharge()
            if management_recharge is None:
                cached = [
                    config.management_recharge_multiplier
                    for config in configs
                    if config.management_recharge_multiplier is not None
                ]
                management_recharge = cached[0] if cached else None
            imported = await import_sub2api_daily_stats(
                db,
                upstream=channel,
                configs=configs,
                stats_by_account=filtered_stats,
                management_recharge_multiplier=management_recharge,
                now=now,
                time_zone=time_zone,
            )
            await db.commit()
            return imported

    async def finalize_elapsed_usage_history(
        self,
        db: AsyncSession,
        *,
        time_zone: str,
    ) -> int:
        """Freeze elapsed local snapshots without contacting an upstream."""

        return await finalize_elapsed_usage(
            db,
            now=_utcnow(),
            time_zone=time_zone,
        )

    async def discover_channel(
        self,
        db: AsyncSession,
        upstream_id: str,
        *,
        options: UpstreamDiscoveryOptions | None = None,
    ) -> UpstreamOut:
        async with self._discover_all_lock:
            remote_accounts, management_recharge_snapshot = await asyncio.gather(
                self.accounts._remote_accounts(),
                self._management_recharge(),
            )
            remote_by_id = {
                account_id: account
                for account in remote_accounts
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
            discover_kwargs: dict[str, Any] = {
                "sync_inventory": False,
                "remote_by_id": remote_by_id,
                "management_recharge_snapshot": management_recharge_snapshot,
            }
            if options is not None:
                discover_kwargs["options"] = options
            result = await self._discover_channel(db, upstream_id, **discover_kwargs)
            if options is None or options.sync_priorities is not False:
                await self._rebalance_priorities_best_effort(
                    db,
                    remote_by_id=remote_by_id,
                )
            refreshed = await self.overview(
                db,
                sync_inventory=False,
                remote_by_id=remote_by_id,
                management_recharge_snapshot=management_recharge_snapshot,
            )
            return next(
                (item for item in refreshed.upstreams if item.upstream_id == upstream_id),
                result,
            )

    async def refresh_upstream_monitors(
        self,
        db: AsyncSession,
        upstream_id: str,
    ) -> UpstreamMonitorsOut:
        async with self._discover_all_lock:
            result = await self._discover_channel(
                db,
                upstream_id,
                sync_inventory=False,
                include_upstream_monitor_details=True,
                monitor_details_only=True,
            )
            refreshed = await self.overview(db, sync_inventory=False)
            channel = next(
                (item for item in refreshed.upstreams if item.upstream_id == upstream_id),
                result,
            )
        return UpstreamMonitorsOut(
            upstream_id=channel.upstream_id,
            upstream_monitors=channel.upstream_monitors,
            upstream_monitor_count=channel.upstream_monitor_count,
            upstream_monitor_status=channel.upstream_monitor_status,
            upstream_monitor_message=channel.upstream_monitor_message,
            upstream_monitor_checked_at=channel.upstream_monitor_checked_at,
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
        options: UpstreamDiscoveryOptions | None = None,
        skip_upstream_ids: set[str] | None = None,
    ) -> UpstreamDiscoverAllOut:
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
                    options=options,
                    skip_upstream_ids=skip_upstream_ids,
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
        options: UpstreamDiscoveryOptions | None = None,
        skip_upstream_ids: set[str] | None = None,
    ) -> UpstreamDiscoverAllOut:
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
            (
                (
                    remote_by_id,
                    _configs,
                    _channels_by_id,
                    _membership_changed,
                ),
                management_recharge_snapshot,
            ) = await asyncio.gather(
                self.sync_inventory(db),
                self._management_recharge(),
            )
        else:
            remote_accounts, management_recharge_snapshot = await asyncio.gather(
                self.accounts._remote_accounts(),
                self._management_recharge(),
            )
            remote_by_id = {
                account_id: account
                for account in remote_accounts
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        inventory_duration_ms = elapsed_ms(inventory_started_at)
        overview = await self.overview(
            db,
            sync_inventory=False,
            remote_by_id=remote_by_id,
            management_recharge_snapshot=management_recharge_snapshot,
        )
        if legacy_bindings is not None:
            # The first confirmation can bind a legacy row whose live endpoint
            # differs from its stored channel. Inventory sync then creates the
            # origin-rebind tombstone; revalidate the same strict fingerprints
            # once more so this single informed confirmation covers both steps.
            await self.accounts.bind_legacy_identities(db, legacy_bindings)
        channels: list[UpstreamOut] = []
        succeeded = 0
        failed = 0
        occupied_channels = [
            channel for channel in overview.upstreams if channel.account_count > 0
        ]
        explicit_skip_ids = {str(upstream_id) for upstream_id in (skip_upstream_ids or set())}
        eligible_channels = [
            channel
            for channel in occupied_channels
            if channel.upstream_id not in explicit_skip_ids
            and channel.probe_enabled
            and (
                not require_management_credentials
                or channel.access_token_set
                or (
                    channel.refresh_token_set
                    and (
                        channel.platform_type in {"auto", "sub2api"}
                        or channel.resolved_platform_type == "sub2api"
                    )
                )
                or channel.login_credentials_set
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
        cached_upstream_ids = {channel.upstream_id for channel in cached_channels}
        channels_to_probe = [
            channel
            for channel in eligible_channels
            if channel.upstream_id not in cached_upstream_ids
        ]
        skipped = len(occupied_channels) - len(eligible_channels)
        effective_concurrency = (
            len(channels_to_probe) if max_concurrency == 0 else max_concurrency
        )
        probe_started_at = perf_counter()
        if effective_concurrency <= 1:
            results: list[UpstreamOut | BaseException] = []
            for channel in channels_to_probe:
                try:
                    discover_kwargs = {
                        "sync_inventory": False,
                        "remote_by_id": remote_by_id,
                        "management_recharge_snapshot": management_recharge_snapshot,
                    }
                    if options is not None:
                        discover_kwargs["options"] = options
                    results.append(
                        await self._discover_channel(
                            db, channel.upstream_id, **discover_kwargs
                        )
                    )
                except ApiAccountServiceError as exc:
                    results.append(exc)
                except OperationalError as exc:
                    if not _is_sqlite_locked_error(exc):
                        raise
                    await db.rollback()
                    db.expire_all()
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
            async def discover_with_session(upstream_id: str) -> UpstreamOut:
                coordinator = self._active_writeback_coordinator
                if coordinator is not None:
                    await coordinator.start_probe(upstream_id)
                async with session_factory() as worker_db:
                    discover_kwargs = {
                        "sync_inventory": False,
                        "remote_by_id": remote_by_id,
                        "management_recharge_snapshot": management_recharge_snapshot,
                    }
                    if options is not None:
                        discover_kwargs["options"] = options
                    return await self._discover_channel(worker_db, upstream_id, **discover_kwargs)

            writeback_coordinator = _DiscoveryWritebackCoordinator(
                [channel.upstream_id for channel in channels_to_probe],
                effective_concurrency,
            )
            self._active_writeback_coordinator = writeback_coordinator
            tasks: list[asyncio.Task[UpstreamOut]] = []
            try:
                for channel in channels_to_probe:
                    task = asyncio.create_task(
                        discover_with_session(channel.upstream_id)
                    )
                    task.add_done_callback(
                        lambda _completed, upstream_id=channel.upstream_id: (
                            writeback_coordinator.task_done(upstream_id)
                        )
                    )
                    tasks.append(task)
                results = list(
                    await asyncio.gather(
                        *tasks,
                        return_exceptions=True,
                    )
                )
            finally:
                self._active_writeback_coordinator = None
        probe_duration_ms = elapsed_ms(probe_started_at)

        for channel, result in zip(channels_to_probe, results):
            if isinstance(result, ApiAccountServiceError):
                failed += 1
                continue
            if _is_sqlite_locked_error(result):
                failed += 1
                logger.warning(
                    "Upstream channel %s writeback failed because SQLite remained locked.",
                    channel.upstream_id,
                )
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
            account.management_account_id
            for channel in eligible_channels
            for account in channel.accounts
        }
        try:
            remote_by_id = {
                account_id: account
                for account in await self.accounts._remote_accounts()
                if (account_id := self.accounts._numeric_remote_id(account)) is not None
            }
        except ApiAccountServiceError:
            pass
        priority_started_at = perf_counter()
        if options is None or options.sync_priorities is not False:
            await self._rebalance_priorities_best_effort(
                db,
                account_ids=eligible_account_ids,
                remote_by_id=remote_by_id,
            )
        priority_duration_ms = elapsed_ms(priority_started_at)
        db.expire_all()
        refreshed = await self.overview(
            db,
            sync_inventory=False,
            remote_by_id=remote_by_id,
            management_recharge_snapshot=management_recharge_snapshot,
        )
        return UpstreamDiscoverAllOut(
            total=len(occupied_channels),
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
            upstreams=refreshed.upstreams,
            overview=refreshed,
        )


_service: UpstreamService | None = None


def get_upstream_service() -> UpstreamService:
    global _service
    if _service is None:
        from app.services.upstream_priorities import get_upstream_priority_service

        _service = UpstreamService(
            accounts=get_api_account_service(),
            priorities=get_upstream_priority_service(),
        )
    return _service
