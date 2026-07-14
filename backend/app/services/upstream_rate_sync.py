from __future__ import annotations

import asyncio

from app.core.database import AsyncSessionLocal
from app.services.events import record_event
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
        self._wake = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        self._wake.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stop.clear()
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                interval = await self.runtime_config.get_monitor_interval_seconds()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure()
                interval = 60
            await self._wait_for_next_run(interval)
            if self._stop.is_set():
                break
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._record_failure()

    async def _run_once(self) -> None:
        retention_days = await self.runtime_config.get_upstream_rate_log_retention_days()
        async with AsyncSessionLocal() as db:
            await prune_upstream_rate_change_logs(
                db,
                retention_days=retention_days,
            )

        enabled = await self.runtime_config.get_upstream_rate_sync_enabled()
        paused = await self.runtime_config.get_automation_paused()
        if not enabled or paused:
            return
        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(
                self.channel_service.discover_all(db),
                timeout=UPSTREAM_RATE_SYNC_TIMEOUT_SECONDS,
            )

    async def _wait_for_next_run(self, interval: int) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
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
            self._wake.clear()

    async def _record_failure(self) -> None:
        try:
            async with AsyncSessionLocal() as db:
                await record_event(
                    db,
                    "upstream_rate_sync_failed",
                    "Scheduled upstream rate synchronization failed.",
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
