from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccountSchedulingChangeLog,
    AppSetting,
    UpstreamAccountDataArchive,
    UpstreamChannelChangeEvent,
    utcnow,
)


UPSTREAM_CHANGE_READ_CURSOR = "upstream_change_event_last_read_id"
ACCOUNT_RATE_CHANGE_READ_CURSOR = "account_rate_change_event_last_read_id"
ACCOUNT_SCHEDULING_READ_CURSOR = "account_scheduling_change_last_read_id"
ACCOUNT_RATE_EVENT_TYPE = "account_rate_changed"


def _cursor_value(raw: str | None) -> int:
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


async def _read_cursor(
    db: AsyncSession,
    key: str,
    *,
    fallback_key: str | None = None,
) -> int:
    setting = await db.get(AppSetting, key)
    if setting is None and fallback_key is not None:
        setting = await db.get(AppSetting, fallback_key)
    return _cursor_value(setting.value if setting is not None else None)


async def _effective_cursor(
    db: AsyncSession,
    model: type[Any],
    key: str,
    *,
    fallback_key: str | None = None,
    repair: bool = True,
) -> int:
    cursor = await _read_cursor(db, key, fallback_key=fallback_key)
    maximum = int(await db.scalar(select(func.max(model.id))) or 0)
    if cursor <= maximum:
        return cursor
    setting = await db.get(AppSetting, key)
    if repair and setting is not None:
        # SQLite may reuse low integer primary keys after every row is pruned.
        # Treat existing rows as unread instead of letting a stale high-water
        # mark hide them permanently.
        setting.value = "0"
        await db.flush()
    return 0


async def _mark_read(
    db: AsyncSession,
    model: type[Any],
    key: str,
    through_id: int,
    *,
    fallback_key: str | None = None,
    preserve_ahead: bool = False,
) -> int:
    maximum = int(await db.scalar(select(func.max(model.id))) or 0)
    raw_requested = max(0, int(through_id))
    requested = raw_requested if preserve_ahead else min(raw_requested, maximum)
    setting = await db.get(AppSetting, key)
    current = await _effective_cursor(db, model, key, fallback_key=fallback_key)
    next_value = max(current, requested)
    if setting is None:
        db.add(AppSetting(key=key, value=str(next_value)))
    elif next_value != current:
        setting.value = str(next_value)
    await db.commit()
    if preserve_ahead:
        # Return the highest currently known event ID. If the caller supplied
        # a cursor ahead of the local ledger, retaining that high-water mark
        # lets _effective_cursor restore unread rows until the corresponding
        # records actually exist.
        return min(raw_requested, maximum)
    return next_value


async def mark_upstream_changes_read(
    db: AsyncSession,
    through_id: int,
    *,
    category: str = "all",
) -> int:
    cursor_key = (
        ACCOUNT_RATE_CHANGE_READ_CURSOR
        if category == "account_rate"
        else UPSTREAM_CHANGE_READ_CURSOR
    )
    return await _mark_read(
        db,
        UpstreamChannelChangeEvent,
        cursor_key,
        through_id,
        fallback_key=(
            UPSTREAM_CHANGE_READ_CURSOR if category == "account_rate" else None
        ),
        preserve_ahead=True,
    )


async def mark_account_scheduling_changes_read(db: AsyncSession, through_id: int) -> int:
    return await _mark_read(
        db,
        AccountSchedulingChangeLog,
        ACCOUNT_SCHEDULING_READ_CURSOR,
        through_id,
    )


