from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.upstream_accounts import router
from app.core.database import Base, get_db
from app.core.security import require_admin
from app.core.validation import sanitized_request_validation_handler
from app.models import UpstreamAccountConfig
from app.schemas import UpstreamAccountUpdate
from app.services.sub2api import (
    MAX_SUB2API_ACCOUNT_PAGES,
    MAX_SUB2API_RESPONSE_BYTES,
    Sub2ApiClient,
    Sub2ApiRequestError,
)
from app.services.upstream_accounts import (
    DEFAULT_ENCRYPTION_KEY,
    UpstreamAccountService,
    UpstreamAccountServiceError,
    get_upstream_account_service,
)
from app.services.upstream_channels import UpstreamChannelService
from app.services.upstream_client import DiscoveryResult, GroupOption


class FakeSub2Api(Sub2ApiClient):
    def __init__(self) -> None:
        super().__init__()
        self.accounts = [
            {
                "id": 7,
                "name": "upstream-seven",
                "platform": "openai",
                "type": "apikey",
                "status": "active",
                "rate_multiplier": 0.8,
                "credentials": {"base_url": "https://upstream.example.com/v1"},
            }
        ]
        self.balance_result: dict = {
            "status": "ok",
            "message": "Balance available.",
            "remaining": 12500.25,
            "total": 20000,
            "used": 7499.75,
            "unit": "USD",
            "checked_at": "2026-07-13T12:00:00Z",
        }
        self.balance_error: BaseException | None = None
        self.local_info: tuple[float, bool] = (1.0, True)
        self.local_error: BaseException | None = None
        self.update_calls: list[tuple[int, float]] = []
        self.schedulable_calls: list[tuple[int, bool]] = []
        self.delete_calls: list[int] = []

    async def list_api_key_accounts(self) -> list[dict]:
        return self.accounts

    async def get_account_balance(self, account: dict | str | int) -> dict:
        if self.balance_error is not None:
            raise self.balance_error
        return dict(self.balance_result)

    async def get_payment_balance_recharge_multiplier_info(self) -> tuple[float, bool]:
        if self.local_error is not None:
            raise self.local_error
        return self.local_info

    async def update_account_rate_multiplier(self, account_id: str | int, rate_multiplier: float) -> None:
        parsed_id = int(account_id)
        self.update_calls.append((parsed_id, rate_multiplier))
        self.accounts[0]["rate_multiplier"] = rate_multiplier

    async def get_account_current_rate_multiplier(self, account_id: str | int) -> float:
        return float(self.accounts[0]["rate_multiplier"])

    async def get_account(self, account: dict | str) -> dict | None:
        account_id = int(account if isinstance(account, str) else account["id"])
        return next((item for item in self.accounts if int(item["id"]) == account_id), None)

    async def set_account_schedulable(self, account_id: str | int, schedulable: bool) -> None:
        parsed_id = int(account_id)
        self.schedulable_calls.append((parsed_id, schedulable))
        account = await self.get_account(str(parsed_id))
        if account is not None:
            account["schedulable"] = schedulable

    async def delete_account(self, account: dict | str) -> bool:
        account_id = int(account if isinstance(account, str) else account["id"])
        self.delete_calls.append(account_id)
        before = len(self.accounts)
        self.accounts = [item for item in self.accounts if int(item["id"]) != account_id]
        return len(self.accounts) != before


def discovery_result(
    *,
    group: float | None,
    recharge: float | None,
    status: str = "ok",
) -> DiscoveryResult:
    option = GroupOption(id="gold", name="Gold", multiplier=group or 1.0, source="groups.available")
    return DiscoveryResult(
        upstream_type="sub2api",
        source="configured",
        status=status,
        groups=[option] if status == "ok" else [],
        matched_group=option if status == "ok" and group is not None else None,
        discovered_group_multiplier=group if status == "ok" else None,
        discovered_group_multiplier_source="groups.available" if group is not None else None,
        discovered_recharge_multiplier=recharge if status == "ok" else None,
        discovered_recharge_multiplier_source="payment.checkout-info" if recharge is not None else None,
        recharge_discovery_status=(
            "error" if status != "ok" else ("ok" if recharge is not None else "missing")
        ),
        message="safe discovery result",
    )


class UpstreamAccountServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.sub2api = FakeSub2Api()
        self.service = UpstreamAccountService(self.sub2api)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    async def _manage(self, **overrides: object):
        values: dict[str, object] = {
            "base_url": "https://upstream.example.com",
            "upstream_type": "sub2api",
        }
        values.update(overrides)
        return await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(**values),
        )

    async def _config(self) -> UpstreamAccountConfig:
        result = await self.db.execute(
            select(UpstreamAccountConfig).where(UpstreamAccountConfig.sub2api_account_id == 7)
        )
        return result.scalar_one()

    async def test_list_includes_unmanaged_remote_api_key_account(self) -> None:
        accounts = await self.service.list_accounts(self.db)

        self.assertEqual(len(accounts), 1)
        self.assertFalse(accounts[0].managed)
        self.assertEqual(accounts[0].base_url, "https://upstream.example.com")
        self.assertEqual(accounts[0].current_rate, 0.8)

    async def test_explicit_channel_unassignment_survives_overview_roundtrip(self) -> None:
        channel_service = UpstreamChannelService(accounts=self.service)
        initial = await channel_service.overview(self.db)
        channel_id = initial.channels[0].id

        detached = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(channel_id=None),
        )
        roundtrip = await channel_service.overview(self.db)
        stored = await self._config()

        self.assertIsNone(detached.channel_id)
        self.assertEqual(detached.base_url, "https://upstream.example.com")
        self.assertTrue(stored.channel_auto_assign_disabled)
        self.assertEqual(roundtrip.channels, [])
        self.assertEqual(
            [item.sub2api_account_id for item in roundtrip.unassigned_accounts],
            [7],
        )

        await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(channel_id=channel_id),
        )
        rebound = await channel_service.overview(self.db)
        stored = await self._config()
        self.assertFalse(stored.channel_auto_assign_disabled)
        self.assertEqual([item.sub2api_account_id for item in rebound.channels[0].accounts], [7])

    async def test_discover_unmanaged_account_opts_in_and_reads_balance_without_key(self) -> None:
        account = await self.service.discover_account(self.db, 7)

        self.assertTrue(account.managed)
        self.assertEqual(account.base_url, "https://upstream.example.com")
        self.assertEqual(account.balance_status, "ok")
        self.assertEqual(account.balance_remaining, 12500.25)
        self.assertEqual(account.group_multiplier_status, "credentials_missing")
        self.assertEqual(account.recharge_multiplier_status, "credentials_missing")
        self.assertIsNone(account.target_rate)
        self.assertIsNotNone(await self._config())

    async def test_discover_all_opts_in_unmanaged_accounts_for_balance(self) -> None:
        result = await self.service.discover_all(self.db)

        self.assertEqual(result.total, 1)
        self.assertEqual(result.succeeded, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(result.accounts), 1)
        self.assertTrue(result.accounts[0].managed)
        self.assertEqual(result.accounts[0].balance_status, "ok")

    async def test_upsert_encrypts_and_blank_api_key_preserves_ciphertext(self) -> None:
        secret = "sk-live-super-secret-value"
        created = await self._manage(api_key=secret)
        config = await self._config()
        original_ciphertext = config.encrypted_api_key

        self.assertIsNotNone(original_ciphertext)
        self.assertNotIn(secret, original_ciphertext or "")
        self.assertIsNone(created.api_key_hint)
        self.assertNotIn("encrypted_api_key", created.model_dump())

        updated = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(api_key="   ", manual_group_multiplier=2),
        )
        config = await self._config()
        self.assertEqual(config.encrypted_api_key, original_ciphertext)
        self.assertTrue(updated.api_key_set)
        self.assertEqual(updated.manual_group_multiplier, 2)

    async def test_production_default_encryption_key_rejects_new_secret(self) -> None:
        settings = SimpleNamespace(app_env="production", app_encryption_key=DEFAULT_ENCRYPTION_KEY)
        with patch("app.services.upstream_accounts.get_settings", return_value=settings):
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self._manage(api_key="must-not-be-saved")

        self.assertEqual(context.exception.status_code, 503)
        result = await self.db.execute(select(UpstreamAccountConfig))
        self.assertIsNone(result.scalar_one_or_none())

    async def test_oversized_secret_is_rejected_without_echoing_it(self) -> None:
        secret = "sk-" + ("sensitive" * 600)

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self._manage(api_key=secret)

        self.assertEqual(context.exception.status_code, 422)
        self.assertNotIn(secret, str(context.exception))
        result = await self.db.execute(select(UpstreamAccountConfig))
        self.assertIsNone(result.scalar_one_or_none())

    async def test_discover_is_dry_run_and_rounds_half_up_to_four_places(self) -> None:
        await self._manage(api_key="sk-discovery")
        self.sub2api.local_info = (1.0, True)
        discovered = discovery_result(group=1.00005, recharge=1.0)

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovered),
        ) as call:
            account = await self.service.discover_account(self.db, 7)

        self.assertEqual(account.target_rate, 1.0001)
        self.assertEqual(account.group_multiplier_source, "groups.available")
        self.assertEqual(account.recharge_multiplier_source, "payment.checkout-info")
        self.assertEqual(account.local_recharge_source, "sub2api_settings")
        self.assertEqual(account.balance_remaining, 12500.25)
        self.assertTrue(account.would_change)
        self.assertEqual(self.sub2api.update_calls, [])
        call.assert_awaited_once()
        self.assertEqual(call.await_args.kwargs["api_key"], "sk-discovery")

    async def test_recharge_cost_uses_cny_paid_per_usd_credit(self) -> None:
        await self._manage(api_key="sk-cost-direction")
        # sub2api stores USD credited per CNY paid, so 10 means CNY 0.1 per USD.
        self.sub2api.local_info = (10.0, True)

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=1.0, recharge=0.1)),
        ):
            account = await self.service.discover_account(self.db, 7)

        self.assertEqual(account.local_recharge_multiplier, 0.1)
        self.assertEqual(account.effective_recharge_multiplier, 0.1)
        self.assertEqual(account.target_rate, 1.0)
        self.assertAlmostEqual(
            (account.target_rate or 0) * (account.local_recharge_multiplier or 0),
            (account.effective_group_multiplier or 0)
            * (account.effective_recharge_multiplier or 0),
            places=4,
        )

    async def test_auto_configuration_exposes_last_resolved_upstream_type(self) -> None:
        await self._manage(api_key="sk-auto-type", upstream_type="auto")
        discovered = discovery_result(group=1.0, recharge=1.0)
        discovered.upstream_type = "newapi"

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovered),
        ):
            account = await self.service.discover_account(self.db, 7)

        self.assertEqual(account.upstream_type, "auto")
        self.assertEqual(account.resolved_upstream_type, "newapi")

    async def test_configuration_change_immediately_invalidates_old_preview(self) -> None:
        await self._manage(api_key="sk-preview")
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=2.0, recharge=1.0)),
        ):
            preview = await self.service.discover_account(self.db, 7)
        self.assertEqual(preview.target_rate, 2.0)
        self.assertTrue(preview.group_options)

        changed = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(manual_group_multiplier=3.0),
        )

        self.assertIsNone(changed.target_rate)
        self.assertIsNone(changed.effective_group_multiplier)
        self.assertIsNone(changed.effective_recharge_multiplier)
        self.assertEqual(changed.group_options, [])
        self.assertEqual(changed.group_multiplier_status, "not_discovered")
        self.assertEqual(changed.recharge_multiplier_status, "not_discovered")
        self.assertIsNone(changed.last_discovered_at)

    async def test_identity_changes_clear_old_group_but_same_values_and_new_selection_survive(self) -> None:
        created = await self._manage(
            base_url="https://upstream.example.com",
            upstream_type="sub2api",
            api_key="sk-original",
            access_token="access-original",
            selected_group_id="gold",
            selected_group_name="Gold",
        )
        self.assertEqual((created.selected_group_id, created.selected_group_name), ("gold", "Gold"))

        same = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(
                base_url="https://upstream.example.com",
                upstream_type="sub2api",
                api_key="sk-original",
                access_token="access-original",
            ),
        )
        self.assertEqual((same.selected_group_id, same.selected_group_name), ("gold", "Gold"))

        changed_base = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(
                base_url="https://new-upstream.example.com",
                confirm_credential_rebind=True,
            ),
        )
        self.assertIsNone(changed_base.selected_group_id)
        self.assertIsNone(changed_base.selected_group_name)

        await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(selected_group_id="silver", selected_group_name="Silver"),
        )
        changed_type = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(upstream_type="newapi"),
        )
        self.assertIsNone(changed_type.selected_group_id)
        self.assertIsNone(changed_type.selected_group_name)

        await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(selected_group_id="bronze", selected_group_name="Bronze"),
        )
        changed_key = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(api_key="sk-replaced"),
        )
        self.assertIsNone(changed_key.selected_group_id)
        self.assertIsNone(changed_key.selected_group_name)

        await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(selected_group_id="vip", selected_group_name="VIP"),
        )
        changed_token = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(access_token="access-replaced"),
        )
        self.assertIsNone(changed_token.selected_group_id)
        self.assertIsNone(changed_token.selected_group_name)

        explicit_new = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(
                api_key="sk-third",
                selected_group_id="new-group",
                selected_group_name="New Group",
            ),
        )
        self.assertEqual(
            (explicit_new.selected_group_id, explicit_new.selected_group_name),
            ("new-group", "New Group"),
        )

    async def test_origin_change_requires_explicit_credential_rebind_confirmation(self) -> None:
        await self._manage(
            base_url="https://upstream.example.com",
            api_key="sk-origin-bound",
        )

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.upsert_account(
                self.db,
                7,
                UpstreamAccountUpdate(base_url="https://replacement.example.com"),
            )

        self.assertEqual(context.exception.status_code, 409)
        await self.db.rollback()
        config = await self._config()
        self.assertEqual(config.base_url, "https://upstream.example.com")

    async def test_default_recharge_has_null_discovered_value_and_explicit_status(self) -> None:
        await self._manage(api_key="sk-default")
        self.sub2api.local_info = (1.0, False)
        discovered = discovery_result(group=2.0, recharge=None)

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovered),
        ):
            account = await self.service.discover_account(self.db, 7)

        self.assertIsNone(account.discovered_recharge_multiplier)
        self.assertEqual(account.effective_recharge_multiplier, 1.0)
        self.assertEqual(account.recharge_multiplier_source, "default")
        self.assertEqual(account.recharge_multiplier_status, "default_missing")
        self.assertEqual(account.local_recharge_source, "default")
        self.assertEqual(account.local_recharge_status, "default_missing")
        self.assertEqual(account.target_rate, 2.0)

    async def test_failed_discovery_clears_target_instead_of_defaulting_recharge(self) -> None:
        await self._manage(api_key="sk-failure", manual_group_multiplier=2.0)
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=2.0, recharge=1.0)),
        ):
            first = await self.service.discover_account(self.db, 7)
        self.assertEqual(first.target_rate, 2.0)

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=None, recharge=None, status="error")),
        ):
            failed = await self.service.discover_account(self.db, 7)
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self.service.apply_account(self.db, 7, first.target_rate or 0)

        self.assertIsNone(failed.effective_recharge_multiplier)
        self.assertEqual(failed.recharge_multiplier_status, "discovery_failed")
        self.assertIsNone(failed.target_rate)
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_invalid_recharge_value_cannot_create_or_apply_target(self) -> None:
        await self._manage(api_key="sk-invalid-recharge", manual_group_multiplier=2.0)
        invalid = discovery_result(group=2.0, recharge=None)
        invalid.discovered_recharge_multiplier = "not-a-number"  # type: ignore[assignment]

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=invalid),
        ):
            preview = await self.service.discover_account(self.db, 7)
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self.service.apply_account(self.db, 7, 2.0)

        self.assertIsNone(preview.target_rate)
        self.assertEqual(preview.recharge_multiplier_status, "invalid")
        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_failed_recharge_endpoint_with_valid_groups_cannot_default(self) -> None:
        await self._manage(api_key="sk-partial-failure")
        partial = discovery_result(group=2.0, recharge=None)
        partial.recharge_discovery_status = "error"

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=partial),
        ):
            account = await self.service.discover_account(self.db, 7)

        self.assertEqual(account.effective_group_multiplier, 2.0)
        self.assertIsNone(account.effective_recharge_multiplier)
        self.assertEqual(account.recharge_multiplier_status, "discovery_failed")
        self.assertIsNone(account.target_rate)

    async def test_extreme_multiplier_product_is_unavailable_instead_of_raising_decimal_error(self) -> None:
        await self._manage(
            manual_group_multiplier=1000,
            manual_recharge_multiplier=1e-300,
        )

        preview = await self.service.discover_account(self.db, 7)
        self.assertIsNone(preview.target_rate)
        self.assertIn("outside the supported range", preview.last_error or "")
        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.apply_account(self.db, 7, 1.0)

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_positive_ratio_that_rounds_to_zero_is_not_applicable(self) -> None:
        await self._manage(
            manual_group_multiplier=1e-300,
            manual_recharge_multiplier=1000,
        )

        preview = await self.service.discover_account(self.db, 7)

        self.assertIsNone(preview.target_rate)
        self.assertIn("outside the supported range", preview.last_error or "")
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_insecure_direct_discovery_is_rejected_with_422(self) -> None:
        with self.assertRaises(ValueError):
            UpstreamAccountUpdate(base_url="http://upstream.example.com", api_key="sk-no-http")
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_manual_group_works_without_upstream_credentials(self) -> None:
        await self._manage(manual_group_multiplier=3.0, manual_recharge_multiplier=1.5)

        account = await self.service.discover_account(self.db, 7)

        self.assertEqual(account.group_multiplier_source, "manual")
        self.assertEqual(account.recharge_multiplier_source, "manual")
        self.assertEqual(account.target_rate, 4.5)

    async def test_balance_failure_preserves_last_successful_values(self) -> None:
        secret = "sk-never-leak-balance"
        await self._manage(manual_group_multiplier=1.0)
        first = await self.service.discover_account(self.db, 7)
        self.assertEqual(first.balance_remaining, 12500.25)
        self.assertIsNotNone(first.balance_checked_at)

        self.sub2api.balance_error = Sub2ApiRequestError(f"HTTP body contained {secret}")
        second = await self.service.discover_account(self.db, 7)

        self.assertEqual(second.balance_remaining, 12500.25)
        self.assertEqual(second.balance_checked_at, first.balance_checked_at)
        self.assertEqual(second.balance_status, "error")
        serialized = str(second.model_dump())
        self.assertNotIn(secret, serialized)
        self.assertIn("Balance check failed.", second.last_error or "")

    async def test_negative_remaining_balance_is_preserved_as_valid_data(self) -> None:
        await self._manage(manual_group_multiplier=1.0)
        self.sub2api.balance_result["remaining"] = -40.904106

        account = await self.service.discover_account(self.db, 7)

        self.assertEqual(account.balance_status, "ok")
        self.assertAlmostEqual(account.balance_remaining or 0, -40.904106)

    async def test_invalid_present_balance_number_preserves_last_successful_snapshot(self) -> None:
        await self._manage(manual_group_multiplier=1.0, manual_recharge_multiplier=1.0)
        first = await self.service.discover_account(self.db, 7)
        self.assertEqual(first.balance_status, "ok")
        old_snapshot = (
            first.balance_remaining,
            first.balance_total,
            first.balance_used,
            first.balance_checked_at,
        )

        self.sub2api.balance_result = {
            "status": "ok",
            "message": "claims success with malformed data",
            "remaining": "not-finite",
            "total": 99999,
            "used": 1,
            "checked_at": "2026-07-14T12:00:00Z",
        }
        second = await self.service.discover_account(self.db, 7)

        self.assertEqual(second.balance_status, "error")
        self.assertEqual(
            (
                second.balance_remaining,
                second.balance_total,
                second.balance_used,
                second.balance_checked_at,
            ),
            old_snapshot,
        )

    async def test_upstream_group_text_is_scrubbed_before_persistence_and_response(self) -> None:
        api_key = "sk-persisted-reflection"
        access_token = "access-persisted-reflection"
        self.sub2api.accounts[0]["name"] = f"remote {api_key}"
        self.sub2api.balance_result["message"] = f"message {access_token}"
        self.sub2api.balance_result["unit"] = f"USD-{api_key}"
        await self._manage(api_key=api_key, access_token=access_token)
        option = GroupOption(
            id=api_key,
            name=f"name {access_token}",
            multiplier=2.0,
            description=f"description {api_key}",
            source="groups.available",
        )
        reflected = DiscoveryResult(
            upstream_type="sub2api",
            source="configured",
            status="ok",
            groups=[option],
            matched_group=option,
            discovered_group_multiplier=2.0,
            discovered_group_multiplier_source="groups.available",
            discovered_recharge_multiplier=1.0,
            discovered_recharge_multiplier_source="payment.checkout-info",
            message=f"message {api_key} {access_token}",
        )

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=reflected),
        ):
            account = await self.service.discover_account(self.db, 7)

        config = await self._config()
        persisted = str(
            {
                "group_options": config.group_options,
                "selected_group_id": config.selected_group_id,
                "selected_group_name": config.selected_group_name,
                "remote_name": config.remote_name,
                "balance_message": config.balance_message,
                "balance_unit": config.balance_unit,
            }
        )
        serialized = str(account.model_dump())
        self.assertNotIn(api_key, persisted)
        self.assertNotIn(access_token, persisted)
        self.assertNotIn(api_key, serialized)
        self.assertNotIn(access_token, serialized)

    async def test_upstream_failure_preserves_options_and_does_not_leak_secret(self) -> None:
        secret = "sk-top-secret-discovery"
        await self._manage(api_key=secret, manual_group_multiplier=4.0)
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=2.0, recharge=2.0)),
        ):
            first = await self.service.discover_account(self.db, 7)
        self.assertEqual(len(first.group_options), 1)

        self.sub2api.balance_error = Sub2ApiRequestError(f"unsafe {secret}")
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(side_effect=RuntimeError(f"unsafe {secret}")),
        ):
            second = await self.service.discover_account(self.db, 7)

        self.assertEqual(len(second.group_options), 1)
        self.assertEqual(second.group_multiplier_source, "manual")
        self.assertEqual(second.group_multiplier_status, "fallback_manual")
        self.assertNotIn(secret, str(second.model_dump()))

    async def test_apply_re_discovers_and_rejects_stale_confirmation(self) -> None:
        await self._manage(api_key="sk-stale")
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=1.0, recharge=1.0)),
        ):
            initial = await self.service.discover_account(self.db, 7)

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=2.0, recharge=1.0)),
        ):
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self.service.apply_account(self.db, 7, initial.target_rate or 0)

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_apply_writes_reads_back_and_sets_last_applied(self) -> None:
        await self._manage(api_key="sk-apply")
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=1.11115, recharge=1.0)),
        ):
            account = await self.service.apply_account(self.db, 7, 1.1112)

        self.assertEqual(self.sub2api.update_calls, [(7, 1.1112)])
        self.assertEqual(account.current_rate, 1.1112)
        self.assertFalse(account.would_change)
        self.assertIsNotNone(account.last_applied_at)

    async def test_clear_access_token_only_when_explicitly_requested(self) -> None:
        await self._manage(access_token="access-token-secret")
        first = await self._config()
        original_ciphertext = first.encrypted_access_token

        await self.service.upsert_account(self.db, 7, UpstreamAccountUpdate(access_token=""))
        preserved = await self._config()
        self.assertEqual(preserved.encrypted_access_token, original_ciphertext)

        cleared = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(clear_access_token=True),
        )
        self.assertFalse(cleared.access_token_set)
        self.assertIsNone((await self._config()).encrypted_access_token)

    async def test_delete_removes_only_local_config_without_remote_call(self) -> None:
        await self._manage(api_key="sk-local-only")
        self.sub2api.list_api_key_accounts = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("remote delete/list must not be called")
        )

        removed = await self.service.delete_account(self.db, 7)

        self.assertTrue(removed)
        result = await self.db.execute(select(UpstreamAccountConfig))
        self.assertIsNone(result.scalar_one_or_none())
        self.sub2api.list_api_key_accounts.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_disable_updates_and_verifies_real_sub2api_account(self) -> None:
        await self._manage(api_key="sk-disable-test")

        account = await self.service.set_account_enabled(self.db, 7, False)

        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        self.assertFalse(account.remote_schedulable)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])

    async def test_remote_delete_removes_sub2api_account_and_local_config(self) -> None:
        await self._manage(api_key="sk-delete-test")

        deleted = await self.service.delete_remote_account(self.db, 7)

        self.assertTrue(deleted)
        self.assertEqual(self.sub2api.delete_calls, [7])
        self.assertEqual(self.sub2api.accounts, [])
        result = await self.db.execute(select(UpstreamAccountConfig))
        self.assertIsNone(result.scalar_one_or_none())

    async def test_apply_transport_failure_never_sets_last_applied_or_leaks_error(self) -> None:
        secret = "sk-transport-error-secret"
        await self._manage(api_key="sk-apply-failure")
        self.sub2api.update_account_rate_multiplier = AsyncMock(  # type: ignore[method-assign]
            side_effect=Sub2ApiRequestError(f"unsafe response {secret}")
        )

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=1.2, recharge=1.0)),
        ):
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self.service.apply_account(self.db, 7, 1.2)

        self.assertEqual(context.exception.status_code, 502)
        config = await self._config()
        self.assertIsNone(config.last_applied_at)
        self.assertNotIn(secret, config.last_error or "")
        self.assertNotIn(secret, str(context.exception))


