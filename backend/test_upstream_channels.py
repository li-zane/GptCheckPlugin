from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.upstream_channels import router
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import Base, get_db
from app.core.security import require_admin
from app.core.validation import sanitized_request_validation_handler
from app.models import (
    AccountSchedulingChangeLog,
    AppEvent,
    NotificationOutbox,
    ApiAccountDataArchive,
    ApiAccount,
    ApiAccountDailyUsage,
    Upstream,
    UpstreamChangeEvent,
    UpstreamPriorityInterval,
    UpstreamRateChangeLog,
)
from app.schemas import (
    ApiAccountUpdate,
    UpstreamDiscoverAllRequest,
    UpstreamDiscoverAllOut,
    UpstreamMonitorsOut,
    UpstreamUpdate,
    UpstreamOverviewOut,
)
from app.services.sub2api import Sub2ApiClient
from app.services.upstream_accounts import (
    AUTO_PAUSE_REASON_MONITOR,
    ApiAccountServiceError,
    _remote_base_url,
)
from app.services.upstream_channels import (
    UpstreamService,
    UpstreamDiscoveryOptions,
    _account_group_rate_change_reason,
    get_upstream_service,
)
from app.services.upstream_client import (
    AccountGroupMatch,
    AccountUpstreamState,
    Sub2ApiLoginTokenPair,
    Sub2ApiTokenPair,
)


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
        self.api_endpoint_url_update_calls: list[tuple[int, str]] = []
        self.schedulable_calls: list[tuple[int, bool]] = []
        self.get_account_calls: list[int] = []
        self.today_costs: dict[int, float] = {}
        self.today_cost_calls: list[list[int]] = []
        self.daily_costs: dict[int, dict] = {}
        self.daily_cost_calls: list[tuple[list[int], int]] = []
        self.connection_test_calls: list[tuple[int, str]] = []
        self.connection_test_results: list[tuple[bool, str | None]] = []
        self.account_model_calls: list[int] = []
        default_models = [
            {"id": model_id, "display_name": model_id}
            for model_id in (
                "account-test-model",
                "monitor-test-model",
                "fallback-model",
                "gpt-4o-mini",
                "test-model",
            )
        ]
        self.account_models: dict[int, list[dict[str, str]]] = {
            7: list(default_models),
            8: list(default_models),
        }
        self.local_credit_per_cny = 10.0

    async def list_api_key_accounts(self) -> list[dict]:
        return list(self.accounts)

    async def get_account_by_id(self, account_id: str | int, **_kwargs) -> dict | None:
        parsed_id = int(account_id)
        self.get_account_calls.append(parsed_id)
        return next(
            (dict(item) for item in self.accounts if int(item["id"]) == parsed_id),
            None,
        )

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

    async def get_account_today_costs(self, account_ids: list[int]) -> dict[int, float]:
        self.today_cost_calls.append(list(account_ids))
        return {
            account_id: self.today_costs[account_id]
            for account_id in account_ids
            if account_id in self.today_costs
        }

    async def get_account_daily_costs(self, account_ids: list[int], *, days: int = 2) -> dict[int, dict]:
        self.daily_cost_calls.append((list(account_ids), days))
        return {
            account_id: dict(self.daily_costs[account_id])
            for account_id in account_ids
            if account_id in self.daily_costs
        }

    async def get_account_models(self, account: dict | str) -> list[dict[str, str]]:
        account_id = int(account if isinstance(account, str) else account["id"])
        self.account_model_calls.append(account_id)
        return [dict(item) for item in self.account_models.get(account_id, [])]

    async def get_account_management_billing_multiplier_multiplier(self, account_id: str | int) -> float:
        parsed_id = int(account_id)
        account = next(item for item in self.accounts if int(item["id"]) == parsed_id)
        return float(account["rate_multiplier"])

    async def update_account_rate_multiplier(self, account_id: str | int, rate_multiplier: float) -> None:
        parsed_id = int(account_id)
        self.rate_update_calls.append((parsed_id, rate_multiplier))
        account = next(item for item in self.accounts if int(item["id"]) == parsed_id)
        account["rate_multiplier"] = rate_multiplier

    async def update_account_base_url(
        self,
        account_id: str | int,
        base_url: str,
        *,
        validate_current=None,
    ) -> dict:
        parsed_id = int(account_id)
        account = next(item for item in self.accounts if int(item["id"]) == parsed_id)
        if validate_current is not None:
            validate_current(dict(account))
        self.api_endpoint_url_update_calls.append((parsed_id, base_url))
        account.setdefault("credentials", {})["base_url"] = base_url
        return dict(account)

    async def set_account_schedulable(self, account_id: str | int, schedulable: bool) -> None:
        parsed_id = int(account_id)
        self.schedulable_calls.append((parsed_id, schedulable))
        account = next(item for item in self.accounts if int(item["id"]) == parsed_id)
        account["schedulable"] = schedulable

    async def test_account_connection(
        self,
        account_id: str | int,
        model: str,
    ) -> tuple[bool, str | None]:
        parsed_id = int(account_id)
        self.connection_test_calls.append((parsed_id, model))
        if self.connection_test_results:
            return self.connection_test_results.pop(0)
        return False, "Connection test failed."


class UpstreamServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_account_group_rate_reason_prioritizes_assignment_over_multiplier(self) -> None:
        self.assertEqual(
            _account_group_rate_change_reason(
                previous_group_id="old",
                current_group_id="new",
                previous_group_name="Old",
                current_group_name="New",
                multiplier_changed=True,
            ),
            "upstream_group_assignment_change",
        )
        self.assertEqual(
            _account_group_rate_change_reason(
                previous_group_id="same",
                current_group_id="same",
                previous_group_name="Same",
                current_group_name="Same",
                multiplier_changed=True,
            ),
            "upstream_group_change",
        )

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.sub2api = FakeSub2Api()
        self.service = UpstreamService(self.sub2api)
        self.runtime_config = SimpleNamespace(
            get_public_settings=AsyncMock(
                return_value={"display_timezone": "Asia/Shanghai"}
            ),
            get_upstream_rate_sync_enabled=AsyncMock(return_value=False),
            get_automation_paused=AsyncMock(return_value=False),
            get_api_key_auto_disable_on_upstream_unavailable=AsyncMock(return_value=False),
            get_api_key_auto_pause_on_negative_balance_enabled=AsyncMock(
                return_value=False
            ),
            get_upstream_negative_balance_basis=AsyncMock(return_value="wallet"),
            get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled=AsyncMock(
                return_value=False
            ),
            get_api_key_availability_all_tests_must_succeed=AsyncMock(
                return_value=False
            ),
            get_upstream_monitor_auto_probe_enabled=AsyncMock(return_value=True),
            get_account_model_whitelist_sync_each_time=AsyncMock(return_value=False),
            get_upstream_monitor_unavailable_consecutive_threshold=AsyncMock(
                return_value=2
            ),
            get_upstream_monitor_recovery_consecutive_threshold=AsyncMock(
                return_value=2
            ),
            get_upstream_monitor_fallback_without_monitor_enabled=AsyncMock(
                return_value=False
            ),
            get_upstream_monitor_fallback_test_models=AsyncMock(return_value=[]),
            get_upstream_monitor_fallback_test_model=AsyncMock(return_value=""),
            get_upstream_monitor_fallback_test_attempts=AsyncMock(return_value=1),
            get_upstream_monitor_recovery_test_attempts=AsyncMock(return_value=1),
            get_upstream_monitor_test_attempt_interval_seconds=AsyncMock(return_value=0),
            get_upstream_balance_pause_threshold=AsyncMock(return_value=0.0),
            get_upstream_priority_sync_enabled=AsyncMock(return_value=True),
            get_notification_config=AsyncMock(
                return_value={
                    "enabled": True,
                    "oauth_account_disabled_enabled": True,
                    "api_key_rate_changed_enabled": True,
                    "upstream_balance_low_enabled": True,
                }
            ),
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

    def _account_update(self, account_id: int, **values: object) -> ApiAccountUpdate:
        remote = next(account for account in self.sub2api.accounts if int(account["id"]) == account_id)
        return ApiAccountUpdate(
            expected_identity_fingerprint=self.service.accounts._remote_identity_fingerprint(remote),
            **values,
        )

    async def _enable_rate_interval(
        self,
        *,
        absolute: float = 1.0,
    ) -> None:
        interval = UpstreamPriorityInterval(
            name="倍率暂停测试区间",
            start_priority=0,
            end_priority=100,
            step=1,
            rate_pause_enabled=True,
            rate_absolute_threshold=absolute,
        )
        self.db.add(interval)
        await self.db.flush()
        configs = list((await self.db.scalars(select(ApiAccount))).all())
        for config in configs:
            config.priority_interval_id = interval.id
        await self.db.commit()

    async def _configure_sub2api_credentials(
        self,
        *,
        access_token: str = "at-old-private",
        refresh_token: str = "rt-old-private",
    ) -> int:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(
                platform_type="sub2api",
                access_token=access_token,
                refresh_token=refresh_token,
            ),
        )
        return upstream_id

    @staticmethod
    def _discovery_result(
        *,
        status: str = "ok",
        auth_rejected: bool = False,
        wallet_balance_usd: float | None = 42.75,
        account_upstream_states: dict[int, AccountUpstreamState] | None = None,
        today_upstream_wallet_cost_usd: float | None = 3.25,
        today_balance_status: str = "ok",
        today_balance_error: str | None = None,
        yesterday_upstream_wallet_cost_usd: float | None = 2.75,
        yesterday_balance_status: str = "ok",
        yesterday_balance_error: str | None = None,
        upstream_monitors: list[dict] | None = None,
        upstream_monitor_status: str = "ok",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            platform_type="sub2api",
            status=status,
            sub2api_auth_rejected=auth_rejected,
            groups=[{"id": "default", "name": "Default", "multiplier": 1.0}],
            discovered_upstream_recharge_multiplier=1.0,
            discovered_upstream_recharge_multiplier_source="payment.config",
            recharge_discovery_status="ok",
            wallet_balance_usd=wallet_balance_usd,
            wallet_total_usd=None,
            wallet_used_usd=None,
            balance_unit="USD",
            balance_status="ok" if status == "ok" else "error",
            balance_message="Balance available." if status == "ok" else "Credentials rejected.",
            today_upstream_wallet_cost_usd=today_upstream_wallet_cost_usd,
            today_balance_unit="USD" if today_upstream_wallet_cost_usd is not None else None,
            today_balance_status=today_balance_status,
            today_balance_error=today_balance_error,
            yesterday_upstream_wallet_cost_usd=yesterday_upstream_wallet_cost_usd,
            yesterday_balance_unit="USD" if yesterday_upstream_wallet_cost_usd is not None else None,
            yesterday_balance_status=yesterday_balance_status,
            yesterday_balance_error=yesterday_balance_error,
            account_upstream_states=account_upstream_states or {},
            upstream_monitors=upstream_monitors or [],
            upstream_monitors_total=len(upstream_monitors or []),
            upstream_monitors_status=upstream_monitor_status,
            upstream_monitors_message="Channel monitor data available.",
        )

    async def test_upstream_probe_runs_after_read_transaction_is_closed(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()

        async def discovery_without_transaction(**_kwargs):
            self.assertFalse(self.db.in_transaction())
            return self._discovery_result()

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=discovery_without_transaction,
        ):
            await self.service.discover_channel(self.db, upstream_id)

    async def test_discovery_collects_all_network_observations_without_a_transaction(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "independent_model"
        config.availability_test_model = "account-test-model"
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        await self.db.commit()
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        observed: set[str] = set()

        def assert_transaction_closed(stage: str) -> None:
            self.assertFalse(self.db.in_transaction(), stage)
            observed.add(stage)

        async def list_accounts() -> list[dict]:
            assert_transaction_closed("account inventory")
            return list(self.sub2api.accounts)

        async def management_recharge() -> tuple[float, bool]:
            assert_transaction_closed("local recharge")
            return 10.0, True

        async def export_api_keys(account_ids: list[int]) -> dict[int, str]:
            assert_transaction_closed("API key export")
            return {account_id: f"sk-imported-{account_id}" for account_id in account_ids}

        async def discover_without_transaction(**_kwargs):
            assert_transaction_closed("upstream discovery")
            result = self._discovery_result(
                wallet_balance_usd=None,
                account_upstream_states={
                    account_id: AccountUpstreamState(
                        key_status="active",
                        group_status="available",
                        group_id="default",
                        group_name="Default",
                    )
                    for account_id in (7, 8)
                },
            )
            result.balance_status = "error"
            result.account_group_matches = {
                account_id: {"id": "default", "name": "Default", "multiplier": 1.0}
                for account_id in (7, 8)
            }
            return result

        async def today_costs(_account_ids: list[int]) -> dict[int, float]:
            assert_transaction_closed("today costs")
            return {7: 1.25}

        async def daily_costs(
            _account_ids: list[int], *, days: int = 2
        ) -> dict[int, dict]:
            self.assertEqual(days, 2)
            assert_transaction_closed("daily costs")
            return {}

        async def account_balance(_account_id: int) -> dict:
            assert_transaction_closed("balance fallback")
            return {"status": "ok", "remaining": 9.5, "unit": "USD"}

        async def connection_test(
            account_id: str | int, model: str
        ) -> tuple[bool, str | None]:
            self.assertEqual((int(account_id), model), (7, "account-test-model"))
            assert_transaction_closed("availability test")
            return True, None

        with (
            patch.object(
                self.sub2api,
                "list_api_key_accounts",
                new=AsyncMock(side_effect=list_accounts),
            ),
            patch.object(
                self.sub2api,
                "get_payment_balance_recharge_multiplier_info",
                new=AsyncMock(side_effect=management_recharge),
            ),
            patch.object(
                self.sub2api,
                "export_api_key_secrets",
                new=AsyncMock(side_effect=export_api_keys),
            ),
            patch.object(
                self.sub2api,
                "get_account_today_costs",
                new=AsyncMock(side_effect=today_costs),
            ),
            patch.object(
                self.sub2api,
                "get_account_daily_costs",
                new=AsyncMock(side_effect=daily_costs),
            ),
            patch.object(
                self.sub2api,
                "get_account_balance",
                new=AsyncMock(side_effect=account_balance),
            ),
            patch.object(
                self.sub2api,
                "test_account_connection",
                new=AsyncMock(side_effect=connection_test),
            ),
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=discover_without_transaction,
            ),
            patch(
                "app.services.upstream_channels.finalize_cached_yesterday_usage",
                new=AsyncMock(return_value=False),
            ),
        ):
            await self.service.discover_channel(
                self.db,
                upstream_id,
                options=UpstreamDiscoveryOptions(sync_priorities=False),
            )

        self.assertEqual(
            observed,
            {
                "account inventory",
                "local recharge",
                "API key export",
                "upstream discovery",
                "today costs",
                "daily costs",
                "balance fallback",
                "availability test",
            },
        )
        await self.db.refresh(config)
        self.assertEqual(decrypt_text(config.encrypted_api_key), "sk-imported-7")

    async def test_overview_groups_equivalent_v1_urls_into_one_channel(self) -> None:
        overview = await self.service.overview(self.db)

        self.assertEqual(len(overview.upstreams), 1)
        self.assertEqual(overview.upstreams[0].api_endpoint_url, "https://upstream.example")
        self.assertEqual([item.management_account_id for item in overview.upstreams[0].accounts], [7, 8])
        self.assertEqual(
            [item.remote_platform for item in overview.upstreams[0].accounts],
            ["openai", "anthropic"],
        )
        self.assertEqual(overview.management_recharge_multiplier, 0.1)
        self.assertEqual(overview.unassigned_accounts, [])

        again = await self.service.overview(self.db)
        self.assertEqual(len(again.upstreams), 1)
        channels = await self.db.execute(select(Upstream))
        self.assertEqual(len(channels.scalars().all()), 1)

    async def test_overview_resolves_rate_pause_policy_from_priority_interval(self) -> None:
        await self.service.overview(self.db)
        await self._enable_rate_interval(absolute=0.15)

        overview = await self.service.overview(self.db, sync_inventory=False)
        account = next(
            item
            for item in overview.upstreams[0].accounts
            if item.management_account_id == 7
        )

        self.assertEqual(account.rate_pause_policy, "inherit")
        self.assertTrue(account.rate_pause_effective_enabled)
        self.assertEqual(account.rate_pause_effective_source, "priority_interval")
        self.assertEqual(account.rate_absolute_threshold, 0.15)

    async def test_inventory_marks_missing_accounts_without_clearing_their_configuration(self) -> None:
        initial = await self.service.overview(self.db)
        self.assertEqual(
            {account.management_account_id for account in initial.upstreams[0].accounts},
            {7, 8},
        )
        missing_config = await self.db.scalar(
            select(ApiAccount).where(ApiAccount.management_account_id == 8)
        )
        self.assertIsNotNone(missing_config)
        interval = UpstreamPriorityInterval(
            name="persist-missing-account",
            start_priority=40,
            end_priority=50,
            step=1,
        )
        self.db.add(interval)
        await self.db.flush()
        missing_config.priority_interval_id = interval.id
        missing_config.desired_priority = 42
        missing_config.priority_sync_status = "synced"
        known_usage_at = datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc)
        missing_config.today_upstream_wallet_cost_usd = 3.25
        missing_config.today_upstream_usage_unit = "USD"
        missing_config.today_upstream_usage_status = "ok"
        missing_config.today_upstream_usage_checked_at = known_usage_at
        snapshot_updated_at = missing_config.remote_snapshot_updated_at
        await self.db.commit()
        removed = next(account for account in self.sub2api.accounts if int(account["id"]) == 8)
        self.sub2api.accounts = [account for account in self.sub2api.accounts if int(account["id"]) != 8]

        missing = await self.service.overview(self.db)
        self.assertEqual(
            {account.management_account_id for account in missing.upstreams[0].accounts},
            {7},
        )
        missing_config = await self.db.scalar(
            select(ApiAccount).where(ApiAccount.management_account_id == 8)
        )
        self.assertIsNotNone(missing_config)
        self.assertFalse(missing_config.remote_present)
        self.assertIsNotNone(missing_config.remote_missing_at)
        self.assertEqual(missing_config.priority_interval_id, interval.id)
        self.assertEqual(missing_config.desired_priority, 42)
        self.assertEqual(missing_config.priority_sync_status, "remote_missing")
        self.assertEqual(missing_config.today_upstream_wallet_cost_usd, 3.25)
        self.assertEqual(
            missing_config.today_upstream_usage_checked_at,
            known_usage_at.replace(tzinfo=None),
        )
        self.assertEqual(missing_config.remote_snapshot_updated_at, snapshot_updated_at)
        archives = list((await self.db.scalars(select(ApiAccountDataArchive))).all())
        self.assertEqual(archives, [])

        self.sub2api.accounts.append(removed)
        restored = await self.service.overview(self.db)
        self.assertEqual(
            {account.management_account_id for account in restored.upstreams[0].accounts},
            {7, 8},
        )
        restored_config = await self.db.scalar(
            select(ApiAccount).where(ApiAccount.management_account_id == 8)
        )
        self.assertTrue(restored_config.remote_present)
        self.assertIsNone(restored_config.remote_missing_at)

    async def test_overview_includes_empty_channel_and_delete_removes_it(self) -> None:
        empty_channel = Upstream(
            display_name="Old upstream",
            api_endpoint_url="https://old-upstream.example",
            platform_type="auto",
            group_options=[],
            recharge_multiplier_status="not_discovered",
            balance_status="not_checked",
        )
        self.db.add(empty_channel)
        await self.db.commit()

        overview = await self.service.overview(self.db)
        empty = next(
            upstream
            for upstream in overview.upstreams
            if upstream.upstream_id == empty_channel.id
        )
        self.assertEqual(empty.account_count, 0)
        self.assertEqual(empty.accounts, [])

        await self.service.delete_channel(self.db, empty.upstream_id)

        deleted = await self.db.get(Upstream, empty.upstream_id)
        self.assertIsNotNone(deleted)
        self.assertIsNotNone(deleted.deleted_at)
        after_delete = await self.service.overview(self.db)
        self.assertNotIn(
            empty.upstream_id,
            {upstream.upstream_id for upstream in after_delete.upstreams},
        )

    async def test_delete_channel_rejects_current_api_key_accounts(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id

        with self.assertRaises(ApiAccountServiceError) as caught:
            await self.service.delete_channel(self.db, upstream_id)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNotNone(await self.db.get(Upstream, upstream_id))

    async def test_delete_channel_rejects_unsynced_account_with_same_origin(self) -> None:
        empty_channel = Upstream(
            display_name="New upstream",
            api_endpoint_url="https://new-upstream.example",
            platform_type="auto",
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

        with self.assertRaises(ApiAccountServiceError) as caught:
            await self.service.delete_channel(self.db, empty_channel.id)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNotNone(await self.db.get(Upstream, empty_channel.id))

    async def test_delete_empty_channel_removes_stale_account_configs(self) -> None:
        empty_channel = Upstream(
            display_name="Stale upstream",
            api_endpoint_url="https://stale-upstream.example",
            platform_type="auto",
        )
        self.db.add(empty_channel)
        await self.db.flush()
        stale_config = ApiAccount(
            management_account_id=999,
            remote_name="Removed account",
            upstream_id=empty_channel.id,
        )
        self.db.add(stale_config)
        await self.db.commit()

        await self.service.delete_channel(self.db, empty_channel.id)

        deleted_upstream = await self.db.get(Upstream, empty_channel.id)
        deleted_account = await self.db.get(ApiAccount, stale_config.id)
        self.assertIsNotNone(deleted_upstream.deleted_at)
        self.assertIsNotNone(deleted_account.deleted_at)

    async def test_read_only_overview_does_not_flush_projected_expected_management_billing_multiplier(self) -> None:
        overview = await self.service.overview(self.db)
        upstream_id = overview.upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(config)
        self.assertIsNotNone(channel)
        config.upstream_group_multiplier = 1.0
        config.expected_management_billing_multiplier = 9.0
        channel.upstream_recharge_multiplier = 1.0
        await self.db.commit()

        projected = await self.service.overview(self.db, sync_inventory=False)
        account = next(
            item
            for item in projected.upstreams[0].accounts
            if item.management_account_id == 7
        )
        self.assertEqual(account.expected_management_billing_multiplier, 10.0)

        await self.db.execute(select(Upstream.id))
        await self.db.commit()
        async with self.session_factory() as verifier:
            stored_target = await verifier.scalar(
                select(ApiAccount.expected_management_billing_multiplier).where(
                    ApiAccount.management_account_id == 7
                )
            )
        self.assertEqual(stored_target, 9.0)

    async def test_concurrent_overview_serializes_inventory_creation(self) -> None:
        async with self.session_factory() as first_db, self.session_factory() as second_db:
            first, second = await asyncio.gather(
                self.service.overview(first_db),
                self.service.overview(second_db),
            )

        self.assertEqual(len(first.upstreams), 1)
        self.assertEqual(len(second.upstreams), 1)
        async with self.session_factory() as verifier:
            channels = list((await verifier.execute(select(Upstream))).scalars())
            configs = list(
                (await verifier.execute(select(ApiAccount))).scalars()
            )
        self.assertEqual(len(channels), 1)
        self.assertEqual(len(configs), 2)

    async def test_inventory_retries_and_translates_concurrent_integrity_conflicts(
        self,
    ) -> None:
        conflict = IntegrityError(
            "UPDATE upstream_account_configs",
            {},
            RuntimeError("concurrent unique conflict"),
        )
        synchronized = ({}, {}, {}, False)
        with (
            patch.object(
                self.service,
                "_sync_inventory_rows",
                new=AsyncMock(side_effect=[conflict, synchronized]),
            ) as sync_rows,
            patch.object(
                self.service.accounts,
                "sync_available_models",
                new=AsyncMock(),
            ),
        ):
            result = await self.service._sync_inventory_unlocked(self.db)

        self.assertEqual(result, synchronized)
        self.assertEqual(sync_rows.await_count, 2)

        repeated_conflict = AsyncMock(side_effect=[conflict, conflict])
        with patch.object(
            self.service,
            "_sync_inventory_rows",
            new=repeated_conflict,
        ):
            with self.assertRaises(ApiAccountServiceError) as context:
                await self.service._sync_inventory_unlocked(self.db)

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(repeated_conflict.await_count, 2)

    async def test_inventory_quarantines_duplicate_unassigned_record_ids_before_assignment(
        self,
    ) -> None:
        await self.service.overview(self.db)
        configs = list((await self.db.scalars(select(ApiAccount))).all())
        self.assertEqual(len(configs), 2)
        for config in configs:
            config.upstream_id = None
            config.upstream_auto_assign_disabled = False
            config.remote_upstream_api_key_id = 88
            config.upstream_identity_rebind_required = False
            config.selected_group_id = "legacy"
            config.selected_group_name = "Legacy"
            config.upstream_group_multiplier = 1.25
            config.expected_management_billing_multiplier = 0.5
        await self.db.commit()

        overview = await self.service.overview(self.db)
        repeated = await self.service.overview(self.db)

        self.assertEqual(len(overview.upstreams), 1)
        self.assertEqual(overview.upstreams[0].accounts, [])
        self.assertEqual(
            sorted(account.management_account_id for account in overview.unassigned_accounts),
            [7, 8],
        )
        self.assertEqual(repeated.upstreams[0].accounts, [])
        self.assertEqual(
            sorted(account.management_account_id for account in repeated.unassigned_accounts),
            [7, 8],
        )
        stored = list((await self.db.scalars(select(ApiAccount))).all())
        for config in stored:
            self.assertIsNone(config.upstream_id)
            self.assertIsNone(config.remote_upstream_api_key_id)
            self.assertTrue(config.upstream_auto_assign_disabled)
            self.assertTrue(config.upstream_identity_rebind_required)
            self.assertEqual(config.selected_group_id, "legacy")
            self.assertEqual(config.selected_group_name, "Legacy")
            self.assertEqual(config.upstream_group_multiplier, 1.25)
            self.assertEqual(config.expected_management_billing_multiplier, 0.5)
            self.assertIn("duplicate upstream API key record #88", config.last_error or "")

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

        self.assertEqual(len(overview.upstreams), 1)

        self.assertEqual(account.management_account_id, 7)
        async with self.session_factory() as verifier:
            configs = list(
                (
                    await verifier.execute(
                        select(ApiAccount).where(
                            ApiAccount.management_account_id == 7
                        )
                    )
                ).scalars()
            )
        self.assertEqual(len(configs), 1)

    async def test_overview_reports_background_discovery_until_task_finishes(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_discovery(_upstream_id: int) -> None:
            started.set()
            await release.wait()

        with patch.object(
            self.service,
            "_discover_channel_in_background",
            new=delayed_discovery,
        ):
            self.service.queue_discover_channel(upstream_id)
            await started.wait()
            pending = await self.service.overview(self.db)
            self.assertTrue(pending.upstreams[0].background_discovery_pending)
            release.set()
            await asyncio.gather(*tuple(self.service._background_discovery_tasks))
            await asyncio.sleep(0)

        completed = await self.service.overview(self.db)
        self.assertFalse(completed.upstreams[0].background_discovery_pending)

    async def test_background_discovery_cannot_requeue_during_shutdown(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        started = asyncio.Event()
        release = asyncio.Event()

        async def delayed_discovery(_upstream_id: int) -> None:
            started.set()
            await release.wait()

        with patch.object(
            self.service,
            "_discover_channel_in_background",
            new=delayed_discovery,
        ):
            self.service.queue_discover_channel(upstream_id)
            await started.wait()
            stopping = asyncio.create_task(self.service.stop_background_tasks())
            while not self.service._background_discovery_stopping:
                await asyncio.sleep(0)
            self.service.queue_discover_channel(upstream_id)
            await stopping

        self.assertEqual(self.service._background_discovery_tasks, set())
        self.assertEqual(self.service._background_discovery_counts, {})

    async def test_overview_and_account_discover_all_share_config_creation_locks(self) -> None:
        async with self.session_factory() as overview_db, self.session_factory() as account_db:
            overview, discovery = await asyncio.gather(
                self.service.overview(overview_db),
                self.service.accounts.discover_all(account_db),
            )

        self.assertEqual(len(overview.upstreams), 1)
        self.assertEqual(discovery.total, 2)
        async with self.session_factory() as verifier:
            configs = list(
                (await verifier.execute(select(ApiAccount))).scalars()
            )
        self.assertEqual(
            sorted(config.management_account_id for config in configs),
            [7, 8],
        )

    async def test_inventory_and_channel_url_update_never_leak_unique_errors(self) -> None:
        overview = await self.service.overview(self.db)
        original_upstream_id = overview.upstreams[0].upstream_id
        self.sub2api.accounts[0]["credentials"]["base_url"] = (
            "https://replacement.example/v1"
        )
        self.sub2api.accounts[1]["credentials"]["base_url"] = (
            "https://replacement.example/v1"
        )
        payload = UpstreamUpdate(
            api_endpoint_url="https://replacement.example",
            confirm_credential_rebind=True,
        )

        async with self.session_factory() as overview_db, self.session_factory() as update_db:
            results = await asyncio.gather(
                self.service.overview(overview_db),
                self.service.update_channel(
                    update_db,
                    original_upstream_id,
                    payload,
                ),
                return_exceptions=True,
            )

        for result in results:
            if isinstance(result, Exception):
                self.assertIsInstance(result, ApiAccountServiceError)
                self.assertEqual(result.status_code, 409)
        async with self.session_factory() as verifier:
            replacement_channels = list(
                (
                    await verifier.execute(
                        select(Upstream).where(
                            Upstream.api_endpoint_url
                            == "https://replacement.example"
                        )
                    )
                ).scalars()
            )
        self.assertEqual(len(replacement_channels), 1)

    async def test_auto_assigned_account_follows_remote_endpoint_change(self) -> None:
        overview = await self.service.overview(self.db)
        original_channel = overview.upstreams[0]
        stored = (
            await self.db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 7
                )
            )
        ).scalar_one()
        original_channel_row = await self.db.get(Upstream, original_channel.upstream_id)
        assert original_channel_row is not None
        original_channel_row.encrypted_access_token = encrypt_text("old-channel-token")
        stored.encrypted_access_token = original_channel_row.encrypted_access_token
        stored.encrypted_api_key = encrypt_text("old-endpoint-api-key")
        stored.upstream_wallet_cost_usd = 12.375
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = stored.created_at
        stored.selected_group_id = "legacy"
        stored.selected_group_name = "Legacy"
        stored.upstream_group_multiplier_override = 2.0
        stored.upstream_group_multiplier = 2.0
        stored.expected_management_billing_multiplier = 2.0
        await self.db.commit()

        self.sub2api.accounts[0]["credentials"]["base_url"] = (
            "https://replacement.example/api/v1"
        )
        updated = await self.service.overview(self.db)

        by_url = {channel.api_endpoint_url: channel for channel in updated.upstreams}
        self.assertEqual(
            [account.management_account_id for account in by_url["https://upstream.example"].accounts],
            [8],
        )
        replacement = by_url["https://replacement.example"]
        self.assertEqual(
            [account.management_account_id for account in replacement.accounts],
            [7],
        )
        self.assertFalse(replacement.accounts[0].api_key_set)
        self.assertTrue(replacement.accounts[0].api_key_origin_rebind_required)
        self.assertFalse(replacement.access_token_set)

        await self.db.refresh(stored)
        self.assertEqual(stored.upstream_id, replacement.upstream_id)
        self.assertEqual(stored.api_endpoint_url, "https://replacement.example")
        self.assertIsNone(stored.encrypted_api_key)
        self.assertIsNone(stored.encrypted_access_token)
        self.assertIsNone(stored.upstream_wallet_cost_usd)
        self.assertIsNone(stored.upstream_usage_unit)
        self.assertIsNone(stored.upstream_usage_checked_at)
        self.assertTrue(stored.api_key_origin_rebind_required)
        self.assertIsNone(stored.selected_group_id)
        self.assertIsNone(stored.selected_group_name)
        self.assertIsNone(stored.upstream_group_multiplier_override)
        self.assertIsNone(stored.expected_management_billing_multiplier)
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
        channel = (await self.service.overview(self.db)).upstreams[0]
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
        original_ciphertext = encrypt_text("sk-original-seven")
        by_id[7].encrypted_api_key = original_ciphertext
        by_id[7].upstream_wallet_cost_usd = 12.375
        by_id[7].upstream_usage_unit = "USD"
        by_id[7].upstream_usage_checked_at = by_id[7].created_at
        by_id[8].encrypted_api_key = encrypt_text("sk-current-eight")
        await self.db.commit()

        self.sub2api.accounts[0]["name"] = "replacement-seven"
        self.sub2api.accounts[0]["created_at"] = "2026-07-15T12:00:00Z"
        overview = await self.service.overview(self.db)

        self.assertEqual(
            [account.management_account_id for account in overview.upstreams[0].accounts],
            [7, 8],
        )
        self.assertEqual(overview.unassigned_accounts, [])
        isolated = overview.upstreams[0].accounts[0]
        self.assertEqual(isolated.identity_binding_status, "mismatch")
        self.assertTrue(isolated.identity_rebind_required)
        self.assertFalse(isolated.managed)
        self.assertFalse(isolated.api_key_set)
        await self.db.refresh(by_id[7])
        self.assertEqual(by_id[7].encrypted_api_key, original_ciphertext)
        self.assertIsNone(by_id[7].upstream_wallet_cost_usd)
        self.assertIsNone(by_id[7].upstream_usage_unit)
        self.assertIsNone(by_id[7].upstream_usage_checked_at)
        archive = await self.db.scalar(
            select(ApiAccountDataArchive).where(
                ApiAccountDataArchive.management_account_id == 7
            )
        )
        self.assertIsNotNone(archive)
        self.assertEqual(archive.reason, "remote_identity_mismatch")
        self.assertEqual(archive.snapshot["upstream"]["upstream_wallet_cost_usd"], 12.375)
        self.assertEqual(archive.snapshot["upstream"]["upstream_usage_unit"], "USD")

        result = SimpleNamespace(
            platform_type="newapi",
            status="ok",
            groups=[{"id": "vip", "name": "VIP", "multiplier": 2.0}],
            account_group_matches={
                7: {"id": "vip", "name": "VIP", "multiplier": 2.0},
                8: {"id": "vip", "name": "VIP", "multiplier": 2.0},
            },
            discovered_upstream_recharge_multiplier=0.1,
            discovered_upstream_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            wallet_balance_usd=100,
            wallet_total_usd=None,
            wallet_used_usd=None,
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
            await self.service.discover_channel(self.db, channel.upstream_id)

        self.assertEqual(discovery.await_args.kwargs["account_api_keys"], {8: "sk-current-eight"})
        self.assertNotIn(7, [account_id for account_id, _rate in self.sub2api.rate_update_calls])
        await self.db.refresh(by_id[7])
        self.assertEqual(by_id[7].encrypted_api_key, original_ciphertext)

    async def test_identity_mismatch_archive_is_idempotent_per_rebind_episode(
        self,
    ) -> None:
        await self.service.overview(self.db)
        stored = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertIsNotNone(stored)
        stored.upstream_wallet_cost_usd = 1.25
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = stored.created_at
        await self.db.commit()

        self.sub2api.accounts[0]["created_at"] = "2026-07-15T12:00:00Z"
        await self.service.overview(self.db)
        await self.service.overview(self.db)
        await self.service.overview(self.db)

        archives = list(
            (
                await self.db.scalars(
                    select(ApiAccountDataArchive).where(
                        ApiAccountDataArchive.management_account_id == 7,
                        ApiAccountDataArchive.reason
                        == "remote_identity_mismatch",
                    )
                )
            ).all()
        )
        self.assertEqual(len(archives), 1)
        await self.db.refresh(stored)
        self.assertTrue(stored.api_key_origin_rebind_required)

        await self.service.accounts.upsert_account(
            self.db,
            7,
            self._account_update(
                7,
                confirm_identity_rebind=True,
                confirm_credential_rebind=True,
            ),
        )
        await self.db.refresh(stored)
        self.assertFalse(stored.api_key_origin_rebind_required)
        stored.upstream_wallet_cost_usd = 2.5
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = stored.updated_at
        await self.db.commit()

        self.sub2api.accounts[0]["created_at"] = "2026-07-16T12:00:00Z"
        await self.service.overview(self.db)
        archives = list(
            (
                await self.db.scalars(
                    select(ApiAccountDataArchive).where(
                        ApiAccountDataArchive.management_account_id == 7,
                        ApiAccountDataArchive.reason
                        == "remote_identity_mismatch",
                    )
                )
            ).all()
        )
        self.assertEqual(len(archives), 2)
        self.assertEqual(
            sorted(
                archive.snapshot["upstream"]["upstream_wallet_cost_usd"]
                for archive in archives
            ),
            [1.25, 2.5],
        )

    async def test_remote_name_only_change_reconciles_cached_name_and_binding(self) -> None:
        await self.service.overview(self.db)
        stored = (
            await self.db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 7
                )
            )
        ).scalar_one()
        original_ciphertext = encrypt_text("sk-name-reconcile")
        stable_fingerprint = self.service.accounts._remote_binding_fingerprint(
            self.sub2api.accounts[0]
        )
        stored.encrypted_api_key = original_ciphertext
        stored.remote_identity_fingerprint = (
            self.service.accounts._legacy_remote_binding_fingerprint(
                self.sub2api.accounts[0]
            )
        )
        stored.remote_upstream_api_key_id = 321
        stored.selected_group_id = "stable-group"
        stored.selected_group_name = "Stable group"
        stored.today_upstream_wallet_cost_usd = 4.5
        stored.today_upstream_usage_unit = "USD"
        await self.db.commit()

        self.sub2api.accounts[0]["name"] = "renamed-outside-the-plugin"
        overview = await self.service.overview(self.db)
        account = next(
            account
            for channel in overview.upstreams
            for account in channel.accounts
            if account.management_account_id == 7
        )

        await self.db.refresh(stored)
        self.assertEqual(account.remote_name, "renamed-outside-the-plugin")
        self.assertEqual(account.identity_binding_status, "bound")
        self.assertTrue(account.managed)
        self.assertTrue(account.api_key_set)
        self.assertEqual(stored.remote_name, "renamed-outside-the-plugin")
        self.assertEqual(
            stored.remote_identity_fingerprint,
            stable_fingerprint,
        )
        self.assertEqual(stored.encrypted_api_key, original_ciphertext)
        self.assertEqual(stored.remote_upstream_api_key_id, 321)
        self.assertEqual(
            (stored.selected_group_id, stored.selected_group_name),
            ("stable-group", "Stable group"),
        )
        self.assertEqual(stored.today_upstream_wallet_cost_usd, 4.5)

    async def test_remote_name_reconciliation_redacts_known_credentials(self) -> None:
        await self.service.overview(self.db)
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
        api_key = "sk-name-must-stay-encrypted"
        access_token = "at-name-must-stay-encrypted"
        by_id[7].encrypted_api_key = encrypt_text(api_key)
        by_id[8].encrypted_access_token = encrypt_text(access_token)
        await self.db.commit()

        self.sub2api.accounts[0]["name"] = api_key
        self.sub2api.accounts[1]["name"] = access_token
        overview = await self.service.overview(self.db)
        projected = {
            account.management_account_id: account
            for channel in overview.upstreams
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
            account.management_account_id: account
            for channel in ordinary.upstreams
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
            for channel in secret_overview.upstreams
            for account in channel.accounts
            if account.management_account_id == int(remote["id"])
        )
        self.assertEqual(secret_account.remote_name, "[redacted]")

        remote["name"] = "ordinary-name-after-secret"
        ordinary_overview = await self.service.overview(self.db)
        ordinary_account = next(
            account
            for channel in ordinary_overview.upstreams
            for account in channel.accounts
            if account.management_account_id == int(remote["id"])
        )

        self.assertEqual(ordinary_account.remote_name, "ordinary-name-after-secret")
        self.assertEqual(ordinary_account.identity_binding_status, "bound")
        self.assertTrue(ordinary_account.managed)

    async def test_inventory_scrubs_bound_plaintext_remote_name(self) -> None:
        await self.service.overview(self.db)
        stored = (
            await self.db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 7
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
            for channel in overview.upstreams
            for account in channel.accounts
            if account.management_account_id == 7
        )

        await self.db.refresh(stored)
        self.assertEqual(stored.remote_name, "[redacted]")
        self.assertEqual(projected.remote_name, "[redacted]")

    async def test_manual_bulk_sync_claims_only_confirmed_legacy_null_bindings(self) -> None:
        overview = await self.service.overview(self.db)
        accounts = [account for channel in overview.upstreams for account in channel.accounts]
        confirmations = {
            account.management_account_id: account.identity_fingerprint
            for account in accounts
        }
        configs = await self.db.execute(select(ApiAccount))
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
                if int(account["id"]) == config.management_account_id
            )
            self.assertEqual(
                config.remote_identity_fingerprint,
                self.service.accounts._remote_binding_fingerprint(remote),
            )

    def test_confirmation_schema_supports_more_than_500_pending_accounts(self) -> None:
        payload = UpstreamDiscoverAllRequest(
            confirm_legacy_bindings=True,
            account_bindings=[
                {
                    "management_account_id": account_id,
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

        configs = await self.db.execute(select(ApiAccount))
        stored = list(configs.scalars().all())
        for config in stored:
            config.remote_identity_fingerprint = None
        await self.db.commit()

        confirmation_overview = await self.service.overview(self.db)
        visible_accounts = [
            account
            for channel in confirmation_overview.upstreams
            for account in channel.accounts
        ]
        confirmations = {
            account.management_account_id: account.identity_fingerprint
            for account in visible_accounts
        }
        pending = next(
            account for account in visible_accounts if account.management_account_id == 8
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
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(rebound.api_endpoint_url, "https://replacement.example")
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

    async def test_legacy_unbound_inventory_sync_preserves_data_until_confirmation(
        self,
    ) -> None:
        await self.service.overview(self.db)
        stored = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertIsNotNone(stored)
        interval = UpstreamPriorityInterval(
            name="legacy-binding-pending",
            start_priority=40,
            end_priority=50,
            step=1,
        )
        self.db.add(interval)
        await self.db.flush()
        checked_at = datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc)
        stored.remote_identity_fingerprint = None
        stored.priority_interval_id = interval.id
        stored.desired_priority = 42
        stored.priority_sync_status = "synced"
        stored.upstream_wallet_cost_usd = 12.375
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = checked_at
        stored.today_upstream_wallet_cost_usd = 12.375
        stored.today_upstream_usage_unit = "USD"
        stored.today_upstream_usage_status = "ok"
        stored.today_upstream_usage_checked_at = checked_at
        await self.db.commit()

        overview = await self.service.overview(self.db)
        pending = next(
            account
            for channel in overview.upstreams
            for account in channel.accounts
            if account.management_account_id == 7
        )
        self.assertEqual(pending.identity_binding_status, "unbound")
        self.assertFalse(pending.api_key_origin_rebind_required)

        await self.db.refresh(stored)
        self.assertEqual(stored.priority_interval_id, interval.id)
        self.assertEqual(stored.desired_priority, 42)
        self.assertEqual(stored.priority_sync_status, "synced")
        self.assertEqual(stored.upstream_wallet_cost_usd, 12.375)
        self.assertEqual(stored.upstream_usage_unit, "USD")
        self.assertEqual(
            stored.upstream_usage_checked_at,
            checked_at.replace(tzinfo=None),
        )
        self.assertEqual(stored.today_upstream_wallet_cost_usd, 12.375)
        self.assertEqual(stored.today_upstream_usage_status, "ok")
        self.assertEqual(
            stored.today_upstream_usage_checked_at,
            checked_at.replace(tzinfo=None),
        )
        archives = list(
            (
                await self.db.scalars(
                    select(ApiAccountDataArchive).where(
                        ApiAccountDataArchive.management_account_id == 7
                    )
                )
            ).all()
        )
        self.assertEqual(archives, [])

    async def test_background_sync_never_claims_legacy_null_bindings(self) -> None:
        await self.service.overview(self.db)
        configs = await self.db.execute(select(ApiAccount))
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
        account = overview.upstreams[0].accounts[0]
        config = (
            await self.db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == account.management_account_id
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
                {account.management_account_id: account.identity_fingerprint},
            )

        self.assertEqual(rebound, 1)
        self.assertEqual(remote_reads.await_count, 2)
        await self.db.refresh(config)
        self.assertFalse(config.api_key_origin_rebind_required)

    async def test_channel_token_is_encrypted_and_never_returned(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        secret = "channel-access-token-private"
        refresh_secret = "channel-refresh-token-private"

        updated = await self.service.update_channel(
            self.db,
            channel.upstream_id,
            UpstreamUpdate(
                display_name="Example upstream",
                api_endpoint_url="https://upstream.example/v1",
                platform_type="newapi",
                upstream_user_id="42",
                access_token=secret,
                refresh_token=refresh_secret,
                upstream_recharge_multiplier_override=0.1,
            ),
        )

        stored = await self.db.get(Upstream, channel.upstream_id)
        self.assertIsNotNone(stored)
        self.assertTrue(updated.access_token_set)
        self.assertTrue(updated.refresh_token_set)
        self.assertNotIn("access_token", updated.model_dump())
        self.assertNotIn("refresh_token", updated.model_dump())
        self.assertNotEqual(stored.encrypted_access_token, secret)
        self.assertNotIn(secret, stored.encrypted_access_token or "")
        self.assertNotEqual(stored.encrypted_refresh_token, refresh_secret)
        self.assertNotIn(refresh_secret, stored.encrypted_refresh_token or "")

    async def test_channel_login_credentials_are_encrypted_and_never_returned(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        username = "xingchen@example.com"
        password = "login-password-private"

        updated = await self.service.update_channel(
            self.db,
            channel.upstream_id,
            UpstreamUpdate(
                platform_type="sub2api",
                login_username=username,
                login_password=password,
            ),
        )

        stored = await self.db.get(Upstream, channel.upstream_id)
        self.assertIsNotNone(stored)
        self.assertTrue(updated.login_credentials_set)
        self.assertNotIn(username, repr(updated.model_dump()))
        self.assertNotIn(password, repr(updated.model_dump()))
        self.assertNotEqual(stored.encrypted_login_username, username)
        self.assertNotEqual(stored.encrypted_login_password, password)
        self.assertEqual(decrypt_text(stored.encrypted_login_username), username)
        self.assertEqual(decrypt_text(stored.encrypted_login_password), password)

    async def test_login_credentials_restore_access_token_without_refresh_token(self) -> None:
        upstream_id = await self._configure_sub2api_credentials(access_token="", refresh_token="")
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(
                login_username="xingchen@example.com",
                login_password="login-password-private",
            ),
        )
        new_access_token = "at-login-private"
        discovery = AsyncMock(
            side_effect=[
                self._discovery_result(status="error", auth_rejected=True),
                self._discovery_result(wallet_balance_usd=73.5),
            ]
        )
        with (
            patch("app.services.upstream_channels.discover_upstream", new=discovery),
            patch(
                "app.services.upstream_channels.login_sub2api_tokens",
                new=AsyncMock(
                    return_value=Sub2ApiLoginTokenPair(access_token=new_access_token)
                ),
            ) as login,
        ):
            result = await self.service.discover_channel(self.db, upstream_id)

        login.assert_awaited_once_with(
            "https://upstream.example",
            "xingchen@example.com",
            "login-password-private",
        )
        self.assertEqual(discovery.await_count, 2)
        self.assertEqual(discovery.await_args_list[1].kwargs["access_token"], new_access_token)
        self.assertEqual(result.wallet_balance_usd, 73.5)
        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), new_access_token)
        self.assertIsNone(stored.encrypted_refresh_token)
        configs = list((await self.db.scalars(select(ApiAccount).where(ApiAccount.upstream_id == upstream_id))).all())
        self.assertTrue(configs)
        self.assertTrue(all(decrypt_text(config.encrypted_access_token) == new_access_token for config in configs))

    async def test_batch_discovery_logs_in_when_missing_access_token_even_if_public_probe_succeeds(self) -> None:
        upstream_id = await self._configure_sub2api_credentials(access_token="", refresh_token="")
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(
                login_username="xingchen@example.com",
                login_password="login-password-private",
            ),
        )
        new_access_token = "at-login-public-probe-private"
        discovery = AsyncMock(return_value=self._discovery_result(status="ok", auth_rejected=False))
        with (
            patch("app.services.upstream_channels.discover_upstream", new=discovery),
            patch(
                "app.services.upstream_channels.login_sub2api_tokens",
                new=AsyncMock(
                    return_value=Sub2ApiLoginTokenPair(access_token=new_access_token)
                ),
            ) as login,
        ):
            result = await self.service.discover_all(
                self.db,
                max_concurrency=1,
                require_management_credentials=True,
            )

        login.assert_awaited_once_with(
            "https://upstream.example",
            "xingchen@example.com",
            "login-password-private",
        )
        self.assertEqual(discovery.await_count, 2)
        self.assertIsNone(discovery.await_args_list[0].kwargs["access_token"])
        self.assertEqual(discovery.await_args_list[1].kwargs["access_token"], new_access_token)
        self.assertEqual((result.succeeded, result.failed, result.skipped), (1, 0, 0))
        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), new_access_token)

    async def test_login_without_refresh_token_clears_failed_refresh_token(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(
                login_username="xingchen@example.com",
                login_password="login-password-private",
            ),
        )
        discovery = AsyncMock(
            side_effect=[
                self._discovery_result(status="error", auth_rejected=True),
                self._discovery_result(wallet_balance_usd=74.5),
            ]
        )
        with (
            patch("app.services.upstream_channels.discover_upstream", new=discovery),
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(return_value=None),
            ) as refresh,
            patch(
                "app.services.upstream_channels.login_sub2api_tokens",
                new=AsyncMock(
                    return_value=Sub2ApiLoginTokenPair(access_token="at-login-no-rt")
                ),
            ) as login,
        ):
            await self.service.discover_channel(self.db, upstream_id)

        refresh.assert_awaited_once_with("https://upstream.example", "rt-old-private")
        login.assert_awaited_once()
        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), "at-login-no-rt")
        self.assertIsNone(stored.encrypted_refresh_token)

    async def test_channel_base_url_update_propagates_to_linked_sub2api_accounts(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]

        await self.service.update_channel(
            self.db,
            channel.upstream_id,
            UpstreamUpdate(
                api_endpoint_url="https://replacement.example",
                confirm_credential_rebind=True,
            ),
        )

        self.assertEqual(
            self.sub2api.api_endpoint_url_update_calls,
            [(7, "https://replacement.example"), (8, "https://replacement.example")],
        )
        self.assertTrue(
            all(
                account["credentials"]["base_url"] == "https://replacement.example"
                for account in self.sub2api.accounts
            )
        )
        configs = list((await self.db.scalars(select(ApiAccount))).all())
        self.assertEqual(
            {config.api_endpoint_url for config in configs},
            {"https://replacement.example"},
        )

    async def test_channel_base_url_partial_failure_rolls_back_remote_and_local_state(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        original_update = self.sub2api.update_account_base_url

        async def fail_second(account_id, base_url, *, validate_current=None):
            if int(account_id) == 8 and base_url == "https://replacement.example":
                raise RuntimeError("synthetic partial failure")
            return await original_update(
                account_id,
                base_url,
                validate_current=validate_current,
            )

        with patch.object(self.sub2api, "update_account_base_url", new=fail_second):
            with self.assertRaises(ApiAccountServiceError):
                await self.service.update_channel(
                    self.db,
                    channel.upstream_id,
                    UpstreamUpdate(
                        api_endpoint_url="https://replacement.example",
                        confirm_credential_rebind=True,
                    ),
                )

        stored = await self.db.get(Upstream, channel.upstream_id)
        self.assertEqual(stored.api_endpoint_url, "https://upstream.example")
        self.assertTrue(
            all(
                _remote_base_url(account) == "https://upstream.example"
                for account in self.sub2api.accounts
            )
        )
        event = await self.db.scalar(
            select(AppEvent)
            .where(AppEvent.kind == "upstream_url_update_failed")
            .order_by(AppEvent.id.desc())
        )
        self.assertIsNotNone(event)
        self.assertFalse(event.details["rollback_incomplete"])

    async def test_channel_base_url_rollback_failure_is_recorded(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        original_update = self.sub2api.update_account_base_url

        async def fail_update_and_rollback(account_id, base_url, *, validate_current=None):
            if int(account_id) == 8 and base_url == "https://replacement.example":
                raise RuntimeError("synthetic partial failure")
            if int(account_id) == 7 and base_url == "https://upstream.example":
                raise RuntimeError("synthetic rollback failure")
            return await original_update(
                account_id,
                base_url,
                validate_current=validate_current,
            )

        with patch.object(
            self.sub2api,
            "update_account_base_url",
            new=fail_update_and_rollback,
        ):
            with self.assertRaisesRegex(ApiAccountServiceError, "Rollback was incomplete"):
                await self.service.update_channel(
                    self.db,
                    channel.upstream_id,
                    UpstreamUpdate(
                        api_endpoint_url="https://replacement.example",
                        confirm_credential_rebind=True,
                    ),
                )

        stored = await self.db.get(Upstream, channel.upstream_id)
        self.assertEqual(stored.api_endpoint_url, "https://upstream.example")
        event = await self.db.scalar(
            select(AppEvent)
            .where(AppEvent.kind == "upstream_url_update_failed")
            .order_by(AppEvent.id.desc())
        )
        self.assertIsNotNone(event)
        self.assertTrue(event.details["rollback_incomplete"])

    async def test_account_discovery_redacts_channel_refresh_token_from_remote_name(self) -> None:
        refresh_secret = "rt-remote-name-must-not-leak"
        upstream_id = await self._configure_sub2api_credentials(
            refresh_token=refresh_secret,
        )
        remote = self.sub2api.accounts[0]
        remote["name"] = refresh_secret
        config = (
            await self.db.execute(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == int(remote["id"])
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
            for channel in overview.upstreams
            for account in channel.accounts
            if account.management_account_id == int(remote["id"])
        )
        listed = await self.service.accounts.list_accounts(self.db)
        listed_account = next(
            account for account in listed if account.management_account_id == int(remote["id"])
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
        self.assertEqual(config.upstream_id, upstream_id)
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
        by_url = {item.api_endpoint_url: item for item in overview.upstreams}
        source = by_url["https://upstream.example"]
        target = by_url["https://other.example"]

        with self.assertRaises(ApiAccountServiceError) as context:
            await self.service.update_channel(
                self.db,
                source.upstream_id,
                UpstreamUpdate(api_endpoint_url="https://OTHER.example/api/v1/"),
            )

        self.assertEqual(context.exception.status_code, 409)
        await self.db.rollback()
        source_row = await self.db.get(Upstream, source.upstream_id)
        target_row = await self.db.get(Upstream, target.upstream_id)
        self.assertEqual(source_row.api_endpoint_url, "https://upstream.example")
        self.assertEqual(target_row.api_endpoint_url, "https://other.example")

    async def test_shared_credential_change_invalidates_every_bound_account_preview(self) -> None:
        overview = await self.service.overview(self.db)
        upstream_id = overview.upstreams[0].upstream_id
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.resolved_platform_type = "newapi"
        channel.group_options = [{"id": "default", "name": "Default", "multiplier": 1.0}]
        channel.discovered_upstream_recharge_multiplier = 0.1
        channel.upstream_recharge_multiplier = 0.1
        channel.recharge_multiplier_source = "status.price"
        channel.recharge_multiplier_status = "ok"
        channel.wallet_balance_usd = 12.5
        channel.balance_status = "ok"
        channel.balance_guard_state = "insufficient"
        channel.balance_guard_basis = "wallet"
        channel.balance_guard_value = -1.5
        channel.balance_guard_checked_at = datetime(
            2026, 7, 16, 8, 0, tzinfo=timezone.utc
        )
        channel.balance_guard_episode_id = "old-balance-episode"
        channel.balance_guard_paused_count = 2
        self.db.add(
            NotificationOutbox(
                event_type="upstream_balance_low",
                dedupe_key=f"upstream-balance-low:{upstream_id}:old-balance-episode",
                title="Old balance alert",
                message="Old balance alert",
            )
        )

        configs_result = await self.db.execute(
            select(ApiAccount).where(ApiAccount.upstream_id == upstream_id)
        )
        configs = list(configs_result.scalars().all())
        self.assertEqual(len(configs), 2)
        for config in configs:
            config.discovered_upstream_group_multiplier = 1.0
            config.upstream_group_multiplier = 1.0
            config.group_multiplier_source = "upstream"
            config.group_multiplier_status = "ok"
            config.expected_management_billing_multiplier = 0.1
            config.last_discovered_at = channel.updated_at
            config.upstream_wallet_cost_usd = 12.375
            config.upstream_usage_unit = "USD"
            config.upstream_usage_checked_at = channel.updated_at
        await self.db.commit()

        secret = "fictional-shared-token-rotated"
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(
                access_token=secret,
                upstream_recharge_multiplier_override=None,
            ),
        )

        refreshed_channel = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(refreshed_channel)
        self.assertEqual(refreshed_channel.resolved_platform_type, "newapi")
        self.assertEqual(
            refreshed_channel.group_options,
            [{"id": "default", "name": "Default", "multiplier": 1.0}],
        )
        self.assertEqual(refreshed_channel.upstream_recharge_multiplier, 0.1)
        self.assertEqual(refreshed_channel.recharge_multiplier_status, "ok")
        self.assertEqual(refreshed_channel.wallet_balance_usd, 12.5)
        self.assertEqual(refreshed_channel.balance_status, "ok")
        self.assertEqual(refreshed_channel.balance_guard_state, "not_checked")
        self.assertIsNone(refreshed_channel.balance_guard_basis)
        self.assertIsNone(refreshed_channel.balance_guard_value)
        self.assertIsNone(refreshed_channel.balance_guard_checked_at)
        self.assertIsNone(refreshed_channel.balance_guard_episode_id)
        self.assertEqual(refreshed_channel.balance_guard_paused_count, 0)
        self.assertNotEqual(refreshed_channel.encrypted_access_token, secret)
        notification = await self.db.scalar(select(NotificationOutbox))
        self.assertEqual(notification.status, "canceled")

        refreshed_configs_result = await self.db.execute(
            select(ApiAccount).where(ApiAccount.upstream_id == upstream_id)
        )
        refreshed_configs = list(refreshed_configs_result.scalars().all())
        for config in refreshed_configs:
            self.assertIsNone(config.discovered_upstream_group_multiplier)
            self.assertIsNone(config.upstream_group_multiplier)
            self.assertIsNone(config.group_multiplier_source)
            self.assertEqual(config.group_multiplier_status, "not_discovered")
            self.assertIsNone(config.expected_management_billing_multiplier)
            self.assertIsNone(config.last_discovered_at)
            self.assertIsNone(config.upstream_wallet_cost_usd)
            self.assertIsNone(config.upstream_usage_unit)
            self.assertIsNone(config.upstream_usage_checked_at)
            self.assertEqual(config.encrypted_access_token, refreshed_channel.encrypted_access_token)

    async def test_shared_identity_change_clears_existing_pause_holds(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        now = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
        self.service.accounts.set_pause_hold(
            config,
            "upstream_rate_increase",
            active=True,
            scope_upstream_id=upstream_id,
            recovery_mode="rate_within_threshold",
            now=now,
            evidence={"baseline_multiplier": 1.0, "observed_multiplier": 1.5},
        )
        config.pause_owned_by_plugin = True
        config.auto_pause_episode_id = "legacy-rate-episode"
        config.auto_pause_upstream_id = upstream_id
        config.auto_paused_at = now
        config.pause_operation = "paused"
        self.service.accounts.sync_pause_compatibility_fields(config)
        self.sub2api.accounts[0]["schedulable"] = False
        await self.db.commit()

        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(access_token="rotated-shared-access-token"),
        )

        await self.db.refresh(config, attribute_names=["pause_holds"])
        self.assertFalse(any(hold.active for hold in config.pause_holds))
        self.assertTrue(config.pause_owned_by_plugin)
        self.assertEqual(config.auto_pause_episode_id, "legacy-rate-episode")
        self.assertEqual(config.auto_pause_upstream_id, upstream_id)
        self.assertEqual(config.pause_operation, "paused")
        self.assertIsNone(config.auto_disabled_reason)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(config, attribute_names=["pause_holds"])
        self.assertFalse(config.pause_owned_by_plugin)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, True)])

    async def test_account_credential_change_preserves_restore_ownership(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        now = datetime(2026, 7, 18, 8, 30, tzinfo=timezone.utc)
        self.service.accounts.set_pause_hold(
            config,
            "upstream_rate_increase",
            active=True,
            scope_upstream_id=upstream_id,
            recovery_mode="rate_within_threshold",
            now=now,
            evidence={"baseline_multiplier": 1.0, "observed_multiplier": 1.5},
        )
        config.pause_owned_by_plugin = True
        config.auto_pause_episode_id = "account-credential-episode"
        config.auto_pause_upstream_id = upstream_id
        config.auto_paused_at = now
        config.pause_operation = "paused"
        self.service.accounts.sync_pause_compatibility_fields(config)
        self.sub2api.accounts[0]["schedulable"] = False
        await self.db.commit()

        updated = await self.service.accounts.upsert_account(
            self.db,
            7,
            self._account_update(7, access_token="rotated-account-access-token"),
        )

        await self.db.refresh(config, attribute_names=["pause_holds"])
        self.assertFalse(any(hold.active for hold in config.pause_holds))
        self.assertTrue(config.pause_owned_by_plugin)
        self.assertTrue(updated.auto_restore_eligible)
        self.assertEqual(updated.active_pause_holds, [])
        self.assertEqual(config.auto_pause_episode_id, "account-credential-episode")
        self.assertEqual(config.pause_operation, "paused")
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(config, attribute_names=["pause_holds"])
        self.assertFalse(config.pause_owned_by_plugin)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, True)])

    async def test_channel_routes_only_reveal_credentials_from_explicit_no_store_endpoint(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")

        async def fake_db():
            async with self.session_factory() as session:
                yield session

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_service] = lambda: self.service
        secret = "fictional-route-access-token-private"
        refresh_secret = "fictional-route-refresh-token-private"
        login_username = "fictional-route-user@example.com"
        login_password = "fictional-route-password-private"
        with TestClient(app) as client:
            updated_response = client.put(
                f"/api/upstreams/{upstream_id}",
                json={
                    "access_token": secret,
                    "refresh_token": refresh_secret,
                    "login_username": login_username,
                    "login_password": login_password,
                },
            )
            overview_response = client.get("/api/upstreams")
            credentials_response = client.get(
                f"/api/upstreams/{upstream_id}/credentials"
            )

        self.assertEqual(updated_response.status_code, 200, updated_response.text)
        self.assertEqual(overview_response.status_code, 200, overview_response.text)
        self.assertEqual(
            credentials_response.status_code, 200, credentials_response.text
        )
        self.assertEqual(
            credentials_response.json(),
            {
                "access_token": secret,
                "refresh_token": refresh_secret,
                "login_username": login_username,
                "login_password": login_password,
            },
        )
        self.assertEqual(credentials_response.headers["cache-control"], "no-store")
        self.assertEqual(credentials_response.headers["pragma"], "no-cache")
        self.assertEqual(credentials_response.headers["expires"], "0")
        async with self.session_factory() as session:
            stored = await session.get(Upstream, upstream_id)
            self.assertIsNotNone(stored)
            ciphertext = stored.encrypted_access_token or ""
            refresh_ciphertext = stored.encrypted_refresh_token or ""
            username_ciphertext = stored.encrypted_login_username or ""
            password_ciphertext = stored.encrypted_login_password or ""
        self.assertTrue(ciphertext)
        self.assertTrue(refresh_ciphertext)
        self.assertTrue(username_ciphertext)
        self.assertTrue(password_ciphertext)

        for payload in (updated_response.json(), overview_response.json()):
            serialized = str(payload)
            self.assertNotIn(secret, serialized)
            self.assertNotIn(ciphertext, serialized)
            self.assertNotIn(refresh_secret, serialized)
            self.assertNotIn(refresh_ciphertext, serialized)
            self.assertNotIn(login_username, serialized)
            self.assertNotIn(username_ciphertext, serialized)
            self.assertNotIn(login_password, serialized)
            self.assertNotIn(password_ciphertext, serialized)
            self.assertNotIn("access_token", nested_keys(payload))
            self.assertNotIn("encrypted_access_token", nested_keys(payload))
            self.assertNotIn("refresh_token", nested_keys(payload))
            self.assertNotIn("encrypted_refresh_token", nested_keys(payload))
            self.assertNotIn("login_username", nested_keys(payload))
            self.assertNotIn("encrypted_login_username", nested_keys(payload))
            self.assertNotIn("login_password", nested_keys(payload))
            self.assertNotIn("encrypted_login_password", nested_keys(payload))

    async def test_channel_discovery_sets_one_balance_and_derives_each_account_rate(self) -> None:
        overview = await self.service.overview(self.db)
        upstream_id = overview.upstreams[0].upstream_id
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(access_token="channel-management-token"),
        )
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
        alpha_key = "sk-channel-alpha-private-A1B2"
        beta_key = "sk-channel-beta-private-C3D4"
        by_id[7].encrypted_api_key = encrypt_text(alpha_key)
        self.sub2api.exported_api_keys = {8: beta_key}
        await self.db.commit()

        result = SimpleNamespace(
            platform_type="newapi",
            status="ok",
            groups=[
                {"id": "default", "name": "Default", "multiplier": 1.0, "source": "self.groups"},
                {"id": "vip", "name": "VIP", "multiplier": 2.0, "source": "self.groups"},
            ],
            account_group_matches={
                7: {"id": "default", "name": "Default", "multiplier": 1.0},
                8: {"id": "vip", "name": "VIP", "multiplier": 2.0},
            },
            discovered_upstream_recharge_multiplier=0.1,
            discovered_upstream_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            wallet_balance_usd=1476.34,
            wallet_total_usd=None,
            wallet_used_usd=123.66,
            balance_unit="USD",
            balance_status="ok",
            balance_message="Balance available.",
        )
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ) as discover:
            channel = await self.service.discover_channel(self.db, upstream_id)

        discover.assert_awaited_once()
        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(
            discover.await_args.kwargs["account_api_keys"],
            {7: alpha_key, 8: beta_key},
        )
        self.assertEqual(channel.wallet_balance_usd, 1476.34)
        self.assertEqual([account.expected_management_billing_multiplier for account in channel.accounts], [1.0, 2.0])
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
            await self.service.discover_channel(self.db, upstream_id)
        self.assertEqual(self.sub2api.export_calls, [])
        serialized = str(channel.model_dump())
        for secret in (alpha_key, beta_key, by_id[7].encrypted_api_key or ""):
            self.assertNotIn(secret, serialized)

    async def test_channel_manual_recharge_overrides_discovered_recharge(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(upstream_recharge_multiplier_override=1.0),
        )
        result = self._discovery_result()
        result.discovered_upstream_recharge_multiplier = 7.3
        result.discovered_upstream_recharge_multiplier_source = "status.price"

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            channel = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(channel.discovered_upstream_recharge_multiplier, 7.3)
        self.assertEqual(channel.upstream_recharge_multiplier, 1.0)
        self.assertEqual(channel.recharge_multiplier_source, "manual")
        self.assertEqual(channel.recharge_multiplier_status, "manual")

    async def test_channel_discovery_uses_configured_display_timezone(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        self.runtime_config.get_upstream_monitor_auto_probe_enabled.return_value = False
        self.runtime_config.get_public_settings.return_value = {
            "display_timezone": "America/New_York"
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ) as discover:
            await self.service.discover_channel(self.db, upstream_id)

        discover.assert_awaited_once()
        self.assertEqual(
            discover.await_args.kwargs["today_timezone"],
            "America/New_York",
        )
        self.assertFalse(
            discover.await_args.kwargs["include_upstream_monitor_details"]
        )
        self.assertFalse(discover.await_args.kwargs["include_upstream_monitors"])

    async def test_refresh_upstream_monitors_opts_in_to_detail_discovery(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        result = self._discovery_result()
        result.upstream_monitors = [
            {
                "id": 17,
                "name": "Primary monitor",
                "primary_status": "operational",
            }
        ]
        result.upstream_monitors_total = 1
        result.upstream_monitors_status = "ok"
        result.upstream_monitors_message = "Read 1 upstream channel monitor(s)."

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ) as discover:
            refreshed = await self.service.refresh_upstream_monitors(
                self.db,
                upstream_id,
            )

        discover.assert_awaited_once()
        self.assertTrue(
            discover.await_args.kwargs["include_upstream_monitor_details"]
        )
        self.assertTrue(discover.await_args.kwargs["include_upstream_monitors"])
        self.assertTrue(discover.await_args.kwargs["monitor_only"])
        self.assertEqual(discover.await_args.kwargs["account_api_keys"], {})
        self.assertEqual(refreshed.upstream_id, upstream_id)
        self.assertEqual(refreshed.upstream_monitor_count, 1)
        self.assertEqual(refreshed.upstream_monitor_status, "ok")
        self.assertEqual(
            refreshed.upstream_monitors[0]["primary_status"],
            "operational",
        )
        self.assertIsNotNone(refreshed.upstream_monitor_checked_at)
        stored_channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNone(stored_channel.wallet_balance_usd)
        self.assertIsNone(stored_channel.last_discovered_at)
        self.assertEqual(self.sub2api.today_cost_calls, [])
        self.assertEqual(self.sub2api.rate_update_calls, [])
        self.assertEqual(self.sub2api.schedulable_calls, [])

    async def test_disabled_auto_probe_preserves_cached_upstream_monitors(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        channel = await self.db.get(Upstream, upstream_id)
        cached_checked_at = datetime(2026, 7, 20, 3, 4, tzinfo=timezone.utc)
        channel.upstream_monitors = [
            {
                "id": 17,
                "name": "Cached monitor",
                "primary_status": "available",
                "timeline": [],
            }
        ]
        channel.upstream_monitor_count = 1
        channel.upstream_monitor_status = "ok"
        channel.upstream_monitor_checked_at = cached_checked_at
        await self.db.commit()
        self.runtime_config.get_upstream_monitor_auto_probe_enabled.return_value = False
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        result = self._discovery_result(
            upstream_monitors=[
                {
                    "id": 99,
                    "name": "Unrequested summary",
                    "primary_status": "unavailable",
                    "timeline": [],
                }
            ]
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ) as discover:
            refreshed = await self.service.discover_channel(self.db, upstream_id)

        self.assertFalse(discover.await_args.kwargs["include_upstream_monitor_details"])
        self.assertFalse(discover.await_args.kwargs["include_upstream_monitors"])
        self.assertEqual(refreshed.upstream_monitors[0]["id"], 17)
        self.assertEqual(
            refreshed.upstream_monitor_checked_at.replace(tzinfo=timezone.utc),
            cached_checked_at,
        )
        self.assertEqual(refreshed.upstream_monitor_guard_state, "account_scoped")

    async def test_full_discovery_does_not_apply_channel_level_connection_fallbacks(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        result = self._discovery_result(
            upstream_monitors=[
                {
                    "id": 17,
                    "name": "Unavailable monitor",
                    "primary_status": "unavailable",
                    "timeline": [],
                }
            ]
        )
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            refreshed = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(self.sub2api.connection_test_calls, [])
        self.assertNotIn("effective_status", refreshed.upstream_monitors[0])
        self.assertEqual(refreshed.upstream_monitors[0]["primary_status"], "unavailable")

    async def test_refresh_upstream_monitors_allows_configured_empty_channel(self) -> None:
        empty_channel = Upstream(
            display_name="Monitor-only upstream",
            api_endpoint_url="https://monitor-only.example",
            platform_type="sub2api",
            resolved_platform_type="sub2api",
            encrypted_access_token=encrypt_text("monitor-access-token"),
        )
        self.db.add(empty_channel)
        await self.db.commit()

        result = self._discovery_result()
        result.upstream_monitors = [
            {
                "id": 17,
                "name": "Primary monitor",
                "primary_status": "available",
            }
        ]
        result.upstream_monitors_total = 1
        result.upstream_monitors_status = "ok"
        result.upstream_monitors_message = "Read 1 upstream channel monitor(s)."

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ) as discover:
            refreshed = await self.service.refresh_upstream_monitors(
                self.db,
                empty_channel.id,
            )

        discover.assert_awaited_once()
        self.assertTrue(discover.await_args.kwargs["monitor_only"])
        self.assertEqual(discover.await_args.kwargs["account_api_keys"], {})
        self.assertEqual(refreshed.upstream_monitor_count, 1)
        self.assertEqual(refreshed.upstream_monitors[0]["primary_status"], "available")

    async def test_monitor_inventory_is_never_treated_as_site_health(self) -> None:
        monitors = [
            {
                "id": index + 1,
                "name": f"Monitor {index + 1}",
                "primary_status": "unavailable",
                "timeline": [],
            }
            for index in range(100)
        ]
        channel = Upstream(
            display_name="Large monitor inventory",
            api_endpoint_url="https://large-monitor.example",
            platform_type="sub2api",
            upstream_monitor_status="ok",
            upstream_monitor_count=101,
            upstream_monitors=monitors,
        )

        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        action = await self.service._prepare_upstream_monitor_guard(
            channel,
            self.runtime_config,
        )
        self.assertIsNone(action)
        self.assertEqual(channel.upstream_monitor_guard_state, "account_scoped")
        channel.upstream_monitor_count = 100
        action = await self.service._prepare_upstream_monitor_guard(
            channel,
            self.runtime_config,
        )
        self.assertIsNone(action)
        self.assertEqual(channel.upstream_monitor_guard_state, "account_scoped")

    async def test_monitor_only_401_rotates_sub2api_tokens_and_retries_once(self) -> None:
        upstream_id = await self._configure_sub2api_credentials(
            access_token="expired-monitor-access-token",
            refresh_token="monitor-refresh-token",
        )
        rejected = self._discovery_result(status="error", auth_rejected=True)
        rejected.upstream_monitors = []
        rejected.upstream_monitors_total = 0
        rejected.upstream_monitors_status = "credentials_rejected"
        rejected.upstream_monitors_message = "Credentials rejected."
        recovered = self._discovery_result()
        recovered.upstream_monitors = [
            {
                "id": 17,
                "name": "Primary monitor",
                "primary_status": "available",
            }
        ]
        recovered.upstream_monitors_total = 1
        recovered.upstream_monitors_status = "ok"
        recovered.upstream_monitors_message = "Read 1 upstream channel monitor(s)."

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(side_effect=[rejected, recovered]),
            ) as discover,
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(
                    return_value=Sub2ApiTokenPair(
                        access_token="rotated-monitor-access-token",
                        refresh_token="rotated-monitor-refresh-token",
                    )
                ),
            ) as refresh,
        ):
            result = await self.service.refresh_upstream_monitors(self.db, upstream_id)

        self.assertEqual(discover.await_count, 2)
        self.assertTrue(
            all(call.kwargs["monitor_only"] for call in discover.await_args_list)
        )
        self.assertEqual(
            discover.await_args_list[1].kwargs["access_token"],
            "rotated-monitor-access-token",
        )
        refresh.assert_awaited_once_with(
            "https://upstream.example",
            "monitor-refresh-token",
        )
        self.assertEqual(result.upstream_monitor_status, "ok")
        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertEqual(
            decrypt_text(stored.encrypted_access_token),
            "rotated-monitor-access-token",
        )
        self.assertEqual(
            decrypt_text(stored.encrypted_refresh_token),
            "rotated-monitor-refresh-token",
        )

    async def test_monitor_only_auth_refresh_failure_marks_token_invalid_without_unconfigured_notification(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        rejected = self._discovery_result(status="error", auth_rejected=True)

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=rejected),
            ),
            patch(
                "app.services.upstream_channels.refresh_sub2api_tokens",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await self.service.refresh_upstream_monitors(self.db, upstream_id)

        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        notification = await self.db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == "upstream_token_invalid"
            )
        )
        self.assertEqual(result.upstream_monitor_status, "token_invalid")
        self.assertEqual(stored.balance_status, "token_invalid")
        self.assertEqual(stored.last_error, "Upstream channel token is invalid.")
        self.assertIsNone(stored.balance_message)
        self.assertIsNone(notification)

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
            [account.remote_platform for account in result.upstreams[0].accounts],
            ["openai", "anthropic"],
        )
        serialized = str(result.model_dump())
        for secret in self.sub2api.exported_api_keys.values():
            self.assertNotIn(secret, serialized)

    async def test_automatic_discovery_does_not_sync_new_api_key_inventory(self) -> None:
        initial = await self.service.overview(self.db)
        initial_upstream_ids = {channel.upstream_id for channel in initial.upstreams}
        initial_config_rows = (
            await self.db.execute(select(ApiAccount))
        ).scalars().all()
        initial_bindings = {
            row.management_account_id: (row.upstream_id, row.remote_identity_fingerprint)
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
            await self.db.execute(select(ApiAccount))
        ).scalars().all()
        self.assertEqual(
            {
                row.management_account_id: (row.upstream_id, row.remote_identity_fingerprint)
                for row in after_automatic_configs
            },
            initial_bindings,
        )
        after_automatic_channels = (
            await self.db.execute(select(Upstream))
        ).scalars().all()
        self.assertEqual({upstream.id for upstream in after_automatic_channels}, initial_upstream_ids)

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result()),
        ):
            manual = await self.service.discover_all(self.db)

        self.assertEqual(manual.total, 2)
        added = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 9
            )
        )
        self.assertIsNotNone(added)
        self.assertIsNotNone(added.upstream_id)

    async def test_missing_recharge_field_clears_stale_discovered_value(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.discovered_upstream_recharge_multiplier = 0.25
        channel.upstream_recharge_multiplier = 0.25
        channel.recharge_multiplier_source = "payment.config"
        channel.recharge_multiplier_status = "ok"
        await self.db.commit()

        result = self._discovery_result()
        result.discovered_upstream_recharge_multiplier = None
        result.discovered_upstream_recharge_multiplier_source = None
        result.recharge_discovery_status = "missing"
        result.account_group_matches = {}
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, upstream_id)

        self.assertIsNone(discovered.discovered_upstream_recharge_multiplier)
        self.assertEqual(discovered.upstream_recharge_multiplier, 1.0)
        self.assertEqual(discovered.recharge_multiplier_source, "default")
        self.assertEqual(discovered.recharge_multiplier_status, "default_missing")

    async def test_discovery_persists_key_usage_and_channel_today_balance_use(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
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
            today_upstream_wallet_cost_usd=3.25,
        )
        result.account_group_matches = {
            7: {"id": "default", "name": "Default", "multiplier": 1.0}
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(discovered.today_upstream_wallet_cost_usd, 3.25)
        self.assertEqual(discovered.today_balance_unit, "USD")
        self.assertEqual(discovered.today_balance_status, "ok")
        account = next(item for item in discovered.accounts if item.management_account_id == 7)
        self.assertEqual(account.upstream_wallet_cost_usd, 12.375)
        self.assertEqual(account.upstream_usage_unit, "USD")
        self.assertIsNotNone(account.upstream_usage_checked_at)

        stored_channel = await self.db.get(Upstream, upstream_id)
        stored_account = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertEqual(stored_channel.today_upstream_wallet_cost_usd, 3.25)
        self.assertEqual(stored_channel.yesterday_upstream_wallet_cost_usd, 2.75)
        self.assertEqual(stored_account.upstream_wallet_cost_usd, 12.375)

    async def test_discovery_falls_back_to_converted_local_today_cost(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        self.sub2api.today_costs[7] = 3.0
        result = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="vip",
                    group_name="VIP",
                )
            }
        )
        result.groups = [{"id": "vip", "name": "VIP", "multiplier": 2.0}]
        result.account_group_matches = {
            7: {"id": "vip", "name": "VIP", "multiplier": 2.0}
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, upstream_id)

        account = next(item for item in discovered.accounts if item.management_account_id == 7)
        self.assertEqual(account.today_upstream_wallet_cost_usd, 6.0)
        self.assertEqual(
            account.today_upstream_usage_source,
            "local_sub2api_today_cost_converted",
        )
        self.assertEqual(account.today_upstream_usage_status, "estimated")
        daily_usage = await self.db.scalar(
            select(ApiAccountDailyUsage).where(
                ApiAccountDailyUsage.management_account_id == 7
            )
        )
        self.assertIsNotNone(daily_usage)
        self.assertAlmostEqual(daily_usage.management_account_cost_usd, 3.0)
        self.assertAlmostEqual(daily_usage.management_recharge_multiplier, 0.1)
        self.assertIsNone(daily_usage.management_user_charge_usd)
        self.assertIsNone(daily_usage.actual_income_cny)
        self.assertIsNone(daily_usage.income_unit)

    async def test_successful_discovery_keeps_same_day_usage_when_upstream_omits_a_key(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs = (
            await self.db.execute(select(ApiAccount))
        ).scalars().all()
        checked_at = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        by_id = {config.management_account_id: config for config in configs}
        for config in by_id.values():
            config.upstream_wallet_cost_usd = 12.375
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
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch(
                "app.services.upstream_channels._utcnow",
                return_value=now,
            ),
        ):
            discovered = await self.service.discover_channel(self.db, upstream_id)

        matched = next(item for item in discovered.accounts if item.management_account_id == 7)
        unmatched = next(item for item in discovered.accounts if item.management_account_id == 8)
        for account in (matched, unmatched):
            self.assertEqual(account.upstream_wallet_cost_usd, 12.375)
            self.assertEqual(account.upstream_usage_unit, "USD")
            self.assertEqual(
                account.upstream_usage_checked_at,
                checked_at.replace(tzinfo=None),
            )
            self.assertEqual(account.today_upstream_wallet_cost_usd, 12.375)
            self.assertEqual(account.today_upstream_usage_status, "stale")
            self.assertEqual(
                account.today_upstream_usage_checked_at,
                checked_at.replace(tzinfo=None),
            )

    async def test_discovery_clears_stale_today_usage_when_upstream_stops_providing_it(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        channel = await self.db.get(Upstream, upstream_id)
        channel.today_upstream_wallet_cost_usd = 3.25
        channel.today_balance_unit = "USD"
        channel.today_balance_status = "ok"
        channel.today_balance_checked_at = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        channel.yesterday_upstream_wallet_cost_usd = 2.75
        channel.yesterday_balance_unit = "USD"
        channel.yesterday_balance_status = "ok"
        channel.yesterday_balance_checked_at = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
        await self.db.commit()

        result = self._discovery_result(
            today_upstream_wallet_cost_usd=None,
            today_balance_status="unsupported",
            yesterday_upstream_wallet_cost_usd=None,
            yesterday_balance_status="not_available",
        )
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(discovered.today_balance_status, "unsupported")
        self.assertIsNone(discovered.today_upstream_wallet_cost_usd)
        self.assertIsNone(discovered.today_balance_unit)
        self.assertIsNone(discovered.today_balance_checked_at)
        self.assertEqual(discovered.yesterday_balance_status, "not_available")
        self.assertIsNone(discovered.yesterday_upstream_wallet_cost_usd)
        self.assertIsNone(discovered.yesterday_balance_unit)
        self.assertIsNone(discovered.yesterday_balance_checked_at)

    async def test_unmatched_key_does_not_reuse_historical_group_for_billing(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(access_token="channel-management-token"),
        )
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
        by_id[7].selected_group_id = "default"
        by_id[7].selected_group_name = "Default"
        by_id[7].encrypted_api_key = encrypt_text("sk-unmatched-private-E5F6")
        await self.db.commit()
        result = SimpleNamespace(
            platform_type="newapi",
            status="ok",
            groups=[
                {"id": "default", "name": "Default", "multiplier": 1.0},
                {"id": "vip", "name": "VIP", "multiplier": 2.0},
            ],
            account_group_matches={},
            discovered_upstream_recharge_multiplier=0.1,
            discovered_upstream_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            wallet_balance_usd=10,
            wallet_total_usd=None,
            wallet_used_usd=None,
            balance_unit="USD",
            balance_status="ok",
            balance_message="Balance available.",
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            channel = await self.service.discover_channel(self.db, upstream_id)

        account = next(item for item in channel.accounts if item.management_account_id == 7)
        self.assertEqual((account.selected_group_id, account.selected_group_name), (None, None))
        self.assertIsNone(account.expected_management_billing_multiplier)
        self.assertEqual(account.group_multiplier_status, "group_selection_missing")

    async def test_management_url_is_used_without_changing_inference_url(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        await self.service.update_channel(
            self.db,
            channel.upstream_id,
            UpstreamUpdate(
                management_url="https://management.example/api/v1",
                platform_type="sub2api",
                access_token="management-token",
                confirm_credential_rebind=True,
            ),
        )
        result = self._discovery_result()
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ) as discover:
            discovered = await self.service.discover_channel(self.db, channel.upstream_id)

        self.assertEqual(discover.await_args.kwargs["base_url"], "https://management.example")
        self.assertEqual(discovered.api_endpoint_url, "https://upstream.example")
        self.assertEqual(discovered.management_url, "https://management.example")

    async def test_channel_origin_change_requires_explicit_credential_rebind_confirmation(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]

        with self.assertRaises(ApiAccountServiceError) as context:
            await self.service.update_channel(
                self.db,
                channel.upstream_id,
                UpstreamUpdate(
                    management_url="https://replacement.example/api/v1",
                ),
            )

        self.assertEqual(context.exception.status_code, 409)
        await self.db.rollback()
        stored = await self.db.get(Upstream, channel.upstream_id)
        self.assertIsNotNone(stored)
        self.assertIsNone(stored.management_url)

    async def test_canonical_origin_change_is_checked_independently_from_management_origin(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        configured = await self.service.update_channel(
            self.db,
            channel.upstream_id,
            UpstreamUpdate(
                management_url="https://management.example/api/v1",
                access_token="retained-channel-token",
                confirm_credential_rebind=True,
            ),
        )

        with self.assertRaises(ApiAccountServiceError) as context:
            await self.service.update_channel(
                self.db,
                channel.upstream_id,
                UpstreamUpdate(api_endpoint_url="https://replacement.example/v1"),
            )

        self.assertEqual(context.exception.status_code, 409)
        await self.db.rollback()
        stored = await self.db.get(Upstream, channel.upstream_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.api_endpoint_url, configured.api_endpoint_url)
        self.assertEqual(stored.management_url, "https://management.example")

    async def test_confirmed_canonical_origin_change_clears_account_origin_rebind_flags(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        configs_result = await self.db.execute(
            select(ApiAccount).where(
                ApiAccount.upstream_id == channel.upstream_id
            )
        )
        configs = list(configs_result.scalars().all())
        for config in configs:
            config.api_key_origin_rebind_required = True
        await self.db.commit()

        updated = await self.service.update_channel(
            self.db,
            channel.upstream_id,
            UpstreamUpdate(
                api_endpoint_url="https://replacement.example/v1",
                confirm_credential_rebind=True,
            ),
        )

        self.assertEqual(updated.api_endpoint_url, "https://replacement.example")
        for config in configs:
            await self.db.refresh(config)
            self.assertFalse(config.api_key_origin_rebind_required)

    async def test_same_day_transient_daily_usage_failures_keep_last_good_value(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        stored = await self.db.get(Upstream, channel.upstream_id)
        checked_at = datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)
        now = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)

        for status, reason in (
            ("timeout", "timeout"),
            ("network_error", "network"),
            ("error", "http_429"),
            ("error", "http_503"),
        ):
            with self.subTest(status=status, reason=reason):
                stored.today_upstream_wallet_cost_usd = 3.25
                stored.today_balance_unit = "USD"
                stored.today_balance_status = "ok"
                stored.today_balance_checked_at = checked_at
                result = self._discovery_result(
                    today_upstream_wallet_cost_usd=None,
                    today_balance_status=status,
                    today_balance_error=reason,
                )

                with patch(
                    "app.services.upstream_channels._utcnow",
                    return_value=now,
                ):
                    self.service._apply_discovery_to_channel(
                        stored,
                        result,
                        time_zone="Asia/Shanghai",
                    )

                self.assertEqual(stored.today_balance_status, "stale")
                self.assertEqual(stored.today_upstream_wallet_cost_usd, 3.25)
                self.assertEqual(stored.today_balance_unit, "USD")
                self.assertEqual(stored.today_balance_checked_at, checked_at)

    async def test_same_day_authoritative_daily_usage_failures_clear_cached_value(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        stored = await self.db.get(Upstream, channel.upstream_id)
        checked_at = datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)
        now = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)

        for reason in ("http_401", "invalid_payload", "field_missing"):
            with self.subTest(reason=reason):
                stored.today_upstream_wallet_cost_usd = 3.25
                stored.today_balance_unit = "USD"
                stored.today_balance_status = "ok"
                stored.today_balance_checked_at = checked_at
                result = self._discovery_result(
                    today_upstream_wallet_cost_usd=None,
                    today_balance_status="error",
                    today_balance_error=reason,
                )

                with patch(
                    "app.services.upstream_channels._utcnow",
                    return_value=now,
                ):
                    self.service._apply_discovery_to_channel(
                        stored,
                        result,
                        time_zone="Asia/Shanghai",
                    )

                self.assertEqual(stored.today_balance_status, "error")
                self.assertIsNone(stored.today_upstream_wallet_cost_usd)
                self.assertIsNone(stored.today_balance_unit)
                self.assertIsNone(stored.today_balance_checked_at)

    async def test_discover_all_does_not_count_failed_manual_fallback_as_success(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        await self.service.update_channel(
            self.db,
            channel.upstream_id,
            UpstreamUpdate(upstream_recharge_multiplier_override=2.0),
        )
        stored_channel = await self.db.get(Upstream, channel.upstream_id)
        stored_channel.today_upstream_wallet_cost_usd = 3.25
        stored_channel.today_balance_unit = "USD"
        stored_channel.today_balance_status = "ok"
        stored_channel.today_balance_checked_at = datetime(
            2026, 7, 16, 8, 0, tzinfo=timezone.utc
        )
        stored_channel.yesterday_upstream_wallet_cost_usd = 2.75
        stored_channel.yesterday_balance_unit = "USD"
        stored_channel.yesterday_balance_status = "ok"
        stored_channel.yesterday_balance_checked_at = datetime(
            2026, 7, 16, 8, 0, tzinfo=timezone.utc
        )
        stored_channel.wallet_balance_usd = -3.5
        stored_channel.balance_source = "upstream_wallet"
        stored_channel.balance_checked_at = datetime(
            2026, 7, 16, 8, 0, tzinfo=timezone.utc
        )
        await self.db.commit()

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result(status="error")),
        ):
            result = await self.service.discover_all(self.db)

        self.assertEqual((result.total, result.succeeded, result.failed), (1, 0, 1))
        self.assertEqual(result.upstreams[0].recharge_multiplier_status, "fallback_manual")
        self.assertEqual(result.upstreams[0].last_error, "Upstream channel discovery failed.")
        self.assertEqual(result.upstreams[0].today_balance_status, "error")
        self.assertIsNone(result.upstreams[0].today_upstream_wallet_cost_usd)
        self.assertEqual(result.upstreams[0].yesterday_balance_status, "error")
        self.assertIsNone(result.upstreams[0].yesterday_upstream_wallet_cost_usd)
        self.assertEqual(result.upstreams[0].wallet_balance_usd, -3.5)
        self.assertEqual(result.upstreams[0].balance_source, "upstream_wallet")

    async def test_failed_discovery_preserves_cached_channel_recharge_and_balance(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        stored = await self.db.get(Upstream, channel.upstream_id)
        checked_at = datetime(2026, 7, 28, 0, 43, tzinfo=timezone.utc)
        stored.discovered_upstream_recharge_multiplier = 0.0621
        stored.upstream_recharge_multiplier = 0.0621
        stored.last_known_recharge_multiplier = 0.0621
        stored.recharge_multiplier_source = "payment.config"
        stored.recharge_multiplier_status = "ok"
        stored.wallet_balance_usd = 83.38
        stored.wallet_total_usd = 100.0
        stored.wallet_used_usd = 16.62
        stored.balance_unit = "USD"
        stored.balance_source = "upstream_wallet"
        stored.balance_status = "ok"
        stored.balance_checked_at = checked_at

        succeeded = self.service._apply_discovery_to_channel(
            stored,
            self._discovery_result(status="error"),
        )

        self.assertFalse(succeeded)
        self.assertEqual(stored.discovered_upstream_recharge_multiplier, 0.0621)
        self.assertEqual(stored.upstream_recharge_multiplier, 0.0621)
        self.assertEqual(stored.recharge_multiplier_source, "payment.config")
        self.assertEqual(stored.recharge_multiplier_status, "ok")
        self.assertEqual(stored.wallet_balance_usd, 83.38)
        self.assertEqual(stored.wallet_total_usd, 100.0)
        self.assertEqual(stored.wallet_used_usd, 16.62)
        self.assertEqual(stored.balance_unit, "USD")
        self.assertEqual(stored.balance_source, "upstream_wallet")
        self.assertEqual(stored.balance_checked_at, checked_at)
        self.assertEqual(stored.balance_status, "error")

    async def test_channel_output_recovers_cleared_recharge_from_last_known_value(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        stored = await self.db.get(Upstream, channel.upstream_id)
        stored.upstream_recharge_multiplier = None
        stored.last_known_recharge_multiplier = 1.0
        stored.recharge_multiplier_source = None
        stored.recharge_multiplier_status = "discovery_failed"
        stored.wallet_balance_usd = 83.38
        stored.balance_unit = "USD"

        output = self.service._channel_out(stored, [])

        self.assertEqual(output.upstream_recharge_multiplier, 1.0)
        self.assertEqual(output.recharge_multiplier_source, "cached")
        self.assertEqual(output.recharge_multiplier_status, "stale")
        self.assertEqual(output.actual_balance_cny, 83.38)

    async def test_failed_management_discovery_uses_one_api_key_balance_without_summing(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        self.sub2api.balance_results = {
            7: {"status": "ok", "remaining": 29.58802934, "unit": "USD"},
            8: {"status": "ok", "remaining": 29.58802934, "unit": "USD"},
        }
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=self._discovery_result(status="error")),
        ):
            discovered = await self.service.discover_channel(self.db, channel.upstream_id)

        self.assertEqual(discovered.balance_status, "ok")
        self.assertAlmostEqual(discovered.wallet_balance_usd or 0, 29.58802934)
        self.assertIn("API Key balance", discovered.balance_message or "")
        self.assertTrue(all(account.expected_management_billing_multiplier is None for account in discovered.accounts))

    async def test_full_sync_records_an_external_account_rate_change(self) -> None:
        await self.service.overview(self.db)
        self.sub2api.accounts[0]["rate_multiplier"] = 1.25
        notification_runtime = SimpleNamespace(
            get_notification_config=AsyncMock(
                return_value={"enabled": True, "api_key_rate_changed_enabled": True}
            )
        )

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=self._discovery_result()),
            ),
            patch(
                "app.services.notifications.get_runtime_config_service",
                return_value=notification_runtime,
            ),
        ):
            await self.service.discover_all(self.db, sync_inventory=True)

        events = list(
            (
                await self.db.scalars(
                    select(UpstreamChangeEvent).where(
                        UpstreamChangeEvent.event_type == "account_rate_changed"
                    )
                )
            ).all()
        )
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].old_value, events[0].new_value), (1.0, 1.25))
        self.assertEqual(events[0].details["reason"], "upstream_recharge_change")
        self.assertEqual(events[0].details["transition_reason"], "external_observed")
        notification = await self.db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == "api_key_rate_changed"
            )
        )
        self.assertIsNotNone(notification)
        self.assertEqual(notification.details["reason"], "external_observed")
        self.assertEqual(self.sub2api.rate_update_calls, [])

    async def test_group_multiplier_notification_collects_related_account_rates(self) -> None:
        channel = (await self.service.overview(self.db)).upstreams[0]
        stored_channel = await self.db.get(Upstream, channel.upstream_id)
        observed_at = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)

        await self.service._enqueue_discovery_change_notifications(
            self.db,
            channel=stored_channel,
            group_changes=[
                {
                    "change_type": "multiplier",
                    "group_id": "codex-pro",
                    "group_name": "codex pro",
                    "old_multiplier": 1.0,
                    "new_multiplier": 1.2,
                    "details": {},
                }
            ],
            rate_events=[
                {
                    "account_id": 7,
                    "account_name": "哈基米 pro",
                    "upstream_id": channel.upstream_id,
                    "upstream_name": channel.display_name,
                    "group_id": "codex-pro",
                    "group_name": "codex pro",
                    "old_rate": 0.10,
                    "new_rate": 0.12,
                    "observed_at": observed_at,
                    "reason": "automatic_apply",
                    "change_cause": "upstream_group_change",
                    "status": "applied",
                },
                {
                    "account_id": 8,
                    "account_name": "哈基米 pro+",
                    "upstream_id": channel.upstream_id,
                    "upstream_name": channel.display_name,
                    "group_id": "codex-pro",
                    "group_name": "codex pro",
                    "old_rate": 0.20,
                    "new_rate": 0.24,
                    "observed_at": observed_at,
                    "reason": "external_observed",
                    "change_cause": "upstream_group_change",
                    "status": "observed",
                },
                {
                    "account_id": 9,
                    "account_name": "独立变化账号",
                    "upstream_id": channel.upstream_id,
                    "upstream_name": channel.display_name,
                    "group_id": "other",
                    "group_name": "Other",
                    "old_rate": 0.30,
                    "new_rate": 0.35,
                    "observed_at": observed_at,
                    "reason": "external_observed",
                    "change_cause": "external_observed",
                    "status": "observed",
                },
            ],
            runtime_config=self.runtime_config,
        )
        await self.db.commit()

        notifications = list(
            (await self.db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all()
        )
        self.assertEqual(
            [item.event_type for item in notifications],
            ["upstream_group_multiplier_changed", "api_key_rate_changed"],
        )
        self.assertEqual(
            [item["account_name"] for item in notifications[0].details["affected_accounts"]],
            ["哈基米 pro", "哈基米 pro+"],
        )
        self.assertEqual(notifications[1].details["account_name"], "独立变化账号")

    async def test_enabled_sync_applies_changed_rate_once_and_records_safe_logs(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(access_token="channel-management-token"),
        )
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
        by_id[7].encrypted_api_key = encrypt_text("sk-alpha-auto-sync")
        by_id[8].encrypted_api_key = encrypt_text("sk-beta-auto-sync")
        await self.db.commit()
        remote_rates = {7: 1.0, 8: 0.5}

        async def detached_account_list() -> list[dict]:
            return [
                {**account, "rate_multiplier": remote_rates[int(account["id"])]}
                for account in self.sub2api.accounts
            ]

        async def detached_rate_update(account_id: str | int, rate: float) -> None:
            self.assertFalse(self.db.in_transaction())
            parsed_id = int(account_id)
            self.sub2api.rate_update_calls.append((parsed_id, rate))
            remote_rates[parsed_id] = rate

        async def detached_rate_readback(account_id: str | int) -> float:
            return remote_rates[int(account_id)]

        result = SimpleNamespace(
            platform_type="newapi",
            status="ok",
            groups=[
                {"id": "default", "name": "Default", "multiplier": 1.0},
                {"id": "vip", "name": "VIP", "multiplier": 2.0},
            ],
            account_group_matches={
                7: {"id": "default", "name": "Default", "multiplier": 1.0},
                8: {"id": "vip", "name": "VIP", "multiplier": 2.0},
            },
            discovered_upstream_recharge_multiplier=0.1,
            discovered_upstream_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            wallet_balance_usd=100,
            wallet_total_usd=None,
            wallet_used_usd=None,
            balance_unit="USD",
            balance_status="ok",
            balance_message="Balance available.",
        )
        runtime = SimpleNamespace(
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
            get_notification_config=AsyncMock(
                return_value={
                    "enabled": True,
                    "api_key_rate_changed_enabled": True,
                }
            ),
        )
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=result),
            ),
            patch.object(
                self.sub2api,
                "list_api_key_accounts",
                new=detached_account_list,
            ),
            patch.object(
                self.sub2api,
                "update_account_rate_multiplier",
                new=detached_rate_update,
            ),
            patch.object(
                self.sub2api,
                "get_account_management_billing_multiplier_multiplier",
                new=detached_rate_readback,
            ),
            patch(
                "app.services.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
            patch(
                "app.services.notifications.get_runtime_config_service",
                return_value=SimpleNamespace(
                    get_notification_config=AsyncMock(
                        return_value={
                            "enabled": True,
                            "api_key_rate_changed_enabled": True,
                        }
                    )
                ),
            ),
        ):
            first = await self.service.discover_channel(self.db, upstream_id)
            second = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(self.sub2api.rate_update_calls, [(8, 2.0)])
        self.assertEqual([item.management_billing_multiplier for item in first.accounts], [1.0, 2.0])
        self.assertEqual([item.management_billing_multiplier for item in second.accounts], [1.0, 2.0])
        logs_result = await self.db.execute(
            select(UpstreamRateChangeLog).order_by(UpstreamRateChangeLog.management_account_id)
        )
        logs = list(logs_result.scalars().all())
        self.assertEqual([(item.management_account_id, item.status) for item in logs], [(7, "observed"), (8, "applied")])
        self.assertTrue(all(item.safe_error is None for item in logs))
        self.assertEqual([item.old_upstream_multiplier for item in logs], [None, None])
        self.assertEqual(
            [item.new_upstream_multiplier for item in logs],
            [0.1, 0.2],
        )
        notifications = list(
            (await self.db.scalars(select(NotificationOutbox))).all()
        )
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].details["old_rate"], 0.5)
        self.assertEqual(notifications[0].details["new_rate"], 2.0)
        self.assertEqual(notifications[0].details["reason"], "automatic_apply")

    async def test_pause_is_rechecked_before_each_remote_rate_write(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
        by_id[7].encrypted_api_key = encrypt_text("sk-alpha-paused-sync")
        by_id[8].encrypted_api_key = encrypt_text("sk-beta-paused-sync")
        await self.db.commit()
        result = SimpleNamespace(
            platform_type="newapi",
            status="ok",
            groups=[{"id": "vip", "name": "VIP", "multiplier": 3.0}],
            account_group_matches={
                7: {"id": "vip", "name": "VIP", "multiplier": 3.0},
                8: {"id": "vip", "name": "VIP", "multiplier": 3.0},
            },
            discovered_upstream_recharge_multiplier=1.0,
            discovered_upstream_recharge_multiplier_source="status.price",
            recharge_discovery_status="ok",
            wallet_balance_usd=100,
            wallet_total_usd=None,
            wallet_used_usd=None,
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
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(self.sub2api.rate_update_calls, [])

    async def test_pause_is_rechecked_after_management_billing_multiplier_read(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
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
            await self.service.discover_channel(self.db, upstream_id)

        self.assertGreaterEqual(pause_checks, 2)
        self.assertEqual(self.sub2api.rate_update_calls, [])

    async def test_cancellation_releases_account_rate_lock(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
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
        original_get_rate = self.sub2api.get_account_management_billing_multiplier_multiplier

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
                "get_account_management_billing_multiplier_multiplier",
                new=AsyncMock(side_effect=blocking_get_rate),
            ),
        ):
            task = asyncio.create_task(self.service.discover_channel(self.db, upstream_id))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertIsNotNone(blocked_account_id)
        account_lock = await self.service.accounts._lock_for(blocked_account_id or 0)
        await asyncio.wait_for(account_lock.acquire(), timeout=1)
        account_lock.release()

    async def test_changed_account_inputs_skip_stale_channel_result(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
        by_id[7].upstream_group_multiplier_override = 1.0
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
                    self._account_update(7, upstream_group_multiplier_override=4.0),
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
            discovered = await self.service.discover_channel(self.db, upstream_id)

        account = next(item for item in discovered.accounts if item.management_account_id == 7)
        self.assertEqual(account.upstream_group_multiplier_override, 4.0)
        self.assertIsNone(account.expected_management_billing_multiplier)
        self.assertFalse(any(account_id == 7 for account_id, _rate in self.sub2api.rate_update_calls))

    async def test_concurrent_api_key_save_wins_over_export(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
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
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(discover.await_args.kwargs["account_api_keys"].get(8), newly_saved_key)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(decrypt_text(stored.encrypted_api_key), newly_saved_key)

    async def test_identity_rebind_during_export_drops_stale_exported_key(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs_result = await self.db.execute(select(ApiAccount))
        configs_by_id = {
            config.management_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[7].encrypted_api_key = encrypt_text("sk-current-seven")
        configs_by_id[8].encrypted_api_key = None
        await self.db.commit()
        configs = await self.service._bound_configs(self.db, upstream_id)
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
                upstream_id,
            )

        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(exported.get(7), "sk-current-seven")
        self.assertNotIn(8, exported)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 8
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
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs_result = await self.db.execute(select(ApiAccount))
        configs_by_id = {
            config.management_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[7].encrypted_api_key = encrypt_text("sk-current-seven")
        configs_by_id[8].encrypted_api_key = None
        await self.db.commit()
        configs = await self.service._bound_configs(self.db, upstream_id)
        stale_exported_key = "sk-exported-before-origin-rebind"

        async def export_after_origin_rebind(account_ids: list[int]) -> dict[int, str]:
            self.sub2api.export_calls.append(list(account_ids))
            async with self.session_factory() as concurrent_db:
                stored = (
                    await concurrent_db.execute(
                        select(ApiAccount).where(
                            ApiAccount.management_account_id == 8
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
                upstream_id,
            )

        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(exported.get(7), "sk-current-seven")
        self.assertNotIn(8, exported)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertTrue(stored.api_key_origin_rebind_required)
            self.assertIsNone(stored.encrypted_api_key)

    async def test_remote_endpoint_change_during_export_drops_exported_key(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs_result = await self.db.execute(select(ApiAccount))
        configs_by_id = {
            config.management_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[7].encrypted_api_key = encrypt_text("sk-current-seven")
        configs_by_id[8].encrypted_api_key = None
        await self.db.commit()
        configs = await self.service._bound_configs(self.db, upstream_id)
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
                upstream_id,
            )

        self.assertEqual(self.sub2api.export_calls, [[8]])
        self.assertEqual(exported.get(7), "sk-current-seven")
        self.assertNotIn(8, exported)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertIsNone(stored.encrypted_api_key)
            self.assertFalse(stored.api_key_origin_rebind_required)
            self.assertEqual(stored.api_endpoint_url, "https://upstream.example")

    async def test_remote_endpoint_mismatch_blocks_an_already_stored_key(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs_result = await self.db.execute(select(ApiAccount))
        configs_by_id = {
            config.management_account_id: config
            for config in configs_result.scalars().all()
        }
        configs_by_id[8].encrypted_api_key = encrypt_text("sk-stored-for-old-origin")
        await self.db.commit()
        self.sub2api.accounts[1]["credentials"]["base_url"] = (
            "https://replacement.example/v1"
        )
        configs = await self.service._bound_configs(self.db, upstream_id)

        account_api_keys = await self.service._account_api_keys(
            self.db,
            configs,
            upstream_id,
        )

        self.assertNotIn(8, account_api_keys)
        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(
                decrypt_text(stored.encrypted_api_key),
                "sk-stored-for-old-origin",
            )

    async def test_manual_channel_with_explicit_key_ignores_remote_endpoint_metadata(self) -> None:
        await self.service.overview(self.db)
        manual_channel = Upstream(
            display_name="Manual upstream",
            api_endpoint_url="https://manual.example",
            platform_type="auto",
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
                upstream_id=manual_channel.id,
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
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 8
                )
            )
        ).scalar_one()
        self.assertTrue(stored.upstream_auto_assign_disabled)

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
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        configs = await self.service._bound_configs(self.db, upstream_id)

        await self.service._account_api_keys(self.db, configs, upstream_id)

        self.assertEqual([len(batch) for batch in self.sub2api.export_calls], [200, 1])
        self.assertEqual(
            [account_id for batch in self.sub2api.export_calls for account_id in batch],
            list(range(7, 208)),
        )

    async def test_rebind_is_not_overwritten_by_token_or_url_propagation(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        other_token = "other-channel-token"
        retry_api_key = "sk-saved-during-refresh"
        other_channel = Upstream(
            display_name="Other upstream",
            api_endpoint_url="https://other.example",
            platform_type="sub2api",
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
                        upstream_id=other_channel.id,
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
            await self.service.discover_channel(self.db, upstream_id)

        retry_keys = discover.await_args_list[1].kwargs["account_api_keys"]
        self.assertNotIn(7, retry_keys)
        self.assertEqual(retry_keys.get(8), retry_api_key)

        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(
                api_endpoint_url="https://changed-source.example",
                confirm_credential_rebind=True,
            ),
        )

        async with self.session_factory() as verifier:
            stored = (
                await verifier.execute(
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 7
                    )
                )
            ).scalar_one()
            self.assertEqual(stored.upstream_id, other_channel.id)
            self.assertEqual(stored.api_endpoint_url, other_channel.api_endpoint_url)
            self.assertEqual(decrypt_text(stored.encrypted_access_token), other_token)
            stored_retry_account = (
                await verifier.execute(
                    select(ApiAccount).where(
                        ApiAccount.management_account_id == 8
                    )
                )
            ).scalar_one()
            self.assertEqual(decrypt_text(stored_retry_account.encrypted_api_key), retry_api_key)

    async def test_rate_logs_normalize_group_and_recharge_and_track_each_input_change(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        channel_access_token = "channel-management-token"
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(access_token=channel_access_token),
        )
        configs = await self.db.execute(select(ApiAccount))
        by_id = {item.management_account_id: item for item in configs.scalars().all()}
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
        group_notifications = AsyncMock()

        async def discover(*, group: float, recharge: float) -> None:
            result = SimpleNamespace(
                platform_type="newapi",
                status="ok",
                groups=[{"id": "default", "name": "Default", "multiplier": group}],
                account_group_matches={
                    7: {"id": "default", "name": "Default", "multiplier": group},
                    8: {"id": "default", "name": "Default", "multiplier": group},
                },
                discovered_upstream_recharge_multiplier=recharge,
                discovered_upstream_recharge_multiplier_source="status.price",
                recharge_discovery_status="ok",
                wallet_balance_usd=100,
                wallet_total_usd=None,
                wallet_used_usd=None,
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
                patch(
                    "app.services.upstream_channels.enqueue_upstream_group_changed",
                    new=group_notifications,
                ),
            ):
                await self.service.discover_channel(self.db, upstream_id)

        await discover(group=1.0, recharge=0.1)
        await discover(group=2.0, recharge=0.1)
        await discover(group=2.0, recharge=0.2)
        self.sub2api.local_credit_per_cny = 5.0
        await discover(group=2.0, recharge=0.2)

        result = await self.db.execute(
            select(UpstreamRateChangeLog)
            .where(UpstreamRateChangeLog.management_account_id == 7)
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
        self.assertEqual({item.management_account_id for item in all_logs}, {7, 8})
        logged_names = [(item.management_account_id, item.account_name) for item in all_logs]
        self.assertEqual(logged_names[:2], [(7, "[redacted]"), (8, "[redacted]")])
        self.assertNotIn(api_key, repr(logged_names))
        self.assertNotIn(channel_access_token, repr(logged_names))
        self.assertEqual(
            [item.reason for item in logs],
            [
                "upstream_group_assignment_change",
                "upstream_group_change",
                "upstream_recharge_change",
                "management_recharge_change",
            ],
        )
        self.assertEqual(group_notifications.await_count, 1)
        self.assertEqual(group_notifications.await_args.kwargs["change_type"], "multiplier")
        self.assertEqual(group_notifications.await_args.kwargs["old_multiplier"], 1.0)
        self.assertEqual(group_notifications.await_args.kwargs["new_multiplier"], 2.0)
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
            [(item.old_expected_management_billing_multiplier, item.new_expected_management_billing_multiplier) for item in logs],
            [(None, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 2.0)],
        )

    async def test_auth_me_401_persists_rotated_tokens_before_retrying_once(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        new_access_token = "at-rotated-private"
        new_refresh_token = "rt-rotated-private"
        discovery_calls: list[dict] = []

        async def fake_discovery(**kwargs):
            discovery_calls.append(kwargs)
            if len(discovery_calls) == 1:
                return self._discovery_result(status="error", auth_rejected=True)

            # A separate session can only see the pair after the durability
            # commit that must precede the retried upstream request.
            self.assertFalse(self.db.in_transaction())
            async with self.session_factory() as verifier:
                persisted = await verifier.get(Upstream, upstream_id)
                self.assertIsNotNone(persisted)
                self.assertEqual(decrypt_text(persisted.encrypted_access_token), new_access_token)
                self.assertEqual(decrypt_text(persisted.encrypted_refresh_token), new_refresh_token)
                configs_result = await verifier.execute(
                    select(ApiAccount).where(ApiAccount.upstream_id == upstream_id)
                )
                persisted_configs = list(configs_result.scalars().all())
                self.assertTrue(persisted_configs)
                self.assertTrue(
                    all(
                        decrypt_text(config.encrypted_access_token) == new_access_token
                        for config in persisted_configs
                    )
                )
            return self._discovery_result(wallet_balance_usd=46.226)

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
            channel = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(discover.await_count, 2)
        self.assertEqual(discovery_calls[0]["access_token"], "at-old-private")
        self.assertEqual(discovery_calls[1]["access_token"], new_access_token)
        refresh.assert_awaited_once_with("https://upstream.example", "rt-old-private")
        self.assertEqual(channel.wallet_balance_usd, 46.226)
        self.assertTrue(channel.refresh_token_set)

    async def test_refresh_only_sub2api_channel_is_eligible_for_batch_discovery(self) -> None:
        upstream_id = await self._configure_sub2api_credentials(
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
        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), new_access_token)
        self.assertEqual(decrypt_text(stored.encrypted_refresh_token), new_refresh_token)

    async def test_refresh_failure_preserves_old_credentials(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        before = await self.db.get(Upstream, upstream_id)
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
            await self.service.discover_channel(self.db, upstream_id)

        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertEqual(stored.encrypted_access_token, old_access_ciphertext)
        self.assertEqual(stored.encrypted_refresh_token, old_refresh_ciphertext)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), "at-old-private")
        self.assertEqual(decrypt_text(stored.encrypted_refresh_token), "rt-old-private")
        self.assertEqual(stored.balance_status, "token_invalid")
        self.assertEqual(stored.last_error, "Upstream channel token is invalid.")
        self.assertIsNone(stored.balance_message)
        self.assertEqual(discover.await_count, 1)
        refresh.assert_awaited_once_with("https://upstream.example", "rt-old-private")

        notification = await self.db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == "upstream_token_invalid"
            )
        )
        self.assertIsNone(notification)

    async def test_failed_retry_keeps_rotated_tokens_without_refresh_loop(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
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
            channel = await self.service.discover_channel(self.db, upstream_id)

        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertEqual(decrypt_text(stored.encrypted_access_token), new_access_token)
        self.assertEqual(decrypt_text(stored.encrypted_refresh_token), new_refresh_token)
        self.assertEqual(discover.await_count, 2)
        refresh.assert_awaited_once()
        self.assertEqual(channel.balance_status, "token_invalid")

    async def test_newapi_auth_failure_never_uses_sub2api_refresh_token(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        await self.service.update_channel(
            self.db,
            upstream_id,
            UpstreamUpdate(platform_type="newapi"),
        )
        result = self._discovery_result(status="error", auth_rejected=True)
        result.platform_type = "newapi"

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
            await self.service.discover_channel(self.db, upstream_id)

        refresh.assert_not_awaited()

    async def test_non_auth_me_401_signal_does_not_refresh_sub2api(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()

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
            await self.service.discover_channel(self.db, upstream_id)

        refresh.assert_not_awaited()

    async def test_priority_rebalance_failure_does_not_rollback_channel_discovery(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
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
            discovered = await self.service.discover_channel(self.db, upstream_id)

        stored = await self.db.get(Upstream, upstream_id)
        await self.db.refresh(stored)
        self.assertIsNotNone(stored.last_discovered_at)
        self.assertEqual(discovered.upstream_id, upstream_id)

    async def test_discovery_reuses_record_id_and_blocks_silent_rebind(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        stored = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertIsNotNone(stored)
        stored.encrypted_api_key = encrypt_text("sk-local-seven")
        stored.remote_upstream_api_key_id = 41
        await self.db.commit()
        discovery_result = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_record_id=52,
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                )
            }
        )
        discovery = AsyncMock(return_value=discovery_result)

        with patch("app.services.upstream_channels.discover_upstream", new=discovery):
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(discovery.await_args.kwargs["account_api_key_record_ids"][7], 41)
        await self.db.refresh(stored)
        self.assertEqual(stored.remote_upstream_api_key_id, 41)
        self.assertTrue(stored.upstream_identity_rebind_required)
        self.assertIn("changed from #41 to #52", stored.last_error or "")

    async def test_same_record_id_allows_upstream_group_rename_in_place(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        stored = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        stored.encrypted_api_key = encrypt_text("sk-local-seven")
        stored.remote_upstream_api_key_id = 41
        stored.selected_group_id = "vip"
        stored.selected_group_name = "VIP"
        stored.upstream_group_multiplier_override = 2.5
        await self.db.commit()
        result = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_record_id=41,
                    key_status="active",
                    group_status="available",
                    group_id="vip",
                    group_name="VIP renamed",
                )
            }
        )
        result.groups = [
            {"id": "vip", "name": "VIP renamed", "multiplier": 1.5}
        ]
        result.account_group_matches = {
            7: AccountGroupMatch(
                id="vip",
                name="VIP renamed",
                multiplier=1.5,
            )
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(stored)
        self.assertEqual(stored.remote_upstream_api_key_id, 41)
        self.assertFalse(stored.upstream_identity_rebind_required)
        self.assertEqual(stored.selected_group_id, "vip")
        self.assertEqual(stored.selected_group_name, "VIP renamed")
        self.assertEqual(stored.upstream_group_multiplier_override, 2.5)

    async def test_inconclusive_discovery_preserves_cached_remote_upstream_api_key_id(self) -> None:
        self.sub2api.accounts = self.sub2api.accounts[:1]
        upstream_id = await self._configure_sub2api_credentials()
        stored = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertIsNotNone(stored)
        stored.remote_upstream_api_key_id = 41
        stored.resolved_platform_type = "sub2api"
        stored.selected_group_id = "legacy"
        stored.selected_group_name = "Legacy"
        stored.discovered_upstream_group_multiplier = 1.5
        stored.upstream_group_multiplier = 1.5
        stored.group_multiplier_source = "upstream_key"
        stored.group_multiplier_status = "ok"
        stored.expected_management_billing_multiplier = 0.75
        stored.upstream_wallet_cost_usd = 12.5
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = datetime(
            2026,
            7,
            26,
            8,
            0,
            tzinfo=timezone.utc,
        )
        interval = UpstreamPriorityInterval(
            name="inconclusive-identity",
            start_priority=10,
            end_priority=20,
            step=1,
        )
        self.db.add(interval)
        await self.db.flush()
        stored.priority_interval_id = interval.id
        stored.desired_priority = 12
        stored.priority_sync_status = "synced"
        await self.db.commit()

        discovery = AsyncMock(return_value=self._discovery_result())
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=discovery,
        ):
            await self.service.discover_channel(
                self.db,
                upstream_id,
                options=UpstreamDiscoveryOptions(sync_priorities=False),
            )

        await self.db.refresh(stored)
        self.assertEqual(stored.remote_upstream_api_key_id, 41)
        self.assertFalse(stored.upstream_identity_rebind_required)
        self.assertEqual(
            (stored.selected_group_id, stored.selected_group_name),
            ("legacy", "Legacy"),
        )
        self.assertEqual(stored.discovered_upstream_group_multiplier, 1.5)
        self.assertEqual(stored.upstream_group_multiplier, 1.5)
        self.assertEqual(stored.group_multiplier_source, "upstream_key")
        self.assertEqual(stored.group_multiplier_status, "ok")
        self.assertEqual(stored.expected_management_billing_multiplier, 0.75)
        self.assertEqual(stored.upstream_wallet_cost_usd, 12.5)
        self.assertEqual(stored.upstream_usage_unit, "USD")
        self.assertEqual(stored.priority_interval_id, interval.id)
        self.assertEqual(stored.desired_priority, 12)
        self.assertEqual(stored.priority_sync_status, "synced")

    async def test_incomplete_remote_identity_preserves_priority_and_usage(self) -> None:
        await self.service.overview(self.db)
        stored = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertIsNotNone(stored)
        interval = UpstreamPriorityInterval(
            name="identity-evidence-hold",
            start_priority=10,
            end_priority=20,
            step=1,
        )
        self.db.add(interval)
        await self.db.flush()
        stored.priority_interval_id = interval.id
        stored.desired_priority = 12
        stored.priority_sync_status = "synced"
        stored.upstream_wallet_cost_usd = 7.5
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = datetime(
            2026,
            7,
            26,
            8,
            0,
            tzinfo=timezone.utc,
        )
        await self.db.commit()

        original_created_at = self.sub2api.accounts[0].pop("created_at", None)
        overview = await self.service.overview(self.db)

        isolated = next(
            account
            for channel in overview.upstreams
            for account in channel.accounts
            if account.management_account_id == 7
        )
        self.assertEqual(isolated.identity_binding_status, "mismatch")
        self.assertFalse(isolated.managed)
        await self.db.refresh(stored)
        self.assertEqual(stored.priority_interval_id, interval.id)
        self.assertEqual(stored.desired_priority, 12)
        self.assertEqual(stored.priority_sync_status, "synced")
        self.assertEqual(stored.upstream_wallet_cost_usd, 7.5)
        self.assertEqual(stored.upstream_usage_unit, "USD")
        archive = await self.db.scalar(
            select(ApiAccountDataArchive).where(
                ApiAccountDataArchive.management_account_id == 7
            )
        )
        self.assertIsNone(archive)

        self.sub2api.accounts[0]["created_at"] = original_created_at
        recovered = await self.service.overview(self.db)
        recovered_account = next(
            account
            for channel in recovered.upstreams
            for account in channel.accounts
            if account.management_account_id == 7
        )
        self.assertEqual(recovered_account.identity_binding_status, "bound")
        await self.db.refresh(stored)
        self.assertEqual(stored.priority_interval_id, interval.id)
        self.assertEqual(stored.upstream_wallet_cost_usd, 7.5)

    async def test_quarantined_record_owner_blocks_duplicate_binding(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        configs = {
            config.management_account_id: config
            for config in (
                await self.db.scalars(
                    select(ApiAccount).where(
                        ApiAccount.upstream_id == upstream_id
                    )
                )
            ).all()
        }
        owner = configs[7]
        candidate = configs[8]
        owner.remote_upstream_api_key_id = 88
        candidate.remote_upstream_api_key_id = None
        owner.encrypted_api_key = encrypt_text("sk-local-seven")
        candidate.encrypted_api_key = encrypt_text("sk-local-eight")
        await self.db.commit()

        self.sub2api.accounts[0]["created_at"] = "2026-07-15T12:00:00Z"
        result = self._discovery_result(
            account_upstream_states={
                8: AccountUpstreamState(
                    key_record_id=88,
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                )
            }
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(owner)
        await self.db.refresh(candidate)
        self.assertEqual(owner.remote_upstream_api_key_id, 88)
        self.assertIsNone(candidate.remote_upstream_api_key_id)
        self.assertTrue(candidate.upstream_identity_rebind_required)
        self.assertIn("matches more than one local account", candidate.last_error or "")

    async def test_same_record_id_requires_rebind_after_upstream_platform_change(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        channel = await self.db.get(Upstream, upstream_id)
        stored = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertIsNotNone(channel)
        self.assertIsNotNone(stored)
        channel.platform_type = "auto"
        channel.resolved_platform_type = "newapi"
        stored.platform_type = "auto"
        stored.resolved_platform_type = "newapi"
        stored.remote_upstream_api_key_id = 41
        stored.encrypted_api_key = encrypt_text("sk-local-seven")
        await self.db.commit()
        result = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_record_id=41,
                    key_status="active",
                    group_status="available",
                    group_id="default",
                    group_name="Default",
                )
            }
        )
        result.platform_type = "sub2api"

        discovery = AsyncMock(return_value=result)
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=discovery,
        ):
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(discovery.await_args.kwargs["upstream_type"], "auto")
        await self.db.refresh(stored)
        self.assertEqual(stored.remote_upstream_api_key_id, 41)
        self.assertEqual(stored.resolved_platform_type, "newapi")
        self.assertTrue(stored.upstream_identity_rebind_required)
        self.assertIn("platform changed from newapi to sub2api", stored.last_error or "")

    async def test_same_observed_record_id_never_auto_binds_two_local_accounts(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        configs = list(
            (
                await self.db.scalars(
                    select(ApiAccount).where(
                        ApiAccount.upstream_id == upstream_id
                    )
                )
            ).all()
        )
        for config in configs:
            config.encrypted_api_key = encrypt_text(
                f"sk-local-{config.management_account_id}"
            )
        await self.db.commit()
        duplicate_state = AccountUpstreamState(
            key_record_id=88,
            key_status="active",
            group_status="available",
            group_id="default",
            group_name="Default",
        )
        result = self._discovery_result(
            account_upstream_states={
                config.management_account_id: duplicate_state
                for config in configs
            }
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        for config in configs:
            await self.db.refresh(config)
            self.assertIsNone(config.remote_upstream_api_key_id)
            self.assertTrue(config.upstream_identity_rebind_required)
            self.assertIn("matches more than one local account", config.last_error or "")

    async def test_two_confirmed_disabled_key_probes_pause_once_and_recovery_restores(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
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
            await self.service.discover_channel(self.db, upstream_id)
            first = await self.db.scalar(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 7
                )
            )
            self.assertEqual(first.upstream_health_invalid_count, 1)
            self.assertTrue(self.sub2api.accounts[0]["schedulable"])

            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(first)
        self.assertEqual(first.upstream_key_status, "disabled")
        self.assertEqual(first.upstream_group_status, "available")
        self.assertEqual(first.upstream_health_invalid_count, 2)
        self.assertEqual(first.auto_disabled_reason, "upstream_key_unavailable")
        self.assertIsNotNone(first.last_auto_disabled_at)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        self.assertTrue(first.pause_owned_by_plugin)

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
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(first)
        self.assertEqual(first.upstream_key_status, "active")
        self.assertEqual(first.upstream_health_invalid_count, 0)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False), (7, True)])
        self.assertFalse(first.pause_owned_by_plugin)
        self.assertIsNone(first.auto_disabled_reason)
        logs = list(
            (
                await self.db.execute(
                    select(UpstreamRateChangeLog)
                    .where(UpstreamRateChangeLog.management_account_id == 7)
                    .order_by(UpstreamRateChangeLog.id)
                )
            ).scalars()
        )
        self.assertTrue(any(item.status == "account_disabled" for item in logs))
        self.assertTrue(any(item.status == "account_restored" for item in logs))
        self.assertTrue(
            any(item.reason == "upstream_key_unavailable_recovered" for item in logs)
        )
        disabled_log = next(item for item in logs if item.status == "account_disabled")
        self.assertTrue(disabled_log.old_remote_schedulable)
        self.assertFalse(disabled_log.new_remote_schedulable)

    async def test_inconclusive_probe_preserves_authoritative_state_and_breaks_invalid_sequence(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
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
            await self.service.discover_channel(self.db, upstream_id)
            await self.service.discover_channel(self.db, upstream_id)
            await self.service.discover_channel(self.db, upstream_id)

        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertEqual(config.upstream_key_status, "disabled")
        self.assertEqual(config.upstream_group_status, "available")
        self.assertEqual(config.upstream_health_invalid_count, 1)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [])

    async def test_inconclusive_probe_does_not_reopen_confirmed_group_pause_episode(
        self,
    ) -> None:
        upstream_id = await self._configure_sub2api_credentials()
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
        inconclusive = self._discovery_result(account_upstream_states={})
        state_notification = AsyncMock()

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(
                    side_effect=[invalid, invalid, inconclusive, invalid, invalid]
                ),
            ),
            patch(
                "app.services.upstream_accounts.enqueue_api_key_account_state_changed",
                new=state_notification,
            ),
        ):
            await self.service.discover_channel(self.db, upstream_id)
            await self.service.discover_channel(self.db, upstream_id)
            config = await self.db.scalar(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 7
                )
            )
            await self.db.refresh(config, attribute_names=["pause_holds"])
            self.assertFalse(self.sub2api.accounts[0]["schedulable"])

            await self.service.discover_channel(self.db, upstream_id)
            await self.db.refresh(config, attribute_names=["pause_holds"])
            self.assertEqual(config.upstream_health_invalid_count, 0)
            self.assertTrue(
                any(
                    hold.active and hold.reason == "upstream_group_unavailable"
                    for hold in config.pause_holds
                )
            )

            await self.service.discover_channel(self.db, upstream_id)
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        self.assertEqual(
            [item.kwargs["enabled"] for item in state_notification.await_args_list],
            [False],
        )
        episode_logs = list(
            (
                await self.db.scalars(
                    select(UpstreamRateChangeLog)
                    .where(
                        UpstreamRateChangeLog.management_account_id == 7,
                        UpstreamRateChangeLog.status.in_(
                            ("account_disabled", "account_restored")
                        ),
                    )
                    .order_by(UpstreamRateChangeLog.id)
                )
            ).all()
        )
        self.assertEqual([item.status for item in episode_logs], ["account_disabled"])

    async def test_two_confirmed_unavailable_group_probes_disable_account(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
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
            await self.service.discover_channel(self.db, upstream_id)
            await self.service.discover_channel(self.db, upstream_id)

        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        self.assertEqual(config.upstream_group_status, "unavailable")
        self.assertEqual(config.selected_group_id, "retired")
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])

    async def test_disable_readback_failure_is_logged_without_aborting_channel(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
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
            await self.service.discover_channel(self.db, upstream_id)
            discovered = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(discovered.upstream_id, upstream_id)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        failure = await self.db.scalar(
            select(UpstreamRateChangeLog)
            .where(
                UpstreamRateChangeLog.management_account_id == 7,
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
            select(ApiAccount).where(
                ApiAccount.management_account_id == 8
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
        self.assertEqual([item.management_account_id for item in overview.upstreams[0].accounts], [7])
        await self.db.refresh(config)
        self.assertEqual(config.priority_interval_id, interval.id)

    async def test_discover_all_max_concurrency_controls_parallel_channel_work(self) -> None:
        base = await self.service.overview(self.db)
        first = base.upstreams[0]
        second = first.model_copy(update={"upstream_id": str(uuid4())})
        synthetic = base.model_copy(update={"upstreams": [first, second]})

        async def peak_for(limit: int) -> int:
            active = 0
            peak = 0
            remote_snapshot_ids: list[int] = []
            recharge_snapshots: list[tuple[float | None, str | None, str] | None] = []

            async def discover(
                _db,
                upstream_id: int,
                *,
                sync_inventory: bool = True,
                remote_by_id=None,
                management_recharge_snapshot=None,
            ):
                nonlocal active, peak
                remote_snapshot_ids.append(id(remote_by_id))
                recharge_snapshots.append(management_recharge_snapshot)
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return first if upstream_id == first.upstream_id else second

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
            self.assertEqual(len(set(remote_snapshot_ids)), 1)
            self.assertEqual(
                recharge_snapshots,
                [(0.1, "sub2api_settings", "ok")] * 2,
            )
            return peak

        self.assertEqual(await peak_for(1), 1)
        self.assertEqual(await peak_for(2), 2)
        self.assertEqual(await peak_for(0), 2)
        for invalid in (-1, 51, True):
            with self.assertRaises(ValueError):
                await self.service.discover_all(self.db, max_concurrency=invalid)

    async def test_discover_all_serializes_writeback_after_parallel_probe_failure(self) -> None:
        base = await self.service.overview(self.db)
        first = base.upstreams[0].model_copy(update={"recharge_multiplier_status": "ok"})
        second = first.model_copy(update={"upstream_id": str(uuid4())})
        third = first.model_copy(update={"upstream_id": str(uuid4())})
        synthetic = base.model_copy(update={"upstreams": [first, second, third]})
        channels_by_id = {channel.upstream_id: channel for channel in synthetic.upstreams}
        active_probes = 0
        peak_probes = 0
        active_writes = 0
        peak_writes = 0
        write_order: list[int] = []

        async def discover(_db, upstream_id: int, **_kwargs):
            nonlocal active_probes, peak_probes, active_writes, peak_writes
            active_probes += 1
            peak_probes = max(peak_probes, active_probes)
            await asyncio.sleep(0.02 if upstream_id == second.upstream_id else 0.01)
            active_probes -= 1
            if upstream_id == second.upstream_id:
                raise ApiAccountServiceError("synthetic probe failure")
            coordinator = self.service._active_writeback_coordinator
            self.assertIsNotNone(coordinator)
            await coordinator.wait_for_turn(upstream_id)
            active_writes += 1
            peak_writes = max(peak_writes, active_writes)
            write_order.append(upstream_id)
            await asyncio.sleep(0.02)
            active_writes -= 1
            return channels_by_id[upstream_id]

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
            result = await asyncio.wait_for(
                self.service.discover_all(
                    self.db,
                    sync_inventory=False,
                    max_concurrency=2,
                ),
                timeout=2,
            )

        self.assertEqual((result.total, result.succeeded, result.failed), (3, 2, 1))
        self.assertEqual(peak_probes, 2)
        self.assertEqual(peak_writes, 1)
        self.assertEqual(write_order, [first.upstream_id, third.upstream_id])

    async def test_discover_all_isolates_sqlite_lock_to_failed_channel(self) -> None:
        base = await self.service.overview(self.db)
        first = base.upstreams[0].model_copy(update={"recharge_multiplier_status": "ok"})
        second = first.model_copy(update={"upstream_id": str(uuid4())})
        synthetic = base.model_copy(update={"upstreams": [first, second]})

        async def discover(_db, upstream_id: int, **_kwargs):
            if upstream_id == second.upstream_id:
                raise OperationalError(
                    "UPDATE upstream_channels SET last_discovered_at=?",
                    {},
                    RuntimeError("database is locked"),
                )
            return first

        for max_concurrency in (1, 2):
            with (
                patch.object(self.service, "overview", new=AsyncMock(return_value=synthetic)),
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
                result = await self.service.discover_all(
                    self.db,
                    sync_inventory=False,
                    max_concurrency=max_concurrency,
                )

            self.assertEqual((result.total, result.succeeded, result.failed), (2, 1, 1))

    async def test_transient_partial_group_snapshot_does_not_remove_or_readd_groups(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.group_options = [
            {"id": "17", "name": "codex plus", "multiplier": 1.0},
            {"id": "47", "name": "Claude-Kiro-高缓", "multiplier": 2.0},
        ]
        channel.last_discovered_at = datetime.now(timezone.utc)
        configs = list((await self.db.scalars(select(ApiAccount))).all())
        configs[0].selected_group_id = "47"
        configs[0].selected_group_name = "Claude-Kiro-高缓"
        configs[0].upstream_group_multiplier = 2.0
        configs[0].group_multiplier_status = "ok"
        await self.db.commit()

        partial = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="active",
                    group_status="deleted",
                    group_id="47",
                    group_name="Claude-Kiro-高缓",
                ),
                8: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="17",
                    group_name="codex plus",
                ),
            }
        )
        partial.groups = [{"id": "17", "name": "codex plus", "multiplier": 1.0}]
        partial.account_group_matches = {
            8: {"id": "17", "name": "codex plus", "multiplier": 1.0}
        }
        recovered = self._discovery_result(
            account_upstream_states={
                7: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="47",
                    group_name="Claude-Kiro-高缓",
                ),
                8: AccountUpstreamState(
                    key_status="active",
                    group_status="available",
                    group_id="17",
                    group_name="codex plus",
                ),
            }
        )
        recovered.groups = [
            {"id": "17", "name": "codex plus", "multiplier": 1.0},
            {"id": "47", "name": "Claude-Kiro-高缓", "multiplier": 2.0},
        ]
        recovered.account_group_matches = {
            7: {"id": "47", "name": "Claude-Kiro-高缓", "multiplier": 2.0},
            8: {"id": "17", "name": "codex plus", "multiplier": 1.0},
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(side_effect=[partial, recovered]),
        ):
            await self.service.discover_channel(self.db, upstream_id)
            config = await self.db.scalar(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 7
                )
            )
            self.assertEqual(config.selected_group_id, "47")
            self.assertEqual(config.upstream_group_status, "available")
            await self.service.discover_channel(self.db, upstream_id)

        events = list((await self.db.scalars(select(UpstreamChangeEvent))).all())
        self.assertFalse(
            any(event.event_type in {"group_removed", "group_added"} for event in events)
        )
        await self.db.refresh(channel)
        self.assertEqual(channel.pending_group_removal_count, 0)
        self.assertIsNone(channel.pending_group_options)
        self.assertEqual({group["id"] for group in channel.group_options}, {"17", "47"})

    async def test_group_removal_requires_two_matching_snapshots(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.group_options = [
            {"id": "17", "name": "codex plus", "multiplier": 1.0},
            {"id": "47", "name": "Claude-Kiro-高缓", "multiplier": 2.0},
        ]
        channel.last_discovered_at = datetime.now(timezone.utc)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.selected_group_id = "47"
        config.selected_group_name = "Claude-Kiro-高缓"
        config.upstream_group_status = "available"
        await self.db.commit()
        partials = [
            self._discovery_result(),
            self._discovery_result(),
            self._discovery_result(
                account_upstream_states={
                    7: AccountUpstreamState(
                        key_status="active",
                        group_status="unavailable",
                        group_id="47",
                        group_name=config.selected_group_name,
                    )
                }
            ),
        ]
        for partial in partials:
            partial.groups = [{"id": "17", "name": "codex plus", "multiplier": 1.0}]
            partial.account_group_matches = {}
        partials[2].groups.append(
            {
                "id": "99",
                "name": config.selected_group_name,
                "multiplier": 3.0,
            }
        )

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(side_effect=partials),
        ):
            await self.service.discover_channel(self.db, upstream_id)
            first_events = list(
                (await self.db.scalars(select(UpstreamChangeEvent))).all()
            )
            self.assertFalse(any(event.event_type == "group_removed" for event in first_events))
            await self.db.refresh(config)
            self.assertEqual(config.selected_group_id, "47")
            self.assertEqual(config.upstream_group_status, "available")
            await self.service.discover_channel(self.db, upstream_id)
            await self.db.refresh(config)
            self.assertEqual(config.upstream_group_status, "deleted")
            self.assertEqual(config.group_multiplier_status, "group_deleted")
            await self.service.discover_channel(self.db, upstream_id)

        events = list((await self.db.scalars(select(UpstreamChangeEvent))).all())
        removed = [event for event in events if event.event_type == "group_removed"]
        self.assertEqual([(event.group_id, event.group_name) for event in removed], [
            ("47", "Claude-Kiro-高缓")
        ])
        await self.db.refresh(config)
        self.assertEqual(config.selected_group_id, "47")
        self.assertEqual(config.selected_group_name, "Claude-Kiro-高缓")
        self.assertEqual(config.upstream_group_status, "deleted")
        self.assertEqual(config.group_multiplier_status, "group_deleted")
        group_status_events = [
            event
            for event in events
            if event.event_type == "upstream_group_status_changed"
            and (event.details or {}).get("account_id") == 7
        ]
        self.assertEqual(group_status_events, [])
        deletion_events = [
            event
            for event in events
            if event.group_id == "47"
            and event.new_status in {"deleted", "removed"}
        ]
        self.assertEqual(
            [(event.event_type, event.old_status, event.new_status) for event in deletion_events],
            [("group_removed", "available", "removed")],
        )

    async def test_two_empty_group_snapshots_can_confirm_complete_removal(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.group_options = [{"id": "47", "name": "legacy", "multiplier": 2.0}]

        first = SimpleNamespace(status="ok", groups=[])
        self.service._stabilize_discovery_groups(
            channel,
            first,
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(first.groups, channel.group_options)
        self.assertEqual(channel.pending_group_removal_count, 1)

        second = SimpleNamespace(status="ok", groups=[])
        self.service._stabilize_discovery_groups(
            channel,
            second,
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(second.groups, [])
        self.assertEqual(channel.pending_group_removal_count, 0)

    async def test_group_snapshot_does_not_duplicate_mixed_account_id_keys(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.group_options = [
            {"id": "17", "name": "current", "multiplier": 1.0},
            {"id": "47", "name": "legacy", "multiplier": 2.0},
        ]
        result = SimpleNamespace(
            status="ok",
            groups=[{"id": "17", "name": "current", "multiplier": 1.0}],
            account_upstream_states={
                7: AccountUpstreamState(
                    group_status="deleted",
                    group_id="47",
                    group_name="legacy",
                )
            },
            account_group_matches={
                "7": {"id": "17", "name": "current", "multiplier": 1.0}
            },
        )

        self.service._stabilize_discovery_groups(
            channel,
            result,
            now=datetime.now(timezone.utc),
        )

        self.assertNotIn(7, result.account_group_matches)
        self.assertEqual(result.account_group_matches["7"]["id"], "17")

    async def test_group_snapshot_prefers_explicit_id_over_ambiguous_name(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.group_options = [
            {"id": "17", "name": "shared", "multiplier": 1.0},
            {"id": "47", "name": "shared", "multiplier": 2.0},
        ]
        result = SimpleNamespace(
            status="ok",
            groups=[{"id": "17", "name": "shared", "multiplier": 1.0}],
            account_upstream_states={
                7: AccountUpstreamState(
                    group_status="available",
                    group_id="17",
                    group_name="shared",
                ),
                8: AccountUpstreamState(
                    group_status="available",
                    group_name="shared",
                ),
            },
            account_group_matches={},
        )

        self.service._stabilize_discovery_groups(
            channel,
            result,
            now=datetime.now(timezone.utc),
        )

        self.assertNotIn(7, result.account_group_matches)
        self.assertNotIn("7", result.account_group_matches)
        self.assertNotIn(8, result.account_group_matches)
        self.assertNotIn("8", result.account_group_matches)

    async def test_same_group_id_name_change_is_not_remove_and_add(self) -> None:
        upstream_id = await self._configure_sub2api_credentials()
        channel = await self.db.get(Upstream, upstream_id)
        self.assertIsNotNone(channel)
        channel.group_options = [{"id": "47", "name": "旧名称", "multiplier": 2.0}]
        channel.last_discovered_at = datetime.now(timezone.utc)
        await self.db.commit()
        renamed = self._discovery_result()
        renamed.groups = [{"id": "47", "name": "新名称", "multiplier": 2.0}]
        renamed.account_group_matches = {}

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=renamed),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        events = list((await self.db.scalars(select(UpstreamChangeEvent))).all())
        group_events = [event for event in events if event.event_type.startswith("group_")]
        self.assertEqual([event.event_type for event in group_events], ["group_name_changed"])
        self.assertEqual(group_events[0].group_id, "47")
        self.assertEqual(group_events[0].old_value, 2.0)
        self.assertEqual(group_events[0].new_value, 2.0)
        self.assertEqual(group_events[0].details["old_name"], "旧名称")
        self.assertEqual(group_events[0].details["new_name"], "新名称")

    async def test_discover_all_skips_channel_already_under_manual_discovery(self) -> None:
        base = await self.service.overview(self.db)
        first = base.upstreams[0]
        second = first.model_copy(update={"upstream_id": str(uuid4())})
        synthetic = base.model_copy(update={"upstreams": [first, second]})
        discover = AsyncMock(return_value=second)

        with (
            patch.object(self.service, "overview", new=AsyncMock(return_value=synthetic)),
            patch.object(self.service, "_discover_channel", new=discover),
            patch.object(
                self.service,
                "_rebalance_priorities_best_effort",
                new=AsyncMock(),
            ),
        ):
            result = await self.service.discover_all(
                self.db,
                sync_inventory=False,
                max_concurrency=1,
                skip_upstream_ids={first.upstream_id},
            )

        self.assertEqual(result.skipped, 1)
        discover.assert_awaited_once()
        self.assertEqual(discover.await_args.args[1], second.upstream_id)

    async def test_discover_all_skips_channels_with_automatic_probe_disabled(self) -> None:
        overview = await self.service.overview(self.db)
        channel = await self.db.get(Upstream, overview.upstreams[0].upstream_id)
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
        self.assertFalse(result.upstreams[0].probe_enabled)
        discover.assert_not_awaited()
        rebalance.assert_awaited_once_with(
            self.db,
            account_ids=set(),
            remote_by_id=ANY,
        )

    async def test_manual_discover_all_reuses_complete_fresh_channel_cache(self) -> None:
        overview = await self.service.overview(self.db)
        observed_at = datetime.now(timezone.utc)
        channel = overview.upstreams[0].model_copy(
            update={
                "last_discovered_at": observed_at,
                "accounts": [
                    account.model_copy(update={"last_discovered_at": observed_at})
                    for account in overview.upstreams[0].accounts
                ],
            }
        )
        synthetic = overview.model_copy(update={"upstreams": [channel]})

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
            account_ids={account.management_account_id for account in channel.accounts},
            remote_by_id=ANY,
        )

    async def test_manual_discover_all_reuses_complete_cache_without_ttl(self) -> None:
        overview = await self.service.overview(self.db)
        observed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        channel = overview.upstreams[0].model_copy(
            update={
                "last_discovered_at": observed_at,
                "accounts": [
                    account.model_copy(update={"last_discovered_at": observed_at})
                    for account in overview.upstreams[0].accounts
                ],
            }
        )
        synthetic = overview.model_copy(update={"upstreams": [channel]})

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
        discover_mock = AsyncMock(return_value=overview.upstreams[0])
        recharge_mock = AsyncMock(return_value=(0.1, "sub2api_settings", "ok"))
        with (
            patch.object(self.service, "overview", new=overview_mock),
            patch.object(self.service, "_discover_channel", new=discover_mock),
            patch.object(self.service, "_management_recharge", new=recharge_mock),
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
        self.assertEqual(discover_mock.await_args.args, (self.db, overview.upstreams[0].upstream_id))
        self.assertFalse(discover_mock.await_args.kwargs["sync_inventory"])
        self.assertIn(7, discover_mock.await_args.kwargs["remote_by_id"])
        self.assertEqual(
            discover_mock.await_args.kwargs["management_recharge_snapshot"],
            (0.1, "sub2api_settings", "ok"),
        )
        recharge_mock.assert_awaited_once_with()

    async def test_overlapping_discover_all_batches_share_one_service_lock(self) -> None:
        synthetic = await self.service.overview(self.db)
        active = 0
        peak = 0
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls = 0

        async def discover(
            _db,
            _upstream_id: int,
            *,
            sync_inventory: bool = True,
            remote_by_id=None,
            management_recharge_snapshot=None,
        ):
            nonlocal active, peak, calls
            calls += 1
            active += 1
            peak = max(peak, active)
            if calls == 1:
                first_started.set()
                await release_first.wait()
            active -= 1
            return synthetic.upstreams[0]

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
            await asyncio.wait_for(first_started.wait(), timeout=2)
            second = asyncio.create_task(
                self.service.discover_all(self.db, max_concurrency=1)
            )
            await asyncio.sleep(0.02)
            self.assertEqual(active, 1)
            release_first.set()
            await asyncio.gather(first, second)

        self.assertEqual(peak, 1)


    async def test_negative_balance_guard_restores_only_accounts_it_paused(self) -> None:
        overview = await self.service.overview(self.db)
        upstream_id = overview.upstreams[0].upstream_id
        self.sub2api.accounts[1]["schedulable"] = False
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled = AsyncMock(
            return_value=True
        )
        self.runtime_config.get_upstream_negative_balance_basis = AsyncMock(
            return_value="wallet"
        )
        healthy_states = {
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

        negative = self._discovery_result(
            wallet_balance_usd=-1.25,
            account_upstream_states=healthy_states,
        )
        negative.account_group_matches = {
            account_id: {"id": "default", "name": "Default", "multiplier": 1.0}
            for account_id in healthy_states
        }
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=negative),
        ):
            paused = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        self.assertEqual(paused.balance_guard_state, "insufficient")
        self.assertEqual(paused.balance_guard_paused_count, 1)
        configs = list((await self.db.scalars(select(ApiAccount))).all())
        eligible = {
            item.management_account_id: item.balance_guard_restore_eligible
            for item in configs
        }
        self.assertEqual(eligible, {7: True, 8: False})
        balance_notifications = list(
            (
                await self.db.scalars(
                    select(NotificationOutbox).where(
                        NotificationOutbox.event_type == "upstream_balance_low"
                    )
                )
            ).all()
        )
        self.assertEqual(len(balance_notifications), 1)
        self.assertEqual(balance_notifications[0].details["balance"], -1.25)
        self.assertEqual(balance_notifications[0].details["threshold"], 0.0)
        self.assertEqual(balance_notifications[0].details["basis"], "wallet")

        positive = self._discovery_result(
            wallet_balance_usd=2.0,
            account_upstream_states=healthy_states,
        )
        positive.account_group_matches = negative.account_group_matches
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=positive),
        ):
            restored = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(self.sub2api.schedulable_calls, [(7, False), (7, True)])
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertFalse(self.sub2api.accounts[1]["schedulable"])
        self.assertEqual(restored.balance_guard_state, "healthy")
        self.assertEqual(restored.balance_guard_paused_count, 0)
        async with self.session_factory() as verifier:
            notification = await verifier.scalar(select(NotificationOutbox))
            channel = await verifier.get(Upstream, upstream_id)
            self.assertEqual(notification.status, "canceled")
            self.assertIsNone(channel.balance_guard_episode_id)
        self.assertTrue(self.sub2api.get_account_calls)

    async def test_balance_guard_uses_same_configured_threshold_to_pause_and_restore(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled.return_value = True
        self.runtime_config.get_upstream_negative_balance_basis.return_value = "wallet"
        self.runtime_config.get_upstream_balance_pause_threshold.return_value = 5.0
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }

        def balance_result(balance: float) -> SimpleNamespace:
            discovered = self._discovery_result(
                wallet_balance_usd=balance,
                account_upstream_states=healthy_states,
            )
            discovered.account_group_matches = {
                account_id: {
                    "id": "default",
                    "name": "Default",
                    "multiplier": 1.0,
                }
                for account_id in healthy_states
            }
            return discovered

        state_notification = AsyncMock()
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=balance_result(4.99)),
            ),
            patch(
                "app.services.upstream_accounts.enqueue_api_key_account_state_changed",
                new=state_notification,
            ),
        ):
            paused = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(paused.balance_guard_state, "insufficient")
        self.assertEqual(paused.balance_guard_value, 4.99)
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False), (8, False)])
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        hold = next(
            item
            for item in config.pause_holds
            if item.reason == "upstream_balance_negative" and item.active
        )
        self.assertEqual(hold.recovery_mode, "balance_at_or_above_threshold")
        self.assertEqual(hold.evidence_json["threshold"], 5.0)
        notification = await self.db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == "upstream_balance_low"
            )
        )
        self.assertEqual(notification.details["balance"], 4.99)
        self.assertEqual(notification.details["threshold"], 5.0)
        self.assertTrue(state_notification.await_args_list)
        pause_notification = next(
            item for item in state_notification.await_args_list
            if item.kwargs["enabled"] is False
        )
        self.assertEqual(pause_notification.kwargs["reason"], "upstream_balance_negative")
        self.assertEqual(pause_notification.kwargs["reason_details"]["balance"], 4.99)
        self.assertEqual(pause_notification.kwargs["reason_details"]["threshold"], 5.0)

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=balance_result(5.0)),
        ):
            restored = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(restored.balance_guard_state, "healthy")
        self.assertEqual(restored.balance_guard_value, 5.0)
        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (8, False), (7, True), (8, True)],
        )
        scheduling_events = list(
            (
                await self.db.scalars(
                    select(AccountSchedulingChangeLog).order_by(
                        AccountSchedulingChangeLog.id
                    )
                )
            ).all()
        )
        self.assertEqual(
            [event.event_type for event in scheduling_events],
            ["paused", "paused", "restored", "restored"],
        )
        self.assertTrue(
            all(
                event.reason == "upstream_balance_negative"
                for event in scheduling_events
            )
        )

    async def test_actual_balance_cny_uses_threshold_and_cny_unit(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        self.sub2api.accounts[1]["schedulable"] = False
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled.return_value = True
        self.runtime_config.get_upstream_negative_balance_basis.return_value = (
            "recharge_adjusted"
        )
        self.runtime_config.get_upstream_balance_pause_threshold.return_value = 3.0
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }

        def adjusted_result(recharge_multiplier: float) -> SimpleNamespace:
            discovered = self._discovery_result(
                wallet_balance_usd=4.0,
                account_upstream_states=healthy_states,
            )
            discovered.discovered_upstream_recharge_multiplier = recharge_multiplier
            discovered.account_group_matches = {
                account_id: {
                    "id": "default",
                    "name": "Default",
                    "multiplier": 1.0,
                }
                for account_id in healthy_states
            }
            return discovered

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=adjusted_result(0.5)),
        ):
            paused = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(paused.balance_guard_value, 2.0)
        self.assertEqual(paused.balance_guard_state, "insufficient")
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        hold = next(
            item
            for item in config.pause_holds
            if item.reason == "upstream_balance_negative" and item.active
        )
        self.assertEqual(hold.evidence_json["basis"], "recharge_adjusted")
        self.assertEqual(hold.evidence_json["balance"], 2.0)
        self.assertEqual(hold.evidence_json["threshold"], 3.0)
        self.assertEqual(hold.evidence_json["unit"], "CNY")

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=adjusted_result(0.75)),
        ):
            restored = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(restored.balance_guard_value, 3.0)
        self.assertEqual(restored.balance_guard_state, "healthy")
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False), (7, True)])

    async def test_negative_balance_episode_survives_probe_failure_without_duplicate(self) -> None:
        overview = await self.service.overview(self.db)
        upstream_id = overview.upstreams[0].upstream_id
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled = AsyncMock(
            return_value=True
        )
        self.runtime_config.get_upstream_negative_balance_basis = AsyncMock(
            return_value="wallet"
        )
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }
        negative = self._discovery_result(
            wallet_balance_usd=-1.0,
            account_upstream_states=healthy_states,
        )
        negative.account_group_matches = {
            account_id: {"id": "default", "name": "Default", "multiplier": 1.0}
            for account_id in healthy_states
        }

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=negative),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        channel = await self.db.get(Upstream, upstream_id)
        episode_id = channel.balance_guard_episode_id
        self.assertTrue(episode_id)

        failed = self._discovery_result(status="error", wallet_balance_usd=None)
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=failed),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        await self.db.refresh(channel)
        self.assertEqual(channel.balance_guard_state, "unavailable")
        self.assertEqual(channel.balance_guard_episode_id, episode_id)

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=negative),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        notifications = list(
            (
                await self.db.scalars(
                    select(NotificationOutbox).where(
                        NotificationOutbox.event_type == "upstream_balance_low"
                    )
                )
            ).all()
        )
        self.assertEqual(len(notifications), 1)
        self.assertIn(episode_id, notifications[0].dedupe_key)

    async def test_disabling_balance_policy_closes_existing_episode(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled.return_value = True
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }
        negative = self._discovery_result(
            wallet_balance_usd=-1.0,
            account_upstream_states=healthy_states,
        )
        negative.account_group_matches = {
            account_id: {"id": "default", "name": "Default", "multiplier": 1.0}
            for account_id in healthy_states
        }
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=negative),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        channel = await self.db.get(Upstream, upstream_id)
        episode_id = channel.balance_guard_episode_id
        self.assertTrue(episode_id)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])

        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled.return_value = False
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=negative),
        ):
            result = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(result.balance_guard_state, "disabled")
        self.assertIsNone(result.balance_guard_value)
        self.assertEqual(result.balance_guard_paused_count, 0)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        await self.db.refresh(channel)
        self.assertIsNone(channel.balance_guard_episode_id)
        notification = await self.db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == "upstream_balance_low"
            )
        )
        self.assertEqual(notification.status, "canceled")

    async def test_low_balance_notification_switch_does_not_accumulate(self) -> None:
        overview = await self.service.overview(self.db)
        upstream_id = overview.upstreams[0].upstream_id
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled = AsyncMock(
            return_value=True
        )
        self.runtime_config.get_upstream_negative_balance_basis = AsyncMock(
            return_value="wallet"
        )
        self.runtime_config.get_notification_config.return_value = {
            "enabled": True,
            "oauth_account_disabled_enabled": True,
            "api_key_rate_changed_enabled": True,
            "upstream_balance_low_enabled": False,
        }
        negative = self._discovery_result(wallet_balance_usd=-1.0)
        negative.account_group_matches = {}
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=negative),
        ):
            result = await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(result.balance_guard_state, "insufficient")
        balance_notifications = list(
            (
                await self.db.scalars(
                    select(NotificationOutbox).where(
                        NotificationOutbox.event_type == "upstream_balance_low"
                    )
                )
            ).all()
        )
        self.assertEqual(len(balance_notifications), 0)

    async def test_balance_guard_pause_intent_survives_crash_after_remote_pause(self) -> None:
        overview = await self.service.overview(self.db)
        upstream_id = overview.upstreams[0].upstream_id
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled = AsyncMock(
            return_value=True
        )
        self.runtime_config.get_upstream_negative_balance_basis = AsyncMock(
            return_value="wallet"
        )
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }
        negative = self._discovery_result(
            wallet_balance_usd=-1.0,
            account_upstream_states=healthy_states,
        )
        negative.account_group_matches = {
            account_id: {"id": "default", "name": "Default", "multiplier": 1.0}
            for account_id in healthy_states
        }
        original_get = self.sub2api.get_account_by_id

        async def crash_after_pause(account_id, **kwargs):
            if self.sub2api.schedulable_calls:
                raise asyncio.CancelledError()
            return await original_get(account_id, **kwargs)

        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=negative),
            ),
            patch.object(self.sub2api, "get_account_by_id", new=crash_after_pause),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.service._discover_channel(self.db, upstream_id)
        await self.db.rollback()
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])

        async with self.session_factory() as verifier:
            config = await verifier.scalar(
                select(ApiAccount).where(
                    ApiAccount.management_account_id == 7
                )
            )
            self.assertTrue(config.balance_guard_restore_eligible)
            self.assertEqual(config.balance_guard_operation, "pause_pending")
            self.assertEqual(config.balance_guard_upstream_id, upstream_id)

        positive = self._discovery_result(
            wallet_balance_usd=2.0,
            account_upstream_states=healthy_states,
        )
        positive.account_group_matches = negative.account_group_matches
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=positive),
        ):
            restored = await self.service.discover_channel(self.db, upstream_id)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(restored.balance_guard_paused_count, 0)

    async def test_independent_account_availability_model_only_pauses_that_account(
        self,
    ) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "independent_model"
        config.availability_test_model = "account-test-model"
        await self.db.commit()
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }
        discovered = self._discovery_result(
            account_upstream_states=healthy_states,
            upstream_monitors=[
                {
                    "id": 91,
                    "name": "Primary",
                    "primary_status": "available",
                    "timeline": [],
                }
            ],
        )
        discovered.account_group_matches = {
            account_id: {"id": "default", "name": "Default", "multiplier": 1.0}
            for account_id in healthy_states
        }
        self.sub2api.connection_test_results = [
            (False, "unavailable"),
            (True, None),
        ]

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=discovered),
        ):
            await self.service.discover_channel(self.db, upstream_id)
            self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model")] * 2,
        )
        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (7, True)],
        )
        await self.db.refresh(config)
        self.assertEqual(config.availability_status, "available")
        self.assertFalse(
            any(
                hold.active and hold.reason == "upstream_monitor_unavailable"
                for hold in config.pause_holds
            )
        )

    async def test_account_can_follow_one_selected_upstream_monitor(self) -> None:
        await self.service.overview(self.db)
        channel = await self.db.scalar(select(Upstream))
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = 92
        config.availability_test_model = "account-test-model"
        channel.upstream_monitor_status = "ok"
        channel.upstream_monitors = [
            {"id": 91, "name": "Healthy", "primary_status": "available"},
            {"id": 92, "name": "Broken", "primary_status": "unavailable"},
        ]
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_unavailable_consecutive_threshold.return_value = 1
        self.runtime_config.get_upstream_monitor_recovery_consecutive_threshold.return_value = 1
        self.sub2api.connection_test_results = [
            (False, "still unavailable"),
            (True, None),
        ]

        first_action, first_evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )
        second_action, second_evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(first_action, "hold")
        self.assertEqual(second_action, "clear")
        self.assertEqual(first_evidence["monitor_id"], 92)
        self.assertEqual(second_evidence["monitor_name"], "Broken")
        self.assertEqual(config.availability_status, "available")
        self.assertEqual(config.availability_source, "upstream_monitor_fallback")
        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model"), (7, "account-test-model")],
        )

    async def test_account_without_selected_monitor_is_not_configured(self) -> None:
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="upstream_monitor",
            availability_monitor_id=None,
        )
        channel = Upstream(
            display_name="Overall monitor",
            api_endpoint_url="https://overall-monitor.example",
            upstream_monitor_status="ok",
            upstream_monitor_count=1,
            upstream_monitors=[
                {"id": 91, "name": "Healthy", "primary_status": "available"}
            ],
            upstream_monitor_checked_at=datetime.now(timezone.utc),
        )

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertIsNone(action)
        self.assertEqual(config.availability_status, "not_configured")
        self.assertEqual(config.availability_source, "upstream_monitor")
        self.assertIsNone(evidence["monitor_id"])
        self.assertEqual(evidence["status"], "not_configured")
        self.assertIn("concrete", config.availability_message.lower())
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_account_availability_guard_reads_canonical_runtime_setting_name(self) -> None:
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="disabled",
            availability_status="unavailable",
            availability_unavailable_count=1,
        )
        channel = Upstream(
            display_name="Canonical setting",
            api_endpoint_url="https://canonical-setting.example",
        )
        runtime_config = SimpleNamespace(
            get_api_account_auto_pause_on_upstream_monitor_unavailable_enabled=AsyncMock(
                return_value=True
            ),
            get_upstream_monitor_fallback_test_models=AsyncMock(return_value=[]),
            get_upstream_monitor_fallback_test_attempts=AsyncMock(return_value=1),
            get_upstream_monitor_recovery_test_attempts=AsyncMock(return_value=1),
            get_upstream_monitor_test_attempt_interval_seconds=AsyncMock(return_value=0),
            get_api_key_availability_all_tests_must_succeed=AsyncMock(return_value=False),
        )

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(evidence, {"mode": "disabled", "status": "disabled"})
        runtime_config.get_api_account_auto_pause_on_upstream_monitor_unavailable_enabled.assert_awaited_once_with()

    async def test_account_without_selected_monitor_can_use_ordered_fallback_chain(self) -> None:
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_without_monitor_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_models.return_value = [
            "not-allowed",
            "account-test-model",
            "later-model",
        ]
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="upstream_monitor",
            availability_monitor_id=None,
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"},
                {"id": "later-model", "display_name": "Later"},
            ],
        )
        channel = Upstream(
            display_name="No selected monitor",
            api_endpoint_url="https://no-selected-monitor.example",
            upstream_monitor_status="ok",
            upstream_monitors=[],
            upstream_monitor_checked_at=datetime.now(timezone.utc),
        )
        self.sub2api.connection_test_results = [(True, None)]

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(evidence["model"], "account-test-model")
        self.assertTrue(evidence["fallback_without_monitor_enabled"])
        self.assertEqual(config.availability_source, "upstream_monitor_fallback")
        self.assertIn("No concrete upstream monitor panel", config.availability_message)
        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model")],
        )

    async def test_disabled_account_availability_monitor_clears_without_testing(self) -> None:
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="disabled",
            availability_status="unavailable",
            availability_unavailable_count=2,
        )
        channel = Upstream(
            display_name="Disabled account monitor",
            api_endpoint_url="https://disabled-monitor.example",
        )

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(evidence, {"mode": "disabled", "status": "disabled"})
        self.assertEqual(config.availability_status, "disabled")
        self.assertEqual(config.availability_unavailable_count, 0)
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_available_selected_monitor_clears_without_connection_test(self) -> None:
        await self.service.overview(self.db)
        channel = await self.db.scalar(select(Upstream))
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = 91
        config.availability_test_model = "account-test-model"
        channel.upstream_monitor_status = "ok"
        channel.upstream_monitor_checked_at = datetime.now(timezone.utc)
        channel.upstream_monitors = [
            {"id": 91, "name": "Healthy", "primary_status": "available"}
        ]
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_recovery_consecutive_threshold.return_value = 1

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(evidence["monitor_status"], "available")
        self.assertEqual(config.availability_status, "available")
        self.assertEqual(config.availability_source, "upstream_monitor")
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_degraded_selected_monitor_is_available_without_connection_test(self) -> None:
        await self.service.overview(self.db)
        channel = await self.db.scalar(select(Upstream))
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = 91
        config.availability_test_model = "account-test-model"
        config.availability_status = "unavailable"
        config.availability_source = "upstream_monitor_fallback"
        config.availability_unavailable_count = 2
        channel.upstream_monitor_status = "degraded"
        channel.upstream_monitor_checked_at = datetime.now(timezone.utc)
        channel.upstream_monitors = [
            {"id": 91, "name": "Degraded", "primary_status": "degraded"}
        ]
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_recovery_consecutive_threshold.return_value = 1

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(evidence["monitor_status"], "degraded")
        self.assertNotIn("fallback_reason", evidence)
        self.assertNotIn("test_status", evidence)
        self.assertEqual(config.availability_status, "available")
        self.assertEqual(config.availability_source, "upstream_monitor")
        self.assertEqual(config.availability_unavailable_count, 0)
        self.assertIsNone(config.availability_message)
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_unavailable_monitor_does_not_test_without_local_model_whitelist(self) -> None:
        await self.service.overview(self.db)
        channel = await self.db.scalar(select(Upstream))
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = 91
        config.availability_test_model = "account-test-model"
        config.available_models = None
        channel.upstream_monitor_status = "ok"
        channel.upstream_monitor_checked_at = datetime.now(timezone.utc)
        channel.upstream_monitors = [
            {"id": 91, "name": "Broken", "primary_status": "unavailable"}
        ]
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertIsNone(action)
        self.assertEqual(evidence["status"], "not_configured")
        self.assertEqual(config.availability_status, "not_configured")
        self.assertIn("whitelist", config.availability_message.lower())
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_global_fallback_model_must_be_in_account_whitelist(self) -> None:
        await self.service.overview(self.db)
        channel = await self.db.scalar(select(Upstream))
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = 91
        config.availability_test_model = None
        config.available_models = [{"id": "account-allowed-model", "display_name": "Allowed"}]
        channel.upstream_monitor_status = "ok"
        channel.upstream_monitor_checked_at = datetime.now(timezone.utc)
        channel.upstream_monitors = [
            {"id": 91, "name": "Broken", "primary_status": "unavailable"}
        ]
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_models.return_value = ["global-not-allowed"]

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertIsNone(action)
        self.assertEqual(evidence["status"], "not_configured")
        self.assertIn("whitelist", config.availability_message.lower())
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_cached_upstream_monitor_result_does_not_retest_or_change_account(self) -> None:
        await self.service.overview(self.db)
        channel = await self.db.scalar(select(Upstream))
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = 91
        config.availability_test_model = "account-test-model"
        config.availability_status = "available"
        config.availability_source = "upstream_monitor"
        channel.upstream_monitor_status = "ok"
        channel.upstream_monitors = [
            {"id": 91, "name": "Cached", "primary_status": "unavailable"}
        ]
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
            monitor_probe_fresh=False,
        )

        self.assertIsNone(action)
        self.assertEqual(evidence["status"], "cached")
        self.assertEqual(config.availability_status, "available")
        self.assertEqual(config.availability_source, "upstream_monitor")
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_missing_or_unknown_monitor_falls_back_to_account_connection(self) -> None:
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_recovery_consecutive_threshold.return_value = 1
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="upstream_monitor",
            availability_monitor_id=91,
            availability_test_model="account-test-model",
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"}
            ],
        )
        channel = Upstream(
            display_name="Missing monitor",
            api_endpoint_url="https://missing-monitor.example",
            upstream_monitor_status="ok",
            upstream_monitors=[],
            upstream_monitor_checked_at=datetime.now(timezone.utc),
        )
        self.sub2api.connection_test_results = [(True, None)]

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(config.availability_status, "available")
        self.assertEqual(config.availability_source, "upstream_monitor_fallback")
        self.assertEqual(evidence["monitor_status"], "not_configured")
        self.assertEqual(evidence["test_attempts"], 1)
        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model")],
        )

        channel.upstream_monitors = [
            {"id": 91, "name": "Unknown", "primary_status": "unknown"}
        ]
        self.sub2api.connection_test_results = [(False, "still unavailable")]
        self.runtime_config.get_upstream_monitor_unavailable_consecutive_threshold.return_value = 1

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "hold")
        self.assertEqual(config.availability_status, "unavailable")
        self.assertEqual(evidence["monitor_status"], "unknown")
        self.assertEqual(evidence["test_status"], "unavailable")

    async def test_connection_attempt_limit_stops_on_first_success(self) -> None:
        config = ApiAccount(management_account_id=7, remote_schedulable=True)
        self.sub2api.connection_test_results = [
            (False, "first failed"),
            (True, None),
            (False, "must not run"),
        ]

        sleep = AsyncMock()
        with patch("app.services.upstream_channels.asyncio.sleep", new=sleep):
            success, error, account_id, completed = (
                await self.service._test_account_connection_candidates(
                    [config],
                    "account-test-model",
                    3,
                    attempt_interval_seconds=2,
                )
            )

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertEqual(account_id, 7)
        self.assertEqual(completed, 2)
        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model"), (7, "account-test-model")],
        )
        sleep.assert_awaited_once_with(2.0)

    async def test_strict_channel_fallback_reports_all_successful_attempts(self) -> None:
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_api_key_availability_all_tests_must_succeed.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_attempts.return_value = 2
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="upstream_monitor",
            availability_monitor_id=91,
            availability_test_model="account-test-model",
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"}
            ],
        )
        channel = Upstream(
            display_name="Strict missing monitor",
            api_endpoint_url="https://strict-missing-monitor.example",
            upstream_monitor_status="ok",
            upstream_monitors=[],
            upstream_monitor_checked_at=datetime.now(timezone.utc),
        )
        self.sub2api.connection_test_results = [(True, None), (True, None)]

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(evidence["test_success_policy"], "all")
        self.assertEqual(evidence["test_attempts"], 2)
        self.assertIn(
            "All 2 fallback connection tests succeeded",
            config.availability_message,
        )

    async def test_connection_attempts_require_every_success_in_strict_mode(self) -> None:
        config = ApiAccount(management_account_id=7, remote_schedulable=True)
        self.sub2api.connection_test_results = [
            (True, None),
            (False, "middle failed"),
            (True, None),
        ]

        success, error, account_id, completed = (
            await self.service._test_account_connection_candidates(
                [config],
                "account-test-model",
                3,
                require_all_success=True,
            )
        )

        self.assertFalse(success)
        self.assertEqual(error, "middle failed")
        self.assertEqual(account_id, 7)
        self.assertEqual(completed, 3)
        self.assertEqual(len(self.sub2api.connection_test_calls), 3)

        self.sub2api.connection_test_calls.clear()
        self.sub2api.connection_test_results = [(True, None)] * 3
        success, error, account_id, completed = (
            await self.service._test_account_connection_candidates(
                [config],
                "account-test-model",
                3,
                require_all_success=True,
            )
        )

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertEqual(account_id, 7)
        self.assertEqual(completed, 3)
        self.assertEqual(len(self.sub2api.connection_test_calls), 3)

    async def test_connection_attempt_interval_skips_sleep_after_last_failure(self) -> None:
        config = ApiAccount(management_account_id=7, remote_schedulable=True)
        self.sub2api.connection_test_results = [
            (False, "first failed"),
            (False, "second failed"),
            (False, "last failed"),
        ]
        sleep = AsyncMock()

        with patch("app.services.upstream_channels.asyncio.sleep", new=sleep):
            success, error, account_id, completed = (
                await self.service._test_account_connection_candidates(
                    [config],
                    "account-test-model",
                    3,
                    attempt_interval_seconds=1.5,
                )
            )

        self.assertFalse(success)
        self.assertEqual(error, "last failed")
        self.assertEqual(account_id, 7)
        self.assertEqual(completed, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual([call.args for call in sleep.await_args_list], [(1.5,), (1.5,)])

    async def test_manual_account_availability_uses_independent_pause_and_recovery_attempts(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "independent_model"
        config.availability_test_model = "account-test-model"
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        await self.db.commit()
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_attempts.return_value = 3
        self.runtime_config.get_upstream_monitor_recovery_test_attempts.return_value = 2
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )
        self.sub2api.connection_test_results = [
            (False, "manual failure 1"),
            (False, "manual failure 2"),
            (False, "manual failure 3"),
        ]

        unavailable = await self.service.test_account_availability(
            self.db,
            7,
            fingerprint,
        )

        self.assertEqual(unavailable.policy_action, "hold")
        self.assertEqual(unavailable.account.availability_status, "unavailable")
        self.assertEqual(unavailable.evidence["test_purpose"], "pause")
        self.assertEqual(unavailable.evidence["test_attempts"], 3)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])

        self.sub2api.connection_test_results = [
            (False, "recovery failure"),
            (True, None),
        ]
        available = await self.service.test_account_availability(
            self.db,
            7,
            fingerprint,
        )

        self.assertEqual(available.policy_action, "clear")
        self.assertEqual(available.account.availability_status, "available")
        self.assertEqual(available.evidence["test_purpose"], "recovery")
        self.assertEqual(available.evidence["test_attempts"], 2)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False), (7, True)])
        self.assertFalse(
            any(
                hold.reason == "upstream_monitor_unavailable"
                for hold in available.account.active_pause_holds
            )
        )

    async def test_recovery_all_attempts_fail_and_legacy_counters_are_cleared(self) -> None:
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="independent_model",
            availability_test_model="account-test-model",
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"}
            ],
            availability_unavailable_count=2,
            availability_recovery_count=2,
        )
        channel = Upstream(
            id=11,
            display_name="Recovery attempts",
            api_endpoint_url="https://recovery-attempts.example",
        )
        self.service.accounts.set_pause_hold(
            config,
            "upstream_monitor_unavailable",
            active=True,
            scope_upstream_id=11,
            recovery_mode="account_availability_healthy",
            now=datetime.now(timezone.utc),
        )
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_attempts.return_value = 1
        self.runtime_config.get_upstream_monitor_recovery_test_attempts.return_value = 3
        self.sub2api.connection_test_results = [
            (False, "recovery failure 1"),
            (False, "recovery failure 2"),
            (False, "recovery failure 3"),
        ]

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "hold")
        self.assertEqual(evidence["test_purpose"], "recovery")
        self.assertEqual(evidence["test_attempts"], 3)
        self.assertEqual(len(self.sub2api.connection_test_calls), 3)
        self.assertEqual(config.availability_unavailable_count, 0)
        self.assertEqual(config.availability_recovery_count, 0)
        self.runtime_config.get_upstream_monitor_unavailable_consecutive_threshold.assert_not_awaited()
        self.runtime_config.get_upstream_monitor_recovery_consecutive_threshold.assert_not_awaited()

    async def test_other_pause_hold_skips_availability_connection_test(self) -> None:
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="independent_model",
            availability_test_model="account-test-model",
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"}
            ],
        )
        channel = Upstream(
            id=11,
            display_name="Balance paused",
            api_endpoint_url="https://balance-paused.example",
        )
        self.service.accounts.set_pause_hold(
            config,
            "upstream_balance_negative",
            active=True,
            scope_upstream_id=11,
            recovery_mode="balance_at_or_above_threshold",
            now=datetime.now(timezone.utc),
        )
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        blocker = self.service._availability_test_blocker(config, None)

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
            blocking_pause_reason=blocker,
        )

        self.assertIsNone(action)
        self.assertEqual(evidence["status"], "automatic_test_paused")
        self.assertEqual(evidence["blocked_by"], "upstream_balance_negative")
        self.assertIsNone(config.availability_status)
        self.assertIsNone(config.availability_source)
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_other_pause_hold_skips_upstream_monitor_fallback_test(self) -> None:
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="upstream_monitor",
            availability_monitor_id=91,
            availability_test_model="account-test-model",
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"}
            ],
        )
        channel = Upstream(
            id=11,
            display_name="Balance paused monitor",
            api_endpoint_url="https://balance-paused-monitor.example",
            upstream_monitor_status="ok",
            upstream_monitors=[
                {"id": 91, "name": "Primary", "primary_status": "unavailable"}
            ],
        )
        self.service.accounts.set_pause_hold(
            config,
            "upstream_balance_negative",
            active=True,
            scope_upstream_id=11,
            recovery_mode="balance_at_or_above_threshold",
            now=datetime.now(timezone.utc),
        )
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        blocker = self.service._availability_test_blocker(config, None)

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
            blocking_pause_reason=blocker,
        )

        self.assertIsNone(action)
        self.assertEqual(evidence["status"], "automatic_test_paused")
        self.assertEqual(evidence["monitor_status"], "unavailable")
        self.assertEqual(evidence["blocked_by"], "upstream_balance_negative")
        self.assertEqual(self.sub2api.connection_test_calls, [])

    async def test_pause_attempts_stop_on_first_success_without_creating_hold(self) -> None:
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="independent_model",
            availability_test_model="account-test-model",
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"}
            ],
        )
        channel = Upstream(
            display_name="Pause attempts",
            api_endpoint_url="https://pause-attempts.example",
        )
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_attempts.return_value = 3
        self.sub2api.connection_test_results = [
            (False, "first failure"),
            (True, None),
            (False, "must not run"),
        ]

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "clear")
        self.assertEqual(evidence["test_purpose"], "pause")
        self.assertEqual(evidence["test_attempts"], 2)
        self.assertEqual(len(self.sub2api.connection_test_calls), 2)
        self.assertEqual(config.availability_status, "available")

    async def test_pause_attempts_require_all_success_when_strict_policy_enabled(self) -> None:
        config = ApiAccount(
            management_account_id=7,
            availability_check_mode="independent_model",
            availability_test_model="account-test-model",
            available_models=[
                {"id": "account-test-model", "display_name": "Account test"}
            ],
        )
        channel = Upstream(
            display_name="Strict pause attempts",
            api_endpoint_url="https://strict-pause-attempts.example",
        )
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_api_key_availability_all_tests_must_succeed.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_attempts.return_value = 3
        self.sub2api.connection_test_results = [
            (True, None),
            (False, "second failure"),
            (True, None),
        ]

        action, evidence = await self.service._prepare_account_monitor_guard(
            config,
            channel,
            self.runtime_config,
            automation_paused=False,
        )

        self.assertEqual(action, "hold")
        self.assertEqual(evidence["test_success_policy"], "all")
        self.assertEqual(evidence["test_attempts"], 3)
        self.assertEqual(evidence["test_status"], "unavailable")
        self.assertEqual(len(self.sub2api.connection_test_calls), 3)
        self.assertEqual(config.availability_status, "unavailable")

    async def test_manual_availability_tests_manually_disabled_account_without_enabling_it(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "independent_model"
        config.availability_test_model = "account-test-model"
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        await self.db.commit()
        self.sub2api.accounts[0]["schedulable"] = False
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.sub2api.connection_test_results = [(True, None)]
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )

        result = await self.service.test_account_availability(
            self.db,
            7,
            fingerprint,
        )

        self.assertEqual(result.policy_action, "clear")
        self.assertEqual(result.account.availability_status, "available")
        self.assertTrue(result.evidence["manual_test"])
        self.assertEqual(result.evidence["monitor_refresh_status"], "not_required")
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [])
        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model")],
        )

    async def test_manual_availability_without_monitor_forces_model_fallback(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = None
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        channel = await self.db.get(Upstream, config.upstream_id)
        assert channel is not None
        stale_monitor_checked_at = datetime(2020, 1, 1)
        channel.upstream_monitor_checked_at = stale_monitor_checked_at
        await self.db.commit()
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = False
        self.runtime_config.get_upstream_monitor_fallback_without_monitor_enabled.return_value = False
        self.runtime_config.get_upstream_monitor_fallback_test_models.return_value = [
            "account-test-model"
        ]
        self.sub2api.connection_test_results = [(True, None)]
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )

        result = await self.service.test_account_availability(
            self.db,
            7,
            fingerprint,
        )

        self.assertEqual(result.policy_action, "clear")
        self.assertEqual(result.account.availability_status, "available")
        self.assertEqual(result.account.availability_source, "upstream_monitor_fallback")
        assert result.account.availability_checked_at is not None
        self.assertGreater(
            result.account.availability_checked_at,
            stale_monitor_checked_at,
        )
        self.assertEqual(result.evidence["monitor_refresh_status"], "not_required")
        self.assertTrue(result.evidence["fallback_without_monitor_enabled"])
        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model")],
        )

    async def test_manual_unbound_fallback_failure_does_not_pause_when_policy_disabled(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        assert config is not None
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = None
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        await self.db.commit()
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = False
        self.runtime_config.get_upstream_monitor_fallback_without_monitor_enabled.return_value = False
        self.runtime_config.get_upstream_monitor_fallback_test_models.return_value = [
            "account-test-model"
        ]
        self.sub2api.connection_test_results = [(False, "test failed")]
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )

        result = await self.service.test_account_availability(
            self.db,
            7,
            fingerprint,
        )

        self.assertEqual(result.policy_action, "clear")
        self.assertEqual(result.account.availability_status, "unavailable")
        self.assertEqual(result.account.availability_source, "upstream_monitor_fallback")
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [])
        self.assertEqual(
            self.sub2api.connection_test_calls,
            [(7, "account-test-model")],
        )

    async def test_forced_connection_test_does_not_change_automation_state(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        assert config is not None
        config.availability_check_mode = "disabled"
        config.availability_test_model = "account-test-model"
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        config.availability_status = "unavailable"
        config.availability_message = "previous automatic monitor result"
        self.service.accounts.set_pause_hold(
            config,
            AUTO_PAUSE_REASON_MONITOR,
            active=True,
            scope_upstream_id=config.upstream_id,
            recovery_mode="account_availability_healthy",
            now=datetime.now(timezone.utc),
        )
        await self.db.commit()
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )
        self.sub2api.connection_test_results = [(True, None)]

        result = await self.service.test_account_connection(
            self.db,
            7,
            fingerprint,
        )

        await self.db.refresh(config)
        self.assertTrue(result.success)
        self.assertEqual(result.model, "account-test-model")
        self.assertIsNone(result.error)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(self.sub2api.connection_test_calls, [(7, "account-test-model")])
        self.assertEqual(config.availability_status, "unavailable")
        self.assertEqual(config.availability_message, "previous automatic monitor result")
        self.assertTrue(
            any(
                hold.reason == AUTO_PAUSE_REASON_MONITOR
                for hold in self.service.accounts.active_pause_holds(config)
            )
        )

    async def test_forced_connection_test_allows_account_without_channel(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        assert config is not None
        config.upstream_id = None
        config.availability_test_model = "account-test-model"
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        await self.db.commit()
        self.sub2api.connection_test_results = [(True, None)]
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )

        result = await self.service.test_account_connection(
            self.db,
            7,
            fingerprint,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.model, "account-test-model")
        self.assertEqual(self.sub2api.connection_test_calls, [(7, "account-test-model")])

    async def test_targeted_monitor_refresh_preserves_other_detail_cache(self) -> None:
        checked_at = datetime.now(timezone.utc)
        channel = Upstream(
            display_name="Targeted monitor refresh",
            api_endpoint_url="https://targeted-monitor.example",
            upstream_monitors=[
                {
                    "id": 91,
                    "name": "Primary",
                    "primary_status": "available",
                    "timeline": [{"status": "available", "checked_at": "2026-07-28T00:00:00Z"}],
                },
                {
                    "id": 92,
                    "name": "Secondary",
                    "primary_status": "available",
                    "primary_latency_ms": 21,
                    "timeline": [{"status": "available", "checked_at": "2026-07-28T00:00:00Z"}],
                },
                {"id": 93, "name": "Removed", "primary_status": "available"},
            ],
        )
        result = SimpleNamespace(
            status="ok",
            upstream_monitors=[
                {
                    "id": 91,
                    "name": "Primary",
                    "primary_status": "unavailable",
                    "timeline": [{"status": "unavailable", "checked_at": "2026-07-28T01:00:00Z"}],
                },
                {
                    "id": 92,
                    "name": "Secondary renamed",
                    "primary_status": "unknown",
                    "primary_latency_ms": None,
                    "timeline": [],
                },
            ],
            upstream_monitors_total=2,
            upstream_monitors_status="ok",
            upstream_monitors_message="Read 2 monitor(s).",
        )

        self.service._apply_upstream_monitor_discovery(
            channel,
            result,
            now=checked_at,
            upstream_monitor_detail_ids={91},
        )

        self.assertEqual([item["id"] for item in channel.upstream_monitors], [91, 92])
        primary, secondary = channel.upstream_monitors
        self.assertEqual(primary["primary_status"], "unavailable")
        self.assertEqual(primary["timeline"][0]["status"], "unavailable")
        self.assertEqual(secondary["name"], "Secondary renamed")
        self.assertEqual(secondary["primary_status"], "available")
        self.assertEqual(secondary["primary_latency_ms"], 21)
        self.assertEqual(secondary["timeline"][0]["status"], "available")
        self.assertEqual(channel.upstream_monitor_count, 2)

    async def test_manual_upstream_monitor_availability_refreshes_monitor_details_first(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        channel = await self.db.get(Upstream, config.upstream_id)
        config.availability_check_mode = "upstream_monitor"
        config.availability_monitor_id = 91
        channel.upstream_monitor_status = "ok"
        channel.upstream_monitors = [
            {"id": 91, "name": "Primary", "primary_status": "available"}
        ]
        await self.db.commit()
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )

        discover = AsyncMock()
        with patch.object(self.service, "_discover_channel", new=discover):
            result = await self.service.test_account_availability(
                self.db,
                7,
                fingerprint,
            )

        discover.assert_awaited_once()
        self.assertTrue(discover.await_args.kwargs["monitor_details_only"])
        self.assertTrue(discover.await_args.kwargs["include_upstream_monitor_details"])
        self.assertEqual(discover.await_args.kwargs["upstream_monitor_detail_ids"], {91})
        self.assertFalse(discover.await_args.kwargs["sync_inventory"])
        self.assertEqual(result.account.availability_status, "available")
        self.assertTrue(result.evidence["manual_test"])
        self.assertEqual(result.evidence["monitor_refresh_status"], "refreshed")

    async def test_manual_availability_preserves_probe_error_when_pause_write_fails(self) -> None:
        await self.service.overview(self.db)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "independent_model"
        config.availability_test_model = "account-test-model"
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        await self.db.commit()
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_unavailable_consecutive_threshold.return_value = 1
        fingerprint = self.service.accounts._remote_identity_fingerprint(
            self.sub2api.accounts[0]
        )
        self.sub2api.connection_test_results = [(False, "specific probe failure")]
        self.sub2api.set_account_schedulable = AsyncMock(
            side_effect=RuntimeError("remote write failed")
        )

        result = await self.service.test_account_availability(
            self.db,
            7,
            fingerprint,
        )

        self.assertEqual(result.policy_action, "hold")
        self.assertEqual(result.policy_status, "disable_failed")
        self.assertIsNotNone(result.policy_error)
        self.assertEqual(result.account.availability_status, "unavailable")
        self.assertEqual(result.account.availability_message, "specific probe failure")
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])

    async def test_monitor_and_balance_holds_clear_independently_before_restore(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_api_key_auto_pause_on_negative_balance_enabled.return_value = True
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }
        configs = list(
            (
                await self.db.execute(
                    select(ApiAccount).where(
                        ApiAccount.upstream_id == upstream_id
                    )
                )
            ).scalars()
        )
        for config in configs:
            config.availability_check_mode = "upstream_monitor"
            config.availability_monitor_id = 91
            config.availability_test_model = "account-test-model"
        await self.db.commit()

        def result(balance: float, monitor_status: str) -> SimpleNamespace:
            discovered = self._discovery_result(
                wallet_balance_usd=balance,
                account_upstream_states=healthy_states,
                upstream_monitors=[
                    {
                        "id": 91,
                        "name": "Primary",
                        "primary_status": monitor_status,
                        "timeline": [],
                    }
                ],
            )
            discovered.account_group_matches = {
                account_id: {
                    "id": "default",
                    "name": "Default",
                    "multiplier": 1.0,
                }
                for account_id in healthy_states
            }
            return discovered

        unavailable = result(5.0, "unavailable")
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=unavailable),
        ):
            first = await self.service.discover_channel(self.db, upstream_id)
            second = await self.service.discover_channel(self.db, upstream_id)
        self.assertEqual(first.upstream_monitor_guard_state, "account_scoped")
        self.assertEqual(second.upstream_monitor_guard_state, "account_scoped")
        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (8, False)],
        )

        negative = result(-2.0, "unavailable")
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=negative),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        reasons = {hold.reason for hold in config.pause_holds if hold.active}
        self.assertEqual(
            reasons,
            {"upstream_monitor_unavailable", "upstream_balance_negative"},
        )

        monitor_recovered_balance_still_low = result(-2.0, "available")
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=monitor_recovered_balance_still_low),
        ):
            await self.service.discover_channel(self.db, upstream_id)
            await self.service.discover_channel(self.db, upstream_id)
        await self.db.refresh(config)
        reasons = {hold.reason for hold in config.pause_holds if hold.active}
        self.assertEqual(reasons, {"upstream_balance_negative"})
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(len(self.sub2api.schedulable_calls), 2)

        fully_recovered = result(3.0, "available")
        recovery_notifications = AsyncMock()
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=fully_recovered),
            ),
            patch(
                "app.services.upstream_accounts.enqueue_api_key_account_state_changed",
                new=recovery_notifications,
            ),
        ):
            restored = await self.service.discover_channel(self.db, upstream_id)
        self.assertEqual(restored.balance_guard_state, "healthy")
        self.assertTrue(all(account["schedulable"] for account in self.sub2api.accounts))
        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (8, False), (7, True), (8, True)],
        )
        self.assertEqual(recovery_notifications.await_count, 2)
        self.assertTrue(
            all(
                item.kwargs["reason_details"]["previous_pause_reasons"]
                == ["upstream_balance_negative", "upstream_monitor_unavailable"]
                for item in recovery_notifications.await_args_list
            )
        )

    async def test_rate_hold_uses_absolute_threshold_without_a_previous_baseline(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        await self._enable_rate_interval(absolute=1.2)
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }

        def rate_result(multiplier: float) -> SimpleNamespace:
            discovered = self._discovery_result(
                account_upstream_states=healthy_states,
            )
            discovered.groups = [
                {"id": "default", "name": "Default", "multiplier": multiplier}
            ]
            discovered.account_group_matches = {
                account_id: {
                    "id": "default",
                    "name": "Default",
                    "multiplier": multiplier,
                }
                for account_id in healthy_states
            }
            return discovered

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=rate_result(1.5)),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (8, False)],
        )
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        hold = next(
            hold
            for hold in config.pause_holds
            if hold.reason == "upstream_rate_increase" and hold.active
        )
        self.assertNotIn("baseline_multiplier", hold.evidence_json)
        self.assertEqual(hold.evidence_json["observed_multiplier"], 1.5)
        self.assertEqual(hold.evidence_json["absolute_threshold"], 1.2)

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=rate_result(1.2)),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        self.assertTrue(all(account["schedulable"] for account in self.sub2api.accounts))
        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (8, False), (7, True), (8, True)],
        )

    async def test_absolute_upstream_multiplier_threshold_pauses_above_and_restores_at_boundary(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        await self._enable_rate_interval(absolute=1.2)
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }

        def rate_result(multiplier: float) -> SimpleNamespace:
            discovered = self._discovery_result(
                account_upstream_states=healthy_states,
            )
            discovered.groups = [
                {"id": "default", "name": "Default", "multiplier": multiplier}
            ]
            discovered.account_group_matches = {
                account_id: {
                    "id": "default",
                    "name": "Default",
                    "multiplier": multiplier,
                }
                for account_id in healthy_states
            }
            return discovered

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=rate_result(1.2)),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        self.assertEqual(self.sub2api.schedulable_calls, [])

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=rate_result(1.21)),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False), (8, False)])
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        hold = next(
            item
            for item in config.pause_holds
            if item.reason == "upstream_rate_increase" and item.active
        )
        self.assertEqual(hold.recovery_mode, "rate_at_or_below_absolute_threshold")
        self.assertEqual(hold.evidence_json["mode"], "absolute_multiplier")
        self.assertEqual(hold.evidence_json["absolute_threshold"], 1.2)

        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=rate_result(1.2)),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (8, False), (7, True), (8, True)],
        )
        events = list(
            (
                await self.db.scalars(
                    select(AccountSchedulingChangeLog).order_by(
                        AccountSchedulingChangeLog.id
                    )
                )
            ).all()
        )
        self.assertEqual(
            [event.event_type for event in events],
            ["paused", "paused", "restored", "restored"],
        )
        self.assertTrue(
            all(event.reason == "upstream_rate_increase" for event in events)
        )

    async def test_raising_absolute_rate_threshold_releases_old_hold(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        await self._enable_rate_interval(absolute=1.0)
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }

        def rate_result(multiplier: float) -> SimpleNamespace:
            discovered = self._discovery_result(
                account_upstream_states=healthy_states,
            )
            discovered.groups = [
                {"id": "default", "name": "Default", "multiplier": multiplier}
            ]
            discovered.account_group_matches = {
                account_id: {
                    "id": "default",
                    "name": "Default",
                    "multiplier": multiplier,
                }
                for account_id in healthy_states
            }
            return discovered

        observed = rate_result(1.2)
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=observed),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False), (8, False)])

        interval = await self.db.scalar(select(UpstreamPriorityInterval))
        interval.rate_absolute_threshold = 1.2
        await self.db.commit()
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=observed),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        self.assertEqual(
            self.sub2api.schedulable_calls,
            [(7, False), (8, False), (7, True), (8, True)],
        )
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        rate_hold = next(
            item
            for item in config.pause_holds
            if item.reason == "upstream_rate_increase"
        )
        self.assertFalse(rate_hold.active)

    async def test_disabled_policy_releases_owned_hold_when_discovery_fails(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        now = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)
        self.service.accounts.set_pause_hold(
            config,
            "upstream_rate_increase",
            active=True,
            scope_upstream_id=upstream_id,
            recovery_mode="rate_within_threshold",
            now=now,
            evidence={"baseline_multiplier": 1.0, "observed_multiplier": 1.5},
        )
        config.pause_owned_by_plugin = True
        config.auto_pause_episode_id = "disabled-policy-episode"
        config.auto_pause_upstream_id = upstream_id
        config.auto_paused_at = now
        config.pause_operation = "paused"
        self.service.accounts.sync_pause_compatibility_fields(config)
        self.sub2api.accounts[0]["schedulable"] = False
        await self.db.commit()

        failed = self._discovery_result(status="error", wallet_balance_usd=None)
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=failed),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(config, attribute_names=["pause_holds"])
        self.assertFalse(any(hold.active for hold in config.pause_holds))
        self.assertFalse(config.pause_owned_by_plugin)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, True)])

    async def test_failed_discovery_applies_independent_availability_pause(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        config.availability_check_mode = "independent_model"
        config.availability_test_model = "account-test-model"
        config.available_models = [
            {"id": "account-test-model", "display_name": "Account test"}
        ]
        await self.db.commit()
        self.runtime_config.get_api_key_auto_pause_on_upstream_monitor_unavailable_enabled.return_value = True
        self.runtime_config.get_upstream_monitor_fallback_test_attempts.return_value = 2
        self.sub2api.connection_test_results = [
            (False, "first failure"),
            (False, "second failure"),
        ]

        failed = self._discovery_result(status="error", wallet_balance_usd=None)
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=failed),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(config, attribute_names=["pause_holds"])
        hold = next(
            item
            for item in config.pause_holds
            if item.reason == "upstream_monitor_unavailable"
        )
        self.assertTrue(hold.active)
        self.assertEqual(hold.evidence_json["test_purpose"], "pause")
        self.assertEqual(hold.evidence_json["test_attempts"], 2)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])

    async def test_failed_discovery_retries_owned_restore_after_write_failure(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        now = datetime(2026, 7, 18, 9, 10, tzinfo=timezone.utc)
        self.service.accounts.set_pause_hold(
            config,
            "upstream_rate_increase",
            active=True,
            scope_upstream_id=upstream_id,
            recovery_mode="rate_within_threshold",
            now=now,
            evidence={"baseline_multiplier": 1.0, "observed_multiplier": 1.5},
        )
        config.pause_owned_by_plugin = True
        config.auto_pause_episode_id = "restore-retry-episode"
        config.auto_pause_upstream_id = upstream_id
        config.auto_paused_at = now
        config.pause_operation = "paused"
        self.service.accounts.sync_pause_compatibility_fields(config)
        self.sub2api.accounts[0]["schedulable"] = False
        await self.db.commit()

        original_set_schedulable = self.sub2api.set_account_schedulable
        attempts = 0

        async def fail_first_restore(account_id: int, schedulable: bool) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary restore failure")
            await original_set_schedulable(account_id, schedulable)

        failed = self._discovery_result(status="error", wallet_balance_usd=None)
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=failed),
            ),
            patch.object(
                self.sub2api,
                "set_account_schedulable",
                new=AsyncMock(side_effect=fail_first_restore),
            ),
        ):
            await self.service.discover_channel(self.db, upstream_id)
            await self.db.refresh(config, attribute_names=["pause_holds"])
            self.assertEqual(config.pause_operation, "restore_pending")
            self.assertTrue(config.pause_owned_by_plugin)
            self.assertFalse(self.sub2api.accounts[0]["schedulable"])

            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(config, attribute_names=["pause_holds"])
        self.assertEqual(attempts, 2)
        self.assertFalse(config.pause_owned_by_plugin)
        self.assertIsNone(config.pause_operation)
        self.assertTrue(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, True)])

    async def test_failed_discovery_retries_active_pause_after_write_failure(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        now = datetime(2026, 7, 18, 9, 20, tzinfo=timezone.utc)
        self.service.accounts.set_pause_hold(
            config,
            "upstream_rate_increase",
            active=True,
            scope_upstream_id=upstream_id,
            recovery_mode="rate_within_threshold",
            now=now,
            evidence={"baseline_multiplier": 1.0, "observed_multiplier": 1.5},
        )
        config.rate_pause_policy = "custom"
        config.rate_absolute_threshold = 1.0
        self.service.accounts.sync_pause_compatibility_fields(config)
        await self.db.commit()
        original_set_schedulable = self.sub2api.set_account_schedulable
        attempts = 0

        async def fail_first_pause(account_id: int, schedulable: bool) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary pause failure")
            await original_set_schedulable(account_id, schedulable)

        failed = self._discovery_result(status="error", wallet_balance_usd=None)
        with (
            patch(
                "app.services.upstream_channels.discover_upstream",
                new=AsyncMock(return_value=failed),
            ),
            patch.object(
                self.sub2api,
                "set_account_schedulable",
                new=AsyncMock(side_effect=fail_first_pause),
            ),
        ):
            await self.service.discover_channel(self.db, upstream_id)
            await self.db.refresh(config, attribute_names=["pause_holds"])
            self.assertEqual(config.pause_operation, "pause_pending")
            self.assertTrue(config.pause_owned_by_plugin)
            self.assertTrue(self.sub2api.accounts[0]["schedulable"])

            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(config, attribute_names=["pause_holds"])
        self.assertEqual(attempts, 2)
        self.assertTrue(config.pause_owned_by_plugin)
        self.assertEqual(config.pause_operation, "paused")
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])

    async def test_runtime_setting_errors_preserve_existing_policy_holds(self) -> None:
        upstream_id = (await self.service.overview(self.db)).upstreams[0].upstream_id
        config = await self.db.scalar(
            select(ApiAccount).where(
                ApiAccount.management_account_id == 7
            )
        )
        now = datetime(2026, 7, 18, 9, 30, tzinfo=timezone.utc)
        for reason, recovery_mode in (
            ("upstream_key_unavailable", "upstream_healthy"),
            ("upstream_rate_increase", "rate_within_threshold"),
        ):
            self.service.accounts.set_pause_hold(
                config,
                reason,
                active=True,
                scope_upstream_id=upstream_id,
                recovery_mode=recovery_mode,
                now=now,
                evidence=(
                    {"baseline_multiplier": 1.0, "observed_multiplier": 1.5}
                    if reason == "upstream_rate_increase"
                    else {"key_status": "disabled"}
                ),
            )
        config.pause_owned_by_plugin = True
        config.rate_pause_policy = "custom"
        config.rate_absolute_threshold = 1.0
        config.auto_pause_episode_id = "runtime-setting-error-episode"
        config.auto_pause_upstream_id = upstream_id
        config.auto_paused_at = now
        config.pause_operation = "paused"
        self.service.accounts.sync_pause_compatibility_fields(config)
        self.sub2api.accounts[0]["schedulable"] = False
        await self.db.commit()

        self.runtime_config.get_api_key_auto_disable_on_upstream_unavailable.side_effect = (
            RuntimeError("settings unavailable")
        )
        healthy_states = {
            account_id: AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="default",
                group_name="Default",
            )
            for account_id in (7, 8)
        }
        healthy = self._discovery_result(account_upstream_states=healthy_states)
        healthy.account_group_matches = {
            account_id: {"id": "default", "name": "Default", "multiplier": 1.5}
            for account_id in healthy_states
        }
        healthy.groups = [{"id": "default", "name": "Default", "multiplier": 1.5}]
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=healthy),
        ):
            await self.service.discover_channel(self.db, upstream_id)
        failed = self._discovery_result(status="error", wallet_balance_usd=None)
        with patch(
            "app.services.upstream_channels.discover_upstream",
            new=AsyncMock(return_value=failed),
        ):
            await self.service.discover_channel(self.db, upstream_id)

        await self.db.refresh(config, attribute_names=["pause_holds"])
        active_reasons = {hold.reason for hold in config.pause_holds if hold.active}
        self.assertEqual(
            active_reasons,
            {"upstream_key_unavailable", "upstream_rate_increase"},
        )
        self.assertTrue(config.pause_owned_by_plugin)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [])


