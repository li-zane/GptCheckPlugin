from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import AccountSnapshot, AppEvent, RefreshJob
from app.schemas import AccountSnapshotOut, EventOut, ManualRefreshRequest, RefreshJobOut, Sub2ApiSyncResult
from app.services.monitor import get_monitor_service
from app.services.refresh import get_refresh_service

router = APIRouter()


@router.get("", response_model=list[AccountSnapshotOut])
async def list_accounts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AccountSnapshotOut]:
    result = await db.execute(select(AccountSnapshot).order_by(desc(AccountSnapshot.updated_at)))
    return list(result.scalars().all())


@router.post("/sync", response_model=Sub2ApiSyncResult)
async def sync_accounts(_: dict = Depends(require_admin)) -> Sub2ApiSyncResult:
    return await get_monitor_service().sync_once(reason="manual")


@router.post("/refresh", response_model=RefreshJobOut)
async def refresh_account(
    payload: ManualRefreshRequest,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> RefreshJobOut:
    job_id = await get_refresh_service().enqueue_by_email(str(payload.email), reason="manual")
    if job_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find this account in sub2api or it is already refreshing.",
        )
    job = await db.get(RefreshJob, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refresh job was not created.")
    return job


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
