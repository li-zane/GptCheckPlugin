from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models import AccountSnapshot, utcnow
from app.services.sub2api import Sub2ApiClient, Sub2ApiRequestError


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


async def build_usage_estimate(refresh: bool = True) -> dict[str, Any]:
    sub2api = Sub2ApiClient()
    accounts = [account for account in await sub2api.list_accounts() if sub2api.is_gpt_account(account)]
    group_map = await _load_group_map(sub2api)
    estimate_preferences = await _load_usage_estimate_preferences()

    usage_by_account_id: dict[str, dict[str, Any]] = {}
    errors_by_account_id: dict[str, str] = {}
    if refresh:
        usage_by_account_id, errors_by_account_id = await _fetch_usages(sub2api, accounts)

    account_rows = [
        _account_estimate(
            account=account,
            group_map=group_map,
            sub2api=sub2api,
            usage_estimate_enabled=_usage_estimate_enabled(account, sub2api, estimate_preferences),
            usage=usage_by_account_id.get(sub2api.account_id(account) or "", {}),
            usage_error=errors_by_account_id.get(sub2api.account_id(account) or ""),
        )
        for account in accounts
    ]

    return {
        "updated_at": utcnow(),
        "refreshed_usage": refresh,
        "formula": {
            "account_limit": "limit_i = spent_i / (used_percent_i / 100)",
            "account_remaining": "remaining_i = max(limit_i - spent_i, 0)",
            "account_remaining_percent": "remaining_percent_i = remaining_i / limit_i * 100",
            "aggregate_remaining": "total_remaining = sum(remaining_i)",
            "aggregate_remaining_percent": "total_remaining_percent = sum(remaining_i) / sum(limit_i) * 100",
            "aggregate_used_percent": "total_used_percent = sum(spent_i) / sum(limit_i) * 100",
            "estimable_rule": "Only enabled accounts with spent_i > 0 and used_percent_i > 0 are included in limit-based totals.",
        },
        "overall": {
            "account_count": len(account_rows),
            "five_hour": _aggregate_window(account_rows, "five_hour"),
            "seven_day": _aggregate_window(account_rows, "seven_day"),
        },
        "groups": _group_estimates(account_rows),
        "accounts": account_rows,
    }


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


async def _fetch_usages(
    sub2api: Sub2ApiClient,
    accounts: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    semaphore = asyncio.Semaphore(5)
    usage_by_id: dict[str, dict[str, Any]] = {}
    errors_by_id: dict[str, str] = {}

    async def fetch(account: dict[str, Any]) -> None:
        account_id = sub2api.account_id(account)
        if not account_id:
            return
        async with semaphore:
            try:
                usage_by_id[account_id] = await sub2api.get_account_usage(account_id, force=True)
            except Sub2ApiRequestError as exc:
                errors_by_id[account_id] = str(exc)

    await asyncio.gather(*(fetch(account) for account in accounts))
    return usage_by_id, errors_by_id


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
) -> dict[str, Any]:
    account_id = sub2api.account_id(account)
    groups = _account_groups(account, group_map)
    return {
        "email": sub2api.account_email(account) or _stringify(account.get("name")) or account_id or "unknown",
        "sub2api_account_id": account_id,
        "platform": sub2api.account_platform(account),
        "account_type": sub2api.account_type(account),
        "status": sub2api.account_status(account),
        "schedulable": sub2api.account_schedulable(account),
        "deactive": sub2api.is_deactive_account(account),
        "usage_estimate_enabled": usage_estimate_enabled,
        "rate_multiplier": _coerce_float(account.get("rate_multiplier")) or 1,
        "groups": groups,
        "usage_error": usage_error or None,
        "five_hour": _window_estimate(account, usage, "five_hour"),
        "seven_day": _window_estimate(account, usage, "seven_day"),
    }


