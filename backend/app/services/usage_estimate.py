from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, UsageLimitSample, UsageTokenWindow, UsageWindowState, utcnow
from app.services.runtime_config import get_runtime_config_service
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError
from app.core.subscription_types import (
    CORE_SUBSCRIPTION_TYPES,
    SUBSCRIPTION_TYPE_UNKNOWN,
    normalize_subscription_type,
    normalize_usage_limit_ranges,
    subscription_type_label,
    subscription_type_sort_key,
    usage_limit_bounds,
)


WINDOWS = {
    "five_hour": {
        "extra_prefix": "codex_5h",
        "label": "5h",
    },
    "seven_day": {
        "extra_prefix": "codex_7d",
        "label": "7d",
    },
}

UNGROUPED_ID = "ungrouped"
UNGROUPED_NAME = "未分组"
EPSILON = 1e-9
LIMIT_SAMPLE_TARGET = 100
LIMIT_SAMPLE_STATISTICS_MIN_COUNT = 10
LIMIT_SAMPLE_FULL_PERCENT = 99.0
LIMIT_SAMPLE_RESET_TOLERANCE_SECONDS = 120
MONTHLY_WINDOW_MINUTES_THRESHOLD = 28 * 24 * 60
MONTHLY_SAMPLE_WINDOW_KEY = "monthly"
WINDOW_RESET_SECONDS = {
    "five_hour": 5 * 60 * 60,
    "seven_day": 7 * 24 * 60 * 60,
    MONTHLY_SAMPLE_WINDOW_KEY: 30 * 24 * 60 * 60,
}
RESET_AT_RESET_JUMP_FRACTION = 0.5
STALE_DELTA_USED_PERCENT_THRESHOLD = 10.0
STALE_DELTA_SPENT_RATIO_THRESHOLD = 0.1
PLAN_COHORT_UNKNOWN = SUBSCRIPTION_TYPE_UNKNOWN
DEFAULT_SAMPLE_PLAN_COHORTS = CORE_SUBSCRIPTION_TYPES
PLAN_COHORT_PATHS = (
    ("credentials", "plan_type"),
    ("credentials", "planType"),
    ("plan_type",),
    ("planType",),
    ("credentials", "subscription_plan"),
    ("credentials", "subscriptionPlan"),
    ("subscription_plan",),
    ("subscriptionPlan",),
    ("account", "planType"),
    ("account", "plan_type"),
    ("entitlement", "subscription_plan"),
    ("entitlement", "subscriptionPlan"),
    ("account", "entitlement", "subscription_plan"),
    ("account", "entitlement", "subscriptionPlan"),
    ("last_active_subscription", "subscription_plan"),
    ("last_active_subscription", "subscriptionPlan"),
    ("last_active_subscription", "plan_type"),
    ("last_active_subscription", "planType"),
    ("last_active_subscription", "plan"),
    ("last_active_subscription", "name"),
)
SAMPLE_WINDOWS = {
    **WINDOWS,
    MONTHLY_SAMPLE_WINDOW_KEY: {
        "extra_prefix": "codex_7d",
        "label": "月",
    },
}
WINDOW_USAGE_KEYS = {
    "five_hour": ("five_hour", "fiveHour", "5h"),
    "seven_day": ("seven_day", "sevenDay", "7d", "weekly", "week"),
}
MONTHLY_USAGE_KEYS = (
    "monthly",
    "month",
    "monthly_window",
    "monthlyWindow",
    "monthly_limit",
    "monthlyLimit",
)


