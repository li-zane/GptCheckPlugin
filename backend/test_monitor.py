import asyncio
import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.monitor import MonitorService
from app.services.sub2api import Sub2ApiClient
from app.core.database import Base
from app.core.crypto import decrypt_text, encrypt_text
from app.models import AccountSnapshot, MailboxCredential


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


class MonitorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_remote_account_cleanup_deletes_snapshot_and_mailbox(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as db:
                db.add_all(
                    [
                        AccountSnapshot(email="keep@example.com"),
                        AccountSnapshot(email="stale@example.com"),
                        MailboxCredential(
                            gpt_email="keep@example.com",
                            mailbox_email="keep-mail@example.com",
                        ),
                        MailboxCredential(
                            gpt_email="stale@example.com",
                            mailbox_email="stale-mail@example.com",
                        ),
                    ]
                )
                await db.commit()
                service = object.__new__(MonitorService)

                deleted_accounts, deleted_mailboxes = await service._delete_missing_remote_accounts(
                    {"keep@example.com"},
                    db=db,
                )
                await db.commit()

                remaining_accounts = list((await db.execute(select(AccountSnapshot.email))).scalars())
                remaining_mailboxes = list((await db.execute(select(MailboxCredential.gpt_email))).scalars())
            self.assertEqual((deleted_accounts, deleted_mailboxes), (1, 1))
            self.assertEqual(remaining_accounts, ["keep@example.com"])
            self.assertEqual(remaining_mailboxes, ["keep@example.com"])
        finally:
            await engine.dispose()

    async def test_oauth_model_whitelist_syncs_when_missing_and_respects_force(self) -> None:
        snapshot = AccountSnapshot(
            email="oauth@example.com",
            sub2api_account_id="1",
            available_models=None,
        )
        account = {"id": 1, "email": "oauth@example.com"}
        service = object.__new__(MonitorService)
        service.sub2api = Sub2ApiClient()
        service.sub2api.get_account_models = AsyncMock(  # type: ignore[method-assign]
            return_value=[
                {"id": " model-a ", "display_name": " Model A "},
                {"id": "model-a", "display_name": "duplicate"},
                {"id": "model-b", "display_name": ""},
                {"id": "bad\nmodel", "display_name": "bad"},
            ]
        )
        result = MagicMock()
        result.scalars.return_value = [snapshot]
        db = AsyncMock()
        db.execute.return_value = result

        synchronized = await service._sync_oauth_available_models(
            db,
            [account],
            force=False,
        )

        self.assertEqual(synchronized, 1)
        self.assertEqual(
            snapshot.available_models,
            [
                {"id": "model-a", "display_name": "Model A"},
                {"id": "model-b", "display_name": "model-b"},
            ],
        )
        self.assertEqual(snapshot.available_models_status, "ok")
        service.sub2api.get_account_models.assert_awaited_once_with(account)  # type: ignore[attr-defined]

        service.sub2api.get_account_models.reset_mock()  # type: ignore[attr-defined]
        skipped = await service._sync_oauth_available_models(
            db,
            [account],
            force=False,
        )
        self.assertEqual(skipped, 0)
        service.sub2api.get_account_models.assert_not_awaited()  # type: ignore[attr-defined]

        service.sub2api.get_account_models.return_value = [  # type: ignore[attr-defined]
            {"id": "model-c", "display_name": "Model C"}
        ]
        refreshed = await service._sync_oauth_available_models(
            db,
            [account],
            force=True,
        )
        self.assertEqual(refreshed, 1)
        self.assertEqual(
            snapshot.available_models,
            [{"id": "model-c", "display_name": "Model C"}],
        )

    async def test_oauth_model_whitelist_failure_keeps_last_good_models(self) -> None:
        snapshot = AccountSnapshot(
            email="oauth@example.com",
            sub2api_account_id="1",
            available_models=[{"id": "model-a", "display_name": "Model A"}],
        )
        service = object.__new__(MonitorService)
        service.sub2api = Sub2ApiClient()
        service.sub2api.get_account_models = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("models unavailable")
        )
        result = MagicMock()
        result.scalars.return_value = [snapshot]
        db = AsyncMock()
        db.execute.return_value = result

        synchronized = await service._sync_oauth_available_models(
            db,
            [{"id": 1, "email": "oauth@example.com"}],
            force=True,
        )

        self.assertEqual(synchronized, 0)
        self.assertEqual(
            snapshot.available_models,
            [{"id": "model-a", "display_name": "Model A"}],
        )
        self.assertEqual(snapshot.available_models_status, "error")
        self.assertIsNotNone(snapshot.available_models_checked_at)

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
        sub2api.export_oauth_credentials = AsyncMock(return_value={})  # type: ignore[method-assign]
        service = object.__new__(MonitorService)
        service.sub2api = sub2api
        service.runtime_config = SimpleNamespace(
            get_recovery_enabled=AsyncMock(return_value=False)
        )
        service.refresh_service = SimpleNamespace()
        service._upsert_snapshot = AsyncMock(return_value=(False, False, False))  # type: ignore[method-assign]
        service._ensure_routed_mailbox = AsyncMock()  # type: ignore[method-assign]
        service._clear_account_exceptions = AsyncMock()  # type: ignore[method-assign]
        service._delete_missing_remote_accounts = AsyncMock(  # type: ignore[method-assign]
            return_value=(0, 0)
        )
        service._sync_oauth_available_models = AsyncMock(return_value=0)  # type: ignore[method-assign]
        service._dispatch_initial_subscription_refresh = MagicMock()  # type: ignore[method-assign]
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
        sub2api.export_oauth_credentials.assert_awaited_once_with([1])  # type: ignore[attr-defined]
        service._dispatch_initial_subscription_refresh.assert_called_once_with([])  # type: ignore[attr-defined]

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
        sub2api.export_oauth_credentials = AsyncMock(return_value={})  # type: ignore[method-assign]
        service = object.__new__(MonitorService)
        service.sub2api = sub2api
        service.runtime_config = SimpleNamespace(get_recovery_enabled=AsyncMock(return_value=False))
        service.refresh_service = SimpleNamespace()
        service._upsert_snapshot = AsyncMock(return_value=(False, False, False))  # type: ignore[method-assign]
        service._ensure_routed_mailbox = AsyncMock()  # type: ignore[method-assign]
        service._clear_account_exceptions = AsyncMock()  # type: ignore[method-assign]
        service._delete_missing_remote_accounts = AsyncMock(return_value=(0, 0))  # type: ignore[method-assign]
        service._sync_oauth_available_models = AsyncMock(return_value=0)  # type: ignore[method-assign]
        service._dispatch_initial_subscription_refresh = MagicMock()  # type: ignore[method-assign]
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

    async def test_upsert_snapshot_encrypts_exported_oauth_tokens_and_claims_subscription_once(self) -> None:
        access_claims = {
            "exp": 1_900_000_000,
            "https://api.openai.com/auth": {"chatgpt_plan_type": "team"},
        }
        encoded_claims = base64.urlsafe_b64encode(
            json.dumps(access_claims).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
        access_token = f"header.{encoded_claims}.signature"
        account = {
            "id": 7,
            "email": "oauth@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {
                "access_token": access_token,
                "refresh_token": "refresh-secret",
                "id_token": "id-secret",
                "client_id": "client-id",
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

        result = await service._upsert_snapshot(
            "oauth@example.com",
            account,
            is_error=False,
            is_deactive=False,
            db=db,
        )

        snapshot = db.add.call_args.args[0]
        self.assertEqual(result, (False, False, True))
        self.assertEqual(decrypt_text(snapshot.encrypted_openai_access_token), access_token)
        self.assertEqual(decrypt_text(snapshot.encrypted_openai_refresh_token), "refresh-secret")
        self.assertEqual(decrypt_text(snapshot.encrypted_openai_id_token), "id-secret")
        self.assertEqual(decrypt_text(snapshot.encrypted_openai_client_id), "client-id")
        self.assertEqual(snapshot.subscription_plan, "team")
        self.assertEqual(snapshot.openai_token_expires_at, datetime.fromtimestamp(1_900_000_000, tz=timezone.utc))
        self.assertIsNotNone(snapshot.subscription_checked_at)
        self.assertNotIn(access_token, str(snapshot.raw))

        first_checked_at = snapshot.subscription_checked_at
        snapshot.encrypted_openai_access_token = encrypt_text(access_token)
        db.scalar = AsyncMock(return_value=snapshot)
        repeated = await service._upsert_snapshot(
            "oauth@example.com",
            account,
            is_error=False,
            is_deactive=False,
            db=db,
        )

        self.assertEqual(repeated, (False, False, False))
        self.assertEqual(snapshot.subscription_checked_at, first_checked_at)

    async def test_upsert_snapshot_notifies_when_oauth_account_recovers(self) -> None:
        account = {
            "id": 7,
            "email": "oauth@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {},
        }
        snapshot = AccountSnapshot(
            email="oauth@example.com",
            deactive=True,
            last_error="account deactive",
        )
        service = object.__new__(MonitorService)
        service.sub2api = Sub2ApiClient()
        service._sync_phone_from_account_note = AsyncMock()  # type: ignore[method-assign]
        db = SimpleNamespace(scalar=AsyncMock(return_value=snapshot), add=MagicMock())

        with patch(
            "app.services.monitor.enqueue_oauth_account_enabled",
            new=AsyncMock(),
        ) as notify_enabled:
            await service._upsert_snapshot(
                "oauth@example.com",
                account,
                is_error=False,
                is_deactive=False,
                db=db,
            )

        self.assertFalse(snapshot.deactive)
        notify_enabled.assert_awaited_once_with(
            db,
            "oauth@example.com",
            "Sub2API reported the OAuth account as enabled again.",
            account_id="7",
        )

    async def test_initial_subscription_refresh_preserves_unlimited_concurrency(self) -> None:
        accounts = [{"id": 7, "email": "oauth@example.com"}]
        service = object.__new__(MonitorService)
        service.sub2api = Sub2ApiClient()
        service.runtime_config = SimpleNamespace(
            get_subscription_refresh_max_concurrency=AsyncMock(return_value=0)
        )
        refresh = AsyncMock(
            return_value={
                "message": "done",
                "total": 1,
                "refreshed": 1,
                "skipped": 0,
                "failed": 0,
            }
        )
        db = AsyncMock()

        with (
            patch("app.services.monitor.refresh_subscriptions", new=refresh),
            patch("app.services.monitor.AsyncSessionLocal", return_value=_SessionContext(db)),
            patch("app.services.monitor._record_event_best_effort", new=AsyncMock()),
        ):
            await service._refresh_initial_subscriptions(accounts)

        refresh.assert_awaited_once_with(
            protocol_limit=0,
            max_concurrency=0,
            accounts=accounts,
        )


if __name__ == "__main__":
    unittest.main()
