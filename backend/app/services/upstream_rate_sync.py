from __future__ import annotations

import asyncio
from time import perf_counter

from app.core.database import AsyncSessionLocal
from app.services.events import elapsed_ms, record_event
from app.services.runtime_config import RuntimeConfigService, get_runtime_config_service
from app.services.upstream_channels import UpstreamChannelService, get_upstream_channel_service
from app.services.upstream_rate_logs import prune_upstream_rate_change_logs


UPSTREAM_RATE_SYNC_TIMEOUT_SECONDS = 300


class UpstreamRateSyncService:
    def __init__(
        self,
        runtime_config: RuntimeConfigService | None = None,
        channel_service: UpstreamChannelService | None = None,
    ) -> None:
        self.runtime_config = runtime_config or get_runtime_config_service()
        self.channel_service = channel_service or get_upstream_channel_service()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._inventory_wake = asyncio.Event()
        self._upstream_wake = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._inventory_wake.clear()
        self._upstream_wake.clear()
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

    def wake(self) -> None:
        self._inventory_wake.set()
        self._upstream_wake.set()

    def wake_inventory(self) -> None:
        self._inventory_wake.set()

    def wake_upstream(self) -> None:
        self._upstream_wake.set()

    async def _loop(self) -> None:
        inventory_task = asyncio.create_task(self._inventory_loop())
        upstream_task = asyncio.create_task(self._upstream_loop())
        try:
            await asyncio.gather(inventory_task, upstream_task)
        finally:
            inventory_task.cancel()
            upstream_task.cancel()
            await asyncio.gather(inventory_task, upstream_task, return_exceptions=True)

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

    async def _run_once(self) -> None:
        retention_days = await self.runtime_config.get_upstream_rate_log_retention_days()
        async with AsyncSessionLocal() as db:
            await prune_upstream_rate_change_logs(
                db,
                retention_days=retention_days,
            )

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
