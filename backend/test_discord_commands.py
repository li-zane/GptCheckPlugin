import json
from unittest.mock import AsyncMock, patch
import unittest

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import ApiAccount, Upstream
from app.services.discord_commands import (
    DiscordCommandService,
    build_balance_command_embed,
    build_quota_command_embed,
)


class DiscordCommandEmbedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_balance_omits_empty_categories_and_groups_disabled_accounts(self) -> None:
        async with self.sessions() as db:
            active = Upstream(
                display_name="有账号上游",
                api_endpoint_url="https://active.example",
                wallet_balance_usd=12.5,
                balance_unit="USD",
                upstream_recharge_multiplier=2.0,
                balance_guard_state="healthy",
            )
            disabled = Upstream(
                display_name="无启用上游",
                api_endpoint_url="https://disabled.example",
                wallet_balance_usd=3.0,
                balance_unit="USD",
                upstream_recharge_multiplier=1.5,
                balance_guard_state="insufficient",
            )
            db.add_all([active, disabled])
            await db.flush()
            db.add_all(
                [
                    ApiAccount(
                        management_account_id=1,
                        upstream_id=active.id,
                        remote_name="Active",
                        remote_schedulable=True,
                        remote_present=True,
                        platform_type="auto",
                    ),
                    ApiAccount(
                        management_account_id=2,
                        upstream_id=disabled.id,
                        remote_name="Disabled",
                        remote_schedulable=False,
                        remote_present=True,
                        platform_type="auto",
                    ),
                ]
            )
            await db.commit()

        with patch("app.services.discord_commands.AsyncSessionLocal", self.sessions):
            embed = await build_balance_command_embed()

        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(set(fields), {"有账号", "无启用"})
        self.assertIn("钱包 $12.50 · 实际 ¥25.00", fields["有账号"])
        self.assertIn("钱包 $3.00 · 实际 ¥4.50", fields["无启用"])
        self.assertEqual(embed["color"], 0xED4245)

    async def test_quota_uses_cached_composite_windows(self) -> None:
        cache = {
            "updated_at": "2026-07-25T08:00:00+00:00",
            "overall": {
                "account_count": 3,
                "five_hour": {
                    "remaining": 80,
                    "remaining_percent": 80,
                    "estimated_limit": 100,
                    "spent": 20,
                    "enabled_account_count": 2,
                    "estimable_accounts": 2,
                },
                "seven_day": {
                    "remaining": 120,
                    "remaining_percent": 60,
                    "estimated_limit": 200,
                    "spent": 80,
                    "enabled_account_count": 3,
                    "estimable_accounts": 3,
                },
            },
        }
        with patch(
            "app.services.discord_commands.get_cached_usage_estimate",
            new=AsyncMock(return_value=cache),
        ):
            embed = await build_quota_command_embed()

        self.assertEqual(embed["title"], "OAuth 综合额度")
        self.assertEqual(embed["description"], "共 3 个 OAuth 账号")
        fields = {field["name"]: field for field in embed["fields"]}
        five_hour_field = next(field for name, field in fields.items() if "综合 5h" in name)
        seven_day_field = next(field for name, field in fields.items() if "综合 7d" in name)
        self.assertIn("🟢", five_hour_field["name"])
        self.assertIn("`████████░░` 80%", five_hour_field["value"])
        self.assertIn("**剩余 $80.00** · 总额 $100.00", five_hour_field["value"])
        self.assertIn("`██████░░░░` 60%", seven_day_field["value"])
        self.assertIn("已用 $80.00", seven_day_field["value"])
        self.assertFalse(five_hour_field["inline"])
        self.assertEqual(embed["color"], 0x57F287)


class DiscordCommandServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_channel_returns_ephemeral_message(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        service = DiscordCommandService(client_factory=client_factory)
        await service._handle_interaction(
            "bot-token",
            "123456",
            {
                "id": "111",
                "token": "interaction-token",
                "channel_id": "999999",
                "data": {"name": "balance"},
            },
        )

        payload = json.loads(requests[0].content)
        self.assertEqual(payload["data"]["flags"], 64)
        self.assertIn("已配置的通知频道", payload["data"]["content"])

    async def test_registration_preserves_unrelated_commands(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            path = request.url.path
            if path.endswith("/oauth2/applications/@me"):
                return httpx.Response(200, json={"id": "111111"})
            if path.endswith("/channels/222222"):
                return httpx.Response(200, json={"guild_id": "333333"})
            if request.method == "GET" and path == "/api/v10/applications/111111/commands":
                return httpx.Response(
                    200,
                    json=[
                        {"id": "10", "name": "quota", "description": "legacy global"},
                        {"id": "20", "name": "unrelated", "description": "keep"},
                    ],
                )
            if request.method == "GET" and path.endswith("/guilds/333333/commands"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": "1", "name": "quota", "description": "old"},
                        {"id": "2", "name": "unrelated", "description": "keep"},
                    ],
                )
            return httpx.Response(200, json={})

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        service = DiscordCommandService(client_factory=client_factory)
        await service._register_commands("bot-token", "222222")

        writes = [(request.method, request.url.path) for request in requests if request.method != "GET"]
        self.assertEqual(len(writes), 3)
        self.assertIn(("DELETE", "/api/v10/applications/111111/commands/10"), writes)
        self.assertNotIn(("DELETE", "/api/v10/applications/111111/commands/20"), writes)
        self.assertTrue(any(method == "POST" and "/guilds/333333/commands" in path for method, path in writes))
        self.assertTrue(any(method == "PATCH" and path.endswith("/guilds/333333/commands/1") for method, path in writes))

    async def test_guild_registration_omits_global_install_fields(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            path = request.url.path
            if path.endswith("/oauth2/applications/@me"):
                return httpx.Response(200, json={"id": "111111"})
            if path.endswith("/channels/222222"):
                return httpx.Response(200, json={"guild_id": "333333"})
            if request.method == "GET" and path == "/api/v10/applications/111111/commands":
                return httpx.Response(200, json=[])
            if request.method == "GET" and path.endswith("/guilds/333333/commands"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": "1", "name": "balance", "description": "old", "type": 1},
                        {"id": "2", "name": "quota", "description": "old", "type": 1},
                    ],
                )
            return httpx.Response(200, json={})

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        service = DiscordCommandService(client_factory=client_factory)
        await service._register_commands("bot-token", "222222")

        updates = [request for request in requests if request.method == "PATCH"]
        self.assertEqual(len(updates), 2)
        for request in updates:
            payload = json.loads(request.content)
            self.assertNotIn("integration_types", payload)
            self.assertNotIn("contexts", payload)
            self.assertIn("/guilds/333333/commands/", request.url.path)

    async def test_registration_requires_a_server_channel(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/oauth2/applications/@me"):
                return httpx.Response(200, json={"id": "111111"})
            if request.url.path.endswith("/channels/222222"):
                return httpx.Response(200, json={"type": 1})
            return httpx.Response(500)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        service = DiscordCommandService(client_factory=client_factory)
        with self.assertRaisesRegex(RuntimeError, "discord_guild_channel_required"):
            await service._register_commands("bot-token", "222222")


if __name__ == "__main__":
    unittest.main()
