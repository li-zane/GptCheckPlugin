from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UpstreamRateChangeLog, utcnow


async def prune_upstream_rate_change_logs(
    db: AsyncSession,
    *,
    retention_days: int,
) -> int:
    deleted = await delete_expired_upstream_rate_change_logs(
        db,
        retention_days=retention_days,
    )
    if deleted:
        await db.commit()
    return deleted


async def delete_expired_upstream_rate_change_logs(
    db: AsyncSession,
    *,
    retention_days: int,
) -> int:
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650.")
    cutoff = utcnow() - timedelta(days=retention_days)
    result = await db.execute(
        delete(UpstreamRateChangeLog).where(UpstreamRateChangeLog.created_at < cutoff)
    )
    return int(result.rowcount or 0)


async def list_upstream_rate_change_logs(
    db: AsyncSession,
    *,
    retention_days: int,
    limit: int = 100,
    before_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> list[UpstreamRateChangeLog]:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200.")
    if before_id is not None and before_id < 1:
        raise ValueError("before_id must be positive.")
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise ValueError("start_at must be earlier than end_at.")

    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650.")
    changed_fields = (
        (UpstreamRateChangeLog.old_group_multiplier, UpstreamRateChangeLog.new_group_multiplier),
        (UpstreamRateChangeLog.old_upstream_multiplier, UpstreamRateChangeLog.new_upstream_multiplier),
        (UpstreamRateChangeLog.old_expected_management_billing_multiplier, UpstreamRateChangeLog.new_expected_management_billing_multiplier),
        (UpstreamRateChangeLog.old_management_billing_multiplier, UpstreamRateChangeLog.new_management_billing_multiplier),
        (UpstreamRateChangeLog.old_upstream_key_status, UpstreamRateChangeLog.new_upstream_key_status),
        (UpstreamRateChangeLog.old_upstream_group_status, UpstreamRateChangeLog.new_upstream_group_status),
        (UpstreamRateChangeLog.old_remote_schedulable, UpstreamRateChangeLog.new_remote_schedulable),
    )
    statement = select(UpstreamRateChangeLog).where(
        or_(
            UpstreamRateChangeLog.status.in_(
                ("apply_failed", "disable_failed", "account_disabled", "already_disabled")
            ),
            *(
                and_(old.is_not(None), new.is_not(None), old != new)
                for old, new in changed_fields
            )
        )
    )
    if before_id is not None:
        statement = statement.where(UpstreamRateChangeLog.id < before_id)
    if start_at is not None:
        statement = statement.where(UpstreamRateChangeLog.created_at >= start_at)
    if end_at is not None:
        statement = statement.where(UpstreamRateChangeLog.created_at < end_at)
    result = await db.execute(
        statement.order_by(UpstreamRateChangeLog.id.desc()).limit(limit)
    )
    return list(result.scalars().all())
