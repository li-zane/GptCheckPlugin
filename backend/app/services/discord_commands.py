from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models import UpstreamAccountConfig, UpstreamChannel
from app.services.runtime_config import RuntimeConfigService, get_runtime_config_service
from app.services.usage_estimate import get_cached_usage_estimate


DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
COLOR_GREEN = 0x57F287
COLOR_YELLOW = 0xFEE75C
COLOR_RED = 0xED4245
COLOR_BLURPLE = 0x5865F2
FIELD_VALUE_LIMIT = 1024
QUOTA_PROGRESS_SEGMENTS = 10

# Explicitly opt the commands into both installation types and all interaction
# contexts.  Without these fields, commands created before user-install was
# enabled can remain restricted to guild installs and will not appear in a DM.
COMMAND_INTEGRATION_TYPES = (0, 1)  # Guild install, user install.
COMMAND_CONTEXTS = (0, 1, 2)  # Guild, bot DM, private channel.
COMMANDS = (
    {
        "name": "balance",
        "description": "查看上游余额缓存",
        "type": 1,
        "integration_types": list(COMMAND_INTEGRATION_TYPES),
        "contexts": list(COMMAND_CONTEXTS),
    },
    {
        "name": "quota",
        "description": "查看 OAuth 账号额度缓存",
        "type": 1,
        "integration_types": list(COMMAND_INTEGRATION_TYPES),
        "contexts": list(COMMAND_CONTEXTS),
    },
)

logger = logging.getLogger(__name__)


def _command_requires_update(current: dict[str, Any], command: dict[str, Any]) -> bool:
    """Keep existing global commands aligned with their installation scope."""
    if current.get("description") != command["description"]:
        return True
    if current.get("type") != command["type"]:
        return True
    for field in ("integration_types", "contexts"):
        expected = command[field]
        actual = current.get(field)
        if not isinstance(actual, list) or sorted(actual) != sorted(expected):
            return True
    return False


