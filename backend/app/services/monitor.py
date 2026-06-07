import asyncio

from sqlalchemy import delete, func, select

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, MailboxCredential, PhoneAccountBinding, PhoneNumber, utcnow
from app.schemas import Sub2ApiSyncResult
from app.services.account_exceptions import clear_account_exception, upsert_account_exception
from app.services.events import record_event
from app.services.phone_numbers import bind_phone_to_account, extract_phone_row_from_note, find_reconcilable_placeholder, note_marker
from app.services.refresh import RefreshService, get_refresh_service
from app.services.routed_mail_config import get_routed_mail_config_service
from app.services.runtime_config import get_runtime_config_service
from app.services.sub2api import Sub2ApiClient, looks_deactive_text, sanitize_payload
from app.core.crypto import encrypt_text


STARTUP_SYNC_DELAY_SECONDS = 10


class MonitorService:
    def __init__(self, refresh_service: RefreshService | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.sub2api = Sub2ApiClient(self.settings)
        self.refresh_service = refresh_service or get_refresh_service()
        self.routed_mail_config = get_routed_mail_config_service()
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
        await self._wait_for_startup_delay()
        while not self._stop.is_set():
            if await self.runtime_config.get_automation_paused():
                await self._wait_for_next_run()
                continue
            try:
                await self.sync_once(reason="scheduled")
            except Exception as exc:
                async with AsyncSessionLocal() as db:
                    await record_event(db, "monitor_failed", f"Monitor failed: {exc}")
            await self._wait_for_next_run()

    async def _wait_for_startup_delay(self) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=STARTUP_SYNC_DELAY_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)
            self._wake.clear()

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
        raw_accounts = await self.sub2api.list_accounts()
        accounts, duplicate_accounts = self.sub2api.dedupe_accounts_by_email(
            [account for account in raw_accounts if self.sub2api.is_gpt_account(account)]
        )
        recovery_enabled = await self.runtime_config.get_recovery_enabled()
        total_seen = 0
        error_seen = 0
        queued = 0
        seen_emails: set[str] = set()

        for account in accounts:
            email = self.sub2api.account_email(account)
            if not email:
                continue
            total_seen += 1
            normalized = email.lower()
            seen_emails.add(normalized)
            is_error = self.sub2api.is_error_account(account)
            is_deactive = self.sub2api.is_deactive_account(account)
            is_deactive, auto_refresh_locked = await self._upsert_snapshot(normalized, account, is_error, is_deactive)
            await self._ensure_routed_mailbox(normalized)

            if is_error or is_deactive:
                await self._record_sync_exception(
                    normalized,
                    account,
                    "auto_refresh_locked" if auto_refresh_locked else None,
                    effective_deactive=is_deactive,
                )
            elif self.sub2api.account_looks_healthy(account):
                await self._clear_account_exceptions(normalized, self.sub2api.account_id(account))

            if is_error:
                error_seen += 1
                if not is_deactive:
                    if auto_refresh_locked:
                        continue
                    if not recovery_enabled:
                        await self._mark_recovery_disabled(normalized)
                        continue
                    has_mailbox = await self._has_enabled_mailbox(normalized)
                    if not has_mailbox and not self.refresh_service.can_try_protocol_refresh(account):
                        await self._mark_missing_mailbox(normalized)
                        continue
                    job_id = await self.refresh_service.enqueue(account, reason=f"{reason}: sub2api reported error")
                    if job_id is not None:
                        queued += 1

        deleted_accounts, deleted_mailboxes = await self._delete_missing_remote_accounts(seen_emails)

        async with AsyncSessionLocal() as db:
            await record_event(
                db,
                "monitor_sync",
                (
                    f"Synced {total_seen} GPT accounts; {error_seen} are in error state; queued {queued}; "
                    f"ignored {len(duplicate_accounts)} duplicate sub2api account(s); "
                    f"deleted {deleted_accounts} stale local account(s) and {deleted_mailboxes} mailbox credential(s)."
                ),
                details={
                    "reason": reason,
                    "total_seen": total_seen,
                    "error_seen": error_seen,
                    "queued": queued,
                    "duplicate_accounts_ignored": len(duplicate_accounts),
                    "duplicates": duplicate_accounts[:50],
                    "deleted_accounts": deleted_accounts,
                    "deleted_mailboxes": deleted_mailboxes,
                },
            )
        return Sub2ApiSyncResult(
            message=(
                f"Synced {total_seen} GPT accounts; {error_seen} are in error state; queued {queued}; "
                f"ignored {len(duplicate_accounts)} duplicate sub2api account(s); "
                f"deleted {deleted_accounts} stale local account(s) and {deleted_mailboxes} mailbox credential(s)."
            ),
            total_seen=total_seen,
            error_seen=error_seen,
            queued=queued,
            duplicate_accounts_ignored=len(duplicate_accounts),
            deleted_accounts=deleted_accounts,
            deleted_mailboxes=deleted_mailboxes,
        )

    async def _upsert_snapshot(self, email: str, account: dict, is_error: bool, is_deactive: bool) -> tuple[bool, bool]:
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            was_deactive = bool(snapshot.deactive or looks_deactive_text(snapshot.last_error)) if snapshot is not None else False
            if snapshot is None:
                snapshot = AccountSnapshot(email=email)
                snapshot.usage_estimate_enabled = not is_deactive
                db.add(snapshot)
            remote_healthy = self._remote_looks_healthy(account, is_error, is_deactive)
            effective_deactive = is_deactive or (was_deactive and not remote_healthy)
            if effective_deactive and not was_deactive:
                snapshot.usage_estimate_enabled = False
            if remote_healthy or effective_deactive:
                snapshot.auto_refresh_locked = False
            snapshot.sub2api_account_id = self.sub2api.account_id(account)
            snapshot.platform = self.sub2api.account_platform(account)
            snapshot.account_type = self.sub2api.account_type(account)
            snapshot.status = self.sub2api.account_status(account)
            snapshot.schedulable = self.sub2api.account_schedulable(account)
            snapshot.deactive = effective_deactive
            if remote_healthy:
                snapshot.last_error = None
            snapshot.raw = sanitize_payload(account)
            await self._sync_phone_from_account_note(db, snapshot, email, account)
            snapshot.last_seen_at = utcnow()
            await db.commit()
            return effective_deactive, bool(snapshot.auto_refresh_locked)

    async def _sync_phone_from_account_note(
        self,
        db,
        snapshot: AccountSnapshot,
        email: str,
        account: dict,
    ) -> None:
        binding_exists = await db.scalar(
            select(PhoneAccountBinding.id).where(PhoneAccountBinding.account_email == email).limit(1)
        )
        if binding_exists is not None:
            return

        note_text = self.sub2api.account_phone_note_text(account)
        marker = note_marker(note_text)
        if snapshot.phone_note_sync_marker == marker:
            return

        snapshot.phone_note_sync_marker = marker
        if not note_text:
            return

        parsed = extract_phone_row_from_note(note_text)
        if parsed is None:
            return

        phone = await self._upsert_phone_from_note(db, parsed)
        await bind_phone_to_account(db, int(phone.id), email)

    async def _upsert_phone_from_note(self, db, parsed) -> PhoneNumber:
        existing = await db.scalar(select(PhoneNumber).where(PhoneNumber.phone_key == parsed.phone_key))
        if existing is None:
            placeholder = await find_reconcilable_placeholder(db, parsed.phone_number)
            if placeholder is None:
                placeholder = PhoneNumber(
                    phone_key=parsed.phone_key,
                    phone_number=parsed.phone_number,
                    sms_url=parsed.sms_url,
                    sms_cdk=parsed.sms_cdk,
                    sms_recharge_url=parsed.sms_recharge_url,
                )
                db.add(placeholder)
                await db.flush()
                return placeholder
            existing = placeholder

        existing.phone_key = parsed.phone_key
        existing.phone_number = parsed.phone_number
        existing.sms_url = parsed.sms_url
        existing.sms_cdk = parsed.sms_cdk
        existing.sms_recharge_url = parsed.sms_recharge_url
        await db.flush()
        return existing

    def _remote_looks_healthy(self, account: dict, is_error: bool, is_deactive: bool) -> bool:
        if is_error or is_deactive:
            return False
        return self.sub2api.account_looks_healthy(account)

    async def _delete_missing_remote_accounts(self, seen_emails: set[str]) -> tuple[int, int]:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(AccountSnapshot))
            stale = [snapshot for snapshot in result.scalars().all() if snapshot.email.lower() not in seen_emails]
            if not stale:
                return 0, 0

            stale_emails = [snapshot.email.lower() for snapshot in stale]
            mailbox_result = await db.execute(
                delete(MailboxCredential).where(func.lower(MailboxCredential.gpt_email).in_(stale_emails))
            )
            account_result = await db.execute(delete(AccountSnapshot).where(AccountSnapshot.id.in_([snapshot.id for snapshot in stale])))
            await db.commit()
            return account_result.rowcount or 0, mailbox_result.rowcount or 0

    async def _has_enabled_mailbox(self, email: str) -> bool:
        async with AsyncSessionLocal() as db:
            credential = await db.scalar(select(MailboxCredential).where(MailboxCredential.gpt_email == email))
            return credential is not None and not credential.disabled

    async def _ensure_routed_mailbox(self, email: str) -> None:
        binding = self.routed_mail_config.binding_for_email(email)
        if binding is None:
            return
        async with AsyncSessionLocal() as db:
            credential = await db.scalar(select(MailboxCredential).where(MailboxCredential.gpt_email == email))
            if credential is not None:
                return
            db.add(
                MailboxCredential(
                    gpt_email=email,
                    mailbox_email=binding.mailbox_email,
                    provider="gmail",
                    encrypted_password=encrypt_text(binding.password),
                    proxy_url=binding.proxy_url,
                    disabled=False,
                    last_error=None,
                )
            )
            await db.commit()

    async def _mark_missing_mailbox(self, email: str) -> None:
        reason = "No enabled mailbox credential exists for this GPT account; account was checked only and refresh was not queued."
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            should_record = snapshot is not None and snapshot.last_error != reason
            if snapshot:
                snapshot.refreshing = False
                snapshot.last_error = reason
            if snapshot:
                await upsert_account_exception(
                    db,
                    source="sync",
                    status="missing_mailbox",
                    message=reason,
                    email=email,
                    sub2api_account_id=snapshot.sub2api_account_id,
                    details={"reason": "missing_mailbox"},
                    commit=False,
                )
            if should_record:
                await record_event(db, "refresh_skipped_missing_mailbox", reason, email)
            else:
                await db.commit()

    async def _mark_recovery_disabled(self, email: str) -> None:
        reason = "Recovery is disabled in settings; account was checked only and refresh was not queued."
        async with AsyncSessionLocal() as db:
            snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
            should_record = snapshot is not None and snapshot.last_error != reason
            if snapshot:
                snapshot.refreshing = False
                snapshot.last_error = reason
            if snapshot:
                await upsert_account_exception(
                    db,
                    source="sync",
                    status="recovery_disabled",
                    message=reason,
                    email=email,
                    sub2api_account_id=snapshot.sub2api_account_id,
                    details={"reason": "recovery_disabled"},
                    commit=False,
                )
            if should_record:
                await record_event(db, "refresh_skipped_recovery_disabled", reason, email)
            else:
                await db.commit()

    async def _record_sync_exception(
        self,
        email: str,
        account: dict,
        reason: str | None = None,
        effective_deactive: bool = False,
    ) -> None:
        account_id = self.sub2api.account_id(account)
        status = "deactive" if effective_deactive or self.sub2api.is_deactive_account(account) else "error"
        account_status = self.sub2api.account_status(account) or "unknown"
        error_message = self.sub2api.account_error_message(account)
        if reason == "auto_refresh_locked":
            message = "sub2api reported this account in error state; automatic refresh is locked after a previous failure."
        elif status == "deactive":
            message = "sub2api reported this account as deactivated."
        else:
            message = f"sub2api reported this account in error state: status={account_status}."
        if error_message:
            message = f"{message} {error_message}"
        async with AsyncSessionLocal() as db:
            await upsert_account_exception(
                db,
                source="sync",
                status=status if reason is None else reason,
                message=message,
                email=email,
                sub2api_account_id=account_id,
                details={
                    "reason": reason,
                    "status": account_status,
                    "schedulable": self.sub2api.account_schedulable(account),
                    "remote_error": self.sub2api.is_error_account(account),
                    "deactive": self.sub2api.is_deactive_account(account),
                },
            )

    async def _clear_account_exceptions(self, email: str, sub2api_account_id: str | None) -> None:
        async with AsyncSessionLocal() as db:
            await clear_account_exception(db, source="sync", email=email, sub2api_account_id=sub2api_account_id, commit=False)
            await db.commit()


_monitor_service: MonitorService | None = None


def get_monitor_service() -> MonitorService:
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = MonitorService()
    return _monitor_service
