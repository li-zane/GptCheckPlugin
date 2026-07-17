from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from app.services.runtime_config import RuntimeConfigService, get_runtime_config_service


class AccountLivenessLimiter:
    """Process-wide limiter shared by every manual liveness request."""

    def __init__(self, runtime_config: RuntimeConfigService | None = None) -> None:
        self.runtime_config = runtime_config or get_runtime_config_service()
        self._active = 0
        self._condition = asyncio.Condition()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def active(self) -> int:
        return self._active

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self._acquire()
        try:
            yield
        finally:
            await self._release()

    async def _acquire(self) -> None:
        self._ensure_loop()
        while True:
            limit = await self.runtime_config.get_account_liveness_max_concurrency()
            async with self._condition:
                if limit == 0 or self._active < limit:
                    self._active += 1
                    return
                await self._condition.wait()

    async def _release(self) -> None:
        self._ensure_loop()
        async with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()

    async def wake(self) -> None:
        self._ensure_loop()
        async with self._condition:
            self._condition.notify_all()

    def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        if self._active:
            raise RuntimeError("Cannot move an active liveness limiter to another event loop.")
        self._loop = loop
        self._condition = asyncio.Condition()


_service: AccountLivenessLimiter | None = None


def get_account_liveness_limiter() -> AccountLivenessLimiter:
    global _service
    if _service is None:
        _service = AccountLivenessLimiter()
    return _service
