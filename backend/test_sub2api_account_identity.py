from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.services.sub2api import (
    Sub2ApiClient,
    Sub2ApiRequestError,
    redact_sub2api_error_text,
    sanitize_payload,
)


class Sub2ApiAccountIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Sub2ApiClient()

    def test_account_name_prefers_sub2api_top_level_name(self) -> None:
        account = {
            "name": "School Account 12",
            "profile": {"name": "Profile Name", "email": "student@example.edu"},
        }

        self.assertEqual(self.client.account_name(account), "School Account 12")
        self.assertEqual(self.client.account_email(account), "student@example.edu")

    def test_account_name_trims_and_supports_known_fallback_paths(self) -> None:
        self.assertEqual(self.client.account_name({"account_name": "  OAuth Account  "}), "OAuth Account")
        self.assertEqual(self.client.account_name({"profile": {"name": "Student Profile"}}), "Student Profile")

    def test_account_name_does_not_fall_back_to_email_implicitly(self) -> None:
        account = {"credentials": {"email": "student@example.edu"}}

        self.assertIsNone(self.client.account_name(account))
        self.assertEqual(self.client.account_email(account), "student@example.edu")

    def test_account_platform_supports_nested_credential_metadata(self) -> None:
        self.assertEqual(
            self.client.account_platform({"credentials": {"provider": "gemini"}}),
            "gemini",
        )
        self.assertEqual(
            self.client.account_platform(
                {"platform": "anthropic", "credentials": {"platform": "openai"}}
            ),
            "anthropic",
        )

    def test_account_platform_ignores_structured_metadata_and_blank_values(self) -> None:
        self.assertEqual(
            self.client.account_platform(
                {"platform": " ", "credentials": {"provider": " gemini "}}
            ),
            "gemini",
        )
        self.assertIsNone(
            self.client.account_platform(
                {"credentials": {"provider": {"api_key": "synthetic-marker"}}}
            )
        )

    def test_oauth_detection_excludes_api_key_accounts(self) -> None:
        oauth = {
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "oauth@example.com", "refresh_token": "redacted"},
        }
        api_key = {
            "platform": "openai",
            "type": "api_key",
            "credentials": {"email": "key@example.com", "api_key": "redacted"},
        }

        self.assertTrue(self.client.is_gpt_account(oauth))
        self.assertTrue(self.client.is_oauth_account(oauth))
        self.assertTrue(self.client.is_gpt_account(api_key))
        self.assertFalse(self.client.is_oauth_account(api_key))

    def test_account_error_status_code_reads_explicit_and_message_codes(self) -> None:
        self.assertEqual(
            self.client.account_error_status_code({"error": {"status": 429}}),
            429,
        )
        self.assertEqual(
            self.client.account_error_status_code(
                {"error_message": "Authentication failed (401): token invalidated"}
            ),
            401,
        )
        self.assertEqual(
            self.client.account_error_status_code(
                {"error_message": '{"error":{"code":"token_invalidated"},"status": 401}'}
            ),
            401,
        )

    def test_account_error_status_code_ignores_non_http_business_codes(self) -> None:
        self.assertIsNone(
            self.client.account_error_status_code(
                {
                    "error_code": "token_invalidated",
                    "error_message": "Authentication token is invalid",
                }
            )
        )

    def test_error_redactor_covers_headers_fields_queries_and_known_credentials(self) -> None:
        secrets = {
            "basic-secret",
            "proxy-secret",
            "x-api-secret",
            "cookie-secret",
            "set-cookie-secret",
            "query-secret",
            "json secret with spaces",
            "odd-known-credential",
        }
        raw = (
            "Authorization: Basic basic-secret trailing material\n"
            "Proxy-Authorization: Bearer proxy-secret trailing material\n"
            "X-API-Key: x-api-secret trailing material\n"
            "Cookie: session=cookie-secret; preference=visible\n"
            "Set-Cookie: session=set-cookie-secret; HttpOnly\n"
            "https://example.test/path?access_token=query-secret&visible=yes\n"
            '{"apiKey":"json secret with spaces","visible":"yes"}\n'
            "opaque wrapper odd-known-credential"
        )

        redacted = redact_sub2api_error_text(
            raw,
            known_credentials=("odd-known-credential",),
            limit=2_000,
        )

        for secret in secrets:
            self.assertNotIn(secret, redacted)
        self.assertIn("visible=yes", redacted)
        self.assertIn("***redacted***", redacted)

    def test_error_redactor_covers_limited_provider_api_key_phrases(self) -> None:
        raw = (
            "Incorrect API key provided: provider-secret\n"
            "API key is: second-provider-secret\n"
            "API key maybe: ordinary-diagnostic"
        )

        redacted = redact_sub2api_error_text(raw, limit=2_000)

        self.assertNotIn("provider-secret", redacted)
        self.assertNotIn("second-provider-secret", redacted)
        self.assertIn("API key maybe: ordinary-diagnostic", redacted)
        self.assertEqual(redacted.count("***redacted***"), 2)

    def test_snapshot_sanitizer_redacts_api_key_aliases_recursively(self) -> None:
        raw = {
            "api_key": "first-secret",
            "nested": {
                "apiKey": "second-secret",
                "deeper": [{"apikey": "third-secret"}, {"x-api-key": "fourth-secret"}],
            },
            "visible": "keep-me",
        }

        sanitized = sanitize_payload(raw)

        self.assertEqual(sanitized["api_key"], "***redacted***")
        self.assertEqual(sanitized["nested"]["apiKey"], "***redacted***")
        self.assertEqual(sanitized["nested"]["deeper"][0]["apikey"], "***redacted***")
        self.assertEqual(sanitized["nested"]["deeper"][1]["x-api-key"], "***redacted***")
        self.assertEqual(sanitized["visible"], "keep-me")

    def test_snapshot_sanitizer_redacts_error_strings_without_truncating_plain_values(self) -> None:
        ordinary_value = "ordinary diagnostic " * 80
        raw = {
            "error_message": "Incorrect API key provided: payload-secret",
            "nested": [
                {"message": "API key is: nested-payload-secret"},
                {"description": ordinary_value},
            ],
        }

        sanitized = sanitize_payload(raw)

        self.assertNotIn("payload-secret", sanitized["error_message"])
        self.assertNotIn("nested-payload-secret", sanitized["nested"][0]["message"])
        self.assertEqual(sanitized["nested"][1]["description"], ordinary_value)

    def test_account_error_message_is_bounded_and_redacted_before_schema_use(self) -> None:
        self.client._known_credentials.add("configured-credential")
        account = {
            "error_message": (
                "HTTP 401 X-API-Key: reflected-key\n"
                "opaque configured-credential "
                + ("diagnostic " * 200)
            )
        }

        message = self.client.account_error_message(account)

        self.assertIsNotNone(message)
        self.assertLessEqual(len(message or ""), 500)
        self.assertNotIn("reflected-key", message or "")
        self.assertNotIn("configured-credential", message or "")

    def test_account_error_message_redacts_provider_api_key_phrase(self) -> None:
        message = self.client.account_error_message(
            {"error_message": "Incorrect API key provided: account-provider-secret"}
        )

        self.assertIsNotNone(message)
        self.assertNotIn("account-provider-secret", message or "")
        self.assertIn("***redacted***", message or "")


