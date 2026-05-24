from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.services.events import record_event
from app.services.runtime_config import get_runtime_config_service
from app.services.sub2api import Sub2ApiClient, looks_deactive_text


SENSITIVE_ERROR_RE = re.compile(
    r"(?i)((?:access|refresh|id)_token|api_key|password|client_secret|authorization)([\"'\s:=]+)[^\s,}\"']+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass
class UsageRefreshFailure:
    email: str | None
    account_id: str | None
    error: str


@dataclass
class UsageRefreshSummary:
    total: int = 0
    refreshed: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[UsageRefreshFailure] = field(default_factory=list)

    @property
    def message(self) -> str:
        return (
            f"Usage window refresh finished: {self.refreshed}/{self.total} refreshed, "
            f"{self.skipped} skipped, {self.failed} failed."
        )


class UsageRefreshService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.sub2api = Sub2ApiClient(self.settings)
        self.runtime_config = get_runtime_config_service()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            await self._task

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            interval = await self.runtime_config.get_usage_refresh_interval_seconds()
            await self._wait_for_next_run(interval)
            if self._stop.is_set():
                break
            if not await self.runtime_config.get_usage_refresh_enabled():
                continue
            try:
                await self.refresh_all(reason="scheduled")
            except Exception as exc:
                async with AsyncSessionLocal() as db:
                    await record_event(
                        db,
                        "usage_refresh_failed",
                        "Scheduled usage window refresh failed.",
                        details={"error": _redact_error_text(str(exc))},
                    )

    async def _wait_for_next_run(self, interval: int) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=max(60, interval),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)
            self._wake.clear()

    async def refresh_all(self, reason: str = "manual") -> UsageRefreshSummary:
        async with self._lock:
            summary = await self._refresh_all_inner()
            async with AsyncSessionLocal() as db:
                await record_event(
                    db,
                    "usage_refresh",
                    summary.message,
                    details={
                        "reason": reason,
                        "total": summary.total,
                        "refreshed": summary.refreshed,
                        "skipped": summary.skipped,
                        "failed": summary.failed,
                        "failures": [
                            {
                                "email": failure.email,
                                "account_id": failure.account_id,
                                "error": failure.error,
                            }
                            for failure in summary.failures[:20]
                        ],
                    },
                )
            return summary

    async def _refresh_all_inner(self) -> UsageRefreshSummary:
        accounts = await self.sub2api.list_accounts()
        summary = UsageRefreshSummary()
        semaphore = asyncio.Semaphore(2)
        tasks = []

        for account in accounts:
            if not self.sub2api.is_gpt_account(account):
                continue
            account_id = self.sub2api.account_id(account)
            if not account_id:
                summary.skipped += 1
                continue
            if self.sub2api.is_deactive_account(account):
                summary.skipped += 1
                continue
            summary.total += 1
            tasks.append(asyncio.create_task(self._refresh_one(account, semaphore)))

        for task in asyncio.as_completed(tasks):
            account_id, email, result, error = await task
            if result is True:
                summary.refreshed += 1
            elif result is False:
                summary.skipped += 1
            else:
                summary.failed += 1
                summary.failures.append(UsageRefreshFailure(email=email, account_id=account_id, error=error or "unknown"))

        return summary

    async def _refresh_one(
        self,
        account: dict[str, Any],
        semaphore: asyncio.Semaphore,
    ) -> tuple[str | None, str | None, bool | None, str | None]:
        account_id = self.sub2api.account_id(account)
        email = self.sub2api.account_email(account)
        async with semaphore:
            try:
                refreshed = await self.sub2api.refresh_account_usage(account)
                return account_id, email, refreshed, None
            except Exception as exc:
                error = _redact_error_text(str(exc))
                if looks_deactive_text(error):
                    error = f"account_deactivated: {error}"
                return account_id, email, None, error


def _redact_error_text(value: str) -> str:
    text = BEARER_RE.sub("Bearer ***redacted***", value)
    text = SENSITIVE_ERROR_RE.sub(r"\1\2***redacted***", text)
    return text[:500]


_usage_refresh_service: UsageRefreshService | None = None


def get_usage_refresh_service() -> UsageRefreshService:
    global _usage_refresh_service
    if _usage_refresh_service is None:
        _usage_refresh_service = UsageRefreshService()
    return _usage_refresh_service