class Sub2ApiKeyManagementClientTests(unittest.IsolatedAsyncioTestCase):
    def _runtime_patch(self):
        config = SimpleNamespace(accounts_path="/admin/accounts")
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=config))
        return config, patch("app.services.sub2api.get_runtime_config_service", return_value=runtime)

    async def test_bulk_rate_update_uses_exact_single_numeric_id_payload(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(return_value={})  # type: ignore[method-assign]
        config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            await client.update_account_rate_multiplier(17, 1.2346)

        client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "POST",
            "/admin/accounts/bulk-update",
            config=config,
            json={"account_ids": [17], "rate_multiplier": 1.2346},
        )

        with self.assertRaises(ValueError):
            await client.update_account_rate_multiplier(17.5, 1.0)  # type: ignore[arg-type]

    async def test_export_maps_reordered_rows_by_explicit_account_id(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": [
                    {"id": 18, "credentials": {"api_key": "sk-eighteen-private"}},
                    {"id": 17, "credentials": {"api_key": "sk-seventeen-private"}},
                ]
            }
        )
        _config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            exported = await client.export_api_key_secrets([17, 18])

        self.assertEqual(
            exported,
            {17: "sk-seventeen-private", 18: "sk-eighteen-private"},
        )

    async def test_export_without_explicit_ids_fails_closed(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": [
                    {"credentials": {"api_key": "sk-first-private"}},
                    {"credentials": {"api_key": "sk-second-private"}},
                ]
            }
        )
        _config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            exported = await client.export_api_key_secrets([17, 18])

        self.assertEqual(exported, {})

    async def test_account_pagination_rejects_excessive_page_counts(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": {
                    "items": [],
                    "pages": MAX_SUB2API_ACCOUNT_PAGES + 1,
                }
            }
        )
        _config, runtime_patch = self._runtime_patch()
        with runtime_patch, self.assertRaises(Sub2ApiRequestError):
            await client.list_accounts()

        self.assertEqual(client._request.await_count, 1)  # type: ignore[attr-defined]

    async def test_sub2api_response_body_is_streamed_with_a_hard_limit(self) -> None:
        secret = "response-must-not-be-buffered"

        def handler(_request):
            return httpx.Response(
                200,
                content=(secret.encode("utf-8") + b"x" * MAX_SUB2API_RESPONSE_BYTES),
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        config = SimpleNamespace(
            base_url="https://sub2api.example/api/v1",
            auth_token="",
            auth_scheme="",
            auth_header="x-api-key",
        )

        with self.assertRaises(Sub2ApiRequestError) as context:
            await client._request("GET", "/admin/accounts", config=config)

        self.assertIn("exceeded", str(context.exception))
        self.assertNotIn(secret, str(context.exception))

    async def test_sub2api_accepts_multi_megabyte_account_pages_below_limit(self) -> None:
        body = (
            b'{"data":{"items":[{"id":7,"padding":"'
            + b"x" * (2 * 1024 * 1024)
            + b'"}]}}'
        )

        client = Sub2ApiClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=body)
            )
        )
        config = SimpleNamespace(
            base_url="https://sub2api.example/api/v1",
            auth_token="",
            auth_scheme="",
            auth_header="x-api-key",
        )

        payload = await client._request("GET", "/admin/accounts", config=config)

        self.assertEqual(payload["data"]["items"][0]["id"], 7)

    async def test_schedulable_update_uses_account_specific_endpoint(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(return_value={})  # type: ignore[method-assign]
        config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            await client.set_account_schedulable(17, False)

        client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "POST",
            "/admin/accounts/17/schedulable",
            config=config,
            json={"schedulable": False},
        )

    async def test_settings_default_only_when_field_is_missing(self) -> None:
        client = Sub2ApiClient()
        config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            client._request = AsyncMock(return_value={"code": 0, "data": {}})  # type: ignore[method-assign]
            value, present = await client.get_payment_balance_recharge_multiplier_info()
            self.assertEqual((value, present), (1.0, False))

            client._request = AsyncMock(  # type: ignore[method-assign]
                return_value={"data": {"payment_balance_recharge_multiplier": "2.5"}}
            )
            value, present = await client.get_payment_balance_recharge_multiplier_info()
            self.assertEqual((value, present), (2.5, True))

            client._request = AsyncMock(  # type: ignore[method-assign]
                return_value={"data": {"payment_balance_recharge_multiplier": "invalid"}}
            )
            with self.assertRaises(Sub2ApiRequestError):
                await client.get_payment_balance_recharge_multiplier_info()

            expected = Sub2ApiRequestError("authentication failed", status_code=401)
            client._request = AsyncMock(side_effect=expected)  # type: ignore[method-assign]
            with self.assertRaises(Sub2ApiRequestError) as context:
                await client.get_payment_balance_recharge_multiplier()
            self.assertIs(context.exception, expected)

        self.assertEqual(config.accounts_path, "/admin/accounts")

    async def test_api_key_account_filter_accepts_supported_spellings_only(self) -> None:
        client = Sub2ApiClient()
        client.list_accounts = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {"id": 1, "type": "apikey"},
                {"id": 2, "type": "api_key"},
                {"id": 3, "type": "api-key"},
                {"id": 4, "type": "oauth"},
            ]
        )

        result = await client.list_api_key_accounts()
        self.assertEqual([item["id"] for item in result], [1, 2, 3])


