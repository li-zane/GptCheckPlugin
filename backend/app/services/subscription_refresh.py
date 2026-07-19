import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, MailboxCredential, utcnow
from app.services.chatgpt_account import ChatGptAccessTokenInvalid, ChatGptAccountStatusChecker
from app.services.chatgpt_protocol import ChatGptProtocolRefresher
from app.services.mail import MailAdapterRegistry
from app.services.sub2api import Sub2ApiClient


SENSITIVE_ERROR_RE = re.compile(
    r"(?i)((?:access|refresh|id)_token|rt|api_key|password|client_secret|authorization)([\"'\s:=]+)[^\s,}\"']+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
SUBSCRIPTION_KEYS = (
    "subscription_starts_at",
    "subscription_expires_at",
    "subscription_renews_at",
    "subscription_cancels_at",
    "subscription_billing_period",
    "subscription_plan",
    "has_active_subscription",
)
SUBSCRIPTION_DETAIL_KEYS = (*SUBSCRIPTION_KEYS, "plan_type")


def _redact_error_text(value: str) -> str:
    text = BEARER_RE.sub("Bearer ***redacted***", value)
    text = SENSITIVE_ERROR_RE.sub(r"\1\2***redacted***", text)
    return text[:500]


async def refresh_subscriptions(
    protocol_limit: int = 3,
    max_concurrency: int = 1,
    *,
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    sub2api = Sub2ApiClient(settings)
    checker = ChatGptAccountStatusChecker(settings)
    protocol = ChatGptProtocolRefresher(settings)
    mail = MailAdapterRegistry()
    source_accounts = accounts if accounts is not None else await sub2api.list_accounts()
    accounts, _ = sub2api.dedupe_accounts_by_email(
        [
            account
            for account in source_accounts
            if sub2api.is_gpt_account(account) and sub2api.is_oauth_account(account)
        ]
    )
    snapshots = await _load_snapshots()
    mailboxes = await _load_mailboxes()
    effective_concurrency = len(accounts) if max_concurrency == 0 else max_concurrency
    semaphore = asyncio.Semaphore(max(1, effective_concurrency))
    failures: list[dict[str, str | None]] = []
    refreshed = 0
    skipped = 0
    no_subscription_fields = 0
    protocol_attempts = 0

    async def refresh_one(account: dict[str, Any]) -> None:
        nonlocal refreshed, skipped, no_subscription_fields, protocol_attempts
        email = sub2api.account_email(account)
        account_id = sub2api.account_id(account)
        if not email or not _account_is_available(sub2api, account):
            skipped += 1
            return
        normalized = email.lower()
        snapshot = snapshots.get(normalized)
        remote_metadata = _account_subscription_metadata(account)
        if _snapshot_has_full_subscription(snapshot):
            skipped += 1
            return
        if _has_full_subscription_time(remote_metadata):
            await _save_subscription_metadata(normalized, account, sub2api, remote_metadata)
            refreshed += 1
            return

        async with semaphore:
            try:
                session = await _subscription_session(sub2api, checker, account, snapshot)
                if session is None:
                    mailbox = mailboxes.get(normalized)
                    if mailbox is None:
                        skipped += 1
                        return
                    if protocol_attempts >= protocol_limit:
                        skipped += 1
                        return
                    protocol_attempts += 1
                    session = await _protocol_subscription_session(
                        checker=checker,
                        protocol=protocol,
                        mail=mail,
                        settings=settings,
                        mailbox=mailbox,
                        email=normalized,
                    )
                if session is None:
                    skipped += 1
                    return

                credentials = _merge_subscription_metadata(
                    sub2api.subscription_credentials_from_session(session, ""),
                    remote_metadata,
                )
                if not _has_subscription_details(credentials):
                    no_subscription_fields += 1
                    return
                await _save_subscription_metadata(normalized, account, sub2api, credentials)
                refreshed += 1
            except Exception as exc:
                failures.append(
                    {
                        "email": normalized,
                        "account_id": account_id,
                        "error": _redact_error_text(str(exc)),
                    }
                )

    await asyncio.gather(*(refresh_one(account) for account in accounts))

    failed = len(failures)
    message = f"Refreshed subscription status for {refreshed} account(s)."
    if refreshed == 0 and skipped:
        message = "No subscription status was refreshed; no eligible account needed refresh or no mailbox/access token was available."
    if no_subscription_fields:
        message += f" {no_subscription_fields} account(s) returned no subscription fields."
    if failed:
        message += f" {failed} account(s) failed."
    if protocol_attempts:
        message += f" Protocol login was attempted for {protocol_attempts} account(s)."
    return {
        "message": message,
        "total": len(accounts),
        "refreshed": refreshed,
        "skipped": skipped,
        "no_subscription_fields": no_subscription_fields,
        "protocol_attempts": protocol_attempts,
        "failed": failed,
        "failures": failures,
    }


async def _subscription_session(
    sub2api: Sub2ApiClient,
    checker: ChatGptAccountStatusChecker,
    account: dict[str, Any],
    snapshot: AccountSnapshot | None = None,
) -> dict[str, Any] | None:
    status = await sub2api.check_openai_account_status(account)
    if status:
        if status.get("deactive") or status.get("token_valid") is False:
            return None
        return status

    access_token = sub2api.account_access_token(account)
    if not access_token and snapshot is not None:
        access_token = decrypt_text(snapshot.encrypted_openai_access_token)
    if not access_token:
        return None
    try:
        checked = await checker.check(access_token)
    except ChatGptAccessTokenInvalid:
        return None
    if checked.deactive:
        return None
    return checked.session


async def _protocol_subscription_session(
    *,
    checker: ChatGptAccountStatusChecker,
    protocol: ChatGptProtocolRefresher,
    mail: MailAdapterRegistry,
    settings: Settings,
    mailbox: MailboxCredential,
    email: str,
) -> dict[str, Any] | None:
    async def fetch_code(after: datetime) -> str | None:
        deadline = asyncio.get_running_loop().time() + settings.verification_code_timeout_seconds
        lookup_after = after - timedelta(seconds=settings.verification_code_lookup_grace_seconds)
        while asyncio.get_running_loop().time() < deadline:
            result = await mail.fetch_code(mailbox, lookup_after)
            if result.new_refresh_token or result.new_access_token:
                await _mail_success(mailbox.id, result.new_refresh_token, result.new_access_token)
            if result.status == "ok" and result.code:
                await _mail_success(mailbox.id, result.new_refresh_token, result.new_access_token)
                return result.code
            if result.status == "failed":
                await _mail_error(mailbox.id, result.error or "Mailbox fetch failed.")
                return None
            await asyncio.sleep(settings.verification_code_poll_seconds)
        await _mail_error(mailbox.id, "No verification code found before timeout.")
        return None

    outcome = await protocol.refresh_access_token(email, fetch_code)
    if outcome.status != "ok" or not outcome.access_token:
        raise RuntimeError(outcome.error or "Protocol subscription refresh did not return an access token.")
    checked = await checker.check(outcome.access_token)
    if checked.deactive:
        return None
    return checked.session


async def _load_snapshots() -> dict[str, AccountSnapshot]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AccountSnapshot))
        return {snapshot.email.lower(): snapshot for snapshot in result.scalars().all()}