class DiscordCommandService:
    def __init__(
        self,
        runtime_config: RuntimeConfigService | None = None,
        *,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        reconnect_base_seconds: float = 5.0,
    ) -> None:
        self.runtime_config = runtime_config or get_runtime_config_service()
        self.client_factory = client_factory or httpx.AsyncClient
        self.reconnect_base_seconds = max(0.1, reconnect_base_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._seen_interactions: set[str] = set()
        self._interaction_order: deque[str] = deque()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stop.clear()
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            self._wake.clear()
            try:
                config = await self.runtime_config.get_notification_config()
            except asyncio.CancelledError:
                raise
            except Exception:
                config = {}
            token = _config_string(config, "discord_bot_token")
            channel_id = _config_string(config, "discord_channel_id")
            enabled = _config_bool(config, "enabled")
            if not enabled or not token or not _valid_snowflake(channel_id):
                await self._wait_for_change(30.0)
                failures = 0
                continue
            try:
                await self._register_commands(token, channel_id)
                await self._run_gateway(token, channel_id)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                logger.warning("Discord command service reconnecting after %s", type(exc).__name__)
                delay = min(60.0, self.reconnect_base_seconds * (2 ** min(failures - 1, 4)))
                await self._wait_for_change(delay)

    async def _wait_for_change(self, timeout: float) -> None:
        if self._stop.is_set():
            return
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)

    async def _register_commands(self, token: str, channel_id: str) -> None:
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        async with self.client_factory(timeout=httpx.Timeout(10.0), trust_env=False) as client:
            application_response, channel_response = await asyncio.gather(
                client.get(f"{DISCORD_API_BASE_URL}/oauth2/applications/@me", headers=headers),
                client.get(f"{DISCORD_API_BASE_URL}/channels/{channel_id}", headers=headers),
            )
            application_response.raise_for_status()
            channel_response.raise_for_status()
            application_id = str(application_response.json().get("id") or "").strip()
            channel_payload = channel_response.json()
            guild_id = str(channel_payload.get("guild_id") or "").strip()
            if not _valid_snowflake(application_id):
                raise RuntimeError("discord_application_id_unavailable")
            command_base = f"{DISCORD_API_BASE_URL}/applications/{application_id}"
            if _valid_snowflake(guild_id):
                command_base += f"/guilds/{guild_id}"
            command_base += "/commands"
            existing_response = await client.get(command_base, headers=headers)
            existing_response.raise_for_status()
            existing = {
                str(item.get("name") or ""): item
                for item in existing_response.json()
                if isinstance(item, dict)
            }
            for command in COMMANDS:
                current = existing.get(command["name"])
                if current is None:
                    response = await client.post(command_base, headers=headers, json=command)
                elif _command_requires_update(current, command):
                    response = await client.patch(
                        f"{command_base}/{current['id']}",
                        headers=headers,
                        json=command,
                    )
                else:
                    continue
                response.raise_for_status()

    async def _run_gateway(self, token: str, channel_id: str) -> None:
        async with websockets.connect(
            DISCORD_GATEWAY_URL,
            open_timeout=15,
            close_timeout=5,
            max_size=2 * 1024 * 1024,
        ) as socket:
            hello = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
            if hello.get("op") != 10:
                raise RuntimeError("discord_gateway_hello_missing")
            heartbeat_interval = max(1.0, float(hello["d"]["heartbeat_interval"]) / 1000.0)
            await socket.send(
                json.dumps(
                    {
                        "op": 2,
                        "d": {
                            "token": token,
                            "intents": 0,
                            "properties": {
                                "os": "windows",
                                "browser": "sub2api-guardian",
                                "device": "sub2api-guardian",
                            },
                        },
                    }
                )
            )
            sequence = {"value": None}
            heartbeat = asyncio.create_task(
                self._heartbeat(socket, heartbeat_interval, sequence)
            )
            wake_task = asyncio.create_task(self._wake.wait())
            stop_task = asyncio.create_task(self._stop.wait())
            receive_task = asyncio.create_task(
                self._receive_gateway(socket, token, channel_id, sequence)
            )
            try:
                done, _ = await asyncio.wait(
                    {heartbeat, wake_task, stop_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    if task in {heartbeat, receive_task}:
                        task.result()
            finally:
                for task in (heartbeat, wake_task, stop_task, receive_task):
                    task.cancel()
                await asyncio.gather(
                    heartbeat,
                    wake_task,
                    stop_task,
                    receive_task,
                    return_exceptions=True,
                )

    @staticmethod
    async def _heartbeat(
        socket: Any,
        interval: float,
        sequence: dict[str, int | None],
    ) -> None:
        await asyncio.sleep(interval / 2)
        while True:
            await socket.send(json.dumps({"op": 1, "d": sequence["value"]}))
            await asyncio.sleep(interval)

    async def _receive_gateway(
        self,
        socket: Any,
        token: str,
        channel_id: str,
        sequence: dict[str, int | None],
    ) -> None:
        while True:
            payload = json.loads(await socket.recv())
            operation = payload.get("op")
            if isinstance(payload.get("s"), int):
                sequence["value"] = payload["s"]
            if operation == 0 and payload.get("t") == "INTERACTION_CREATE":
                await self._handle_interaction(token, channel_id, payload.get("d") or {})
            elif operation == 1:
                await socket.send(json.dumps({"op": 1, "d": payload.get("s")}))
            elif operation in {7, 9}:
                raise RuntimeError("discord_gateway_reconnect")

    async def _handle_interaction(
        self,
        token: str,
        configured_channel_id: str,
        interaction: dict[str, Any],
    ) -> None:
        interaction_id = str(interaction.get("id") or "").strip()
        interaction_token = str(interaction.get("token") or "").strip()
        if not interaction_id or not interaction_token or interaction_id in self._seen_interactions:
            return
        self._seen_interactions.add(interaction_id)
        self._interaction_order.append(interaction_id)
        if len(self._seen_interactions) > 500:
            while len(self._seen_interactions) > 250:
                expired = self._interaction_order.popleft()
                self._seen_interactions.discard(expired)
        command_name = str((interaction.get("data") or {}).get("name") or "").strip().lower()
        if command_name not in {"balance", "quota"}:
            return
        actual_channel_id = str(interaction.get("channel_id") or "").strip()
        if actual_channel_id != configured_channel_id:
            data = {"content": "此命令只能在已配置的通知频道使用。", "flags": 64}
        else:
            embed = (
                await build_balance_command_embed()
                if command_name == "balance"
                else await build_quota_command_embed()
            )
            data = {"embeds": [embed], "allowed_mentions": {"parse": []}}
        async with self.client_factory(timeout=httpx.Timeout(10.0), trust_env=False) as client:
            response = await client.post(
                f"{DISCORD_API_BASE_URL}/interactions/{interaction_id}/{interaction_token}/callback",
                headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
                json={"type": 4, "data": data},
            )
            response.raise_for_status()


async def build_balance_command_embed() -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        channels = list(
            (await db.scalars(select(UpstreamChannel).order_by(UpstreamChannel.display_name, UpstreamChannel.id))).all()
        )
        configs = list(
            (
                await db.scalars(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.channel_id.is_not(None),
                        UpstreamAccountConfig.remote_present.is_(True),
                    )
                )
            ).all()
        )
    by_channel: dict[int, list[UpstreamAccountConfig]] = {}
    for config in configs:
        if config.channel_id is not None:
            by_channel.setdefault(int(config.channel_id), []).append(config)
    categories: dict[str, list[str]] = {"有账号": [], "无启用": [], "无账号": []}
    checked_times: list[datetime] = []
    low_balance = False
    for channel in channels:
        accounts = by_channel.get(channel.id, [])
        if not accounts:
            category = "无账号"
        elif not any(_account_is_enabled(account) for account in accounts):
            category = "无启用"
        else:
            category = "有账号"
        wallet = _money(channel.balance_remaining, channel.balance_unit or "USD")
        adjusted = (
            channel.balance_remaining * channel.effective_recharge_multiplier
            if channel.balance_remaining is not None and channel.effective_recharge_multiplier is not None
            else None
        )
        categories[category].append(f"• {channel.display_name} · 原 {wallet} · 综 {_money(adjusted, 'CNY')}")
        if channel.balance_guard_state == "insufficient":
            low_balance = True
        if channel.balance_checked_at is not None:
            checked_times.append(_aware_utc(channel.balance_checked_at))
    fields: list[dict[str, Any]] = []
    for category in ("有账号", "无启用", "无账号"):
        for index, chunk in enumerate(_chunk_lines(categories[category])):
            fields.append(_command_field(category if index == 0 else f"{category}（续）", chunk))
    description = "暂无上游数据" if not channels else f"共 {len(channels)} 个上游"
    embed: dict[str, Any] = {
        "title": "上游余额",
        "description": description,
        "color": COLOR_RED if low_balance else COLOR_GREEN,
        "fields": fields[:25],
        "footer": {"text": "缓存数据 · /balance"},
        "timestamp": (max(checked_times) if checked_times else datetime.now(timezone.utc)).isoformat(),
    }
    return embed


async def build_quota_command_embed() -> dict[str, Any]:
    payload = await get_cached_usage_estimate()
    if not isinstance(payload, dict):
        return {
            "title": "OAuth 额度",
            "description": "暂无额度缓存",
            "color": COLOR_YELLOW,
            "footer": {"text": "缓存数据 · /quota"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    overall = payload.get("overall") if isinstance(payload.get("overall"), dict) else {}
    five_hour = overall.get("five_hour") if isinstance(overall.get("five_hour"), dict) else None
    seven_day = overall.get("seven_day") if isinstance(overall.get("seven_day"), dict) else None
    if five_hour is None and seven_day is None:
        return {
            "title": "OAuth 综合额度",
            "description": "综合额度缓存尚未生成，请等待下一次用量同步。",
            "color": COLOR_YELLOW,
            "footer": {"text": "缓存数据 · /quota"},
            "timestamp": _iso_datetime(payload.get("updated_at")) or datetime.now(timezone.utc).isoformat(),
        }

    account_count = _integer(overall.get("account_count"))
    fields = [
        _quota_aggregate_field("综合 5h", five_hour),
        _quota_aggregate_field("综合 7d", seven_day),
    ]
    updated_at = _iso_datetime(payload.get("updated_at"))
    return {
        "title": "OAuth 综合额度",
        "description": f"共 {account_count} 个 OAuth 账号" if account_count is not None else "OAuth 综合额度",
        "color": _quota_embed_color((five_hour, seven_day)),
        "fields": fields,
        "footer": {"text": "缓存数据 · /quota"},
        "timestamp": updated_at or datetime.now(timezone.utc).isoformat(),
    }


def _quota_aggregate_field(name: str, aggregate: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(aggregate, dict):
        return {"name": f"⚪ {name} · 暂无数据", "value": "`░░░░░░░░░░`\n暂无综合额度", "inline": False}
    enabled_count = _integer(aggregate.get("enabled_account_count"))
    account_count = _integer(aggregate.get("account_count"))
    estimable_count = _integer(aggregate.get("estimable_accounts"))
    remaining_percent = _quota_remaining_percent(aggregate)
    account_summary = ""
    if enabled_count is not None and account_count is not None:
        account_summary = f"\n统计 {enabled_count}/{account_count} 个账号"
    if estimable_count is not None:
        account_summary += f" · 可估 {estimable_count}"
    return {
        "name": f"{_quota_tone_marker(remaining_percent)} {name} · 剩余 {_percent(remaining_percent)}",
        "value": (
            f"{_quota_progress_bar(remaining_percent)}\n"
            f"**剩余 {_money(aggregate.get('remaining'), 'USD')}** · 总额 {_money(aggregate.get('estimated_limit'), 'USD')}\n"
            f"已用 {_money(aggregate.get('spent'), 'USD')}"
            f"{account_summary}"
        ),
        "inline": False,
    }


def _quota_remaining_percent(aggregate: dict[str, Any]) -> float | None:
    remaining_percent = _number(aggregate.get("remaining_percent"))
    if remaining_percent is not None:
        return min(max(remaining_percent, 0.0), 100.0)
    remaining = _number(aggregate.get("remaining"))
    total = _number(aggregate.get("estimated_limit"))
    if remaining is not None and total is not None and total > 0:
        return min(max((remaining / total) * 100, 0.0), 100.0)
    return None


def _quota_progress_bar(remaining_percent: float | None) -> str:
    if remaining_percent is None:
        return "`░░░░░░░░░░`"
    filled = round((remaining_percent / 100) * QUOTA_PROGRESS_SEGMENTS)
    filled = min(max(filled, 0), QUOTA_PROGRESS_SEGMENTS)
    return f"`{'█' * filled}{'░' * (QUOTA_PROGRESS_SEGMENTS - filled)}` {_percent(remaining_percent)}"


def _quota_tone_marker(remaining_percent: float | None) -> str:
    if remaining_percent is None:
        return "⚪"
    if remaining_percent <= 20:
        return "🔴"
    if remaining_percent <= 40:
        return "🟡"
    return "🟢"


def _quota_embed_color(aggregates: tuple[dict[str, Any] | None, ...]) -> int:
    percentages = [
        remaining_percent
        for aggregate in aggregates
        if isinstance(aggregate, dict)
        and (remaining_percent := _quota_remaining_percent(aggregate)) is not None
    ]
    if not percentages:
        return COLOR_BLURPLE
    lowest = min(percentages)
    if lowest <= 20:
        return COLOR_RED
    if lowest <= 40:
        return COLOR_YELLOW
    return COLOR_GREEN


def _command_field(name: str, value: str) -> dict[str, Any]:
    return {"name": name[:256], "value": value[:FIELD_VALUE_LIMIT], "inline": False}


def _chunk_lines(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        line = _short_text(line, FIELD_VALUE_LIMIT)
        added = len(line) + (1 if current else 0)
        if current and length + added > FIELD_VALUE_LIMIT:
            chunks.append("\n".join(current))
            current = []
            length = 0
        current.append(line)
        length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _money(value: Any, unit: str) -> str:
    number = _number(value)
    if number is None:
        return "—"
    normalized_unit = str(unit or "").strip().upper()
    prefix = "$" if normalized_unit == "USD" else "¥" if normalized_unit in {"CNY", "RMB"} else ""
    suffix = "" if prefix else f" {normalized_unit}" if normalized_unit else ""
    return f"{prefix}{number:.2f}{suffix}"


def _percent(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{min(max(number, 0.0), 100.0):.0f}%"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _short_text(value: str, limit: int) -> str:
    normalized = str(value or "").strip()
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1].rstrip()}…"


def _iso_datetime(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _aware_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00"))).isoformat()
    except ValueError:
        return None


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _config_string(config: dict[str, Any] | object, key: str) -> str:
    value = config.get(key) if isinstance(config, dict) else getattr(config, key, None)
    return value.strip() if isinstance(value, str) else ""


def _config_bool(config: dict[str, Any] | object, key: str) -> bool:
    value = config.get(key) if isinstance(config, dict) else getattr(config, key, None)
    return bool(value)


def _valid_snowflake(value: str) -> bool:
    return value.isascii() and value.isdigit() and 1 <= len(value) <= 32


def _account_is_enabled(account: UpstreamAccountConfig) -> bool:
    if account.remote_schedulable is False:
        return False
    status = str(account.remote_status or "").strip().lower()
    return status not in {"disabled", "inactive", "paused", "deactivated"}


_command_service: DiscordCommandService | None = None


def get_discord_command_service() -> DiscordCommandService:
    global _command_service
    if _command_service is None:
        _command_service = DiscordCommandService()
    return _command_service
