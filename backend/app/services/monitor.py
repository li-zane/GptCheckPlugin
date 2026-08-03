import asyncio
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, MailboxCredential, PhoneAccountBinding, PhoneNumber, utcnow
from app.schemas import Sub2ApiSyncResult
from app.services.account_exceptions import clear_account_exception, upsert_account_exception
from app.services.events import elapsed_ms, record_event
from app.services.mailbox_notes import parse_noted_mailbox_binding
from app.services.phone_numbers import bind_phone_to_account, extract_phone_row_from_note, find_reconcilable_placeholder, note_marker
from app.services.refresh import RefreshService, get_refresh_service
from app.services.routed_mail_config import get_routed_mail_config_service
from app.services.runtime_config import get_runtime_config_service
from app.services.notifications import (
    enqueue_oauth_account_disabled,
    enqueue_oauth_account_enabled,
)
from app.services.sub2api import Sub2ApiClient, looks_deactive_text, sanitize_payload
from app.services.subscription_refresh import refresh_subscriptions


STARTUP_SYNC_DELAY_SECONDS = 10
OAUTH_CREDENTIAL_EXPORT_BATCH_SIZE = 20
_initial_subscription_background_tasks: set[asyncio.Task[None]] = set()


def _normalize_available_models(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value[:1000]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()[:160]
        if not model_id or model_id in seen or any(ord(character) < 32 for character in model_id):
            continue
        display_name = str(item.get("display_name") or model_id).strip()[:200]
        seen.add(model_id)
        models.append({"id": model_id, "display_name": display_name or model_id})
        if len(models) >= 500:
            break
    return models


def _consume_initial_subscription_task(task: asyncio.Task[None]) -> None:
    _initial_subscription_background_tasks.discard(task)
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _coerce_expiry(value: Any) -> datetime | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.isdigit():
            try:
                parsed = datetime.fromtimestamp(float(normalized), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            try:
                parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            except ValueError:
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed if 0 < parsed.year <= 3000 else None


async def _record_event_best_effort(db, *args, **kwargs) -> None:
    try:
        await record_event(db, *args, **kwargs)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


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
            if (
                await self.runtime_config.get_automation_paused()
                or not await self.runtime_config.get_oauth_account_sync_enabled()
            ):
                await self._wait_for_next_run()
                continue
            started_at = perf_counter()
            try:
                await self.sync_once(reason="scheduled")
            except Exception as exc:
                async with AsyncSessionLocal() as db:
                    await _record_event_best_effort(
                        db,
                        "monitor_failed",
                        f"Monitor failed: {self.sub2api.redact_error_text(exc)}",
                        details={
                            "reason": "scheduled",
                            "duration_ms": elapsed_ms(started_at),
                        },
                    )
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

    async def sync_once(
        self,
        reason: str = "manual",
        *,
        raw_accounts: list[dict] | None = None,
    ) -> Sub2ApiSyncResult:
        started_at = perf_counter()
        fetched_remote_accounts = raw_accounts is None
        account_list_started_at = perf_counter()
        raw_accounts = raw_accounts if raw_accounts is not None else await self.sub2api.list_accounts()
        account_list_duration_ms = (
            elapsed_ms(account_list_started_at) if fetched_remote_accounts else None
        )
        present_remote_emails = {
            email.lower()
            for account in raw_accounts
            if (email := self.sub2api.account_email(account))
        }
        accounts, duplicate_accounts = self.sub2api.dedupe_accounts_by_email(
            [
                account
                for account in raw_accounts
                if self.sub2api.is_gpt_account(account)
                and self.sub2api.is_oauth_account(account)
            ]
        )
        exported_credentials: dict[int, dict[str, Any]] = {}
        oauth_account_ids = [
            int(account_id)
            for account in accounts
            if (account_id := self.sub2api.account_id(account)) and account_id.isdigit()
        ]
        for start in range(0, len(oauth_account_ids), OAUTH_CREDENTIAL_EXPORT_BATCH_SIZE):
            batch = oauth_account_ids[start : start + OAUTH_CREDENTIAL_EXPORT_BATCH_SIZE]
            exported_credentials.update(await self.sub2api.export_oauth_credentials(batch))
        for account in accounts:
            account_id = self.sub2api.account_id(account)
            if not account_id or not account_id.isdigit():
                continue
            imported = exported_credentials.get(int(account_id))
            if not imported:
                continue
            credentials = account.get("credentials")
            if not isinstance(credentials, dict):
                credentials = {}
                account["credentials"] = credentials
            credentials.update(imported)

        recovery_enabled = await self.runtime_config.get_recovery_enabled()
        # Whitelist refresh has its own scheduler. Normal account sync only
        # fills a missing local whitelist so it remains fast and predictable.
        model_whitelist_force = False
        total_seen = 0
        error_seen = 0
        queued = 0
        recovery_candidates: list[dict] = []
        initial_subscription_candidates: list[dict[str, Any]] = []
        deferred_events: list[tuple[str, str, str]] = []

        async with AsyncSessionLocal() as inventory_db:
            for account in accounts:
                email = self.sub2api.account_email(account)
                if not email:
                    continue
                total_seen += 1
                normalized = email.lower()
                is_error = self.sub2api.is_error_account(account)
                is_deactive = self.sub2api.is_deactive_account(account)
                is_deactive, auto_refresh_locked, initial_subscription_check = await self._upsert_snapshot(
                    normalized,
                    account,
                    is_error,
                    is_deactive,
                    db=inventory_db,
                )
                if initial_subscription_check:
                    initial_subscription_candidates.append(account)
                await self._ensure_routed_mailbox(normalized, db=inventory_db)
                await self._ensure_noted_mailbox(normalized, account, db=inventory_db)

                if is_error or is_deactive:
                    await self._record_sync_exception(
                        normalized,
                        account,
                        "auto_refresh_locked" if auto_refresh_locked else None,
                        effective_deactive=is_deactive,
                        db=inventory_db,
                    )
                elif self.sub2api.account_looks_healthy(account):
                    await self._clear_account_exceptions(
                        normalized,
                        self.sub2api.account_id(account),
                        db=inventory_db,
                    )

                if is_error:
                    error_seen += 1
                    if not is_deactive:
                        if auto_refresh_locked:
                            continue
                        if not recovery_enabled:
                            if await self._mark_recovery_disabled(normalized, db=inventory_db):
                                deferred_events.append(
                                    (
                                        "refresh_skipped_recovery_disabled",
                                        "Recovery is disabled in settings; account was checked only and refresh was not queued.",
                                        normalized,
                                    )
                                )
                            continue
                        has_mailbox = await self._has_enabled_mailbox(
                            normalized,
                            db=inventory_db,
                        )
                        if not has_mailbox and not self.refresh_service.can_try_protocol_refresh(account):
                            if await self._mark_missing_mailbox(normalized, db=inventory_db):
                                deferred_events.append(
                                    (
                                        "refresh_skipped_missing_mailbox",
                                        "No enabled mailbox credential exists for this GPT account; account was checked only and refresh was not queued.",
                                        normalized,
                                    )
                                )
                            continue
                        recovery_candidates.append(account)

            deleted_accounts, deleted_mailboxes = await self._delete_missing_remote_accounts(
                present_remote_emails,
                db=inventory_db,
            )
            await self._sync_oauth_available_models(
                inventory_db,
                accounts,
                force=model_whitelist_force,
            )
            await inventory_db.commit()

        self._dispatch_initial_subscription_refresh(initial_subscription_candidates)

        for event_kind, event_message, event_email in deferred_events:
            async with AsyncSessionLocal() as event_db:
                await _record_event_best_effort(
                    event_db,
                    event_kind,
                    event_message,
                    event_email,
                )

        for account in recovery_candidates:
            job_id = await self.refresh_service.enqueue(
                account,
                reason=f"{reason}: sub2api reported error",
            )
            if job_id is not None:
                queued += 1

        async with AsyncSessionLocal() as db:
            details = {
                "reason": reason,
                "total_seen": total_seen,
                "error_seen": error_seen,
                "queued": queued,
                "duplicate_accounts_ignored": len(duplicate_accounts),
                "duplicates": duplicate_accounts[:50],
                "deleted_accounts": deleted_accounts,
                "deleted_mailboxes": deleted_mailboxes,
                "oauth_credentials_exported": len(exported_credentials),
                "initial_subscription_checks_queued": len(initial_subscription_candidates),
                "duration_ms": elapsed_ms(started_at),
            }
            if account_list_duration_ms is not None:
                details["account_list_duration_ms"] = account_list_duration_ms
            await _record_event_best_effort(
                db,
                "monitor_sync",
                (
                    f"Synced {total_seen} OAuth GPT accounts; {error_seen} are in error state; queued {queued}; "
                    f"ignored {len(duplicate_accounts)} duplicate sub2api account(s); "
                    f"deleted {deleted_accounts} stale local account(s) and {deleted_mailboxes} mailbox credential(s)."
                ),
                details=details,
            )
        return Sub2ApiSyncResult(
            message=(
                f"Synced {total_seen} OAuth GPT accounts; {error_seen} are in error state; queued {queued}; "
                f"ignored {len(duplicate_accounts)} duplicate sub2api account(s); "
                f"imported credentials for {len(exported_credentials)} account(s); "
                f"queued {len(initial_subscription_candidates)} initial subscription check(s); "
                f"deleted {deleted_accounts} stale local account(s) and {deleted_mailboxes} mailbox credential(s)."
            ),
            total_seen=total_seen,
            error_seen=error_seen,
            queued=queued,
            duplicate_accounts_ignored=len(duplicate_accounts),
            deleted_accounts=deleted_accounts,
            deleted_mailboxes=deleted_mailboxes,
        )

    async def _upsert_snapshot(
        self,
        email: str,
        account: dict,
        is_error: bool,
        is_deactive: bool,
        *,
        db: AsyncSession | None = None,
    ) -> tuple[bool, bool, bool]:
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                result = await self._upsert_snapshot(
                    email,
                    account,
                    is_error,
                    is_deactive,
                    db=owned_db,
                )
                await owned_db.commit()
                return result

        snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
        was_deactive = bool(snapshot.deactive or looks_deactive_text(snapshot.last_error)) if snapshot is not None else False
        if snapshot is None:
            snapshot = AccountSnapshot(email=email)
            snapshot.usage_estimate_enabled = not is_deactive
            db.add(snapshot)
        initial_subscription_check = self._sync_oauth_credentials(snapshot, account)
        remote_healthy = self._remote_looks_healthy(account, is_error, is_deactive)
        effective_deactive = is_deactive or (was_deactive and not remote_healthy)
        if effective_deactive and not was_deactive:
            snapshot.usage_estimate_enabled = False
            await enqueue_oauth_account_disabled(
                db,
                email,
                "Sub2API reported the OAuth account as disabled.",
            )
        elif was_deactive and not effective_deactive:
            await enqueue_oauth_account_enabled(
                db,
                email,
                "Sub2API reported the OAuth account as enabled again.",
                account_id=self.sub2api.account_id(account),
            )
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
        return effective_deactive, bool(snapshot.auto_refresh_locked), initial_subscription_check

    def _sync_oauth_credentials(self, snapshot: AccountSnapshot, account: dict[str, Any]) -> bool:
        credentials = account.get("credentials")
        if not isinstance(credentials, dict):
            return False
        access_token = self.sub2api.account_access_token(account)
        state = self.sub2api.credentials_from_session(credentials, access_token or "")
        had_access_token = bool(decrypt_text(snapshot.encrypted_openai_access_token))

        for attribute, key in (
            ("encrypted_openai_access_token", "access_token"),
            ("encrypted_openai_refresh_token", "refresh_token"),
            ("encrypted_openai_id_token", "id_token"),
            ("encrypted_openai_client_id", "client_id"),
        ):
            value = state.get(key)
            if not _persistable_oauth_credential(value):
                continue
            normalized = value.strip()
            current = getattr(snapshot, attribute)
            if decrypt_text(current) != normalized:
                setattr(snapshot, attribute, encrypt_text(normalized))

        expires_at = _coerce_expiry(state.get("expires_at"))
        if expires_at is not None:
            snapshot.openai_token_expires_at = expires_at

        subscription_metadata = {
            "subscription_starts_at": state.get("subscription_starts_at"),
            "subscription_expires_at": state.get("subscription_expires_at"),
            "subscription_renews_at": state.get("subscription_renews_at"),
            "subscription_cancels_at": state.get("subscription_cancels_at"),
            "subscription_billing_period": state.get("subscription_billing_period"),
            "subscription_plan": state.get("subscription_plan") or state.get("plan_type"),
        }
        for attribute, value in subscription_metadata.items():
            if getattr(snapshot, attribute) is not None or not isinstance(value, str):
                continue
            normalized = value.strip()
            if normalized:
                setattr(snapshot, attribute, normalized)
        if snapshot.has_active_subscription is None and isinstance(state.get("has_active_subscription"), bool):
            snapshot.has_active_subscription = state["has_active_subscription"]

        has_subscription_time = bool(snapshot.subscription_starts_at or snapshot.subscription_expires_at)
        if has_subscription_time and snapshot.subscription_checked_at is None:
            snapshot.subscription_checked_at = utcnow()
        newly_imported_access_token = not had_access_token and bool(access_token)
        if (
            newly_imported_access_token
            and not has_subscription_time
            and snapshot.subscription_checked_at is None
        ):
            # Claim the one-time enrichment in the same transaction as the AT.
            snapshot.subscription_checked_at = utcnow()
            return True
        return False

    def _dispatch_initial_subscription_refresh(self, accounts: list[dict[str, Any]]) -> None:
        if not accounts:
            return
        task = asyncio.create_task(self._refresh_initial_subscriptions(accounts))
        _initial_subscription_background_tasks.add(task)
        task.add_done_callback(_consume_initial_subscription_task)

    async def _refresh_initial_subscriptions(self, accounts: list[dict[str, Any]]) -> None:
        try:
            configured_concurrency = await self.runtime_config.get_subscription_refresh_max_concurrency()
            result = await refresh_subscriptions(
                protocol_limit=0,
                max_concurrency=configured_concurrency,
                accounts=accounts,
            )
            async with AsyncSessionLocal() as db:
                await _record_event_best_effort(
                    db,
                    "initial_subscription_refresh",
                    result["message"],
                    details={
                        "reason": "oauth_credential_import",
                        "total": result["total"],
                        "refreshed": result["refreshed"],
                        "skipped": result["skipped"],
                        "failed": result["failed"],
                    },
                )
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                await _record_event_best_effort(
                    db,
                    "initial_subscription_refresh_failed",
                    f"Initial subscription refresh failed: {self.sub2api.redact_error_text(exc)}",
                    details={"reason": "oauth_credential_import"},
                )

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

    async def _delete_missing_remote_accounts(
        self,
        present_remote_emails: set[str],
        *,
        db: AsyncSession | None = None,
    ) -> tuple[int, int]:
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                result = await self._delete_missing_remote_accounts(
                    present_remote_emails,
                    db=owned_db,
                )
                await owned_db.commit()
                return result

        result = await db.execute(select(AccountSnapshot))
        stale = [
            snapshot
            for snapshot in result.scalars().all()
            if snapshot.email.lower() not in present_remote_emails
        ]
        if not stale:
            return 0, 0

        stale_emails = [snapshot.email.lower() for snapshot in stale]
        mailbox_result = await db.execute(
            delete(MailboxCredential).where(func.lower(MailboxCredential.gpt_email).in_(stale_emails))
        )
        account_result = await db.execute(
            delete(AccountSnapshot).where(
                AccountSnapshot.id.in_([snapshot.id for snapshot in stale])
            )
        )
        return account_result.rowcount or 0, mailbox_result.rowcount or 0

    async def _sync_oauth_available_models(
        self,
        db: AsyncSession,
        accounts: list[dict[str, Any]],
        *,
        force: bool,
    ) -> int:
        remote_by_id = {
            account_id: account
            for account in accounts
            if (account_id := self.sub2api.account_id(account))
        }
        if not remote_by_id:
            return 0
        await db.flush()
        snapshots = list(
            (
                await db.execute(
                    select(AccountSnapshot).where(
                        AccountSnapshot.sub2api_account_id.in_(remote_by_id)
                    )
                )
            ).scalars()
        )
        targets = [
            (snapshot, remote_by_id[str(snapshot.sub2api_account_id)])
            for snapshot in snapshots
            if str(snapshot.sub2api_account_id) in remote_by_id
            and (force or snapshot.available_models is None)
        ]
        semaphore = asyncio.Semaphore(8)

        async def fetch(snapshot: AccountSnapshot, account: dict[str, Any]) -> bool:
            async with semaphore:
                try:
                    models = _normalize_available_models(
                        await self.sub2api.get_account_models(account)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    snapshot.available_models_status = "error"
                    snapshot.available_models_checked_at = utcnow()
                    return False
            snapshot.available_models = models
            snapshot.available_models_status = "ok"
            snapshot.available_models_checked_at = utcnow()
            return True

        results = await asyncio.gather(
            *(fetch(snapshot, account) for snapshot, account in targets)
        )
        return sum(results)

    async def _has_enabled_mailbox(
        self,
        email: str,
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                return await self._has_enabled_mailbox(email, db=owned_db)
        credential = await db.scalar(select(MailboxCredential).where(MailboxCredential.gpt_email == email))
        return credential is not None and not credential.disabled

    async def _ensure_routed_mailbox(
        self,
        email: str,
        *,
        db: AsyncSession | None = None,
    ) -> None:
        binding = self.routed_mail_config.binding_for_email(email)
        if binding is None:
            return
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                await self._ensure_routed_mailbox(email, db=owned_db)
                await owned_db.commit()
                return
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

    async def _ensure_noted_mailbox(
        self,
        email: str,
        account: dict[str, Any],
        *,
        db: AsyncSession | None = None,
    ) -> None:
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                await self._ensure_noted_mailbox(email, account, db=owned_db)
                await owned_db.commit()
                return
        credential = await db.scalar(select(MailboxCredential).where(MailboxCredential.gpt_email == email))
        if credential is not None:
            return
        binding = parse_noted_mailbox_binding(self.sub2api.account_notes(account), email)
        if binding is None:
            return
        db.add(
            MailboxCredential(
                gpt_email=binding.gpt_email,
                mailbox_email=binding.mailbox_email,
                provider="url",
                custom_fetch_url=binding.custom_fetch_url,
                disabled=False,
                last_error=None,
            )
        )

    async def _mark_missing_mailbox(
        self,
        email: str,
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        reason = "No enabled mailbox credential exists for this GPT account; account was checked only and refresh was not queued."
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                should_record = await self._mark_missing_mailbox(email, db=owned_db)
                await owned_db.commit()
                if should_record:
                    await _record_event_best_effort(
                        owned_db,
                        "refresh_skipped_missing_mailbox",
                        reason,
                        email,
                    )
            return should_record

        snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
        should_record = snapshot is not None and snapshot.last_error != reason
        if snapshot:
            snapshot.refreshing = False
            snapshot.last_error = reason
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
        return should_record

    async def _mark_recovery_disabled(
        self,
        email: str,
        *,
        db: AsyncSession | None = None,
    ) -> bool:
        reason = "Recovery is disabled in settings; account was checked only and refresh was not queued."
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                should_record = await self._mark_recovery_disabled(email, db=owned_db)
                await owned_db.commit()
                if should_record:
                    await _record_event_best_effort(
                        owned_db,
                        "refresh_skipped_recovery_disabled",
                        reason,
                        email,
                    )
            return should_record

        snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
        should_record = snapshot is not None and snapshot.last_error != reason
        if snapshot:
            snapshot.refreshing = False
            snapshot.last_error = reason
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
        return should_record

    async def _record_sync_exception(
        self,
        email: str,
        account: dict,
        reason: str | None = None,
        effective_deactive: bool = False,
        db: AsyncSession | None = None,
    ) -> None:
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                await self._record_sync_exception(
                    email,
                    account,
                    reason,
                    effective_deactive,
                    db=owned_db,
                )
                await owned_db.commit()
                return
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
            commit=False,
        )

    async def _clear_account_exceptions(
        self,
        email: str,
        sub2api_account_id: str | None,
        *,
        db: AsyncSession | None = None,
    ) -> None:
        if db is None:
            async with AsyncSessionLocal() as owned_db:
                await self._clear_account_exceptions(
                    email,
                    sub2api_account_id,
                    db=owned_db,
                )
                await owned_db.commit()
                return
        await clear_account_exception(
            db,
            source="sync",
            email=email,
            sub2api_account_id=sub2api_account_id,
            commit=False,
        )


def _persistable_oauth_credential(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    lowered = text.casefold()
    if lowered in {"redacted", "***redacted***", "[redacted]", "***", "********"}:
        return False
    return not (len(text) >= 3 and set(text) <= {"*"})


_monitor_service: MonitorService | None = None


def get_monitor_service() -> MonitorService:
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = MonitorService()
    return _monitor_service
