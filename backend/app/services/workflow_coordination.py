from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator


class WorkflowCoordinator:
    """Give interactive upstream probes priority over background usage I/O."""

    def __init__(self) -> None:
        self._active_upstream_batches = 0
        self._condition = asyncio.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None

    @asynccontextmanager
    async def upstream_batch(self) -> AsyncIterator[None]:
        self._ensure_loop()
        async with self._condition:
            self._active_upstream_batches += 1
            self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._active_upstream_batches = max(
                    0,
                    self._active_upstream_batches - 1,
                )
                self._condition.notify_all()

    async def wait_for_upstream_idle(self, grace_seconds: float = 0.75) -> None:
        self._ensure_loop()
        await asyncio.sleep(max(0.0, grace_seconds))
        async with self._condition:
            while self._active_upstream_batches:
                await self._condition.wait()

    def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._active_upstream_batches:
            raise RuntimeError("Cannot move an active workflow coordinator to another event loop.")
        self._loop = loop
        self._condition = asyncio.Condition()


_service: WorkflowCoordinator | None = None


def get_workflow_coordinator() -> WorkflowCoordinator:
    global _service
    if _service is None:
        _service = WorkflowCoordinator()
    return _service
