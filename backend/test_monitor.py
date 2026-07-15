from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

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
            {"oauth@example.com", "api-key@example.com"}
        )
        record.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
