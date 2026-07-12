from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot
from app.services.events import record_event
from app.services.runtime_config import get_runtime_config_service
from app.services.sub2api import Sub2ApiClient, looks_deactive_text
from app.services.usage_estimate import account_rate_limited_windows, materialize_usage_reset_times, record_usage_limit_samples


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
    max_concurrency: int = 1
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
        self._latest_usage_by_id: dict[str, dict[str, Any]] = {}
        self._latest_usage_monotonic: float | None = None

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

    def latest_usage_snapshot(self, max_age_seconds: int = 900) -> dict[str, dict[str, Any]] | None:
        if self._latest_usage_monotonic is None:
            return None
        if time.monotonic() - self._latest_usage_monotonic > max_age_seconds:
            return None
        return dict(self._latest_usage_by_id)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            interval = await self.runtime_config.get_usage_refresh_interval_seconds()
            await self._wait_for_next_run(interval)
            if self._stop.is_set():
                break
            if await self.runtime_config.get_automation_paused():
                continue
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
                        "max_concurrency": summary.max_concurrency,
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
        accounts, _ = self.sub2api.dedupe_accounts_by_email(
            [
                account
                for account in await self.sub2api.list_accounts()
                if self.sub2api.is_gpt_account(account) and self.sub2api.is_oauth_account(account)
            ]
        )
        summary = UsageRefreshSummary()
        summary.max_concurrency = await self.runtime_config.get_usage_refresh_max_concurrency()
        sample_thresholds = await self.runtime_config.get_usage_limit_sample_thresholds()
        semaphore = asyncio.Semaphore(summary.max_concurrency)
        tasks = []
        usage_by_id: dict[str, dict[str, Any]] = {}

        for account in accounts:
            account_id = self.sub2api.account_id(account)
            if not account_id:
                summary.skipped += 1
                continue
            if self.sub2api.is_deactive_account(account):
                summary.skipped += 1
                continue
            summary.total += 1
            tasks.append(asyncio.create_task(self._refresh_one(account, semaphore, sample_thresholds)))

        for task in asyncio.as_completed(tasks):
            account_id, email, result, error, usage = await task
            if account_id and usage is not None:
                usage_by_id[account_id] = usage
            if result is True:
                summary.refreshed += 1
            elif result is False:
                summary.skipped += 1
            else:
                summary.failed += 1
                summary.failures.append(UsageRefreshFailure(email=email, account_id=account_id, error=error or "unknown"))

        await record_usage_limit_samples(self.sub2api, accounts, usage_by_id)
        self._latest_usage_by_id = dict(usage_by_id)
        self._latest_usage_monotonic = time.monotonic()
        return summary

    async def _refresh_one(
        self,
        account: dict[str, Any],
        semaphore: asyncio.Semaphore,
        sample_thresholds: dict[str, float],
    ) -> tuple[str | None, str | None, bool | None, str | None, dict[str, Any] | None]:
        account_id = self.sub2api.account_id(account)
        email = self.sub2api.account_email(account)
        async with semaphore:
            try:
                usage = await self.sub2api.refresh_account_usage_data(account)
                if usage is not None:
                    usage = materialize_usage_reset_times(usage)
                    await self._recover_cleared_rate_limits(account, usage, sample_thresholds)
                    await self._mark_deactivated_stale_rate_limit(account, usage, sample_thresholds)
                return account_id, email, usage is not None, None, usage
            except Exception as exc:
                error = _redact_error_text(str(exc))
                if looks_deactive_text(error):
                    await self._mark_account_deactive(email, f"sub2api usage refresh reported account_deactivated: {error}")
                    error = f"account_deactivated: {error}"
                return account_id, email, None, error, None

    async def _recover_cleared_rate_limits(
        self,
        account: dict[str, Any],
        usage: dict[str, Any],
        sample_thresholds: dict[str, float],
    ) -> None:
        if self.sub2api.is_deactive_account(account):
            return
        if not self.sub2api.account_has_stale_rate_limit_state(account):
            return
        limited_windows_after = account_rate_limited_windows(account, usage, sample_thresholds)
        if limited_windows_after:
            return
        await self.sub2api.recover_account_state(account)
        await self.sub2api.clear_rate_limit_state(account)

    async def _mark_deactivated_stale_rate_limit(
        self,
        account: dict[str, Any],
        usage: dict[str, Any],
        sample_thresholds: dict[str, float],
    ) -> None:
        if self.sub2api.is_deactive_account(account):
            return
        if not account_rate_limited_windows(account, usage, sample_thresholds):
            return
        email = self.sub2api.account_email(account)
        if not email:
            return
        try:
            status = await self.sub2api.check_openai_account_status(account)
        except Exception:
            return
        if not isinstance(status, dict):
            return
        message = str(status.get("message") or "").strip()
        if not status.get("deactive") and not looks_deactive_text(message):
            return
        reason = message or "sub2api account status check reported account_deactivated during usage refresh."
        await self._mark_account_deactive(email, reason)

    async def _mark_account_deactive(self, email: str | None, reason: str) -> None:
        normalized_email = str(email or "").strip().lower()
        if not normalized_email:
            return
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == normalized_email))
            if snapshot is None:
                snapshot = AccountSnapshot(email=normalized_email, usage_estimate_enabled=False)
                db.add(snapshot)
            elif not snapshot.deactive:
                snapshot.usage_estimate_enabled = False
            snapshot.deactive = True
            snapshot.refreshing = False
            snapshot.auto_refresh_locked = False
            snapshot.last_error = reason
            await db.commit()
            await record_event(db, "account_deactive", reason, normalized_email)


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
