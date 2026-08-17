from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ApiAccount,
    ApiAccountDailyUsage,
    Upstream,
    UpstreamDailyUsage,
    UpstreamUsageTotal,
)


DEFAULT_TIME_ZONE = "Asia/Shanghai"
VALID_DAILY_USAGE_STATUSES = frozenset({"ok", "stale", "estimated", "stored"})
MAX_HISTORY_DAYS = 3660


def usage_day(now: datetime, time_zone: str) -> date:
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


def upstream_id(upstream: Upstream) -> str:
    return str(upstream.id)


def _snapshot_multiplier(upstream: Upstream, existing: float | None = None) -> float | None:
    if existing is not None:
        return _finite_amount(existing)
    current = (
        upstream.upstream_recharge_multiplier
        if upstream.upstream_recharge_multiplier is not None
        else upstream.last_known_recharge_multiplier
    )
    return _finite_amount(current)


def _adjusted(amount: float | None, multiplier: float | None) -> float | None:
    if amount is None or multiplier is None:
        return None
    value = amount * multiplier
    return value if math.isfinite(value) else None


def _stats_amount(stats: Any, field: str) -> float | None:
    if isinstance(stats, dict):
        return _finite_amount(stats.get(field))
    return _finite_amount(getattr(stats, field, None))


