from __future__ import annotations

import asyncio
from time import perf_counter

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, UpstreamAccountConfig, utcnow
from app.services.change_logs import prune_change_logs
from app.services.events import elapsed_ms, record_event
from app.services.runtime_config import RuntimeConfigService, get_runtime_config_service
from app.services.sub2api import Sub2ApiClient
from app.services.upstream_channels import UpstreamChannelService, get_upstream_channel_service
from app.services.upstream_rate_logs import prune_upstream_rate_change_logs
from app.services.upstream_usage_history import DEFAULT_TIME_ZONE, prune_upstream_usage_history


UPSTREAM_RATE_SYNC_TIMEOUT_SECONDS = 300


class UpstreamRateSyncService:
    def __init__(
        self,
        runtime_config: RuntimeConfigService | None = None,
        channel_service: UpstreamChannelService | None = None,
        sub2api: Sub2ApiClient | None = None,
    ) -> None:
        self.runtime_config = runtime_config or get_runtime_config_service()
        self.channel_service = channel_service or get_upstream_channel_service()
        self.sub2api = sub2api or Sub2ApiClient()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._inventory_wake = asyncio.Event()
        self._upstream_wake = asyncio.Event()
        self._priority_wake = asyncio.Event()
        self._model_whitelist_wake = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._inventory_wake.clear()
        self._upstream_wake.clear()
        self._priority_wake.clear()
        self._model_whitelist_wake.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        self.wake()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stop.clear()
            self._inventory_wake.clear()
            self._upstream_wake.clear()
            self._priority_wake.clear()
            self._model_whitelist_wake.clear()

    def wake(self) -> None:
        self._inventory_wake.set()
        self._upstream_wake.set()
        self._priority_wake.set()
        self._model_whitelist_wake.set()

    def wake_inventory(self) -> None:
        self._inventory_wake.set()

    def wake_upstream(self) -> None:
        self._upstream_wake.set()

    def wake_priority(self) -> None:
        self._priority_wake.set()

    def wake_model_whitelist(self) -> None:
        self._model_whitelist_wake.set()

    async def _loop(self) -> None:
        inventory_task = asyncio.create_task(self._inventory_loop())
        upstream_task = asyncio.create_task(self._upstream_loop())
        priority_task = asyncio.create_task(self._priority_loop())
        model_whitelist_task = asyncio.create_task(self._model_whitelist_loop())
        try:
            await asyncio.gather(
                inventory_task,
                upstream_task,
                priority_task,
                model_whitelist_task,
            )
        finally:
            inventory_task.cancel()
            upstream_task.cancel()
            priority_task.cancel()
            model_whitelist_task.cancel()
            await asyncio.gather(
                inventory_task,
                upstream_task,
                priority_task,
                model_whitelist_task,
                return_exceptions=True,
            )

    async def _inventory_loop(self) -> None:
        while not self._stop.is_set():
            try:
                interval = await self.runtime_config.get_api_key_account_sync_interval_seconds()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure(
                    "api_key_inventory_sync_failed",
                    "Scheduled API key account inventory synchronization failed.",
                )
                interval = 60
            await self._wait_for_next_run(interval, self._inventory_wake)
            if self._stop.is_set():
                break
            started_at = perf_counter()
            try:
                await self._run_inventory_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure(
                    "api_key_inventory_sync_failed",
                    "Scheduled API key account inventory synchronization failed.",
                    duration_ms=elapsed_ms(started_at),
                )

    async def _upstream_loop(self) -> None:
        while not self._stop.is_set():
            try:
                interval = await self.runtime_config.get_upstream_sync_interval_seconds()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure()
                interval = 60
            await self._wait_for_next_run(interval, self._upstream_wake)
            if self._stop.is_set():
                break
            started_at = perf_counter()
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure(duration_ms=elapsed_ms(started_at))

    async def _model_whitelist_loop(self) -> None:
        while not self._stop.is_set():
            try:
                interval = await self.runtime_config.get_account_model_whitelist_sync_interval_seconds()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure(
                    "account_model_whitelist_sync_failed",
                    "Scheduled account model whitelist synchronization failed.",
                )
                interval = 60
            await self._wait_for_next_run(interval, self._model_whitelist_wake)
            if self._stop.is_set():
                break
            started_at = perf_counter()
            try:
                await self._run_model_whitelist_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure(
                    "account_model_whitelist_sync_failed",
                    "Scheduled account model whitelist synchronization failed.",
                    duration_ms=elapsed_ms(started_at),
                )

    async def _priority_loop(self) -> None:
        while not self._stop.is_set():
            await self._priority_wake.wait()
            self._priority_wake.clear()
            if self._stop.is_set():
                break
            started_at = perf_counter()
            try:
                await self._run_priority_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure(
                    "upstream_priority_sync_failed",
                    "Scheduled API key account priority synchronization failed.",
                    duration_ms=elapsed_ms(started_at),
                )

    async def _run_inventory_once(self) -> None:
        enabled = await self.runtime_config.get_api_key_account_sync_enabled()
        paused = await self.runtime_config.get_automation_paused()
        if not enabled or paused:
            return
        started_at = perf_counter()
        async with AsyncSessionLocal() as db:
            inventory = await asyncio.wait_for(
                self.channel_service.sync_inventory(db),
                timeout=UPSTREAM_RATE_SYNC_TIMEOUT_SECONDS,
            )
            if inventory[3]:
                await self.channel_service._rebalance_priorities_best_effort(db)
        await self._record_event(
            "api_key_inventory_sync",
            "Scheduled API key account inventory synchronization finished.",
            details={
                "reason": "scheduled",
                "duration_ms": elapsed_ms(started_at),
            },
        )

    async def _run_model_whitelist_once(self) -> None:
        enabled = await self.runtime_config.get_account_model_whitelist_sync_enabled()
        paused = await self.runtime_config.get_automation_paused()
        if not enabled or paused:
            return

        started_at = perf_counter()
        async with AsyncSessionLocal() as db:
            oauth_rows = list(
                (
                    await db.execute(
                        select(AccountSnapshot).where(
                            AccountSnapshot.sub2api_account_id.is_not(None)
                        )
                    )
                ).scalars()
            )
            api_key_rows = list(
                (
                    await db.execute(
                        select(UpstreamAccountConfig)
                    )
                ).scalars()
            )

            rows_by_account_id: dict[str, list[AccountSnapshot | UpstreamAccountConfig]] = {}
            for row in (*oauth_rows, *api_key_rows):
                account_id = str(row.sub2api_account_id or "").strip()
                if account_id:
                    rows_by_account_id.setdefault(account_id, []).append(row)

            semaphore = asyncio.Semaphore(8)

            async def refresh(account_id: str, rows: list[AccountSnapshot | UpstreamAccountConfig]) -> bool:
                async with semaphore:
                    try:
                        models = await self.sub2api.get_account_models(account_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        for row in rows:
                            row.available_models_status = "error"
                            row.available_models_checked_at = utcnow()
                        return False
                for row in rows:
                    row.available_models = models
                    row.available_models_status = "ok"
                    row.available_models_checked_at = utcnow()
                return True

            results = await asyncio.gather(
                *(refresh(account_id, rows) for account_id, rows in rows_by_account_id.items())
            )
            await db.commit()

        await self._record_event(
            "account_model_whitelist_sync",
            "Scheduled account model whitelist synchronization finished.",
            details={
                "reason": "scheduled",
                "duration_ms": elapsed_ms(started_at),
                "total": len(results),
                "succeeded": sum(results),
                "failed": len(results) - sum(results),
            },
        )

    async def _run_priority_once(self) -> None:
        enabled = await self.runtime_config.get_upstream_priority_sync_enabled()
        paused = await self.runtime_config.get_automation_paused()
        if not enabled or paused:
            return
        started_at = perf_counter()
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                self.channel_service._priority_service().rebalance(db),
                timeout=UPSTREAM_RATE_SYNC_TIMEOUT_SECONDS,
            )
        await self._record_event(
            "upstream_priority_sync",
            "Scheduled API key account priority synchronization finished.",
            details={
                "reason": "settings_changed",
                "duration_ms": elapsed_ms(started_at),
                "considered": int(getattr(result, "considered", 0) or 0),
                "updated": int(getattr(result, "updated", 0) or 0),
                "unchanged": int(getattr(result, "unchanged", 0) or 0),
                "failed": int(getattr(result, "failed", 0) or 0),
            },
        )

    async def _run_once(self) -> None:
        retention_days = await self.runtime_config.get_upstream_rate_log_retention_days()
        usage_retention_getter = getattr(
            self.runtime_config,
            "get_upstream_usage_data_retention_days",
            None,
        )
        usage_retention_days = (
            int(await usage_retention_getter())
            if callable(usage_retention_getter)
            else None
        )
        usage_history_time_zone = DEFAULT_TIME_ZONE
        public_settings_getter = getattr(self.runtime_config, "get_public_settings", None)
        if callable(public_settings_getter):
            try:
                public_settings = await public_settings_getter()
                if isinstance(public_settings, dict):
                    configured_time_zone = str(public_settings.get("display_timezone") or "").strip()
                    if configured_time_zone:
                        usage_history_time_zone = configured_time_zone
            except Exception:
                pass
        async with AsyncSessionLocal() as db:
            await prune_upstream_rate_change_logs(
                db,
                retention_days=retention_days,
            )
            await prune_change_logs(
                db,
                retention_days=retention_days,
            )
            if (
                usage_retention_days is not None
                and callable(getattr(db, "execute", None))
            ):
                await prune_upstream_usage_history(
                    db,
                    retention_days=usage_retention_days,
                    time_zone=usage_history_time_zone,
                )
                commit = getattr(db, "commit", None)
                if callable(commit):
                    await commit()

        enabled = await self.runtime_config.get_upstream_sync_enabled()
        paused = await self.runtime_config.get_automation_paused()
        if not enabled or paused:
            return
        started_at = perf_counter()
        max_concurrency = await self.runtime_config.get_upstream_sync_max_concurrency()
        async with AsyncSessionLocal() as db:
            result = await asyncio.wait_for(
                self.channel_service.discover_all(
                    db,
                    max_concurrency=max_concurrency,
                    sync_inventory=False,
                    require_management_credentials=True,
                    force=True,
                ),
                timeout=UPSTREAM_RATE_SYNC_TIMEOUT_SECONDS,
            )
        details = {
            "reason": "scheduled",
            "duration_ms": elapsed_ms(started_at),
            "max_concurrency": max_concurrency,
            "force": True,
        }
        for field_name in (
            "total",
            "succeeded",
            "failed",
            "cached",
            "skipped",
            "inventory_duration_ms",
            "probe_duration_ms",
            "priority_duration_ms",
        ):
            value = getattr(result, field_name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                details[field_name] = value
        await self._record_event(
            "upstream_sync",
            "Scheduled upstream synchronization finished.",
            details=details,
        )

    async def _wait_for_next_run(self, interval: int, wake: asyncio.Event) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=max(1, interval),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)
            wake.clear()

    async def _record_failure(
        self,
        kind: str = "upstream_rate_sync_failed",
        message: str = "Scheduled upstream synchronization failed.",
        duration_ms: int | None = None,
    ) -> None:
        details = {"reason": "scheduled"}
        if duration_ms is not None:
            details["duration_ms"] = duration_ms
        await self._record_event(kind, message, details=details)

    async def _record_event(
        self,
        kind: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await record_event(
                    db,
                    kind,
                    message,
                    details=details,
                )
        except Exception:
            # A logging failure must not terminate the scheduler either.
            return


_service: UpstreamRateSyncService | None = None


def get_upstream_rate_sync_service() -> UpstreamRateSyncService:
    global _service
    if _service is None:
        _service = UpstreamRateSyncService()
    return _service
