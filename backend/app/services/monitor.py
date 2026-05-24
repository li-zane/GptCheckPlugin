import asyncio

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, utcnow
from app.schemas import Sub2ApiSyncResult
from app.services.events import record_event
from app.services.refresh import RefreshService, get_refresh_service
from app.services.sub2api import Sub2ApiClient, sanitize_payload


class MonitorService:
    def __init__(self, refresh_service: RefreshService | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.sub2api = Sub2ApiClient(self.settings)
        self.refresh_service = refresh_service or get_refresh_service()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self.settings.monitor_enabled and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sync_once(reason="scheduled")
            except Exception as exc:
                async with AsyncSessionLocal() as db:
                    await record_event(db, "monitor_failed", f"Monitor failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.monitor_interval_seconds)
            except asyncio.TimeoutError:
                continue

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
            if snapshot is None:
                snapshot = AccountSnapshot(email=email)
                db.add(snapshot)
            snapshot.sub2api_account_id = self.sub2api.account_id(account)
            snapshot.platform = self.sub2api.account_platform(account)
            snapshot.account_type = self.sub2api.account_type(account)
            snapshot.status = self.sub2api.account_status(account)
            snapshot.schedulable = self.sub2api.account_schedulable(account)
            snapshot.deactive = snapshot.deactive or is_deactive
            snapshot.raw = sanitize_payload(account)
            snapshot.last_seen_at = utcnow()
            await db.commit()


_monitor_service: MonitorService | None = None


def get_monitor_service() -> MonitorService:
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = MonitorService()
    return _monitor_service
