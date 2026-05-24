from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import AccountSnapshot, AppEvent, MailboxCredential, RefreshJob
from app.schemas import (
    AccountSnapshotOut,
    DeactivatedCleanupResult,
    EventOut,
    ManualRefreshRequest,
    MessageResponse,
    RefreshJobOut,
    Sub2ApiSyncResult,
    UsageEstimateOut,
    UsageEstimatePreferenceUpdate,
    UsageRefreshFailureOut,
    UsageRefreshResult,
)
from app.services.events import record_event
from app.services.monitor import get_monitor_service
from app.services.refresh import get_refresh_service
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError
from app.services.usage_estimate import build_usage_estimate
from app.services.usage_refresh import get_usage_refresh_service

router = APIRouter()


@router.get("", response_model=list[AccountSnapshotOut])
async def list_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AccountSnapshotOut]:
    result = await db.execute(select(AccountSnapshot).order_by(desc(AccountSnapshot.updated_at)))
    snapshots = list(result.scalars().all())
    mailbox_result = await db.execute(
        select(MailboxCredential.gpt_email).where(MailboxCredential.disabled.is_(False))
    )
    bound_emails = {email.lower() for email in mailbox_result.scalars().all()}

    for snapshot in snapshots:
        snapshot.mailbox_bound = snapshot.email.lower() in bound_emails
    return [AccountSnapshotOut.model_validate(snapshot) for snapshot in snapshots]


@router.get("/usage-estimate", response_model=UsageEstimateOut)
async def usage_estimate(
    refresh: bool = Query(default=True),
    _: dict = Depends(require_admin),
) -> UsageEstimateOut:
    try:
        return await build_usage_estimate(refresh=refresh)
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.patch("/{account_id}/usage-estimate", response_model=MessageResponse)
async def update_usage_estimate_preference(
    account_id: int,
    payload: UsageEstimatePreferenceUpdate,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    snapshot = await db.get(AccountSnapshot, account_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account snapshot not found.")
    snapshot.usage_estimate_enabled = payload.enabled
    await db.commit()
    state = "enabled" if payload.enabled else "disabled"
    return MessageResponse(message=f"Usage estimate {state} for {snapshot.email}.")


@router.post("/sync", response_model=Sub2ApiSyncResult)
async def sync_accounts(_: dict = Depends(require_admin)) -> Sub2ApiSyncResult:
    try:
        return await get_monitor_service().sync_once(reason="manual")
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/usage-refresh", response_model=UsageRefreshResult)
async def refresh_usage_windows(_: dict = Depends(require_admin)) -> UsageRefreshResult:
    try:
        result = await get_usage_refresh_service().refresh_all(reason="manual")
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return UsageRefreshResult(
        message=result.message,
        total=result.total,
        refreshed=result.refreshed,
        skipped=result.skipped,
        failed=result.failed,
        failures=[
            UsageRefreshFailureOut(email=failure.email, account_id=failure.account_id, error=failure.error)
            for failure in result.failures
        ],
    )


@router.post("/refresh", response_model=RefreshJobOut)
async def refresh_account(
    payload: ManualRefreshRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RefreshJobOut:
    try:
        job_id = await get_refresh_service().enqueue_by_email(str(payload.email), reason="manual")
    except Sub2ApiRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if job_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find this account in sub2api or it is already refreshing.",
        )
    job = await db.get(RefreshJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refresh job was not created.")
    return job


@router.delete("/deactivated", response_model=DeactivatedCleanupResult)
async def delete_deactivated_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DeactivatedCleanupResult:
    result = await db.execute(select(AccountSnapshot).where(AccountSnapshot.deactive.is_(True)))
    snapshots = list(result.scalars().all())
    if not snapshots:
        return DeactivatedCleanupResult(
            message="No deactivated accounts to delete.",
            deleted_accounts=0,
            deleted_mailboxes=0,
            deleted_sub2api_accounts=0,
            failed_sub2api_accounts=[],
        )

    sub2api = Sub2ApiClient()
    local_snapshot_ids: list[int] = []
    local_emails: list[str] = []
    failed_remote: list[str] = []
    deleted_remote = 0

    for snapshot in snapshots:
        if snapshot.sub2api_account_id:
            try:
                if await sub2api.delete_account(snapshot.sub2api_account_id):
                    deleted_remote += 1
            except Sub2ApiRequestError as exc:
                failed_remote.append(f"{snapshot.email}: {exc}")
                continue
        local_snapshot_ids.append(snapshot.id)
        local_emails.append(snapshot.email)

    deleted_mailboxes = 0
    deleted_accounts = 0
    if local_emails:
        mailbox_result = await db.execute(delete(MailboxCredential).where(MailboxCredential.gpt_email.in_(local_emails)))
        deleted_mailboxes = mailbox_result.rowcount or 0
        account_result = await db.execute(delete(AccountSnapshot).where(AccountSnapshot.id.in_(local_snapshot_ids)))
        deleted_accounts = account_result.rowcount or 0

    await record_event(
        db,
        "deactivated_accounts_deleted",
        f"Deleted {deleted_accounts} deactivated account(s), {deleted_mailboxes} mailbox credential(s), and {deleted_remote} sub2api account(s).",
        details={
            "deleted_accounts": deleted_accounts,
            "deleted_mailboxes": deleted_mailboxes,
            "deleted_sub2api_accounts": deleted_remote,
            "failed_sub2api_accounts": failed_remote,
        },
    )
    message = (
        f"Deleted {deleted_accounts} deactivated account(s), {deleted_mailboxes} mailbox credential(s), "
        f"and {deleted_remote} sub2api account(s)."
    )
    if failed_remote:
        message += f" {len(failed_remote)} sub2api account(s) could not be deleted."
    return DeactivatedCleanupResult(
        message=message,
        deleted_accounts=deleted_accounts,
        deleted_mailboxes=deleted_mailboxes,
        deleted_sub2api_accounts=deleted_remote,
        failed_sub2api_accounts=failed_remote,
    )


@router.get("/jobs", response_model=list[RefreshJobOut])
async def list_jobs(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[RefreshJobOut]:
    result = await db.execute(select(RefreshJob).order_by(desc(RefreshJob.created_at)).limit(100))
    return list(result.scalars().all())


@router.get("/events", response_model=list[EventOut])
async def list_events(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[EventOut]:
    result = await db.execute(select(AppEvent).order_by(desc(AppEvent.created_at)).limit(100))
    return list(result.scalars().all())


@router.delete("/history", response_model=MessageResponse)
async def clear_history(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> MessageResponse:
    await db.execute(delete(RefreshJob))
    await db.execute(delete(AppEvent))
    await db.commit()
    return MessageResponse(message="历史已清空")
