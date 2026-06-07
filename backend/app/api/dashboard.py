from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import AccountSnapshot, MailboxCredential, RefreshJob
from app.schemas import DashboardSummary
from app.services.sub2api import HEALTHY_ACCOUNT_STATUSES

router = APIRouter()


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def dashboard_summary(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DashboardSummary:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    total_accounts = await db.scalar(select(func.count()).select_from(AccountSnapshot)) or 0
    lowered_status = func.lower(func.coalesce(AccountSnapshot.status, ""))
    healthy_statuses = tuple(HEALTHY_ACCOUNT_STATUSES)
    manually_paused = and_(
        AccountSnapshot.schedulable.is_(False),
        lowered_status == "active",
    )
    current_error = or_(
        lowered_status.like("%error%"),
        lowered_status.like("%fail%"),
        lowered_status.like("%invalid%"),
        lowered_status.like("%expired%"),
        lowered_status.like("%disabled%"),
        and_(AccountSnapshot.schedulable.is_(False), ~manually_paused),
    )
    error_accounts = await db.scalar(
        select(func.count())
        .select_from(AccountSnapshot)
        .where(
            AccountSnapshot.deactive.is_(False),
            or_(
                current_error,
                and_(
                    AccountSnapshot.last_error.is_not(None),
                    AccountSnapshot.schedulable.is_not(True),
                    lowered_status.not_in(healthy_statuses),
                ),
            ),
        )
    ) or 0
    paused_accounts = await db.scalar(
        select(func.count())
        .select_from(AccountSnapshot)
        .where(
            AccountSnapshot.deactive.is_(False),
            manually_paused,
        )
    ) or 0
    deactive_accounts = await db.scalar(
        select(func.count()).select_from(AccountSnapshot).where(AccountSnapshot.deactive.is_(True))
    ) or 0
    refreshing_accounts = await db.scalar(
        select(func.count()).select_from(AccountSnapshot).where(AccountSnapshot.refreshing.is_(True))
    ) or 0
    mailbox_count = await db.scalar(select(func.count()).select_from(MailboxCredential)) or 0
    recent_success = await db.scalar(
        select(func.count())
        .select_from(RefreshJob)
        .where(RefreshJob.status == "succeeded", RefreshJob.created_at >= since)
    ) or 0
    recent_failed = await db.scalar(
        select(func.count())
        .select_from(RefreshJob)
        .where(RefreshJob.status.in_(["failed", "deactive"]), RefreshJob.created_at >= since)
    ) or 0

    return DashboardSummary(
        total_accounts=total_accounts,
        error_accounts=error_accounts,
        paused_accounts=paused_accounts,
        deactive_accounts=deactive_accounts,
        refreshing_accounts=refreshing_accounts,
        mailbox_count=mailbox_count,
        recent_success=recent_success,
        recent_failed=recent_failed,
    )
