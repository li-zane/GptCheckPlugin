from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountExceptionRecord, AccountSnapshot, AppSetting, utcnow


_BACKFILL_KEY = "migration.account_exception_records.current_only_backfilled"


def account_exception_fingerprint(source: str, email: str | None = None, sub2api_account_id: str | None = None) -> str:
    source_key = str(source or "account").strip().lower() or "account"
    email_key = str(email or "").strip().lower()
    account_key = str(sub2api_account_id or "").strip().lower()
    if email_key:
        return f"{source_key}:email:{email_key}"
    if account_key:
        return f"{source_key}:account:{account_key}"
    return f"{source_key}:unknown"


async def upsert_account_exception(
    db: AsyncSession,
    *,
    source: str,
    status: str,
    message: str,
    email: str | None = None,
    sub2api_account_id: str | None = None,
    details: dict[str, Any] | None = None,
    commit: bool = True,
) -> AccountExceptionRecord:
    normalized_email = str(email or "").strip().lower() or None
    account_id = str(sub2api_account_id or "").strip() or None
    fingerprint = account_exception_fingerprint(source, normalized_email, account_id)
    now = utcnow()
    record = await db.scalar(select(AccountExceptionRecord).where(AccountExceptionRecord.fingerprint == fingerprint))
    if record is None:
        record = AccountExceptionRecord(
            fingerprint=fingerprint,
            email=normalized_email,
            sub2api_account_id=account_id,
            source=source,
            status=status,
            message=message,
            details=details,
            created_at=now,
            updated_at=now,
        )
        db.add(record)
    else:
        record.email = normalized_email
        record.sub2api_account_id = account_id
        record.source = source
        record.status = status
        record.message = message
        record.details = details
        record.updated_at = now
    if commit:
        await db.commit()
        await db.refresh(record)
    return record


async def clear_account_exception(
    db: AsyncSession,
    *,
    source: str,
    email: str | None = None,
    sub2api_account_id: str | None = None,
    commit: bool = True,
) -> int:
    fingerprint = account_exception_fingerprint(source, email, sub2api_account_id)
    result = await db.execute(delete(AccountExceptionRecord).where(AccountExceptionRecord.fingerprint == fingerprint))
    if commit:
        await db.commit()
    return result.rowcount or 0


async def backfill_account_exception_records(db: AsyncSession) -> None:
    if await db.get(AppSetting, _BACKFILL_KEY) is not None:
        return

    snapshot_result = await db.execute(
        select(AccountSnapshot)
        .where((AccountSnapshot.deactive.is_(True)) | (AccountSnapshot.last_error.is_not(None)))
        .order_by(desc(AccountSnapshot.updated_at))
        .limit(100)
    )
    for snapshot in snapshot_result.scalars().all():
        status = "deactive" if snapshot.deactive else "error"
        await upsert_account_exception(
            db,
            source="sync",
            status=status,
            message=snapshot.last_error or "Account snapshot is in an exception state.",
            email=snapshot.email,
            sub2api_account_id=snapshot.sub2api_account_id,
            details={"snapshot_id": snapshot.id, "backfilled": True},
            commit=False,
        )

    db.add(AppSetting(key=_BACKFILL_KEY, value="1"))
    await db.commit()


async def prune_account_exception_records_to_current_accounts(db: AsyncSession) -> None:
    snapshot_result = await db.execute(
        select(AccountSnapshot.email).where((AccountSnapshot.deactive.is_(True)) | (AccountSnapshot.last_error.is_not(None)))
    )
    current_error_emails = {email.lower() for email in snapshot_result.scalars().all() if email}
    await db.execute(delete(AccountExceptionRecord).where(AccountExceptionRecord.source != "sync"))
    if current_error_emails:
        await db.execute(
            delete(AccountExceptionRecord).where(
                AccountExceptionRecord.source == "sync",
                (AccountExceptionRecord.email.is_(None)) | (~func.lower(AccountExceptionRecord.email).in_(current_error_emails)),
            )
        )
    else:
        await db.execute(delete(AccountExceptionRecord).where(AccountExceptionRecord.source == "sync"))
    await db.commit()
