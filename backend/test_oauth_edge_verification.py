import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.openai_oauth import (
    OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE,
    OAuthContext,
    OpenAiOAuthOutcome,
    OpenAiOAuthRefresher,
    _is_protocol_edge_verification_response,
)
from app.services.refresh import RefreshService


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        text: str = "",
        headers: dict[str, str] | None = None,
        url: str = "https://auth.openai.com/log-in",
        payload: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _context() -> OAuthContext:
    return OAuthContext(
        state="expected-state",
        code_verifier="verifier",
        redirect_uri="http://localhost:1455/auth/callback",
        client_id="client-id",
        auth_url="https://auth.openai.com/oauth/authorize?state=expected-state",
    )


class OAuthEdgeVerificationClassificationTests(unittest.IsolatedAsyncioTestCase):
    def test_http_403_sentinel_response_has_stable_actionable_reason(self) -> None:
        response = FakeResponse(
            403,
            text='{"error":"sentinel challenge","token":"sensitive-response-token"}',
            headers={"x-openai-sentinel": "sensitive-header-token"},
        )

        outcome = OpenAiOAuthRefresher(SimpleNamespace())._http_error(
            "OpenAI OAuth email submission",
            response,
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertIn(OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE, outcome.error or "")
        self.assertIn("HTTP 403", outcome.error or "")
        self.assertIn("OPENAI_OAUTH_PROTOCOL_IMPERSONATES", outcome.error or "")
        self.assertNotIn("sensitive-response-token", outcome.error or "")
        self.assertNotIn("sensitive-header-token", outcome.error or "")

    def test_http_409_is_classified_even_with_opaque_body(self) -> None:
        outcome = OpenAiOAuthRefresher(SimpleNamespace())._http_error(
            "OpenAI OAuth OTP validation",
            FakeResponse(409, text="opaque conflict body with credential=secret-value"),
        )

        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertIn("HTTP 409", outcome.error or "")
        self.assertNotIn("secret-value", outcome.error or "")

    def test_non_edge_http_error_remains_generic_and_does_not_echo_body(self) -> None:
        outcome = OpenAiOAuthRefresher(SimpleNamespace())._http_error(
            "OpenAI OAuth OTP validation",
            FakeResponse(422, text="invalid code; submitted=123456; token=secret-value"),
        )

        self.assertEqual(outcome.status, "failed")
        self.assertIsNone(outcome.reason_code)
        self.assertEqual(outcome.error, "OpenAI OAuth OTP validation failed with HTTP 422.")

    def test_normal_cloudflare_proxy_headers_do_not_trigger_edge_block(self) -> None:
        response = FakeResponse(
            200,
            text='{"page":{"type":"email_otp_verification"}}',
            headers={"server": "cloudflare", "cf-ray": "request-id"},
        )

        self.assertFalse(_is_protocol_edge_verification_response(response))

    async def test_authorization_start_403_stops_before_email_submission(self) -> None:
        class FakeSession:
            get = AsyncMock(return_value=FakeResponse(403, text="Access denied"))
            post = AsyncMock()

        fetch_code = AsyncMock()
        outcome = await OpenAiOAuthRefresher(SimpleNamespace())._run_protocol_oauth(
            FakeSession(),
            _context(),
            "person@example.com",
            fetch_code,
        )

        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        FakeSession.post.assert_not_awaited()
        fetch_code.assert_not_awaited()

    async def test_email_submission_http_200_challenge_stops_before_otp_lookup(self) -> None:
        class FakeSession:
            get = AsyncMock(return_value=FakeResponse(200, payload={}))
            post = AsyncMock(
                return_value=FakeResponse(
                    200,
                    text="Cloudflare challenge-platform security verification",
                )
            )

        fetch_code = AsyncMock()
        outcome = await OpenAiOAuthRefresher(SimpleNamespace())._run_protocol_oauth(
            FakeSession(),
            _context(),
            "person@example.com",
            fetch_code,
        )

        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertIn("email submission", outcome.error or "")
        fetch_code.assert_not_awaited()

    async def test_otp_validation_http_200_challenge_is_classified(self) -> None:
        class FakeSession:
            get = AsyncMock(return_value=FakeResponse(200, payload={}))
            post = AsyncMock(
                side_effect=[
                    FakeResponse(
                        200,
                        payload={"page": {"type": "email_otp_verification"}},
                    ),
                    FakeResponse(
                        200,
                        text="OpenAI-Sentinel challenge required",
                    ),
                ]
            )

        fetch_code = AsyncMock(return_value="123456")
        outcome = await OpenAiOAuthRefresher(SimpleNamespace())._run_protocol_oauth(
            FakeSession(),
            _context(),
            "person@example.com",
            fetch_code,
        )

        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertIn("OTP validation", outcome.error or "")
        fetch_code.assert_awaited_once()

    async def test_http_200_cloudflare_page_is_classified_before_page_parsing(self) -> None:
        class FakeSession:
            get = AsyncMock(
                return_value=FakeResponse(
                    200,
                    text="Performing security verification. Verify you are not a bot.",
                    url="https://auth.openai.com/log-in",
                )
            )

        outcome = await OpenAiOAuthRefresher(SimpleNamespace())._follow_protocol_url(
            FakeSession(),
            "https://auth.openai.com/log-in",
            _context(),
        )

        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertIn("Cloudflare/Sentinel", outcome.error or "")

    async def test_codex_consent_selects_workspace_and_extracts_callback_code(self) -> None:
        consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        session = SimpleNamespace(
            get=AsyncMock(
                return_value=FakeResponse(
                    200,
                    text=(
                        '<form method="post" action="/sign-in-with-chatgpt/codex/consent">'
                        '<input type="hidden" name="workspace_id" value="workspace-id">'
                        '<button type="submit">Continue</button></form>'
                    ),
                    url=consent_url,
                )
            ),
            post=AsyncMock(
                return_value=FakeResponse(
                    302,
                    headers={
                        "location": (
                            "http://localhost:1455/auth/callback?"
                            "code=authorization-code&state=expected-state"
                        )
                    },
                    url=consent_url,
                )
            ),
        )

        outcome = await OpenAiOAuthRefresher(SimpleNamespace())._follow_protocol_url(
            session,
            consent_url,
            _context(),
        )

        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.access_token, "authorization-code")
        session.post.assert_awaited_once()
        self.assertEqual(
            session.post.await_args.args[0],
            "https://auth.openai.com/api/accounts/workspace/select",
        )
        self.assertEqual(session.post.await_args.kwargs["json"], {"workspace_id": "workspace-id"})

    async def test_codex_consent_form_rejects_untrusted_action(self) -> None:
        consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        session = SimpleNamespace(
            get=AsyncMock(
                return_value=FakeResponse(
                    200,
                    text=(
                        '<form method="post" action="https://example.com/sign-in-with-chatgpt/codex/consent">'
                        '<input type="hidden" name="workspace_id" value="workspace-id">'
                        "</form>"
                    ),
                    url=consent_url,
                )
            ),
            post=AsyncMock(),
        )

        outcome = await OpenAiOAuthRefresher(SimpleNamespace())._follow_protocol_url(
            session,
            consent_url,
            _context(),
        )

        self.assertEqual(outcome.status, "failed")
        session.post.assert_not_awaited()

    async def test_token_exchange_edge_block_does_not_echo_authorization_code(self) -> None:
        settings = SimpleNamespace(
            openai_oauth_token_url="https://auth.openai.com/oauth/token",
            openai_oauth_user_agent="test-agent",
        )
        response = FakeResponse(
            403,
            text="Cloudflare blocked authorization-code-secret",
            url=settings.openai_oauth_token_url,
        )
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = False
        client.post.return_value = response

        with patch("app.services.openai_oauth.httpx.AsyncClient", return_value=client):
            outcome = await OpenAiOAuthRefresher(settings)._exchange_code(
                _context(),
                "authorization-code-secret",
                "person@example.com",
            )

        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertNotIn("authorization-code-secret", outcome.error or "")

    async def test_refresh_event_logs_reason_code_and_protocol_fingerprint_guidance(self) -> None:
        service = object.__new__(RefreshService)
        service._record_event_safely = AsyncMock()
        edge_error = OpenAiOAuthRefresher(SimpleNamespace())._http_error(
            "OpenAI OAuth continuation",
            FakeResponse(403, text="sentinel secret-value"),
        ).error

        await service._record_openai_oauth_warning(
            17,
            "person@example.com",
            edge_error or "",
            will_fallback=False,
        )

        service._record_event_safely.assert_awaited_once()
        event_args = service._record_event_safely.await_args.args
        self.assertIn("OPENAI_OAUTH_PROTOCOL_IMPERSONATES", event_args[1])
        self.assertEqual(event_args[3]["reason_code"], OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertNotIn("secret-value", event_args[3]["error"])


class OAuthProtocolFingerprintSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorization_start_edge_block_tries_next_protocol_fingerprint(self) -> None:
        profiles: list[str] = []

        class RecordingSession:
            def __init__(self, *, impersonate: str, timeout: int) -> None:
                profiles.append(impersonate)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

        refresher = OpenAiOAuthRefresher(
            SimpleNamespace(openai_oauth_protocol_impersonates=["chrome136", "chrome142"])
        )
        start_edge = refresher._protocol_edge_blocked_outcome(
            "OpenAI OAuth authorization start",
            403,
        )
        next_outcome = OpenAiOAuthOutcome(status="failed", error="verification code pending")

        with (
            patch("curl_cffi.requests.AsyncSession", RecordingSession),
            patch.object(refresher, "_new_context", return_value=_context()),
            patch.object(
                refresher,
                "_run_protocol_oauth",
                new=AsyncMock(side_effect=[start_edge, next_outcome]),
            ),
        ):
            outcome = await refresher.refresh_with_protocol("person@example.com", AsyncMock())

        self.assertIs(outcome, next_outcome)
        self.assertEqual(profiles, ["chrome136", "chrome142"])

    async def test_later_edge_block_does_not_restart_oauth_with_another_fingerprint(self) -> None:
        profiles: list[str] = []

        class RecordingSession:
            def __init__(self, *, impersonate: str, timeout: int) -> None:
                profiles.append(impersonate)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

        refresher = OpenAiOAuthRefresher(
            SimpleNamespace(openai_oauth_protocol_impersonates=["chrome142", "safari184"])
        )
        email_edge = refresher._protocol_edge_blocked_outcome(
            "OpenAI OAuth email submission",
            403,
        )

        with (
            patch("curl_cffi.requests.AsyncSession", RecordingSession),
            patch.object(refresher, "_new_context", return_value=_context()),
            patch.object(refresher, "_run_protocol_oauth", new=AsyncMock(return_value=email_edge)),
        ):
            outcome = await refresher.refresh_with_protocol("person@example.com", AsyncMock())

        self.assertIs(outcome, email_edge)
        self.assertEqual(profiles, ["chrome142"])

    async def test_exhausted_start_profiles_report_every_attempt(self) -> None:
        class RecordingSession:
            def __init__(self, *, impersonate: str, timeout: int) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

        refresher = OpenAiOAuthRefresher(
            SimpleNamespace(openai_oauth_protocol_impersonates=["chrome136", "chrome145"])
        )
        outcomes = [
            refresher._protocol_edge_blocked_outcome("OpenAI OAuth authorization start", 403),
            refresher._protocol_edge_blocked_outcome("OpenAI OAuth authorization start", 403),
        ]

        with (
            patch("curl_cffi.requests.AsyncSession", RecordingSession),
            patch.object(refresher, "_new_context", return_value=_context()),
            patch.object(refresher, "_run_protocol_oauth", new=AsyncMock(side_effect=outcomes)),
        ):
            outcome = await refresher.refresh_with_protocol("person@example.com", AsyncMock())

        self.assertEqual(outcome.reason_code, OAUTH_PROTOCOL_EDGE_BLOCK_REASON_CODE)
        self.assertIn("Tried protocol fingerprints: chrome136, chrome145", outcome.error or "")


if __name__ == "__main__":
    unittest.main()