async def _load_mailboxes() -> dict[str, MailboxCredential]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(MailboxCredential).where(MailboxCredential.disabled.is_(False)))
        return {mailbox.gpt_email.lower(): mailbox for mailbox in result.scalars().all()}


def _account_is_available(sub2api: Sub2ApiClient, account: dict[str, Any]) -> bool:
    if sub2api.is_deactive_account(account) or sub2api.is_error_account(account):
        return False
    return sub2api.account_schedulable(account) is not False


def _snapshot_has_full_subscription(snapshot: AccountSnapshot | None) -> bool:
    return bool(snapshot and snapshot.subscription_starts_at and snapshot.subscription_expires_at)


def _has_full_subscription_time(metadata: dict[str, Any]) -> bool:
    return bool(metadata.get("subscription_starts_at") and metadata.get("subscription_expires_at"))


def _has_subscription_details(metadata: dict[str, Any]) -> bool:
    if any(metadata.get(key) not in (None, "") for key in SUBSCRIPTION_DETAIL_KEYS):
        return True
    return isinstance(metadata.get("has_active_subscription"), bool)


def _merge_subscription_metadata(
    primary: dict[str, Any],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    return {
        key: primary.get(key) if primary.get(key) not in (None, "") else fallback.get(key)
        for key in SUBSCRIPTION_DETAIL_KEYS
    }


def _account_subscription_metadata(account: dict[str, Any]) -> dict[str, Any]:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    metadata = {key: credentials.get(key) for key in SUBSCRIPTION_KEYS}
    metadata["plan_type"] = credentials.get("plan_type") or credentials.get("planType")
    metadata["subscription_plan"] = metadata["plan_type"] or metadata.get("subscription_plan")
    return metadata


async def _save_subscription_metadata(
    email: str,
    account: dict[str, Any],
    sub2api: Sub2ApiClient,
    metadata: dict[str, Any],
) -> None:
    async with AsyncSessionLocal() as db:
        snapshot = await db.scalar(select(AccountSnapshot).where(AccountSnapshot.email == email))
        if snapshot is None:
            snapshot = AccountSnapshot(email=email)
            snapshot.usage_estimate_enabled = not sub2api.is_deactive_account(account)
            db.add(snapshot)
        snapshot.sub2api_account_id = sub2api.account_id(account)
        snapshot.platform = sub2api.account_platform(account)
        snapshot.account_type = sub2api.account_type(account)
        snapshot.status = sub2api.account_status(account)
        snapshot.schedulable = sub2api.account_schedulable(account)
        snapshot.deactive = sub2api.is_deactive_account(account)
        _set_if_present(snapshot, "subscription_starts_at", metadata.get("subscription_starts_at"))
        _set_if_present(snapshot, "subscription_expires_at", metadata.get("subscription_expires_at"))
        _set_if_present(snapshot, "subscription_renews_at", metadata.get("subscription_renews_at"))
        _set_if_present(snapshot, "subscription_cancels_at", metadata.get("subscription_cancels_at"))
        _set_if_present(snapshot, "subscription_billing_period", metadata.get("subscription_billing_period"))
        _set_if_present(
            snapshot,
            "subscription_plan",
            metadata.get("plan_type") or metadata.get("subscription_plan"),
        )
        active_subscription = metadata.get("has_active_subscription")
        if isinstance(active_subscription, bool):
            snapshot.has_active_subscription = active_subscription
        snapshot.subscription_checked_at = utcnow()
        snapshot.last_seen_at = utcnow()
        await db.commit()


async def _mail_error(credential_id: int, error: str) -> None:
    async with AsyncSessionLocal() as db:
        credential = await db.get(MailboxCredential, credential_id)
        if credential:
            credential.last_error = error
            await db.commit()


async def _mail_success(
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


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _set_if_present(snapshot: AccountSnapshot, key: str, value: Any) -> None:
    text = _string_or_none(value)
    if text is not None:
        setattr(snapshot, key, text)