class UpstreamAccountAuthenticationTests(unittest.TestCase):
    def test_every_route_requires_admin_session(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_account_service] = lambda: SimpleNamespace()
        requests = [
            ("GET", "/api/upstream-accounts", None),
            ("GET", "/api/upstream-accounts/rate-change-logs", None),
            ("POST", "/api/upstream-accounts/discover-all", None),
            ("PUT", "/api/upstream-accounts/7", {}),
            ("DELETE", "/api/upstream-accounts/7", None),
            ("PATCH", "/api/upstream-accounts/7/enabled", {"enabled": False}),
            ("DELETE", "/api/upstream-accounts/7/remote", {"confirmed_account_id": 7}),
            ("POST", "/api/upstream-accounts/7/discover", None),
            ("POST", "/api/upstream-accounts/7/apply", {"confirmed_target_rate": 1}),
        ]

        with TestClient(app) as client:
            for method, url, body in requests:
                response = client.request(method, url, json=body)
                self.assertEqual(response.status_code, 401, (method, url, response.text))

    def test_oversized_secret_422_response_does_not_echo_input(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        secret = "sk-" + ("sensitive" * 600)
        service = SimpleNamespace(
            upsert_account=AsyncMock(
                side_effect=UpstreamAccountServiceError("The API key is too long.", status_code=422)
            )
        )

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_account_service] = lambda: service
        with TestClient(app) as client:
            response = client.put("/api/upstream-accounts/7", json={"api_key": secret})

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        self.assertEqual(response.json(), {"detail": "The API key is too long."})

    def test_unrelated_schema_error_does_not_echo_valid_secret_field(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        secret = "sk-valid-but-private-secret"
        service = SimpleNamespace(upsert_account=AsyncMock())

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_account_service] = lambda: service
        with TestClient(app) as client:
            response = client.put(
                "/api/upstream-accounts/7",
                json={"api_key": secret, "base_url": "file:///private/path"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)
        service.upsert_account.assert_not_awaited()

    def test_validation_errors_redact_malformed_credentials_and_secret_url(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)
        app.include_router(router, prefix="/api/upstream-accounts")
        api_secret = "sk-malformed-list-secret"
        access_secret = "access-malformed-dict-secret"
        url_secret = "url-userinfo-query-secret"
        service = SimpleNamespace(upsert_account=AsyncMock())

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_account_service] = lambda: service
        with TestClient(app) as client:
            response = client.put(
                "/api/upstream-accounts/7",
                json={
                    "api_key": [api_secret],
                    "access_token": {"value": access_secret},
                    "base_url": f"https://user:{url_secret}@example.com/path?token={url_secret}",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn(api_secret, response.text)
        self.assertNotIn(access_secret, response.text)
        self.assertNotIn(url_secret, response.text)
        self.assertIn("[redacted]", response.text)
        service.upsert_account.assert_not_awaited()

    def test_validation_handler_preserves_non_sensitive_error_input(self) -> None:
        app = FastAPI()
        app.add_exception_handler(RequestValidationError, sanitized_request_validation_handler)
        app.include_router(router, prefix="/api/upstream-accounts")
        service = SimpleNamespace(upsert_account=AsyncMock())

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_account_service] = lambda: service
        with TestClient(app) as client:
            response = client.put(
                "/api/upstream-accounts/7",
                json={"manual_group_multiplier": -1},
            )

        self.assertEqual(response.status_code, 422)
        self.assertTrue(
            any(item.get("input") == -1 for item in response.json().get("detail", []))
        )
        service.upsert_account.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