def _window_estimate(account: dict[str, Any], usage: dict[str, Any], window_key: str) -> dict[str, Any]:
    window = usage.get(window_key)
    window_data = window if isinstance(window, dict) else {}
    stats = _first_dict(window_data, ("window_stats",), ("windowStats",), ("stats",))
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    prefix = WINDOWS[window_key]["extra_prefix"]

    used_percent = _coerce_float(
        _first_present(
            _path_get(window_data, ("utilization",)),
            _path_get(window_data, ("used_percent",)),
            _path_get(window_data, ("usage_percent",)),
            _path_get(window_data, ("percent",)),
            _path_get(extra, (f"{prefix}_used_percent",)),
            _path_get(account, (f"{prefix}_used_percent",)),
        )
    )
    spent, spend_source = _spent_value(stats, window_data)
    estimated_limit = _limit_from_usage(spent, used_percent)
    remaining = None
    remaining_percent = None
    if estimated_limit is not None and spent is not None:
        remaining = max(estimated_limit - spent, 0)
        remaining_percent = (remaining / estimated_limit * 100) if estimated_limit > 0 else 0

    source = "usage" if window_data else "account"
    return {
        "used_percent": _clamp_percent(used_percent),
        "spent": spent,
        "spend_source": spend_source,
        "estimated_limit": estimated_limit,
        "remaining": remaining,
        "remaining_percent": _clamp_percent(remaining_percent),
        "reset_at": _stringify(
            _first_present(
                _path_get(window_data, ("resets_at",)),
                _path_get(window_data, ("reset_at",)),
                _path_get(window_data, ("resetAt",)),
                _path_get(extra, (f"{prefix}_reset_at",)),
                _path_get(account, (f"{prefix}_reset_at",)),
            )
        ),
        "remaining_seconds": _coerce_int(
            _first_present(
                _path_get(window_data, ("remaining_seconds",)),
                _path_get(window_data, ("reset_after_seconds",)),
                _path_get(extra, (f"{prefix}_reset_after_seconds",)),
                _path_get(account, (f"{prefix}_reset_after_seconds",)),
            )
        ),
        "requests": _coerce_int(_path_get(stats, ("requests",))),
        "tokens": _coerce_int(_path_get(stats, ("tokens",))),
        "estimable": estimated_limit is not None,
        "source": source,
    }


def _spent_value(stats: dict[str, Any], window_data: dict[str, Any]) -> tuple[float | None, str | None]:
    candidates = (
        ("cost", _path_get(stats, ("cost",))),
        ("account_cost", _path_get(stats, ("account_cost",))),
        ("account_stats_cost", _path_get(stats, ("account_stats_cost",))),
        ("actual_cost", _path_get(stats, ("actual_cost",))),
        ("user_cost", _path_get(stats, ("user_cost",))),
        ("standard_cost", _path_get(stats, ("standard_cost",))),
        ("cost", _path_get(window_data, ("cost",))),
    )
    for source, value in candidates:
        number = _coerce_float(value)
        if number is not None:
            return number, source
    return None, None


def _limit_from_usage(spent: float | None, used_percent: float | None) -> float | None:
    if spent is None or spent <= 0 or used_percent is None or used_percent <= 0:
        return None
    return spent / (used_percent / 100)


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
        enabled_account_count += 1
        window = row[window_key]
        if window.get("spent") is not None:
            spent += float(window["spent"])
        if window.get("estimated_limit") is None or window.get("remaining") is None:
            continue
        estimable_accounts += 1
        estimated_spent += float(window.get("spent") or 0)
        limit += float(window["estimated_limit"])
        remaining += float(window["remaining"])

    remaining_percent = (remaining / limit * 100) if limit > 0 else None
    used_percent = (estimated_spent / limit * 100) if limit > 0 else None
    return {
        "spent": spent,
        "estimated_limit": limit if estimable_accounts else None,
        "remaining": remaining if estimable_accounts else None,
        "remaining_percent": _clamp_percent(remaining_percent),
        "used_percent": _clamp_percent(used_percent),
        "account_count": len(rows),
        "enabled_account_count": enabled_account_count,
        "estimable_accounts": estimable_accounts,
    }


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
