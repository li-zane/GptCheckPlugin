import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.accounts import router
from app.core.database import get_db
from app.core.security import require_admin
from app.services.refresh import RefreshService
from app.services.runtime_config import EffectiveSub2ApiConfig
from app.schemas import Sub2ApiSyncResult
from app.services.sub2api import Sub2ApiClient
from app.services.usage_refresh import (
    UsageRefreshService,
    UsageRefreshSummary,
    cached_usage_from_account,
    resolve_usage_refresh_force,
)


class RefreshAutomationTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_refresh_force_defaults_to_automatic_runs_only(self) -> None:
        self.assertTrue(resolve_usage_refresh_force("scheduled", None))
        self.assertFalse(resolve_usage_refresh_force("manual", None))
        self.assertFalse(resolve_usage_refresh_force("oauth_sync", None))
        self.assertTrue(resolve_usage_refresh_force("manual", True))
        self.assertFalse(resolve_usage_refresh_force("scheduled", False))

    async def test_usage_request_only_sends_force_for_forced_refreshes(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(return_value={"data": {}})  # type: ignore[method-assign]
        config = EffectiveSub2ApiConfig(
            base_url="http://sub2api.test/api/v1",
            auth_token="admin-token",
            auth_header="Authorization",
            auth_scheme="Bearer",
            accounts_path="/admin/accounts",
            access_token_path="credentials.access_token",
            auto_clear_error=True,
            auto_recover_state=True,
        )

        await client.refresh_account_usage_data("7", config=config, force=False)
        self.assertEqual(client._request.await_args.kwargs["params"], {"source": "active"})

        await client.refresh_account_usage_data("7", config=config, force=True)
        self.assertEqual(
            client._request.await_args.kwargs["params"],
            {"source": "active", "force": "true"},
        )

    async def test_manual_usage_refresh_reads_account_snapshot_without_request(self) -> None:
        account = {
            "id": 7,
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "cached@example.com"},
            "extra": {
                "codex_usage_updated_at": "2026-07-17T08:00:00Z",
                "codex_5h_used_percent": 12.5,
                "codex_7d_used_percent": 34.5,
            },
        }
        service = UsageRefreshService()
        service.sub2api.refresh_account_usage_data = AsyncMock()  # type: ignore[method-assign]

        result = await service._refresh_one_unlimited(
            account,
            "7",
            "cached@example.com",
            {},
            SimpleNamespace(),
            False,
        )

        self.assertEqual(result[:4], ("7", "cached@example.com", True, None))
        self.assertEqual(
            result[4],
            {
                "source": "account_snapshot",
                "updated_at": "2026-07-17T08:00:00Z",
            },
        )
        service.sub2api.refresh_account_usage_data.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_manual_usage_refresh_skips_missing_snapshot_without_request(self) -> None:
        account = {
            "id": 8,
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "missing@example.com"},
        }
        service = UsageRefreshService()
        service.sub2api.refresh_account_usage_data = AsyncMock()  # type: ignore[method-assign]

        result = await service._refresh_one_unlimited(
            account,
            "8",
            "missing@example.com",
            {},
            SimpleNamespace(),
            False,
        )

        self.assertEqual(result, ("8", "missing@example.com", False, None, None))
        service.sub2api.refresh_account_usage_data.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_automatic_usage_refresh_forces_remote_request(self) -> None:
        account = {
            "id": 9,
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "forced@example.com"},
            "extra": {"codex_5h_used_percent": 10},
        }
        service = UsageRefreshService()
        service.sub2api.refresh_account_usage_data = AsyncMock(return_value={})  # type: ignore[method-assign]
        config = SimpleNamespace()

        await service._refresh_one_unlimited(
            account,
            "9",
            "forced@example.com",
            {},
            config,
            True,
        )

        service.sub2api.refresh_account_usage_data.assert_awaited_once_with(  # type: ignore[attr-defined]
            account,
            config=config,
            force=True,
        )

    def test_cached_usage_requires_a_materialized_window_value(self) -> None:
        self.assertIsNone(cached_usage_from_account({"extra": {"codex_usage_updated_at": "now"}}))
        self.assertEqual(
            cached_usage_from_account({"extra": {"codex_5h_used_percent": 0}}),
            {"source": "account_snapshot", "updated_at": None},
        )

    async def test_manual_refresh_uses_shared_executor_when_auto_refresh_is_disabled(self) -> None:
        account = {
            "id": 7,
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "manual@example.com"},
        }
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(return_value=[account])  # type: ignore[method-assign]
        service = object.__new__(RefreshService)
        service.sub2api = sub2api
        service.runtime_config = SimpleNamespace(
            get_recovery_enabled=AsyncMock(return_value=False)
        )
        service.enqueue = AsyncMock(return_value=11)  # type: ignore[method-assign]

        job_id = await service.enqueue_by_email("manual@example.com")

        self.assertEqual(job_id, 11)
        service.runtime_config.get_recovery_enabled.assert_not_awaited()
        service.enqueue.assert_awaited_once_with(  # type: ignore[attr-defined]
            account,
            "manual",
            allow_deactive=True,
            manual=True,
        )