class Sub2ApiSecurityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _config() -> SimpleNamespace:
        return SimpleNamespace(
            base_url="http://sub2api.test/api/v1",
            auth_token="current-config-secret",
            auth_header="X-API-Key",
            auth_scheme="",
            accounts_path="/admin/accounts",
        )

    @staticmethod
    def _api_key_account(
        account_id: int | None,
        name: str,
        base_url: str,
        api_key: str | None = None,
    ) -> dict:
        account = {
            "name": name,
            "platform": "openai",
            "type": "api_key",
            "credentials": {"base_url": base_url},
        }
        if account_id is not None:
            account["id"] = account_id
        if api_key is not None:
            account["credentials"]["api_key"] = api_key
        return account

    @staticmethod
    def _oauth_account(
        account_id: int | None,
        email: str,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        id_token: str | None = None,
    ) -> dict:
        credentials = {"email": email}
        if access_token is not None:
            credentials["access_token"] = access_token
        if refresh_token is not None:
            credentials["refresh_token"] = refresh_token
        if id_token is not None:
            credentials["id_token"] = id_token
        account = {
            "email": email,
            "name": email,
            "platform": "openai",
            "type": "oauth",
            "credentials": credentials,
        }
        if account_id is not None:
            account["id"] = account_id
        return account

    async def test_remote_error_body_is_never_copied_into_request_exception(self) -> None:
        reflected = "raw-body-secret"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                502,
                text=f'upstream exploded: {{"api_key":"{reflected}"}} current-config-secret',
            )

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(Sub2ApiRequestError) as raised:
            await client._request("GET", "/admin/accounts", config=self._config())

        message = str(raised.exception)
        self.assertIn("HTTP 502", message)
        self.assertNotIn("upstream exploded", message)
        self.assertNotIn(reflected, message)
        self.assertNotIn("current-config-secret", message)

    async def test_non_json_response_diagnostic_does_not_copy_remote_body(self) -> None:
        reflected = "non-json-body-secret"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=f"not json {reflected}")

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(Sub2ApiRequestError) as raised:
            await client._request("GET", "/admin/accounts", config=self._config())

        self.assertIn("non-JSON", str(raised.exception))
        self.assertNotIn(reflected, str(raised.exception))

    async def test_request_total_deadline_stops_a_slow_response_stream(self) -> None:
        class SlowJsonStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in (b'{"secret":"', b"slow-stream-secret", b'","ok":true}'):
                    await asyncio.sleep(0.02)
                    yield chunk

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=SlowJsonStream())

        client = Sub2ApiClient(transport=httpx.MockTransport(handler))
        with (
            patch("app.services.sub2api.SUB2API_REQUEST_TOTAL_TIMEOUT_SECONDS", 0.03),
            self.assertRaises(Sub2ApiRequestError) as raised,
        ):
            await asyncio.wait_for(
                client._request("GET", "/admin/accounts", config=self._config()),
                timeout=0.5,
            )

        self.assertEqual(str(raised.exception), "sub2api request timed out.")
        self.assertNotIn("slow-stream-secret", str(raised.exception))

    async def test_request_total_deadline_preserves_normal_json_response(self) -> None:
        payload = {"data": {"accounts": [{"id": 17, "name": "normal"}]}}
        client = Sub2ApiClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=payload)
            )
        )

        result = await client._request("GET", "/admin/accounts", config=self._config())

        self.assertEqual(result, payload)

    async def test_single_id_exports_bind_stable_accounts_without_platform_or_url_matching(self) -> None:
        before_alpha = self._api_key_account(17, "alpha", "https://list-alpha.example")
        before_beta = self._api_key_account(18, "beta", "https://list-beta.example")
        exported_alpha = self._api_key_account(
            None,
            "alpha",
            "https://export-alpha.example/v1",
            "key-alpha",
        )
        exported_alpha["platform"] = "custom"
        exported_beta = self._api_key_account(
            None,
            "beta",
            "https://export-beta.example/v1",
            "key-beta",
        )
        client = Sub2ApiClient()
        client.get_account_by_id = AsyncMock(
            side_effect=[before_alpha, before_alpha, before_beta, before_beta]
        )
        client._request = AsyncMock(
            side_effect=[
                {"data": {"accounts": [exported_alpha]}},
                {"data": {"accounts": [exported_beta]}},
            ]
        )
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=self._config()))

        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            result = await client.export_api_key_secrets([17, 18])

        self.assertEqual(result, {17: "key-alpha", 18: "key-beta"})
        self.assertEqual(
            [item.args[0] for item in client.get_account_by_id.await_args_list],
            [17, 17, 18, 18],
        )
        self.assertEqual(
            [item.kwargs["params"]["ids"] for item in client._request.await_args_list],
            ["17", "18"],
        )

    async def test_single_id_export_rejects_zero_or_multiple_rows(self) -> None:
        account = self._api_key_account(17, "alpha", "https://alpha.example")
        valid_export = self._api_key_account(
            None,
            "alpha",
            "https://alpha.example",
            "key-alpha",
        )
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=self._config()))

        for exported in ([], [valid_export, valid_export]):
            client = Sub2ApiClient()
            client.get_account_by_id = AsyncMock(return_value=account)
            client._request = AsyncMock(return_value={"data": {"accounts": exported}})
            with (
                self.subTest(exported_count=len(exported)),
                patch("app.services.sub2api.get_runtime_config_service", return_value=runtime),
            ):
                result = await client.export_api_key_secrets([17])
            self.assertEqual(result, {})
            self.assertEqual(client.get_account_by_id.await_count, 1)

    async def test_single_id_export_rejects_changed_or_mismatched_identity(self) -> None:
        before = self._api_key_account(17, "alpha", "https://alpha.example")
        changed = self._api_key_account(17, "renamed", "https://alpha.example")
        matching_export = self._api_key_account(
            None,
            "alpha",
            "https://alpha.example",
            "key-alpha",
        )
        mismatched_export = self._api_key_account(
            None,
            "other",
            "https://alpha.example",
            "key-alpha",
        )
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=self._config()))

        cases = (
            ([before], mismatched_export),
            ([before, changed], matching_export),
        )
        for account_reads, exported in cases:
            client = Sub2ApiClient()
            client.get_account_by_id = AsyncMock(side_effect=account_reads)
            client._request = AsyncMock(return_value={"data": {"accounts": [exported]}})
            with (
                self.subTest(read_count=len(account_reads)),
                patch("app.services.sub2api.get_runtime_config_service", return_value=runtime),
            ):
                result = await client.export_api_key_secrets([17])
            self.assertEqual(result, {})

    async def test_single_id_export_rejects_missing_redacted_or_mismatched_id_key(self) -> None:
        account = self._api_key_account(17, "alpha", "https://alpha.example")
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=self._config()))
        exports = (
            self._api_key_account(None, "alpha", "https://alpha.example"),
            self._api_key_account(None, "alpha", "https://alpha.example", "***redacted***"),
            {
                **self._api_key_account(None, "alpha", "https://alpha.example", "key-alpha"),
                "id": 18,
            },
        )

        for exported in exports:
            client = Sub2ApiClient()
            client.get_account_by_id = AsyncMock(return_value=account)
            client._request = AsyncMock(return_value={"data": {"accounts": [exported]}})
            with (
                self.subTest(exported_id=exported.get("id")),
                patch("app.services.sub2api.get_runtime_config_service", return_value=runtime),
            ):
                result = await client.export_api_key_secrets([17])
            self.assertEqual(result, {})

    async def test_explicit_id_oauth_export_returns_only_allowlisted_credentials(self) -> None:
        client = Sub2ApiClient()
        client._request = AsyncMock(
            return_value={
                "data": [
                    {
                        **self._oauth_account(
                            17,
                            "alpha@example.com",
                            access_token="access-alpha",
                            refresh_token="refresh-alpha",
                            id_token="id-alpha",
                        ),
                        "credentials": {
                            "email": "alpha@example.com",
                            "access_token": "access-alpha",
                            "refresh_token": "refresh-alpha",
                            "id_token": "id-alpha",
                            "client_id": "client-alpha",
                            "password": "must-not-return",
                        },
                    }
                ]
            }
        )
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=self._config()))
        client.list_accounts = AsyncMock(side_effect=AssertionError("must not relist"))

        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            result = await client.export_oauth_credentials([17])

        self.assertEqual(
            result,
            {
                17: {
                    "access_token": "access-alpha",
                    "refresh_token": "refresh-alpha",
                    "id_token": "id-alpha",
                    "client_id": "client-alpha",
                }
            },
        )
        self.assertNotIn("password", result[17])
        client.list_accounts.assert_not_awaited()

    async def test_idless_oauth_export_maps_by_stable_unique_email(self) -> None:
        inventory = [
            self._oauth_account(17, "alpha@example.com"),
            self._oauth_account(18, "beta@example.com"),
        ]
        probe = [
            self._oauth_account(None, "alpha@example.com", access_token="probe-alpha"),
            self._oauth_account(None, "beta@example.com", access_token="probe-beta"),
        ]
        accepted = [
            self._oauth_account(
                None,
                "beta@example.com",
                access_token="access-beta",
                refresh_token="refresh-beta",
            ),
            self._oauth_account(None, "alpha@example.com", access_token="access-alpha"),
        ]
        client = Sub2ApiClient()
        client.list_accounts = AsyncMock(side_effect=[inventory, list(reversed(inventory))])
        client._request = AsyncMock(
            side_effect=[
                {"data": {"accounts": probe}},
                {"data": {"accounts": accepted}},
            ]
        )
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=self._config()))

        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            result = await client.export_oauth_credentials([17, 18])

        self.assertEqual(result[17]["access_token"], "access-alpha")
        self.assertEqual(result[18]["access_token"], "access-beta")
        self.assertEqual(result[18]["refresh_token"], "refresh-beta")
        self.assertNotIn("probe-alpha", str(result))
        self.assertEqual(client._request.await_count, 2)

    async def test_idless_oauth_export_rejects_inventory_email_rebind(self) -> None:
        before = [self._oauth_account(17, "alpha@example.com")]
        after = [self._oauth_account(17, "different@example.com")]
        exported = [
            self._oauth_account(None, "alpha@example.com", access_token="access-alpha")
        ]
        client = Sub2ApiClient()
        client.list_accounts = AsyncMock(side_effect=[before, after])
        client._request = AsyncMock(return_value={"data": {"accounts": exported}})
        runtime = SimpleNamespace(get_sub2api_config=AsyncMock(return_value=self._config()))

        with patch("app.services.sub2api.get_runtime_config_service", return_value=runtime):
            result = await client.export_oauth_credentials([17])

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
