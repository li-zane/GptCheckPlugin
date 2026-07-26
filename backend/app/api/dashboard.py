from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.models import AccountSnapshot, MailboxCredential, RefreshJob, UpstreamChannel
from app.schemas import DashboardSummary
from app.services.runtime_config import get_runtime_config_service
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
    runtime_config = get_runtime_config_service()
    show_stale = await runtime_config.get_show_stale_negative_balance_alert()
    basis_getter = getattr(runtime_config, "get_upstream_negative_balance_basis", None)
    threshold_getter = getattr(runtime_config, "get_upstream_balance_pause_threshold", None)
    balance_basis = str(await basis_getter()) if basis_getter else "wallet"
    balance_threshold = float(await threshold_getter()) if threshold_getter else 0.0
    historical_balance = UpstreamChannel.balance_remaining
    historical_balance_ready = UpstreamChannel.balance_remaining.is_not(None)
    if balance_basis == "recharge_adjusted":
        historical_balance = (
            UpstreamChannel.balance_remaining
            * UpstreamChannel.effective_recharge_multiplier
        )
        historical_balance_ready = and_(
            historical_balance_ready,
            UpstreamChannel.effective_recharge_multiplier.is_not(None),
        )
    low_balance_filter = UpstreamChannel.balance_guard_state == "insufficient"
    if show_stale:
        low_balance_filter = or_(
            low_balance_filter,
            and_(
                historical_balance_ready,
                historical_balance < balance_threshold,
                UpstreamChannel.balance_checked_at.is_not(None),
                UpstreamChannel.balance_source == "upstream_wallet",
            ),
        )
    low_balance_result = await db.execute(
        select(UpstreamChannel)
        .where(low_balance_filter)
        .order_by(UpstreamChannel.display_name, UpstreamChannel.id)
    )
    low_balance_channels = []
    for channel in low_balance_result.scalars().all():
        guard_balance_available = (
            channel.balance_guard_state == "insufficient"
            and channel.balance_guard_value is not None
        )
        historical_value = channel.balance_remaining
        if (
            not guard_balance_available
            and balance_basis == "recharge_adjusted"
            and historical_value is not None
            and channel.effective_recharge_multiplier is not None
        ):
            historical_value *= channel.effective_recharge_multiplier
        output_basis = (
            channel.balance_guard_basis
            if guard_balance_available
            else balance_basis
        )
        low_balance_channels.append(
            {
                "id": channel.id,
                "name": channel.display_name,
                "balance": (
                    channel.balance_guard_value
                    if guard_balance_available
                    else historical_value
                ),
                "unit": (
                    "CNY"
                    if output_basis == "recharge_adjusted"
                    else channel.balance_unit
                ),
                "basis": output_basis,
                "threshold": balance_threshold,
                "paused_accounts": channel.balance_guard_paused_count,
                "checked_at": (
                    channel.balance_guard_checked_at
                    if guard_balance_available
                    else channel.balance_checked_at
                ),
            }
        )

    return DashboardSummary(
        total_accounts=total_accounts,
        error_accounts=error_accounts,
        paused_accounts=paused_accounts,
        deactive_accounts=deactive_accounts,
        refreshing_accounts=refreshing_accounts,
        mailbox_count=mailbox_count,
        recent_success=recent_success,
        recent_failed=recent_failed,
        low_balance_channel_count=len(low_balance_channels),
        low_balance_channels=low_balance_channels,
    )
