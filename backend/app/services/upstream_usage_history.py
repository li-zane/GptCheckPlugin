from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    UpstreamAccountConfig,
    UpstreamAccountDailyUsage,
    UpstreamChannel,
    UpstreamChannelDailyUsage,
    UpstreamChannelUsageTotal,
)


DEFAULT_TIME_ZONE = "Asia/Shanghai"
VALID_DAILY_USAGE_STATUSES = frozenset({"ok", "stale", "estimated", "stored"})
MAX_HISTORY_DAYS = 3660


def usage_day(now: datetime, time_zone: str) -> date:
    """Return the configured local calendar day, falling back deterministically."""

    normalized_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    try:
        zone = ZoneInfo(str(time_zone or "").strip())
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        zone = ZoneInfo(DEFAULT_TIME_ZONE)
    return normalized_now.astimezone(zone).date()


def normalize_history_time_zone(value: str | None) -> str:
    candidate = str(value or "").strip() or DEFAULT_TIME_ZONE
    try:
        ZoneInfo(candidate)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("The time zone is invalid.") from exc
    return candidate


def _finite_amount(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


def _safe_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:limit] if normalized else None


def channel_identity(channel: UpstreamChannel) -> str:
    """Return the durable upstream identity used by accounting rows."""

    return _safe_text(channel.canonical_base_url, limit=500) or f"channel:{channel.id}"


def _snapshot_multiplier(channel: UpstreamChannel, existing: float | None = None) -> float | None:
    return _finite_amount(
        existing
        if existing is not None
        else channel.effective_recharge_multiplier
        if channel.effective_recharge_multiplier is not None
        else channel.last_known_recharge_multiplier
    )


def _adjusted(amount: float | None, multiplier: float | None) -> float | None:
    if amount is None or multiplier is None:
        return None
    adjusted = amount * multiplier
    return adjusted if math.isfinite(adjusted) else None


def _stats_amount(stats: Any, field: str) -> float | None:
    if isinstance(stats, dict):
        return _finite_amount(stats.get(field))
    return _finite_amount(getattr(stats, field, None))


