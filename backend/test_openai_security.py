import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.openai_oauth import (
    AUTH_ACCOUNTS_BASE,
    OAuthContext,
    OpenAiOAuthRefresher,
)
from app.services.openai_token_service import (
    _NoRedirectHandler,
    _build_no_redirect_opener,
)


class OpenAiOAuthDestinationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _context() -> OAuthContext:
        return OAuthContext(
            state="expected-state",
            code_verifier="verifier",
            redirect_uri="http://localhost:1455/auth/callback",
            client_id="client",
            auth_url="https://auth.openai.com/oauth/authorize",
        )

    async def test_protocol_continuation_rejects_cross_origin_without_request(self) -> None:
        refresher = OpenAiOAuthRefresher(SimpleNamespace())

        class FakeSession:
            calls = 0

            async def get(self, url, allow_redirects=False):
                self.calls += 1
                raise AssertionError("cross-origin continuation must not be requested")

        session = FakeSession()
        result = await refresher._follow_protocol_url(
            session,
            "https://attacker.example/private",
            self._context(),
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("untrusted origin", result.error or "")
        self.assertEqual(session.calls, 0)

    async def test_sensitive_posts_disable_redirects_and_reject_cross_origin_location(self) -> None:
        refresher = OpenAiOAuthRefresher(SimpleNamespace())

        class FakeResponse:
            def __init__(self, status_code, url, payload=None, headers=None):
                self.status_code = status_code
                self.url = url
                self.text = ""
                self.headers = headers or {}
                self._payload = payload or {}

            def json(self):
                return self._payload

        class FakeSession:
            def __init__(self):
                self.get_calls = []
                self.post_calls = []

            async def get(self, url, **kwargs):
                self.get_calls.append((url, kwargs))
                return FakeResponse(200, url)

            async def post(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                if len(self.post_calls) == 1:
                    return FakeResponse(
                        200,
                        url,
                        payload={"page": {"type": "email_otp_verification"}},
                    )
                return FakeResponse(
                    307,
                    url,
                    headers={"location": "https://attacker.example/collect"},
                )

        async def fetch_code(_requested_at):
            return "123456"

        session = FakeSession()
        result = await refresher._run_protocol_oauth(
            session,
            self._context(),
            "person@example.com",
            fetch_code,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("untrusted origin", result.error or "")
        self.assertEqual(len(session.post_calls), 2)
        self.assertTrue(all(call[1].get("allow_redirects") is False for call in session.post_calls))
        self.assertEqual([call[0] for call in session.get_calls], [self._context().auth_url])

    async def test_email_post_cross_origin_location_is_not_requested(self) -> None:
        refresher = OpenAiOAuthRefresher(SimpleNamespace())

        class FakeResponse:
            status_code = 307
            url = f"{AUTH_ACCOUNTS_BASE}/authorize/continue"
            text = ""
            headers = {"location": "https://attacker.example/collect"}

            @staticmethod
            def json():
                return {}

        class FakeSession:
            def __init__(self):
                self.get_calls = []
                self.post_kwargs = None

            async def get(self, url, **kwargs):
                self.get_calls.append((url, kwargs))
                if len(self.get_calls) > 1:
                    raise AssertionError("cross-origin Location must not be requested")
                return SimpleNamespace(
                    status_code=200,
                    url=url,
                    text="",
                    headers={},
                    json=lambda: {},
                )

            async def post(self, _url, **kwargs):
                self.post_kwargs = kwargs
                return FakeResponse()

        async def fetch_code(_requested_at):
            raise AssertionError("a redirect response must not advance to OTP collection")

        session = FakeSession()
        result = await refresher._run_protocol_oauth(
            session,
            self._context(),
            "person@example.com",
            fetch_code,
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("untrusted origin", result.error or "")
        self.assertIs(session.post_kwargs.get("allow_redirects"), False)
        self.assertEqual(len(session.get_calls), 1)

    async def test_email_post_same_origin_location_continues_with_get(self) -> None:
        refresher = OpenAiOAuthRefresher(SimpleNamespace())
        continuation_url = "https://auth.openai.com/oauth/continue"
        callback_url = (
            "http://localhost:1455/auth/callback"
            "?state=expected-state&code=valid"
        )

        class FakeResponse:
            def __init__(self, status_code, url, headers=None):
                self.status_code = status_code
                self.url = url
                self.text = ""
                self.headers = headers or {}

            @staticmethod
            def json():
                return {}

        class FakeSession:
            def __init__(self):
                self.get_calls = []
                self.post_calls = []

            async def get(self, url, **kwargs):
                self.get_calls.append((url, kwargs))
                if len(self.get_calls) == 1:
                    return FakeResponse(200, url)
                self.assert_continuation_request(url, kwargs)
                return FakeResponse(302, url, headers={"location": callback_url})

            async def post(self, url, **kwargs):
                self.post_calls.append((url, kwargs))
                return FakeResponse(307, url, headers={"location": continuation_url})

            @staticmethod
            def assert_continuation_request(url, kwargs):
                if url != continuation_url or kwargs.get("allow_redirects") is not False:
                    raise AssertionError("trusted continuation must use a no-redirect GET")

        async def fetch_code(_requested_at):
            raise AssertionError("a redirect continuation must not advance to OTP collection")

        session = FakeSession()
        result = await refresher._run_protocol_oauth(
            session,
            self._context(),
            "person@example.com",
            fetch_code,
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.access_token, "valid")
        self.assertEqual(len(session.post_calls), 1)
        self.assertIs(session.post_calls[0][1].get("allow_redirects"), False)
        self.assertEqual([call[0] for call in session.get_calls], [self._context().auth_url, continuation_url])

    def test_callback_code_requires_exact_redirect_target(self) -> None:
        refresher = OpenAiOAuthRefresher(SimpleNamespace())
        redirect_uri = self._context().redirect_uri

        self.assertIsNone(
            refresher._code_from_url(
                "https://attacker.example/auth/callback?state=expected-state&code=stolen",
                "expected-state",
                redirect_uri,
            )
        )
        self.assertEqual(
            refresher._code_from_url(
                "http://localhost:1455/auth/callback?state=expected-state&code=valid",
                "expected-state",
                redirect_uri,
            ),
            "valid",
        )

        rejected_urls = (
            "http://localhost:1455/auth/callback;other?state=expected-state&code=valid",
            "http://localhost:1455/auth/callback?state=expected-state&code=valid#fragment",
            "http://localhost:1455/auth/callback?state=expected-state&state=other&code=valid",
            "http://localhost:1455/auth/callback?state=expected-state&code=valid&code=other",
            "http://localhost:1455/auth/callback?state=expected-state&code=valid&error=one&error=two",
            (
                "http://localhost:1455/auth/callback?state=expected-state&code=valid"
                "&error_description=one&error_description=two"
            ),
        )
        for callback_url in rejected_urls:
            with self.subTest(callback_url=callback_url):
                self.assertIsNone(
                    refresher._code_from_url(callback_url, "expected-state", redirect_uri)
                )


class UrllibRedirectTests(unittest.TestCase):
    def test_fallback_opener_installs_no_redirect_handler(self) -> None:
        sentinel = object()
        with patch(
            "app.services.openai_token_service.urllib_request.build_opener",
            return_value=sentinel,
        ) as build_opener:
            self.assertIs(_build_no_redirect_opener(), sentinel)

        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, _NoRedirectHandler)
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other"))


if __name__ == "__main__":
    unittest.main()
