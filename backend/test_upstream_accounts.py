from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

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
from app.models import UpstreamAccountConfig, UpstreamRateChangeLog
from app.schemas import UpstreamAccountOut, UpstreamAccountUpdate
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
from app.services.upstream_client import AccountUpstreamState, DiscoveryResult, GroupOption


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
                "created_at": "2026-07-01T00:00:00Z",
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
        self.name_update_calls: list[tuple[int, str]] = []
        self.schedulable_calls: list[tuple[int, bool]] = []
        self.delete_calls: list[int] = []
        self.balance_calls: list[int] = []

    async def list_api_key_accounts(self) -> list[dict]:
        return self.accounts

    async def get_account_balance(self, account: dict | str | int) -> dict:
        self.balance_calls.append(int(account if not isinstance(account, dict) else account["id"]))
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

    async def update_account_name(
        self,
        account_id: str | int,
        name: str,
        *,
        validate_current=None,
    ) -> dict:
        parsed_id = int(account_id)
        account = await self.get_account_by_id(parsed_id)
        if account is None:
            raise Sub2ApiRequestError("sub2api account was not found.", status_code=404)
        if validate_current is not None:
            validate_current(account)
        self.name_update_calls.append((parsed_id, name))
        account["name"] = name
        return account

    async def get_account_by_id(self, account_id: str | int, **_kwargs) -> dict | None:
        return await self.get_account(str(account_id))

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
    account_state: AccountUpstreamState | None = None,
) -> DiscoveryResult:
    option = GroupOption(id="gold", name="Gold", multiplier=group or 1.0, source="groups.available")
    return DiscoveryResult(
        upstream_type="sub2api",
        source="configured",
        status=status,
        groups=[option] if status == "ok" else [],
        matched_group=option if status == "ok" and group is not None else None,
        matched_account_state=account_state,
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
            self._update(**values),
        )

    def _update(self, **values: object) -> UpstreamAccountUpdate:
        return UpstreamAccountUpdate(
            expected_identity_fingerprint=self.service._remote_identity_fingerprint(
                self.sub2api.accounts[0]
            ),
            **values,
        )

    async def _config(self) -> UpstreamAccountConfig:
        result = await self.db.execute(
            select(UpstreamAccountConfig).where(UpstreamAccountConfig.sub2api_account_id == 7)
        )
        return result.scalar_one()

    async def _fingerprint(self) -> str:
        return (await self.service.list_accounts(self.db))[0].identity_fingerprint

    async def _discover(self):
        return await self.service.discover_account(
            self.db,
            7,
            await self._fingerprint(),
        )

    async def test_list_includes_unmanaged_remote_api_key_account(self) -> None:
        accounts = await self.service.list_accounts(self.db)

        self.assertEqual(len(accounts), 1)
        self.assertFalse(accounts[0].managed)
        self.assertEqual(accounts[0].base_url, "https://upstream.example.com")
        self.assertEqual(accounts[0].current_rate, 0.8)
        self.assertRegex(accounts[0].identity_fingerprint, r"^[0-9a-f]{64}$")

    async def test_single_account_discovery_uses_two_confirmations_and_never_auto_recovers(self) -> None:
        self.sub2api.accounts[0]["schedulable"] = True
        await self._manage(api_key="sk-managed-health-key")
        runtime = SimpleNamespace(
            get_api_key_auto_disable_on_upstream_unavailable=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
            get_upstream_priority_sync_enabled=AsyncMock(return_value=False),
        )
        disabled = discovery_result(
            group=1.0,
            recharge=1.0,
            account_state=AccountUpstreamState(
                key_status="disabled",
                group_status="available",
                group_id="gold",
                group_name="Gold",
            ),
        )
        with (
            patch(
                "app.services.upstream_accounts.get_runtime_config_service",
                return_value=runtime,
            ),
            patch(
                "app.services.upstream_accounts.discover_upstream",
                new=AsyncMock(return_value=disabled),
            ),
        ):
            await self._discover()
            first = await self._config()
            self.assertEqual(first.upstream_health_invalid_count, 1)
            self.assertTrue(self.sub2api.accounts[0]["schedulable"])
            await self._discover()

        config = await self._config()
        self.assertEqual(config.upstream_health_invalid_count, 2)
        self.assertEqual(config.auto_disabled_reason, "upstream_key_unavailable")
        self.assertIsNotNone(config.last_auto_disabled_at)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        disabled_log = await self.db.scalar(
            select(UpstreamRateChangeLog).where(
                UpstreamRateChangeLog.sub2api_account_id == 7,
                UpstreamRateChangeLog.status == "account_disabled",
            )
        )
        self.assertIsNotNone(disabled_log)

        recovered = discovery_result(
            group=1.0,
            recharge=1.0,
            account_state=AccountUpstreamState(
                key_status="active",
                group_status="available",
                group_id="gold",
                group_name="Gold",
            ),
        )
        with (
            patch(
                "app.services.upstream_accounts.get_runtime_config_service",
                return_value=runtime,
            ),
            patch(
                "app.services.upstream_accounts.discover_upstream",
                new=AsyncMock(return_value=recovered),
            ),
        ):
            await self._discover()

        config = await self._config()
        self.assertEqual(config.upstream_key_status, "active")
        self.assertEqual(config.upstream_health_invalid_count, 0)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])
        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])

        await self.service.set_account_enabled(
            self.db,
            7,
            True,
            await self._fingerprint(),
        )
        config = await self._config()
        self.assertIsNone(config.auto_disabled_reason)
        self.assertIsNone(config.last_auto_disabled_at)

    async def test_discovery_priority_rebalance_respects_runtime_switch(self) -> None:
        await self._manage(api_key="sk-priority-switch")
        priority_service = self.service._priority_service()
        runtime = SimpleNamespace(
            get_api_key_auto_disable_on_upstream_unavailable=AsyncMock(return_value=False),
            get_automation_paused=AsyncMock(return_value=False),
            get_upstream_priority_sync_enabled=AsyncMock(return_value=False),
        )
        with (
            patch(
                "app.services.upstream_accounts.get_runtime_config_service",
                return_value=runtime,
            ),
            patch(
                "app.services.upstream_accounts.discover_upstream",
                new=AsyncMock(return_value=discovery_result(group=1.0, recharge=1.0)),
            ),
            patch.object(
                priority_service,
                "rebalance",
                new=AsyncMock(),
            ) as rebalance,
        ):
            await self._discover()

        rebalance.assert_not_awaited()

    async def test_successful_discovery_clears_stale_usage_when_key_match_is_missing(self) -> None:
        await self._manage(api_key="sk-managed-usage")
        config = await self._config()
        config.upstream_usage_amount = 12.375
        config.upstream_usage_unit = "USD"
        config.upstream_usage_checked_at = config.created_at
        await self.db.commit()

        result = discovery_result(
            group=1.0,
            recharge=1.0,
        )
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=result),
        ):
            discovered = await self._discover()

        self.assertIsNone(discovered.upstream_usage_amount)
        self.assertIsNone(discovered.upstream_usage_unit)
        self.assertIsNone(discovered.upstream_usage_checked_at)
        stored = await self._config()
        self.assertIsNone(stored.upstream_usage_amount)
        self.assertIsNone(stored.upstream_usage_unit)
        self.assertIsNone(stored.upstream_usage_checked_at)

    async def test_duplicate_valid_remote_ids_fail_closed_for_list_and_point_lookup(self) -> None:
        duplicate = dict(self.sub2api.accounts[0])
        duplicate["name"] = "different-account-with-same-id"
        duplicate["credentials"] = {"base_url": "https://different.example/v1"}
        self.sub2api.accounts.append(duplicate)

        with self.assertRaises(UpstreamAccountServiceError) as list_context:
            await self.service.list_accounts(self.db)
        with self.assertRaises(UpstreamAccountServiceError) as point_context:
            await self.service.upsert_account(
                self.db,
                7,
                self._update(base_url="https://upstream.example.com"),
            )

        self.assertEqual(list_context.exception.status_code, 502)
        self.assertEqual(point_context.exception.status_code, 502)

    async def test_identity_fingerprint_ignores_mutable_state_but_changes_with_identity(self) -> None:
        first = (await self.service.list_accounts(self.db))[0]
        self.sub2api.accounts[0]["rate_multiplier"] = 9.5
        self.sub2api.accounts[0]["status"] = "error"
        self.sub2api.accounts[0]["schedulable"] = False
        state_changed = (await self.service.list_accounts(self.db))[0]
        self.sub2api.accounts[0]["name"] = "replacement-account"
        identity_changed = (await self.service.list_accounts(self.db))[0]

        self.assertEqual(first.identity_fingerprint, state_changed.identity_fingerprint)
        self.assertNotEqual(first.identity_fingerprint, identity_changed.identity_fingerprint)

    async def test_remote_mutations_reject_stale_identity_fingerprint(self) -> None:
        await self._manage(api_key="sk-stale-identity")
        displayed = (await self.service.list_accounts(self.db))[0]
        self.sub2api.accounts[0]["name"] = "replacement-account"

        with self.assertRaises(UpstreamAccountServiceError) as enabled_context:
            await self.service.set_account_enabled(
                self.db,
                7,
                False,
                displayed.identity_fingerprint,
            )
        with self.assertRaises(UpstreamAccountServiceError) as delete_context:
            await self.service.delete_remote_account(
                self.db,
                7,
                displayed.identity_fingerprint,
            )
        with self.assertRaises(UpstreamAccountServiceError) as apply_context:
            await self.service.apply_account(
                self.db,
                7,
                1.0,
                displayed.identity_fingerprint,
            )

        self.assertEqual(enabled_context.exception.status_code, 409)
        self.assertEqual(delete_context.exception.status_code, 409)
        self.assertEqual(apply_context.exception.status_code, 409)
        self.assertEqual(self.sub2api.schedulable_calls, [])
        self.assertEqual(self.sub2api.delete_calls, [])
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_id_reuse_blocks_local_update_delete_and_discovery_before_side_effects(
        self,
    ) -> None:
        await self._manage(api_key="sk-original-identity")
        displayed = (await self.service.list_accounts(self.db))[0]
        original = await self._config()
        original_base_url = original.base_url
        self.sub2api.accounts[0]["name"] = "replacement-account"
        discovery = AsyncMock(return_value=discovery_result(group=1.0, recharge=1.0))

        with self.assertRaises(UpstreamAccountServiceError) as update_context:
            await self.service.upsert_account(
                self.db,
                7,
                UpstreamAccountUpdate(
                    expected_identity_fingerprint=displayed.identity_fingerprint,
                    base_url="https://replacement.example.com",
                    confirm_credential_rebind=True,
                ),
            )
        with self.assertRaises(UpstreamAccountServiceError) as delete_context:
            await self.service.delete_account(
                self.db,
                7,
                displayed.identity_fingerprint,
            )
        with patch("app.services.upstream_accounts.discover_upstream", new=discovery):
            with self.assertRaises(UpstreamAccountServiceError) as discover_context:
                await self.service.discover_account(
                    self.db,
                    7,
                    displayed.identity_fingerprint,
                )

        self.assertEqual(
            [
                update_context.exception.status_code,
                delete_context.exception.status_code,
                discover_context.exception.status_code,
            ],
            [409, 409, 409],
        )
        config = await self._config()
        self.assertEqual(config.base_url, original_base_url)
        self.assertEqual(self.sub2api.balance_calls, [])
        discovery.assert_not_awaited()

    async def test_id_reuse_after_refresh_stays_quarantined_until_explicit_rebind(self) -> None:
        await self._manage(api_key="sk-original-identity")
        original = (await self.service.list_accounts(self.db))[0]
        stored = await self._config()
        original_binding = stored.remote_identity_fingerprint
        original_ciphertext = stored.encrypted_api_key

        self.sub2api.accounts[0]["name"] = "replacement-account"
        self.sub2api.accounts[0]["created_at"] = "2026-07-15T12:00:00Z"
        refreshed = (await self.service.list_accounts(self.db))[0]

        self.assertNotEqual(refreshed.identity_fingerprint, original.identity_fingerprint)
        self.assertEqual(refreshed.identity_binding_status, "mismatch")
        self.assertTrue(refreshed.identity_rebind_required)
        self.assertFalse(refreshed.managed)
        self.assertFalse(refreshed.api_key_set)
        self.assertNotIn("original-identity", refreshed.model_dump_json())

        with self.assertRaises(UpstreamAccountServiceError) as enabled_context:
            await self.service.set_account_enabled(
                self.db,
                7,
                False,
                refreshed.identity_fingerprint,
            )
        with self.assertRaises(UpstreamAccountServiceError) as update_context:
            await self.service.upsert_account(
                self.db,
                7,
                UpstreamAccountUpdate(
                    expected_identity_fingerprint=refreshed.identity_fingerprint,
                    remote_name="replacement-account",
                ),
            )

        self.assertEqual(enabled_context.exception.status_code, 409)
        self.assertEqual(update_context.exception.status_code, 409)
        self.assertEqual(self.sub2api.schedulable_calls, [])
        await self.db.refresh(stored)
        self.assertEqual(stored.remote_identity_fingerprint, original_binding)
        self.assertEqual(stored.encrypted_api_key, original_ciphertext)

        rebound = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(
                expected_identity_fingerprint=refreshed.identity_fingerprint,
                confirm_identity_rebind=True,
            ),
        )
        self.assertEqual(rebound.identity_binding_status, "bound")
        self.assertFalse(rebound.identity_rebind_required)
        self.assertTrue(rebound.managed)
        self.assertTrue(rebound.api_key_set)
        await self.db.refresh(stored)
        self.assertNotEqual(stored.remote_identity_fingerprint, original_binding)
        self.assertEqual(stored.encrypted_api_key, original_ciphertext)
        self.assertEqual(stored.remote_name, "replacement-account")
        self.assertEqual(rebound.remote_name, "replacement-account")

    async def test_mismatched_remote_name_redacts_the_previous_api_key(self) -> None:
        secret = "sk-previous-account-secret"
        await self._manage(api_key=secret)

        self.sub2api.accounts[0]["name"] = secret
        self.sub2api.accounts[0]["created_at"] = "2026-07-15T12:00:00Z"
        refreshed = (await self.service.list_accounts(self.db))[0]

        self.assertEqual(refreshed.identity_binding_status, "mismatch")
        self.assertEqual(refreshed.remote_name, "[redacted]")
        self.assertNotIn(secret, refreshed.model_dump_json())

    async def test_legacy_unbound_config_binds_only_on_explicit_account_save(self) -> None:
        await self._manage(api_key="sk-legacy-binding")
        stored = await self._config()
        stored.remote_identity_fingerprint = None
        await self.db.commit()

        listed = (await self.service.list_accounts(self.db))[0]
        self.assertEqual(listed.identity_binding_status, "unbound")
        self.assertFalse(listed.managed)
        self.assertFalse(listed.api_key_set)

        rebound = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(
                expected_identity_fingerprint=listed.identity_fingerprint,
            ),
        )
        self.assertEqual(rebound.identity_binding_status, "bound")
        self.assertTrue(rebound.api_key_set)
        await self.db.refresh(stored)
        self.assertEqual(
            stored.remote_identity_fingerprint,
            self.service._remote_binding_fingerprint(self.sub2api.accounts[0]),
        )

    async def test_missing_created_at_cannot_be_claimed_or_receive_credentials(self) -> None:
        self.sub2api.accounts[0].pop("created_at")
        remote = self.sub2api.accounts[0]
        config = self.service._new_config(remote, 7)
        self.db.add(config)
        await self.db.commit()

        listed = (await self.service.list_accounts(self.db))[0]
        self.assertEqual(listed.identity_binding_status, "unbound")
        self.assertTrue(listed.identity_rebind_required)
        self.assertFalse(listed.managed)
        self.assertFalse(listed.api_key_set)

        with self.assertRaises(UpstreamAccountServiceError) as manage_context:
            await self.service.upsert_account(
                self.db,
                7,
                UpstreamAccountUpdate(
                    expected_identity_fingerprint=listed.identity_fingerprint,
                    api_key="sk-must-not-be-saved",
                ),
            )
        with self.assertRaises(UpstreamAccountServiceError) as claim_context:
            await self.service.bind_legacy_identities(
                self.db,
                {7: listed.identity_fingerprint},
            )
        with self.assertRaises(UpstreamAccountServiceError) as enable_context:
            await self.service.set_account_enabled(
                self.db,
                7,
                False,
                listed.identity_fingerprint,
            )

        self.assertEqual(
            [
                manage_context.exception.status_code,
                claim_context.exception.status_code,
                enable_context.exception.status_code,
            ],
            [409, 409, 409],
        )
        await self.db.refresh(config)
        self.assertIsNone(config.remote_identity_fingerprint)
        self.assertIsNone(config.encrypted_api_key)
        self.assertEqual(self.sub2api.schedulable_calls, [])

    async def test_new_api_key_clears_origin_rebind_without_decrypting_old_key(self) -> None:
        await self._manage(api_key="sk-old-origin")
        config = await self._config()
        old_ciphertext = config.encrypted_api_key
        config.api_key_origin_rebind_required = True
        await self.db.commit()

        blocked = (await self.service.list_accounts(self.db))[0]
        self.assertTrue(blocked.api_key_origin_rebind_required)
        self.assertFalse(blocked.api_key_set)

        from app.core.crypto import decrypt_text as real_decrypt_text

        def reject_old_ciphertext(value: str | None) -> str | None:
            if value == old_ciphertext:
                raise AssertionError("origin-blocked API key was decrypted")
            return real_decrypt_text(value)

        with patch(
            "app.services.upstream_accounts.decrypt_text",
            side_effect=reject_old_ciphertext,
        ):
            updated = await self.service.upsert_account(
                self.db,
                7,
                self._update(api_key="sk-new-origin"),
            )

        self.assertFalse(updated.api_key_origin_rebind_required)
        self.assertTrue(updated.api_key_set)
        await self.db.refresh(config)
        self.assertFalse(config.api_key_origin_rebind_required)
        self.assertEqual(real_decrypt_text(config.encrypted_api_key), "sk-new-origin")

    async def test_explicit_channel_unassignment_survives_overview_roundtrip(self) -> None:
        channel_service = UpstreamChannelService(accounts=self.service)
        initial = await channel_service.overview(self.db)
        channel_id = initial.channels[0].id

        detached = await self.service.upsert_account(
            self.db,
            7,
            self._update(channel_id=None),
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
            self._update(channel_id=channel_id),
        )
        rebound = await channel_service.overview(self.db)
        stored = await self._config()
        self.assertTrue(stored.channel_auto_assign_disabled)
        self.assertEqual([item.sub2api_account_id for item in rebound.channels[0].accounts], [7])

        self.sub2api.accounts[0]["credentials"]["base_url"] = "https://changed.example/v1"
        pinned = await channel_service.overview(self.db)
        stored = await self._config()
        self.assertEqual(stored.channel_id, channel_id)
        self.assertEqual(stored.base_url, "https://upstream.example.com")
        self.assertEqual(pinned.channels[0].canonical_base_url, "https://upstream.example.com")

    async def test_discover_unmanaged_account_opts_in_and_reads_balance_without_key(self) -> None:
        account = await self._discover()

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

    async def test_discover_all_does_not_count_failed_manual_fallback_as_success(self) -> None:
        await self._manage(
            api_key="sk-failed-fallback",
            manual_group_multiplier=1.2,
            manual_recharge_multiplier=1.1,
        )
        self.sub2api.balance_result = {"status": "unsupported"}

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=None, recharge=None, status="error")),
        ):
            result = await self.service.discover_all(self.db)

        self.assertEqual((result.total, result.succeeded, result.failed), (1, 0, 1))
        self.assertIsNotNone(result.accounts[0].target_rate)
        self.assertEqual(result.accounts[0].group_multiplier_status, "fallback_manual")
        self.assertEqual(result.accounts[0].recharge_multiplier_status, "fallback_manual")

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
            self._update(api_key="   ", manual_group_multiplier=2),
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
            account = await self._discover()

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
            account = await self._discover()

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
            account = await self._discover()

        self.assertEqual(account.upstream_type, "auto")
        self.assertEqual(account.resolved_upstream_type, "newapi")

    async def test_configuration_change_immediately_invalidates_old_preview(self) -> None:
        await self._manage(api_key="sk-preview")
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=2.0, recharge=1.0)),
        ):
            preview = await self._discover()
        self.assertEqual(preview.target_rate, 2.0)
        self.assertTrue(preview.group_options)

        changed = await self.service.upsert_account(
            self.db,
            7,
            self._update(manual_group_multiplier=3.0),
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
        stored = await self._config()
        stored.upstream_usage_amount = 12.375
        stored.upstream_usage_unit = "USD"
        stored.upstream_usage_checked_at = stored.created_at
        await self.db.commit()

        same = await self.service.upsert_account(
            self.db,
            7,
            self._update(
                base_url="https://upstream.example.com",
                upstream_type="sub2api",
                api_key="sk-original",
                access_token="access-original",
            ),
        )
        self.assertEqual((same.selected_group_id, same.selected_group_name), ("gold", "Gold"))
        self.assertEqual(same.upstream_usage_amount, 12.375)

        changed_base = await self.service.upsert_account(
            self.db,
            7,
            self._update(
                base_url="https://new-upstream.example.com",
                confirm_credential_rebind=True,
            ),
        )
        self.assertIsNone(changed_base.selected_group_id)
        self.assertIsNone(changed_base.selected_group_name)
        self.assertIsNone(changed_base.upstream_usage_amount)
        self.assertIsNone(changed_base.upstream_usage_unit)
        self.assertIsNone(changed_base.upstream_usage_checked_at)

        await self.service.upsert_account(
            self.db,
            7,
            self._update(selected_group_id="silver", selected_group_name="Silver"),
        )
        changed_type = await self.service.upsert_account(
            self.db,
            7,
            self._update(upstream_type="newapi"),
        )
        self.assertIsNone(changed_type.selected_group_id)
        self.assertIsNone(changed_type.selected_group_name)

        await self.service.upsert_account(
            self.db,
            7,
            self._update(selected_group_id="bronze", selected_group_name="Bronze"),
        )
        changed_key = await self.service.upsert_account(
            self.db,
            7,
            self._update(api_key="sk-replaced"),
        )
        self.assertIsNone(changed_key.selected_group_id)
        self.assertIsNone(changed_key.selected_group_name)

        await self.service.upsert_account(
            self.db,
            7,
            self._update(selected_group_id="vip", selected_group_name="VIP"),
        )
        changed_token = await self.service.upsert_account(
            self.db,
            7,
            self._update(access_token="access-replaced"),
        )
        self.assertIsNone(changed_token.selected_group_id)
        self.assertIsNone(changed_token.selected_group_name)

        explicit_new = await self.service.upsert_account(
            self.db,
            7,
            self._update(
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
                self._update(base_url="https://replacement.example.com"),
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
            account = await self._discover()

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
            first = await self._discover()
        self.assertEqual(first.target_rate, 2.0)

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=None, recharge=None, status="error")),
        ):
            failed = await self._discover()
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self.service.apply_account(
                    self.db,
                    7,
                    first.target_rate or 0,
                    await self._fingerprint(),
                )

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
            preview = await self._discover()
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self.service.apply_account(self.db, 7, 2.0, await self._fingerprint())

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
            account = await self._discover()

        self.assertEqual(account.effective_group_multiplier, 2.0)
        self.assertIsNone(account.effective_recharge_multiplier)
        self.assertEqual(account.recharge_multiplier_status, "discovery_failed")
        self.assertIsNone(account.target_rate)

    async def test_extreme_multiplier_product_is_unavailable_instead_of_raising_decimal_error(self) -> None:
        await self._manage(
            manual_group_multiplier=1000,
            manual_recharge_multiplier=1e-300,
        )

        preview = await self._discover()
        self.assertIsNone(preview.target_rate)
        self.assertIn("outside the supported range", preview.last_error or "")
        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.apply_account(self.db, 7, 1.0, await self._fingerprint())

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_positive_ratio_that_rounds_to_zero_is_not_applicable(self) -> None:
        await self._manage(
            manual_group_multiplier=1e-300,
            manual_recharge_multiplier=1000,
        )

        preview = await self._discover()

        self.assertIsNone(preview.target_rate)
        self.assertIn("outside the supported range", preview.last_error or "")
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_insecure_direct_discovery_is_rejected_with_422(self) -> None:
        with self.assertRaises(ValueError):
            self._update(base_url="http://upstream.example.com", api_key="sk-no-http")
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_manual_group_works_without_upstream_credentials(self) -> None:
        await self._manage(manual_group_multiplier=3.0, manual_recharge_multiplier=1.5)

        account = await self._discover()

        self.assertEqual(account.group_multiplier_source, "manual")
        self.assertEqual(account.recharge_multiplier_source, "manual")
        self.assertEqual(account.target_rate, 4.5)

    async def test_balance_failure_preserves_last_successful_values(self) -> None:
        secret = "sk-never-leak-balance"
        await self._manage(manual_group_multiplier=1.0)
        first = await self._discover()
        self.assertEqual(first.balance_remaining, 12500.25)
        self.assertIsNotNone(first.balance_checked_at)

        self.sub2api.balance_error = Sub2ApiRequestError(f"HTTP body contained {secret}")
        second = await self._discover()

        self.assertEqual(second.balance_remaining, 12500.25)
        self.assertEqual(second.balance_checked_at, first.balance_checked_at)
        self.assertEqual(second.balance_status, "error")
        serialized = str(second.model_dump())
        self.assertNotIn(secret, serialized)
        self.assertIn("Balance check failed.", second.last_error or "")

    async def test_negative_remaining_balance_is_preserved_as_valid_data(self) -> None:
        await self._manage(manual_group_multiplier=1.0)
        self.sub2api.balance_result["remaining"] = -40.904106

        account = await self._discover()

        self.assertEqual(account.balance_status, "ok")
        self.assertAlmostEqual(account.balance_remaining or 0, -40.904106)

    async def test_invalid_present_balance_number_preserves_last_successful_snapshot(self) -> None:
        await self._manage(manual_group_multiplier=1.0, manual_recharge_multiplier=1.0)
        first = await self._discover()
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
        second = await self._discover()

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
            account = await self._discover()

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
            first = await self._discover()
        self.assertEqual(len(first.group_options), 1)

        self.sub2api.balance_error = Sub2ApiRequestError(f"unsafe {secret}")
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(side_effect=RuntimeError(f"unsafe {secret}")),
        ):
            second = await self._discover()

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
            initial = await self._discover()

        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=2.0, recharge=1.0)),
        ):
            with self.assertRaises(UpstreamAccountServiceError) as context:
                await self.service.apply_account(
                    self.db,
                    7,
                    initial.target_rate or 0,
                    await self._fingerprint(),
                )

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.sub2api.update_calls, [])

    async def test_apply_writes_reads_back_and_sets_last_applied(self) -> None:
        await self._manage(api_key="sk-apply")
        with patch(
            "app.services.upstream_accounts.discover_upstream",
            new=AsyncMock(return_value=discovery_result(group=1.11115, recharge=1.0)),
        ):
            account = await self.service.apply_account(
                self.db,
                7,
                1.1112,
                await self._fingerprint(),
            )

        self.assertEqual(self.sub2api.update_calls, [(7, 1.1112)])
        self.assertEqual(account.current_rate, 1.1112)
        self.assertFalse(account.would_change)
        self.assertIsNotNone(account.last_applied_at)

    async def test_clear_access_token_only_when_explicitly_requested(self) -> None:
        await self._manage(access_token="access-token-secret")
        first = await self._config()
        original_ciphertext = first.encrypted_access_token

        await self.service.upsert_account(self.db, 7, self._update(access_token=""))
        preserved = await self._config()
        self.assertEqual(preserved.encrypted_access_token, original_ciphertext)

        cleared = await self.service.upsert_account(
            self.db,
            7,
            self._update(clear_access_token=True),
        )
        self.assertFalse(cleared.access_token_set)
        self.assertIsNone((await self._config()).encrypted_access_token)

    async def test_origin_blocked_api_key_still_redacts_remote_metadata(self) -> None:
        secret = "sk-origin-blocked-secret"
        await self._manage(api_key=secret)
        config = await self._config()
        config.api_key_origin_rebind_required = True
        self.sub2api.accounts[0]["name"] = secret
        await self.db.commit()

        listed = (await self.service.list_accounts(self.db))[0]

        self.assertTrue(listed.api_key_origin_rebind_required)
        self.assertFalse(listed.api_key_set)
        self.assertEqual(listed.remote_name, "[redacted]")
        self.assertNotIn(secret, listed.model_dump_json())

    def test_remote_name_schema_rejects_blank_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "remote_name must not be blank"):
            UpstreamAccountUpdate(
                expected_identity_fingerprint="a" * 64,
                remote_name="   ",
            )

    async def test_saving_api_key_scrubs_preexisting_plaintext_remote_name(self) -> None:
        secret = "sk-name-before-credential"
        self.sub2api.accounts[0]["name"] = secret
        await UpstreamChannelService(self.sub2api).overview(self.db)
        before = await self._config()
        self.assertEqual(before.remote_name, secret)

        saved = await self.service.upsert_account(
            self.db,
            7,
            self._update(api_key=secret),
        )

        stored = await self._config()
        self.assertEqual(stored.remote_name, "[redacted]")
        self.assertEqual(saved.remote_name, "[redacted]")

    async def test_remote_name_is_redacted_before_the_length_limit_is_applied(self) -> None:
        secret = "QZ9v7-credential-crossing-the-boundary"
        self.sub2api.accounts[0]["name"] = ("x" * 195) + secret

        saved = await self._manage(api_key=secret)
        stored = await self._config()

        self.assertNotIn("QZ9v7", stored.remote_name or "")
        self.assertNotIn("QZ9v7", saved.remote_name)
        self.assertEqual(len(stored.remote_name or ""), 200)

    async def test_saving_credentials_preserves_an_unchanged_legacy_long_name(self) -> None:
        legacy_name = "legacy-" + ("x" * 143)
        self.sub2api.accounts[0]["name"] = legacy_name
        await UpstreamChannelService(self.sub2api).overview(self.db)

        saved = await self.service.upsert_account(
            self.db,
            7,
            self._update(api_key="sk-unrelated-new-credential"),
        )

        self.assertEqual((await self._config()).remote_name, legacy_name)
        self.assertEqual(saved.remote_name, legacy_name)

    async def test_remote_rename_updates_sub2api_name_and_binding_fingerprint(self) -> None:
        await self._manage(api_key="sk-rename-preserved")
        before = await self._config()
        original_fingerprint = before.remote_identity_fingerprint
        original_ciphertext = before.encrypted_api_key

        renamed = await self.service.upsert_account(
            self.db,
            7,
            self._update(remote_name="renamed-upstream-account"),
        )

        stored = await self._config()
        self.assertEqual(self.sub2api.name_update_calls, [(7, "renamed-upstream-account")])
        self.assertEqual(self.sub2api.accounts[0]["name"], "renamed-upstream-account")
        self.assertEqual(renamed.remote_name, "renamed-upstream-account")
        self.assertEqual(renamed.identity_binding_status, "bound")
        self.assertEqual(
            stored.remote_identity_fingerprint,
            self.service._remote_binding_fingerprint(self.sub2api.accounts[0]),
        )
        self.assertNotEqual(stored.remote_identity_fingerprint, original_fingerprint)
        self.assertEqual(stored.encrypted_api_key, original_ciphertext)

    async def test_remote_rename_rejects_identity_change_before_put(self) -> None:
        await self._manage(api_key="sk-rename-preflight")
        before = await self._config()
        original_name = before.remote_name
        original_fingerprint = before.remote_identity_fingerprint
        replacement = dict(self.sub2api.accounts[0])
        replacement["created_at"] = "2026-07-15T12:00:00Z"
        self.sub2api.get_account_by_id = AsyncMock(  # type: ignore[method-assign]
            return_value=replacement
        )

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.upsert_account(
                self.db,
                7,
                self._update(remote_name="must-not-reach-the-replacement"),
            )

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(self.sub2api.name_update_calls, [])
        stored = await self._config()
        self.assertEqual(stored.remote_name, original_name)
        self.assertEqual(stored.remote_identity_fingerprint, original_fingerprint)

    async def test_identity_rebind_and_remote_rename_keep_the_renamed_fingerprint(self) -> None:
        await self._manage(api_key="sk-rebind-and-rename")
        self.sub2api.accounts[0]["name"] = "replacement-account"
        self.sub2api.accounts[0]["created_at"] = "2026-07-15T12:00:00Z"
        refreshed = (await self.service.list_accounts(self.db))[0]

        renamed = await self.service.upsert_account(
            self.db,
            7,
            UpstreamAccountUpdate(
                expected_identity_fingerprint=refreshed.identity_fingerprint,
                confirm_identity_rebind=True,
                remote_name="renamed-replacement-account",
            ),
        )

        stored = await self._config()
        self.assertEqual(self.sub2api.name_update_calls, [(7, "renamed-replacement-account")])
        self.assertEqual(renamed.identity_binding_status, "bound")
        self.assertTrue(renamed.managed)
        self.assertEqual(renamed.remote_name, "renamed-replacement-account")
        self.assertEqual(
            stored.remote_identity_fingerprint,
            self.service._remote_binding_fingerprint(self.sub2api.accounts[0]),
        )

    async def test_remote_rename_failure_does_not_update_local_name_or_fingerprint(self) -> None:
        await self._manage(api_key="sk-rename-failure")
        before = await self._config()
        original_name = before.remote_name
        original_fingerprint = before.remote_identity_fingerprint
        self.sub2api.update_account_name = AsyncMock(  # type: ignore[method-assign]
            side_effect=Sub2ApiRequestError("unsafe remote failure")
        )

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.upsert_account(
                self.db,
                7,
                self._update(remote_name="must-not-land-locally"),
            )

        self.assertEqual(context.exception.status_code, 502)
        stored = await self._config()
        self.assertEqual(stored.remote_name, original_name)
        self.assertEqual(stored.remote_identity_fingerprint, original_fingerprint)

    async def test_remote_rename_rejects_changed_immutable_identity_after_readback(self) -> None:
        await self._manage(api_key="sk-rename-race")
        before = await self._config()
        original_name = before.remote_name
        original_fingerprint = before.remote_identity_fingerprint

        async def rename_replaced_account(
            account_id: str | int,
            name: str,
            *,
            validate_current=None,
        ) -> dict:
            renamed = await FakeSub2Api.update_account_name(
                self.sub2api,
                account_id,
                name,
                validate_current=validate_current,
            )
            renamed["created_at"] = "2026-07-15T12:00:00Z"
            return renamed

        self.sub2api.update_account_name = rename_replaced_account  # type: ignore[method-assign]

        with self.assertRaises(UpstreamAccountServiceError) as context:
            await self.service.upsert_account(
                self.db,
                7,
                self._update(remote_name="replacement-name"),
            )

        self.assertEqual(context.exception.status_code, 409)
        stored = await self._config()
        self.assertEqual(stored.remote_name, original_name)
        self.assertEqual(stored.remote_identity_fingerprint, original_fingerprint)

    async def test_delete_validates_identity_and_removes_only_local_config(self) -> None:
        await self._manage(api_key="sk-local-only")
        fingerprint = await self._fingerprint()
        list_accounts = AsyncMock(wraps=self.sub2api.list_api_key_accounts)
        self.sub2api.list_api_key_accounts = list_accounts  # type: ignore[method-assign]
        self.sub2api.delete_account = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("remote deletion must not be called")
        )

        removed = await self.service.delete_account(self.db, 7, fingerprint)

        self.assertTrue(removed)
        result = await self.db.execute(select(UpstreamAccountConfig))
        self.assertIsNone(result.scalar_one_or_none())
        list_accounts.assert_awaited_once_with()
        self.sub2api.delete_account.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_disable_updates_and_verifies_real_sub2api_account(self) -> None:
        await self._manage(api_key="sk-disable-test")

        account = await self.service.set_account_enabled(
            self.db,
            7,
            False,
            await self._fingerprint(),
        )

        self.assertEqual(self.sub2api.schedulable_calls, [(7, False)])
        self.assertFalse(account.remote_schedulable)
        self.assertFalse(self.sub2api.accounts[0]["schedulable"])

    async def test_remote_delete_removes_sub2api_account_and_local_config(self) -> None:
        await self._manage(api_key="sk-delete-test")

        deleted = await self.service.delete_remote_account(
            self.db,
            7,
            await self._fingerprint(),
        )

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
                await self.service.apply_account(
                    self.db,
                    7,
                    1.2,
                    await self._fingerprint(),
                )

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

    async def test_account_detail_uses_single_account_route_and_unwraps_data(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={"data": {"id": 17, "name": "account-seventeen"}}
        )
        config, runtime_patch = self._runtime_patch()

        with runtime_patch:
            account = await client.get_account_by_id(17)

        self.assertEqual(account, {"id": 17, "name": "account-seventeen"})
        client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "GET",
            "/admin/accounts/17",
            config=config,
        )

    async def test_account_detail_maps_404_to_missing_and_rejects_mismatched_ids(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=Sub2ApiRequestError("missing", status_code=404)
        )
        _config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            self.assertIsNone(await client.get_account_by_id(17))

        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={"data": {"id": 18, "name": "wrong-account"}}
        )
        _config, mismatch_runtime_patch = self._runtime_patch()
        with mismatch_runtime_patch, self.assertRaises(Sub2ApiRequestError):
            await client.get_account_by_id(17)

    async def test_account_name_update_uses_put_and_reads_back_from_same_config(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"data": {"id": 17, "name": "old-name", "type": "apikey"}},
                {},
                {"data": {"id": 17, "name": "renamed", "type": "apikey"}},
            ]
        )
        config, runtime_patch = self._runtime_patch()

        with runtime_patch:
            updated = await client.update_account_name(17, "  renamed  ")

        self.assertEqual(updated["name"], "renamed")
        self.assertEqual(
            client._request.await_args_list,  # type: ignore[attr-defined]
            [
                call("GET", "/admin/accounts/17", config=config),
                call(
                    "PUT",
                    "/admin/accounts/17",
                    config=config,
                    json={"name": "renamed"},
                ),
                call("GET", "/admin/accounts/17", config=config),
            ],
        )

    async def test_account_name_update_checks_precondition_before_put(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(return_value={})  # type: ignore[method-assign]
        current = {"id": 17, "name": "old-name", "type": "apikey"}
        client.get_account_by_id = AsyncMock(return_value=current)  # type: ignore[method-assign]
        config, runtime_patch = self._runtime_patch()
        checked: list[dict] = []

        def reject(account: dict) -> None:
            checked.append(account)
            raise RuntimeError("stale identity")

        with runtime_patch, self.assertRaisesRegex(RuntimeError, "stale identity"):
            await client.update_account_name(17, "new-name", validate_current=reject)

        self.assertEqual(checked, [current])
        client.get_account_by_id.assert_awaited_once_with(17, config=config)  # type: ignore[attr-defined]
        client._request.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_account_name_update_requires_confirming_readback(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(return_value={})  # type: ignore[method-assign]
        client.get_account_by_id = AsyncMock(  # type: ignore[method-assign]
            return_value={"id": 17, "name": "old-name", "type": "apikey"}
        )
        _config, runtime_patch = self._runtime_patch()

        with runtime_patch, self.assertRaises(Sub2ApiRequestError):
            await client.update_account_name(17, "new-name")

    async def test_account_name_update_recovers_a_lost_put_response_with_get_only(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=Sub2ApiRequestError("lost mutation response")
        )
        client.get_account_by_id = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"id": 17, "name": "old-name", "type": "apikey"},
                {"id": 17, "name": "renamed", "type": "apikey"},
            ]
        )
        config, runtime_patch = self._runtime_patch()

        with runtime_patch:
            updated = await client.update_account_name(17, "renamed")

        self.assertEqual(updated["name"], "renamed")
        client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "PUT",
            "/admin/accounts/17",
            config=config,
            json={"name": "renamed"},
        )
        self.assertEqual(client.get_account_by_id.await_count, 2)  # type: ignore[attr-defined]
        for readback_call in client.get_account_by_id.await_args_list:  # type: ignore[attr-defined]
            self.assertEqual(readback_call.args, (17,))
            self.assertEqual(readback_call.kwargs, {"config": config})

    async def test_account_name_update_retries_only_readback_on_transient_failure(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(return_value={})  # type: ignore[method-assign]
        client.get_account_by_id = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"id": 17, "name": "old-name", "type": "apikey"},
                Sub2ApiRequestError("transient readback failure"),
                {"id": 17, "name": "renamed", "type": "apikey"},
            ]
        )
        config, runtime_patch = self._runtime_patch()

        with runtime_patch:
            updated = await client.update_account_name(17, "renamed")

        self.assertEqual(updated["name"], "renamed")
        self.assertEqual(client._request.await_count, 1)  # type: ignore[attr-defined]
        self.assertEqual(client.get_account_by_id.await_count, 3)  # type: ignore[attr-defined]
        for readback_call in client.get_account_by_id.await_args_list:  # type: ignore[attr-defined]
            self.assertEqual(readback_call.args, (17,))
            self.assertEqual(readback_call.kwargs, {"config": config})

    async def test_export_accepts_matching_explicit_ids_from_single_account_exports(self) -> None:
        client = Sub2ApiClient()
        account_seventeen = {
            "id": 17,
            "name": "account-seventeen",
            "type": "api_key",
        }
        account_eighteen = {
            "id": 18,
            "name": "account-eighteen",
            "type": "apikey",
        }
        client.get_account_by_id = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                account_seventeen,
                account_seventeen,
                account_eighteen,
                account_eighteen,
            ]
        )
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "data": [
                        {
                            **account_seventeen,
                            "credentials": {"api_key": "sk-seventeen-private"},
                        }
                    ]
                },
                {
                    "data": [
                        {
                            **account_eighteen,
                            "credentials": {"api_key": "sk-eighteen-private"},
                        }
                    ]
                },
            ]
        )
        config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            exported = await client.export_api_key_secrets([17, 18])

        self.assertEqual(
            exported,
            {17: "sk-seventeen-private", 18: "sk-eighteen-private"},
        )
        self.assertEqual(
            client._request.await_args_list,  # type: ignore[attr-defined]
            [
                call(
                    "GET",
                    "/admin/accounts/data",
                    config=config,
                    params={"ids": "17", "include_proxies": "false"},
                ),
                call(
                    "GET",
                    "/admin/accounts/data",
                    config=config,
                    params={"ids": "18", "include_proxies": "false"},
                ),
            ],
        )

    async def test_export_maps_idless_backup_rows_by_isolated_account_id(self) -> None:
        client = Sub2ApiClient()
        account_seventeen = {
            "id": "17",
            "name": "account-seventeen",
            "platform": "OPENAI",
            "type": "api-key",
            "credentials": {"base_url": "https://upstream-seventeen.example"},
        }
        account_eighteen = {
            "id": "18",
            "name": "account-eighteen",
            "platform": "anthropic",
            "type": "apikey",
            "credentials": {"base_url": "https://upstream-eighteen.example"},
        }
        client.get_account_by_id = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                account_seventeen,
                account_seventeen,
                account_eighteen,
                account_eighteen,
            ]
        )
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "data": {
                        "accounts": [
                        {
                            "name": "account-seventeen",
                            "platform": "custom",
                            "type": "api_key",
                            "credentials": {
                                "base_url": "https://different-seventeen.example/v1",
                                "api_key": "sk-seventeen-private",
                            },
                        },
                        ],
                        "proxies": [],
                    }
                },
                {
                    "data": {
                        "accounts": [
                            {
                                "name": "account-eighteen",
                                "platform": "different",
                                "type": "apikey",
                                "credentials": {
                                    "base_url": "https://different-eighteen.example/api/v1",
                                    "api_key": "sk-eighteen-private",
                                },
                            },
                        ],
                        "proxies": [],
                    }
                },
            ]
        )
        config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            exported = await client.export_api_key_secrets([17, 18])

        self.assertEqual(
            exported,
            {17: "sk-seventeen-private", 18: "sk-eighteen-private"},
        )
        expected_export_call = call(
            "GET",
            "/admin/accounts/data",
            config=config,
            params={"ids": "17", "include_proxies": "false"},
        )
        self.assertEqual(client._request.await_count, 2)  # type: ignore[attr-defined]
        self.assertEqual(
            client._request.await_args_list,  # type: ignore[attr-defined]
            [
                expected_export_call,
                call(
                    "GET",
                    "/admin/accounts/data",
                    config=config,
                    params={"ids": "18", "include_proxies": "false"},
                ),
            ],
        )
        self.assertEqual(client.get_account_by_id.await_count, 4)  # type: ignore[attr-defined]

    async def test_export_backup_rows_with_duplicate_identity_fail_closed(self) -> None:
        client = Sub2ApiClient()
        exported_account = {
            "name": "duplicate",
            "platform": "openai",
            "type": "apikey",
            "credentials": {
                "base_url": "https://duplicate.example/v1",
                "api_key": "sk-private",
            },
        }
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={"data": {"accounts": [exported_account]}}
        )
        client.list_accounts = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "id": 17,
                    "name": "duplicate",
                    "platform": "openai",
                    "type": "apikey",
                    "credentials": {"base_url": "https://duplicate.example"},
                },
                {
                    "id": 18,
                    "name": "duplicate",
                    "platform": "openai",
                    "type": "apikey",
                    "credentials": {"base_url": "https://duplicate.example"},
                },
            ]
        )
        _config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            exported = await client.export_api_key_secrets([17, 18])

        self.assertEqual(exported, {})

    async def test_export_malformed_explicit_id_does_not_fall_back_to_identity(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "data": [
                    {
                        "id": "not-a-number",
                        "name": "account-seventeen",
                        "platform": "openai",
                        "type": "apikey",
                        "credentials": {
                            "base_url": "https://upstream-seventeen.example",
                            "api_key": "sk-seventeen-private",
                        },
                    }
                ]
            }
        )
        client.list_accounts = AsyncMock()  # type: ignore[method-assign]
        _config, runtime_patch = self._runtime_patch()
        with runtime_patch:
            exported = await client.export_api_key_secrets([17])

        self.assertEqual(exported, {})
        client.list_accounts.assert_not_awaited()  # type: ignore[attr-defined]

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
    def test_point_mutation_routes_forward_the_expected_identity_fingerprint(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        db = AsyncMock()
        fingerprint = "a" * 64
        account_out = UpstreamAccountOut(
            sub2api_account_id=7,
            identity_fingerprint=fingerprint,
            remote_name="account-seven",
            managed=True,
        )
        service = SimpleNamespace(
            upsert_account=AsyncMock(return_value=account_out),
            delete_account=AsyncMock(return_value=True),
            set_account_enabled=AsyncMock(return_value=account_out),
            delete_remote_account=AsyncMock(return_value=True),
            discover_account=AsyncMock(return_value=account_out),
            apply_account=AsyncMock(return_value=account_out),
        )

        async def fake_db():
            yield db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_account_service] = lambda: service
        with TestClient(app) as client:
            updated = client.put(
                "/api/upstream-accounts/7",
                json={
                    "remote_name": "account-seven",
                    "expected_identity_fingerprint": fingerprint,
                },
            )
            local_deleted = client.request(
                "DELETE",
                "/api/upstream-accounts/7",
                json={"expected_identity_fingerprint": fingerprint},
            )
            enabled = client.patch(
                "/api/upstream-accounts/7/enabled",
                json={"enabled": False, "expected_identity_fingerprint": fingerprint},
            )
            deleted = client.request(
                "DELETE",
                "/api/upstream-accounts/7/remote",
                json={
                    "confirmed_account_id": 7,
                    "expected_identity_fingerprint": fingerprint,
                },
            )
            discovered = client.post(
                "/api/upstream-accounts/7/discover",
                json={"expected_identity_fingerprint": fingerprint},
            )
            applied = client.post(
                "/api/upstream-accounts/7/apply",
                json={
                    "confirmed_target_rate": 1.25,
                    "expected_identity_fingerprint": fingerprint,
                },
            )

        self.assertEqual(
            [
                updated.status_code,
                local_deleted.status_code,
                enabled.status_code,
                deleted.status_code,
                discovered.status_code,
                applied.status_code,
            ],
            [200, 200, 200, 200, 200, 200],
        )
        forwarded_update = service.upsert_account.await_args.args[2]
        self.assertIsInstance(forwarded_update, UpstreamAccountUpdate)
        self.assertEqual(forwarded_update.expected_identity_fingerprint, fingerprint)
        service.delete_account.assert_awaited_once_with(db, 7, fingerprint)
        service.set_account_enabled.assert_awaited_once_with(db, 7, False, fingerprint)
        service.delete_remote_account.assert_awaited_once_with(db, 7, fingerprint)
        service.discover_account.assert_awaited_once_with(db, 7, fingerprint)
        service.apply_account.assert_awaited_once_with(db, 7, 1.25, fingerprint)

    def test_point_mutation_routes_require_an_identity_fingerprint(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        service = SimpleNamespace(
            upsert_account=AsyncMock(),
            delete_account=AsyncMock(),
            set_account_enabled=AsyncMock(),
            delete_remote_account=AsyncMock(),
            discover_account=AsyncMock(),
            apply_account=AsyncMock(),
        )

        async def fake_db():
            yield AsyncMock()

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_upstream_account_service] = lambda: service
        with TestClient(app) as client:
            responses = [
                client.put("/api/upstream-accounts/7", json={"remote_name": "account-seven"}),
                client.request("DELETE", "/api/upstream-accounts/7", json={}),
                client.patch("/api/upstream-accounts/7/enabled", json={"enabled": False}),
                client.request(
                    "DELETE",
                    "/api/upstream-accounts/7/remote",
                    json={"confirmed_account_id": 7},
                ),
                client.post("/api/upstream-accounts/7/discover", json={}),
                client.post(
                    "/api/upstream-accounts/7/apply",
                    json={"confirmed_target_rate": 1.25},
                ),
            ]

        self.assertEqual([response.status_code for response in responses], [422] * 6)
        service.upsert_account.assert_not_awaited()
        service.delete_account.assert_not_awaited()
        service.set_account_enabled.assert_not_awaited()
        service.delete_remote_account.assert_not_awaited()
        service.discover_account.assert_not_awaited()
        service.apply_account.assert_not_awaited()

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
            response = client.put(
                "/api/upstream-accounts/7",
                json={
                    "api_key": secret,
                    "expected_identity_fingerprint": "a" * 64,
                },
            )

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
                json={
                    "api_key": secret,
                    "base_url": "file:///private/path",
                    "expected_identity_fingerprint": "a" * 64,
                },
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
