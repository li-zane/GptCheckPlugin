import asyncio

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, MailboxCredential, utcnow
from app.schemas import Sub2ApiSyncResult
from app.services.events import record_event
from app.services.refresh import RefreshService, get_refresh_service
from app.services.runtime_config import get_runtime_config_service
from app.services.sub2api import Sub2ApiClient, sanitize_payload


class MonitorService:
    def __init__(self, refresh_service: RefreshService | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.sub2api = Sub2ApiClient(self.settings)
        self.refresh_service = refresh_service or get_refresh_service()
        self.runtime_config = get_runtime_config_service()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def start(self) -> None:
        if self.settings.monitor_enabled and self._task is None:
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
            try:
                await self.sync_once(reason="scheduled")
            except Exception as exc:
                async with AsyncSessionLocal() as db:
                    await record_event(db, "monitor_failed", f"Monitor failed: {exc}")
            await self._wait_for_next_run()

    async def _wait_for_next_run(self) -> None:
        interval = await self.runtime_config.get_monitor_interval_seconds()
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)
            self._wake.clear()

    async def sync_once(self, reason: str = "manual") -> Sub2ApiSyncResult:
        accounts = await self.sub2api.list_accounts()
        total_seen = 0
        error_seen = 0
        queued = 0

        for account in accounts:
            if not self.sub2api.is_gpt_account(account):
                continue
            email = self.sub2api.account_email(account)
            if not email:
                continue
            total_seen += 1
            normalized = email.lower()
            is_error = self.sub2api.is_error_account(account)
            is_deactive = self.sub2api.is_deactive_account(account)
            await self._upsert_snapshot(normalized, account, is_deactive)

            if is_error:
                error_seen += 1
                if not is_deactive:
                    if not await self._has_enabled_mailbox(normalized):
                        await self._mark_missing_mailbox(normalized)
                        continue
                    job_id = await self.refresh_service.enqueue(account, reason=f"{reason}: sub2api reported error")
                    if job_id is not None:
                        queued += 1

        async with AsyncSessionLocal() as db:
            await record_event(
                db,
                "monitor_sync",
                f"Synced {total_seen} GPT accounts; {error_seen} are in error state; queued {queued}.",
                details={"total_seen": total_seen, "error_seen": error_seen, "queued": queued},
            )
        return Sub2ApiSyncResult(total_seen=total_seen, error_seen=error_seen, queued=queued)

    async def _upsert_snapshot(self, email: str, account: dict, is_deactive: bool) -> None:
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            was_deactive = snapshot.deactive if snapshot is not None else False
            if snapshot is None:
                snapshot = AccountSnapshot(email=email)
                snapshot.usage_estimate_enabled = not is_deactive
                db.add(snapshot)
            elif is_deactive and not was_deactive:
                snapshot.usage_estimate_enabled = False
            snapshot.sub2api_account_id = self.sub2api.account_id(account)
            snapshot.platform = self.sub2api.account_platform(account)
            snapshot.account_type = self.sub2api.account_type(account)
            snapshot.status = self.sub2api.account_status(account)
            snapshot.schedulable = self.sub2api.account_schedulable(account)
            snapshot.deactive = is_deactive
            snapshot.raw = sanitize_payload(account)
            snapshot.last_seen_at = utcnow()
            await db.commit()

    async def _has_enabled_mailbox(self, email: str) -> bool:
        async with AsyncSessionLocal() as db:
            credential = await db.scalar(select(MailboxCredential).where(MailboxCredential.gpt_email == email))
            return credential is not None and not credential.disabled

    async def _mark_missing_mailbox(self, email: str) -> None:
        reason = "No enabled mailbox credential exists for this GPT account; account was checked only and refresh was not queued."
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            should_record = snapshot is not None and snapshot.last_error != reason
            if snapshot:
                snapshot.refreshing = False
                snapshot.last_error = reason
            if should_record:
                await record_event(db, "refresh_skipped_missing_mailbox", reason, email)
            else:
                await db.commit()


_monitor_service: MonitorService | None = None


def get_monitor_service() -> MonitorService:
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = MonitorService()
    return _monitor_service
