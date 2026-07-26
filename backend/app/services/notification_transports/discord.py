from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from app.services.notification_transports.base import (
    NotificationEnvelope,
    NotificationTransport,
    NotificationTransportError,
)
from app.services.runtime_config import RuntimeConfigService, get_runtime_config_service


DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DISCORD_MESSAGE_LIMIT = 2000
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024

COLOR_BLURPLE = 0x5865F2
COLOR_GREEN = 0x57F287
COLOR_YELLOW = 0xFEE75C
COLOR_ORANGE = 0xE67E22
COLOR_RED = 0xED4245
COLOR_GRAY = 0x95A5A6

_REASON_LABELS = {
    "manual": "手动操作",
    "manual_apply": "手动应用倍率",
    "automatic_apply": "自动应用倍率",
    "external_observed": "检测到外部修改倍率",
    "upstream_balance_negative": "上游渠道余额低于配置阈值",
    "upstream_key_unavailable": "上游 API Key 不可用",
    "upstream_group_unavailable": "上游分组不可用",
    "channel_monitor_unavailable": "上游渠道监控异常",
    "upstream_rate_increase": "上游倍率涨幅超过停用阈值",
    "upstream_channel_token_invalid": "上游登录 Token 已失效",
    "Account state changed manually.": "手动修改账号状态",
    "All automatic pause conditions cleared.": "所有自动暂停条件均已解除",
    "Sub2API reported the OAuth account as disabled.": "Sub2API 检测到 OAuth 账号已停用",
    "Sub2API reported the OAuth account as enabled again.": "Sub2API 检测到 OAuth 账号已恢复启用",
    "OAuth account refresh restored the account.": "OAuth 账号刷新后恢复正常",
}


