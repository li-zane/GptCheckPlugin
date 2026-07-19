import asyncio
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.crypto import encrypt_text
from app.services.chatgpt_account import (
    ChatGptAccountCheckError,
    _chatgpt_account_id_from_token,
    _subscription_from_response,
)
from app.services.sub2api import Sub2ApiClient
from app.services.subscription_refresh import refresh_subscriptions


class SubscriptionRefreshTests(unittest.IsolatedAsyncioTestCase):
    def test_subscription_response_maps_active_until_and_renewal(self) -> None:
        metadata = _subscription_from_response(
            200,
            json.dumps(
                {
                    "id": "subscription-id",
                    "plan_type": "plus",
                    "active_until": "2099-08-17T12:09:09Z",
                    "will_renew": True,
                }
            ),
        )

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata["subscription_starts_at"], "2099-07-17T12:09:09Z")
        self.assertEqual(metadata["subscription_expires_at"], "2099-08-17T12:09:09Z")
        self.assertEqual(metadata["subscription_renews_at"], "2099-08-17T12:09:09Z")
        self.assertEqual(metadata["subscription_billing_period"], "monthly")
        self.assertTrue(metadata["has_active_subscription"])

    def test_subscription_account_id_accepts_poid_claim(self) -> None:
        payload = {
            "https://api.openai.com/auth": {
                "poid": "workspace-from-poid",
            }
        }
        encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")

        self.assertEqual(
            _chatgpt_account_id_from_token(f"header.{encoded}.signature"),
            "workspace-from-poid",
        )

    async def test_partial_remote_expiry_uses_saved_access_token_for_enrichment(self) -> None:
        account = {
            "id": 17,
            "email": "plus@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {
                "plan_type": "plus",
                "subscription_expires_at": "2026-08-17T12:06:03Z",
            },
        }
        snapshot = SimpleNamespace(
            subscription_starts_at=None,
            subscription_expires_at="2026-08-17T12:06:03Z",
            encrypted_openai_access_token=encrypt_text("saved-access-token"),
        )
        sub2api = Sub2ApiClient()
        sub2api.check_openai_account_status = AsyncMock(return_value={})  # type: ignore[method-assign]
        checker = SimpleNamespace(
            check=AsyncMock(
                return_value=SimpleNamespace(
                    deactive=False,
                    session={
                        "plan_type": "plus",
                        "subscription_starts_at": "2026-07-17T06:06:03Z",
                        "subscription_expires_at": "2026-08-17T12:06:03Z",
                        "subscription_renews_at": "2026-08-17T06:06:03Z",
                        "subscription_billing_period": "monthly",
                        "has_active_subscription": True,
                    },
                )
            )
        )
        save = AsyncMock()

        with (
            patch("app.services.subscription_refresh.Sub2ApiClient", return_value=sub2api),
            patch("app.services.subscription_refresh.ChatGptAccountStatusChecker", return_value=checker),
            patch(
                "app.services.subscription_refresh._load_snapshots",
                new=AsyncMock(return_value={"plus@example.com": snapshot}),
            ),
            patch("app.services.subscription_refresh._load_mailboxes", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._save_subscription_metadata", new=save),
        ):
            result = await refresh_subscriptions(accounts=[account])

        self.assertEqual(result["refreshed"], 1)
        checker.check.assert_awaited_once_with("saved-access-token")
        metadata = save.await_args.args[3]
        self.assertEqual(metadata["subscription_starts_at"], "2026-07-17T06:06:03Z")
        self.assertEqual(metadata["subscription_billing_period"], "monthly")
        self.assertEqual(metadata["subscription_renews_at"], "2026-08-17T06:06:03Z")
        self.assertTrue(metadata["has_active_subscription"])
        sub2api.check_openai_account_status.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_k12_without_consumer_billing_dates_still_refreshes_plan(self) -> None:
        account = {
            "id": 18,
            "email": "student@example.edu",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {"access_token": "k12-access-token", "plan_type": "k12"},
        }
        sub2api = Sub2ApiClient()
        sub2api.check_openai_account_status = AsyncMock(return_value={})  # type: ignore[method-assign]
        checker = SimpleNamespace(
            check=AsyncMock(
                return_value=SimpleNamespace(
                    deactive=False,
                    session={
                        "plan_type": "k12",
                        "subscription_plan": "chatgptfreeplan",
                        "has_active_subscription": False,
                    },
                )
            )
        )
        save = AsyncMock()

        with (
            patch("app.services.subscription_refresh.Sub2ApiClient", return_value=sub2api),
            patch("app.services.subscription_refresh.ChatGptAccountStatusChecker", return_value=checker),
            patch("app.services.subscription_refresh._load_snapshots", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._load_mailboxes", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._save_subscription_metadata", new=save),
        ):
            result = await refresh_subscriptions(accounts=[account])

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["no_subscription_fields"], 0)
        metadata = save.await_args.args[3]
        self.assertEqual(metadata["plan_type"], "k12")
        self.assertFalse(metadata["has_active_subscription"])

    async def test_partial_plus_expiry_is_completed_when_direct_check_is_blocked(self) -> None:
        account = {
            "id": 19,
            "email": "fallback-plus@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {
                "plan_type": "plus",
                "subscription_expires_at": "2099-08-17T12:09:09Z",
            },
        }
        snapshot = SimpleNamespace(
            subscription_starts_at=None,
            subscription_expires_at="2099-08-17T12:09:09Z",
            encrypted_openai_access_token=encrypt_text("blocked-access-token"),
        )
        sub2api = Sub2ApiClient()
        sub2api.check_openai_account_status = AsyncMock(return_value={})  # type: ignore[method-assign]
        checker = SimpleNamespace(
            check=AsyncMock(side_effect=ChatGptAccountCheckError("blocked with HTTP 403"))
        )
        save = AsyncMock()

        with (
            patch("app.services.subscription_refresh.Sub2ApiClient", return_value=sub2api),
            patch("app.services.subscription_refresh.ChatGptAccountStatusChecker", return_value=checker),
            patch(
                "app.services.subscription_refresh._load_snapshots",
                new=AsyncMock(return_value={"fallback-plus@example.com": snapshot}),
            ),
            patch("app.services.subscription_refresh._load_mailboxes", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._save_subscription_metadata", new=save),
        ):
            result = await refresh_subscriptions(accounts=[account])

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["failed"], 0)
        metadata = save.await_args.args[3]
        self.assertEqual(metadata["subscription_starts_at"], "2099-07-17T12:09:09Z")
        self.assertEqual(metadata["subscription_billing_period"], "monthly")
        self.assertTrue(metadata["has_active_subscription"])
        self.assertIsNone(metadata["subscription_renews_at"])
        sub2api.check_openai_account_status.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_provided_exported_account_uses_access_token_without_relisting(self) -> None:
        account = {
            "id": 17,
            "email": "oauth@example.com",
            "platform": "openai",
            "type": "oauth",
            "status": "active",
            "schedulable": True,
            "credentials": {"access_token": "exported-access-token"},
        }
        sub2api = Sub2ApiClient()
        sub2api.list_accounts = AsyncMock(side_effect=AssertionError("must not relist"))  # type: ignore[method-assign]
        sub2api.check_openai_account_status = AsyncMock(return_value={})  # type: ignore[method-assign]
        checker = SimpleNamespace(
            check=AsyncMock(
                return_value=SimpleNamespace(
                    deactive=False,
                    session={
                        "plan_type": "team",
                        "subscription_starts_at": "2026-07-01T00:00:00Z",
                        "subscription_expires_at": "2026-08-01T00:00:00Z",
                    },
                )
            )
        )
        save = AsyncMock()

        with (
            patch("app.services.subscription_refresh.Sub2ApiClient", return_value=sub2api),
            patch("app.services.subscription_refresh.ChatGptAccountStatusChecker", return_value=checker),
            patch("app.services.subscription_refresh._load_snapshots", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._load_mailboxes", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._save_subscription_metadata", new=save),
        ):
            result = await refresh_subscriptions(
                protocol_limit=0,
                max_concurrency=1,
                accounts=[account],
            )

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["failed"], 0)
        checker.check.assert_awaited_once_with("exported-access-token")
        sub2api.list_accounts.assert_not_awaited()  # type: ignore[attr-defined]
        save.assert_awaited_once()
        metadata = save.await_args.args[3]
        self.assertEqual(metadata["plan_type"], "team")
        self.assertEqual(metadata["subscription_expires_at"], "2026-08-01T00:00:00Z")

    async def test_zero_concurrency_limit_runs_all_eligible_accounts_in_parallel(self) -> None:
        accounts = [
            {
                "id": account_id,
                "email": f"oauth-{account_id}@example.com",
                "platform": "openai",
                "type": "oauth",
                "status": "active",
                "schedulable": True,
                "credentials": {"access_token": f"access-{account_id}"},
            }
            for account_id in (17, 18, 19)
        ]
        sub2api = Sub2ApiClient()
        sub2api.check_openai_account_status = AsyncMock(return_value={})  # type: ignore[method-assign]
        all_started = asyncio.Event()
        active = 0
        max_active = 0

        async def check(_access_token: str):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == len(accounts):
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
            active -= 1
            return SimpleNamespace(
                deactive=False,
                session={
                    "plan_type": "plus",
                    "subscription_expires_at": "2026-08-01T00:00:00Z",
                },
            )

        checker = SimpleNamespace(check=AsyncMock(side_effect=check))

        with (
            patch("app.services.subscription_refresh.Sub2ApiClient", return_value=sub2api),
            patch("app.services.subscription_refresh.ChatGptAccountStatusChecker", return_value=checker),
            patch("app.services.subscription_refresh._load_snapshots", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._load_mailboxes", new=AsyncMock(return_value={})),
            patch("app.services.subscription_refresh._save_subscription_metadata", new=AsyncMock()),
        ):
            result = await refresh_subscriptions(
                protocol_limit=0,
                max_concurrency=0,
                accounts=accounts,
            )

        self.assertEqual(result["refreshed"], len(accounts))
        self.assertEqual(result["failed"], 0)
        self.assertEqual(max_active, len(accounts))


if __name__ == "__main__":
    unittest.main()