def _usage_limit_sample_allowed(
    window_key: str,
    plan_cohort: str,
    observed_limit: float | None,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> bool:
    if window_key not in SAMPLE_WINDOWS:
        return False
    normalized = _normalize_plan_cohort(plan_cohort)
    if normalized == PLAN_COHORT_UNKNOWN:
        return False
    value = _coerce_float(observed_limit)
    if value is None or value <= 0:
        return False
    lower_bound, _ = _default_limit_bounds(window_key, normalized, default_ranges)
    return value >= lower_bound


async def build_usage_estimate(
    refresh: bool = True,
    usage_by_account_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sub2api = Sub2ApiClient()
    accounts, _ = sub2api.dedupe_accounts_by_email(
        [
            account
            for account in await sub2api.list_accounts()
            if sub2api.is_gpt_account(account) and sub2api.is_oauth_account(account)
        ]
    )
    group_map = await _load_group_map(sub2api)
    estimate_preferences = await _load_usage_estimate_preferences()
    usage_states = await _load_usage_window_states()

    refreshed_usage = refresh or usage_by_account_id is not None
    usage_by_account_id = dict(usage_by_account_id or {})
    errors_by_account_id: dict[str, str] = {}
    if refresh and not usage_by_account_id:
        usage_by_account_id, errors_by_account_id = await _fetch_usages(sub2api, accounts)
        accounts = await _reload_usage_sample_accounts(sub2api, accounts)
        await record_usage_limit_samples(sub2api, accounts, usage_by_account_id)
    allowed_account_ids = {account_id for account in accounts if (account_id := sub2api.account_id(account))}
    runtime_config = get_runtime_config_service()
    default_ranges = await runtime_config.get_usage_limit_default_ranges()
    limit_calibrations = await _load_usage_limit_calibrations(default_ranges)
    token_history = await _load_usage_token_window_history(allowed_account_ids, default_ranges)
    sample_thresholds = await runtime_config.get_usage_limit_sample_thresholds()

    account_rows = [
        _account_estimate(
            account=account,
            group_map=group_map,
            sub2api=sub2api,
            usage_estimate_enabled=_usage_estimate_enabled(account, sub2api, estimate_preferences),
            usage=usage_by_account_id.get(sub2api.account_id(account) or "", {}),
            usage_error=errors_by_account_id.get(sub2api.account_id(account) or ""),
            usage_states=usage_states,
            limit_calibrations=limit_calibrations,
            token_history=token_history,
            sample_thresholds=sample_thresholds,
            default_ranges=default_ranges,
        )
        for account in accounts
    ]
    await _save_usage_window_states(usage_states)

    result = {
        "updated_at": utcnow(),
        "refreshed_usage": refreshed_usage,
        "formula": {
            "basis": "当前官方窗口用量 = sub2api 原始窗口用量。",
            "reset_rule": "当前估算直接使用 sub2api 返回的窗口已用额度、重置时间和剩余秒数，不再基于本地状态机修正已用额度。",
            "account_limit": "单账号总额：优先按 sub2api 原始窗口已用额度 / sub2api 官方已用百分比 反推；反推总额存在时优先于 sub2api 原始 limit 字段；官方窗口已满时直接采用当前窗口反推总额；窗口用量为 0 时优先取同套餐样本窗口代表值，样本数量小于 10 条时取默认窗口代表值；未满窗口总额按限流样本区间裁剪，但不会裁到低于当前官方已用额度。",
            "account_remaining": "单账号剩余：有 sub2api 官方已用百分比时，按 估算总额 × (100 - 官方已用百分比) / 100 估算；缺少官方百分比时回退为 估算总额 - 估算已用额度。",
            "aggregate_remaining": "综合 5h 剩余只统计当前可用账号：有独立 5h 窗口时使用 5h 额度，只有 7d/月窗口时复用该长窗口额度；综合 7d/月剩余统计尚未达到 7d/月限额账号的长窗口额度，不因 5h 窗口限流而排除。",
            "estimable_rule": "sub2api 官方已用百分比始终保留用于显示；有实际窗口用量和官方百分比时，总额度先按两者反推，未满窗口再按样本区间裁剪；已用额度继续使用 sub2api 原始窗口已用金额；若当前已用额度已经超过样本上界，则保留官方用量/百分比反推结果；官方窗口已满时不做默认/样本裁剪；当前窗口用量为 0 时优先使用同套餐样本，样本数量小于 10 条时使用默认窗口；只有百分比、没有用量金额时只估算剩余，不反填已用金额。",
        },
        "overall": {
            "account_count": len(account_rows),
            "five_hour": _aggregate_window(account_rows, "five_hour"),
            "seven_day": _aggregate_window(account_rows, "seven_day"),
        },
        "groups": _group_estimates(account_rows),
        "accounts": account_rows,
    }
    return result


def _normalize_plan_cohort(value: Any) -> str:
    return normalize_subscription_type(value)


def _plan_cohort_from_account(account: dict[str, Any]) -> str:
    for path in PLAN_COHORT_PATHS:
        cohort = _normalize_plan_cohort(_path_get(account, path))
        if cohort != PLAN_COHORT_UNKNOWN:
            return cohort
    for path in (("email",), ("account_email",), ("name",), ("credentials", "email"), ("extra", "email")):
        text = _stringify(_path_get(account, path))
        if text and "gptteam" in text.lower():
            return "team"
    return PLAN_COHORT_UNKNOWN


def _plan_cohort_label(cohort: str) -> str:
    return subscription_type_label(cohort)


def _plan_cohort_sort_key(cohort: str) -> tuple[int, str]:
    return subscription_type_sort_key(cohort)


def _default_limit_bounds(
    window_key: str,
    plan_cohort: str = PLAN_COHORT_UNKNOWN,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> tuple[float, float]:
    return usage_limit_bounds(default_ranges, window_key, plan_cohort)


def _default_sample_plan_cohorts(
    window_key: str,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> tuple[str, ...]:
    ranges = normalize_usage_limit_ranges(default_ranges)
    return tuple(
        subscription_type
        for subscription_type in sorted(ranges, key=_plan_cohort_sort_key)
        if subscription_type != PLAN_COHORT_UNKNOWN and window_key in ranges[subscription_type]
    )


async def _load_group_map(sub2api: Sub2ApiClient) -> dict[str, str]:
    try:
        groups = await sub2api.list_groups()
    except Sub2ApiRequestError:
        return {}

    group_map: dict[str, str] = {}
    for group in groups:
        group_id = _stringify(group.get("id"))
        name = _stringify(group.get("name")) or group_id
        if group_id and name:
            group_map[group_id] = name
    return group_map


async def _load_usage_estimate_preferences() -> dict[str, bool]:
    preferences: dict[str, bool] = {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(AccountSnapshot))
        for snapshot in result.scalars().all():
            enabled = snapshot.usage_estimate_enabled is not False
            preferences[f"email:{snapshot.email.lower()}"] = enabled
            if snapshot.sub2api_account_id:
                preferences[f"id:{snapshot.sub2api_account_id}"] = enabled
    return preferences


async def _load_usage_window_states() -> dict[tuple[str, str], dict[str, Any]]:
    states: dict[tuple[str, str], dict[str, Any]] = {}
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UsageWindowState))
        for state in result.scalars().all():
            states[(state.account_key, state.window_key)] = {
                "account_key": state.account_key,
                "email": state.email,
                "sub2api_account_id": state.sub2api_account_id,
                "window_key": state.window_key,
                "baseline_spent": state.baseline_spent,
                "estimate_uses_spent_delta": bool(state.estimate_uses_spent_delta),
                "last_raw_spent": state.last_raw_spent,
                "last_used_percent": state.last_used_percent,
                "last_reset_at": state.last_reset_at,
                "last_remaining_seconds": state.last_remaining_seconds,
            }
    return states


async def _save_usage_window_states(states: dict[tuple[str, str], dict[str, Any]]) -> None:
    dirty_states = [state for state in states.values() if state.get("dirty")]
    if not dirty_states:
        return

    async with AsyncSessionLocal() as db:
        for state_data in dirty_states:
            account_key = state_data["account_key"]
            window_key = state_data["window_key"]
            result = await db.execute(
                select(UsageWindowState).where(
                    UsageWindowState.account_key == account_key,
                    UsageWindowState.window_key == window_key,
                )
            )
            state = result.scalar_one_or_none()
            if state is None:
                state = UsageWindowState(account_key=account_key, window_key=window_key)
                db.add(state)
            state.email = state_data.get("email")
            state.sub2api_account_id = state_data.get("sub2api_account_id")
            state.baseline_spent = state_data.get("baseline_spent")
            state.estimate_uses_spent_delta = bool(state_data.get("estimate_uses_spent_delta", False))
            state.last_raw_spent = state_data.get("last_raw_spent")
            state.last_used_percent = state_data.get("last_used_percent")
            state.last_reset_at = state_data.get("last_reset_at")
            state.last_remaining_seconds = state_data.get("last_remaining_seconds")
            state.updated_at = utcnow()
        await db.commit()


async def _load_usage_token_window_history(
    allowed_account_ids: set[str] | None = None,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if allowed_account_ids is not None and not allowed_account_ids:
        return history

    async with AsyncSessionLocal() as db:
        sample_query = (
            select(UsageLimitSample)
            .where(UsageLimitSample.window_key.in_(["seven_day", MONTHLY_SAMPLE_WINDOW_KEY]))
            .order_by(UsageLimitSample.account_key, UsageLimitSample.updated_at.desc(), UsageLimitSample.id.desc())
        )
        if allowed_account_ids is not None:
            sample_query = sample_query.where(UsageLimitSample.sub2api_account_id.in_(allowed_account_ids))
        sample_result = await db.execute(sample_query)
        limit_samples_by_account: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in sample_result.scalars().all():
            if not _usage_limit_sample_allowed(
                row.window_key, row.plan_cohort, row.observed_limit, default_ranges
            ):
                continue
            limit_samples_by_account[(row.account_key, row.window_key)].append(
                {
                    "reset_at": row.reset_at,
                    "observed_limit": float(row.observed_limit or 0),
                }
            )
        history_query = (
            select(UsageTokenWindow)
            .where(UsageTokenWindow.window_key.in_(["seven_day", MONTHLY_SAMPLE_WINDOW_KEY]))
            .order_by(UsageTokenWindow.account_key, UsageTokenWindow.last_observed_at.desc(), UsageTokenWindow.id.desc())
        )
        if allowed_account_ids is not None:
            history_query = history_query.where(UsageTokenWindow.sub2api_account_id.in_(allowed_account_ids))
        result = await db.execute(history_query)
        for row in result.scalars().all():
            first_observed_at = row.first_observed_at or row.created_at or row.updated_at or utcnow()
            last_observed_at = row.last_observed_at or row.updated_at or first_observed_at
            history[row.account_key].append(
                {
                    "window_key": row.window_key,
                    "window_reset_key": row.window_reset_key,
                    "window_start_at": row.window_start_at,
                    "reset_at": row.reset_at,
                    "spent": float(row.spent or 0),
                    "tokens": int(row.tokens or 0),
                    "estimated_limit": _historical_window_limit(limit_samples_by_account.get((row.account_key, row.window_key), []), row.reset_at),
                    "first_observed_at": first_observed_at,
                    "last_observed_at": last_observed_at,
                }
            )
    return history


async def _save_usage_token_windows(
    accounts: list[dict[str, Any]],
    sub2api: Sub2ApiClient,
    usage_by_account_id: dict[str, dict[str, Any]],
) -> None:
    observations = _usage_token_window_observations(accounts, sub2api, usage_by_account_id)
    if not observations:
        return

    now = utcnow()
    async with AsyncSessionLocal() as db:
        for observation in observations:
            result = await db.execute(
                select(UsageTokenWindow).where(
                    UsageTokenWindow.account_key == observation["account_key"],
                    UsageTokenWindow.window_key == observation["window_key"],
                    UsageTokenWindow.window_reset_key == observation["window_reset_key"],
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = UsageTokenWindow(
                    account_key=observation["account_key"],
                    window_key=observation["window_key"],
                    window_reset_key=observation["window_reset_key"],
                )
                row.first_observed_at = now
                db.add(row)
            row.email = observation.get("email")
            row.sub2api_account_id = observation.get("sub2api_account_id")
            row.window_start_at = observation.get("window_start_at")
            row.reset_at = observation.get("reset_at")
            row.spent = max(float(row.spent or 0), float(observation.get("spent") or 0))
            row.tokens = max(int(row.tokens or 0), int(observation["tokens"]))
            row.last_observed_at = now
            row.updated_at = now
        await db.commit()


async def _fetch_usages(
    sub2api: Sub2ApiClient,
    accounts: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    configured_concurrency = (
        await get_runtime_config_service().get_usage_refresh_max_concurrency()
    )
    effective_concurrency = (
        len(accounts) if configured_concurrency == 0 else configured_concurrency
    )
    semaphore = asyncio.Semaphore(max(1, effective_concurrency))
    usage_by_id: dict[str, dict[str, Any]] = {}
    errors_by_id: dict[str, str] = {}

    async def fetch(account: dict[str, Any]) -> None:
        account_id = sub2api.account_id(account)
        if not account_id:
            return
        async with semaphore:
            try:
                usage_by_id[account_id] = materialize_usage_reset_times(await sub2api.get_account_usage(account_id, force=True))
            except Sub2ApiRequestError as exc:
                errors_by_id[account_id] = str(exc)

    await asyncio.gather(*(fetch(account) for account in accounts))
    return usage_by_id, errors_by_id


def materialize_usage_reset_times(usage: dict[str, Any], observed_at: datetime | None = None) -> dict[str, Any]:
    observed_at = _normalize_datetime(observed_at or utcnow())
    for window_key in WINDOWS:
        window_data, _ = _usage_window_data(usage, window_key)
        if not window_data or _usage_window_reset_at(window_data):
            continue
        remaining_seconds = _usage_window_remaining_seconds(window_data)
        if remaining_seconds is not None:
            window_data["reset_at"] = _format_datetime(observed_at + timedelta(seconds=max(remaining_seconds, 0)))
    return usage


async def record_usage_limit_samples(
    sub2api: Sub2ApiClient,
    accounts: list[dict[str, Any]],
    usage_by_account_id: dict[str, dict[str, Any]],
) -> None:
    for usage in usage_by_account_id.values():
        if isinstance(usage, dict):
            materialize_usage_reset_times(usage)
    accounts = await _reload_usage_sample_accounts(sub2api, accounts)
    await _save_refreshed_usage_window_states(accounts, sub2api, usage_by_account_id)
    await _save_usage_token_windows(accounts, sub2api, usage_by_account_id)
    runtime_config = get_runtime_config_service()
    thresholds = await runtime_config.get_usage_limit_sample_thresholds()
    default_ranges = await runtime_config.get_usage_limit_default_ranges()
    samples = _usage_limit_samples(accounts, sub2api, usage_by_account_id, thresholds, default_ranges)
    await _save_usage_limit_samples(samples, default_ranges)


async def _reload_usage_sample_accounts(
    sub2api: Sub2ApiClient,
    accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        refreshed_accounts, _ = sub2api.dedupe_accounts_by_email(
            [
                account
                for account in await sub2api.list_accounts()
                if sub2api.is_gpt_account(account) and sub2api.is_oauth_account(account)
            ]
        )
    except Sub2ApiRequestError:
        return accounts

    refreshed_by_id = {
        account_id: account
        for account in refreshed_accounts
        if (account_id := sub2api.account_id(account))
    }
    return [refreshed_by_id.get(account_id, account) if (account_id := sub2api.account_id(account)) else account for account in accounts]


async def _save_refreshed_usage_window_states(
    accounts: list[dict[str, Any]],
    sub2api: Sub2ApiClient,
    usage_by_account_id: dict[str, dict[str, Any]],
) -> None:
    usage_states = await _load_usage_window_states()
    for account in accounts:
        account_id = sub2api.account_id(account)
        if not account_id:
            continue
        usage = usage_by_account_id.get(account_id)
        if not usage:
            continue
        email = sub2api.account_email(account) or _stringify(account.get("name")) or account_id
        account_key = _account_state_key(account_id, email)
        for window_key in WINDOWS:
            values = _refreshed_window_state_values(account, usage, window_key)
            raw_spent = values["raw_spent"]
            used_percent = values["used_percent"]
            if raw_spent is None or used_percent is None:
                continue
            _window_effective_spent(
                account_key=account_key,
                account_id=account_id,
                email=email,
                window_key=window_key,
                raw_spent=raw_spent,
                used_percent=used_percent,
                reset_at=values["reset_at"],
                remaining_seconds=values["remaining_seconds"],
                usage_states=usage_states,
            )
    await _save_usage_window_states(usage_states)


def _refreshed_window_state_values(account: dict[str, Any], usage: dict[str, Any], window_key: str) -> dict[str, Any]:
    values = _window_values(account, usage, window_key)
    account_values = _window_values(account, {}, window_key)
    used_percent = _clamp_percent(_coerce_float(values.get("used_percent")))
    raw_spent = _coerce_float(values.get("raw_spent"))
    reset_at = _stringify(values.get("reset_at"))
    remaining_seconds = _coerce_int(values.get("remaining_seconds"))

    account_used_percent = _clamp_percent(_coerce_float(account_values.get("used_percent")))
    if _usage_window_clears_stale_spend(account_values, used_percent, raw_spent, remaining_seconds, reset_at):
        raw_spent = 0.0
        used_percent = account_used_percent if account_used_percent is not None else 0.0
    elif _account_clears_zero_spend_limit(account_values, used_percent, raw_spent):
        raw_spent = 0.0
        used_percent = account_used_percent
        reset_at = _stringify(account_values.get("reset_at")) or reset_at
        remaining_seconds = _coerce_int(account_values.get("remaining_seconds")) or remaining_seconds
    elif not values["window_data"] and account_used_percent is not None and account_used_percent < LIMIT_SAMPLE_FULL_PERCENT:
        if used_percent is not None and used_percent >= LIMIT_SAMPLE_FULL_PERCENT:
            raw_spent = 0.0
        used_percent = account_used_percent
        reset_at = _stringify(account_values.get("reset_at")) or reset_at
        remaining_seconds = _coerce_int(account_values.get("remaining_seconds")) or remaining_seconds

    return {
        "raw_spent": raw_spent,
        "used_percent": used_percent,
        "reset_at": reset_at,
        "remaining_seconds": remaining_seconds,
    }


def _account_clears_zero_spend_limit(
    account_values: dict[str, Any],
    used_percent: float | None,
    raw_spent: float | None,
) -> bool:
    account_used_percent = _clamp_percent(_coerce_float(account_values.get("used_percent")))
    if account_used_percent is None or account_used_percent >= LIMIT_SAMPLE_FULL_PERCENT:
        return False

    normalized_used_percent = _clamp_percent(used_percent)
    if normalized_used_percent is None or normalized_used_percent < LIMIT_SAMPLE_FULL_PERCENT:
        return False

    normalized_raw_spent = _coerce_float(raw_spent)
    return normalized_raw_spent is None or normalized_raw_spent <= EPSILON


def _usage_window_clears_stale_spend(
    account_values: dict[str, Any],
    used_percent: float | None,
    raw_spent: float | None,
    remaining_seconds: int | None,
    reset_at: str | None,
) -> bool:
    normalized_raw_spent = _coerce_float(raw_spent)
    if normalized_raw_spent is None or normalized_raw_spent <= EPSILON:
        return False

    normalized_used_percent = _clamp_percent(used_percent)
    if normalized_used_percent is None or normalized_used_percent > EPSILON:
        return False

    account_used_percent = _clamp_percent(_coerce_float(account_values.get("used_percent")))
    if account_used_percent is not None and account_used_percent > EPSILON:
        return False

    normalized_remaining_seconds = _coerce_int(remaining_seconds)
    if normalized_remaining_seconds is not None:
        return normalized_remaining_seconds <= 0

    reset_dt = _parse_datetime(_stringify(reset_at))
    return reset_dt is not None and _normalize_datetime(reset_dt) <= datetime.now(timezone.utc)


def _usage_token_window_observations(
    accounts: list[dict[str, Any]],
    sub2api: Sub2ApiClient,
    usage_by_account_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    observed_at = utcnow()
    for account in accounts:
        account_id = sub2api.account_id(account)
        if not account_id:
            continue
        usage = usage_by_account_id.get(account_id)
        if not usage:
            continue
        email = sub2api.account_email(account) or _stringify(account.get("name")) or account_id
        values, _, window_kind = _estimate_window_values(account, usage, "seven_day")
        history_window_key = MONTHLY_SAMPLE_WINDOW_KEY if window_kind == "monthly" else "seven_day"
        tokens = _tokens_value(values["stats"], values["window_data"])
        spent = _coerce_float(values.get("raw_spent"))
        if (tokens is None or tokens <= 0) and (spent is None or spent <= 0):
            continue
        reset_key, reset_at, window_start_at = _token_window_identity(
            values.get("reset_at"),
            _coerce_int(values.get("remaining_seconds")),
            observed_at,
            history_window_key,
            _coerce_int(values.get("window_minutes")),
        )
        observations.append(
            {
                "account_key": _account_state_key(account_id, email),
                "email": email,
                "sub2api_account_id": account_id,
                "window_key": history_window_key,
                "window_reset_key": reset_key,
                "window_start_at": window_start_at,
                "reset_at": reset_at,
                "spent": spent or 0.0,
                "tokens": tokens or 0,
            }
        )
    return observations


def _token_window_identity(
    reset_at: str | None,
    remaining_seconds: int | None,
    observed_at: datetime,
    window_key: str = "seven_day",
    window_minutes: int | None = None,
) -> tuple[str, str | None, str | None]:
    reset_dt = _parse_datetime(reset_at)
    effective_reset_at = reset_at
    if reset_dt is None and remaining_seconds is not None:
        reset_dt = observed_at + timedelta(seconds=max(remaining_seconds, 0))
        effective_reset_at = _format_datetime(reset_dt)

    if reset_dt is not None:
        reset_dt = _normalize_datetime(reset_dt)
        return (
            f"reset_date:{reset_dt.date().isoformat()}",
            effective_reset_at or _format_datetime(reset_dt),
            _format_datetime(reset_dt - timedelta(seconds=_token_window_seconds(window_key, window_minutes))),
        )

    if window_key == MONTHLY_SAMPLE_WINDOW_KEY:
        month_start = observed_at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return f"observed_month:{observed_at.year}-{observed_at.month:02d}", None, _format_datetime(month_start)

    iso_year, iso_week, _ = observed_at.isocalendar()
    week_start = observed_at - timedelta(days=observed_at.weekday())
    return f"observed_week:{iso_year}-W{iso_week:02d}", None, _format_datetime(week_start)


def _token_window_seconds(window_key: str, window_minutes: int | None = None) -> int:
    if window_key == MONTHLY_SAMPLE_WINDOW_KEY and (
        window_minutes is None or window_minutes <= MONTHLY_WINDOW_MINUTES_THRESHOLD
    ):
        return WINDOW_RESET_SECONDS[MONTHLY_SAMPLE_WINDOW_KEY]
    if window_minutes is not None and window_minutes > 0:
        return window_minutes * 60
    return WINDOW_RESET_SECONDS.get(window_key, WINDOW_RESET_SECONDS["seven_day"])


def account_rate_limited_windows(
    account: dict[str, Any],
    usage: dict[str, Any] | None = None,
    sample_thresholds: dict[str, float] | None = None,
) -> list[str]:
    usage = usage if isinstance(usage, dict) else {}
    windows: list[str] = []
    for window_key in WINDOWS:
        values, account_values, window_kind = _estimate_window_values(account, usage, window_key)
        used_percent = _clamp_percent(_coerce_float(values.get("used_percent")))
        raw_spent = _coerce_float(values.get("raw_spent"))
        if _account_clears_zero_spend_limit(account_values, used_percent, raw_spent):
            continue
        threshold_percent = _limit_sample_threshold(_display_rate_limited_window_key(window_key, window_kind), sample_thresholds)
        if used_percent is not None and used_percent < threshold_percent:
            continue
        if _window_rate_limited(
            account,
            "seven_day" if window_kind == "monthly" else window_key,
            used_percent=used_percent,
            reset_at=_stringify(values.get("reset_at")),
            threshold_percent=threshold_percent,
        ):
            _append_rate_limited_window(windows, _display_rate_limited_window_key(window_key, window_kind))
    return windows


def _display_rate_limited_window_key(window_key: str, window_kind: str | None) -> str:
    if window_kind == "monthly":
        return MONTHLY_SAMPLE_WINDOW_KEY
    return window_key


def _append_rate_limited_window(windows: list[str], window_key: str) -> None:
    if window_key not in windows:
        windows.append(window_key)


async def _save_usage_limit_samples(
    samples: list[dict[str, Any]],
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> None:
    valid_samples = [
        sample
        for sample in samples
        if _usage_limit_sample_allowed(
            sample["window_key"], sample.get("plan_cohort"), sample.get("observed_limit"), default_ranges
        )
    ]
    if not valid_samples:
        await _prune_usage_limit_samples(default_ranges)
        return

    async with AsyncSessionLocal() as db:
        for sample in valid_samples:
            result = await db.execute(
                select(UsageLimitSample)
                .where(
                    UsageLimitSample.account_key == sample["account_key"],
                    UsageLimitSample.window_key == sample["window_key"],
                    UsageLimitSample.reset_key == sample["reset_key"],
                )
                .limit(1)
            )
            row = result.scalars().first()
            if row is None and sample.get("reset_at"):
                result = await db.execute(
                    select(UsageLimitSample)
                    .where(
                        UsageLimitSample.account_key == sample["account_key"],
                        UsageLimitSample.window_key == sample["window_key"],
                    )
                    .order_by(
                        UsageLimitSample.updated_at.desc(),
                        UsageLimitSample.created_at.desc(),
                        UsageLimitSample.id.desc(),
                    )
                )
                row = next(
                    (
                        existing
                        for existing in result.scalars().all()
                        if _same_sample_reset(existing.reset_at, sample.get("reset_at"))
                    ),
                    None,
                )
            if row is None:
                row = UsageLimitSample(
                    account_key=sample["account_key"],
                    window_key=sample["window_key"],
                    reset_key=sample["reset_key"],
                )
                db.add(row)
            row.email = sample.get("email")
            row.sub2api_account_id = sample.get("sub2api_account_id")
            row.plan_cohort = _normalize_plan_cohort(sample.get("plan_cohort"))
            row.reset_key = sample["reset_key"]
            row.reset_at = sample.get("reset_at")
            row.observed_limit = sample["observed_limit"]
            row.raw_spent = sample["raw_spent"]
            row.used_percent = sample["used_percent"]
            row.updated_at = utcnow()
        await db.commit()

    await _prune_usage_limit_samples(default_ranges)


async def _prune_usage_limit_samples(
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> None:
    async with AsyncSessionLocal() as db:
        for window_key in SAMPLE_WINDOWS:
            result = await db.execute(
                select(UsageLimitSample)
                .where(UsageLimitSample.window_key == window_key)
                .order_by(UsageLimitSample.plan_cohort, UsageLimitSample.observed_limit, UsageLimitSample.updated_at)
            )
            rows = list(result.scalars().all())
            rows_by_cohort: dict[str, list[UsageLimitSample]] = defaultdict(list)
            for row in rows:
                if not _usage_limit_sample_allowed(
                    row.window_key, row.plan_cohort, row.observed_limit, default_ranges
                ):
                    await db.delete(row)
                    continue
                rows_by_cohort[_normalize_plan_cohort(row.plan_cohort)].append(row)
            for cohort_rows in rows_by_cohort.values():
                if len(cohort_rows) <= LIMIT_SAMPLE_TARGET:
                    continue
                start = (len(cohort_rows) - LIMIT_SAMPLE_TARGET) // 2
                keep_ids = {row.id for row in cohort_rows[start : start + LIMIT_SAMPLE_TARGET]}
                delete_ids = [row.id for row in cohort_rows if row.id not in keep_ids]
                if delete_ids:
                    await db.execute(delete(UsageLimitSample).where(UsageLimitSample.id.in_(delete_ids)))
        await db.commit()


async def _load_usage_limit_calibrations(
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    async with AsyncSessionLocal() as db:
        query = select(UsageLimitSample).order_by(
            UsageLimitSample.window_key,
            UsageLimitSample.plan_cohort,
            UsageLimitSample.observed_limit,
        )
        result = await db.execute(query)
        rows = list(result.scalars().all())

    values_by_window: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        value = _coerce_float(row.observed_limit)
        if _usage_limit_sample_allowed(row.window_key, row.plan_cohort, value, default_ranges):
            values_by_window[row.window_key][_normalize_plan_cohort(row.plan_cohort)].append(value)

    calibrations: dict[str, dict[str, dict[str, Any]]] = {}
    for window_key in SAMPLE_WINDOWS:
        cohort_values = values_by_window.get(window_key, {})
        calibrations[window_key] = {
            cohort: _limit_calibration(window_key, values, cohort, default_ranges)
            for cohort, values in cohort_values.items()
        }
        for cohort in (*_default_sample_plan_cohorts(window_key, default_ranges), PLAN_COHORT_UNKNOWN):
            if cohort not in calibrations[window_key]:
                calibrations[window_key][cohort] = _limit_calibration(window_key, [], cohort, default_ranges)
    return calibrations


def _resolve_limit_calibration(
    window_key: str,
    calibrations: dict[str, dict[str, dict[str, Any]]],
    plan_cohort: str,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_plan_cohort(plan_cohort)
    by_window = calibrations.get(window_key) or {}
    if normalized == PLAN_COHORT_UNKNOWN:
        return by_window.get(PLAN_COHORT_UNKNOWN) or _limit_calibration(
            window_key, [], PLAN_COHORT_UNKNOWN, default_ranges
        )
    return by_window.get(normalized) or _limit_calibration(window_key, [], normalized, default_ranges)


def _limit_calibration(
    window_key: str,
    values: list[float],
    plan_cohort: str = PLAN_COHORT_UNKNOWN,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, Any]:
    sample_count = len(values)
    if sample_count >= LIMIT_SAMPLE_STATISTICS_MIN_COUNT:
        mean = sum(values) / sample_count
        variance = sum((value - mean) ** 2 for value in values) / sample_count
        sigma = math.sqrt(max(variance, 0.0))
        lower = max(mean - 3 * sigma, 0.0)
        upper = max(mean + 3 * sigma, lower)
        return {
            "source": "sigma",
            "sample_count": sample_count,
            "lower": lower,
            "upper": upper,
            "mean": mean,
            "sigma": sigma,
            "plan_cohort": _normalize_plan_cohort(plan_cohort),
            "plan_label": _plan_cohort_label(plan_cohort),
        }

    lower, upper = _default_limit_bounds(window_key, plan_cohort, default_ranges)
    return {
        "source": "default",
        "sample_count": sample_count,
        "lower": lower,
        "upper": upper,
        "mean": None,
        "sigma": None,
        "plan_cohort": _normalize_plan_cohort(plan_cohort),
        "plan_label": _plan_cohort_label(plan_cohort),
    }


def _monthly_limit_calibration(
    plan_cohort: str = PLAN_COHORT_UNKNOWN,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, Any]:
    return _limit_calibration(MONTHLY_SAMPLE_WINDOW_KEY, [], plan_cohort, default_ranges)


def _usage_limit_sample_updates(
    accounts: list[dict[str, Any]],
    sub2api: Sub2ApiClient,
    usage_by_account_id: dict[str, dict[str, Any]],
    sample_thresholds: dict[str, float] | None = None,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> list[dict[str, Any]]:
    samples: dict[tuple[str, str, str], dict[str, Any]] = {}
    for account in accounts:
        account_id = sub2api.account_id(account)
        if not account_id:
            continue
        usage = usage_by_account_id.get(account_id)
        if not usage:
            continue
        email = sub2api.account_email(account) or _stringify(account.get("name")) or account_id
        account_key = _account_state_key(account_id, email)
        plan_cohort = _plan_cohort_from_account(account)
        for window_key in WINDOWS:
            if window_key == "seven_day":
                usage_window, account_window, window_kind = _estimate_window_values(account, usage, window_key)
            else:
                account_window = _window_values(account, {}, window_key)
                usage_window = _window_values(account, usage, window_key)
                window_kind = _window_kind(window_key, usage_window, account_window)
            account_used_percent = _clamp_percent(_coerce_float(account_window.get("used_percent")))
            if window_kind != window_key:
                if window_kind == "monthly":
                    _add_usage_limit_sample(
                        samples,
                        account=account,
                        account_key=account_key,
                        account_id=account_id,
                        email=email,
                        plan_cohort=plan_cohort,
                        sample_window_key=MONTHLY_SAMPLE_WINDOW_KEY,
                        rate_limit_window_key="seven_day",
                        values=usage_window,
                        account_used_percent=account_used_percent,
                        threshold_percent=_limit_sample_threshold(MONTHLY_SAMPLE_WINDOW_KEY, sample_thresholds),
                        default_ranges=default_ranges,
                    )
                continue
            _add_usage_limit_sample(
                samples,
                account=account,
                account_key=account_key,
                account_id=account_id,
                email=email,
                plan_cohort=plan_cohort,
                sample_window_key=window_key,
                rate_limit_window_key=window_key,
                values=usage_window,
                account_used_percent=account_used_percent,
                threshold_percent=_limit_sample_threshold(window_key, sample_thresholds),
                default_ranges=default_ranges,
            )
    return list(samples.values())


def _add_usage_limit_sample(
    samples: dict[tuple[str, str, str], dict[str, Any]],
    *,
    account: dict[str, Any],
    account_key: str,
    account_id: str,
    email: str,
    plan_cohort: str,
    sample_window_key: str,
    rate_limit_window_key: str,
    values: dict[str, Any],
    account_used_percent: float | None,
    threshold_percent: float,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> None:
    if not _usage_limit_sample_allowed(
        sample_window_key,
        plan_cohort,
        _default_limit_bounds(sample_window_key, plan_cohort, default_ranges)[0],
        default_ranges,
    ):
        return
    account_contradicts_limit = not values["window_data"] and account_used_percent is not None and account_used_percent < threshold_percent
    raw_spent = _coerce_float(values.get("raw_spent"))
    used_percent = _clamp_percent(_coerce_float(values.get("used_percent")))
    if raw_spent is None or used_percent is None:
        return
    reset_at = _stringify(values.get("reset_at"))
    reset_key = _sample_reset_key(reset_at)
    if not _window_limit_reached(
        account,
        rate_limit_window_key,
        raw_spent,
        used_percent,
        reset_at,
        threshold_percent,
    ):
        return
    if account_contradicts_limit:
        return
    observed_limit = _limit_from_usage(raw_spent, used_percent)
    if observed_limit is None:
        return
    if not _usage_limit_sample_allowed(sample_window_key, plan_cohort, observed_limit, default_ranges):
        return
    samples[(account_key, sample_window_key, reset_key)] = {
        "account_key": account_key,
        "email": email,
        "sub2api_account_id": account_id,
        "plan_cohort": plan_cohort,
        "window_key": sample_window_key,
        "reset_key": reset_key,
        "reset_at": reset_at,
        "observed_limit": observed_limit,
        "raw_spent": raw_spent,
        "used_percent": used_percent,
    }


def _usage_limit_samples(
    accounts: list[dict[str, Any]],
    sub2api: Sub2ApiClient,
    usage_by_account_id: dict[str, dict[str, Any]],
    sample_thresholds: dict[str, float] | None = None,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> list[dict[str, Any]]:
    return _usage_limit_sample_updates(accounts, sub2api, usage_by_account_id, sample_thresholds, default_ranges)


def _limit_sample_threshold(window_key: str, sample_thresholds: dict[str, float] | None = None) -> float:
    threshold_key = "seven_day" if window_key == MONTHLY_SAMPLE_WINDOW_KEY else window_key
    configured = _clamp_percent(_coerce_float((sample_thresholds or {}).get(threshold_key)))
    if configured is not None and configured > 0:
        return configured
    return LIMIT_SAMPLE_FULL_PERCENT


def _sample_reset_key(reset_at: str | None) -> str:
    return f"reset:{reset_at}" if reset_at else "reset:unknown"


def _same_sample_reset(left: str | None, right: str | None) -> bool:
    left_text = _stringify(left)
    right_text = _stringify(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    left_epoch = _reset_epoch(left_text)
    right_epoch = _reset_epoch(right_text)
    if left_epoch is None or right_epoch is None:
        return False
    return abs(left_epoch - right_epoch) <= LIMIT_SAMPLE_RESET_TOLERANCE_SECONDS


def _reset_epoch(value: str) -> float | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return _normalize_datetime(parsed).timestamp()


def _parse_datetime(value: str | None) -> datetime | None:
    text = _stringify(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _normalize_datetime(value).isoformat().replace("+00:00", "Z")


def _window_limit_reached(
    account: dict[str, Any],
    window_key: str,
    raw_spent: float,
    used_percent: float,
    reset_at: str | None,
    threshold_percent: float = LIMIT_SAMPLE_FULL_PERCENT,
) -> bool:
    if raw_spent <= 0 or used_percent <= 0:
        return False
    if reset_at and not _reset_at_is_future(reset_at):
        return False
    normalized_used_percent = _clamp_percent(used_percent)
    normalized_threshold = _clamp_percent(_coerce_float(threshold_percent))
    if normalized_threshold is None or normalized_threshold <= 0:
        normalized_threshold = LIMIT_SAMPLE_FULL_PERCENT
    return normalized_used_percent is not None and normalized_used_percent >= normalized_threshold


def _window_rate_limited(
    account: dict[str, Any],
    window_key: str,
    used_percent: float | None = None,
    reset_at: str | None = None,
    threshold_percent: float = LIMIT_SAMPLE_FULL_PERCENT,
) -> bool:
    if reset_at and not _reset_at_is_future(reset_at):
        return False

    normalized_used_percent = _clamp_percent(used_percent)
    if normalized_used_percent is not None:
        normalized_threshold = _clamp_percent(_coerce_float(threshold_percent))
        if normalized_threshold is None or normalized_threshold <= 0:
            normalized_threshold = LIMIT_SAMPLE_FULL_PERCENT
        return normalized_used_percent >= normalized_threshold

    account_rate_limited_at = _stringify(
        _first_present(
            _path_get(account, ("rate_limited_at",)),
            _path_get(account, ("extra", "rate_limited_at")),
        )
    )
    if not account_rate_limited_at:
        return False

    account_reset_at = _stringify(
        _first_present(
            _path_get(account, ("rate_limit_reset_at",)),
            _path_get(account, ("extra", "rate_limit_reset_at")),
        )
    )
    if account_reset_at and not _reset_at_is_future(account_reset_at):
        return False
    if account_reset_at and reset_at:
        return _same_sample_reset(account_reset_at, reset_at)

    return window_key == "seven_day" and bool(account_reset_at)


def _reset_at_is_future(value: str | None) -> bool:
    parsed = _parse_datetime(value)
    if parsed is None:
        return True
    return parsed > datetime.now(timezone.utc)


def _usage_estimate_enabled(
    account: dict[str, Any],
    sub2api: Sub2ApiClient,
    preferences: dict[str, bool],
) -> bool:
    account_id = sub2api.account_id(account)
    if account_id and f"id:{account_id}" in preferences:
        return preferences[f"id:{account_id}"]
    email = sub2api.account_email(account)
    if email and f"email:{email.lower()}" in preferences:
        return preferences[f"email:{email.lower()}"]
    return not sub2api.is_deactive_account(account)


def _account_estimate(
    account: dict[str, Any],
    group_map: dict[str, str],
    sub2api: Sub2ApiClient,
    usage_estimate_enabled: bool,
    usage: dict[str, Any],
    usage_error: str | None,
    usage_states: dict[tuple[str, str], dict[str, Any]],
    limit_calibrations: dict[str, dict[str, Any]],
    token_history: dict[str, list[dict[str, Any]]],
    sample_thresholds: dict[str, float] | None = None,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, Any]:
    account_id = sub2api.account_id(account)
    email = sub2api.account_email(account) or _stringify(account.get("name")) or account_id or "unknown"
    account_name = sub2api.account_name(account) or email
    account_key = _account_state_key(account_id, email)
    plan_cohort = _plan_cohort_from_account(account)
    groups = _account_groups(account, group_map)
    error = sub2api.is_error_account(account)
    five_hour = _window_estimate(
        account,
        usage,
        "five_hour",
        account_key,
        account_id,
        email,
        plan_cohort,
        usage_states,
        limit_calibrations,
        sample_thresholds,
        default_ranges,
    )
    seven_day = _window_estimate(
        account,
        usage,
        "seven_day",
        account_key,
        account_id,
        email,
        plan_cohort,
        usage_states,
        limit_calibrations,
        sample_thresholds,
        default_ranges,
    )
    rate_limited_windows: list[str] = []
    for window_key, window in (("five_hour", five_hour), ("seven_day", seven_day)):
        if window.get("rate_limited"):
            _append_rate_limited_window(
                rate_limited_windows,
                _display_rate_limited_window_key(window_key, _stringify(window.get("window_kind"))),
            )
    return {
        "email": email,
        "account_name": account_name,
        "sub2api_account_id": account_id,
        "platform": sub2api.account_platform(account),
        "account_type": sub2api.account_type(account),
        "subscription_plan": _account_subscription_plan(account, plan_cohort),
        "subscription_type": plan_cohort,
        "subscription_label": _plan_cohort_label(plan_cohort),
        "subscription_billing_period": _first_string_from_paths(
            account,
            ("credentials", "subscription_billing_period"),
            ("credentials", "subscriptionBillingPeriod"),
            ("subscription_billing_period",),
            ("subscriptionBillingPeriod",),
            ("entitlement", "billing_period"),
            ("entitlement", "billingPeriod"),
            ("account", "entitlement", "billing_period"),
            ("account", "entitlement", "billingPeriod"),
            ("last_active_subscription", "billing_period"),
            ("last_active_subscription", "billingPeriod"),
            ("last_active_subscription", "interval"),
            ("last_active_subscription", "plan_interval"),
            ("last_active_subscription", "planInterval"),
            ("extra", "subscription_billing_period"),
        ),
        "has_active_subscription": _account_has_active_subscription(account),
        "status": sub2api.account_status(account),
        "schedulable": sub2api.account_schedulable(account),
        "deactive": sub2api.is_deactive_account(account),
        "error": error,
        "rate_limited": bool(rate_limited_windows),
        "rate_limited_windows": rate_limited_windows,
        "usage_estimate_enabled": usage_estimate_enabled,
        "rate_multiplier": _coerce_float(account.get("rate_multiplier")) or 1,
        "groups": groups,
        "usage_error": usage_error or None,
        "five_hour": five_hour,
        "seven_day": seven_day,
        "seven_day_token_history": _account_token_history(token_history.get(account_key, [])),
    }


def _account_subscription_plan(account: dict[str, Any], plan_cohort: str) -> str | None:
    plan = _first_string_from_paths(account, *PLAN_COHORT_PATHS)
    if plan:
        return plan
    return None if plan_cohort == PLAN_COHORT_UNKNOWN else plan_cohort


def _account_has_active_subscription(account: dict[str, Any]) -> bool | None:
    for path in (
        ("credentials", "has_active_subscription"),
        ("credentials", "hasActiveSubscription"),
        ("has_active_subscription",),
        ("hasActiveSubscription",),
        ("entitlement", "has_active_subscription"),
        ("entitlement", "hasActiveSubscription"),
        ("account", "entitlement", "has_active_subscription"),
        ("account", "entitlement", "hasActiveSubscription"),
        ("last_active_subscription", "has_active_subscription"),
        ("last_active_subscription", "hasActiveSubscription"),
    ):
        value = _bool_or_none(_path_get(account, path))
        if value is not None:
            return value
    return _subscription_status_is_active(
        _first_string_from_paths(account, ("last_active_subscription", "status"), ("subscription_status",), ("subscriptionStatus",))
    )


def _first_string_from_paths(data: dict[str, Any], *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        text = _stringify(_path_get(data, path))
        if text is not None:
            return text
    return None


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "active"}:
            return True
        if text in {"0", "false", "no", "inactive"}:
            return False
    return None


def _subscription_status_is_active(value: str | None) -> bool | None:
    if not value:
        return None
    text = value.strip().lower()
    if text in {"active", "trialing", "paid", "valid"}:
        return True
    if text in {"inactive", "canceled", "cancelled", "expired", "past_due", "unpaid"}:
        return False
    return None


def _account_state_key(account_id: str | None, email: str | None) -> str:
    if account_id:
        return f"id:{account_id}"
    if email:
        return f"email:{email.lower()}"
    return "unknown"


def _account_token_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    windows = [
        {
            "window_key": row["window_key"],
            "window_reset_key": row["window_reset_key"],
            "window_start_at": row.get("window_start_at"),
            "reset_at": row.get("reset_at"),
            "spent": float(row.get("spent") or 0),
            "tokens": int(row.get("tokens") or 0),
            "estimated_limit": _coerce_float(row.get("estimated_limit")),
            "first_observed_at": row.get("first_observed_at"),
            "last_observed_at": row.get("last_observed_at"),
        }
        for row in rows
    ]
    total_tokens = sum(window["tokens"] for window in windows)
    total_spent = sum(window["spent"] for window in windows)
    total_estimated_limit = sum(window["estimated_limit"] or 0 for window in windows)
    return {
        "total_spent": total_spent,
        "total_tokens": total_tokens,
        "total_estimated_limit": total_estimated_limit,
        "window_count": len(windows),
        "windows": windows,
    }


def _historical_window_limit(samples: list[dict[str, Any]], reset_at: str | None) -> float | None:
    if not reset_at:
        return None
    for sample in samples:
        if _same_sample_reset(_stringify(sample.get("reset_at")), reset_at):
            return _coerce_float(sample.get("observed_limit"))
    return None


def _usage_window_data(usage: dict[str, Any], window_key: str) -> tuple[dict[str, Any], bool]:
    direct_window: dict[str, Any] = {}
    for key in WINDOW_USAGE_KEYS.get(window_key, (window_key,)):
        window = usage.get(key)
        if isinstance(window, dict) and window:
            direct_window = window
            break
    if window_key == "seven_day":
        for key in MONTHLY_USAGE_KEYS:
            window = usage.get(key)
            if isinstance(window, dict) and window:
                if not direct_window or _window_data_minutes(direct_window) == 0:
                    return window, True
                break
    return direct_window, False


def _window_data_minutes(window_data: dict[str, Any]) -> int | None:
    return _coerce_int(
        _first_present(
            _path_get(window_data, ("window_minutes",)),
            _path_get(window_data, ("windowMinutes",)),
            _path_get(window_data, ("duration_minutes",)),
            _path_get(window_data, ("durationMinutes",)),
        )
    )


def _usage_window_reset_at(window_data: dict[str, Any]) -> str | None:
    return _stringify(
        _first_present(
            _path_get(window_data, ("resets_at",)),
            _path_get(window_data, ("reset_at",)),
            _path_get(window_data, ("resetAt",)),
        )
    )


def _usage_window_remaining_seconds(window_data: dict[str, Any]) -> int | None:
    return _coerce_int(
        _first_present(
            _path_get(window_data, ("remaining_seconds",)),
            _path_get(window_data, ("remainingSeconds",)),
            _path_get(window_data, ("reset_after_seconds",)),
            _path_get(window_data, ("resetAfterSeconds",)),
        )
    )


def _window_values(account: dict[str, Any], usage: dict[str, Any], window_key: str) -> dict[str, Any]:
    window_data, monthly_alias = _usage_window_data(usage, window_key)
    stats = _first_dict(window_data, ("window_stats",), ("windowStats",), ("stats",), ("usage",), ("usage_stats",), ("usageStats",))
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    prefix = WINDOWS[window_key]["extra_prefix"]

    used_percent = _used_percent_value(window_data, extra, account, prefix)
    raw_limit, limit_source = _limit_value(stats, window_data)
    raw_remaining, remaining_source = _remaining_value(stats, window_data)
    raw_used, used_source = _used_amount_value(stats, window_data)
    raw_spent, spend_source = _spent_value(stats, window_data)
    if (raw_spent is None or raw_spent <= EPSILON) and raw_used is not None and raw_used > EPSILON:
        raw_spent = raw_used
        spend_source = used_source
    if (raw_spent is None or raw_spent <= EPSILON) and raw_limit is not None and raw_remaining is not None:
        raw_spent = max(raw_limit - raw_remaining, 0.0)
        spend_source = f"{limit_source or 'limit'}-{remaining_source or 'remaining'}"
    reset_at = _stringify(
        _first_present(
            _usage_window_reset_at(window_data),
            _path_get(extra, (f"{prefix}_reset_at",)),
            _path_get(account, (f"{prefix}_reset_at",)),
        )
    )
    remaining_seconds = _coerce_int(
        _first_present(
            _usage_window_remaining_seconds(window_data),
            _path_get(extra, (f"{prefix}_reset_after_seconds",)),
            _path_get(account, (f"{prefix}_reset_after_seconds",)),
        )
    )
    window_minutes = _coerce_int(
        _first_present(
            _path_get(window_data, ("window_minutes",)),
            _path_get(window_data, ("windowMinutes",)),
            _path_get(window_data, ("duration_minutes",)),
            _path_get(window_data, ("durationMinutes",)),
            _path_get(extra, (f"{prefix}_window_minutes",)),
            _path_get(account, (f"{prefix}_window_minutes",)),
        )
    )
    if monthly_alias and window_minutes is None:
        window_minutes = MONTHLY_WINDOW_MINUTES_THRESHOLD
    return {
        "window_data": window_data,
        "monthly_alias": monthly_alias,
        "stats": stats,
        "extra": extra,
        "used_percent": used_percent,
        "raw_spent": raw_spent,
        "raw_limit": raw_limit,
        "raw_remaining": raw_remaining,
        "spend_source": spend_source,
        "reset_at": reset_at,
        "remaining_seconds": remaining_seconds,
        "window_minutes": window_minutes,
    }


def _estimate_window_values(
    account: dict[str, Any],
    usage: dict[str, Any],
    window_key: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    values = _window_values(account, usage, window_key)
    account_values = _window_values(account, {}, window_key)
    window_kind = _window_kind(window_key, values, account_values)
    can_infer_monthly = _plan_cohort_from_account(account) == "team"
    if window_key == "seven_day" and window_kind == "seven_day" and can_infer_monthly and (
        _account_has_no_independent_five_hour(account, usage)
        or (_window_looks_monthly_quota(values) and not _account_has_independent_five_hour_quota(account, usage))
    ):
        return _monthly_values_with_fallback_spent(account, usage, values), account_values, "monthly"
    if window_key == "seven_day" and window_kind == "monthly":
        return _monthly_values_with_fallback_spent(account, usage, values), account_values, window_kind
    if window_key != "five_hour" or (window_kind != "none" and _window_has_quota_signal(values, account_values)):
        return values, account_values, window_kind

    monthly_values = _window_values(account, usage, "seven_day")
    monthly_account_values = _window_values(account, {}, "seven_day")
    if (
        _window_kind("seven_day", monthly_values, monthly_account_values) == "monthly"
        or (
            can_infer_monthly
            and (
                (_window_looks_monthly_quota(monthly_values) and not _account_has_independent_five_hour_quota(account, usage))
                or (
                    _account_has_no_independent_five_hour(account, usage)
                    and _window_has_quota_signal(monthly_values, monthly_account_values)
                )
            )
        )
    ):
        return _monthly_values_with_fallback_spent(account, usage, monthly_values), monthly_account_values, "monthly"
    return values, account_values, window_kind


def _account_has_no_independent_five_hour(account: dict[str, Any], usage: dict[str, Any]) -> bool:
    values = _window_values(account, usage, "five_hour")
    account_values = _window_values(account, {}, "five_hour")
    return _window_kind("five_hour", values, account_values) == "none" and _window_has_quota_signal(values, account_values)


def _account_has_independent_five_hour_quota(account: dict[str, Any], usage: dict[str, Any]) -> bool:
    values = _window_values(account, usage, "five_hour")
    account_values = _window_values(account, {}, "five_hour")
    return _window_kind("five_hour", values, account_values) != "none" and _window_has_quota_signal(values, account_values)


def _monthly_values_with_fallback_spent(
    account: dict[str, Any],
    usage: dict[str, Any],
    monthly_values: dict[str, Any],
) -> dict[str, Any]:
    monthly_raw_spent = _coerce_float(monthly_values.get("raw_spent"))
    if monthly_raw_spent is not None and monthly_raw_spent > EPSILON:
        return monthly_values

    five_hour_values = _window_values(account, usage, "five_hour")
    five_hour_account_values = _window_values(account, {}, "five_hour")
    if _window_kind("five_hour", five_hour_values, five_hour_account_values) != "none":
        return monthly_values
    five_hour_spent = _coerce_float(five_hour_values.get("raw_spent"))
    if five_hour_spent is None or five_hour_spent <= EPSILON:
        return monthly_values

    values = dict(monthly_values)
    values["raw_spent"] = five_hour_spent
    values["spend_source"] = f"five_hour:{five_hour_values.get('spend_source') or 'raw_spent'}"
    return values


def _window_looks_monthly_quota(values: dict[str, Any]) -> bool:
    window_minutes = _coerce_int(values.get("window_minutes"))
    if window_minutes is not None and window_minutes >= MONTHLY_WINDOW_MINUTES_THRESHOLD:
        return True
    raw_limit = _coerce_float(values.get("raw_limit"))
    monthly_lower, _ = _default_limit_bounds(MONTHLY_SAMPLE_WINDOW_KEY, "team")
    if raw_limit is not None and raw_limit >= monthly_lower:
        return True
    raw_remaining = _coerce_float(values.get("raw_remaining"))
    _, seven_day_upper = _default_limit_bounds("seven_day", "team")
    return raw_remaining is not None and raw_remaining > seven_day_upper


def _window_has_quota_signal(*windows: dict[str, Any]) -> bool:
    for window in windows:
        if window.get("window_data"):
            return True
        if _coerce_float(window.get("used_percent")) is not None:
            return True
        if _coerce_float(window.get("raw_spent")) is not None:
            return True
        if _stringify(window.get("reset_at")):
            return True
        if _coerce_int(window.get("remaining_seconds")) is not None:
            return True
        if _coerce_int(window.get("window_minutes")) is not None:
            return True
    return False


def _window_estimate(
    account: dict[str, Any],
    usage: dict[str, Any],
    window_key: str,
    account_key: str,
    account_id: str | None,
    email: str | None,
    plan_cohort: str,
    usage_states: dict[tuple[str, str], dict[str, Any]],
    limit_calibrations: dict[str, dict[str, dict[str, Any]]],
    sample_thresholds: dict[str, float] | None = None,
    default_ranges: dict[str, dict[str, dict[str, float]]] | None = None,
) -> dict[str, Any]:
    values, account_values, window_kind = _estimate_window_values(account, usage, window_key)
    window_data = values["window_data"]
    stats = values["stats"]
    used_percent = values["used_percent"]
    raw_spent = values["raw_spent"]
    raw_limit = values.get("raw_limit")
    raw_remaining = values.get("raw_remaining")
    spend_source = values["spend_source"]
    reset_at = values["reset_at"]
    remaining_seconds = values["remaining_seconds"]
    account_used_percent = _clamp_percent(_coerce_float(account_values.get("used_percent")))
    account_reset_at = _stringify(account_values.get("reset_at"))
    account_remaining_seconds = _coerce_int(account_values.get("remaining_seconds"))
    window_minutes = _window_minutes(values, account_values)
    window_label = _window_label(window_kind)
    if window_kind == "none":
        return _empty_window_estimate(window_kind, window_minutes, window_label, reset_at or account_reset_at, remaining_seconds)
    usage_used_percent = _clamp_percent(used_percent)
    source = "usage" if window_data else "account"
    if _usage_window_clears_stale_spend(account_values, usage_used_percent, raw_spent, remaining_seconds, reset_at):
        raw_spent = 0.0
        used_percent = account_used_percent if account_used_percent is not None else 0.0
    elif _account_clears_zero_spend_limit(account_values, usage_used_percent, raw_spent):
        raw_spent = 0.0
        used_percent = account_used_percent
        if account_reset_at:
            reset_at = account_reset_at
        if account_remaining_seconds is not None:
            remaining_seconds = account_remaining_seconds
        source = "account"
    elif not window_data and account_used_percent is not None and account_used_percent < LIMIT_SAMPLE_FULL_PERCENT:
        if usage_used_percent is not None and usage_used_percent >= LIMIT_SAMPLE_FULL_PERCENT:
            raw_spent = None
            spend_source = None
            stats = {}
        used_percent = account_used_percent
        if account_reset_at:
            reset_at = account_reset_at
        if account_remaining_seconds is not None:
            remaining_seconds = account_remaining_seconds
        source = "account"
    if not window_data:
        cache_window_key = "seven_day" if window_kind == "monthly" and window_key == "five_hour" else window_key
        cached_state = usage_states.get((account_key, cache_window_key))
        if cached_state:
            cached_raw_spent = _coerce_float(cached_state.get("last_raw_spent"))
            cached_spend_is_stale = _usage_window_clears_stale_spend(
                account_values,
                used_percent,
                cached_raw_spent,
                remaining_seconds,
                reset_at,
            )
            if raw_spent is None and cached_raw_spent is not None and not cached_spend_is_stale:
                raw_spent = cached_raw_spent
                source = "cached" if used_percent is None else "account"
            if used_percent is None:
                used_percent = _cached_used_percent(cached_state, cached_raw_spent)
                if used_percent is not None:
                    source = "cached"
            if reset_at is None:
                reset_at = _stringify(cached_state.get("last_reset_at"))
            if remaining_seconds is None:
                remaining_seconds = _coerce_int(cached_state.get("last_remaining_seconds"))
    normalized_used_percent = _clamp_percent(used_percent)
    if _zero_spend_is_missing(window_kind, raw_spent, normalized_used_percent):
        raw_spent = None
        spend_source = None
    display_used_percent = _clamp_percent(normalized_used_percent if normalized_used_percent is not None else account_used_percent)
    spent = raw_spent
    baseline_spent = None
    estimate_spent = raw_spent
    estimate_basis = "official_window" if raw_spent is not None else None
    calibration_window_key = MONTHLY_SAMPLE_WINDOW_KEY if window_kind == "monthly" else window_key
    calibration = _resolve_limit_calibration(
        calibration_window_key, limit_calibrations, plan_cohort, default_ranges
    )
    usage_estimated_limit = _limit_from_usage(raw_spent, normalized_used_percent)
    raw_limit_value = _coerce_float(raw_limit)
    raw_estimated_limit = usage_estimated_limit or raw_limit_value
    used_zero_usage_fallback = False
    if (
        (raw_estimated_limit is None or raw_estimated_limit <= 0)
        and (estimate_spent is None or estimate_spent <= 0)
    ):
        raw_estimated_limit = _zero_usage_limit_estimate(calibration)
        used_zero_usage_fallback = raw_estimated_limit is not None
    estimated_limit = (
        _trusted_full_window_limit(raw_estimated_limit, normalized_used_percent)
        or _calibrated_limit(
            raw_estimated_limit,
            calibration,
            spent_floor=estimate_spent,
        )
    )
    if estimated_limit is not None and raw_estimated_limit is not None and abs(estimated_limit - raw_estimated_limit) > EPSILON:
        estimate_basis = "official_window_clipped"
    if estimated_limit is not None and estimate_spent is None:
        if normalized_used_percent is not None and normalized_used_percent > EPSILON:
            estimate_basis = "percent_only_missing_usage"
        else:
            estimate_spent = 0.0
            estimate_basis = "sample_limit_zero_usage"
    elif estimated_limit is not None and used_zero_usage_fallback:
        estimate_basis = "sample_limit_zero_usage"
    rate_limited = _window_rate_limited(
        account,
        "seven_day" if window_kind == "monthly" else window_key,
        used_percent=normalized_used_percent if normalized_used_percent is not None else account_used_percent,
        reset_at=reset_at or account_reset_at,
        threshold_percent=_limit_sample_threshold(_display_rate_limited_window_key(window_key, window_kind), sample_thresholds),
    )
    remaining = None
    remaining_percent = None
    if estimated_limit is not None:
        parsed_raw_remaining = _coerce_float(raw_remaining)
        if (
            usage_estimated_limit is None
            and parsed_raw_remaining is not None
            and raw_estimated_limit is not None
            and abs(estimated_limit - raw_estimated_limit) <= EPSILON
        ):
            remaining = max(parsed_raw_remaining, 0.0)
            remaining_percent = (remaining / estimated_limit * 100) if estimated_limit > 0 else 0
        elif normalized_used_percent is not None:
            remaining_percent = max(100 - normalized_used_percent, 0)
            remaining = estimated_limit * (remaining_percent / 100)
        elif estimate_spent is not None:
            remaining = max(estimated_limit - estimate_spent, 0)
            remaining_percent = (remaining / estimated_limit * 100) if estimated_limit > 0 else 0

    return {
        "used_percent": display_used_percent,
        "spent": spent,
        "raw_spent": raw_spent,
        "baseline_spent": baseline_spent,
        "estimate_spent": estimate_spent,
        "estimate_basis": estimate_basis,
        "spend_source": spend_source,
        "estimated_limit": estimated_limit,
        "remaining": remaining,
        "remaining_percent": _clamp_percent(remaining_percent),
        "reset_at": reset_at,
        "remaining_seconds": remaining_seconds,
        "requests": _coerce_int(_path_get(stats, ("requests",))),
        "tokens": _tokens_value(stats, window_data),
        "estimable": estimated_limit is not None,
        "rate_limited": rate_limited,
        "source": source,
        "window_kind": window_kind,
        "window_minutes": window_minutes,
        "window_label": window_label,
    }


def _window_minutes(values: dict[str, Any], fallback_values: dict[str, Any] | None = None) -> int | None:
    minutes = _coerce_int(values.get("window_minutes"))
    if minutes is not None:
        return minutes
    if fallback_values is None:
        return None
    return _coerce_int(fallback_values.get("window_minutes"))


def _window_kind(window_key: str, values: dict[str, Any], fallback_values: dict[str, Any] | None = None) -> str:
    minutes = _window_minutes(values, fallback_values)
    if window_key == "five_hour" and minutes == 0:
        return "none"
    if window_key == "seven_day" and (
        bool(values.get("monthly_alias")) or bool(fallback_values and fallback_values.get("monthly_alias"))
    ):
        return "monthly"
    if window_key == "seven_day" and minutes is not None and minutes >= MONTHLY_WINDOW_MINUTES_THRESHOLD:
        return "monthly"
    return window_key


def _window_label(window_kind: str) -> str:
    return {
        "five_hour": "5h",
        "seven_day": "7d",
        "monthly": "月",
        "none": "无 5h",
    }.get(window_kind, window_kind)


def _empty_window_estimate(
    window_kind: str,
    window_minutes: int | None,
    window_label: str,
    reset_at: str | None,
    remaining_seconds: int | None,
) -> dict[str, Any]:
    return {
        "used_percent": None,
        "spent": None,
        "raw_spent": None,
        "baseline_spent": None,
        "estimate_spent": None,
        "estimate_basis": None,
        "spend_source": None,
        "estimated_limit": None,
        "remaining": None,
        "remaining_percent": None,
        "reset_at": reset_at,
        "remaining_seconds": remaining_seconds,
        "requests": None,
        "tokens": None,
        "estimable": False,
        "rate_limited": False,
        "source": "not_applicable",
        "window_kind": window_kind,
        "window_minutes": window_minutes,
        "window_label": window_label,
    }


def _cached_used_percent(cached_state: dict[str, Any], cached_raw_spent: float | None) -> float | None:
    cached_used_percent = _coerce_float(cached_state.get("last_used_percent"))
    if cached_raw_spent is not None and cached_raw_spent <= 0 and cached_used_percent is not None and cached_used_percent > 0:
        return None
    return cached_used_percent


def _used_percent_value(
    window_data: dict[str, Any],
    extra: dict[str, Any],
    account: dict[str, Any],
    prefix: str,
) -> float | None:
    utilization = _coerce_float(_path_get(window_data, ("utilization",)))
    if utilization is not None:
        return utilization * 100 if 0 < utilization < 1 else utilization

    return _coerce_float(
        _first_present(
            _path_get(window_data, ("used_percent",)),
            _path_get(window_data, ("usedPercent",)),
            _path_get(window_data, ("usage_percent",)),
            _path_get(window_data, ("usagePercent",)),
            _path_get(window_data, ("percent",)),
            _path_get(extra, (f"{prefix}_used_percent",)),
            _path_get(account, (f"{prefix}_used_percent",)),
        )
    )


def _window_effective_spent(
    account_key: str,
    account_id: str | None,
    email: str | None,
    window_key: str,
    raw_spent: float | None,
    used_percent: float | None,
    reset_at: str | None,
    remaining_seconds: int | None,
    usage_states: dict[tuple[str, str], dict[str, Any]],
) -> tuple[float | None, float | None, bool]:
    state_key = (account_key, window_key)
    state = usage_states.get(state_key)
    if state is None:
        if raw_spent is None:
            return None, None, False
        state = {
            "account_key": account_key,
            "window_key": window_key,
        }
        usage_states[state_key] = state

    baseline_spent = _coerce_float(state.get("baseline_spent"))
    estimate_uses_spent_delta = bool(state.get("estimate_uses_spent_delta", False))
    last_raw_spent = _coerce_float(state.get("last_raw_spent"))
    last_used_percent = _coerce_float(state.get("last_used_percent"))
    last_reset_at = _stringify(state.get("last_reset_at"))
    last_remaining_seconds = _coerce_int(state.get("last_remaining_seconds"))

    if raw_spent is None:
        return None, baseline_spent, estimate_uses_spent_delta

    if estimate_uses_spent_delta and baseline_spent is None:
        estimate_uses_spent_delta = False
    if raw_spent + EPSILON < (baseline_spent or 0.0):
        baseline_spent = 0.0
        estimate_uses_spent_delta = False
    elif _window_reset_detected(
        window_key=window_key,
        current_reset_at=reset_at,
        last_reset_at=last_reset_at,
        current_remaining_seconds=remaining_seconds,
        last_remaining_seconds=last_remaining_seconds,
        current_used_percent=used_percent,
        last_used_percent=last_used_percent,
    ):
        if last_raw_spent is not None and raw_spent + EPSILON >= last_raw_spent:
            baseline_spent = last_raw_spent
            estimate_uses_spent_delta = baseline_spent > EPSILON
        else:
            baseline_spent = 0.0
            estimate_uses_spent_delta = False

    if estimate_uses_spent_delta and baseline_spent is not None:
        corrected_spent = max(raw_spent - baseline_spent, 0.0)
        if _delta_correction_looks_stale(window_key, raw_spent, corrected_spent, used_percent):
            baseline_spent = 0.0
            estimate_uses_spent_delta = False

    _update_state(
        state,
        email=email,
        account_id=account_id,
        raw_spent=raw_spent,
        used_percent=used_percent,
        reset_at=reset_at,
        remaining_seconds=remaining_seconds,
        baseline_spent=baseline_spent,
        estimate_uses_spent_delta=estimate_uses_spent_delta,
    )

    if estimate_uses_spent_delta and baseline_spent is not None:
        return max(raw_spent - baseline_spent, 0.0), baseline_spent, True
    return raw_spent, baseline_spent, False


def _window_reset_detected(
    window_key: str,
    current_reset_at: str | None,
    last_reset_at: str | None,
    current_remaining_seconds: int | None,
    last_remaining_seconds: int | None,
    current_used_percent: float | None,
    last_used_percent: float | None,
) -> bool:
    if _reset_at_change_indicates_reset(window_key, current_reset_at, last_reset_at):
        return True

    if current_remaining_seconds is not None and last_remaining_seconds is not None:
        threshold = 1_800 if window_key == "five_hour" else 21_600
        if current_remaining_seconds > last_remaining_seconds + threshold:
            return True

    if current_used_percent is not None and last_used_percent is not None:
        if last_used_percent >= 20 and current_used_percent <= 5:
            return True
        if last_used_percent - current_used_percent >= 30:
            return True

    return False


def _reset_at_change_indicates_reset(
    window_key: str,
    current_reset_at: str | None,
    last_reset_at: str | None,
) -> bool:
    if not current_reset_at or not last_reset_at:
        return False

    current_epoch = _reset_epoch(current_reset_at)
    last_epoch = _reset_epoch(last_reset_at)
    if current_epoch is None or last_epoch is None:
        return current_reset_at == last_reset_at

    window_seconds = WINDOW_RESET_SECONDS.get(window_key)
    if window_seconds is None:
        return False

    # Rolling windows can slide the reset time slightly between polls.
    return current_epoch - last_epoch >= window_seconds * RESET_AT_RESET_JUMP_FRACTION


def _delta_correction_looks_stale(
    window_key: str,
    raw_spent: float | None,
    corrected_spent: float | None,
    used_percent: float | None,
) -> bool:
    if raw_spent is None or corrected_spent is None or raw_spent <= 0:
        return False

    normalized_used_percent = _clamp_percent(used_percent)
    if normalized_used_percent is None or normalized_used_percent < STALE_DELTA_USED_PERCENT_THRESHOLD:
        return False

    if corrected_spent <= EPSILON:
        return True

    return corrected_spent / raw_spent <= STALE_DELTA_SPENT_RATIO_THRESHOLD


def _update_state(
    state: dict[str, Any],
    email: str | None,
    account_id: str | None,
    raw_spent: float | None,
    used_percent: float | None,
    reset_at: str | None,
    remaining_seconds: int | None,
    baseline_spent: float | None,
    estimate_uses_spent_delta: bool,
) -> None:
    updates = {
        "email": email,
        "sub2api_account_id": account_id,
        "baseline_spent": baseline_spent,
        "estimate_uses_spent_delta": estimate_uses_spent_delta,
        "last_raw_spent": raw_spent,
        "last_used_percent": used_percent,
        "last_reset_at": reset_at,
        "last_remaining_seconds": remaining_seconds,
    }
    for key, value in updates.items():
        if state.get(key) != value:
            state[key] = value
            state["dirty"] = True


def _spent_value(stats: dict[str, Any], window_data: dict[str, Any]) -> tuple[float | None, str | None]:
    candidates = (
        ("cost", _path_get(stats, ("cost",))),
        ("total_cost", _path_get(stats, ("total_cost",))),
        ("totalCost", _path_get(stats, ("totalCost",))),
        ("usage_cost", _path_get(stats, ("usage_cost",))),
        ("usageCost", _path_get(stats, ("usageCost",))),
        ("used_cost", _path_get(stats, ("used_cost",))),
        ("usedCost", _path_get(stats, ("usedCost",))),
        ("used_amount", _path_get(stats, ("used_amount",))),
        ("usedAmount", _path_get(stats, ("usedAmount",))),
        ("spent", _path_get(stats, ("spent",))),
        ("spend", _path_get(stats, ("spend",))),
        ("account_cost", _path_get(stats, ("account_cost",))),
        ("account_stats_cost", _path_get(stats, ("account_stats_cost",))),
        ("actual_cost", _path_get(stats, ("actual_cost",))),
        ("user_cost", _path_get(stats, ("user_cost",))),
        ("standard_cost", _path_get(stats, ("standard_cost",))),
        ("cost", _path_get(window_data, ("cost",))),
        ("total_cost", _path_get(window_data, ("total_cost",))),
        ("totalCost", _path_get(window_data, ("totalCost",))),
        ("usage_cost", _path_get(window_data, ("usage_cost",))),
        ("usageCost", _path_get(window_data, ("usageCost",))),
        ("used_cost", _path_get(window_data, ("used_cost",))),
        ("usedCost", _path_get(window_data, ("usedCost",))),
        ("used_amount", _path_get(window_data, ("used_amount",))),
        ("usedAmount", _path_get(window_data, ("usedAmount",))),
        ("spent", _path_get(window_data, ("spent",))),
        ("spend", _path_get(window_data, ("spend",))),
    )
    for source, value in candidates:
        number = _coerce_float(value)
        if number is not None:
            return number, source
    return None, None


def _used_amount_value(stats: dict[str, Any], window_data: dict[str, Any]) -> tuple[float | None, str | None]:
    candidates = (
        ("used", _path_get(stats, ("used",))),
        ("used", _path_get(window_data, ("used",))),
        ("usage", _path_get(stats, ("usage",))),
        ("usage", _path_get(window_data, ("usage",))),
    )
    for source, value in candidates:
        number = _coerce_float(value)
        if number is not None:
            return number, source
    return None, None


def _limit_value(stats: dict[str, Any], window_data: dict[str, Any]) -> tuple[float | None, str | None]:
    candidates = (
        ("limit", _path_get(stats, ("limit",))),
        ("limit", _path_get(window_data, ("limit",))),
        ("quota_limit", _path_get(stats, ("quota_limit",))),
        ("quota_limit", _path_get(window_data, ("quota_limit",))),
        ("quotaLimit", _path_get(stats, ("quotaLimit",))),
        ("quotaLimit", _path_get(window_data, ("quotaLimit",))),
        ("monthly_limit", _path_get(stats, ("monthly_limit",))),
        ("monthly_limit", _path_get(window_data, ("monthly_limit",))),
        ("monthlyLimit", _path_get(stats, ("monthlyLimit",))),
        ("monthlyLimit", _path_get(window_data, ("monthlyLimit",))),
        ("total_amount", _path_get(stats, ("total_amount",))),
        ("total_amount", _path_get(window_data, ("total_amount",))),
        ("totalAmount", _path_get(stats, ("totalAmount",))),
        ("totalAmount", _path_get(window_data, ("totalAmount",))),
    )
    for source, value in candidates:
        number = _coerce_float(value)
        if number is not None:
            return number, source
    return None, None


def _remaining_value(stats: dict[str, Any], window_data: dict[str, Any]) -> tuple[float | None, str | None]:
    candidates = (
        ("remaining", _path_get(stats, ("remaining",))),
        ("remaining", _path_get(window_data, ("remaining",))),
        ("remaining_amount", _path_get(stats, ("remaining_amount",))),
        ("remaining_amount", _path_get(window_data, ("remaining_amount",))),
        ("remainingAmount", _path_get(stats, ("remainingAmount",))),
        ("remainingAmount", _path_get(window_data, ("remainingAmount",))),
        ("unused", _path_get(stats, ("unused",))),
        ("unused", _path_get(window_data, ("unused",))),
        ("available", _path_get(stats, ("available",))),
        ("available", _path_get(window_data, ("available",))),
    )
    for source, value in candidates:
        number = _coerce_float(value)
        if number is not None:
            return number, source
    return None, None


def _tokens_value(stats: dict[str, Any], window_data: dict[str, Any]) -> int | None:
    for value in (
        _path_get(stats, ("tokens",)),
        _path_get(stats, ("total_tokens",)),
        _path_get(stats, ("account_stats_tokens",)),
        _path_get(window_data, ("tokens",)),
        _path_get(window_data, ("total_tokens",)),
    ):
        tokens = _coerce_int(value)
        if tokens is not None:
            return tokens

    prompt_tokens = _coerce_int(_first_present(_path_get(stats, ("input_tokens",)), _path_get(stats, ("prompt_tokens",))))
    completion_tokens = _coerce_int(_first_present(_path_get(stats, ("output_tokens",)), _path_get(stats, ("completion_tokens",))))
    token_parts = [value for value in (prompt_tokens, completion_tokens) if value is not None]
    if token_parts:
        return sum(token_parts)
    return None


def _limit_from_usage(spent: float | None, used_percent: float | None) -> float | None:
    if spent is None or spent <= 0 or used_percent is None or used_percent <= 0:
        return None
    return spent / (used_percent / 100)


def _zero_spend_is_missing(window_kind: str, raw_spent: float | None, used_percent: float | None) -> bool:
    return bool(
        window_kind == "monthly"
        and raw_spent is not None
        and raw_spent <= EPSILON
        and used_percent is not None
        and used_percent > EPSILON
    )


def _calibrated_limit(
    raw_limit: float | None,
    calibration: dict[str, Any],
    spent_floor: float | None = None,
) -> float | None:
    if raw_limit is None or raw_limit <= 0:
        return None
    current_spent = _coerce_float(spent_floor)
    effective_limit = raw_limit
    if current_spent is not None and current_spent > effective_limit:
        effective_limit = current_spent
    lower = _coerce_float(calibration.get("lower"))
    upper = _coerce_float(calibration.get("upper"))
    if lower is not None and effective_limit < lower:
        return lower
    if upper is not None and effective_limit > upper:
        if current_spent is not None and current_spent > upper:
            return effective_limit
        return upper
    return effective_limit


def _trusted_full_window_limit(raw_limit: float | None, used_percent: float | None) -> float | None:
    if raw_limit is None or raw_limit <= 0:
        return None
    normalized_used_percent = _clamp_percent(used_percent)
    if normalized_used_percent is None or normalized_used_percent < LIMIT_SAMPLE_FULL_PERCENT:
        return None
    return raw_limit


def _zero_usage_limit_estimate(
    calibration: dict[str, Any],
) -> float | None:
    mean = _coerce_float(calibration.get("mean"))
    if mean is not None and mean > 0:
        return mean

    lower = _coerce_float(calibration.get("lower"))
    upper = _coerce_float(calibration.get("upper"))
    if lower is not None and upper is not None and upper >= lower:
        return (lower + upper) / 2
    if upper is not None and upper > 0:
        return upper
    if lower is not None and lower > 0:
        return lower
    return None


def _estimate_spent_value(
    raw_spent: float | None,
    spent: float | None,
    estimate_uses_spent_delta: bool,
    baseline_spent: float | None,
    used_percent: float | None,
) -> tuple[float | None, str | None]:
    if spent is not None:
        return spent, "reset_corrected" if estimate_uses_spent_delta else "official_window"
    if raw_spent is not None:
        return raw_spent, "official_window"
    return None, None


def _aggregate_window(rows: list[dict[str, Any]], window_key: str) -> dict[str, Any]:
    spent = 0.0
    estimated_spent = 0.0
    limit = 0.0
    remaining = 0.0
    estimable_accounts = 0
    enabled_account_count = 0
    for row in rows:
        if not row.get("usage_estimate_enabled", True):
            continue
        window = _aggregate_source_window(row, window_key)
        if _exclude_from_aggregate(row, window, window_key):
            continue
        enabled_account_count += 1
        estimate_spent = window.get("estimate_spent")
        if estimate_spent is not None:
            spent += float(estimate_spent)
        if window.get("estimated_limit") is None or window.get("remaining") is None:
            continue
        estimable_accounts += 1
        estimated_spent += float(estimate_spent or 0)
        limit += float(window["estimated_limit"])
        remaining += float(window["remaining"])

    remaining_percent = (remaining / limit * 100) if limit > 0 else None
    used_percent = (estimated_spent / limit * 100) if limit > 0 else None
    return {
        "spent": spent,
        "estimated_limit": limit if estimable_accounts else None,
        "remaining": remaining if estimable_accounts else (0.0 if enabled_account_count == 0 else None),
        "remaining_percent": _clamp_percent(remaining_percent),
        "used_percent": _clamp_percent(used_percent),
        "account_count": len(rows),
        "enabled_account_count": enabled_account_count,
        "estimable_accounts": estimable_accounts,
    }


def _aggregate_source_window(row: dict[str, Any], window_key: str) -> dict[str, Any]:
    window = row.get(window_key) if isinstance(row.get(window_key), dict) else {}
    if window_key == "five_hour" and window.get("window_kind") == "none":
        longer_window = row.get("seven_day") if isinstance(row.get("seven_day"), dict) else {}
        if longer_window.get("window_kind") in {"seven_day", "monthly"}:
            return longer_window
    return window


def _exclude_from_aggregate(row: dict[str, Any], window: dict[str, Any], window_key: str) -> bool:
    return bool(
        row.get("deactive")
        or row.get("error")
        or row.get("usage_error")
        or (window_key == "five_hour" and row.get("rate_limited"))
        or window.get("rate_limited")
        or window.get("window_kind") == "none"
    )


def _group_estimates(account_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, str] = {}
    for row in account_rows:
        for group in row["groups"]:
            group_id = group["id"]
            grouped[group_id].append(row)
            names[group_id] = group["name"]

    return [
        {
            "group_id": group_id,
            "group_name": names[group_id],
            "account_count": len(rows),
            "five_hour": _aggregate_window(rows, "five_hour"),
            "seven_day": _aggregate_window(rows, "seven_day"),
        }
        for group_id, rows in sorted(grouped.items(), key=lambda item: (item[0] == UNGROUPED_ID, names[item[0]]))
    ]


def _account_groups(account: dict[str, Any], group_map: dict[str, str]) -> list[dict[str, str]]:
    groups: list[dict[str, str]] = []

    def add(group_id: Any, name: Any = None) -> None:
        text_id = _stringify(group_id)
        text_name = _stringify(name)
        if not text_id and text_name:
            text_id = text_name
        if not text_id:
            return
        final_name = text_name or group_map.get(text_id) or text_id
        item = {"id": text_id, "name": final_name}
        if item not in groups:
            groups.append(item)

    raw_groups = _first_present(
        account.get("groups"),
        account.get("group_ids"),
        account.get("group_id"),
        account.get("group"),
        account.get("account_groups"),
    )
    if isinstance(raw_groups, list):
        for group in raw_groups:
            if isinstance(group, dict):
                add(group.get("id") or group.get("group_id"), group.get("name") or group.get("group_name"))
            else:
                add(group)
    elif isinstance(raw_groups, dict):
        add(raw_groups.get("id") or raw_groups.get("group_id"), raw_groups.get("name") or raw_groups.get("group_name"))
    else:
        add(raw_groups)

    if not groups:
        groups.append({"id": UNGROUPED_ID, "name": UNGROUPED_NAME})
    return groups


def _first_dict(data: dict[str, Any], *paths: tuple[str, ...]) -> dict[str, Any]:
    for path in paths:
        value = _path_get(data, path)
        if isinstance(value, dict):
            return value
    return {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _path_get(data: Any, path: tuple[str, ...]) -> Any:
    current = data
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("%", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _coerce_int(value: Any) -> int | None:
    number = _coerce_float(value)
    return int(number) if number is not None else None


def _clamp_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(float(value), 100.0))


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
