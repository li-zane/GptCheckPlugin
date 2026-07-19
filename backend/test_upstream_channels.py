from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

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
from app.models import (
    UpstreamAccountConfig,
    UpstreamChannel,
    UpstreamPriorityInterval,
    UpstreamRateChangeLog,
)
from app.schemas import (
    UpstreamAccountUpdate,
    UpstreamChannelDiscoverAllRequest,
    UpstreamChannelDiscoverAllOut,
    UpstreamChannelUpdate,
    UpstreamOverviewOut,
)
from app.services.sub2api import Sub2ApiClient
from app.services.upstream_accounts import UpstreamAccountServiceError
from app.services.upstream_channels import UpstreamChannelService, get_upstream_channel_service
from app.services.upstream_client import AccountUpstreamState, Sub2ApiTokenPair


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
                "schedulable": True,
                "created_at": "2026-07-01T00:00:07Z",
                "rate_multiplier": 1.0,
                "credentials": {"base_url": "https://UPSTREAM.example/v1"},
            },
            {
                "id": 8,
                "name": "beta",
                "platform": "anthropic",
                "type": "api_key",
                "status": "active",
                "schedulable": True,
                "created_at": "2026-07-01T00:00:08Z",
                "rate_multiplier": 0.5,
                "credentials": {"base_url": "https://upstream.example/api/v1/"},
            },
        ]
        self.exported_api_keys: dict[int, str] = {}
        self.export_calls: list[list[int]] = []
        self.balance_results: dict[int, dict] = {}
        self.rate_update_calls: list[tuple[int, float]] = []
        self.schedulable_calls: list[tuple[int, bool]] = []
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

    async def set_account_schedulable(self, account_id: str | int, schedulable: bool) -> None:
        parsed_id = int(account_id)
        self.schedulable_calls.append((parsed_id, schedulable))
        account = next(item for item in self.accounts if int(item["id"]) == parsed_id)
        account["schedulable"] = schedulable


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
            get_public_settings=AsyncMock(
                return_value={"display_timezone": "Asia/Shanghai"}
            ),
            get_upstream_rate_sync_enabled=AsyncMock(return_value=False),
            get_automation_paused=AsyncMock(return_value=False),
            get_api_key_auto_disable_on_upstream_unavailable=AsyncMock(return_value=False),
            get_upstream_priority_sync_enabled=AsyncMock(return_value=True),
        )
        self.runtime_config_patcher = patch(
            "app.services.upstream_channels.get_runtime_config_service",
            return_value=self.runtime_config,
        )
        self.runtime_config_patcher.start()
        self.addCleanup(self.runtime_config_patcher.stop)
        self.account_runtime_config_patcher = patch(
            "app.services.upstream_accounts.get_runtime_config_service",
            return_value=self.runtime_config,
        )
        self.account_runtime_config_patcher.start()
        self.addCleanup(self.account_runtime_config_patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    def _account_update(self, account_id: int, **values: object) -> UpstreamAccountUpdate:
        remote = next(account for account in self.sub2api.accounts if int(account["id"]) == account_id)
        return UpstreamAccountUpdate(
            expected_identity_fingerprint=self.service.accounts._remote_identity_fingerprint(remote),
            **values,
        )

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
        account_upstream_states: dict[int, AccountUpstreamState] | None = None,
        today_balance_used: float | None = 3.25,
        today_balance_status: str = "ok",
        yesterday_balance_used: float | None = 2.75,
        yesterday_balance_status: str = "ok",
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
            today_balance_used=today_balance_used,
            today_balance_unit="USD" if today_balance_used is not None else None,
            today_balance_status=today_balance_status,
            yesterday_balance_used=yesterday_balance_used,
            yesterday_balance_unit="USD" if yesterday_balance_used is not None else None,
            yesterday_balance_status=yesterday_balance_status,
            account_upstream_states=account_upstream_states or {},
        )

    async def test_overview_groups_equivalent_v1_urls_into_one_channel(self) -> None:
        overview = await self.service.overview(self.db)

        self.assertEqual(len(overview.channels), 1)
        self.assertEqual(overview.channels[0].canonical_base_url, "https://upstream.example")
        self.assertEqual([item.sub2api_account_id for item in overview.channels[0].accounts], [7, 8])
        self.assertEqual(
            [item.remote_platform for item in overview.channels[0].accounts],
            ["openai", "anthropic"],
        )
        self.assertEqual(overview.local_recharge_multiplier, 0.1)
        self.assertEqual(overview.unassigned_accounts, [])

        again = await self.service.overview(self.db)
        self.assertEqual(len(again.channels), 1)
        channels = await self.db.execute(select(UpstreamChannel))
        self.assertEqual(len(channels.scalars().all()), 1)

    async def test_overview_includes_empty_channel_and_delete_removes_it(self) -> None:
        empty_channel = UpstreamChannel(
            display_name="Old upstream",
            canonical_base_url="https://old-upstream.example",
            upstream_type="auto",
            group_options=[],
            recharge_multiplier_status="not_discovered",
            balance_status="not_checked",
        )
        self.db.add(empty_channel)
        await self.db.commit()

        overview = await self.service.overview(self.db)
        empty = next(channel for channel in overview.channels if channel.id == empty_channel.id)
        self.assertEqual(empty.account_count, 0)
        self.assertEqual(empty.accounts, [])

        await self.service.delete_channel(self.db, empty_channel.id)

        self.assertIsNone(await self.db.get(UpstreamChannel, empty_channel.id))

    async def test_delete_channel_rejects_current_api_key_accounts(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id

        with self.assertRaises(UpstreamAccountServiceError) as caught:
            await self.service.delete_channel(self.db, channel_id)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNotNone(await self.db.get(UpstreamChannel, channel_id))

    async def test_delete_channel_rejects_unsynced_account_with_same_origin(self) -> None:
        empty_channel = UpstreamChannel(
            display_name="New upstream",
            canonical_base_url="https://new-upstream.example",
            upstream_type="auto",
        )
        self.db.add(empty_channel)
        await self.db.commit()
        self.sub2api.accounts.append(
            {
                "id": 9,
                "name": "not-yet-synced",
                "platform": "openai",
                "type": "api_key",
                "status": "active",
                "schedulable": True,
                "credentials": {"base_url": "https://new-upstream.example/v1"},
            }
        )

        with self.assertRaises(UpstreamAccountServiceError) as caught:
            await self.service.delete_channel(self.db, empty_channel.id)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNotNone(await self.db.get(UpstreamChannel, empty_channel.id))

    async def test_delete_empty_channel_removes_stale_account_configs(self) -> None:
        empty_channel = UpstreamChannel(
            display_name="Stale upstream",
            canonical_base_url="https://stale-upstream.example",
            upstream_type="auto",
        )
        self.db.add(empty_channel)
        await self.db.flush()
        stale_config = UpstreamAccountConfig(
            sub2api_account_id=999,
            remote_name="Removed account",
            channel_id=empty_channel.id,
        )
        self.db.add(stale_config)
        await self.db.commit()

        await self.service.delete_channel(self.db, empty_channel.id)

        self.assertIsNone(await self.db.get(UpstreamChannel, empty_channel.id))
        self.assertIsNone(await self.db.get(UpstreamAccountConfig, stale_config.id))

    async def test_read_only_overview_does_not_flush_projected_target_rate(self) -> None:
        overview = await self.service.overview(self.db)
        channel_id = overview.channels[0].id
        config = await self.db.scalar(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.sub2api_account_id == 7
            )
        )
        channel = await self.db.get(UpstreamChannel, channel_id)
        self.assertIsNotNone(config)
        self.assertIsNotNone(channel)
        config.effective_group_multiplier = 1.0
        config.target_rate = 9.0
        channel.effective_recharge_multiplier = 1.0
        await self.db.commit()

        projected = await self.service.overview(self.db, sync_inventory=False)
        account = next(
            item
            for item in projected.channels[0].accounts
            if item.sub2api_account_id == 7
        )
        self.assertEqual(account.target_rate, 10.0)

        await self.db.execute(select(UpstreamChannel.id))
        await self.db.commit()
        async with self.session_factory() as verifier:
            stored_target = await verifier.scalar(
                select(UpstreamAccountConfig.target_rate).where(
                    UpstreamAccountConfig.sub2api_account_id == 7
                )
            )
        self.assertEqual(stored_target, 9.0)

    async def test_concurrent_overview_serializes_inventory_creation(self) -> None:
        async with self.session_factory() as first_db, self.session_factory() as second_db:
            first, second = await asyncio.gather(
                self.service.overview(first_db),
                self.service.overview(second_db),
            )

        self.assertEqual(len(first.channels), 1)
        self.assertEqual(len(second.channels), 1)
        async with self.session_factory() as verifier:
            channels = list((await verifier.execute(select(UpstreamChannel))).scalars())
            configs = list(
                (await verifier.execute(select(UpstreamAccountConfig))).scalars()
            )
        self.assertEqual(len(channels), 1)
        self.assertEqual(len(configs), 2)

    async def test_overview_and_account_discovery_share_config_creation_locks(self) -> None:
        expected_fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )
        async with self.session_factory() as overview_db, self.session_factory() as account_db:
            overview, account = await asyncio.gather(
                self.service.overview(overview_db),
                self.service.accounts.discover_account(
                    account_db,
                    7,
                    expected_fingerprint,
                ),
            )

        self.assertEqual(len(overview.channels), 1)
        self.assertEqual(account.sub2api_account_id, 7)
        async with self.session_factory() as verifier:
            configs = list(
                (
                    await verifier.execute(
                        select(UpstreamAccountConfig).where(
                            UpstreamAccountConfig.sub2api_account_id == 7
                        )
                    )
                ).scalars()
            )
        self.assertEqual(len(configs), 1)

    async def test_overview_and_account_discover_all_share_config_creation_locks(self) -> None:
        async with self.session_factory() as overview_db, self.session_factory() as account_db:
            overview, discovery = await asyncio.gather(
                self.service.overview(overview_db),
                self.service.accounts.discover_all(account_db),
            )

        self.assertEqual(len(overview.channels), 1)
        self.assertEqual(discovery.total, 2)
        async with self.session_factory() as verifier:
            configs = list(
                (await verifier.execute(select(UpstreamAccountConfig))).scalars()
            )
        self.assertEqual(
            sorted(config.sub2api_account_id for config in configs),
            [7, 8],
        )

    async def test_inventory_and_channel_url_update_never_leak_unique_errors(self) -> None:
        overview = await self.service.overview(self.db)
        original_channel_id = overview.channels[0].id
        self.sub2api.accounts[0]["credentials"]["base_url"] = (
            "https://replacement.example/v1"
        )
        self.sub2api.accounts[1]["credentials"]["base_url"] = (
            "https://replacement.example/v1"
        )
        payload = UpstreamChannelUpdate(
            base_url="https://replacement.example",
            confirm_credential_rebind=True,
        )

        async with self.session_factory() as overview_db, self.session_factory() as update_db:
            results = await asyncio.gather(
                self.service.overview(overview_db),
                self.service.update_channel(
                    update_db,
                    original_channel_id,
                    payload,
                ),
                return_exceptions=True,
            )

        for result in results:
            if isinstance(result, Exception):
                self.assertIsInstance(result, UpstreamAccountServiceError)
                self.assertEqual(result.status_code, 409)
        async with self.session_factory() as verifier:
            replacement_channels = list(
                (
                    await verifier.execute(
                        select(UpstreamChannel).where(
                            UpstreamChannel.canonical_base_url
                            == "https://replacement.example"
                        )
                    )
                ).scalars()
            )
        self.assertEqual(len(replacement_channels), 1)

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
        stored.upstream_usage_amount = 12.375
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = stored.created_at
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
        self.assertTrue(replacement.accounts[0].api_key_origin_rebind_required)
        self.assertFalse(replacement.access_token_set)

        await self.db.refresh(stored)
        self.assertEqual(stored.channel_id, replacement.id)
        self.assertEqual(stored.base_url, "https://replacement.example")
        self.assertIsNone(stored.encrypted_api_key)
        self.assertIsNone(stored.encrypted_access_token)
        self.assertIsNone(stored.upstream_usage_amount)
        self.assertIsNone(stored.upstream_usage_unit)
        self.assertIsNone(stored.upstream_usage_checked_at)
        self.assertTrue(stored.api_key_origin_rebind_required)
        self.assertIsNone(stored.selected_group_id)
        self.assertIsNone(stored.selected_group_name)
        self.assertIsNone(stored.manual_group_multiplier)
        self.assertIsNone(stored.target_rate)
        self.assertEqual(
            stored.last_error,
            "The upstream endpoint changed; rediscovery is required.",
        )

        bound = await self.service.accounts.bind_legacy_identities(
            self.db,
            {7: replacement.accounts[0].identity_fingerprint},
        )
        self.assertEqual(bound, 1)
        await self.db.refresh(stored)
        self.assertFalse(stored.api_key_origin_rebind_required)

    async def test_reused_id_is_quarantined_from_overview_and_background_rate_sync(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        original_ciphertext = encrypt_text("sk-original-seven")
        by_id[7].encrypted_api_key = original_ciphertext
        by_id[7].upstream_usage_amount = 12.375
        by_id[7].upstream_usage_unit = "USD"
        by_id[7].upstream_usage_checked_at = by_id[7].created_at
        by_id[8].encrypted_api_key = encrypt_text("sk-current-eight")
        await self.db.commit()

        self.sub2api.accounts[0]["name"] = "replacement-seven"
        self.sub2api.accounts[0]["created_at"] = "2026-07-15T12:00:00Z"
        overview = await self.service.overview(self.db)

        self.assertEqual(
            [account.sub2api_account_id for account in overview.channels[0].accounts],
            [7, 8],
        )
        self.assertEqual(overview.unassigned_accounts, [])
        isolated = overview.channels[0].accounts[0]
        self.assertEqual(isolated.identity_binding_status, "mismatch")
        self.assertTrue(isolated.identity_rebind_required)
        self.assertFalse(isolated.managed)
        self.assertFalse(isolated.api_key_set)
        await self.db.refresh(by_id[7])
        self.assertEqual(by_id[7].encrypted_api_key, original_ciphertext)
        self.assertIsNone(by_id[7].upstream_usage_amount)
        self.assertIsNone(by_id[7].upstream_usage_unit)
        self.assertIsNone(by_id[7].upstream_usage_checked_at)

        result = SimpleNamespace(
            upstream_type="newapi",
            status="ok",
            groups=[{"id": "vip", "name": "VIP", "multiplier": 2.0}],
            account_group_matches={
                7: {"id": "vip", "name": "VIP", "multiplier": 2.0},
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
        discovery = AsyncMock(return_value=result)
        with (
            patch("app.services.upstream_channels.discover_upstream", new=discovery),
            patch(
                "app.services.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
        ):
            await self.service.discover_channel(self.db, channel.id)

        self.assertEqual(discovery.await_args.kwargs["account_api_keys"], {8: "sk-current-eight"})
        self.assertNotIn(7, [account_id for account_id, _rate in self.sub2api.rate_update_calls])
        await self.db.refresh(by_id[7])
        self.assertEqual(by_id[7].encrypted_api_key, original_ciphertext)

    async def test_remote_name_only_change_reconciles_cached_name_and_binding(self) -> None:
        await self.service.overview(self.db)
        stored = (
            await self.db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == 7
                )
            )
        ).scalar_one()
        original_ciphertext = encrypt_text("sk-name-reconcile")
        stored.encrypted_api_key = original_ciphertext
        await self.db.commit()

        self.sub2api.accounts[0]["name"] = "renamed-outside-the-plugin"
        overview = await self.service.overview(self.db)
        account = next(
            account
            for channel in overview.channels
            for account in channel.accounts
            if account.sub2api_account_id == 7
        )

        await self.db.refresh(stored)
        self.assertEqual(account.remote_name, "renamed-outside-the-plugin")
        self.assertEqual(account.identity_binding_status, "bound")
        self.assertTrue(account.managed)
        self.assertTrue(account.api_key_set)
        self.assertEqual(stored.remote_name, "renamed-outside-the-plugin")
        self.assertEqual(
            stored.remote_identity_fingerprint,
            self.service.accounts._remote_binding_fingerprint(self.sub2api.accounts[0]),
        )
        self.assertEqual(stored.encrypted_api_key, original_ciphertext)

    async def test_remote_name_reconciliation_redacts_known_credentials(self) -> None:
        await self.service.overview(self.db)
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        api_key = "sk-name-must-stay-encrypted"
        access_token = "at-name-must-stay-encrypted"
        by_id[7].encrypted_api_key = encrypt_text(api_key)
        by_id[8].encrypted_access_token = encrypt_text(access_token)
        await self.db.commit()

        self.sub2api.accounts[0]["name"] = api_key
        self.sub2api.accounts[1]["name"] = access_token
        overview = await self.service.overview(self.db)
        projected = {
            account.sub2api_account_id: account
            for channel in overview.channels
            for account in channel.accounts
        }

        await self.db.refresh(by_id[7])
        await self.db.refresh(by_id[8])
        self.assertEqual(by_id[7].remote_name, "[redacted]")
        self.assertEqual(by_id[8].remote_name, "[redacted]")
        self.assertEqual(projected[7].remote_name, "[redacted]")
        self.assertEqual(projected[8].remote_name, "[redacted]")
        self.assertEqual(decrypt_text(by_id[7].encrypted_api_key), api_key)
        self.assertEqual(decrypt_text(by_id[8].encrypted_access_token), access_token)

        self.sub2api.accounts[0]["name"] = "ordinary-renamed-seven"
        self.sub2api.accounts[1]["name"] = "ordinary-renamed-eight"
        ordinary = await self.service.overview(self.db)
        ordinary_by_id = {
            account.sub2api_account_id: account
            for channel in ordinary.channels
            for account in channel.accounts
        }
        self.assertEqual(ordinary_by_id[7].remote_name, "ordinary-renamed-seven")
        self.assertEqual(ordinary_by_id[8].remote_name, "ordinary-renamed-eight")
        self.assertEqual(ordinary_by_id[7].identity_binding_status, "bound")
        self.assertEqual(ordinary_by_id[8].identity_binding_status, "bound")

    async def test_remote_name_reconciliation_recovers_from_a_redacted_channel_token(self) -> None:
        refresh_secret = "rt-name-fingerprint-secret"
        await self._configure_sub2api_credentials(refresh_token=refresh_secret)
        remote = self.sub2api.accounts[0]

        remote["name"] = refresh_secret
        secret_overview = await self.service.overview(self.db)
        secret_account = next(
            account
            for channel in secret_overview.channels
            for account in channel.accounts
            if account.sub2api_account_id == int(remote["id"])
        )
        self.assertEqual(secret_account.remote_name, "[redacted]")

        remote["name"] = "ordinary-name-after-secret"
        ordinary_overview = await self.service.overview(self.db)
        ordinary_account = next(
            account
            for channel in ordinary_overview.channels
            for account in channel.accounts
            if account.sub2api_account_id == int(remote["id"])
        )

        self.assertEqual(ordinary_account.remote_name, "ordinary-name-after-secret")
        self.assertEqual(ordinary_account.identity_binding_status, "bound")
        self.assertTrue(ordinary_account.managed)

    async def test_inventory_scrubs_bound_plaintext_remote_name(self) -> None:
        await self.service.overview(self.db)
        stored = (
            await self.db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == 7
                )
            )
        ).scalar_one()
        secret = "sk-historical-remote-name"
        self.sub2api.accounts[0]["name"] = secret
        stored.encrypted_api_key = encrypt_text(secret)
        stored.remote_name = secret
        stored.remote_identity_fingerprint = (
            self.service.accounts._require_remote_binding_fingerprint(
                self.sub2api.accounts[0]
            )
        )
        await self.db.commit()

        overview = await self.service.overview(self.db)
        projected = next(
            account
            for channel in overview.channels
            for account in channel.accounts
            if account.sub2api_account_id == 7
        )

        await self.db.refresh(stored)
        self.assertEqual(stored.remote_name, "[redacted]")
        self.assertEqual(projected.remote_name, "[redacted]")

    async def test_manual_bulk_sync_claims_only_confirmed_legacy_null_bindings(self) -> None:
        overview = await self.service.overview(self.db)
        accounts = [account for channel in overview.channels for account in channel.accounts]
        confirmations = {
            account.sub2api_account_id: account.identity_fingerprint
            for account in accounts
        }
        configs = await self.db.execute(select(UpstreamAccountConfig))
        stored = list(configs.scalars().all())
        for config in stored:
            config.remote_identity_fingerprint = None
        await self.db.commit()

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ):
            result = await self.service.discover_all(
                self.db,
                legacy_bindings=confirmations,
            )

        self.assertEqual(result.total, 1)
        for config in stored:
            await self.db.refresh(config)
            remote = next(
                account
                for account in self.sub2api.accounts
                if int(account["id"]) == config.sub2api_account_id
            )
            self.assertEqual(
                config.remote_identity_fingerprint,
                self.service.accounts._remote_binding_fingerprint(remote),
            )

    def test_confirmation_schema_supports_more_than_500_pending_accounts(self) -> None:
        payload = UpstreamChannelDiscoverAllRequest(
            confirm_legacy_bindings=True,
            account_bindings=[
                {
                    "sub2api_account_id": account_id,
                    "expected_identity_fingerprint": f"{account_id:064x}",
                }
                for account_id in range(1, 502)
            ],
        )

        self.assertEqual(len(payload.account_bindings), 501)

    async def test_one_confirmation_covers_legacy_binding_and_endpoint_migration(self) -> None:
        await self.service.overview(self.db)
        replacement_key = "sk-replacement-eight"
        self.sub2api.accounts[1]["credentials"]["base_url"] = (
            "https://replacement.example/v1"
        )
        self.sub2api.exported_api_keys[8] = replacement_key

        configs = await self.db.execute(select(UpstreamAccountConfig))
        stored = list(configs.scalars().all())
        for config in stored:
            config.remote_identity_fingerprint = None
        await self.db.commit()

        confirmation_overview = await self.service.overview(self.db)
        visible_accounts = [
            account
            for channel in confirmation_overview.channels
            for account in channel.accounts
        ]
        confirmations = {
            account.sub2api_account_id: account.identity_fingerprint
            for account in visible_accounts
        }
        pending = next(
            account for account in visible_accounts if account.sub2api_account_id == 8
        )
        self.assertEqual(pending.identity_binding_status, "unbound")
        self.assertFalse(pending.api_key_origin_rebind_required)

        discovery = AsyncMock(return_value=self._discovery_result())
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=discovery,
        ):
            result = await self.service.discover_all(
                self.db,
                legacy_bindings=confirmations,
            )

        self.assertEqual(result.total, 2)
        async with self.session_factory() as verifier:
            rebound = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(rebound.base_url, "https://replacement.example")
            self.assertFalse(rebound.api_key_origin_rebind_required)
            self.assertEqual(decrypt_text(rebound.encrypted_api_key), replacement_key)

        replacement_call = next(
            call
            for call in discovery.await_args_list
            if call.kwargs["base_url"] == "https://replacement.example"
        )
        self.assertEqual(
            replacement_call.kwargs["account_api_keys"],
            {8: replacement_key},
        )

    async def test_background_sync_never_claims_legacy_null_bindings(self) -> None:
        await self.service.overview(self.db)
        configs = await self.db.execute(select(UpstreamAccountConfig))
        stored = list(configs.scalars().all())
        for config in stored:
            config.remote_identity_fingerprint = None
        await self.db.commit()

        discovery = AsyncMock(return_value=self._discovery_result())
        with patch("app.services.upstream_channels.discover_upstream", new=discovery):
            result = await self.service.discover_all(self.db)

        self.assertEqual((result.total, result.succeeded, result.failed), (1, 0, 1))
        discovery.assert_not_awaited()
        for config in stored:
            await self.db.refresh(config)
            self.assertIsNone(config.remote_identity_fingerprint)

    async def test_live_confirmation_double_checks_before_clearing_origin_rebind(self) -> None:
        overview = await self.service.overview(self.db)
        account = overview.channels[0].accounts[0]
        config = (
            await self.db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == account.sub2api_account_id
                )
            )
        ).scalar_one()
        config.api_key_origin_rebind_required = True
        await self.db.commit()
        original_remote_accounts = self.service.accounts._remote_accounts
        remote_reads = AsyncMock(side_effect=original_remote_accounts)

        with patch.object(
            self.service.accounts,
            "_remote_accounts",
            new=remote_reads,
        ):
            rebound = await self.service.accounts.bind_legacy_identities(
                self.db,
                {account.sub2api_account_id: account.identity_fingerprint},
            )

        self.assertEqual(rebound, 1)
        self.assertEqual(remote_reads.await_count, 2)
        await self.db.refresh(config)
        self.assertFalse(config.api_key_origin_rebind_required)

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

    async def test_account_discovery_redacts_channel_refresh_token_from_remote_name(self) -> None:
        refresh_secret = "rt-remote-name-must-not-leak"
        channel_id = await self._configure_sub2api_credentials(
            refresh_token=refresh_secret,
        )
        remote = self.sub2api.accounts[0]
        remote["name"] = refresh_secret
        config = (
            await self.db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == int(remote["id"])
                )
            )
        ).scalar_one()
        config.remote_identity_fingerprint = (
            self.service.accounts._require_remote_binding_fingerprint(remote)
        )
        await self.db.commit()

        overview = await self.service.overview(self.db)
        projected = next(
            account
            for channel in overview.channels
            for account in channel.accounts
            if account.sub2api_account_id == int(remote["id"])
        )
        listed = await self.service.accounts.list_accounts(self.db)
        listed_account = next(
            account for account in listed if account.sub2api_account_id == int(remote["id"])
        )
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ):
            discovered = await self.service.accounts.discover_account(
                self.db,
                int(remote["id"]),
                listed_account.identity_fingerprint,
            )

        await self.db.refresh(config)
        self.assertEqual(config.channel_id, channel_id)
        self.assertEqual(projected.remote_name, "[redacted]")
        self.assertEqual(listed_account.remote_name, "[redacted]")
        self.assertEqual(discovered.remote_name, "[redacted]")
        self.assertEqual(config.remote_name, "[redacted]")
        self.assertNotIn(refresh_secret, discovered.model_dump_json())

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
            config.upstream_usage_amount = 12.375
            config.upstream_usage_unit = "USD"
            config.upstream_usage_checked_at = channel.updated_at
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
            self.assertIsNone(config.upstream_usage_amount)
            self.assertIsNone(config.upstream_usage_unit)
            self.assertIsNone(config.upstream_usage_checked_at)
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

    async def test_channel_discovery_uses_configured_display_timezone(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        self.runtime_config.get_public_settings.return_value = {
            "display_timezone": "America/New_York"
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ) as discover:
            await self.service.discover_channel(self.db, channel_id)

        discover.assert_awaited_once()
        self.assertEqual(
            discover.await_args.kwargs["today_timezone"],
            "America/New_York",
        )

    async def test_discover_all_syncs_multi_platform_inventory_and_probes_each_channel(self) -> None:
        self.sub2api.exported_api_keys = {
            7: "sk-openai-private-A1B2",
            8: "sk-anthropic-private-C3D4",
        }
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ) as discover:
            result = await self.service.discover_all(self.db)

        self.assertEqual((result.total, result.succeeded, result.failed), (1, 1, 0))
        discover.assert_awaited_once()
        self.assertEqual(
            [account.remote_platform for account in result.channels[0].accounts],
            ["openai", "anthropic"],
        )
        serialized = str(result.model_dump())
        for secret in self.sub2api.exported_api_keys.values():
            self.assertNotIn(secret, serialized)

    async def test_automatic_discovery_does_not_sync_new_api_key_inventory(self) -> None:
        initial = await self.service.overview(self.db)
        initial_channel_ids = {channel.id for channel in initial.channels}
        initial_config_rows = (
            await self.db.execute(select(UpstreamAccountConfig))
        ).scalars().all()
        initial_bindings = {
            row.sub2api_account_id: (row.channel_id, row.remote_identity_fingerprint)
            for row in initial_config_rows
        }
        self.sub2api.accounts.append(
            {
                "id": 9,
                "name": "gamma",
                "platform": "openai",
                "type": "apikey",
                "status": "active",
                "schedulable": True,
                "created_at": "2026-07-01T00:00:09Z",
                "rate_multiplier": 0.75,
                "credentials": {"base_url": "https://new-upstream.example/v1"},
            }
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ):
            automatic = await self.service.discover_all(
                self.db,
                sync_inventory=False,
            )

        self.assertEqual(automatic.total, 1)
        after_automatic_configs = (
            await self.db.execute(select(UpstreamAccountConfig))
        ).scalars().all()
        self.assertEqual(
            {
                row.sub2api_account_id: (row.channel_id, row.remote_identity_fingerprint)
                for row in after_automatic_configs
            },
            initial_bindings,
        )
        after_automatic_channels = (
            await self.db.execute(select(UpstreamChannel))
        ).scalars().all()
        self.assertEqual({channel.id for channel in after_automatic_channels}, initial_channel_ids)

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ):
            manual = await self.service.discover_all(self.db)

        self.assertEqual(manual.total, 2)
        added = await self.db.scalar(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.sub2api_account_id == 9
            )
        )
        self.assertIsNotNone(added)
        self.assertIsNotNone(added.channel_id)

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

    async def test_discovery_persists_key_usage_and_channel_today_balance_use(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        result = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                    usage_amount=12.375,
                    usage_unit="USD",
                )
            },
            today_balance_used=3.25,
        )
        result.account_group_matches = {
            7: {"id": "default", "name": "Default", "multiplier": 1.0}
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, channel_id)

        self.assertEqual(discovered.today_balance_used, 3.25)
        self.assertEqual(discovered.today_balance_unit, "USD")
        self.assertEqual(discovered.today_balance_status, "ok")
        account = next(item for item in discovered.accounts if item.sub2api_account_id == 7)
        self.assertEqual(account.upstream_usage_amount, 12.375)
        self.assertEqual(account.upstream_usage_unit, "USD")
        self.assertIsNotNone(account.upstream_usage_checked_at)

        stored_channel = await self.db.get(UpstreamChannel, channel_id)
        stored_account = await self.db.scalar(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.sub2api_account_id == 7
            )
        )
        self.assertEqual(stored_channel.today_balance_used, 3.25)
        self.assertEqual(stored_channel.yesterday_balance_used, 2.75)
        self.assertEqual(stored_account.upstream_usage_amount, 12.375)

    async def test_successful_discovery_clears_stale_usage_for_unmatched_keys(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs = (
            await self.db.execute(select(UpstreamAccountConfig))
        ).scalars().all()
        checked_at = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        by_id = {config.sub2api_account_id: config for config in configs}
        for config in by_id.values():
            config.upstream_usage_amount = 12.375
            config.upstream_usage_unit = "USD"
            config.upstream_usage_checked_at = checked_at
        await self.db.commit()

        result = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                )
            },
        )
        result.account_group_matches = {
            7: {"id": "default", "name": "Default", "multiplier": 1.0}
        }
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, channel_id)

        matched = next(item for item in discovered.accounts if item.sub2api_account_id == 7)
        unmatched = next(item for item in discovered.accounts if item.sub2api_account_id == 8)
        self.assertIsNone(matched.upstream_usage_amount)
        self.assertIsNone(matched.upstream_usage_unit)
        self.assertIsNone(matched.upstream_usage_checked_at)
        self.assertIsNone(unmatched.upstream_usage_amount)
        self.assertIsNone(unmatched.upstream_usage_unit)
        self.assertIsNone(unmatched.upstream_usage_checked_at)

    async def test_discovery_clears_stale_today_usage_when_upstream_stops_providing_it(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        channel = await self.db.get(UpstreamChannel, channel_id)
        channel.today_balance_used = 3.25
        channel.today_balance_unit = "USD"
        channel.today_balance_status = "ok"
        channel.today_balance_checked_at = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        channel.yesterday_balance_used = 2.75
        channel.yesterday_balance_unit = "USD"
        channel.yesterday_balance_status = "ok"
        channel.yesterday_balance_checked_at = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        await self.db.commit()

        result = self._discovery_result(
            today_balance_used=None,
            today_balance_status="unsupported",
            yesterday_balance_used=None,
            yesterday_balance_status="not_available",
        )
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, channel_id)

        self.assertEqual(discovered.today_balance_status, "unsupported")
        self.assertIsNone(discovered.today_balance_used)
        self.assertIsNone(discovered.today_balance_unit)
        self.assertIsNone(discovered.today_balance_checked_at)
        self.assertEqual(discovered.yesterday_balance_status, "not_available")
        self.assertIsNone(discovered.yesterday_balance_used)
        self.assertIsNone(discovered.yesterday_balance_unit)
        self.assertIsNone(discovered.yesterday_balance_checked_at)

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

    async def test_canonical_origin_change_is_checked_independently_from_management_origin(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]
        configured = await self.service.update_channel(
            self.db,
            channel.id,
            UpstreamChannelUpdate(
                management_base_url="https://management.example/api/v1",
                access_token="retained-channel-token",
                confirm_credential_rebind=True,
            ),
        )

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.update_channel(
                self.db,
                channel.id,
                UpstreamChannelUpdate(base_url="https://replacement.example/v1"),
            )

        self.assertEqual(context.exception.status_code, 409)
        await self.db.rollback()
        stored = await self.db.get(UpstreamChannel, channel.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.canonical_base_url, configured.canonical_base_url)
        self.assertEqual(stored.management_base_url, "https://management.example")

    async def test_confirmed_canonical_origin_change_clears_account_origin_rebind_flags(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]
        configs_result = await self.db.execute(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.channel_id == channel.id
            )
        )
        configs = list(configs_result.scalars().all())
        for config in configs:
            config.api_key_origin_rebind_required = True
        await self.db.commit()

        updated = await self.service.update_channel(
            self.db,
            channel.id,
            UpstreamChannelUpdate(
                base_url="https://replacement.example/v1",
                confirm_credential_rebind=True,
            ),
        )

        self.assertEqual(updated.canonical_base_url, "https://replacement.example")
        for config in configs:
            await self.db.refresh(config)
            self.assertFalse(config.api_key_origin_rebind_required)

    async def test_discover_all_does_not_count_failed_manual_fallback_as_success(self) -> None:
        channel = (await self.service.overview(self.db)).channels[0]
        await self.service.update_channel(
            self.db,
            channel.id,
            UpstreamChannelUpdate(manual_recharge_multiplier=2.0),
        )
        stored_channel = await self.db.get(UpstreamChannel, channel.id)
        stored_channel.today_balance_used = 3.25
        stored_channel.today_balance_unit = "USD"
        stored_channel.today_balance_status = "ok"
        stored_channel.today_balance_checked_at = datetime(
            2026, 7, 16, 8, 0, tzinfo=timezone.utc
        )
        stored_channel.yesterday_balance_used = 2.75
        stored_channel.yesterday_balance_unit = "USD"
        stored_channel.yesterday_balance_status = "ok"
        stored_channel.yesterday_balance_checked_at = datetime(
            2026, 7, 16, 8, 0, tzinfo=timezone.utc
        )
        await self.db.commit()

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result(status="error")),
        ):
            result = await self.service.discover_all(self.db)

        self.assertEqual((result.total, result.succeeded, result.failed), (1, 0, 1))
        self.assertEqual(result.channels[0].recharge_multiplier_status, "fallback_manual")
        self.assertEqual(result.channels[0].last_error, "Upstream channel discovery failed.")
        self.assertEqual(result.channels[0].today_balance_status, "error")
        self.assertEqual(result.channels[0].today_balance_used, 3.25)
        self.assertEqual(result.channels[0].yesterday_balance_status, "error")
        self.assertEqual(result.channels[0].yesterday_balance_used, 2.75)

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
                    self._account_update(7, manual_group_multiplier=4.0),
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
                    self._account_update(8, api_key=newly_saved_key),
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

    async def test_identity_rebind_during_export_drops_stale_exported_key(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs_result = await self.db.execute(select(UpstreamAccountConfig))
        configs_by_id = {
            config.sub2api_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[7].encrypted_api_key = encrypt_text("sk-current-seven")
        configs_by_id[8].encrypted_api_key = None
        await self.db.commit()
        configs = await self.service._bound_configs(self.db, channel_id)
        stale_exported_key = "sk-exported-before-identity-rebind"

        async def export_after_identity_rebind(account_ids: list[int]) -> dict[int, str]:
            self.sub2api.export_calls.append(list(account_ids))
            replacement = dict(self.sub2api.accounts[1])
            replacement["name"] = "replacement-eight"
            replacement["created_at"] = "2026-07-15T12:00:08Z"
            self.sub2api.accounts[1] = replacement
            async with self.session_factory() as concurrent_db:
                await self.service.accounts.upsert_account(
                    concurrent_db,
                    8,
                    self._account_update(8, confirm_identity_rebind=True),
                )
            return {8: stale_exported_key} if 8 in account_ids else {}

        with patch.object(
            self.sub2api,
            "export_api_key_secrets",
            new=AsyncMock(side_effect=export_after_identity_rebind),
        ):
            exported = await self.service._account_api_keys(
                self.db,
                configs,
                channel_id,
            )

        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(exported.get(7), "sk-current-seven")
        self.assertNotIn(8, exported)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertIsNone(stored.encrypted_api_key)
            self.assertEqual(
                stored.remote_identity_fingerprint,
                self.service.accounts._remote_binding_fingerprint(
                    self.sub2api.accounts[1]
                ),
            )

    async def test_origin_rebind_flag_set_during_export_blocks_persist_and_fallback(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs_result = await self.db.execute(select(UpstreamAccountConfig))
        configs_by_id = {
            config.sub2api_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[7].encrypted_api_key = encrypt_text("sk-current-seven")
        configs_by_id[8].encrypted_api_key = None
        await self.db.commit()
        configs = await self.service._bound_configs(self.db, channel_id)
        stale_exported_key = "sk-exported-before-origin-rebind"

        async def export_after_origin_rebind(account_ids: list[int]) -> dict[int, str]:
            self.sub2api.export_calls.append(list(account_ids))
            async with self.session_factory() as concurrent_db:
                stored = (
                    await concurrent_db.execute(
                        select(UpstreamAccountConfig).where(
                            UpstreamAccountConfig.sub2api_account_id == 8
                        )
                    )
                ).scalar_one()
                stored.api_key_origin_rebind_required = True
                stored.encrypted_api_key = None
                await concurrent_db.commit()
            return {8: stale_exported_key} if 8 in account_ids else {}

        with patch.object(
            self.sub2api,
            "export_api_key_secrets",
            new=AsyncMock(side_effect=export_after_origin_rebind),
        ):
            exported = await self.service._account_api_keys(
                self.db,
                configs,
                channel_id,
            )

        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(exported.get(7), "sk-current-seven")
        self.assertNotIn(8, exported)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertTrue(stored.api_key_origin_rebind_required)
            self.assertIsNone(stored.encrypted_api_key)

    async def test_remote_endpoint_change_during_export_drops_exported_key(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs_result = await self.db.execute(select(UpstreamAccountConfig))
        configs_by_id = {
            config.sub2api_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[7].encrypted_api_key = encrypt_text("sk-current-seven")
        configs_by_id[8].encrypted_api_key = None
        await self.db.commit()
        configs = await self.service._bound_configs(self.db, channel_id)
        replacement_key = "sk-key-from-replacement-origin"

        async def export_after_endpoint_change(account_ids: list[int]) -> dict[int, str]:
            self.sub2api.export_calls.append(list(account_ids))
            replacement = dict(self.sub2api.accounts[1])
            replacement["credentials"] = {
                **replacement["credentials"],
                "base_url": "https://replacement.example/v1",
            }
            self.sub2api.accounts[1] = replacement
            return {8: replacement_key} if 8 in account_ids else {}

        with patch.object(
            self.sub2api,
            "export_api_key_secrets",
            new=AsyncMock(side_effect=export_after_endpoint_change),
        ):
            exported = await self.service._account_api_keys(
                self.db,
                configs,
                channel_id,
            )

        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(exported.get(7), "sk-current-seven")
        self.assertNotIn(8, exported)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertIsNone(stored.encrypted_api_key)
            self.assertFalse(stored.api_key_origin_rebind_required)
            self.assertEqual(stored.base_url, "https://upstream.example")

    async def test_remote_endpoint_mismatch_blocks_an_already_stored_key(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        configs_result = await self.db.execute(select(UpstreamAccountConfig))
        configs_by_id = {
            config.sub2api_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[8].encrypted_api_key = encrypt_text("sk-stored-for-old-origin")
        await self.db.commit()
        self.sub2api.accounts[1]["credentials"]["base_url"] = (
            "https://replacement.example/v1"
        )
        configs = await self.service._bound_configs(self.db, channel_id)

        account_api_keys = await self.service._account_api_keys(
            self.db,
            configs,
            channel_id,
        )

        self.assertNotIn(8, account_api_keys)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(UpstreamAccountConfig).where(
                        UpstreamAccountConfig.sub2api_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(
                decrypt_text(stored.encrypted_api_key),
                "sk-stored-for-old-origin",
            )

    async def test_manual_channel_with_explicit_key_ignores_remote_endpoint_metadata(self) -> None:
        await self.service.overview(self.db)
        manual_channel = UpstreamChannel(
            display_name="Manual upstream",
            canonical_base_url="https://manual.example",
            upstream_type="auto",
            group_options=[],
            recharge_multiplier_status="not_discovered",
            balance_status="not_checked",
        )
        self.db.add(manual_channel)
        await self.db.commit()
        await self.db.refresh(manual_channel)
        manual_key = "sk-explicit-manual-origin"

        await self.service.accounts.upsert_account(
            self.db,
            8,
            self._account_update(
                8,
                channel_id=manual_channel.id,
                api_key=manual_key,
                confirm_credential_rebind=True,
            ),
        )

        configs = await self.service._bound_configs(self.db, manual_channel.id)
        account_api_keys = await self.service._account_api_keys(
            self.db,
            configs,
            manual_channel.id,
        )

        self.assertEqual(account_api_keys, {8: manual_key})
        stored = (
            await self.db.execute(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == 8
                )
            )
        ).scalar_one()
        self.assertTrue(stored.channel_auto_assign_disabled)

    async def test_api_key_export_is_batched_at_sub2api_limit(self) -> None:
        for account_id in range(9, 208):
            self.sub2api.accounts.append(
                {
                    "id": account_id,
                    "name": f"account-{account_id}",
                    "platform": "openai",
                    "type": "apikey",
                    "status": "active",
                    "created_at": f"2026-07-02T00:{account_id % 60:02}:00Z",
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
                    self._account_update(
                        7,
                        channel_id=other_channel.id,
                        confirm_credential_rebind=True,
                    ),
                )
                await self.service.accounts.upsert_account(
                    concurrent_db,
                    8,
                    self._account_update(8, api_key=retry_api_key),
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
        channel_access_token = "channel-management-token"
        await self.service.update_channel(
            self.db,
            channel_id,
            UpstreamChannelUpdate(access_token=channel_access_token),
        )
        configs = await self.db.execute(select(UpstreamAccountConfig))
        by_id = {item.sub2api_account_id: item for item in configs.scalars().all()}
        api_key = "sk-rate-log-name-secret"
        by_id[7].encrypted_api_key = encrypt_text(api_key)
        by_id[7].remote_name = api_key
        by_id[8].encrypted_api_key = encrypt_text("sk-rate-log-eight")
        by_id[8].remote_name = channel_access_token
        await self.db.commit()

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
        all_logs = list(
            (
                await self.db.execute(
                    select(UpstreamRateChangeLog).order_by(UpstreamRateChangeLog.id)
                )
            ).scalars().all()
        )
        self.assertEqual({item.sub2api_account_id for item in all_logs}, {7, 8})
        logged_names = [(item.sub2api_account_id, item.account_name) for item in all_logs]
        self.assertEqual(logged_names[:2], [(7, "[redacted]"), (8, "[redacted]")])
        self.assertNotIn(api_key, repr(logged_names))
        self.assertNotIn(channel_access_token, repr(logged_names))
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

    async def test_refresh_only_sub2api_channel_is_eligible_for_batch_discovery(self) -> None:
        channel_id = await self._configure_sub2api_credentials(
            access_token="",
            refresh_token="rt-refresh-only-private",
        )
        new_access_token = "at-restored-private"
        new_refresh_token = "rt-rotated-private"

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=self._discovery_result()),
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
            result = await self.service.discover_all(
                self.db,
                max_concurrency=1,
                require_management_credentials=True,
            )

        self.assertEqual((result.succeeded, result.failed, result.skipped), (1, 0, 0))
        refresh.assert_awaited_once_with(
            "https://upstream.example",
            "rt-refresh-only-private",
        )
        discover.assert_awaited_once()
        self.assertEqual(discover.await_args.kwargs["access_token"], new_access_token)
        stored = await self.db.get(UpstreamChannel, channel_id)
        await self.db.refresh(stored)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), new_access_token)
        self.assertEqual(decrypt_text(stored.encrypted_refresh_token), new_refresh_token)

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

    async def test_priority_rebalance_failure_does_not_rollback_channel_discovery(self) -> None:
        channel_id = (await self.service.overview(self.db)).channels[0].id
        priority_service = self.service._priority_service()
        result = self._discovery_result()

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch.object(
                priority_service,
                "rebalance",
                new=AsyncMock(side_effect=RuntimeError("priority sync failed")),
            ),
        ):
            discovered = await self.service.discover_channel(self.db, channel_id)

        stored = await self.db.get(UpstreamChannel, channel_id)
        await self.db.refresh(stored)
        self.assertIsNotNone(stored.last_discovered_at)
        self.assertEqual(discovered.id, channel_id)

    async def test_two_confirmed_disabled_key_probes_disable_once_and_recovery_does_not_enable(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        self.runtime_config.get_api_key_auto_disable_on_upstream_unavailable.return_value = True
        disabled = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="disabled",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                ),
                8: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                ),
            }
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=disabled),
        ):
            await self.service.discover_channel(self.db, channel_id)
            first = await self.db.scalar(
                select(UpstreamAccountConfig).where(
                    UpstreamAccountConfig.sub2api_account_id == 7
                )
            )
            self.assertEqual(first.upstream_health_invalid_count, 1)
            self.assertTrue(self.sub2api.accounts[0]["schedulable"])

            await self.service.discover_channel(self.db, channel_id)

        await self.db.refresh(first)
        self.assertEqual(first.upstream_key_status, "disabled")
        self.assertEqual(first.upstream_group_status, "available")
        self.assertEqual(first.upstream_health_invalid_count, 2)
        self.assertEqual(first.auto_disabled_reason, "upstream_key_unavailable")
        self.assertIsNotNone(first.last_auto_disabled_at)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])

        recovered = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                ),
                8: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                ),
            }
        )
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=recovered),
        ):
            await self.service.discover_channel(self.db, channel_id)

        await self.db.refresh(first)
        self.assertEqual(first.upstream_key_status, "active")
        self.assertEqual(first.upstream_health_invalid_count, 0)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        logs = list(
            (
                await self.db.execute(
                    select(UpstreamRateChangeLog)
                    .where(UpstreamRateChangeLog.sub2api_account_id == 7)
                    .order_by(UpstreamRateChangeLog.id)
                )
            ).scalars()
        )
        self.assertTrue(any(item.status == "account_disabled" for item in logs))
        self.assertTrue(any(item.reason == "upstream_key_recovered" for item in logs))
        disabled_log = next(item for item in logs if item.status == "account_disabled")
        self.assertTrue(disabled_log.old_remote_schedulable)
        self.assertFalse(disabled_log.new_remote_schedulable)

    async def test_inconclusive_probe_preserves_authoritative_state_and_breaks_invalid_sequence(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        self.runtime_config.get_api_key_auto_disable_on_upstream_unavailable.return_value = True
        invalid = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="disabled",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                )
            }
        )
        inconclusive = self._discovery_result(account_upstream_states={})

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(side_effect=[invalid, inconclusive, invalid]),
        ):
            await self.service.discover_channel(self.db, channel_id)
            await self.service.discover_channel(self.db, channel_id)
            await self.service.discover_channel(self.db, channel_id)

        config = await self.db.scalar(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.sub2api_account_id == 7
            )
        )
        self.assertEqual(config.upstream_key_status, "disabled")
        self.assertEqual(config.upstream_group_status, "available")
        self.assertEqual(config.upstream_health_invalid_count, 1)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [])

    async def test_two_confirmed_unavailable_group_probes_disable_account(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        self.runtime_config.get_api_key_auto_disable_on_upstream_unavailable.return_value = True
        invalid = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="active",
                    group_status="unavailable",
                    group_id="retired",
                    group_name="Retired",
                )
            }
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=invalid),
        ):
            await self.service.discover_channel(self.db, channel_id)
            await self.service.discover_channel(self.db, channel_id)

        config = await self.db.scalar(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.sub2api_account_id == 7
            )
        )
        self.assertEqual(config.upstream_group_status, "unavailable")
        self.assertEqual(config.selected_group_id, "retired")
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])

    async def test_disable_readback_failure_is_logged_without_aborting_channel(self) -> None:
        channel_id = await self._configure_sub2api_credentials()
        self.runtime_config.get_api_key_auto_disable_on_upstream_unavailable.return_value = True
        invalid = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="disabled",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                )
            }
        )

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=invalid),
            ),
            patch.object(
                self.sub2api,
                "set_account_schedulable",
                new=AsyncMock(),
            ),
        ):
            await self.service.discover_channel(self.db, channel_id)
            discovered = await self.service.discover_channel(self.db, channel_id)

        self.assertEqual(discovered.id, channel_id)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        failure = await self.db.scalar(
            select(UpstreamRateChangeLog)
            .where(
                UpstreamRateChangeLog.sub2api_account_id == 7,
                UpstreamRateChangeLog.status == "disable_failed",
            )
            .order_by(UpstreamRateChangeLog.id.desc())
        )
        self.assertIsNotNone(failure)
        self.assertIn("Unable to disable", failure.safe_error)

    async def test_overview_inventory_cleanup_rebalances_priorities(self) -> None:
        await self.service.overview(self.db)
        interval = UpstreamPriorityInterval(
            name="removed-account",
            start_priority=80,
            end_priority=90,
            step=1,
        )
        self.db.add(interval)
        await self.db.flush()
        config = await self.db.scalar(
            select(UpstreamAccountConfig).where(
                UpstreamAccountConfig.sub2api_account_id == 8
            )
        )
        config.priority_interval_id = interval.id
        await self.db.commit()
        self.sub2api.accounts = [self.sub2api.accounts[0]]

        with patch.object(
            self.service,
            "_rebalance_priorities_best_effort",
            new=AsyncMock(),
        ) as rebalance:
            overview = await self.service.overview(self.db)

        rebalance.assert_awaited_once_with(self.db)
        self.assertEqual([item.sub2api_account_id for item in overview.channels[0].accounts], [7])
        await self.db.refresh(config)
        self.assertIsNone(config.priority_interval_id)

    async def test_discover_all_max_concurrency_controls_parallel_channel_work(self) -> None:
        base = await self.service.overview(self.db)
        first = base.channels[0]
        second = first.model_copy(update={"id": first.id + 1000})
        synthetic = base.model_copy(update={"channels": [first, second]})

        async def peak_for(limit: int) -> int:
            active = 0
            peak = 0

            async def discover(
                _db,
                channel_id: int,
                *,
                sync_inventory: bool = True,
                remote_by_id=None,
            ):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return first if channel_id == first.id else second

            with (
                patch.object(
                    self.service,
                    "overview",
                    new=AsyncMock(return_value=synthetic),
                ),
                patch.object(
                    self.service,
                    "_discover_channel",
                    new=AsyncMock(side_effect=discover),
                ),
                patch.object(
                    self.service,
                    "_rebalance_priorities_best_effort",
                    new=AsyncMock(),
                ),
            ):
                await self.service.discover_all(self.db, max_concurrency=limit)
            return peak

        self.assertEqual(await peak_for(1), 1)
        self.assertEqual(await peak_for(2), 2)
        self.assertEqual(await peak_for(0), 2)
        for invalid in (-1, 51, True):
            with self.assertRaises(ValueError):
                await self.service.discover_all(self.db, max_concurrency=invalid)

    async def test_discover_all_skips_channels_with_automatic_probe_disabled(self) -> None:
        overview = await self.service.overview(self.db)
        channel = await self.db.get(UpstreamChannel, overview.channels[0].id)
        self.assertIsNotNone(channel)
        channel.probe_enabled = False
        await self.db.commit()

        with (
            patch.object(
                self.service,
                "_discover_channel",
                new=AsyncMock(),
            ) as discover,
            patch.object(
                self.service,
                "_rebalance_priorities_best_effort",
                new=AsyncMock(),
            ) as rebalance,
        ):
            result = await self.service.discover_all(self.db, max_concurrency=0)

        self.assertEqual((result.total, result.succeeded, result.failed, result.skipped), (1, 0, 0, 1))
        self.assertIsNotNone(result.overview)
        self.assertFalse(result.channels[0].probe_enabled)
        discover.assert_not_awaited()
        rebalance.assert_awaited_once_with(self.db, account_ids=set())

    async def test_manual_discover_all_reuses_complete_fresh_channel_cache(self) -> None:
        overview = await self.service.overview(self.db)
        observed_at = datetime.now(timezone.utc)
        channel = overview.channels[0].model_copy(
            update={
                "last_discovered_at": observed_at,
                "accounts": [
                    account.model_copy(update={"last_discovered_at": observed_at})
                    for account in overview.channels[0].accounts
                ],
            }
        )
        synthetic = overview.model_copy(update={"channels": [channel]})

        with (
            patch.object(
                self.service,
                "overview",
                new=AsyncMock(return_value=synthetic),
            ),
            patch.object(
                self.service,
                "_discover_channel",
                new=AsyncMock(),
            ) as discover,
            patch.object(
                self.service,
                "_rebalance_priorities_best_effort",
                new=AsyncMock(),
            ) as rebalance,
        ):
            result = await self.service.discover_all(
                self.db,
                max_concurrency=0,
                force=False,
                cache_max_age_seconds=900,
            )

        self.assertEqual((result.succeeded, result.failed, result.cached), (0, 0, 1))
        self.assertFalse(result.force)
        discover.assert_not_awaited()
        rebalance.assert_awaited_once_with(
            self.db,
            account_ids={account.sub2api_account_id for account in channel.accounts},
        )

    async def test_manual_discover_all_reuses_complete_cache_without_ttl(self) -> None:
        overview = await self.service.overview(self.db)
        observed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        channel = overview.channels[0].model_copy(
            update={
                "last_discovered_at": observed_at,
                "accounts": [
                    account.model_copy(update={"last_discovered_at": observed_at})
                    for account in overview.channels[0].accounts
                ],
            }
        )
        synthetic = overview.model_copy(update={"channels": [channel]})

        with (
            patch.object(
                self.service,
                "overview",
                new=AsyncMock(return_value=synthetic),
            ),
            patch.object(
                self.service,
                "_discover_channel",
                new=AsyncMock(),
            ) as discover,
            patch.object(
                self.service,
                "_rebalance_priorities_best_effort",
                new=AsyncMock(),
            ),
        ):
            result = await self.service.discover_all(
                self.db,
                max_concurrency=0,
                force=False,
                cache_max_age_seconds=None,
            )

        self.assertEqual(result.cached, 1)
        self.assertIsNone(result.cache_max_age_seconds)
        discover.assert_not_awaited()

    async def test_discover_all_syncs_inventory_once_and_reuses_the_batch_snapshot(self) -> None:
        overview = await self.service.overview(self.db)
        overview_mock = AsyncMock(side_effect=[overview, overview])
        discover_mock = AsyncMock(return_value=overview.channels[0])
        with (
            patch.object(self.service, "overview", new=overview_mock),
            patch.object(self.service, "_discover_channel", new=discover_mock),
            patch.object(
                self.service,
                "_rebalance_priorities_best_effort",
                new=AsyncMock(),
            ),
        ):
            await self.service.discover_all(self.db, max_concurrency=1)

        self.assertEqual(
            [call.kwargs["sync_inventory"] for call in overview_mock.await_args_list],
            [False, False],
        )
        self.assertEqual(discover_mock.await_count, 1)
        self.assertEqual(discover_mock.await_args.args, (self.db, overview.channels[0].id))
        self.assertFalse(discover_mock.await_args.kwargs["sync_inventory"])
        self.assertIn(7, discover_mock.await_args.kwargs["remote_by_id"])

    async def test_overlapping_discover_all_batches_share_one_service_lock(self) -> None:
        synthetic = await self.service.overview(self.db)
        active = 0
        peak = 0
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def discover(
            _db,
            _channel_id: int,
            *,
            sync_inventory: bool = True,
            remote_by_id=None,
        ):
            nonlocal active, peak, calls
            calls += 1
            active += 1
            peak = max(peak, active)
            if calls == 1:
                first_started.set()
                await release_first.wait()
            active -= 1
            return synthetic.channels[0]

        with (
            patch.object(
                self.service,
                "overview",
                new=AsyncMock(return_value=synthetic),
            ),
            patch.object(
                self.service,
                "_discover_channel",
                new=AsyncMock(side_effect=discover),
            ),
            patch.object(
                self.service,
                "_rebalance_priorities_best_effort",
                new=AsyncMock(),
            ),
        ):
            first = asyncio.create_task(
                self.service.discover_all(self.db, max_concurrency=1)
            )
            await first_started.wait()
            second = asyncio.create_task(
                self.service.discover_all(self.db, max_concurrency=1)
            )
            await asyncio.sleep(0.02)
            self.assertEqual(active, 1)
            release_first.set()
            await asyncio.gather(first, second)

        self.assertEqual(peak, 1)