async def _prune(db: AsyncSession, model: type[Any], retention_days: int) -> None:
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650.")
    cutoff = utcnow() - timedelta(days=retention_days)
    await db.execute(
        delete(model)
        .where(model.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )


async def delete_expired_change_logs(
    db: AsyncSession,
    *,
    retention_days: int,
) -> None:
    await _prune(db, UpstreamChannelChangeEvent, retention_days)
    await _prune(db, AccountSchedulingChangeLog, retention_days)
    await _prune(db, UpstreamAccountDataArchive, retention_days)
    await _effective_cursor(
        db,
        UpstreamChannelChangeEvent,
        UPSTREAM_CHANGE_READ_CURSOR,
    )
    await _effective_cursor(
        db,
        UpstreamChannelChangeEvent,
        ACCOUNT_RATE_CHANGE_READ_CURSOR,
        fallback_key=UPSTREAM_CHANGE_READ_CURSOR,
    )
    await _effective_cursor(
        db,
        AccountSchedulingChangeLog,
        ACCOUNT_SCHEDULING_READ_CURSOR,
    )


async def prune_change_logs(
    db: AsyncSession,
    *,
    retention_days: int,
) -> None:
    await delete_expired_change_logs(db, retention_days=retention_days)
    await db.commit()


async def _list_page(
    db: AsyncSession,
    model: type[Any],
    cursor_key: str,
    *,
    retention_days: int,
    limit: int,
    before_id: int | None,
    start_at: datetime | None,
    end_at: datetime | None,
    where_clause: Any | None = None,
    cursor_fallback_key: str | None = None,
) -> tuple[list[Any], int, int]:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200.")
    if before_id is not None and before_id < 1:
        raise ValueError("before_id must be positive.")
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise ValueError("start_at must be earlier than end_at.")
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650.")
    cursor = await _effective_cursor(
        db,
        model,
        cursor_key,
        fallback_key=cursor_fallback_key,
        repair=False,
    )
    statement = select(model)
    if where_clause is not None:
        statement = statement.where(where_clause)
    if before_id is not None:
        boundary = await db.get(model, before_id)
        if boundary is None:
            statement = statement.where(model.id != model.id)
        else:
            statement = statement.where(
                (model.created_at < boundary.created_at)
                | ((model.created_at == boundary.created_at) & (model.id < boundary.id))
            )
    if start_at is not None:
        statement = statement.where(model.created_at >= start_at)
    if end_at is not None:
        statement = statement.where(model.created_at < end_at)
    rows = list(
        (
            await db.execute(
                statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit)
            )
        ).scalars().all()
    )
    unread_statement = select(func.count()).select_from(model).where(model.id > cursor)
    if where_clause is not None:
        unread_statement = unread_statement.where(where_clause)
    if model is UpstreamChannelChangeEvent:
        unread_statement = unread_statement.where(
            UpstreamChannelChangeEvent.legacy_imported.is_(False)
        )
    unread_count = int(await db.scalar(unread_statement) or 0)
    return rows, cursor, unread_count


async def list_upstream_channel_changes(
    db: AsyncSession,
    *,
    retention_days: int,
    limit: int = 100,
    before_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    category: str = "all",
) -> tuple[list[UpstreamChannelChangeEvent], int, int]:
    if category not in {"all", "upstream", "account_rate"}:
        raise ValueError("Unsupported upstream change category.")
    where_clause = (
        UpstreamChannelChangeEvent.event_type == ACCOUNT_RATE_EVENT_TYPE
        if category == "account_rate"
        else UpstreamChannelChangeEvent.event_type != ACCOUNT_RATE_EVENT_TYPE
        if category == "upstream"
        else None
    )
    return await _list_page(
        db,
        UpstreamChannelChangeEvent,
        (
            ACCOUNT_RATE_CHANGE_READ_CURSOR
            if category == "account_rate"
            else UPSTREAM_CHANGE_READ_CURSOR
        ),
        retention_days=retention_days,
        limit=limit,
        before_id=before_id,
        start_at=start_at,
        end_at=end_at,
        where_clause=where_clause,
        cursor_fallback_key=(
            UPSTREAM_CHANGE_READ_CURSOR if category == "account_rate" else None
        ),
    )