class UpstreamAuthenticationTests(unittest.TestCase):
    upstream_id = "00000000-0000-4000-8000-000000000007"

    def test_refresh_upstream_monitors_uses_authenticated_service(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")
        db = AsyncMock()
        db.execute.return_value = SimpleNamespace(
            scalar_one_or_none=lambda: SimpleNamespace(id=self.upstream_id)
        )
        result = UpstreamMonitorsOut(
            upstream_id=self.upstream_id,
            upstream_monitors=[{"id": 17, "name": "Monitor"}],
            upstream_monitor_count=1,
            upstream_monitor_status="ok",
        )
        service = SimpleNamespace(
            refresh_upstream_monitors=AsyncMock(return_value=result)
        )

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_service] = lambda: service
        with TestClient(app) as client:
            response = client.post(
                f"/api/upstreams/{self.upstream_id}/upstream-monitors/refresh"
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["upstream_monitor_count"], 1)
        service.refresh_upstream_monitors.assert_awaited_once_with(db, self.upstream_id)

    def test_delete_channel_uses_authenticated_service(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")
        db = AsyncMock()
        db.execute.return_value = SimpleNamespace(
            scalar_one_or_none=lambda: SimpleNamespace(id=self.upstream_id)
        )
        service = SimpleNamespace(delete_channel=AsyncMock())

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_service] = lambda: service
        with TestClient(app) as client:
            response = client.delete(f"/api/upstreams/{self.upstream_id}")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["message"], "空上游已归档。")
        service.delete_channel.assert_awaited_once_with(db, self.upstream_id)

    def test_sync_inventory_returns_overview_without_running_discovery(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")
        db = AsyncMock()
        result = UpstreamOverviewOut()
        service = SimpleNamespace(overview=AsyncMock(return_value=result))

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_service] = lambda: service
        with (
            patch("app.api.upstream_channels.record_event", new=AsyncMock()) as record,
            TestClient(app) as client,
        ):
            response = client.post("/api/upstreams/sync-inventory")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["upstreams"], [])
        service.overview.assert_awaited_once_with(db)
        record.assert_awaited_once_with(
            db,
            "manual_api_key_inventory_sync",
            "Synchronized 0 API key account(s) across 0 upstream(s).",
            details={
                "reason": "manual",
                "accounts": 0,
                "upstreams": 0,
                "unassigned_accounts": 0,
                "duration_ms": ANY,
            },
        )

    def test_discover_all_records_only_summary_counts(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")
        db = AsyncMock()
        db.execute.return_value = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [self.upstream_id])
        )
        result = UpstreamDiscoverAllOut(
            total=2,
            succeeded=1,
            failed=1,
            force=True,
            cache_max_age_seconds=None,
            upstreams=[],
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
        app.dependency_overrides[get_upstream_service] = lambda: service
        with (
            patch("app.api.upstream_channels.record_event", new=AsyncMock()) as record,
            patch(
                "app.api.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/upstreams/discover-all",
                json={"skip_upstream_ids": [self.upstream_id]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["failed"], 1)
        record.assert_awaited_once_with(
            db,
            "manual_upstream_sync",
            "Synchronized 2 upstream(s); 1 probed successfully, 0 reused cached state, 1 failed, and 0 skipped.",
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
                "skip_upstream_ids": [self.upstream_id],
            },
        )
        service.discover_all.assert_awaited_once_with(
            db,
            max_concurrency=2,
            require_management_credentials=True,
            force=True,
            skip_upstream_ids={self.upstream_id},
        )

    def test_manual_discover_all_probes_when_scheduled_probe_is_disabled(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")
        db = AsyncMock()
        result = UpstreamDiscoverAllOut(
            total=1,
            succeeded=1,
            failed=0,
            force=True,
            upstreams=[],
        )
        service = SimpleNamespace(
            discover_all=AsyncMock(return_value=result),
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
        app.dependency_overrides[get_upstream_service] = lambda: service
        with (
            patch("app.api.upstream_channels.record_event", new=AsyncMock()),
            patch(
                "app.api.upstream_channels.get_runtime_config_service",
                return_value=runtime,
            ),
            TestClient(app) as client,
        ):
            response = client.post("/api/upstreams/discover-all")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["probe_globally_enabled"])
        self.assertEqual(response.json()["total"], 1)
        self.assertEqual(response.json()["skipped"], 0)
        service.discover_all.assert_awaited_once_with(
            db,
            max_concurrency=0,
            require_management_credentials=True,
            force=True,
        )

    def test_discover_all_returns_success_when_summary_event_cannot_be_saved(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")
        db = AsyncMock()
        result = UpstreamDiscoverAllOut(
            total=1,
            succeeded=1,
            failed=0,
            force=False,
            cache_max_age_seconds=900,
            upstreams=[],
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
        app.dependency_overrides[get_upstream_service] = lambda: service
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
            response = client.post("/api/upstreams/discover-all")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["succeeded"], 1)
        db.rollback.assert_awaited_once()

    def test_every_route_requires_admin_session(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstreams")

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = fake_db
        requests = [
            ("GET", "/api/upstreams", None),
            ("POST", "/api/upstreams/sync-inventory", None),
            ("POST", "/api/upstreams/discover-all", None),
            ("GET", f"/api/upstreams/{self.upstream_id}/credentials", None),
            ("PUT", f"/api/upstreams/{self.upstream_id}", {"display_name": "test"}),
            ("POST", f"/api/upstreams/{self.upstream_id}/discover", None),
            (
                "POST",
                f"/api/upstreams/{self.upstream_id}/upstream-monitors/refresh",
                None,
            ),
        ]
        with TestClient(app) as client:
            for method, url, body in requests:
                response = client.request(method, url, json=body)
                self.assertEqual(response.status_code, 401, (method, url, response.text))

    def test_malformed_access_token_is_redacted_from_validation_response(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)
        app.include_router(router, prefix="/api/upstreams")
        secret = "fictional-malformed-channel-token"
        service = SimpleNamespace(update_channel=AsyncMock())

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_service] = lambda: service
        with TestClient(app) as client:
            response = client.put(
                f"/api/upstreams/{self.upstream_id}",
                json={"access_token": {"value": secret}},
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertIn("[redacted]", response.text)
        service.update_channel.assert_not_awaited()

    def test_malformed_refresh_token_is_redacted_from_validation_response(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)
        app.include_router(router, prefix="/api/upstreams")
        secret = "fictional-malformed-refresh-token"
        service = SimpleNamespace(update_channel=AsyncMock())

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_service] = lambda: service
        with TestClient(app) as client:
            response = client.put(
                f"/api/upstreams/{self.upstream_id}",
                json={"refresh_token": {"value": secret}},
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertIn("[redacted]", response.text)
        service.update_channel.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
