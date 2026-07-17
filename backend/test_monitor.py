import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from app.services.monitor import MonitorService
from app.services.sub2api import Sub2ApiClient


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


class MonitorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_once_excludes_openai_api_key_accounts(self) -> None:
        oauth_account = {
            "id": 1,
            "email": "oauth@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {"refresh_token": "redacted"},
        }
        api_key_account = {
            "id": 2,
            "email": "api-key@example.com",
            "platform": "openai",
            "type": "api_key",
            "status": "active",
            "schedulable": True,
            "credentials": {"api_key": "redacted"},
        }
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(  # type: ignore[method-assign]
            return_value=[oauth_account, api_key_account]
        )
        service = object.__new__(MonitorService)
        service.sub2api = sub2api
        service.runtime_config = SimpleNamespace(
            get_recovery_enabled=AsyncMock(return_value=False)
        )
        service.refresh_service = SimpleNamespace()
        service._upsert_snapshot = AsyncMock(return_value=(False, False))  # type: ignore[method-assign]
        service._ensure_routed_mailbox = AsyncMock()  # type: ignore[method-assign]
        service._clear_account_exceptions = AsyncMock()  # type: ignore[method-assign]
        service._delete_missing_remote_accounts = AsyncMock(  # type: ignore[method-assign]
            return_value=(0, 0)
        )
        db = AsyncMock()

        with (
            patch("app.services.monitor.AsyncSessionLocal", return_value=_SessionContext(db)),
            patch("app.services.monitor.record_event", new=AsyncMock()) as record,
        ):
            result = await service.sync_once(reason="manual")

        self.assertEqual(result.total_seen, 1)
        self.assertIn("OAuth GPT accounts", result.message)
        service._upsert_snapshot.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(service._upsert_snapshot.await_args.args[0], "oauth@example.com")  # type: ignore[attr-defined]
        service._delete_missing_remote_accounts.assert_awaited_once_with(  # type: ignore[attr-defined]
            {"oauth@example.com", "api-key@example.com"},
            db=ANY,
        )
        db.commit.assert_awaited_once()
        record.assert_awaited_once()
        details = record.await_args.kwargs["details"]
        self.assertEqual(details["reason"], "manual")
        self.assertIsInstance(details["duration_ms"], int)
        self.assertIsInstance(details["account_list_duration_ms"], int)

    async def test_sync_once_returns_result_when_post_commit_audit_fails(self) -> None:
        account = {
            "id": 1,
            "email": "oauth@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {"refresh_token": "redacted"},
        }
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(return_value=[account])  # type: ignore[method-assign]
        service = object.__new__(MonitorService)
        service.sub2api = sub2api
        service.runtime_config = SimpleNamespace(get_recovery_enabled=AsyncMock(return_value=False))
        service.refresh_service = SimpleNamespace()
        service._upsert_snapshot = AsyncMock(return_value=(False, False))  # type: ignore[method-assign]
        service._ensure_routed_mailbox = AsyncMock()  # type: ignore[method-assign]
        service._clear_account_exceptions = AsyncMock()  # type: ignore[method-assign]
        service._delete_missing_remote_accounts = AsyncMock(return_value=(0, 0))  # type: ignore[method-assign]
        db = AsyncMock()

        with (
            patch("app.services.monitor.AsyncSessionLocal", return_value=_SessionContext(db)),
            patch(
                "app.services.monitor.record_event",
                new=AsyncMock(side_effect=RuntimeError("audit unavailable")),
            ),
        ):
            result = await service.sync_once(reason="manual")

        self.assertEqual(result.total_seen, 1)
        db.rollback.assert_awaited_once()

    async def test_monitor_loop_continues_when_failure_audit_is_unavailable(self) -> None:
        service = object.__new__(MonitorService)
        service._stop = asyncio.Event()
        service._wake = asyncio.Event()
        service.sub2api = Sub2ApiClient()
        service.runtime_config = SimpleNamespace(
            get_automation_paused=AsyncMock(return_value=False),
            get_oauth_account_sync_enabled=AsyncMock(return_value=True),
        )
        service.sync_once = AsyncMock(side_effect=RuntimeError("sync unavailable"))  # type: ignore[method-assign]
        service._wait_for_startup_delay = AsyncMock()  # type: ignore[method-assign]

        async def stop_after_iteration() -> None:
            service._stop.set()

        service._wait_for_next_run = AsyncMock(side_effect=stop_after_iteration)  # type: ignore[method-assign]
        db = AsyncMock()
        with (
            patch("app.services.monitor.AsyncSessionLocal", return_value=_SessionContext(db)),
            patch(
                "app.services.monitor.record_event",
                new=AsyncMock(side_effect=RuntimeError("audit unavailable")),
            ),
        ):
            await service._loop()

        service._wait_for_next_run.assert_awaited_once()  # type: ignore[attr-defined]
        db.rollback.assert_awaited_once()

    async def test_monitor_loop_skips_scheduled_sync_when_oauth_inventory_is_disabled(self) -> None:
        service = object.__new__(MonitorService)
        service._stop = asyncio.Event()
        service._wake = asyncio.Event()
        service.runtime_config = SimpleNamespace(
            get_automation_paused=AsyncMock(return_value=False),
            get_oauth_account_sync_enabled=AsyncMock(return_value=False),
        )
        service.sync_once = AsyncMock()  # type: ignore[method-assign]
        service._wait_for_startup_delay = AsyncMock()  # type: ignore[method-assign]

        async def stop_after_iteration() -> None:
            service._stop.set()

        service._wait_for_next_run = AsyncMock(side_effect=stop_after_iteration)  # type: ignore[method-assign]

        await service._loop()

        service.sync_once.assert_not_awaited()  # type: ignore[attr-defined]
        service._wait_for_next_run.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_upsert_snapshot_never_persists_api_key_aliases(self) -> None:
        account = {
            "id": 7,
            "email": "oauth@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {
                "api_key": "first-secret",
                "nested": {"apiKey": "second-secret", "apikey": "third-secret"},
            },
        }
        service = object.__new__(MonitorService)
        service.sub2api = Sub2ApiClient()
        service._sync_phone_from_account_note = AsyncMock()  # type: ignore[method-assign]
        db = SimpleNamespace(
            scalar=AsyncMock(return_value=None),
            add=MagicMock(),
            commit=AsyncMock(),
        )

        with patch("app.services.monitor.AsyncSessionLocal", return_value=_SessionContext(db)):
            await service._upsert_snapshot(
                "oauth@example.com",
                account,
                is_error=False,
                is_deactive=False,
            )

        snapshot = db.add.call_args.args[0]
        self.assertEqual(snapshot.raw["credentials"]["api_key"], "***redacted***")
        self.assertEqual(
            snapshot.raw["credentials"]["nested"],
            {"apiKey": "***redacted***", "apikey": "***redacted***"},
        )
        serialized = str(snapshot.raw)
        self.assertNotIn("first-secret", serialized)
        self.assertNotIn("second-secret", serialized)
        self.assertNotIn("third-secret", serialized)


if __name__ == "__main__":
    unittest.main()