class UpstreamChannelAuthenticationTests(unittest.TestCase):
    def test_delete_channel_uses_authenticated_service(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")
        db = AsyncMock()
        service = SimpleNamespace(delete_channel=AsyncMock())

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: service
        with TestClient(app) as client:
            response = client.delete("/api/upstream-channels/7")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["message"], "空渠道已删除。")
        service.delete_channel.assert_awaited_once_with(db, 7)

    def test_sync_inventory_returns_overview_without_running_discovery(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")
        db = AsyncMock()
        result = UpstreamOverviewOut()
        service = SimpleNamespace(overview=AsyncMock(return_value=result))

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: service
        with (
            patch("app.api.upstream_channels.record_event", new=AsyncMock()) as record,
            TestClient(app) as client,
        ):
            response = client.post("/api/upstream-channels/sync-inventory")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["channels"], [])
        service.overview.assert_awaited_once_with(db)
        record.assert_awaited_once_with(
            db,
            "manual_api_key_inventory_sync",
            "Synchronized 0 API key account(s) across 0 upstream channel(s).",
            details={
                "reason": "manual",
                "accounts": 0,
                "channels": 0,
                "unassigned_accounts": 0,
                "duration_ms": ANY,
            },
        )

    def test_discover_all_records_only_summary_counts(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")
        db = AsyncMock()
        result = UpstreamChannelDiscoverAllOut(
            total=2,
            succeeded=1,
            failed=1,
            force=True,
            cache_max_age_seconds=None,
            channels=[],
        )
        service = SimpleNamespace(discover_all=AsyncMock(return_value=result))
        runtime = SimpleNamespace(
            get_upstream_sync_max_concurrency=AsyncMock(return_value=2),
            get_upstream_sync_interval_seconds=AsyncMock(return_value=900),
            get_upstream_sync_enabled=AsyncMock(return_value=True),
        )

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: service
        with (
            patch("app.api.upstream_channels.record_event", new=AsyncMock()) as record,
            patch(
                "app.api.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
            TestClient(app) as client,
        ):
            response = client.post("/api/upstream-channels/discover-all")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["failed"], 1)
        record.assert_awaited_once_with(
            db,
            "manual_upstream_sync",
            "Synchronized 2 API key channel(s); 1 probed successfully, 0 reused cached state, 1 failed, and 0 skipped.",
            details={
                "reason": "manual",
                "total": 2,
                "succeeded": 1,
                "failed": 1,
                "cached": 0,
                "skipped": 0,
                "force": True,
                "cache_max_age_seconds": None,
                "probe_globally_enabled": True,
                "duration_ms": ANY,
            },
        )
        service.discover_all.assert_awaited_once_with(
            db,
            max_concurrency=2,
            require_management_credentials=True,
            force=True,
        )

    def test_discover_all_only_syncs_inventory_when_global_probe_is_disabled(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")
        db = AsyncMock()
        overview = UpstreamOverviewOut(
            channels=[
                {
                    "id": 1,
                    "display_name": "disabled-global-probe",
                    "canonical_base_url": "https://upstream.example",
                    "base_url": "https://upstream.example",
                    "account_count": 0,
                }
            ]
        )
        service = SimpleNamespace(
            overview=AsyncMock(return_value=overview),
            discover_all=AsyncMock(),
        )
        runtime = SimpleNamespace(
            get_upstream_sync_max_concurrency=AsyncMock(return_value=0),
            get_upstream_sync_interval_seconds=AsyncMock(return_value=900),
            get_upstream_sync_enabled=AsyncMock(return_value=False),
        )

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: service
        with (
            patch("app.api.upstream_channels.record_event", new=AsyncMock()),
            patch(
                "app.api.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
            TestClient(app) as client,
        ):
            response = client.post("/api/upstream-channels/discover-all")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["probe_globally_enabled"])
        self.assertEqual(response.json()["total"], 0)
        self.assertEqual(response.json()["skipped"], 0)
        service.overview.assert_awaited_once_with(db)
        service.discover_all.assert_not_awaited()

    def test_discover_all_returns_success_when_summary_event_cannot_be_saved(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")
        db = AsyncMock()
        result = UpstreamChannelDiscoverAllOut(
            total=1,
            succeeded=1,
            failed=0,
            force=False,
            cache_max_age_seconds=900,
            channels=[],
        )
        service = SimpleNamespace(discover_all=AsyncMock(return_value=result))
        runtime = SimpleNamespace(
            get_upstream_sync_max_concurrency=AsyncMock(return_value=1),
            get_upstream_sync_interval_seconds=AsyncMock(return_value=900),
            get_upstream_sync_enabled=AsyncMock(return_value=True),
        )

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_channel_service] = lambda: service
        with (
            patch(
                "app.api.upstream_channels.record_event",
                new=AsyncMock(side_effect=RuntimeError("synthetic audit failure")),
            ),
            patch(
                "app.api.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
            TestClient(app) as client,
        ):
            response = client.post("/api/upstream-channels/discover-all")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["succeeded"], 1)
        db.rollback.assert_awaited_once()

    def test_every_route_requires_admin_session(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-channels")

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = fake_db
        requests = [
            ("GET", "/api/upstream-channels", None),
            ("POST", "/api/upstream-channels/sync-inventory", None),
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