class DiscordBotTransport(NotificationTransport):
    def __init__(
        self,
        runtime_config: RuntimeConfigService | None = None,
        *,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.runtime_config = runtime_config or get_runtime_config_service()
        self.client_factory = client_factory or httpx.AsyncClient
        self.timeout_seconds = timeout_seconds

    async def send(self, notification: NotificationEnvelope) -> None:
        config = await self.runtime_config.get_notification_config()
        token = _config_string(config, "discord_bot_token")
        channel_id = _config_string(config, "discord_channel_id")
        if not token or not _is_valid_channel_id(channel_id):
            raise NotificationTransportError("discord_not_configured", retryable=False)

        payload = _build_payload(notification)
        try:
            async with self.client_factory(
                timeout=httpx.Timeout(self.timeout_seconds),
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
                    headers={
                        "Authorization": f"Bot {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                # A channel can allow normal messages while denying the
                # Embed Links permission. Keep alerts deliverable in that
                # case by falling back to the compact text representation.
                if response.status_code in {400, 403}:
                    response = await client.post(
                        f"{DISCORD_API_BASE_URL}/channels/{channel_id}/messages",
                        headers={
                            "Authorization": f"Bot {token}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "content": _format_message(notification),
                            "allowed_mentions": {"parse": []},
                        },
                    )
        except httpx.TimeoutException as exc:
            raise NotificationTransportError("discord_timeout") from exc
        except httpx.HTTPError as exc:
            raise NotificationTransportError("discord_network_error") from exc

        if 200 <= response.status_code < 300:
            return
        if response.status_code == 429:
            raise NotificationTransportError(
                "discord_rate_limited",
                retry_after_seconds=_retry_after_seconds(response.headers.get("Retry-After")),
            )
        if response.status_code == 401:
            raise NotificationTransportError("discord_unauthorized", retryable=False)
        if 500 <= response.status_code < 600:
            raise NotificationTransportError("discord_server_error")
        raise NotificationTransportError("discord_request_rejected", retryable=False)


def _format_message(notification: NotificationEnvelope) -> str:
    embed = _build_embed(notification)
    title = str(embed.get("title") or "通知").strip()
    description = str(embed.get("description") or "").strip()
    lines = [f"**{title}**"]
    if description:
        lines.append(description)
    for field in embed.get("fields") or []:
        lines.append(f"**{field['name']}**  {field['value']}")
    content = "\n".join(lines)
    if len(content) <= DISCORD_MESSAGE_LIMIT:
        return content
    return f"{content[: DISCORD_MESSAGE_LIMIT - 3].rstrip()}..."


def _build_payload(notification: NotificationEnvelope) -> dict[str, Any]:
    return {
        "embeds": [_build_embed(notification)],
        "allowed_mentions": {"parse": []},
    }


def _build_embed(notification: NotificationEnvelope) -> dict[str, Any]:
    details = notification.details if isinstance(notification.details, dict) else {}
    event_type = notification.event_type.strip().lower()

    if event_type in {"account_disabled", "oauth_account_disabled"}:
        title = "调度变化"
        description = "账号已停用"
        color = COLOR_RED
        fields = _account_state_fields(details, enabled=False)
    elif event_type == "account_enabled":
        title = "调度变化"
        description = "账号已启用"
        color = COLOR_GREEN
        fields = _account_state_fields(details, enabled=True)
    elif event_type == "api_key_rate_changed":
        title = "倍率变化"
        description = _rate_description(details, "API Key 账号倍率已变化")
        color = _rate_color(details)
        fields = _rate_fields(details, include_group=False)
    elif event_type == "upstream_group_changed":
        title = "上游分组变化"
        description = _group_change_description(details)
        color = _rate_color(details, old_key="old_multiplier", new_key="new_multiplier")
        fields = _group_change_fields(details)
    elif event_type == "upstream_group_multiplier_changed":
        title = "倍率变化"
        description = _rate_description(details, "上游分组倍率已变化")
        color = _rate_color(details, old_key="old_multiplier", new_key="new_multiplier")
        fields = _rate_fields(details, include_group=True)
    elif event_type == "upstream_balance_low":
        title = "余额不足"
        description = "上游余额低于安全阈值"
        color = COLOR_RED
        fields = _balance_fields(details)
    elif event_type == "upstream_channel_token_invalid":
        title = "Token 失效"
        description = "上游拒绝登录凭据，自动刷新未能恢复"
        color = COLOR_RED
        fields = [
            _field("上游渠道", _channel_label(details) or "未知渠道"),
            _field("当前状态", "🔴 Token 失效"),
        ]
    elif event_type == "notification_test":
        title = "通知测试"
        description = "Discord Bot 配置有效"
        color = COLOR_BLURPLE
        fields = [_field("状态", "✅ 发送正常")]
    else:
        title = notification.title.strip() or "系统通知"
        description = notification.message.strip() or "收到一条新的系统通知。"
        color = COLOR_GRAY
        fields = []

    embed: dict[str, Any] = {
        "title": _trim(title, DISCORD_EMBED_TITLE_LIMIT),
        "description": _trim(description, DISCORD_EMBED_DESCRIPTION_LIMIT),
        "color": color,
        "footer": {"text": "sub2api · 自动通知"},
        "timestamp": _event_timestamp(details),
    }
    if fields:
        embed["fields"] = fields[:25]
    return embed


def _account_state_fields(details: dict[str, Any], *, enabled: bool) -> list[dict[str, Any]]:
    account_name = _first_text(
        details.get("account_name"),
        details.get("email"),
        _id_label("账号", details.get("account_id")),
        "未知账号",
    )
    account_type = str(details.get("account_type") or "").strip().lower()
    type_label = {"oauth": "OAuth", "api_key": "API Key"}.get(account_type, account_type or "未知")
    fields = [
        _field("账号", account_name),
        _field("类型", type_label),
        _field("当前状态", "🟢 已启用" if enabled else "🔴 已停用"),
    ]
    channel = _channel_label(details)
    if channel:
        fields.append(_field("关联渠道", channel))
    reason = _reason_label(details.get("reason"))
    if reason:
        fields.append(_field("恢复说明" if enabled else "触发原因", reason, inline=False))
    if enabled:
        previous_pause_reasons = _reason_labels(details.get("previous_pause_reasons"))
        if previous_pause_reasons:
            fields.append(
                _field(
                    "原暂停原因",
                    "\n".join(f"• {item}" for item in previous_pause_reasons),
                    inline=False,
                )
            )
    if not enabled and details.get("reason") == "upstream_balance_negative":
        fields.extend(_balance_detail_fields(details))
    return fields


def _rate_fields(details: dict[str, Any], *, include_group: bool) -> list[dict[str, Any]]:
    old_key = "old_multiplier" if include_group else "old_rate"
    new_key = "new_multiplier" if include_group else "new_rate"
    old_value = _number(details.get(old_key))
    new_value = _number(details.get(new_key))
    fields: list[dict[str, Any]] = []

    channel = _channel_label(details)
    if channel:
        fields.append(_field("上游渠道", channel))
    if include_group:
        group = _first_text(
            details.get("group_name"),
            details.get("group_id"),
            "未知分组",
        )
        fields.append(_field("上游分组", group))
    else:
        account = _first_text(
            details.get("account_name"),
            _id_label("账号", details.get("account_id")),
            "未知账号",
        )
        fields.append(_field("API Key 账号", account))

    fields.extend(
        [
            _field("原倍率", _multiplier_label(old_value)),
            _field("新倍率", _multiplier_label(new_value)),
            _field("变化幅度", _change_label(old_value, new_value)),
        ]
    )
    reason = _reason_label(details.get("reason"))
    if reason:
        fields.append(_field("变更来源", reason, inline=False))
    if include_group:
        fields.extend(_affected_account_fields(details.get("affected_accounts")))
    return fields


def _affected_account_fields(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        account = _first_text(
            item.get("account_name"),
            _id_label("账号", item.get("account_id")),
            "未知账号",
        )
        old_rate = _multiplier_label(_number(item.get("old_rate")))
        new_rate = _multiplier_label(_number(item.get("new_rate")))
        status = {
            "applied": "已应用",
            "observed": "已确认",
            "apply_failed": "应用失败",
            "skipped": "未应用",
        }.get(str(item.get("status") or "").strip().lower(), "已变化")
        lines.append(f"• {account} · {old_rate} → {new_rate} · {status}")
    return [
        _field(f"关联账号 · {len(lines)}", chunk, inline=False)
        for chunk in _chunk_lines(lines, DISCORD_EMBED_FIELD_VALUE_LIMIT)
    ]


def _group_change_description(details: dict[str, Any]) -> str:
    channel = _channel_label(details)
    group = _first_text(details.get("group_name"), details.get("group_id"), "未知分组")
    change_type = {
        "added": "新增",
        "removed": "删除",
        "renamed": "名称变化",
        "multiplier": "倍率变化",
    }.get(str(details.get("change_type") or "").strip().lower(), "状态变化")
    if channel:
        return f"检测到 **{_escape_markdown(channel)}** 的上游分组 **{_escape_markdown(group)}** 发生{change_type}。"
    return f"检测到上游分组 **{_escape_markdown(group)}** 发生{change_type}。"


def _group_change_fields(details: dict[str, Any]) -> list[dict[str, Any]]:
    fields = [
        _field("上游渠道", _channel_label(details) or "未知渠道"),
        _field("上游分组", _first_text(details.get("group_name"), details.get("group_id"), "未知分组")),
        _field("变化类型", {
            "added": "新增",
            "removed": "删除",
            "renamed": "名称变化",
            "multiplier": "倍率变化",
        }.get(str(details.get("change_type") or "").strip().lower(), "状态变化")),
    ]
    old_name = details.get("old_name")
    new_name = details.get("new_name")
    if old_name or new_name:
        fields.append(_field("名称", f"{old_name or '--'} → {new_name or '--'}"))
    old_status = details.get("old_status")
    new_status = details.get("new_status")
    if old_status or new_status:
        fields.append(_field("状态", f"{old_status or '--'} → {new_status or '--'}"))
    old_multiplier = _number(details.get("old_multiplier"))
    new_multiplier = _number(details.get("new_multiplier"))
    if old_multiplier is not None or new_multiplier is not None:
        fields.append(_field("倍率", f"{_multiplier_label(old_multiplier)} → {_multiplier_label(new_multiplier)}"))
    return fields


def _balance_fields(details: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _field("上游渠道", _channel_label(details) or "未知渠道"),
        *_balance_detail_fields(details),
    ]


def _balance_detail_fields(details: dict[str, Any]) -> list[dict[str, Any]]:
    balance = _number(details.get("balance"))
    threshold = _number(details.get("threshold"))
    unit = str(details.get("unit") or "USD").strip() or "USD"
    basis = str(details.get("basis") or "").strip().lower()
    basis_label = {
        "wallet": "上游钱包余额",
        "upstream_wallet": "上游钱包余额",
        "recharge_adjusted": "充值倍率折算余额",
    }.get(basis, basis or "未指定")
    fields = [
        _field(
            "当前余额",
            f"🔴 {balance:.2f} {unit}" if balance is not None else f"-- {unit}",
        )
    ]
    if threshold is not None:
        fields.append(_field("配置阈值", f"{threshold:.2f} {unit}"))
    fields.append(_field("检测口径", basis_label))
    return fields


def _rate_description(details: dict[str, Any], fallback: str) -> str:
    channel = _channel_label(details)
    if not channel:
        return fallback
    group = _first_text(details.get("group_name"), details.get("group_id"))
    return " · ".join(item for item in (channel, group) if item)


def _rate_color(
    details: dict[str, Any],
    *,
    old_key: str = "old_rate",
    new_key: str = "new_rate",
) -> int:
    old_value = _number(details.get(old_key))
    new_value = _number(details.get(new_key))
    if old_value is None or new_value is None or old_value == new_value:
        return COLOR_YELLOW
    return COLOR_ORANGE if new_value > old_value else COLOR_GREEN


def _event_timestamp(details: dict[str, Any]) -> str:
    raw_value = details.get("observed_at")
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            parsed = datetime.fromisoformat(raw_value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _field(name: str, value: Any, *, inline: bool = True) -> dict[str, Any]:
    return {
        "name": _trim(str(name), DISCORD_EMBED_FIELD_NAME_LIMIT),
        "value": _trim(_escape_markdown(str(value)), DISCORD_EMBED_FIELD_VALUE_LIMIT),
        "inline": inline,
    }


def _channel_label(details: dict[str, Any]) -> str:
    return _first_text(
        details.get("channel_name"),
        _id_label("渠道", details.get("channel_id")),
    )


def _reason_label(value: Any) -> str:
    reason = str(value or "").strip()
    return _REASON_LABELS.get(reason, reason)


def _reason_labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    labels: list[str] = []
    for item in value:
        label = _reason_label(item)
        if label and label not in labels:
            labels.append(label)
    return labels


def _id_label(prefix: str, value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    normalized = str(value).strip()
    return f"{prefix} #{normalized}" if normalized else ""


def _first_text(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _multiplier_label(value: float | None) -> str:
    return f"{value:.6g}×" if value is not None else "--"


def _change_label(old_value: float | None, new_value: float | None) -> str:
    if old_value is None or new_value is None or old_value == 0:
        return "--"
    percent = (new_value - old_value) / abs(old_value) * 100
    if percent > 0:
        return f"↑ +{percent:.2f}%"
    if percent < 0:
        return f"↓ {percent:.2f}%"
    return "无变化"


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in ("*", "_", "~", "`", ">", "|"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _trim(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3].rstrip()}..."


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        normalized = _trim(line, limit)
        added_length = len(normalized) + (1 if current else 0)
        if current and current_length + added_length > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(normalized)
        current_length += len(normalized) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _config_string(config: dict[str, Any] | object, key: str) -> str:
    value = config.get(key) if isinstance(config, dict) else getattr(config, key, None)
    return value.strip() if isinstance(value, str) else ""


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return min(max(parsed, 0.0), 86_400.0)


def _is_valid_channel_id(value: str) -> bool:
    return value.isascii() and value.isdigit() and 1 <= len(value) <= 32
