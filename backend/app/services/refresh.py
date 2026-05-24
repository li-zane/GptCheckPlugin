import asyncio
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.crypto import encrypt_text
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, MailboxCredential, RefreshJob, utcnow
from app.services.browser import BrowserRefreshOutcome, ChatGptBrowserRefresher
from app.services.events import record_event
from app.services.mail import MailAdapterRegistry
from app.services.sub2api import Sub2ApiClient


class RefreshService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.sub2api = Sub2ApiClient(self.settings)
        self.mail = MailAdapterRegistry()
        self.browser = ChatGptBrowserRefresher(self.settings)
        self._running: set[str] = set()
        self._semaphore = asyncio.Semaphore(self.settings.refresh_max_concurrency)

    async def enqueue(self, account: dict, reason: str) -> int | None:
        email = self.sub2api.account_email(account)
        if not email:
            return None
        normalized = email.lower()
        if normalized in self._running:
            return None

        async with AsyncSessionLocal() as db:
            existing = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == normalized))
            if existing and existing.deactive:
                return None
            job = RefreshJob(
                email=normalized,
                sub2api_account_id=self.sub2api.account_id(account),
                status="queued",
                reason=reason,
            )
            db.add(job)
            await db.commit()
            await db.refresh(job)
            job_id = job.id

        asyncio.create_task(self._run(job_id, account))
        return job_id

    async def enqueue_by_email(self, email: str, reason: str = "manual") -> int | None:
        normalized = email.lower()
        accounts = await self.sub2api.list_accounts()
        for account in accounts:
            if self.sub2api.account_email(account) == normalized:
                return await self.enqueue(account, reason)
        return None

    async def _run(self, job_id: int, account: dict) -> None:
        email = (self.sub2api.account_email(account) or "").lower()
        if not email or email in self._running:
            return

        self._running.add(email)
        async with self._semaphore:
            try:
                await self._mark_started(job_id, email)
                await self._run_inner(job_id, email, account)
            finally:
                self._running.discard(email)

    async def _run_inner(self, job_id: int, email: str, account: dict) -> None:
        credential = await self._load_credential(email)
        if credential is None:
            await self._finish(job_id, email, "failed", "No mailbox credential exists for this GPT account.")
            return
        if credential.disabled:
            await self._finish(job_id, email, "failed", "Mailbox credential is disabled.")
            return

        async def fetch_code(after: datetime) -> str | None:
            deadline = asyncio.get_running_loop().time() + self.settings.verification_code_timeout_seconds
            last_error = "No verification code found yet."
            while asyncio.get_running_loop().time() < deadline:
                result = await self.mail.fetch_code(credential, after)
                if result.status == "ok" and result.code:
                    await self._mail_success(credential.id, result.new_refresh_token)
                    return result.code
                if result.status == "failed":
                    last_error = result.error or "Mailbox fetch failed."
                    await self._mail_error(credential.id, last_error)
                    return None
                last_error = result.error or last_error
                await asyncio.sleep(self.settings.verification_code_poll_seconds)
            await self._mail_error(credential.id, last_error)
            return None

        outcome = await self.browser.refresh_access_token(email, fetch_code)
        await self._handle_browser_outcome(job_id, email, account, outcome)

    async def _handle_browser_outcome(
        self,
        job_id: int,
        email: str,
        account: dict,
        outcome: BrowserRefreshOutcome,
    ) -> None:
        if outcome.status == "deactive":
            await self._mark_deactive(email, outcome.error or "Account is deactive.")
            await self._finish(job_id, email, "deactive", outcome.error or "Account is deactive.")
            return

        if outcome.status != "ok" or not outcome.access_token:
            await self._finish(job_id, email, "failed", outcome.error or "Failed to refresh access token.")
            return

        try:
            await self.sub2api.update_access_token(account, outcome.access_token)
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api token update failed: {exc}")
            return

        await self._finish(
            job_id,
            email,
            "succeeded",
            "Access token refreshed and written back to sub2api.",
            access_token_tail=outcome.access_token[-8:],
        )

    async def _load_credential(self, email: str) -> MailboxCredential | None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(MailboxCredential).where(MailboxCredential.gpt_email == email))
            credential = result.scalar_one_or_none()
            if credential:
                db.expunge(credential)
            return credential

    async def _mark_started(self, job_id: int, email: str) -> None:
        async with AsyncSessionLocal() as db:
            job = await db.get(RefreshJob, job_id)
            if job:
                job.status = "running"
                job.started_at = utcnow()
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot:
                snapshot.refreshing = True
                snapshot.last_error = None
            await record_event(db, "refresh_started", "Refresh job started.", email, {"job_id": job_id})

    async def _finish(
        self,
        job_id: int,
        email: str,
        status: str,
        reason: str,
        access_token_tail: str | None = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            job = await db.get(RefreshJob, job_id)
            if job:
                job.status = status
                job.reason = reason
                job.access_token_tail = access_token_tail
                job.finished_at = utcnow()
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot:
                snapshot.refreshing = False
                snapshot.last_error = None if status == "succeeded" else reason
                if status == "succeeded":
                    snapshot.status = "recovered"
                    snapshot.schedulable = True
            await record_event(db, f"refresh_{status}", reason, email, {"job_id": job_id})

    async def _mark_deactive(self, email: str, reason: str) -> None:
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot:
                snapshot.deactive = True
                snapshot.refreshing = False
                snapshot.last_error = reason
            await record_event(db, "account_deactive", reason, email)

    async def _mail_error(self, credential_id: int, error: str) -> None:
        async with AsyncSessionLocal() as db:
            credential = await db.get(MailboxCredential, credential_id)
            if credential:
                credential.last_error = error
                await db.commit()

    async def _mail_success(self, credential_id: int, new_refresh_token: str | None = None) -> None:
        async with AsyncSessionLocal() as db:
            credential = await db.get(MailboxCredential, credential_id)
            if credential:
                credential.last_error = None
                credential.last_success_at = datetime.now(timezone.utc)
                if new_refresh_token:
                    credential.encrypted_refresh_token = encrypt_text(new_refresh_token)
                await db.commit()


_refresh_service: RefreshService | None = None


def get_refresh_service() -> RefreshService:
    global _refresh_service
    if _refresh_service is None:
        _refresh_service = RefreshService()
    return _refresh_service
