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


def _record_account_income(
    row: UpstreamAccountDailyUsage,
    actual_cost: Any,
    current_recharge_multiplier: Any,
) -> None:
    charge = _finite_amount(actual_cost)
    if charge is None:
        return
    multiplier = _finite_amount(row.local_recharge_multiplier)
    if multiplier is None:
        multiplier = _finite_amount(current_recharge_multiplier)
    row.sub2api_actual_cost = charge
    if multiplier is None:
        return
    row.local_recharge_multiplier = multiplier
    row.income = _adjusted(charge, multiplier)
    row.income_unit = "CNY"


def _total_values(row: UpstreamChannelDailyUsage | None) -> dict[str, float | None]:
    return {
        "balance_used": row.balance_used if row is not None else None,
        "balance_used_adjusted": row.balance_used_adjusted if row is not None else None,
        "upstream_api_key_usage": row.upstream_api_key_usage if row is not None else None,
        "sub2api_actual_cost": row.sub2api_actual_cost if row is not None else None,
        "income": row.income if row is not None else None,
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
                func.sum(UpstreamAccountDailyUsage.sub2api_actual_cost),
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
    actual_cost = _finite_amount(sums[1])
    income = _finite_amount(sums[2])
    minimum_multiplier = _finite_amount(sums[3])
    maximum_multiplier = _finite_amount(sums[4])
    row.upstream_api_key_usage = upstream_usage
    row.sub2api_actual_cost = actual_cost
    row.income = income
    row.income_recharge_multiplier = (
        minimum_multiplier
        if minimum_multiplier is not None and minimum_multiplier == maximum_multiplier
        else None
    )
    row.income_unit = "CNY" if income is not None else None


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
        ("sub2api_actual_cost", "total_sub2api_actual_cost"),
        ("income", "total_income"),
    ):
        old_number = before[field] if before[field] is not None else 0.0
        new_number = after[field] if after[field] is not None else 0.0
        next_total = float(getattr(total, total_field) or 0.0) + new_number - old_number
        setattr(total, total_field, max(0.0, next_total))


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
    income_actual_cost_by_account: dict[int, float],
    local_recharge_multiplier: float | None,
    now: datetime,
    time_zone: str,
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
    if (
        str(channel.today_balance_status or "").strip().lower()
        in VALID_DAILY_USAGE_STATUSES
    ):
        balance_used = _finite_amount(channel.today_balance_used)
        if balance_used is not None:
            multiplier = _snapshot_multiplier(channel, row.recharge_multiplier)
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
        _record_account_income(
            account_row,
            income_actual_cost_by_account.get(account_id),
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
    income_actual_cost_by_account: dict[int, float] | None = None,
    local_recharge_multiplier: float | None = None,
    now: datetime,
    time_zone: str,
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
        _record_account_income(
            account_row,
            (income_actual_cost_by_account or {}).get(account_row.sub2api_account_id),
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
        account_items = [
            {
                "sub2api_account_id": int(item.sub2api_account_id),
                "account_name": item.account_name,
                "upstream_api_key_record_id": item.upstream_api_key_record_id,
                "upstream_usage": item.upstream_usage,
                "upstream_usage_adjusted": _adjusted(item.upstream_usage, multiplier),
                "upstream_usage_unit": item.upstream_usage_unit,
                "upstream_usage_source": item.upstream_usage_source,
                "sub2api_actual_cost": item.sub2api_actual_cost,
                "local_recharge_multiplier": item.local_recharge_multiplier,
                "income": item.income,
                "income_unit": item.income_unit,
            }
            for item in account_day_rows
        ]
        account_usage = sum(
            item.upstream_usage for item in account_day_rows if item.upstream_usage is not None
        )
        has_account_usage = any(item.upstream_usage is not None for item in account_day_rows)
        income = sum(item.income for item in account_day_rows if item.income is not None)
        has_income = any(item.income is not None for item in account_day_rows)
        income_actual_cost = sum(
            item.sub2api_actual_cost
            for item in account_day_rows
            if item.sub2api_actual_cost is not None
        )
        has_income_actual_cost = any(
            item.sub2api_actual_cost is not None for item in account_day_rows
        )
        income_multipliers = {
            float(item.local_recharge_multiplier)
            for item in account_day_rows
            if item.local_recharge_multiplier is not None
        }
        channel_balance = channel_row.balance_used if channel_row is not None else None
        channel_adjusted = (
            channel_row.balance_used_adjusted if channel_row is not None else None
        )
        # A key filter switches the cost series to that key's observed upstream
        # usage.  Without a filter, the authoritative channel-wallet reading is
        # the cost series requested by the balance card.
        # Once a specific API key is selected, never substitute the whole
        # channel-wallet expense for a missing per-key reading.  Doing so
        # makes the filtered income and cost series describe different scopes.
        filtered_account_cost = account_usage if has_account_usage else None
        cost_raw = (
            filtered_account_cost
            if api_key_account_id is not None
            else channel_balance
        )
        cost_adjusted = (
            _adjusted(cost_raw, multiplier) if api_key_account_id is not None else channel_adjusted
        )
        days.append(
            {
                "date": cursor,
                "balance_used": channel_balance,
                "balance_used_adjusted": channel_adjusted,
                "balance_unit": channel_row.balance_unit if channel_row is not None else None,
                "recharge_multiplier": multiplier,
                "upstream_api_key_usage": account_usage if has_account_usage else None,
                "income_actual_cost": income_actual_cost if has_income_actual_cost else None,
                "income_recharge_multiplier": (
                    next(iter(income_multipliers)) if len(income_multipliers) == 1 else None
                ),
                "income": income if has_income else None,
                "income_unit": "CNY" if has_income else None,
                "cost": cost_raw,
                "cost_adjusted": cost_adjusted,
                "finalized": bool(channel_row.finalized) if channel_row is not None else False,
                "api_key_accounts": account_items,
            }
        )
        cursor += timedelta(days=1)

    def sum_days(field: str) -> float:
        return sum(float(item[field]) for item in days if item[field] is not None)

    total = await db.get(UpstreamChannelUsageTotal, identity)
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
            "income_actual_cost": sum_days("income_actual_cost"),
            "income": sum_days("income"),
            "cost": sum_days("cost"),
            "cost_adjusted": sum_days("cost_adjusted"),
        },
        "lifetime_totals": {
            "balance_used": float(total.total_balance_used) if total is not None else 0.0,
            "balance_used_adjusted": (
                float(total.total_balance_used_adjusted) if total is not None else 0.0
            ),
            "upstream_api_key_usage": (
                float(total.total_upstream_api_key_usage) if total is not None else 0.0
            ),
            "income_actual_cost": (
                float(total.total_sub2api_actual_cost) if total is not None else 0.0
            ),
            "income": float(total.total_income) if total is not None else 0.0,
            "cost": float(total.total_balance_used) if total is not None else 0.0,
            "cost_adjusted": (
                float(total.total_balance_used_adjusted) if total is not None else 0.0
            ),
        },
    }


__all__ = [
    "DEFAULT_TIME_ZONE",
    "MAX_HISTORY_DAYS",
    "channel_identity",
    "finalize_cached_yesterday_usage",
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
