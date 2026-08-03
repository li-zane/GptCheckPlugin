from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from pydantic import ValidationError

from app.core.crypto import decrypt_text, encrypt_text
from app.models import AccountSnapshot
from app.schemas import AppSettingsUpdate
from app.services.monitor import MonitorService
from app.services.openai_oauth import OpenAiOAuthOutcome
from app.services.refresh import (
    OAUTH_PHONE_VERIFICATION_STOPPED_REASON,
    RefreshService,
)
from app.services.runtime_config import RuntimeConfigService
from app.services.sub2api import Sub2ApiClient


class RefreshStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, *, oauth_login_mode: str, stop_on_phone: bool) -> RefreshService:
        service = object.__new__(RefreshService)
        service.settings = SimpleNamespace(
            verification_code_timeout_seconds=1,
            verification_code_lookup_grace_seconds=30,
            verification_code_poll_seconds=1,
            openai_oauth_client_id="client-id",
        )
        service.runtime_config = SimpleNamespace(
            get_recovery_enabled=AsyncMock(return_value=True),
            get_oauth_login_mode=AsyncMock(return_value=oauth_login_mode),
            get_oauth_stop_on_phone_verification=AsyncMock(return_value=stop_on_phone),
        )
        service._manual_jobs = set()
        service._load_credential = AsyncMock(
            return_value=SimpleNamespace(id=7, disabled=False)
        )
        service._mark_started = AsyncMock()
        service._mail_success = AsyncMock()
        service._mail_error = AsyncMock()
        service.mail = SimpleNamespace(fetch_code=AsyncMock())
        service._handle_oauth_outcome = AsyncMock()
        service._finish_oauth_phone_verification_stopped = AsyncMock()
        service._record_openai_oauth_warning = AsyncMock()
        service._finish = AsyncMock()
        service._latest_protocol_failure_summary = AsyncMock(return_value=None)
        service._try_access_token_status_refresh = AsyncMock()
        return service

    async def test_persisted_refresh_token_failure_goes_directly_to_selected_oauth(self) -> None:
        service = self._service(oauth_login_mode="protocol", stop_on_phone=False)
        calls: list[str] = []

        async def fail_refresh_token(*_args, **_kwargs) -> bool:
            calls.append("refresh_token")
            return False

        async def run_protocol_oauth(*_args, **_kwargs) -> OpenAiOAuthOutcome:
            calls.append("oauth_protocol")
            return OpenAiOAuthOutcome(
                status="ok",
                access_token="new-at",
                refresh_token="new-rt",
            )

        service._load_local_openai_tokens = AsyncMock(
            return_value={"refresh_token": "persisted-rt", "access_token": "old-at"}
        )
        service._try_protocol_slot_refresh = AsyncMock(side_effect=fail_refresh_token)
        service._oauth_with_protocol_slot = AsyncMock(side_effect=run_protocol_oauth)
        service._oauth_with_browser_slot = AsyncMock()

        account = {"id": 9, "email": "oauth@example.com", "type": "oauth"}
        await service._run_inner(3, "oauth@example.com", account)

        self.assertEqual(calls, ["refresh_token", "oauth_protocol"])
        service._try_access_token_status_refresh.assert_not_awaited()
        service._oauth_with_browser_slot.assert_not_awaited()
        self.assertEqual(
            service._oauth_with_protocol_slot.await_args.args[1],
            "oauth@example.com",
        )
        service._handle_oauth_outcome.assert_awaited_once()

    async def test_browser_mode_uses_only_headless_oauth_and_forwards_phone_switch(self) -> None:
        service = self._service(oauth_login_mode="browser", stop_on_phone=True)
        service._load_local_openai_tokens = AsyncMock(return_value={})
        service._try_protocol_slot_refresh = AsyncMock()
        service._oauth_with_protocol_slot = AsyncMock()
        service._oauth_with_browser_slot = AsyncMock(
            return_value=OpenAiOAuthOutcome(
                status="failed",
                error="phone verification required",
            )
        )

        account = {"id": 9, "email": "oauth@example.com", "type": "oauth"}
        await service._run_inner(4, "oauth@example.com", account)

        service._try_protocol_slot_refresh.assert_not_awaited()
        service._oauth_with_protocol_slot.assert_not_awaited()
        service._oauth_with_browser_slot.assert_awaited_once()
        self.assertTrue(
            service._oauth_with_browser_slot.await_args.kwargs[
                "stop_on_phone_verification"
            ]
        )
        service._finish_oauth_phone_verification_stopped.assert_awaited_once_with(
            4,
            "oauth@example.com",
            "browser",
            "phone verification required",
        )

    async def test_protocol_edge_verification_automatically_falls_back_to_browser(self) -> None:
        service = self._service(oauth_login_mode="protocol", stop_on_phone=False)
        service._load_local_openai_tokens = AsyncMock(return_value={})
        service._try_protocol_slot_refresh = AsyncMock()
        service._oauth_with_protocol_slot = AsyncMock(
            return_value=OpenAiOAuthOutcome(
                status="failed",
                error="oauth_protocol_edge_verification_blocked: authorization start returned HTTP 403",
            )
        )
        service._oauth_with_browser_slot = AsyncMock(
            return_value=OpenAiOAuthOutcome(
                status="ok",
                access_token="new-at",
                refresh_token="new-rt",
            )
        )

        account = {"id": 9, "email": "oauth@example.com", "type": "oauth"}
        await service._run_inner(5, "oauth@example.com", account)

        service._oauth_with_protocol_slot.assert_awaited_once()
        service._oauth_with_browser_slot.assert_awaited_once()
        service._record_openai_oauth_warning.assert_awaited_once_with(
            5,
            "oauth@example.com",
            "oauth_protocol_edge_verification_blocked: authorization start returned HTTP 403",
            will_fallback=True,
            fallback_target="headless browser",
        )
        self.assertEqual(service._handle_oauth_outcome.await_args.kwargs["source"], "browser")

    async def test_phone_verification_stop_records_stable_event_and_snapshot_reason(self) -> None:
        service = object.__new__(RefreshService)
        service._record_event_safely = AsyncMock()
        service._finish = AsyncMock()

        await service._finish_oauth_phone_verification_stopped(
            5,
            "oauth@example.com",
            "browser",
            "provider phone detail",
        )

        service._record_event_safely.assert_awaited_once()
        event_args = service._record_event_safely.await_args.args
        self.assertEqual(event_args[0], "oauth_phone_verification_stopped")
        self.assertEqual(event_args[1], OAUTH_PHONE_VERIFICATION_STOPPED_REASON)
        self.assertEqual(event_args[3]["reason_code"], "oauth_phone_verification_stopped")
        service._finish.assert_awaited_once_with(
            5,
            "oauth@example.com",
            "failed",
            OAUTH_PHONE_VERIFICATION_STOPPED_REASON,
        )

    async def test_any_persisted_refresh_token_failure_invalidates_local_token(self) -> None:
        service = object.__new__(RefreshService)
        service.settings = SimpleNamespace(openai_oauth_client_id="default-client-id")
        service.openai_tokens = SimpleNamespace(
            refresh_token=AsyncMock(side_effect=RuntimeError("temporary token endpoint error")),
            fetch_profile=AsyncMock(),
        )
        service._load_local_openai_tokens = AsyncMock(
            return_value={"refresh_token": "persisted-rt", "client_id": "synced-client-id"}
        )
        service._store_local_openai_tokens = AsyncMock()
        service._record_local_openai_refresh_token_warning = AsyncMock()

        refreshed = await service._try_local_openai_refresh_token(
            6,
            "oauth@example.com",
            {"id": 9},
        )

        self.assertFalse(refreshed)
        service.openai_tokens.refresh_token.assert_awaited_once_with(
            refresh_token="persisted-rt",
            client_id="synced-client-id",
        )
        service._store_local_openai_tokens.assert_awaited_once_with(
            "oauth@example.com",
            refresh_token=None,
        )
        service._record_local_openai_refresh_token_warning.assert_awaited_once_with(
            6,
            "oauth@example.com",
            "temporary token endpoint error",
            invalid=True,
        )


class OAuthRuntimeConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_oauth_settings_use_persisted_values_and_safe_defaults(self) -> None:
        service = object.__new__(RuntimeConfigService)
        service.settings = SimpleNamespace(
            oauth_login_mode="protocol",
            oauth_stop_on_phone_verification=False,
        )
        service._load_values = AsyncMock(
            return_value={
                "oauth_login_mode": "browser",
                "oauth_stop_on_phone_verification": "true",
            }
        )

        self.assertEqual(await service.get_oauth_login_mode(), "browser")
        self.assertTrue(await service.get_oauth_stop_on_phone_verification())

        service._load_values = AsyncMock(return_value={"oauth_login_mode": "unknown"})
        self.assertEqual(await service.get_oauth_login_mode(), "protocol")
        self.assertFalse(await service.get_oauth_stop_on_phone_verification())

    async def test_settings_schema_restricts_oauth_login_mode(self) -> None:
        self.assertEqual(
            AppSettingsUpdate(oauth_login_mode="browser").oauth_login_mode,
            "browser",
        )
        with self.assertRaises(ValidationError):
            AppSettingsUpdate(oauth_login_mode="fallback")


class SyncedCredentialPersistenceTests(unittest.TestCase):
    def test_sync_preserves_real_local_tokens_when_remote_values_are_redacted(self) -> None:
        service = object.__new__(MonitorService)
        service.sub2api = Sub2ApiClient()
        snapshot = AccountSnapshot(
            email="oauth@example.com",
            encrypted_openai_refresh_token=encrypt_text("real-local-rt"),
            encrypted_openai_access_token=encrypt_text("real-local-at"),
        )

        service._sync_oauth_credentials(
            snapshot,
            {
                "credentials": {
                    "refresh_token": "***redacted***",
                    "access_token": "redacted",
                }
            },
        )

        self.assertEqual(
            decrypt_text(snapshot.encrypted_openai_refresh_token),
            "real-local-rt",
        )
        self.assertEqual(
            decrypt_text(snapshot.encrypted_openai_access_token),
            "real-local-at",
        )

    def test_sync_encrypts_fresh_exported_tokens_in_local_snapshot(self) -> None:
        service = object.__new__(MonitorService)
        service.sub2api = Sub2ApiClient()
        snapshot = AccountSnapshot(email="oauth@example.com")

        service._sync_oauth_credentials(
            snapshot,
            {
                "credentials": {
                    "refresh_token": "fresh-rt",
                    "access_token": "fresh-at",
                    "id_token": "fresh-id",
                    "client_id": "fresh-client",
                }
            },
        )

        self.assertEqual(decrypt_text(snapshot.encrypted_openai_refresh_token), "fresh-rt")
        self.assertEqual(decrypt_text(snapshot.encrypted_openai_access_token), "fresh-at")
        self.assertEqual(decrypt_text(snapshot.encrypted_openai_id_token), "fresh-id")
        self.assertEqual(decrypt_text(snapshot.encrypted_openai_client_id), "fresh-client")


if __name__ == "__main__":
    unittest.main()