class ManualRefreshRouteTests(unittest.TestCase):
    def test_manual_refresh_route_ignores_disabled_automatic_refresh(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/accounts")
        service = SimpleNamespace(
            enqueue_by_email=AsyncMock(return_value=11),
        )
        runtime = SimpleNamespace(
            get_recovery_enabled=AsyncMock(return_value=False),
        )
        job = SimpleNamespace(
            id=11,
            email="manual@example.com",
            sub2api_account_id="7",
            status="queued",
            reason="manual",
            access_token_tail=None,
            memory_peak_rss_bytes=None,
            started_at=None,
            finished_at=None,
            created_at=datetime.now(timezone.utc),
        )
        fake_db = SimpleNamespace(get=AsyncMock(return_value=job))

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        with (
            patch("app.api.accounts.get_refresh_service", return_value=service),
            patch("app.api.accounts.get_runtime_config_service", return_value=runtime),
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/accounts/refresh",
                json={"email": "manual@example.com"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        service.enqueue_by_email.assert_awaited_once_with(
            "manual@example.com",
            reason="manual",
        )
        runtime.get_recovery_enabled.assert_not_awaited()
        fake_db.get.assert_awaited_once()

    def test_manual_oauth_sync_reuses_one_account_snapshot_for_inventory_and_usage(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/accounts")
        raw_accounts = [
            {
                "id": 7,
                "platform": "openai",
                "type": "oauth",
                "status": "active",
                "credentials": {"email": "healthy@example.com"},
            }
        ]
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(return_value=raw_accounts)  # type: ignore[method-assign]
        call_order: list[str] = []

        async def sync_once(*_args, **_kwargs):
            call_order.append("inventory")
            return Sub2ApiSyncResult(
                message="inventory synced",
                total_seen=1,
                error_seen=0,
                queued=0,
            )

        async def refresh_all(*_args, **_kwargs):
            call_order.append("usage")
            return UsageRefreshSummary(total=1, refreshed=1, max_concurrency=0)

        monitor = SimpleNamespace(sync_once=AsyncMock(side_effect=sync_once))
        usage = SimpleNamespace(refresh_all=AsyncMock(side_effect=refresh_all))
        runtime = SimpleNamespace(get_recovery_enabled=AsyncMock(return_value=False))
        fake_db = AsyncMock()

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        record = AsyncMock()
        with (
            patch("app.api.accounts.Sub2ApiClient", return_value=sub2api),
            patch("app.api.accounts.get_monitor_service", return_value=monitor),
            patch("app.api.accounts.get_usage_refresh_service", return_value=usage),
            patch("app.api.accounts.get_runtime_config_service", return_value=runtime),
            patch("app.api.accounts.record_event", new=record),
            TestClient(app) as client,
        ):
            response = client.post("/api/accounts/sync")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["usage_refreshed"], 1)
        self.assertIs(monitor.sync_once.await_args.kwargs["raw_accounts"], raw_accounts)
        self.assertIs(usage.refresh_all.await_args.kwargs["accounts"], raw_accounts)
        sub2api.list_accounts.assert_awaited_once()
        self.assertEqual(call_order, ["inventory", "usage"])
        details = record.await_args.kwargs["details"]
        for field_name in (
            "duration_ms",
            "account_list_duration_ms",
            "inventory_duration_ms",
            "usage_dispatch_duration_ms",
        ):
            self.assertIsInstance(details[field_name], int)
            self.assertGreaterEqual(details[field_name], 0)

    def test_manual_oauth_sync_pending_count_only_includes_query_candidates(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/accounts")
        raw_accounts = [
            {
                "id": 7,
                "platform": "openai",
                "type": "oauth",
                "status": "active",
                "credentials": {"email": "healthy@example.com"},
            },
            {
                "id": 8,
                "platform": "openai",
                "type": "oauth",
                "status": "error",
                "credentials": {"email": "recovering@example.com"},
            },
            {
                "id": 9,
                "platform": "openai",
                "type": "oauth",
                "status": "deactivated",
                "credentials": {"email": "deactivated@example.com"},
            },
            {
                "platform": "openai",
                "type": "oauth",
                "status": "active",
                "credentials": {"email": "missing-id@example.com"},
            },
        ]
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(return_value=raw_accounts)  # type: ignore[method-assign]
        monitor = SimpleNamespace(
            sync_once=AsyncMock(
                return_value=Sub2ApiSyncResult(
                    message="inventory synced",
                    total_seen=4,
                    error_seen=1,
                    queued=1,
                )
            )
        )

        async def slow_usage_refresh(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return UsageRefreshSummary(total=1, refreshed=1, max_concurrency=0)

        usage = SimpleNamespace(refresh_all=AsyncMock(side_effect=slow_usage_refresh))
        runtime = SimpleNamespace(get_recovery_enabled=AsyncMock(return_value=True))
        fake_db = AsyncMock()

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        with (
            patch("app.api.accounts.Sub2ApiClient", return_value=sub2api),
            patch("app.api.accounts.get_monitor_service", return_value=monitor),
            patch("app.api.accounts.get_usage_refresh_service", return_value=usage),
            patch("app.api.accounts.get_runtime_config_service", return_value=runtime),
            patch("app.api.accounts.record_event", new=AsyncMock()),
            TestClient(app) as client,
        ):
            response = client.post("/api/accounts/sync")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["usage_total"], 1)
        self.assertEqual(response.json()["usage_pending"], 1)
        self.assertIn(
            "Usage windows continue in the background for 1 account(s)",
            response.json()["message"],
        )
        self.assertEqual(usage.refresh_all.await_args.kwargs["skip_account_ids"], {"8"})


if __name__ == "__main__":
    unittest.main()
