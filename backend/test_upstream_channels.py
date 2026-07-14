from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.upstream_channels import router
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import Base, get_db
from app.core.security import require_admin
from app.core.validation import sanitized_request_validation_handler
from app.models import UpstreamAccountConfig, UpstreamChannel, UpstreamRateChangeLog
from app.schemas import UpstreamAccountUpdate, UpstreamChannelUpdate
from app.services.sub2api import Sub2ApiClient
from app.services.upstream_accounts import UpstreamAccountServiceError
from app.services.upstream_channels import UpstreamChannelService, get_upstream_channel_service
from app.services.upstream_client import Sub2ApiTokenPair


def nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in nested_keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in nested_keys(item)}
    return set()


class FakeSub2Api(Sub2ApiClient):
    def __init__(self) -> None:
        super().__init__()
        self.accounts = [
            {
                "id": 7,
                "name": "alpha",
                "platform": "openai",
                "type": "apikey",
                "status": "active",
                "rate_multiplier": 1.0,
                "credentials": {"base_url": "https://UPSTREAM.example/v1"},
            },
            {
                "id": 8,
                "name": "beta",
                "platform": "anthropic",
                "type": "api_key",
                "status": "active",
                "rate_multiplier": 0.5,
                "credentials": {"base_url": "https://upstream.example/api/v1/"},
            },
        ]
        self.exported_api_keys: dict[int, str] = {}
        self.export_calls: list[list[int]] = []
        self.balance_results: dict[int, dict] = {}
        self.rate_update_calls: list[tuple[int, float]] = []
        self.local_credit_per_cny = 10.0

    async def list_api_key_accounts(self) -> list[dict]:
        return list(self.accounts)

    async def get_payment_balance_recharge_multiplier_info(self) -> tuple[float, bool]:
        return self.local_credit_per_cny, True

    async def export_api_key_secrets(self, account_ids: list[int]) -> dict[int, str]:
        self.export_calls.append(list(account_ids))
        return {
            account_id: self.exported_api_keys[account_id]
            for account_id in account_ids
            if account_id in self.exported_api_keys
        }

    async def get_account_balance(self, account: dict | str | int) -> dict:
        account_id = int(account if isinstance(account, (str, int)) else account["id"])
        return dict(self.balance_results.get(account_id, {"status": "unsupported"}))

    async def get_account_current_rate_multiplier(self, account_id: str | int) -> float:
        parsed_id = int(account_id)
        account = next(item for item in self.accounts if int(item["id"]) == parsed_id)
        return float(account["rate_multiplier"])

    async def update_account_rate_multiplier(self, account_id: str | int, rate_multiplier: float) -> None:
        parsed_id = int(account_id)
        self.rate_update_calls.append((parsed_id, rate_multiplier))
        account = next(item for item in self.accounts if int(item["id"]) == parsed_id)
        account["rate_multiplier"] = rate_multiplier


class UpstreamChannelServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.sub2api = FakeSub2Api()
        self.service = UpstreamChannelService(self.sub2api)
        self.runtime_config = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=False),
            get_automation_paused=AsyncMock(return_value=False),
        )
        self.runtime_config_patcher = patch(
            "app.services.upstream_channels.get_runtime_config_service",
            return_value=self.runtime_config,
        )
        self.runtime_config_patcher.start()
        self.addCleanup(self.runtime_config_patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    async def _configure_sub2api_credentials(
        self,
        *,
        access_token: str = "at-old-private",
        refresh_token: str = "rt-old-private",
    ) -> int:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(
                upstream_type="sub2api",
                access_token=access_token,
                refresh_token=refresh_token,
            ),
        )
        return channel_id

    @staticmethod
    def _discovery_result(
        *,
        status: str = "ok",
        auth_rejected: bool = False,
        balance_remaining: float | None = 42.75,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            upstream_type="sub2api",
            status=status,
            sub2api_auth_rejected=auth_rejected,
            groups=[{"id": "default", "name": "Default", "multiplier": 1.0}],
            discovered_recharge_multiplier=1.0,
            discovered_recharge_multiplier_source="payment.config",
            recharge_discovery_status="ok",
            balance_remaining=balance_remaining,
            balance_total=None,
            balance_used=None,
            balance_unit="USD",
            balance_status="ok" if status == "ok" else "error",
            balance_message="Balance available." if status == "ok" else "Credentials rejected.",
        )

    async def test_overview_groups_equivalent_v1_urls_into_one_channel(self) -> None:
        overview = await self.service.overview(self.db)

        self.assertEqual(len(overview.channels), 1)
        self.assertEqual(overview.channels[0].canonical_base_url, "https://upstream.example")
        self.assertEqual([item.sub2api_account_id for item in overview.channels[0].accounts], [7, 8])
        self.assertEqual(overview.local_recharge_multiplier, 0.1)
        self.assertEqual(overview.unassigned_accounts, [])

        again = await self.service.overview(self.db)
        self.assertEqual(len(again.channels), 1)
        channels = await self.db.execute(select(UpstreamChannel))
        self.assertEqual(len(channels.scalars().all()), 1)

    async def test_auto_assigned_account_follows_remote_endpoint_change(self) -> None:
        overview = await self.service.overview(self.db)
        original_channel = overview.channels[0]
        stored = (
            await self.db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == 7
                )
            )
        ).scalar_one()
        original_channel_row = await self.db.get(UpstreamChannel, original_channel.id)
        assert original_channel_row is not None
        original_channel_row.encrypted_access_token = encrypt_text("old-channel-token")
        stored.encrypted_access_token = original_channel_row.encrypted_access_token
        stored.encrypted_api_key = encrypt_text("old-endpoint-api-key")
        stored.selected_group_id = "legacy"
        stored.selected_group_name = "Legacy"
        stored.manual_group_multiplier = 2.0
        stored.effective_group_multiplier = 2.0
        stored.target_rate = 2.0
        await self.db.commit()

        self.sub2api.accounts[0]["credentials"]["base_url"] = (
            "https://replacement.example/api/v1"
        )
        updated = await self.service.overview(self.db)

        by_url = {channel.canonical_base_url: channel for channel in updated.channels}
        self.assertEqual(
            [account.sub2api_account_id for account in by_url["https://upstream.example"].accounts],
            [8],
        )
        replacement = by_url["https://replacement.example"]
        self.assertEqual(
            [account.sub2api_account_id for account in replacement.accounts],
            [7],
        )
        self.assertFalse(replacement.accounts[0].api_key_set)
        self.assertFalse(replacement.access_token_set)

        await self.db.refresh(stored)
        self.assertEqual(stored.channel_id, replacement.id)
        self.assertEqual(stored.base_url, "https://replacement.example")
        self.assertIsNone(stored.encrypted_api_key)
        self.assertIsNone(stored.encrypted_access_token)
        self.assertIsNone(stored.selected_group_id)
        self.assertIsNone(stored.selected_group_name)
        self.assertIsNone(stored.manual_group_multiplier)
        self.assertIsNone(stored.target_rate)
        self.assertEqual(
            stored.last_error,
            "The upstream endpoint changed; rediscovery is required.",
        )

    async def test_channel_token_is_encrypted_and_never_returned(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]
        secret = "channel-access-token-private"
        refresh_secret = "channel-refresh-token-private"

        updated = await self.service.update_channel(
            self.db,
            channel.id,
            UpstreamChannelUpdate(
                display_name="Example upstream",
                base_url="https://upstream.example/v1",
                upstream_type="newapi",
                upstream_user_id="42",
                access_token=secret,
                refresh_token=refresh_secret,
                manual_recharge_multiplier=0.1,
            ),
        )

        stored = await self.db.get(UpstreamChannel, channel.id)
        self.assertIsNotNone(stored)
        self.assertTrue(updated.access_token_set)
        self.assertTrue(updated.refresh_token_set)
        self.assertNotIn("access_token", updated.model_dump())
        self.assertNotIn("refresh_token", updated.model_dump())
        self.assertNotEqual(stored.encrypted_access_token, secret)
        self.assertNotIn(secret, stored.encrypted_access_token or "")
        self.assertNotEqual(stored.encrypted_refresh_token, refresh_secret)
        self.assertNotIn(refresh_secret, stored.encrypted_refresh_token or "")

    async def test_canonical_url_collision_is_rejected_without_modifying_channels(self) -> None:
        self.sub2api.accounts.append(
            {
                "id": 9,
                "name": "other",
                "platform": "openai",
                "type": "apikey",
                "status": "active",
                "rate_multiplier": 1.0,
                "credentials": {"base_url": "https://other.example/v1"},
            }
        )
        overview = await self.service.overview(self.db)
        by_url = {item.canonical_base_url: item for item in overview.channels}
        source = by_url["https://upstream.example"]
        target = by_url["https://other.example"]

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.update_channel(
                self.db,
                source.id,
                UpstreamChannelUpdate(base_url="https://OTHER.example/api/v1/"),
            )

        self.assertEqual(context.exception.status_code, 409)
        await self.db.rollback()
        source_row = await self.db.get(UpstreamChannel, source.id)
        target_row = await self.db.get(UpstreamChannel, target.id)
        self.assertEqual(source_row.canonical_base_url, "https://upstream.example")
        self.assertEqual(target_row.canonical_base_url, "https://other.example")

    async def test_shared_credential_change_invalidates_every_bound_account_preview(self) -> None:
        overview = await self.service.overview(self.db)
        channel_id = overview.channels[0].id
        channel = await self.db.get(UpstreamChannel, channel_id)
        self.assertIsNotNone(channel)
        channel.resolved_upstream_type = "newapi"
        channel.group_options = [{"id": "default", "name": "Default", "multiplier": 1.0}]
        channel.discovered_recharge_multiplier = 0.1
        channel.effective_recharge_multiplier = 0.1
        channel.recharge_multiplier_source = "status.price"
        channel.recharge_multiplier_status = "ok"
        channel.balance_remaining = 12.5
        channel.balance_status = "ok"

        configs_result = await self.db.execute(
            select(UpstreamAccountConfig).where(UpstreamAccountConfig.channel_id == channel_id)
        )
        configs = list(configs_result.scalars().all())
        self.assertEqual(len(configs), 2)
        for config in configs:
            config.discovered_group_multiplier = 1.0
            config.effective_group_multiplier = 1.0
            config.group_multiplier_source = "upstream"
            config.group_multiplier_status = "ok"
            config.target_rate = 0.1
            config.last_discovered_at = channel.updated_at
        await self.db.commit()

        secret = "fictional-shared-token-rotated"
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(access_token=secret),
        )

        refreshed_channel = await self.db.get(UpstreamChannel, channel_id)
        await self.db.refresh(refreshed_channel)
        self.assertIsNone(refreshed_channel.resolved_upstream_type)
        self.assertEqual(refreshed_channel.group_options, [])
        self.assertIsNone(refreshed_channel.effective_recharge_multiplier)
        self.assertEqual(refreshed_channel.recharge_multiplier_status, "not_discovered")
        self.assertIsNone(refreshed_channel.balance_remaining)
        self.assertEqual(refreshed_channel.balance_status, "not_checked")
        self.assertNotEqual(refreshed_channel.encrypted_access_token, secret)

        refreshed_configs_result = await self.db.execute(
            select(UpstreamAccountConfig).where(UpstreamAccountConfig.channel_id == channel_id)
        )
        refreshed_configs = list(refreshed_configs_result.scalars().all())
        for config in refreshed_configs:
            self.assertIsNone(config.discovered_group_multiplier)
            self.assertIsNone(config.effective_group_multiplier)
            self.assertIsNone(config.group_multiplier_source)
            self.assertEqual(config.group_multiplier_status, "not_discovered")
            self.assertIsNone(config.target_rate)
            self.assertIsNone(config.last_discovered_at)
            self.assertEqual(config.encrypted_access_token, refreshed_channel.encrypted_access_token)

    async def test_channel_routes_never_echo_access_token_or_ciphertext(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")

        async def fake_db():
            async with self.session_factory() as session:
                yield session

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: self.service
        secret = "fictional-route-access-token-private"
        refresh_secret = "fictional-route-refresh-token-private"
        with TestClient(app) as client:
            updated_response = client.put(
                f"/api/upstream-channels/{channel_id}",
                json={"access_token": secret, "refresh_token": refresh_secret},
            )
            overview_response = client.get("/api/upstream-channels")

        self.assertEqual(updated_response.status_code, 200, updated_response.text)
        self.assertEqual(overview_response.status_code, 200, overview_response.text)
        async with self.session_factory() as session:
            stored = await session.get(UpstreamChannel, channel_id)
            self.assertIsNotNone(stored)
            ciphertext = stored.encrypted_access_token or ""
            refresh_ciphertext = stored.encrypted_refresh_token or ""
        self.assertTrue(ciphertext)
        self.assertTrue(refresh_ciphertext)

        for payload in (updated_response.json(), overview_response.json()):
            serialized = str(payload)
            self.assertNotIn(secret, serialized)
            self.assertNotIn(ciphertext, serialized)
            self.assertNotIn(refresh_secret, serialized)
            self.assertNotIn(refresh_ciphertext, serialized)
            self.assertNotIn("access_token", nested_keys(payload))
            self.assertNotIn("encrypted_access_token", nested_keys(payload))
            self.assertNotIn("refresh_token", nested_keys(payload))
            self.assertNotIn("encrypted_refresh_token", nested_keys(payload))

    async def test_channel_discovery_sets_one_balance_and_derives_each_account_rate(self) -> None:
        overview = await self.service.overview(self.db)
        channel_id = overview.channels[0].id
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(access_token="channel-management-token"),
        )
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        alpha_key = "sk-channel-alpha-private-A1B2"
        beta_key = "sk-channel-beta-private-C3D4"
        by_id[7].encrypted_api_key = encrypt_text(alpha_key)
        self.sub2api.exported_api_keys = {8: beta_key}
        await self.db.commit()

        result = SimpleNamespace(
            upstream_type="newapi",
            status="ok",
            groups=[
                {"id": "default", "name": "Default", "multiplier": 1.0, "source": "self.groups"},
                {"id": "vip", "name": "VIP", "multiplier": 2.0, "source": "self.groups"},
            ],
            account_group_matches={
                7: {"id": "default", "name": "Default", "multiplier": 1.0},
                8: {"id": "vip", "name": "VIP", "multiplier": 2.0},
            },
            discovered_recharge_multiplier=0.1,
            discovered_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            balance_remaining=1476.34,
            balance_total=None,
            balance_used=123.66,
            balance_unit="USD",
            balance_status="ok",
            balance_message="Balance available.",
        )
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ) as discover:
            channel = await self.service.discover_channel(self.db, channel_id)

        discover.assert_awaited_once()
        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(
            discover.await_args.kwargs["account_api_keys"],
            {7: alpha_key, 8: beta_key},
        )
        self.assertEqual(channel.balance_remaining, 1476.34)
        self.assertEqual([account.target_rate for account in channel.accounts], [1.0, 2.0])
        self.assertEqual(
            [(account.selected_group_id, account.selected_group_name) for account in channel.accounts],
            [("default", "Default"), ("vip", "VIP")],
        )
        self.assertTrue(all(account.group_multiplier_source == "upstream_key" for account in channel.accounts))
        await self.db.refresh(by_id[8])
        self.assertEqual(decrypt_text(by_id[8].encrypted_api_key), beta_key)
        self.assertNotEqual(by_id[8].encrypted_api_key, beta_key)
        self.sub2api.export_calls.clear()
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            await self.service.discover_channel(self.db, channel_id)
        self.assertEqual(self.sub2api.export_calls, [])
        serialized = str(channel.model_dump())
        for secret in (alpha_key, beta_key, by_id[7].encrypted_api_key or ""):
            self.assertNotIn(secret, serialized)

    async def test_missing_recharge_field_clears_stale_discovered_value(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        channel = await self.db.get(UpstreamChannel, channel_id)
        self.assertIsNotNone(channel)
        channel.discovered_recharge_multiplier = 0.25
        channel.effective_recharge_multiplier = 0.25
        channel.recharge_multiplier_source = "payment.config"
        channel.recharge_multiplier_status = "ok"
        await self.db.commit()

        result = self._discovery_result()
        result.discovered_recharge_multiplier = None
        result.discovered_recharge_multiplier_source = None
        result.recharge_discovery_status = "missing"
        result.account_group_matches = {}
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, channel_id)

        self.assertIsNone(discovered.discovered_recharge_multiplier)
        self.assertEqual(discovered.effective_recharge_multiplier, 1.0)
        self.assertEqual(discovered.recharge_multiplier_source, "default")
        self.assertEqual(discovered.recharge_multiplier_status, "default_missing")

    async def test_unmatched_key_does_not_reuse_historical_group_for_billing(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(access_token="channel-management-token"),
        )
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        by_id[7].selected_group_id = "default"
        by_id[7].selected_group_name = "Default"
        by_id[7].encrypted_api_key = encrypt_text("sk-unmatched-private-E5F6")
        await self.db.commit()
        result = SimpleNamespace(
            upstream_type="newapi",
            status="ok",
            groups=[
                {"id": "default", "name": "Default", "multiplier": 1.0},
                {"id": "vip", "name": "VIP", "multiplier": 2.0},
            ],
            account_group_matches={},
            discovered_recharge_multiplier=0.1,
            discovered_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            balance_remaining=10,
            balance_total=None,
            balance_used=None,
            balance_unit="USD",
            balance_status="ok",
            balance_message="Balance available.",
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            channel = await self.service.discover_channel(self.db, channel_id)

        account = next(item for item in channel.accounts if item.sub2api_account_id == 7)
        self.assertEqual((account.selected_group_id, account.selected_group_name), (None, None))
        self.assertIsNone(account.target_rate)
        self.assertEqual(account.group_multiplier_status, "group_selection_missing")

    async def test_management_url_is_used_without_changing_inference_url(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]
        await self.service.update_channel(
            self.db,
            channel.id,
            UpstreamChannelUpdate(
                management_base_url="https://management.example/api/v1",
                upstream_type="sub2api",
                access_token="management-token",
                confirm_credential_rebind=True,
            ),
        )
        result = self._discovery_result()
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ) as discover:
            discovered = await self.service.discover_channel(self.db, channel.id)

        self.assertEqual(discover.await_args.kwargs["base_url"], "https://management.example")
        self.assertEqual(discovered.canonical_base_url, "https://upstream.example")
        self.assertEqual(discovered.management_base_url, "https://management.example")

    async def test_channel_origin_change_requires_explicit_credential_rebind_confirmation(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.update_channel(
                self.db,
                channel.id,
                UpstreamChannelUpdate(
                    management_base_url="https://replacement.example/api/v1",
                ),
            )

        self.assertEqual(context.exception.status_code, 409)
        await self.db.rollback()
        stored = await self.db.get(UpstreamChannel, channel.id)
        self.assertIsNotNone(stored)
        self.assertIsNone(stored.management_base_url)

    async def test_failed_management_discovery_uses_one_api_key_balance_without_summing(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]
        self.sub2api.balance_results = {
            7: {"status": "ok", "remaining": 29.58802934, "unit": "USD"},
            8: {"status": "ok", "remaining": 29.58802934, "unit": "USD"},
        }
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result(status="error")),
        ):
            discovered = await self.service.discover_channel(self.db, channel.id)

        self.assertEqual(discovered.balance_status, "ok")
        self.assertAlmostEqual(discovered.balance_remaining or 0, 29.58802934)
        self.assertIn("API Key balance", discovered.balance_message or "")
        self.assertTrue(all(account.target_rate is None for account in discovered.accounts))

    async def test_enabled_sync_applies_changed_rate_once_and_records_safe_logs(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(access_token="channel-management-token"),
        )
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        by_id[7].encrypted_api_key = encrypt_text("sk-alpha-auto-sync")
        by_id[8].encrypted_api_key = encrypt_text("sk-beta-auto-sync")
        await self.db.commit()
        result = SimpleNamespace(
            upstream_type="newapi",
            status="ok",
            groups=[
                {"id": "default", "name": "Default", "multiplier": 1.0},
                {"id": "vip", "name": "VIP", "multiplier": 2.0},
            ],
            account_group_matches={
                7: {"id": "default", "name": "Default", "multiplier": 1.0},
                8: {"id": "vip", "name": "VIP", "multiplier": 2.0},
            },
            discovered_recharge_multiplier=0.1,
            discovered_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            balance_remaining=100,
            balance_total=None,
            balance_used=None,
            balance_unit="USD",
            balance_status="ok",
            balance_message="Balance available.",
        )
        runtime = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
        )
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.services.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
        ):
            first = await self.service.discover_channel(self.db, channel_id)
            second = await self.service.discover_channel(self.db, channel_id)

        self.assertEqual(self.sub2api.rate_update_calls, [(8, 2.0)])
        self.assertEqual([item.current_rate for item in first.accounts], [1.0, 2.0])
        self.assertEqual([item.current_rate for item in second.accounts], [1.0, 2.0])
        logs_result = await self.db.execute(
            select(UpstreamRateChangeLog).order_by(UpstreamRateChangeLog.sub2api_account_id)
        )
        logs = list(logs_result.scalars().all())
        self.assertEqual([(item.sub2api_account_id, item.status) for item in logs], [(7, "observed"), (8, "applied")])
        self.assertTrue(all(item.safe_error is None for item in logs))
        self.assertEqual([item.old_upstream_multiplier for item in logs], [None, None])
        self.assertEqual(
            [item.new_upstream_multiplier for item in logs],
            [0.1, 0.2],
        )

    async def test_pause_is_rechecked_before_each_remote_rate_write(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        by_id[7].encrypted_api_key = encrypt_text("sk-alpha-paused-sync")
        by_id[8].encrypted_api_key = encrypt_text("sk-beta-paused-sync")
        await self.db.commit()
        result = SimpleNamespace(
            upstream_type="newapi",
            status="ok",
            groups=[{"id": "vip", "name": "VIP", "multiplier": 3.0}],
            account_group_matches={
                7: {"id": "vip", "name": "VIP", "multiplier": 3.0},
                8: {"id": "vip", "name": "VIP", "multiplier": 3.0},
            },
            discovered_recharge_multiplier=1.0,
            discovered_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            balance_remaining=100,
            balance_total=None,
            balance_used=None,
            balance_unit="USD",
            balance_status="ok",
            balance_message="Balance available.",
        )
        runtime = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=True),
        )

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.services.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
        ):
            await self.service.discover_channel(self.db, channel_id)

        self.assertEqual(self.sub2api.rate_update_calls, [])

    async def test_pause_is_rechecked_after_current_rate_read(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        result = self._discovery_result()
        result.account_group_matches = {
            7: {"id": "default", "name": "Default", "multiplier": 1.0},
            8: {"id": "default", "name": "Default", "multiplier": 1.0},
        }
        pause_checks = 0

        async def get_automation_paused() -> bool:
            nonlocal pause_checks
            pause_checks += 1
            return pause_checks >= 2

        runtime = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(side_effect=get_automation_paused),
        )
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.services.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
        ):
            await self.service.discover_channel(self.db, channel_id)

        self.assertGreaterEqual(pause_checks, 2)
        self.assertEqual(self.sub2api.rate_update_calls, [])

    async def test_cancellation_releases_account_rate_lock(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        result = self._discovery_result()
        result.account_group_matches = {
            7: {"id": "default", "name": "Default", "multiplier": 1.0},
            8: {"id": "default", "name": "Default", "multiplier": 1.0},
        }
        runtime = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
        )
        started = asyncio.Event()
        never = asyncio.Event()
        blocked_account_id: int | None = None
        original_get_rate = self.sub2api.get_account_current_rate_multiplier

        async def blocking_get_rate(account_id: str | int) -> float:
            nonlocal blocked_account_id
            if blocked_account_id is None:
                blocked_account_id = int(account_id)
                started.set()
                await never.wait()
            return await original_get_rate(account_id)

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.services.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
            patch.object(
                self.sub2api,
                "get_account_current_rate_multiplier",
                new=AsyncMock(side_effect=blocking_get_rate),
            ),
        ):
            task = asyncio.create_task(self.service.discover_channel(self.db, channel_id))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertIsNotNone(blocked_account_id)
        account_lock = await self.service.accounts._lock_for(blocked_account_id or 0)
        await asyncio.wait_for(account_lock.acquire(), timeout=1)
        account_lock.release()

    async def test_changed_account_inputs_skip_stale_channel_result(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        by_id[7].manual_group_multiplier = 1.0
        await self.db.commit()
        result = self._discovery_result()
        result.account_group_matches = {}
        runtime = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
        )

        async def discover_after_account_update(**_kwargs):
            async with self.session_factory() as concurrent_db:
                await self.service.accounts.upsert_account(
                    concurrent_db,
                    7,
                    UpstreamAccountUpdate(manual_group_multiplier=4.0),
                )
            return result

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(side_effect=discover_after_account_update),
            ),
            patch(
                "app.services.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
        ):
            discovered = await self.service.discover_channel(self.db, channel_id)

        account = next(item for item in discovered.accounts if item.sub2api_account_id == 7)
        self.assertEqual(account.manual_group_multiplier, 4.0)
        self.assertIsNone(account.target_rate)
        self.assertFalse(any(account_id == 7 for account_id, _rate in self.sub2api.rate_update_calls))

    async def test_concurrent_api_key_save_wins_over_export(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        stale_exported_key = "sk-exported-stale"
        newly_saved_key = "sk-user-saved"

        async def export_after_account_save(account_ids: list[int]) -> dict[int, str]:
            self.sub2api.export_calls.append(list(account_ids))
            async with self.session_factory() as concurrent_db:
                await self.service.accounts.upsert_account(
                    concurrent_db,
                    8,
                    UpstreamAccountUpdate(api_key=newly_saved_key),
                )
            return {8: stale_exported_key} if 8 in account_ids else {}

        discover = AsyncMock(return_value=self._discovery_result(status="error"))
        with (
            patch.object(
                self.sub2api,
                "export_api_key_secrets",
                new=AsyncMock(side_effect=export_after_account_save),
            ),
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=discover,
            ),
        ):
            await self.service.discover_channel(self.db, channel_id)

        self.assertEqual(discover.await_args.kwargs["account_api_keys"].get(8), newly_saved_key)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(decrypt_text(stored.encrypted_api_key), newly_saved_key)

    async def test_api_key_export_is_batched_at_sub2api_limit(self) -> None:
        for account_id in range(9, 208):
            self.sub2api.accounts.append(
                {
                    "id": account_id,
                    "name": f"account-{account_id}",
                    "platform": "openai",
                    "type": "apikey",
                    "status": "active",
                    "rate_multiplier": 1.0,
                    "credentials": {"base_url": "https://upstream.example/v1"},
                }
            )
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs = await self.service._bound_configs(self.db, channel_id)

        await self.service._account_api_keys(self.db, configs, channel_id)

        self.assertEqual([len(batch) for batch in self.sub2api.export_calls], [200, 1])
        self.assertEqual(
            [account_id for batch in self.sub2api.export_calls for account_id in batch],
            list(range(7, 208)),
        )

    async def test_rebind_is_not_overwritten_by_token_or_url_propagation(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        other_token = "other-channel-token"
        retry_api_key = "sk-saved-during-refresh"
        other_channel = UpstreamChannel(
            display_name="Other upstream",
            canonical_base_url="https://other.example",
            upstream_type="sub2api",
            encrypted_access_token=encrypt_text(other_token),
            group_options=[],
            recharge_multiplier_status="not_discovered",
            balance_status="not_checked",
        )
        self.db.add(other_channel)
        await self.db.commit()
        await self.db.refresh(other_channel)

        async def refresh_after_rebind(_base_url: str, _refresh_token: str) -> Sub2ApiTokenPair:
            async with self.session_factory() as concurrent_db:
                await self.service.accounts.upsert_account(
                    concurrent_db,
                    7,
                    UpstreamAccountUpdate(
                        channel_id=other_channel.id,
                        confirm_credential_rebind=True,
                    ),
                )
                await self.service.accounts.upsert_account(
                    concurrent_db,
                    8,
                    UpstreamAccountUpdate(api_key=retry_api_key),
                )
            return Sub2ApiTokenPair(
                access_token="rotated-source-token",
                refresh_token="rotated-source-refresh",
            )

        discover = AsyncMock(
            side_effect=[
                self._discovery_result(status="error", auth_rejected=True),
                self._discovery_result(),
            ]
        )
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=discover,
            ),
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(side_effect=refresh_after_rebind),
            ),
        ):
            await self.service.discover_channel(self.db, channel_id)

        retry_keys = discover.await_args_list[1].kwargs["account_api_keys"]
        self.assertNotIn(7, retry_keys)
        self.assertEqual(retry_keys.get(8), retry_api_key)

        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(
                base_url="https://changed-source.example",
                confirm_credential_rebind=True,
            ),
        )

        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 7
                    )
                )
            ).scalar_one()
            self.assertEqual(stored.channel_id, other_channel.id)
            self.assertEqual(stored.base_url, other_channel.canonical_base_url)
            self.assertEqual(decrypt_text(stored.encrypted_access_token), other_token)
            stored_retry_account = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(decrypt_text(stored_retry_account.encrypted_api_key), retry_api_key)

    async def test_rate_logs_normalize_group_and_recharge_and_track_each_input_change(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(access_token="channel-management-token"),
        )

        runtime = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
        )

        async def discover(*, group: float, recharge: float) -> None:
            result = SimpleNamespace(
                upstream_type="newapi",
                status="ok",
                groups=[{"id": "default", "name": "Default", "multiplier": group}],
                account_group_matches={
                    7: {"id": "default", "name": "Default", "multiplier": group},
                    8: {"id": "default", "name": "Default", "multiplier": group},
                },
                discovered_recharge_multiplier=recharge,
                discovered_recharge_multiplier_source="status.price",
                recharge_discovery_status="ok",
                balance_remaining=100,
                balance_total=None,
                balance_used=None,
                balance_unit="USD",
                balance_status="ok",
                balance_message="Balance available.",
            )
            with (
                patch(
                    "app.services.upstream_channels.discover_upstream",
                    new=AsyncMock(return_value=result),
                ),
                patch(
                    "app.services.upstream_channels.get_runtime_config_service",
                    return_value=runtime,
                ),
            ):
                await self.service.discover_channel(self.db, channel_id)

        await discover(group=1.0, recharge=0.1)
        await discover(group=2.0, recharge=0.1)
        await discover(group=2.0, recharge=0.2)
        self.sub2api.local_credit_per_cny = 5.0
        await discover(group=2.0, recharge=0.2)

        result = await self.db.execute(
            select(UpstreamRateChangeLog)
            .where(UpstreamRateChangeLog.sub2api_account_id == 7)
            .order_by(UpstreamRateChangeLog.id)
        )
        logs = list(result.scalars().all())
        self.assertEqual(
            [item.reason for item in logs],
            [
                "upstream_group_change",
                "upstream_group_change",
                "upstream_recharge_change",
                "local_recharge_change",
            ],
        )
        self.assertEqual(
            [(item.old_upstream_multiplier, item.new_upstream_multiplier) for item in logs],
            [(None, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 0.4)],
        )
        self.assertEqual(
            [
                (
                    item.old_upstream_recharge_multiplier,
                    item.new_upstream_recharge_multiplier,
                )
                for item in logs
            ],
            [(None, 0.1), (0.1, 0.1), (0.1, 0.2), (0.2, 0.2)],
        )
        self.assertEqual(
            [(item.old_target_rate, item.new_target_rate) for item in logs],
            [(None, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 2.0)],
        )

    async def test_auth_me_401_persists_rotated_tokens_before_retrying_once(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        new_access_token = "at-rotated-private"
        new_refresh_token = "rt-rotated-private"
        discovery_calls: list[dict] = []

        async def fake_discovery(**kwargs):
            discovery_calls.append(kwargs)
            if len(discovery_calls) == 1:
                return self._discovery_result(status="error", auth_rejected=True)

            # A separate session can only see the pair after the durability
            # commit that must precede the retried upstream request.
            async with self.session_factory() as verifier:
                persisted = await verifier.get(UpstreamChannel, channel_id)
                self.assertIsNotNone(persisted)
                self.assertEqual(decrypt_text(persisted.encrypted_access_token), new_access_token)
                self.assertEqual(decrypt_text(persisted.encrypted_refresh_token), new_refresh_token)
                configs_result = await verifier.execute(
                    select(UpstreamAccountConfig).where(UpstreamAccountConfig.channel_id == channel_id)
                )
                persisted_configs = list(configs_result.scalars().all())
                self.assertTrue(persisted_configs)
                self.assertTrue(
                    all(
                        decrypt_text(config.encrypted_access_token) == new_access_token
                        for config in persisted_configs
                    )
                )
            return self._discovery_result(balance_remaining=46.226)

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(side_effect=fake_discovery),
            ) as discover,
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(
                    return_value=Sub2ApiTokenPair(
                        access_token=new_access_token,
                        refresh_token=new_refresh_token,
                        expires_in=3600,
                    )
                ),
            ) as refresh,
        ):
            channel = await self.service.discover_channel(self.db, channel_id)

        self.assertEqual(discover.await_count, 2)
        self.assertEqual(discovery_calls[0]["access_token"], "at-old-private")
        self.assertEqual(discovery_calls[1]["access_token"], new_access_token)
        refresh.assert_awaited_once_with("https://upstream.example", "rt-old-private")
        self.assertEqual(channel.balance_remaining, 46.226)
        self.assertTrue(channel.refresh_token_set)

    async def test_refresh_failure_preserves_old_credentials(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        before = await self.db.get(UpstreamChannel, channel_id)
        self.assertIsNotNone(before)
        old_access_ciphertext = before.encrypted_access_token
        old_refresh_ciphertext = before.encrypted_refresh_token

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(
                    return_value=self._discovery_result(status="error", auth_rejected=True)
                ),
            ) as discover,
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(return_value=None),
            ) as refresh,
        ):
            await self.service.discover_channel(self.db, channel_id)

        stored = await self.db.get(UpstreamChannel, channel_id)
        await self.db.refresh(stored)
        self.assertEqual(stored.encrypted_access_token, old_access_ciphertext)
        self.assertEqual(stored.encrypted_refresh_token, old_refresh_ciphertext)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), "at-old-private")
        self.assertEqual(decrypt_text(stored.encrypted_refresh_token), "rt-old-private")
        self.assertEqual(discover.await_count, 1)
        refresh.assert_awaited_once_with("https://upstream.example", "rt-old-private")

    async def test_failed_retry_keeps_rotated_tokens_without_refresh_loop(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        new_access_token = "at-rotated-after-failed-retry"
        new_refresh_token = "rt-rotated-after-failed-retry"
        first = self._discovery_result(status="error", auth_rejected=True)
        second = self._discovery_result(status="error", auth_rejected=True)

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(side_effect=[first, second]),
            ) as discover,
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(
                    return_value=Sub2ApiTokenPair(
                        access_token=new_access_token,
                        refresh_token=new_refresh_token,
                    )
                ),
            ) as refresh,
        ):
            channel = await self.service.discover_channel(self.db, channel_id)

        stored = await self.db.get(UpstreamChannel, channel_id)
        await self.db.refresh(stored)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), new_access_token)
        self.assertEqual(decrypt_text(stored.encrypted_refresh_token), new_refresh_token)
        self.assertEqual(discover.await_count, 2)
        refresh.assert_awaited_once()
        self.assertEqual(channel.balance_status, "error")

    async def test_newapi_auth_failure_never_uses_sub2api_refresh_token(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(upstream_type="newapi"),
        )
        result = self._discovery_result(status="error", auth_rejected=True)
        result.upstream_type = "newapi"

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(),
            ) as refresh,
        ):
            await self.service.discover_channel(self.db, channel_id)

        refresh.assert_not_awaited()

    async def test_non_auth_me_401_signal_does_not_refresh_sub2api(self) -> None:
        channel_id = await self._configure_sub2api_credentials()

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(
                    return_value=self._discovery_result(status="error", auth_rejected=False)
                ),
            ),
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(),
            ) as refresh,
        ):
            await self.service.discover_channel(self.db, channel_id)

        refresh.assert_not_awaited()


