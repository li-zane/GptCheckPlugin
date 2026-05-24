import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.crypto import encrypt_text
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, MailboxCredential, RefreshJob, utcnow
from app.services.browser import BrowserRefreshOutcome, ChatGptBrowserRefresher
from app.services.chatgpt_account import ChatGptAccessTokenInvalid, ChatGptAccountStatusChecker
from app.services.chatgpt_protocol import ChatGptProtocolRefresher, ProtocolRefreshOutcome
from app.services.events import record_event
from app.services.mail import MailAdapterRegistry
from app.services.memory import ProcessMemorySampler
from app.services.runtime_config import get_runtime_config_service
from app.services.sub2api import Sub2ApiClient, looks_deactive_text


SENSITIVE_ERROR_RE = re.compile(
    r"(?i)((?:access|refresh|id)_token|api_key|password|client_secret|authorization)([\"'\s:=]+)[^\s,}\"']+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def _redact_error_text(value: str) -> str:
    text = BEARER_RE.sub("Bearer ***redacted***", value)
    text = SENSITIVE_ERROR_RE.sub(r"\1\2***redacted***", text)
    return text[:500]


class RefreshService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.sub2api = Sub2ApiClient(self.settings)
        self.mail = MailAdapterRegistry()
        self.browser = ChatGptBrowserRefresher(self.settings)
        self.protocol = ChatGptProtocolRefresher(self.settings)
        self.account_status = ChatGptAccountStatusChecker(self.settings)
        self.runtime_config = get_runtime_config_service()
        self._running: set[str] = set()
        self._active_refreshes = 0
        self._concurrency_condition = asyncio.Condition()

    async def enqueue(self, account: dict, reason: str) -> int | None:
        email = self.sub2api.account_email(account)
        if not email:
            return None
        normalized = email.lower()
        if normalized in self._running:
            return None

        async with AsyncSessionLocal() as db:
            active_job = await db.scalar(
                select(RefreshJob).where(RefreshJob.email == normalized, RefreshJob.status.in_(["queued", "running"]))
            )
            if active_job:
                return None
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
        acquired_slot = False
        try:
            await self._acquire_refresh_slot()
            acquired_slot = True
            sampler = ProcessMemorySampler(interval_seconds=1.0)
            try:
                async with sampler:
                    try:
                        await self._mark_started(job_id, email)
                        await self._run_inner(job_id, email, account)
                    except Exception as exc:
                        await self._finish(
                            job_id,
                            email,
                            "failed",
                            f"Refresh task crashed before it could finish cleanly: {_redact_error_text(str(exc))}",
                        )
            finally:
                if sampler.peak_rss_bytes:
                    await self._record_memory_peak(job_id, email, sampler.peak_rss_bytes)
        finally:
            if acquired_slot:
                await self._release_refresh_slot()
            self._running.discard(email)

    async def _acquire_refresh_slot(self) -> None:
        async with self._concurrency_condition:
            while True:
                limit = await self.runtime_config.get_refresh_max_concurrency()
                if self._active_refreshes < limit:
                    self._active_refreshes += 1
                    return
                await self._concurrency_condition.wait()

    async def _release_refresh_slot(self) -> None:
        async with self._concurrency_condition:
            self._active_refreshes = max(0, self._active_refreshes - 1)
            self._concurrency_condition.notify_all()

    async def wake_concurrency(self) -> None:
        async with self._concurrency_condition:
            self._concurrency_condition.notify_all()

    async def _run_inner(self, job_id: int, email: str, account: dict) -> None:
        if await self._try_access_token_status_refresh(job_id, email, account):
            return

        credential = await self._load_credential(email)
        if credential is None:
            await self._finish(
                job_id,
                email,
                "failed",
                "No enabled mailbox credential exists for this GPT account; checked only and skipped browser refresh.",
            )
            return
        if credential.disabled:
            await self._finish(
                job_id,
                email,
                "failed",
                "Mailbox credential is disabled; checked only and skipped browser refresh.",
            )
            return

        async def fetch_code(after: datetime) -> str | None:
            deadline = asyncio.get_running_loop().time() + self.settings.verification_code_timeout_seconds
            lookup_after = after - timedelta(seconds=self.settings.verification_code_lookup_grace_seconds)
            last_error = "No verification code found yet."
            while asyncio.get_running_loop().time() < deadline:
                result = await self.mail.fetch_code(credential, lookup_after)
                if result.status == "ok" and result.code:
                    await self._mail_success(credential.id, result.new_refresh_token, result.new_access_token)
                    return result.code
                if result.status == "failed":
                    last_error = result.error or "Mailbox fetch failed."
                    await self._mail_error(credential.id, last_error)
                    return None
                last_error = result.error or last_error
                await asyncio.sleep(self.settings.verification_code_poll_seconds)
            await self._mail_error(credential.id, last_error)
            return None

        protocol_outcome = await self.protocol.refresh_access_token(email, fetch_code)
        if protocol_outcome.status in {"ok", "deactive"}:
            await self._handle_browser_outcome(job_id, email, account, protocol_outcome, source="protocol")
            return
        await self._record_chatgpt_protocol_warning(
            job_id,
            email,
            protocol_outcome.error or "ChatGPT protocol refresh failed.",
            will_fallback=True,
        )

        outcome = await self.browser.refresh_access_token(email, fetch_code)
        await self._handle_browser_outcome(job_id, email, account, outcome)

    async def _try_access_token_status_refresh(self, job_id: int, email: str, account: dict) -> bool:
        if await self._try_sub2api_account_status_refresh(job_id, email, account):
            return True

        if await self._try_sub2api_protocol_refresh(job_id, email, account):
            return True

        access_token = self.sub2api.account_access_token(account)
        if not access_token:
            return False

        try:
            status = await self.account_status.check(access_token)
        except ChatGptAccessTokenInvalid as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=True)
            return False
        except Exception as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=True)
            return False

        if status.deactive:
            reason = "ChatGPT account check reported account_deactivated."
            await self._mark_deactive(email, reason)
            await self._finish(job_id, email, "deactive", reason)
            return True

        try:
            changed_keys = await self.sub2api.update_credentials_from_session(account, status.session, access_token)
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api account status update failed: {exc}")
            return True

        usage_status = await self._refresh_sub2api_usage(job_id, email, account)
        try:
            subscription_keys = await self.sub2api.reassert_subscription_state_from_session(
                account,
                status.session,
                access_token,
            )
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api subscription state sync failed: {exc}")
            return True

        changed_text = ", ".join(changed_keys) if changed_keys else "no credential fields changed"
        plan_text = f"plan={status.plan_type}" if status.plan_type else "plan not returned"
        subscription_text = (
            f"reasserted session subscription fields: {', '.join(subscription_keys)}"
            if subscription_keys
            else "no session subscription fields found"
        )
        await self._finish(
            job_id,
            email,
            "succeeded",
            f"Access token status check succeeded; {plan_text}; updated sub2api credentials: {changed_text}; {usage_status}; {subscription_text}.",
            access_token_tail=access_token[-8:],
        )
        return True

    async def _try_sub2api_account_status_refresh(self, job_id: int, email: str, account: dict) -> bool:
        try:
            status = await self.sub2api.check_openai_account_status(account)
        except Exception as exc:
            await self._record_access_token_status_warning(job_id, email, str(exc), will_fallback=True)
            return False

        if not status:
            return False

        message = str(status.get("message") or "sub2api access-token account status check failed")
        if status.get("deactive"):
            await self._record_access_token_status_warning(
                job_id,
                email,
                "sub2api access-token account status check reported account_deactivated; falling back to direct ChatGPT/browser verification.",
                will_fallback=True,
            )
            return False

        if not status.get("token_valid"):
            await self._record_access_token_status_warning(job_id, email, message, will_fallback=True)
            return False

        usage_status = await self._refresh_sub2api_usage(job_id, email, account)

        changed = status.get("changed_credentials")
        changed_keys = [str(item) for item in changed] if isinstance(changed, list) else []
        changed_text = ", ".join(changed_keys) if changed_keys else "no credential fields changed"
        plan_type = str(status.get("plan_type") or "").strip()
        plan_text = f"plan={plan_type}" if plan_type else "plan not returned"
        subscription_keys: list[str] = []
        if plan_type:
            session = {"account": {"planType": plan_type}}
            subscription_expires_at = str(status.get("subscription_expires_at") or "").strip()
            if subscription_expires_at:
                session["account"]["subscriptionExpiresAt"] = subscription_expires_at
            try:
                subscription_keys = await self.sub2api.reassert_subscription_state_from_session(account, session, "")
            except Exception as exc:
                await self._finish(job_id, email, "failed", f"sub2api subscription state sync failed: {exc}")
                return True
        subscription_text = (
            f"reasserted session subscription fields: {', '.join(subscription_keys)}"
            if subscription_keys
            else "no session subscription fields found"
        )
        await self._finish(
            job_id,
            email,
            "succeeded",
            f"sub2api access-token account status check succeeded; {plan_text}; updated sub2api credentials: {changed_text}; {usage_status}; {subscription_text}.",
        )
        return True

    async def _try_sub2api_protocol_refresh(self, job_id: int, email: str, account: dict) -> bool:
        if not self.sub2api.account_has_refresh_token(account):
            return False

        try:
            refreshed_account = await self.sub2api.refresh_account_credentials(account)
        except Exception as exc:
            await self._record_protocol_refresh_warning(job_id, email, str(exc), will_fallback=True)
            return False

        if refreshed_account is None:
            return False

        usage_status = await self._refresh_sub2api_usage(job_id, email, account)
        await self._finish(
            job_id,
            email,
            "succeeded",
            f"sub2api OAuth token refresh succeeded via stored refresh_token; {usage_status}; no browser login was needed.",
        )
        return True

    async def _handle_browser_outcome(
        self,
        job_id: int,
        email: str,
        account: dict,
        outcome: BrowserRefreshOutcome | ProtocolRefreshOutcome,
        source: str = "browser",
    ) -> None:
        if outcome.status == "deactive":
            await self._mark_deactive(email, outcome.error or "Account is deactive.")
            await self._finish(job_id, email, "deactive", outcome.error or "Account is deactive.")
            return

        if outcome.status != "ok" or not outcome.access_token:
            await self._finish(job_id, email, "failed", outcome.error or "Failed to refresh access token.")
            return

        if await self._mark_deactive_if_missing_plan_check_fails(job_id, email, outcome):
            return

        try:
            changed_keys = await self.sub2api.update_credentials_from_session(account, outcome.session, outcome.access_token)
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api token update failed: {exc}")
            return

        await self._test_sub2api_for_deactivation(job_id, email, account)

        usage_status = await self._refresh_sub2api_usage(job_id, email, account)
        try:
            subscription_keys = await self.sub2api.reassert_subscription_state_from_session(
                account,
                outcome.session,
                outcome.access_token,
            )
        except Exception as exc:
            await self._finish(job_id, email, "failed", f"sub2api subscription state sync failed: {exc}")
            return

        changed_text = ", ".join(changed_keys) if changed_keys else "no credential fields changed"
        plan_type = self.sub2api.session_plan_type(outcome.session or {})
        plan_text = f"plan={plan_type}" if plan_type else "plan not returned"
        subscription_text = (
            f"reasserted session subscription fields: {', '.join(subscription_keys)}"
            if subscription_keys
            else "no session subscription fields found"
        )
        success_prefix = (
            "Session refreshed via ChatGPT protocol; no browser login was needed"
            if source == "protocol"
            else "Session refreshed"
        )
        await self._finish(
            job_id,
            email,
            "succeeded",
            f"{success_prefix}; {plan_text}; updated sub2api credentials: {changed_text}; {usage_status}; {subscription_text}.",
            access_token_tail=outcome.access_token[-8:],
        )

    async def _mark_deactive_if_missing_plan_check_fails(
        self,
        job_id: int,
        email: str,
        outcome: BrowserRefreshOutcome | ProtocolRefreshOutcome,
    ) -> bool:
        if not outcome.access_token or self.sub2api.session_plan_type(outcome.session or {}):
            return False

        try:
            status = await self.account_status.check_with_urllib(outcome.access_token)
        except Exception as exc:
            await self._record_access_token_status_warning(
                job_id,
                email,
                f"Refreshed session had no plan_type; accounts/check fallback failed: {exc}",
                will_fallback=False,
            )
            return False

        if status.deactive:
            reason = "ChatGPT accounts/check reported account_deactivated for refreshed session token."
            await self._mark_deactive(email, reason)
            await self._finish(job_id, email, "deactive", reason)
            return True

        outcome.session = _merge_session_status(outcome.session, status.session)
        return False

    async def _refresh_sub2api_usage(self, job_id: int, email: str, account: dict) -> str:
        try:
            refreshed = await self.sub2api.refresh_account_usage(account)
        except Exception as exc:
            if looks_deactive_text(str(exc)):
                await self._record_usage_refresh_warning(job_id, email, str(exc))
                return "sub2api usage refresh returned deactivation text; see events"
            await self._record_usage_refresh_warning(job_id, email, str(exc))
            return "sub2api usage refresh failed; see events"

        if refreshed:
            return "sub2api usage refreshed"
        return "sub2api usage endpoint unavailable"

    async def _test_sub2api_for_deactivation(self, job_id: int, email: str, account: dict) -> str | None:
        try:
            is_deactive = await self.sub2api.test_account_for_deactivation(account)
        except Exception as exc:
            error = str(exc)
            if looks_deactive_text(error):
                await self._record_sub2api_test_warning(job_id, email, error)
                return None
            await self._record_sub2api_test_warning(job_id, email, error)
            return None

        if is_deactive:
            await self._record_sub2api_test_warning(
                job_id,
                email,
                "sub2api connection test reported account_deactivated after successful session refresh; ignored.",
            )
        return None

    async def cleanup_stale_jobs(self) -> int:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(RefreshJob).where(RefreshJob.status.in_(["queued", "running"])))
            jobs = list(result.scalars().all())
            now = utcnow()
            for job in jobs:
                job.status = "failed"
                job.reason = "Service restarted before refresh finished."
                if job.started_at is None:
                    job.started_at = now
                job.finished_at = now

            snapshots_result = await db.execute(select(AccountSnapshot).where(AccountSnapshot.refreshing.is_(True)))
            snapshots = list(snapshots_result.scalars().all())
            for snapshot in snapshots:
                snapshot.refreshing = False
                if snapshot.last_error is None:
                    snapshot.last_error = "Previous refresh was interrupted by service restart."

            count = len(jobs)
            if count or snapshots:
                await record_event(
                    db,
                    "refresh_cleanup",
                    f"Marked {count} stale refresh jobs as failed after startup.",
                    details={"jobs": count, "snapshots": len(snapshots)},
                )
            else:
                await db.commit()
            return count

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
                    snapshot.deactive = False
                    snapshot.status = "recovered"
                    snapshot.schedulable = True
            await record_event(db, f"refresh_{status}", reason, email, {"job_id": job_id})

    async def _record_memory_peak(self, job_id: int, email: str, peak_rss_bytes: int) -> None:
        async with AsyncSessionLocal() as db:
            job = await db.get(RefreshJob, job_id)
            if job:
                job.memory_peak_rss_bytes = peak_rss_bytes
            await record_event(
                db,
                "refresh_memory_peak",
                f"Refresh process tree peak RSS was {_format_bytes(peak_rss_bytes)}.",
                email,
                {"job_id": job_id, "memory_peak_rss_bytes": peak_rss_bytes},
            )

    async def _mark_deactive(self, email: str, reason: str) -> None:
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            if snapshot:
                if not snapshot.deactive:
                    snapshot.usage_estimate_enabled = False
                snapshot.deactive = True
                snapshot.refreshing = False
                snapshot.last_error = reason
            await record_event(db, "account_deactive", reason, email)

    async def _record_usage_refresh_warning(self, job_id: int, email: str, error: str) -> None:
        async with AsyncSessionLocal() as db:
            await record_event(
                db,
                "sub2api_usage_refresh_failed",
                "sub2api usage refresh failed after credentials update.",
                email,
                {"job_id": job_id, "error": _redact_error_text(error)},
            )

    async def _record_sub2api_test_warning(self, job_id: int, email: str, error: str) -> None:
        async with AsyncSessionLocal() as db:
            await record_event(
                db,
                "sub2api_connection_test_failed",
                "sub2api connection test failed after credentials update.",
                email,
                {"job_id": job_id, "error": _redact_error_text(error)},
            )

    async def _record_access_token_status_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        will_fallback: bool,
    ) -> None:
        async with AsyncSessionLocal() as db:
            await record_event(
                db,
                "access_token_status_check_failed",
                "Access-token account status check failed; falling back to browser login."
                if will_fallback
                else "Access-token account status check failed.",
                email,
                {"job_id": job_id, "error": _redact_error_text(error), "will_fallback": will_fallback},
            )

    async def _record_protocol_refresh_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        will_fallback: bool,
    ) -> None:
        async with AsyncSessionLocal() as db:
            await record_event(
                db,
                "sub2api_protocol_refresh_failed",
                "sub2api protocol token refresh failed; falling back to browser login."
                if will_fallback
                else "sub2api protocol token refresh failed.",
                email,
                {"job_id": job_id, "error": _redact_error_text(error), "will_fallback": will_fallback},
            )

    async def _record_chatgpt_protocol_warning(
        self,
        job_id: int,
        email: str,
        error: str,
        will_fallback: bool,
    ) -> None:
        async with AsyncSessionLocal() as db:
            await record_event(
                db,
                "chatgpt_protocol_refresh_failed",
                "ChatGPT protocol session refresh failed; falling back to browser login."
                if will_fallback
                else "ChatGPT protocol session refresh failed.",
                email,
                {"job_id": job_id, "error": _redact_error_text(error), "will_fallback": will_fallback},
            )

    async def _mail_error(self, credential_id: int, error: str) -> None:
        async with AsyncSessionLocal() as db:
            credential = await db.get(MailboxCredential, credential_id)
            if credential:
                credential.last_error = error
                await db.commit()

    async def _mail_success(
        self,
        credential_id: int,
        new_refresh_token: str | None = None,
        new_access_token: str | None = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            credential = await db.get(MailboxCredential, credential_id)
            if credential:
                credential.last_error = None
                credential.last_success_at = datetime.now(timezone.utc)
                if new_refresh_token:
                    credential.encrypted_refresh_token = encrypt_text(new_refresh_token)
                if new_access_token:
                    credential.encrypted_access_token = encrypt_text(new_access_token)
                await db.commit()


_refresh_service: RefreshService | None = None


def get_refresh_service() -> RefreshService:
    global _refresh_service
    if _refresh_service is None:
        _refresh_service = RefreshService()
    return _refresh_service


def _format_bytes(value: int) -> str:
    mib = value / 1024 / 1024
    if mib < 1024:
        return f"{mib:.1f} MiB"
    return f"{mib / 1024:.2f} GiB"


def _merge_session_status(
    session: dict | None,
    status_session: dict | None,
) -> dict:
    merged = dict(session) if isinstance(session, dict) else {}
    if not isinstance(status_session, dict):
        return merged

    for key, value in status_session.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            nested = dict(value)
            nested.update(current)
            merged[key] = nested
        elif current in (None, "", [], {}):
            merged[key] = value
    return merged