def _maximum_cost(*values: float | None) -> float | None:
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _sum_known(values: Iterable[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _profit(
    income: float | None,
    upstream_cost_cny: float | None,
    sub2api_cost_cny: float | None,
) -> float | None:
    consumption = _maximum_cost(upstream_cost_cny, sub2api_cost_cny)
    if income is None or consumption is None:
        return None
    value = income - consumption
    return value if math.isfinite(value) else None


def _profit_margin(consumption_cny: float | None, profit_cny: float | None) -> float | None:
    """Return profit as a percentage of the effective cost."""

    if consumption_cny is None or profit_cny is None or consumption_cny <= 0:
        return None
    value = profit_cny / consumption_cny * 100
    return value if math.isfinite(value) else None


def _record_account_stats(
    row: UpstreamAccountDailyUsage,
    stats: Any,
    current_recharge_multiplier: Any,
) -> None:
    cost = _stats_amount(stats, "cost")
    user_cost = _stats_amount(stats, "user_cost")
    if cost is None and user_cost is None:
        return
    multiplier = _finite_amount(row.local_recharge_multiplier)
    if multiplier is None:
        multiplier = _finite_amount(current_recharge_multiplier)
    # Daily stats are a complete snapshot. Clear legacy synthetic user-charge
    # values when the linked response does not explicitly expose user_cost.
    row.sub2api_cost = cost
    row.sub2api_user_cost = user_cost
    row.sub2api_actual_cost = user_cost
    row.income = None
    row.income_unit = None
    if multiplier is None:
        return
    row.local_recharge_multiplier = multiplier
    if user_cost is not None:
        row.income = _adjusted(user_cost, multiplier)
        row.income_unit = "CNY"


def _total_values(row: UpstreamChannelDailyUsage | None) -> dict[str, float | None]:
    return {
        "balance_used": row.balance_used if row is not None else None,
        "balance_used_adjusted": row.balance_used_adjusted if row is not None else None,
        "upstream_api_key_usage": row.upstream_api_key_usage if row is not None else None,
        "upstream_api_key_cost_cny": (
            row.upstream_api_key_cost_cny if row is not None else None
        ),
        "sub2api_cost": row.sub2api_cost if row is not None else None,
        "sub2api_cost_cny": row.sub2api_cost_cny if row is not None else None,
        "sub2api_user_cost": row.sub2api_user_cost if row is not None else None,
        "sub2api_actual_cost": row.sub2api_actual_cost if row is not None else None,
        "income": row.income if row is not None else None,
        "profit_cny": row.profit_cny if row is not None else None,
    }


async def _daily_row(
    db: AsyncSession,
    *,
    identity: str,
    day: date,
) -> UpstreamChannelDailyUsage | None:
    return await db.scalar(
        select(UpstreamChannelDailyUsage).where(
            UpstreamChannelDailyUsage.channel_identity == identity,
            UpstreamChannelDailyUsage.usage_date == day,
        )
    )


async def _account_daily_row(
    db: AsyncSession,
    *,
    account_id: int,
    day: date,
) -> UpstreamAccountDailyUsage | None:
    return await db.scalar(
        select(UpstreamAccountDailyUsage).where(
            UpstreamAccountDailyUsage.sub2api_account_id == account_id,
            UpstreamAccountDailyUsage.usage_date == day,
        )
    )


async def _refresh_daily_account_aggregates(
    db: AsyncSession,
    row: UpstreamChannelDailyUsage,
) -> None:
    sums = (
        await db.execute(
            select(
                func.sum(UpstreamAccountDailyUsage.upstream_usage),
                func.sum(
                    UpstreamAccountDailyUsage.upstream_usage
                    * UpstreamAccountDailyUsage.upstream_recharge_multiplier
                ),
                func.sum(UpstreamAccountDailyUsage.sub2api_cost),
                func.sum(
                    UpstreamAccountDailyUsage.sub2api_cost
                    * UpstreamAccountDailyUsage.local_recharge_multiplier
                ),
                func.sum(UpstreamAccountDailyUsage.sub2api_user_cost),
                func.sum(UpstreamAccountDailyUsage.income),
                func.min(UpstreamAccountDailyUsage.local_recharge_multiplier),
                func.max(UpstreamAccountDailyUsage.local_recharge_multiplier),
            ).where(
                UpstreamAccountDailyUsage.channel_identity == row.channel_identity,
                UpstreamAccountDailyUsage.usage_date == row.usage_date,
            )
        )
    ).one()
    upstream_usage = _finite_amount(sums[0])
    upstream_cost_cny = _finite_amount(sums[1])
    sub2api_cost = _finite_amount(sums[2])
    sub2api_cost_cny = _finite_amount(sums[3])
    sub2api_user_cost = _finite_amount(sums[4])
    income = _finite_amount(sums[5])
    minimum_multiplier = _finite_amount(sums[6])
    maximum_multiplier = _finite_amount(sums[7])
    row.upstream_api_key_usage = upstream_usage
    row.upstream_api_key_cost_cny = (
        upstream_cost_cny
        if upstream_cost_cny is not None
        else _adjusted(upstream_usage, row.recharge_multiplier)
    )
    row.sub2api_cost = sub2api_cost
    row.sub2api_cost_cny = sub2api_cost_cny
    row.sub2api_user_cost = sub2api_user_cost
    row.sub2api_actual_cost = sub2api_user_cost
    row.income = income
    row.income_recharge_multiplier = (
        minimum_multiplier
        if minimum_multiplier is not None and minimum_multiplier == maximum_multiplier
        else None
    )
    row.income_unit = "CNY" if income is not None else None
    effective_upstream_cost_cny = (
        row.upstream_api_key_cost_cny
        if row.upstream_api_key_cost_cny is not None
        else row.balance_used_adjusted
    )
    row.profit_cny = _profit(income, effective_upstream_cost_cny, sub2api_cost_cny)


async def _apply_lifetime_delta(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    before: dict[str, float | None],
    after: dict[str, float | None],
) -> None:
    changed = any(before[name] != after[name] for name in before)
    if not changed:
        return
    identity = channel_identity(channel)
    total = await db.get(UpstreamChannelUsageTotal, identity)
    if total is None:
        total = UpstreamChannelUsageTotal(
            channel_identity=identity,
            channel_id=channel.id,
            channel_name=_safe_text(channel.display_name, limit=200),
        )
        db.add(total)
    else:
        total.channel_id = channel.id
        total.channel_name = _safe_text(channel.display_name, limit=200)
    for field, total_field in (
        ("balance_used", "total_balance_used"),
        ("balance_used_adjusted", "total_balance_used_adjusted"),
        ("upstream_api_key_usage", "total_upstream_api_key_usage"),
        ("upstream_api_key_cost_cny", "total_upstream_api_key_cost_cny"),
        ("sub2api_cost", "total_sub2api_cost"),
        ("sub2api_cost_cny", "total_sub2api_cost_cny"),
        ("sub2api_user_cost", "total_sub2api_user_cost"),
        ("sub2api_actual_cost", "total_sub2api_actual_cost"),
        ("income", "total_income"),
        ("profit_cny", "total_profit_cny"),
    ):
        old_number = before[field] if before[field] is not None else 0.0
        new_number = after[field] if after[field] is not None else 0.0
        next_total = float(getattr(total, total_field) or 0.0) + new_number - old_number
        setattr(total, total_field, next_total if field == "profit_cny" else max(0.0, next_total))


async def import_sub2api_daily_stats(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    configs: Iterable[UpstreamAccountConfig],
    stats_by_account: dict[int, dict[date, Any]],
    local_recharge_multiplier: float | None,
    now: datetime,
    time_zone: str,
) -> int:
    """Persist linked Sub2API financial history without contacting an upstream.

    The linked API reports cumulative per-account daily values.  Reusing the
    normal account aggregation and lifetime-delta paths keeps repeated imports
    idempotent while leaving all upstream wallet/key fields untouched.
    """

    identity = channel_identity(channel)
    current_day = usage_day(now, time_zone)
    config_by_id = {int(config.sub2api_account_id): config for config in configs}
    imported = 0
    days: dict[date, list[tuple[UpstreamAccountConfig, Any]]] = {}
    for account_id, account_stats in stats_by_account.items():
        config = config_by_id.get(int(account_id))
        if config is None or not isinstance(account_stats, dict):
            continue
        for usage_date, stats in account_stats.items():
            if not isinstance(usage_date, date):
                continue
            if not isinstance(stats, dict):
                continue
            if _stats_amount(stats, "cost") is None and _stats_amount(stats, "user_cost") is None:
                continue
            days.setdefault(usage_date, []).append((config, stats))

    for usage_date in sorted(days):
        row = await _daily_row(db, identity=identity, day=usage_date)
        if row is None:
            row = UpstreamChannelDailyUsage(
                channel_id=channel.id,
                channel_identity=identity,
                channel_name=_safe_text(channel.display_name, limit=200),
                usage_date=usage_date,
            )
            db.add(row)
            await db.flush()
        before = _total_values(row)
        row.channel_id = channel.id
        row.channel_identity = identity
        row.channel_name = _safe_text(channel.display_name, limit=200)
        row.observed_at = now
        for config, stats in days[usage_date]:
            account_row = await _account_daily_row(
                db,
                account_id=int(config.sub2api_account_id),
                day=usage_date,
            )
            if account_row is None:
                account_row = UpstreamAccountDailyUsage(
                    channel_id=channel.id,
                    channel_identity=identity,
                    sub2api_account_id=int(config.sub2api_account_id),
                    usage_date=usage_date,
                )
                db.add(account_row)
            elif account_row.channel_identity != identity:
                continue
            account_row.channel_id = channel.id
            account_row.channel_identity = identity
            account_row.upstream_api_key_record_id = config.upstream_api_key_record_id
            account_row.account_name = _safe_text(config.remote_name, limit=200)
            account_row.remote_identity_fingerprint = _safe_text(
                config.remote_identity_fingerprint,
                limit=64,
            )
            account_row.observed_at = now
            _record_account_stats(account_row, stats, local_recharge_multiplier)
            if usage_date < current_day:
                account_row.finalized = True
            imported += 1
        if usage_date < current_day:
            row.finalized = True
            row.finalized_at = row.finalized_at or now
        await db.flush()
        await _refresh_daily_account_aggregates(db, row)
        await _apply_lifetime_delta(
            db,
            channel=channel,
            before=before,
            after=_total_values(row),
        )
    return imported


async def should_fetch_yesterday_usage(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    now: datetime,
    time_zone: str,
) -> bool:
    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    row = await _daily_row(db, identity=channel_identity(channel), day=yesterday)
    return row is None or _finite_amount(row.balance_used) is None


async def finalize_cached_yesterday_usage(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    now: datetime,
    time_zone: str,
) -> bool:
    """Finalize yesterday's last local snapshot without another upstream read."""

    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    row = await _daily_row(
        db,
        identity=channel_identity(channel),
        day=yesterday,
    )
    if row is None or _finite_amount(row.balance_used) is None:
        return False
    if not row.finalized:
        row.finalized = True
        row.finalized_at = now
        account_rows = list(
            (
                await db.execute(
                    select(UpstreamAccountDailyUsage).where(
                        UpstreamAccountDailyUsage.channel_identity
                        == row.channel_identity,
                        UpstreamAccountDailyUsage.usage_date == yesterday,
                    )
                )
            ).scalars()
        )
        for account_row in account_rows:
            account_row.finalized = True
        await db.flush()
    return await hydrate_yesterday_usage(
        db,
        channel=channel,
        now=now,
        time_zone=time_zone,
    )


async def finalize_elapsed_usage(
    db: AsyncSession,
    *,
    now: datetime,
    time_zone: str,
) -> int:
    """Freeze every persisted day before today without any upstream request."""

    today = usage_day(now, time_zone)
    channel_rows = list(
        (
            await db.execute(
                select(UpstreamChannelDailyUsage).where(
                    UpstreamChannelDailyUsage.usage_date < today,
                    UpstreamChannelDailyUsage.finalized.is_(False),
                )
            )
        ).scalars()
    )
    account_rows = list(
        (
            await db.execute(
                select(UpstreamAccountDailyUsage).where(
                    UpstreamAccountDailyUsage.usage_date < today,
                    UpstreamAccountDailyUsage.finalized.is_(False),
                )
            )
        ).scalars()
    )
    for row in channel_rows:
        row.finalized = True
        row.finalized_at = row.finalized_at or now
    for row in account_rows:
        row.finalized = True
    if channel_rows or account_rows:
        await db.flush()
    return len(channel_rows) + len(account_rows)


async def missing_finalized_usage_dates(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    start_date: date,
    end_date: date,
) -> list[date]:
    if end_date < start_date:
        return []
    rows = list(
        (
            await db.execute(
                select(UpstreamChannelDailyUsage).where(
                    UpstreamChannelDailyUsage.channel_identity == channel_identity(channel),
                    UpstreamChannelDailyUsage.usage_date >= start_date,
                    UpstreamChannelDailyUsage.usage_date <= end_date,
                )
            )
        ).scalars()
    )
    cached_rows = {
        row.usage_date: row
        for row in rows
        if _finite_amount(row.balance_used) is not None
    }
    newly_finalized_dates = {
        usage_date
        for usage_date, row in cached_rows.items()
        if not row.finalized
    }
    if newly_finalized_dates:
        finalized_at = datetime.now(timezone.utc)
        for usage_date in newly_finalized_dates:
            row = cached_rows[usage_date]
            row.finalized = True
            row.finalized_at = row.finalized_at or finalized_at
        account_rows = list(
            (
                await db.execute(
                    select(UpstreamAccountDailyUsage).where(
                        UpstreamAccountDailyUsage.channel_identity
                        == channel_identity(channel),
                        UpstreamAccountDailyUsage.usage_date.in_(newly_finalized_dates),
                    )
                )
            ).scalars()
        )
        for account_row in account_rows:
            account_row.finalized = True
        await db.flush()
    cached_dates = set(cached_rows)
    missing: list[date] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor not in cached_dates:
            missing.append(cursor)
        cursor += timedelta(days=1)
    return missing


async def hydrate_yesterday_usage(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    now: datetime,
    time_zone: str,
) -> bool:
    """Expose a finalized local day in the existing channel-card compatibility fields."""

    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    # Discovery updates the channel before hydrating this compatibility view.
    # Keep the cache lookup read-only so it cannot acquire SQLite's single
    # writer lock while the rest of the discovery writeback is still running.
    with db.no_autoflush:
        row = await _daily_row(db, identity=channel_identity(channel), day=yesterday)
    if row is None or not row.finalized or _finite_amount(row.balance_used) is None:
        return False
    channel.yesterday_balance_used = row.balance_used
    channel.yesterday_balance_unit = row.balance_unit or "USD"
    channel.yesterday_balance_status = "stored"
    channel.yesterday_balance_checked_at = row.finalized_at or row.observed_at or now
    return True


async def snapshot_today_usage(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    configs: Iterable[UpstreamAccountConfig],
    local_recharge_multiplier: float | None,
    now: datetime,
    time_zone: str,
    sub2api_stats_by_account: dict[int, Any] | None = None,
    income_actual_cost_by_account: dict[int, float] | None = None,
) -> UpstreamChannelDailyUsage | None:
    """Upsert the current local day without ever adding duplicate lifetime totals."""

    day = usage_day(now, time_zone)
    identity = channel_identity(channel)
    row = await _daily_row(db, identity=identity, day=day)
    if row is None:
        row = UpstreamChannelDailyUsage(
            channel_id=channel.id,
            channel_identity=identity,
            channel_name=_safe_text(channel.display_name, limit=200),
            usage_date=day,
        )
        db.add(row)
        await db.flush()
    before = _total_values(row)
    row.channel_id = channel.id
    row.channel_identity = identity
    row.channel_name = _safe_text(channel.display_name, limit=200)
    row.observed_at = now
    row.recharge_multiplier = _snapshot_multiplier(channel, row.recharge_multiplier)
    if (
        str(channel.today_balance_status or "").strip().lower()
        in VALID_DAILY_USAGE_STATUSES
    ):
        balance_used = _finite_amount(channel.today_balance_used)
        if balance_used is not None:
            multiplier = row.recharge_multiplier
            row.balance_used = balance_used
            row.balance_unit = _safe_text(channel.today_balance_unit, limit=32) or "USD"
            row.recharge_multiplier = multiplier
            row.balance_used_adjusted = _adjusted(balance_used, multiplier)

    for config in configs:
        account_id = int(config.sub2api_account_id)
        account_row = await _account_daily_row(
            db,
            account_id=account_id,
            day=day,
        )
        if account_row is None:
            account_row = UpstreamAccountDailyUsage(
                channel_id=channel.id,
                channel_identity=identity,
                sub2api_account_id=account_id,
                usage_date=day,
            )
            db.add(account_row)
        elif account_row.channel_identity != identity:
            # Sub2API's daily stats are cumulative. If an API-key account is
            # rebound during the day, recording it under both channels would
            # count the same revenue and key usage twice. Keep the first
            # observed ownership until the next local day.
            continue
        account_row.upstream_api_key_record_id = config.upstream_api_key_record_id
        account_row.account_name = _safe_text(config.remote_name, limit=200)
        account_row.remote_identity_fingerprint = _safe_text(
            config.remote_identity_fingerprint,
            limit=64,
        )
        account_row.observed_at = now
        upstream_status = str(config.today_upstream_usage_status or "").strip().lower()
        upstream_usage = _finite_amount(config.today_upstream_usage_amount)
        if upstream_usage is not None and upstream_status in VALID_DAILY_USAGE_STATUSES:
            account_row.upstream_usage = upstream_usage
            account_row.upstream_usage_unit = (
                _safe_text(config.today_upstream_usage_unit, limit=32) or "USD"
            )
            account_row.upstream_usage_source = _safe_text(
                config.today_upstream_usage_source,
                limit=64,
            )
            if account_row.upstream_recharge_multiplier is None:
                account_row.upstream_recharge_multiplier = _finite_amount(
                    config.effective_recharge_multiplier
                ) or row.recharge_multiplier
        stats = (sub2api_stats_by_account or {}).get(account_id)
        if stats is None and income_actual_cost_by_account is not None:
            legacy_cost = income_actual_cost_by_account.get(account_id)
            if legacy_cost is not None:
                stats = {"cost": legacy_cost, "user_cost": legacy_cost}
        _record_account_stats(
            account_row,
            stats,
            local_recharge_multiplier,
        )

    await db.flush()
    await _refresh_daily_account_aggregates(db, row)
    await _apply_lifetime_delta(
        db,
        channel=channel,
        before=before,
        after=_total_values(row),
    )
    return row


async def finalize_yesterday_usage(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    local_recharge_multiplier: float | None = None,
    now: datetime,
    time_zone: str,
    sub2api_stats_by_account: dict[int, Any] | None = None,
    income_actual_cost_by_account: dict[int, float] | None = None,
) -> bool:
    """Store the final upstream yesterday reading once, then reuse it indefinitely."""

    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    status = str(channel.yesterday_balance_status or "").strip().lower()
    balance_used = _finite_amount(channel.yesterday_balance_used)
    if status not in {"ok", "stored"} or balance_used is None:
        return await hydrate_yesterday_usage(
            db,
            channel=channel,
            now=now,
            time_zone=time_zone,
        )

    identity = channel_identity(channel)
    row = await _daily_row(db, identity=identity, day=yesterday)
    if row is None:
        row = UpstreamChannelDailyUsage(
            channel_id=channel.id,
            channel_identity=identity,
            channel_name=_safe_text(channel.display_name, limit=200),
            usage_date=yesterday,
        )
        db.add(row)
        await db.flush()
    before = _total_values(row)
    multiplier = _snapshot_multiplier(channel, row.recharge_multiplier)
    row.channel_id = channel.id
    row.channel_identity = identity
    row.channel_name = _safe_text(channel.display_name, limit=200)
    row.balance_used = balance_used
    row.balance_unit = _safe_text(channel.yesterday_balance_unit, limit=32) or "USD"
    row.recharge_multiplier = multiplier
    row.balance_used_adjusted = _adjusted(balance_used, multiplier)
    row.observed_at = channel.yesterday_balance_checked_at or now
    row.finalized = True
    row.finalized_at = now
    account_rows = list(
        (
            await db.execute(
                select(UpstreamAccountDailyUsage).where(
                    UpstreamAccountDailyUsage.channel_identity == identity,
                    UpstreamAccountDailyUsage.usage_date == yesterday,
                )
            )
        ).scalars()
    )
    for account_row in account_rows:
        stats = (sub2api_stats_by_account or {}).get(account_row.sub2api_account_id)
        if stats is None and income_actual_cost_by_account is not None:
            legacy_cost = income_actual_cost_by_account.get(account_row.sub2api_account_id)
            if legacy_cost is not None:
                stats = {"cost": legacy_cost, "user_cost": legacy_cost}
        _record_account_stats(
            account_row,
            stats,
            local_recharge_multiplier,
        )
        account_row.finalized = True
    await db.flush()
    await _refresh_daily_account_aggregates(db, row)
    await _apply_lifetime_delta(
        db,
        channel=channel,
        before=before,
        after=_total_values(row),
    )
    await hydrate_yesterday_usage(db, channel=channel, now=now, time_zone=time_zone)
    return True


async def upsert_historical_channel_usage(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    usage_date: date,
    balance_used: float,
    balance_unit: str = "USD",
    recharge_multiplier: float | None = None,
    observed_at: datetime | None = None,
) -> UpstreamChannelDailyUsage:
    """Import a verified historical wallet reading without duplicating totals.

    This is intentionally a service-level primitive for one-off recovery and
    backfill jobs. It applies the same before/after lifetime delta as live
    snapshots, so repeated imports of the same value remain idempotent.
    """

    usage = _finite_amount(balance_used)
    if usage is None:
        raise ValueError("balance_used must be a finite non-negative number.")
    identity = channel_identity(channel)
    row = await _daily_row(db, identity=identity, day=usage_date)
    if row is None:
        row = UpstreamChannelDailyUsage(
            channel_id=channel.id,
            channel_identity=identity,
            channel_name=_safe_text(channel.display_name, limit=200),
            usage_date=usage_date,
        )
        db.add(row)
        await db.flush()
    before = _total_values(row)
    multiplier = _finite_amount(recharge_multiplier)
    if multiplier is None:
        multiplier = _snapshot_multiplier(channel, row.recharge_multiplier)
    row.channel_id = channel.id
    row.channel_identity = identity
    row.channel_name = _safe_text(channel.display_name, limit=200)
    row.balance_used = usage
    row.balance_unit = _safe_text(balance_unit, limit=32) or "USD"
    row.recharge_multiplier = multiplier
    row.balance_used_adjusted = _adjusted(usage, multiplier)
    row.observed_at = observed_at or datetime.now(timezone.utc)
    row.finalized = True
    row.finalized_at = observed_at or datetime.now(timezone.utc)
    await db.flush()
    await _apply_lifetime_delta(
        db,
        channel=channel,
        before=before,
        after=_total_values(row),
    )
    return row


async def prune_upstream_usage_history(
    db: AsyncSession,
    *,
    retention_days: int,
    now: datetime | None = None,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> None:
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650.")
    reference = now or datetime.now(timezone.utc)
    cutoff = usage_day(reference, time_zone) - timedelta(days=retention_days - 1)
    await db.execute(
        delete(UpstreamAccountDailyUsage).where(UpstreamAccountDailyUsage.usage_date < cutoff)
    )
    await db.execute(
        delete(UpstreamChannelDailyUsage).where(UpstreamChannelDailyUsage.usage_date < cutoff)
    )


async def usage_history(
    db: AsyncSession,
    *,
    channel: UpstreamChannel,
    start_date: date,
    end_date: date,
    api_key_account_id: int | None = None,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must not be earlier than start_date.")
    if (end_date - start_date).days + 1 > MAX_HISTORY_DAYS:
        raise ValueError(f"A history request can cover at most {MAX_HISTORY_DAYS} days.")

    identity = channel_identity(channel)
    daily_rows = list(
        (
            await db.execute(
                select(UpstreamChannelDailyUsage)
                .where(
                    UpstreamChannelDailyUsage.channel_identity == identity,
                    UpstreamChannelDailyUsage.usage_date >= start_date,
                    UpstreamChannelDailyUsage.usage_date <= end_date,
                )
                .order_by(UpstreamChannelDailyUsage.usage_date)
            )
        ).scalars()
    )
    daily_by_date = {item.usage_date: item for item in daily_rows}
    account_statement = (
        select(UpstreamAccountDailyUsage)
        .where(
            UpstreamAccountDailyUsage.channel_identity == identity,
            UpstreamAccountDailyUsage.usage_date >= start_date,
            UpstreamAccountDailyUsage.usage_date <= end_date,
        )
        .order_by(
            UpstreamAccountDailyUsage.usage_date,
            UpstreamAccountDailyUsage.sub2api_account_id,
        )
    )
    if api_key_account_id is not None:
        account_statement = account_statement.where(
            UpstreamAccountDailyUsage.sub2api_account_id == api_key_account_id
        )
    account_rows = list((await db.execute(account_statement)).scalars())
    accounts_by_date: dict[date, list[UpstreamAccountDailyUsage]] = {}
    for row in account_rows:
        accounts_by_date.setdefault(row.usage_date, []).append(row)

    configured_accounts = list(
        (
            await db.execute(
                select(UpstreamAccountConfig)
                .where(UpstreamAccountConfig.channel_id == channel.id)
                .order_by(UpstreamAccountConfig.sub2api_account_id)
            )
        ).scalars()
    )
    accounts: dict[int, dict[str, Any]] = {
        int(config.sub2api_account_id): {
            "sub2api_account_id": int(config.sub2api_account_id),
            "account_name": _safe_text(config.remote_name, limit=200),
            "upstream_api_key_record_id": config.upstream_api_key_record_id,
        }
        for config in configured_accounts
    }
    for row in account_rows:
        accounts.setdefault(
            int(row.sub2api_account_id),
            {
                "sub2api_account_id": int(row.sub2api_account_id),
                "account_name": row.account_name,
                "upstream_api_key_record_id": row.upstream_api_key_record_id,
            },
        )

    days: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        channel_row = daily_by_date.get(cursor)
        account_day_rows = accounts_by_date.get(cursor, [])
        multiplier = channel_row.recharge_multiplier if channel_row is not None else None
        account_items: list[dict[str, Any]] = []
        for item in account_day_rows:
            upstream_multiplier = item.upstream_recharge_multiplier or multiplier
            upstream_cost_cny = _adjusted(item.upstream_usage, upstream_multiplier)
            sub2api_cost_cny = _adjusted(
                item.sub2api_cost,
                item.local_recharge_multiplier,
            )
            account_profit_cny = _profit(
                item.income,
                upstream_cost_cny,
                sub2api_cost_cny,
            )
            account_items.append(
                {
                    "sub2api_account_id": int(item.sub2api_account_id),
                    "account_name": item.account_name,
                    "upstream_api_key_record_id": item.upstream_api_key_record_id,
                    "upstream_usage": item.upstream_usage,
                    "upstream_usage_adjusted": upstream_cost_cny,
                    "upstream_usage_unit": item.upstream_usage_unit,
                    "upstream_usage_source": item.upstream_usage_source,
                    "upstream_recharge_multiplier": upstream_multiplier,
                    "upstream_cost_cny": upstream_cost_cny,
                    "sub2api_cost": item.sub2api_cost,
                    "sub2api_cost_cny": sub2api_cost_cny,
                    "sub2api_user_cost": item.sub2api_user_cost,
                    "sub2api_actual_cost": item.sub2api_user_cost,
                    "local_recharge_multiplier": item.local_recharge_multiplier,
                    "income": item.income,
                    "income_unit": item.income_unit,
                    "profit_cny": account_profit_cny,
                    "profit_margin": _profit_margin(
                        _maximum_cost(upstream_cost_cny, sub2api_cost_cny),
                        account_profit_cny,
                    ),
                }
            )
        account_usage = sum(
            item.upstream_usage for item in account_day_rows if item.upstream_usage is not None
        )
        has_account_usage = any(item.upstream_usage is not None for item in account_day_rows)
        account_upstream_cost_cny = sum(
            value
            for item in account_day_rows
            if (
                value := _adjusted(
                    item.upstream_usage,
                    item.upstream_recharge_multiplier or multiplier,
                )
            ) is not None
        )
        has_account_upstream_cost_cny = any(
            _adjusted(
                item.upstream_usage,
                item.upstream_recharge_multiplier or multiplier,
            )
            is not None
            for item in account_day_rows
        )
        sub2api_cost = sum(
            item.sub2api_cost for item in account_day_rows if item.sub2api_cost is not None
        )
        has_sub2api_cost = any(item.sub2api_cost is not None for item in account_day_rows)
        sub2api_cost_cny = sum(
            value
            for item in account_day_rows
            if (value := _adjusted(item.sub2api_cost, item.local_recharge_multiplier))
            is not None
        )
        has_sub2api_cost_cny = any(
            _adjusted(item.sub2api_cost, item.local_recharge_multiplier) is not None
            for item in account_day_rows
        )
        sub2api_user_cost = sum(
            item.sub2api_user_cost
            for item in account_day_rows
            if item.sub2api_user_cost is not None
        )
        has_sub2api_user_cost = any(
            item.sub2api_user_cost is not None for item in account_day_rows
        )
        income = sum(item.income for item in account_day_rows if item.income is not None)
        has_income = any(item.income is not None for item in account_day_rows)
        income_multipliers = {
            float(item.local_recharge_multiplier)
            for item in account_day_rows
            if item.local_recharge_multiplier is not None
        }
        channel_balance = channel_row.balance_used if channel_row is not None else None
        channel_adjusted = (
            channel_row.balance_used_adjusted if channel_row is not None else None
        )
        # Prefer summed per-key upstream usage so all three financial series
        # describe the same linked-account scope. Historical channel wallet
        # rows remain a fallback when per-key detail was unavailable.
        if api_key_account_id is not None:
            cost_raw = account_usage if has_account_usage else None
            upstream_cost_cny = account_upstream_cost_cny if has_account_upstream_cost_cny else None
        else:
            cost_raw = account_usage if has_account_usage else channel_balance
            upstream_cost_cny = (
                account_upstream_cost_cny
                if has_account_upstream_cost_cny
                else channel_row.upstream_api_key_cost_cny
                if channel_row is not None and channel_row.upstream_api_key_cost_cny is not None
                else channel_adjusted
            )
        visible_sub2api_cost = sub2api_cost if has_sub2api_cost else None
        visible_sub2api_cost_cny = sub2api_cost_cny if has_sub2api_cost_cny else None
        visible_user_cost = sub2api_user_cost if has_sub2api_user_cost else None
        consumption_cny = _maximum_cost(upstream_cost_cny, visible_sub2api_cost_cny)
        profit_cny = _profit(
            income if has_income else None,
            upstream_cost_cny,
            visible_sub2api_cost_cny,
        )
        days.append(
            {
                "date": cursor,
                "balance_used": channel_balance,
                "balance_used_adjusted": channel_adjusted,
                "balance_unit": channel_row.balance_unit if channel_row is not None else None,
                "recharge_multiplier": multiplier,
                "upstream_api_key_usage": account_usage if has_account_usage else None,
                "upstream_cost_cny": upstream_cost_cny,
                "sub2api_cost": visible_sub2api_cost,
                "sub2api_cost_cny": visible_sub2api_cost_cny,
                "sub2api_user_cost": visible_user_cost,
                "income_actual_cost": visible_user_cost,
                "income_recharge_multiplier": (
                    next(iter(income_multipliers)) if len(income_multipliers) == 1 else None
                ),
                "income": income if has_income else None,
                "income_unit": "CNY" if has_income else None,
                "cost": cost_raw,
                "cost_adjusted": upstream_cost_cny,
                "consumption_cny": consumption_cny,
                "profit_cny": profit_cny,
                "profit_margin": _profit_margin(
                    consumption_cny,
                    profit_cny,
                ),
                "finalized": bool(channel_row.finalized) if channel_row is not None else False,
                "api_key_accounts": account_items,
            }
        )
        cursor += timedelta(days=1)

    def sum_days(field: str) -> float | None:
        return _sum_known(
            float(item[field]) if item[field] is not None else None for item in days
        )

    total = await db.get(UpstreamChannelUsageTotal, identity)
    if api_key_account_id is not None:
        lifetime_account_rows = list(
            (
                await db.execute(
                    select(UpstreamAccountDailyUsage).where(
                        UpstreamAccountDailyUsage.channel_identity == identity,
                        UpstreamAccountDailyUsage.sub2api_account_id
                        == api_key_account_id,
                    )
                )
            ).scalars()
        )
        lifetime_upstream_usage = _sum_known(
            item.upstream_usage for item in lifetime_account_rows
        )
        lifetime_upstream_cost_cny = _sum_known(
            _adjusted(
                item.upstream_usage,
                item.upstream_recharge_multiplier,
            )
            for item in lifetime_account_rows
        )
        lifetime_sub2api_cost = _sum_known(
            item.sub2api_cost for item in lifetime_account_rows
        )
        lifetime_sub2api_cost_cny = _sum_known(
            _adjusted(item.sub2api_cost, item.local_recharge_multiplier)
            for item in lifetime_account_rows
        )
        lifetime_user_cost = _sum_known(
            item.sub2api_user_cost for item in lifetime_account_rows
        )
        lifetime_income = _sum_known(
            item.income for item in lifetime_account_rows
        )
        lifetime_consumption = _sum_known(
            _maximum_cost(
                _adjusted(
                    item.upstream_usage,
                    item.upstream_recharge_multiplier,
                ),
                _adjusted(item.sub2api_cost, item.local_recharge_multiplier),
            )
            for item in lifetime_account_rows
        )
        lifetime_profit = _sum_known(
            _profit(
                item.income,
                _adjusted(
                    item.upstream_usage,
                    item.upstream_recharge_multiplier,
                ),
                _adjusted(item.sub2api_cost, item.local_recharge_multiplier),
            )
            for item in lifetime_account_rows
        )
        lifetime_values = {
            # Keep the legacy lifetime wallet series channel-scoped even when
            # the requested daily rows are filtered to one API key.
            "balance_used": float(total.total_balance_used) if total is not None else 0.0,
            "balance_used_adjusted": (
                float(total.total_balance_used_adjusted) if total is not None else 0.0
            ),
            "upstream_api_key_usage": lifetime_upstream_usage,
            "upstream_cost_cny": lifetime_upstream_cost_cny,
            "sub2api_cost": lifetime_sub2api_cost,
            "sub2api_cost_cny": lifetime_sub2api_cost_cny,
            "sub2api_user_cost": lifetime_user_cost,
            "income_actual_cost": lifetime_user_cost,
            "income": lifetime_income,
            "cost": float(total.total_balance_used) if total is not None else lifetime_upstream_usage,
            "cost_adjusted": (
                float(total.total_balance_used_adjusted)
                if total is not None
                else lifetime_upstream_cost_cny
            ),
            "consumption_cny": lifetime_consumption,
            "profit_cny": lifetime_profit,
            "profit_margin": _profit_margin(lifetime_consumption, lifetime_profit),
        }
    else:
        lifetime_channel_rows = list(
            (
                await db.execute(
                    select(UpstreamChannelDailyUsage).where(
                        UpstreamChannelDailyUsage.channel_identity == identity
                    )
                )
            ).scalars()
        )
        lifetime_upstream_usage = _sum_known(
            item.upstream_api_key_usage for item in lifetime_channel_rows
        )
        lifetime_upstream_cost_cny = _sum_known(
            item.upstream_api_key_cost_cny
            if item.upstream_api_key_cost_cny is not None
            else item.balance_used_adjusted
            for item in lifetime_channel_rows
        )
        lifetime_sub2api_cost = _sum_known(
            item.sub2api_cost for item in lifetime_channel_rows
        )
        lifetime_sub2api_cost_cny = _sum_known(
            item.sub2api_cost_cny for item in lifetime_channel_rows
        )
        lifetime_user_cost = _sum_known(
            item.sub2api_user_cost for item in lifetime_channel_rows
        )
        lifetime_income = _sum_known(item.income for item in lifetime_channel_rows)
        lifetime_consumption = _sum_known(
            _maximum_cost(
                item.upstream_api_key_cost_cny
                if item.upstream_api_key_cost_cny is not None
                else item.balance_used_adjusted,
                item.sub2api_cost_cny,
            )
            for item in lifetime_channel_rows
        )
        lifetime_profit = _sum_known(
            item.profit_cny
            if item.profit_cny is not None
            else _profit(
                item.income,
                item.upstream_api_key_cost_cny
                if item.upstream_api_key_cost_cny is not None
                else item.balance_used_adjusted,
                item.sub2api_cost_cny,
            )
            for item in lifetime_channel_rows
        )
        lifetime_values = {
            "balance_used": float(total.total_balance_used) if total is not None else 0.0,
            "balance_used_adjusted": (
                float(total.total_balance_used_adjusted) if total is not None else 0.0
            ),
            "upstream_api_key_usage": lifetime_upstream_usage,
            "upstream_cost_cny": lifetime_upstream_cost_cny,
            "sub2api_cost": lifetime_sub2api_cost,
            "sub2api_cost_cny": lifetime_sub2api_cost_cny,
            "sub2api_user_cost": lifetime_user_cost,
            "income_actual_cost": lifetime_user_cost,
            "income": lifetime_income,
            "cost": lifetime_upstream_usage,
            "cost_adjusted": lifetime_upstream_cost_cny,
            "consumption_cny": lifetime_consumption,
            "profit_cny": lifetime_profit,
            "profit_margin": _profit_margin(lifetime_consumption, lifetime_profit),
        }
    return {
        "channel_id": channel.id,
        "channel_name": channel.display_name,
        "time_zone": time_zone,
        "start_date": start_date,
        "end_date": end_date,
        "api_key_account_id": api_key_account_id,
        "api_key_accounts": list(accounts.values()),
        "days": days,
        "totals": {
            "balance_used": sum_days("balance_used"),
            "balance_used_adjusted": sum_days("balance_used_adjusted"),
            "upstream_api_key_usage": sum_days("upstream_api_key_usage"),
            "upstream_cost_cny": sum_days("upstream_cost_cny"),
            "sub2api_cost": sum_days("sub2api_cost"),
            "sub2api_cost_cny": sum_days("sub2api_cost_cny"),
            "sub2api_user_cost": sum_days("sub2api_user_cost"),
            "income_actual_cost": sum_days("income_actual_cost"),
            "income": sum_days("income"),
            "cost": sum_days("cost"),
            "cost_adjusted": sum_days("cost_adjusted"),
            "consumption_cny": sum_days("consumption_cny"),
            "profit_cny": sum_days("profit_cny"),
            "profit_margin": _profit_margin(
                sum_days("consumption_cny"),
                sum_days("profit_cny"),
            ),
        },
        "lifetime_totals": lifetime_values,
    }


__all__ = [
    "DEFAULT_TIME_ZONE",
    "MAX_HISTORY_DAYS",
    "channel_identity",
    "finalize_cached_yesterday_usage",
    "finalize_elapsed_usage",
    "finalize_yesterday_usage",
    "hydrate_yesterday_usage",
    "missing_finalized_usage_dates",
    "normalize_history_time_zone",
    "prune_upstream_usage_history",
    "should_fetch_yesterday_usage",
    "snapshot_today_usage",
    "upsert_historical_channel_usage",
    "usage_day",
    "usage_history",
]