class UpstreamChannelAuthenticationTests(unittest.TestCase):
    def test_every_route_requires_admin_session(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = fake_db
        requests = [
            ("GET", "/api/upstream-channels", None),
            ("POST", "/api/upstream-channels/discover-all", None),
            ("PUT", "/api/upstream-channels/1", {"display_name": "test"}),
            ("POST", "/api/upstream-channels/1/discover", None),
        ]
        with TestClient(app) as client:
            for method, url, body in requests:
                response = client.request(method, url, json=body)
                self.assertEqual(response.status_code, 401, (method, url, response.text))

    def test_malformed_access_token_is_redacted_from_validation_response(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)
        app.include_router(router, prefix="/api/upstream-channels")
        secret = "fictional-malformed-channel-token"
        service = SimpleNamespace(update_channel=AsyncMock())

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: service
        with TestClient(app) as client:
            response = client.put(
                "/api/upstream-channels/1",
                json={"access_token": {"value": secret}},
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertIn("[redacted]", response.text)
        service.update_channel.assert_not_awaited()

    def test_malformed_refresh_token_is_redacted_from_validation_response(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)
        app.include_router(router, prefix="/api/upstream-channels")
        secret = "fictional-malformed-refresh-token"
        service = SimpleNamespace(update_channel=AsyncMock())

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: service
        with TestClient(app) as client:
            response = client.put(
                "/api/upstream-channels/1",
                json={"refresh_token": {"value": secret}},
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertIn("[redacted]", response.text)
        service.update_channel.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