async def list_account_scheduling_changes(
    db: AsyncSession,
    *,
    retention_days: int,
    limit: int = 100,
    before_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> tuple[list[AccountSchedulingChangeLog], int, int]:
    return await _list_page(
        db,
        AccountSchedulingChangeLog,
        ACCOUNT_SCHEDULING_READ_CURSOR,
        retention_days=retention_days,
        limit=limit,
        before_id=before_id,
        start_at=start_at,
        end_at=end_at,
    )


async def change_log_unread_counts(
    db: AsyncSession,
    *,
    retention_days: int | None = None,
) -> tuple[int, int, int]:
    if retention_days is not None and not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650.")
    upstream_cursor = await _effective_cursor(
        db,
        UpstreamChannelChangeEvent,
        UPSTREAM_CHANGE_READ_CURSOR,
        repair=False,
    )
    scheduling_cursor = await _effective_cursor(
        db,
        AccountSchedulingChangeLog,
        ACCOUNT_SCHEDULING_READ_CURSOR,
        repair=False,
    )
    account_rate_cursor = await _effective_cursor(
        db,
        UpstreamChannelChangeEvent,
        ACCOUNT_RATE_CHANGE_READ_CURSOR,
        fallback_key=UPSTREAM_CHANGE_READ_CURSOR,
        repair=False,
    )
    upstream = int(
        await db.scalar(
            select(func.count())
            .select_from(UpstreamChannelChangeEvent)
            .where(
                UpstreamChannelChangeEvent.id > upstream_cursor,
                UpstreamChannelChangeEvent.legacy_imported.is_(False),
                UpstreamChannelChangeEvent.event_type != ACCOUNT_RATE_EVENT_TYPE,
            )
        )
        or 0
    )
    account_rate = int(
        await db.scalar(
            select(func.count())
            .select_from(UpstreamChannelChangeEvent)
            .where(
                UpstreamChannelChangeEvent.id > account_rate_cursor,
                UpstreamChannelChangeEvent.legacy_imported.is_(False),
                UpstreamChannelChangeEvent.event_type == ACCOUNT_RATE_EVENT_TYPE,
            )
        )
        or 0
    )
    scheduling = int(
        await db.scalar(
            select(func.count())
            .select_from(AccountSchedulingChangeLog)
            .where(AccountSchedulingChangeLog.id > scheduling_cursor)
        )
        or 0
    )
    return upstream, account_rate, scheduling


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _numbers_differ(old_value: float | None, new_value: float | None) -> bool:
    if old_value is None or new_value is None:
        return old_value is not None or new_value is not None
    return not math.isclose(old_value, new_value, rel_tol=1e-12, abs_tol=1e-12)


def _group_map(groups: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in groups or []:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or "").strip()
        if group_id:
            result[group_id] = item
    return result


def _recharge_multiplier_details(
    old_recharge: float | None,
    new_recharge: float | None,
) -> dict[str, float]:
    details: dict[str, float] = {}
    if old_recharge is not None:
        details["old_recharge_multiplier"] = old_recharge
    if new_recharge is not None:
        details["new_recharge_multiplier"] = new_recharge
    return details


def record_upstream_channel_changes(
    db: AsyncSession,
    *,
    channel_id: int,
    channel_name: str | None,
    previous_recharge_multiplier: Any,
    current_recharge_multiplier: Any,
    previous_groups: list[dict[str, Any]] | None,
    current_groups: list[dict[str, Any]] | None,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    group_changes: list[dict[str, Any]] = []
    old_recharge = _finite_number(previous_recharge_multiplier)
    new_recharge = _finite_number(current_recharge_multiplier)
    # An unavailable probe is not evidence of a billing change. Both sides
    # must be known before emitting a channel recharge multiplier event.
    if (
        old_recharge is not None
        and new_recharge is not None
        and _numbers_differ(old_recharge, new_recharge)
    ):
        db.add(
            UpstreamChannelChangeEvent(
                channel_id=channel_id,
                channel_name=(channel_name or "")[:200] or None,
                event_type="channel_multiplier_changed",
                old_value=old_recharge,
                new_value=new_recharge,
                created_at=observed_at,
            )
        )

    old_groups = _group_map(previous_groups)
    new_groups = _group_map(current_groups)
    recharge_details = _recharge_multiplier_details(old_recharge, new_recharge)
    for group_id in sorted(old_groups.keys() | new_groups.keys()):
        old_group = old_groups.get(group_id)
        new_group = new_groups.get(group_id)
        source = new_group or old_group or {}
        group_name = str(source.get("name") or "").strip()[:200] or None
        if old_group is None:
            change = {
                "change_type": "added",
                "group_id": group_id[:128],
                "group_name": group_name,
                "old_status": "absent",
                "new_status": "available",
                "old_multiplier": None,
                "new_multiplier": _finite_number(new_group.get("multiplier") if new_group else None),
            }
            group_changes.append(change)
            db.add(
                UpstreamChannelChangeEvent(
                    channel_id=channel_id,
                    channel_name=(channel_name or "")[:200] or None,
                    event_type="group_added",
                    group_id=group_id[:128],
                    group_name=group_name,
                    old_status="absent",
                    new_status="available",
                    new_value=_finite_number(new_group.get("multiplier") if new_group else None),
                    details=recharge_details or None,
                    created_at=observed_at,
                )
            )
            continue
        if new_group is None:
            change = {
                "change_type": "removed",
                "group_id": group_id[:128],
                "group_name": group_name,
                "old_status": "available",
                "new_status": "removed",
                "old_multiplier": _finite_number(old_group.get("multiplier")),
                "new_multiplier": None,
            }
            group_changes.append(change)
            db.add(
                UpstreamChannelChangeEvent(
                    channel_id=channel_id,
                    channel_name=(channel_name or "")[:200] or None,
                    event_type="group_removed",
                    group_id=group_id[:128],
                    group_name=group_name,
                    old_status="available",
                    new_status="removed",
                    old_value=_finite_number(old_group.get("multiplier")),
                    created_at=observed_at,
                )
            )
            continue
        old_value = _finite_number(old_group.get("multiplier"))
        new_value = _finite_number(new_group.get("multiplier"))
        old_name = str(old_group.get("name") or "").strip()[:200] or None
        new_name = str(new_group.get("name") or "").strip()[:200] or None
        if old_name != new_name:
            name_change_details = {
                **recharge_details,
                "old_name": old_name,
                "new_name": new_name,
            }
            group_changes.append({
                "change_type": "renamed",
                "group_id": group_id[:128],
                "group_name": new_name or old_name,
                "old_status": "available",
                "new_status": "available",
                "old_multiplier": old_value,
                "new_multiplier": new_value,
                "details": name_change_details,
            })
            db.add(
                UpstreamChannelChangeEvent(
                    channel_id=channel_id,
                    channel_name=(channel_name or "")[:200] or None,
                    event_type="group_name_changed",
                    group_id=group_id[:128],
                    group_name=new_name or old_name,
                    old_value=old_value,
                    new_value=new_value,
                    details=name_change_details,
                    created_at=observed_at,
                )
            )
        if _numbers_differ(old_value, new_value):
            group_changes.append({
                "change_type": "multiplier",
                "group_id": group_id[:128],
                "group_name": group_name,
                "old_status": "available",
                "new_status": "available",
                "old_multiplier": old_value,
                "new_multiplier": new_value,
            })
            db.add(
                UpstreamChannelChangeEvent(
                    channel_id=channel_id,
                    channel_name=(channel_name or "")[:200] or None,
                    event_type="group_multiplier_changed",
                    group_id=group_id[:128],
                    group_name=group_name,
                    old_value=old_value,
                    new_value=new_value,
                    details=recharge_details or None,
                    created_at=observed_at,
                )
            )
    return group_changes