def _sum_known(values: Iterable[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _single_multiplier(values: Iterable[float | None]) -> float | None:
    known = {float(value) for value in values if value is not None}
    return next(iter(known)) if len(known) == 1 else None


def _maximum_cost(*values: float | None) -> float | None:
    known = [value for value in values if value is not None]
    return max(known) if known else None


def _profit(
    actual_income_cny: float | None,
    upstream_actual_cost_cny: float | None,
    management_account_cost_cny: float | None,
) -> float | None:
    cost = _maximum_cost(upstream_actual_cost_cny, management_account_cost_cny)
    if actual_income_cny is None or cost is None:
        return None
    value = actual_income_cny - cost
    return value if math.isfinite(value) else None


def _profit_margin(consumption_cny: float | None, profit_cny: float | None) -> float | None:
    if consumption_cny is None or profit_cny is None or consumption_cny <= 0:
        return None
    value = profit_cny / consumption_cny * 100
    return value if math.isfinite(value) else None


def _record_account_stats(
    row: ApiAccountDailyUsage,
    stats: Any,
    current_management_recharge_multiplier: Any,
) -> None:
    cost = _stats_amount(stats, "cost")
    user_charge = _stats_amount(stats, "user_cost")
    if cost is None and user_charge is None:
        return
    multiplier = _finite_amount(row.management_recharge_multiplier)
    if multiplier is None:
        multiplier = _finite_amount(current_management_recharge_multiplier)
    row.management_account_cost_usd = cost
    row.management_user_charge_usd = user_charge
    row.management_recharge_multiplier = multiplier
    row.management_account_cost_cny = _adjusted(cost, multiplier)
    row.actual_income_cny = _adjusted(user_charge, multiplier)
    row.income_unit = "CNY" if row.actual_income_cny is not None else None


def _total_values(row: UpstreamDailyUsage | None) -> dict[str, float | None]:
    fields = (
        "upstream_wallet_cost_usd",
        "upstream_actual_cost_cny",
        "management_account_cost_usd",
        "management_account_cost_cny",
        "management_user_charge_usd",
        "actual_income_cny",
        "profit_cny",
    )
    return {field: getattr(row, field) if row is not None else None for field in fields}


async def _daily_row(
    db: AsyncSession,
    *,
    identity: str,
    day: date,
    source_segment: int = 0,
) -> UpstreamDailyUsage | None:
    return await db.scalar(
        select(UpstreamDailyUsage).where(
            UpstreamDailyUsage.upstream_id == identity,
            UpstreamDailyUsage.usage_date == day,
            UpstreamDailyUsage.source_segment == source_segment,
        )
    )


async def _account_daily_row(
    db: AsyncSession,
    *,
    identity: str,
    management_account_id: int,
    day: date,
    source_segment: int = 0,
) -> ApiAccountDailyUsage | None:
    return await db.scalar(
        select(ApiAccountDailyUsage).where(
            ApiAccountDailyUsage.upstream_id == identity,
            ApiAccountDailyUsage.management_account_id == management_account_id,
            ApiAccountDailyUsage.usage_date == day,
            ApiAccountDailyUsage.source_segment == source_segment,
        )
    )


async def _refresh_daily_account_aggregates(
    db: AsyncSession,
    row: UpstreamDailyUsage,
) -> None:
    sums = (
        await db.execute(
            select(
                func.sum(ApiAccountDailyUsage.upstream_wallet_cost_usd),
                func.sum(ApiAccountDailyUsage.upstream_actual_cost_cny),
                func.sum(ApiAccountDailyUsage.management_account_cost_usd),
                func.sum(ApiAccountDailyUsage.management_account_cost_cny),
                func.sum(ApiAccountDailyUsage.management_user_charge_usd),
                func.sum(ApiAccountDailyUsage.actual_income_cny),
                func.min(ApiAccountDailyUsage.management_recharge_multiplier),
                func.max(ApiAccountDailyUsage.management_recharge_multiplier),
            ).where(
                ApiAccountDailyUsage.upstream_id == row.upstream_id,
                ApiAccountDailyUsage.usage_date == row.usage_date,
                ApiAccountDailyUsage.source_segment == row.source_segment,
            )
        )
    ).one()
    account_wallet_cost = _finite_amount(sums[0])
    account_upstream_cost = _finite_amount(sums[1])
    if account_wallet_cost is not None:
        row.upstream_wallet_cost_usd = account_wallet_cost
    if account_upstream_cost is not None:
        row.upstream_actual_cost_cny = account_upstream_cost
    row.management_account_cost_usd = _finite_amount(sums[2])
    row.management_account_cost_cny = _finite_amount(sums[3])
    row.management_user_charge_usd = _finite_amount(sums[4])
    row.actual_income_cny = _finite_amount(sums[5])
    minimum = _finite_amount(sums[6])
    maximum = _finite_amount(sums[7])
    row.management_recharge_multiplier = (
        minimum if minimum is not None and minimum == maximum else None
    )
    row.income_unit = "CNY" if row.actual_income_cny is not None else None
    row.profit_cny = _profit(
        row.actual_income_cny,
        row.upstream_actual_cost_cny,
        row.management_account_cost_cny,
    )


async def _apply_lifetime_delta(
    db: AsyncSession,
    *,
    upstream: Upstream,
    before: dict[str, float | None],
    after: dict[str, float | None],
) -> None:
    if before == after:
        return
    identity = upstream_id(upstream)
    total = await db.get(UpstreamUsageTotal, identity)
    if total is None:
        total = UpstreamUsageTotal(
            upstream_id=identity,
            upstream_name=_safe_text(upstream.display_name, limit=200),
        )
        db.add(total)
    else:
        total.upstream_name = _safe_text(upstream.display_name, limit=200)
    mappings = (
        ("upstream_wallet_cost_usd", "total_upstream_wallet_cost_usd"),
        ("upstream_actual_cost_cny", "total_upstream_actual_cost_cny"),
        ("management_account_cost_usd", "total_management_account_cost_usd"),
        ("management_account_cost_cny", "total_management_account_cost_cny"),
        ("management_user_charge_usd", "total_management_user_charge_usd"),
        ("actual_income_cny", "total_actual_income_cny"),
        ("profit_cny", "total_profit_cny"),
    )
    for field, total_field in mappings:
        old_value = before[field] if before[field] is not None else 0.0
        new_value = after[field] if after[field] is not None else 0.0
        next_value = float(getattr(total, total_field) or 0.0) + new_value - old_value
        setattr(total, total_field, next_value if field == "profit_cny" else max(0.0, next_value))


async def import_sub2api_daily_stats(
    db: AsyncSession,
    *,
    upstream: Upstream,
    configs: Iterable[ApiAccount],
    stats_by_account: dict[int, dict[date, Any]],
    management_recharge_multiplier: float | None,
    now: datetime,
    time_zone: str,
) -> int:
    identity = upstream_id(upstream)
    current_day = usage_day(now, time_zone)
    config_by_id = {int(config.management_account_id): config for config in configs}
    days: dict[date, list[tuple[ApiAccount, Any]]] = defaultdict(list)
    for account_id, account_stats in stats_by_account.items():
        config = config_by_id.get(int(account_id))
        if config is None or not isinstance(account_stats, dict):
            continue
        for usage_date, stats in account_stats.items():
            if (
                isinstance(usage_date, date)
                and isinstance(stats, dict)
                and (
                    _stats_amount(stats, "cost") is not None
                    or _stats_amount(stats, "user_cost") is not None
                )
            ):
                days[usage_date].append((config, stats))

    imported = 0
    for usage_date in sorted(days):
        row = await _daily_row(db, identity=identity, day=usage_date)
        if row is None:
            row = UpstreamDailyUsage(
                upstream_id=identity,
                upstream_name=_safe_text(upstream.display_name, limit=200),
                usage_date=usage_date,
            )
            db.add(row)
            await db.flush()
        before = _total_values(row)
        row.upstream_name = _safe_text(upstream.display_name, limit=200)
        row.observed_at = now
        for config, stats in days[usage_date]:
            account_row = await _account_daily_row(
                db,
                identity=identity,
                management_account_id=int(config.management_account_id),
                day=usage_date,
            )
            if account_row is None:
                account_row = ApiAccountDailyUsage(
                    upstream_id=identity,
                    management_account_id=int(config.management_account_id),
                    usage_date=usage_date,
                )
                db.add(account_row)
            account_row.api_account_id = config.id
            account_row.upstream_api_key_id = config.upstream_api_key_id
            account_row.remote_upstream_api_key_id = config.remote_upstream_api_key_id
            account_row.account_name = _safe_text(config.remote_name, limit=200)
            account_row.remote_identity_fingerprint = _safe_text(
                config.remote_identity_fingerprint,
                limit=64,
            )
            account_row.observed_at = now
            _record_account_stats(account_row, stats, management_recharge_multiplier)
            account_row.finalized = usage_date < current_day
            imported += 1
        row.finalized = usage_date < current_day
        if row.finalized:
            row.finalized_at = row.finalized_at or now
        await db.flush()
        await _refresh_daily_account_aggregates(db, row)
        await _apply_lifetime_delta(
            db,
            upstream=upstream,
            before=before,
            after=_total_values(row),
        )
    return imported


async def should_fetch_yesterday_usage(
    db: AsyncSession,
    *,
    upstream: Upstream,
    now: datetime,
    time_zone: str,
) -> bool:
    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    rows = list(
        (
            await db.scalars(
                select(UpstreamDailyUsage).where(
                    UpstreamDailyUsage.upstream_id == upstream_id(upstream),
                    UpstreamDailyUsage.usage_date == yesterday,
                )
            )
        ).all()
    )
    return not any(_finite_amount(row.upstream_wallet_cost_usd) is not None for row in rows)


async def finalize_cached_yesterday_usage(
    db: AsyncSession,
    *,
    upstream: Upstream,
    now: datetime,
    time_zone: str,
) -> bool:
    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    rows = list(
        (
            await db.scalars(
                select(UpstreamDailyUsage).where(
                    UpstreamDailyUsage.upstream_id == upstream_id(upstream),
                    UpstreamDailyUsage.usage_date == yesterday,
                )
            )
        ).all()
    )
    if not any(_finite_amount(row.upstream_wallet_cost_usd) is not None for row in rows):
        return False
    for row in rows:
        row.finalized = True
        row.finalized_at = row.finalized_at or now
    account_rows = list(
        (
            await db.scalars(
                select(ApiAccountDailyUsage).where(
                    ApiAccountDailyUsage.upstream_id == upstream_id(upstream),
                    ApiAccountDailyUsage.usage_date == yesterday,
                )
            )
        ).all()
    )
    for row in account_rows:
        row.finalized = True
    await db.flush()
    return await hydrate_yesterday_usage(
        db,
        upstream=upstream,
        now=now,
        time_zone=time_zone,
    )


async def finalize_elapsed_usage(
    db: AsyncSession,
    *,
    now: datetime,
    time_zone: str,
) -> int:
    today = usage_day(now, time_zone)
    upstream_rows = list(
        (
            await db.scalars(
                select(UpstreamDailyUsage).where(
                    UpstreamDailyUsage.usage_date < today,
                    UpstreamDailyUsage.finalized.is_(False),
                )
            )
        ).all()
    )
    account_rows = list(
        (
            await db.scalars(
                select(ApiAccountDailyUsage).where(
                    ApiAccountDailyUsage.usage_date < today,
                    ApiAccountDailyUsage.finalized.is_(False),
                )
            )
        ).all()
    )
    for row in upstream_rows:
        row.finalized = True
        row.finalized_at = row.finalized_at or now
    for row in account_rows:
        row.finalized = True
    if upstream_rows or account_rows:
        await db.flush()
    return len(upstream_rows) + len(account_rows)


async def missing_finalized_usage_dates(
    db: AsyncSession,
    *,
    upstream: Upstream,
    start_date: date,
    end_date: date,
) -> list[date]:
    if end_date < start_date:
        return []
    identity = upstream_id(upstream)
    rows = list(
        (
            await db.scalars(
                select(UpstreamDailyUsage).where(
                    UpstreamDailyUsage.upstream_id == identity,
                    UpstreamDailyUsage.usage_date >= start_date,
                    UpstreamDailyUsage.usage_date <= end_date,
                )
            )
        ).all()
    )
    rows_by_date: dict[date, list[UpstreamDailyUsage]] = defaultdict(list)
    for row in rows:
        rows_by_date[row.usage_date].append(row)
    available_dates = {
        usage_date
        for usage_date, date_rows in rows_by_date.items()
        if any(_finite_amount(row.upstream_wallet_cost_usd) is not None for row in date_rows)
    }
    newly_finalized = {
        usage_date
        for usage_date in available_dates
        if any(not row.finalized for row in rows_by_date[usage_date])
    }
    if newly_finalized:
        finalized_at = datetime.now(timezone.utc)
        for usage_date in newly_finalized:
            for row in rows_by_date[usage_date]:
                row.finalized = True
                row.finalized_at = row.finalized_at or finalized_at
        account_rows = list(
            (
                await db.scalars(
                    select(ApiAccountDailyUsage).where(
                        ApiAccountDailyUsage.upstream_id == identity,
                        ApiAccountDailyUsage.usage_date.in_(newly_finalized),
                    )
                )
            ).all()
        )
        for row in account_rows:
            row.finalized = True
        await db.flush()
    result: list[date] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor not in available_dates:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


async def hydrate_yesterday_usage(
    db: AsyncSession,
    *,
    upstream: Upstream,
    now: datetime,
    time_zone: str,
) -> bool:
    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    with db.no_autoflush:
        rows = list(
            (
                await db.scalars(
                    select(UpstreamDailyUsage).where(
                        UpstreamDailyUsage.upstream_id == upstream_id(upstream),
                        UpstreamDailyUsage.usage_date == yesterday,
                        UpstreamDailyUsage.finalized.is_(True),
                    )
                )
            ).all()
        )
    amount = _sum_known(_finite_amount(row.upstream_wallet_cost_usd) for row in rows)
    if amount is None:
        return False
    units = {_safe_text(row.balance_unit, limit=32) or "USD" for row in rows}
    timestamps = [row.finalized_at or row.observed_at for row in rows]
    upstream.yesterday_upstream_wallet_cost_usd = amount
    upstream.yesterday_balance_unit = next(iter(units)) if len(units) == 1 else "USD"
    upstream.yesterday_balance_status = "stored"
    upstream.yesterday_balance_checked_at = max(
        (value for value in timestamps if value is not None),
        default=now,
    )
    return True


async def snapshot_today_usage(
    db: AsyncSession,
    *,
    upstream: Upstream,
    configs: Iterable[ApiAccount],
    management_recharge_multiplier: float | None,
    now: datetime,
    time_zone: str,
    management_stats_by_account: dict[int, Any] | None = None,
) -> UpstreamDailyUsage:
    day = usage_day(now, time_zone)
    identity = upstream_id(upstream)
    row = await _daily_row(db, identity=identity, day=day)
    if row is None:
        row = UpstreamDailyUsage(
            upstream_id=identity,
            upstream_name=_safe_text(upstream.display_name, limit=200),
            usage_date=day,
        )
        db.add(row)
        await db.flush()
    before = _total_values(row)
    row.upstream_name = _safe_text(upstream.display_name, limit=200)
    row.observed_at = now
    row.upstream_recharge_multiplier = _snapshot_multiplier(
        upstream,
        row.upstream_recharge_multiplier,
    )
    if str(upstream.today_balance_status or "").strip().lower() in VALID_DAILY_USAGE_STATUSES:
        wallet_cost = _finite_amount(upstream.today_upstream_wallet_cost_usd)
        if wallet_cost is not None:
            row.upstream_wallet_cost_usd = wallet_cost
            row.balance_unit = _safe_text(upstream.today_balance_unit, limit=32) or "USD"
            row.upstream_actual_cost_cny = _adjusted(
                wallet_cost,
                row.upstream_recharge_multiplier,
            )

    for config in configs:
        management_account_id = int(config.management_account_id)
        account_row = await _account_daily_row(
            db,
            identity=identity,
            management_account_id=management_account_id,
            day=day,
        )
        if account_row is None:
            account_row = ApiAccountDailyUsage(
                upstream_id=identity,
                management_account_id=management_account_id,
                usage_date=day,
            )
            db.add(account_row)
        account_row.api_account_id = config.id
        account_row.upstream_api_key_id = config.upstream_api_key_id
        account_row.remote_upstream_api_key_id = config.remote_upstream_api_key_id
        account_row.account_name = _safe_text(config.remote_name, limit=200)
        account_row.remote_identity_fingerprint = _safe_text(
            config.remote_identity_fingerprint,
            limit=64,
        )
        account_row.observed_at = now
        status = str(config.today_upstream_usage_status or "").strip().lower()
        wallet_cost = _finite_amount(config.today_upstream_wallet_cost_usd)
        if wallet_cost is not None and status in VALID_DAILY_USAGE_STATUSES:
            account_row.upstream_wallet_cost_usd = wallet_cost
            account_row.upstream_usage_unit = (
                _safe_text(config.today_upstream_usage_unit, limit=32) or "USD"
            )
            account_row.upstream_usage_source = _safe_text(
                config.today_upstream_usage_source,
                limit=64,
            )
            if account_row.upstream_recharge_multiplier is None:
                account_row.upstream_recharge_multiplier = (
                    _finite_amount(config.upstream_recharge_multiplier)
                    if config.upstream_recharge_multiplier is not None
                    else row.upstream_recharge_multiplier
                )
            account_row.upstream_actual_cost_cny = _adjusted(
                wallet_cost,
                account_row.upstream_recharge_multiplier,
            )
        _record_account_stats(
            account_row,
            (management_stats_by_account or {}).get(management_account_id),
            management_recharge_multiplier,
        )

    await db.flush()
    await _refresh_daily_account_aggregates(db, row)
    await _apply_lifetime_delta(
        db,
        upstream=upstream,
        before=before,
        after=_total_values(row),
    )
    return row


async def finalize_yesterday_usage(
    db: AsyncSession,
    *,
    upstream: Upstream,
    management_recharge_multiplier: float | None = None,
    now: datetime,
    time_zone: str,
    management_stats_by_account: dict[int, Any] | None = None,
) -> bool:
    yesterday = usage_day(now, time_zone) - timedelta(days=1)
    status = str(upstream.yesterday_balance_status or "").strip().lower()
    wallet_cost = _finite_amount(upstream.yesterday_upstream_wallet_cost_usd)
    if status not in {"ok", "stored"} or wallet_cost is None:
        return await hydrate_yesterday_usage(
            db,
            upstream=upstream,
            now=now,
            time_zone=time_zone,
        )
    identity = upstream_id(upstream)
    row = await _daily_row(db, identity=identity, day=yesterday)
    if row is None:
        row = UpstreamDailyUsage(
            upstream_id=identity,
            upstream_name=_safe_text(upstream.display_name, limit=200),
            usage_date=yesterday,
        )
        db.add(row)
        await db.flush()
    before = _total_values(row)
    row.upstream_name = _safe_text(upstream.display_name, limit=200)
    row.upstream_wallet_cost_usd = wallet_cost
    row.balance_unit = _safe_text(upstream.yesterday_balance_unit, limit=32) or "USD"
    row.upstream_recharge_multiplier = _snapshot_multiplier(
        upstream,
        row.upstream_recharge_multiplier,
    )
    row.upstream_actual_cost_cny = _adjusted(wallet_cost, row.upstream_recharge_multiplier)
    row.observed_at = upstream.yesterday_balance_checked_at or now
    row.finalized = True
    row.finalized_at = now
    account_rows = list(
        (
            await db.scalars(
                select(ApiAccountDailyUsage).where(
                    ApiAccountDailyUsage.upstream_id == identity,
                    ApiAccountDailyUsage.usage_date == yesterday,
                )
            )
        ).all()
    )
    for account_row in account_rows:
        _record_account_stats(
            account_row,
            (management_stats_by_account or {}).get(account_row.management_account_id),
            management_recharge_multiplier,
        )
        account_row.finalized = True
    await db.flush()
    await _refresh_daily_account_aggregates(db, row)
    await _apply_lifetime_delta(
        db,
        upstream=upstream,
        before=before,
        after=_total_values(row),
    )
    await hydrate_yesterday_usage(db, upstream=upstream, now=now, time_zone=time_zone)
    return True


async def upsert_historical_upstream_usage(
    db: AsyncSession,
    *,
    upstream: Upstream,
    usage_date: date,
    upstream_wallet_cost_usd: float,
    balance_unit: str = "USD",
    upstream_recharge_multiplier: float | None = None,
    observed_at: datetime | None = None,
) -> UpstreamDailyUsage:
    wallet_cost = _finite_amount(upstream_wallet_cost_usd)
    if wallet_cost is None:
        raise ValueError("upstream_wallet_cost_usd must be a finite non-negative number.")
    identity = upstream_id(upstream)
    row = await _daily_row(db, identity=identity, day=usage_date)
    if row is None:
        row = UpstreamDailyUsage(
            upstream_id=identity,
            upstream_name=_safe_text(upstream.display_name, limit=200),
            usage_date=usage_date,
        )
        db.add(row)
        await db.flush()
    before = _total_values(row)
    row.upstream_name = _safe_text(upstream.display_name, limit=200)
    row.upstream_wallet_cost_usd = wallet_cost
    row.balance_unit = _safe_text(balance_unit, limit=32) or "USD"
    multiplier = _finite_amount(row.upstream_recharge_multiplier)
    if multiplier is None:
        multiplier = (
            _finite_amount(upstream_recharge_multiplier)
            if upstream_recharge_multiplier is not None
            else _snapshot_multiplier(upstream)
        )
    row.upstream_recharge_multiplier = multiplier
    row.upstream_actual_cost_cny = _adjusted(wallet_cost, multiplier)
    row.observed_at = observed_at or datetime.now(timezone.utc)
    row.finalized = True
    row.finalized_at = row.finalized_at or row.observed_at
    await db.flush()
    await _apply_lifetime_delta(
        db,
        upstream=upstream,
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
        delete(ApiAccountDailyUsage).where(ApiAccountDailyUsage.usage_date < cutoff)
    )
    await db.execute(
        delete(UpstreamDailyUsage).where(UpstreamDailyUsage.usage_date < cutoff)
    )


def _aggregate_account_rows(rows: list[ApiAccountDailyUsage]) -> dict[str, Any]:
    upstream_cost = _sum_known(row.upstream_actual_cost_cny for row in rows)
    management_cost = _sum_known(row.management_account_cost_cny for row in rows)
    income = _sum_known(row.actual_income_cny for row in rows)
    consumption = _maximum_cost(upstream_cost, management_cost)
    profit = _profit(income, upstream_cost, management_cost)
    latest = max(rows, key=lambda row: (row.observed_at or row.created_at, row.id))
    return {
        "management_account_id": int(latest.management_account_id),
        "api_account_id": latest.api_account_id,
        "account_name": latest.account_name,
        "remote_key_id": latest.remote_upstream_api_key_id,
        "upstream_api_key_id": latest.upstream_api_key_id,
        "upstream_wallet_cost_usd": _sum_known(row.upstream_wallet_cost_usd for row in rows),
        "upstream_usage_unit": _safe_text(latest.upstream_usage_unit, limit=32),
        "upstream_usage_source": _safe_text(latest.upstream_usage_source, limit=64),
        "upstream_recharge_multiplier": _single_multiplier(
            row.upstream_recharge_multiplier for row in rows
        ),
        "upstream_actual_cost_cny": upstream_cost,
        "management_account_cost_usd": _sum_known(
            row.management_account_cost_usd for row in rows
        ),
        "management_account_cost_cny": management_cost,
        "management_user_charge_usd": _sum_known(
            row.management_user_charge_usd for row in rows
        ),
        "management_recharge_multiplier": _single_multiplier(
            row.management_recharge_multiplier for row in rows
        ),
        "actual_income_cny": income,
        "income_unit": "CNY" if income is not None else None,
        "profit_cny": profit,
        "profit_margin": _profit_margin(consumption, profit),
    }


def _aggregate_history_values(
    upstream_rows: list[UpstreamDailyUsage],
    account_rows: list[ApiAccountDailyUsage],
    *,
    account_filter: bool,
) -> dict[str, float | None]:
    account_wallet = _sum_known(row.upstream_wallet_cost_usd for row in account_rows)
    account_upstream_cost = _sum_known(row.upstream_actual_cost_cny for row in account_rows)
    upstream_wallet = _sum_known(row.upstream_wallet_cost_usd for row in upstream_rows)
    upstream_cost = _sum_known(row.upstream_actual_cost_cny for row in upstream_rows)
    if account_filter or account_wallet is not None:
        visible_wallet = account_wallet
        visible_upstream_cost = account_upstream_cost
    else:
        visible_wallet = upstream_wallet
        visible_upstream_cost = upstream_cost
    account_management_cost_usd = _sum_known(
        row.management_account_cost_usd for row in account_rows
    )
    account_management_cost_cny = _sum_known(
        row.management_account_cost_cny for row in account_rows
    )
    account_user_charge = _sum_known(row.management_user_charge_usd for row in account_rows)
    account_income = _sum_known(row.actual_income_cny for row in account_rows)
    management_cost_usd = (
        account_management_cost_usd
        if account_management_cost_usd is not None or account_filter
        else _sum_known(row.management_account_cost_usd for row in upstream_rows)
    )
    management_cost_cny = (
        account_management_cost_cny
        if account_management_cost_cny is not None or account_filter
        else _sum_known(row.management_account_cost_cny for row in upstream_rows)
    )
    user_charge = (
        account_user_charge
        if account_user_charge is not None or account_filter
        else _sum_known(row.management_user_charge_usd for row in upstream_rows)
    )
    income = (
        account_income
        if account_income is not None or account_filter
        else _sum_known(row.actual_income_cny for row in upstream_rows)
    )
    consumption = _maximum_cost(visible_upstream_cost, management_cost_cny)
    profit = _profit(income, visible_upstream_cost, management_cost_cny)
    return {
        "upstream_wallet_cost_usd": visible_wallet,
        "upstream_actual_cost_cny": visible_upstream_cost,
        "management_account_cost_usd": management_cost_usd,
        "management_account_cost_cny": management_cost_cny,
        "management_user_charge_usd": user_charge,
        "actual_income_cny": income,
        "consumption_cny": consumption,
        "profit_cny": profit,
        "profit_margin": _profit_margin(consumption, profit),
    }


async def usage_history(
    db: AsyncSession,
    *,
    upstream: Upstream,
    start_date: date,
    end_date: date,
    management_account_id: int | None = None,
    time_zone: str = DEFAULT_TIME_ZONE,
) -> dict[str, Any]:
    if end_date < start_date:
        raise ValueError("end_date must not be earlier than start_date.")
    if (end_date - start_date).days + 1 > MAX_HISTORY_DAYS:
        raise ValueError(f"A history request can cover at most {MAX_HISTORY_DAYS} days.")
    identity = upstream_id(upstream)
    upstream_rows = list(
        (
            await db.scalars(
                select(UpstreamDailyUsage)
                .where(
                    UpstreamDailyUsage.upstream_id == identity,
                    UpstreamDailyUsage.usage_date >= start_date,
                    UpstreamDailyUsage.usage_date <= end_date,
                )
                .order_by(
                    UpstreamDailyUsage.usage_date,
                    UpstreamDailyUsage.source_segment,
                    UpstreamDailyUsage.id,
                )
            )
        ).all()
    )
    account_statement = (
        select(ApiAccountDailyUsage)
        .where(
            ApiAccountDailyUsage.upstream_id == identity,
            ApiAccountDailyUsage.usage_date >= start_date,
            ApiAccountDailyUsage.usage_date <= end_date,
        )
        .order_by(
            ApiAccountDailyUsage.usage_date,
            ApiAccountDailyUsage.management_account_id,
            ApiAccountDailyUsage.source_segment,
            ApiAccountDailyUsage.id,
        )
    )
    if management_account_id is not None:
        account_statement = account_statement.where(
            ApiAccountDailyUsage.management_account_id == management_account_id
        )
    account_rows = list((await db.scalars(account_statement)).all())
    upstream_by_date: dict[date, list[UpstreamDailyUsage]] = defaultdict(list)
    account_by_date: dict[date, list[ApiAccountDailyUsage]] = defaultdict(list)
    for row in upstream_rows:
        upstream_by_date[row.usage_date].append(row)
    for row in account_rows:
        account_by_date[row.usage_date].append(row)

    configured_accounts = list(
        (
            await db.scalars(
                select(ApiAccount)
                .where(ApiAccount.upstream_id == identity)
                .order_by(ApiAccount.management_account_id)
            )
        ).all()
    )
    account_catalog: dict[int, dict[str, Any]] = {
        int(config.management_account_id): {
            "management_account_id": int(config.management_account_id),
            "api_account_id": config.id,
            "account_name": config.remote_name,
            "remote_key_id": config.remote_upstream_api_key_id,
            "upstream_api_key_id": config.upstream_api_key_id,
        }
        for config in configured_accounts
    }
    for row in account_rows:
        account_catalog.setdefault(
            int(row.management_account_id),
            {
                "management_account_id": int(row.management_account_id),
                "api_account_id": row.api_account_id,
                "account_name": row.account_name,
                "remote_key_id": row.remote_upstream_api_key_id,
                "upstream_api_key_id": row.upstream_api_key_id,
            },
        )

    days: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= end_date:
        date_upstream_rows = upstream_by_date.get(cursor, [])
        date_account_rows = account_by_date.get(cursor, [])
        rows_by_account: dict[int, list[ApiAccountDailyUsage]] = defaultdict(list)
        for row in date_account_rows:
            rows_by_account[int(row.management_account_id)].append(row)
        account_items = [
            _aggregate_account_rows(rows_by_account[account_id])
            for account_id in sorted(rows_by_account)
        ]
        values = _aggregate_history_values(
            date_upstream_rows,
            date_account_rows,
            account_filter=management_account_id is not None,
        )
        upstream_multiplier_source: Iterable[float | None]
        if date_account_rows and values["upstream_wallet_cost_usd"] is not None:
            upstream_multiplier_source = (
                row.upstream_recharge_multiplier for row in date_account_rows
            )
        else:
            upstream_multiplier_source = (
                row.upstream_recharge_multiplier for row in date_upstream_rows
            )
        management_multiplier_source: Iterable[float | None] = (
            row.management_recharge_multiplier for row in date_account_rows
        )
        days.append(
            {
                "date": cursor,
                "upstream_wallet_cost_usd": values["upstream_wallet_cost_usd"],
                "upstream_recharge_multiplier": _single_multiplier(
                    upstream_multiplier_source
                ),
                "upstream_actual_cost_cny": values["upstream_actual_cost_cny"],
                "management_account_cost_usd": values["management_account_cost_usd"],
                "management_account_cost_cny": values["management_account_cost_cny"],
                "management_user_charge_usd": values["management_user_charge_usd"],
                "management_recharge_multiplier": _single_multiplier(
                    management_multiplier_source
                ),
                "actual_income_cny": values["actual_income_cny"],
                "income_unit": "CNY" if values["actual_income_cny"] is not None else None,
                "consumption_cny": values["consumption_cny"],
                "profit_cny": values["profit_cny"],
                "profit_margin": values["profit_margin"],
                "finalized": bool(date_upstream_rows)
                and all(row.finalized for row in date_upstream_rows),
                "api_accounts": account_items,
            }
        )
        cursor += timedelta(days=1)

    def sum_days(field: str) -> float | None:
        return _sum_known(
            float(item[field]) if item[field] is not None else None for item in days
        )

    totals = {
        field: sum_days(field)
        for field in (
            "upstream_wallet_cost_usd",
            "upstream_actual_cost_cny",
            "management_account_cost_usd",
            "management_account_cost_cny",
            "management_user_charge_usd",
            "actual_income_cny",
            "consumption_cny",
            "profit_cny",
        )
    }
    totals["profit_margin"] = _profit_margin(
        totals["consumption_cny"],
        totals["profit_cny"],
    )

    if management_account_id is not None:
        lifetime_account_rows = list(
            (
                await db.scalars(
                    select(ApiAccountDailyUsage).where(
                        ApiAccountDailyUsage.upstream_id == identity,
                        ApiAccountDailyUsage.management_account_id
                        == management_account_id,
                    )
                )
            ).all()
        )
        lifetime_values = _aggregate_history_values(
            [],
            lifetime_account_rows,
            account_filter=True,
        )
    else:
        lifetime_upstream_rows = list(
            (
                await db.scalars(
                    select(UpstreamDailyUsage).where(
                        UpstreamDailyUsage.upstream_id == identity
                    )
                )
            ).all()
        )
        lifetime_values = _aggregate_history_values(
            lifetime_upstream_rows,
            [],
            account_filter=False,
        )
        total = await db.get(UpstreamUsageTotal, identity)
        if total is not None:
            lifetime_values = {
                "upstream_wallet_cost_usd": float(total.total_upstream_wallet_cost_usd),
                "upstream_actual_cost_cny": float(total.total_upstream_actual_cost_cny),
                "management_account_cost_usd": float(
                    total.total_management_account_cost_usd
                ),
                "management_account_cost_cny": float(
                    total.total_management_account_cost_cny
                ),
                "management_user_charge_usd": float(
                    total.total_management_user_charge_usd
                ),
                "actual_income_cny": float(total.total_actual_income_cny),
                "consumption_cny": _sum_known(
                    _maximum_cost(
                        row.upstream_actual_cost_cny,
                        row.management_account_cost_cny,
                    )
                    for row in lifetime_upstream_rows
                ),
                "profit_cny": float(total.total_profit_cny),
                "profit_margin": None,
            }
            lifetime_values["profit_margin"] = _profit_margin(
                lifetime_values["consumption_cny"],
                lifetime_values["profit_cny"],
            )

    return {
        "upstream_id": identity,
        "upstream_name": upstream.display_name,
        "time_zone": time_zone,
        "start_date": start_date,
        "end_date": end_date,
        "management_account_id": management_account_id,
        "api_accounts": list(account_catalog.values()),
        "days": days,
        "totals": totals,
        "lifetime_totals": lifetime_values,
    }


__all__ = [
    "DEFAULT_TIME_ZONE",
    "MAX_HISTORY_DAYS",
    "finalize_cached_yesterday_usage",
    "finalize_elapsed_usage",
    "finalize_yesterday_usage",
    "hydrate_yesterday_usage",
    "import_sub2api_daily_stats",
    "missing_finalized_usage_dates",
    "normalize_history_time_zone",
    "prune_upstream_usage_history",
    "should_fetch_yesterday_usage",
    "snapshot_today_usage",
    "upsert_historical_upstream_usage",
    "upstream_id",
    "usage_day",
    "usage_history",
]
