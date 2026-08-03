import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.api.mailboxes import _parse_import
from app.services.mail import UrlPickupMailAdapter, _PICKUP_HTTP_LOG_REDACTION, _PickupHttpLogFilter


PICKUP_URL = "https://mail.example/messages/secret-token/user%40example.com"
PICKUP_HTML = """
<!doctype html>
<html>
  <head><style>.body { color: #123456; }</style></head>
  <body>
    <article class="mail-card">
      <details open>
        <summary>
          <span class="subject">Your temporary ChatGPT login code</span>
          <span class="date">2026-08-02 22:29:30</span>
        </summary>
        <div class="meta">From: OpenAI &lt;noreply@openai.com&gt;</div>
        <pre class="body">Enter this temporary verification code: 381327</pre>
      </details>
    </article>
  </body>
</html>
"""


def _credential() -> SimpleNamespace:
    return SimpleNamespace(
        gpt_email="user@example.com",
        mailbox_email="user@example.com",
        custom_fetch_url=PICKUP_URL,
    )


def _mock_client(response: httpx.Response | None = None, *, error: Exception | None = None) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    if error is not None:
        client.get.side_effect = error
    else:
        client.get.return_value = response
    return client


class MailboxUrlImportTests(unittest.TestCase):
    def test_two_part_url_import_uses_same_gpt_and_mailbox_email(self) -> None:
        parsed = _parse_import(f"User@Example.com----{PICKUP_URL}", "auto")

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].gpt_email, "user@example.com")
        self.assertEqual(parsed[0].mailbox_email, "user@example.com")
        self.assertEqual(parsed[0].provider, "url")
        self.assertEqual(parsed[0].custom_fetch_url, PICKUP_URL)

    def test_three_part_url_import_preserves_gpt_and_mailbox_mapping(self) -> None:
        parsed = _parse_import(f"gpt@example.com----mailbox@example.net----{PICKUP_URL}", "auto")

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].gpt_email, "gpt@example.com")
        self.assertEqual(parsed[0].mailbox_email, "mailbox@example.net")
        self.assertEqual(parsed[0].provider, "url")

    def test_url_import_rejects_non_http_endpoint(self) -> None:
        self.assertEqual(_parse_import("user@example.com----file:///tmp/mail", "auto"), [])

    def test_pickup_http_log_filter_redacts_full_url(self) -> None:
        record = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='HTTP Request: %s %s "%s %d %s"',
            args=("GET", httpx.URL(PICKUP_URL), "HTTP/1.1", 200, "OK"),
            exc_info=None,
        )
        context = _PICKUP_HTTP_LOG_REDACTION.set(True)
        try:
            self.assertTrue(_PickupHttpLogFilter().filter(record))
        finally:
            _PICKUP_HTTP_LOG_REDACTION.reset(context)

        self.assertNotIn(PICKUP_URL, record.getMessage())
        self.assertNotIn("secret-token", record.getMessage())
        self.assertIn("pickup endpoint redacted", record.getMessage())


class UrlPickupMailAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_pickup_uses_get_and_extracts_code(self) -> None:
        response = httpx.Response(
            200,
            text=PICKUP_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", PICKUP_URL),
        )
        client = _mock_client(response)
        adapter = UrlPickupMailAdapter()

        with (
            patch("app.services.mail._pickup_local_timezone", return_value=timezone(timedelta(hours=8))),
            patch("app.services.mail.httpx.AsyncClient", return_value=client),
        ):
            result = await adapter.fetch_code(
                _credential(),
                datetime(2026, 8, 2, 14, 29, 29, tzinfo=timezone.utc),
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.code, "381327")
        client.get.assert_awaited_once()
        self.assertEqual(client.get.await_args.args[0], PICKUP_URL)

    async def test_html_pickup_ignores_code_older_than_lookup_start(self) -> None:
        response = httpx.Response(
            200,
            text=PICKUP_HTML,
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", PICKUP_URL),
        )
        client = _mock_client(response)

        with (
            patch("app.services.mail._pickup_local_timezone", return_value=timezone(timedelta(hours=8))),
            patch("app.services.mail.httpx.AsyncClient", return_value=client),
        ):
            result = await UrlPickupMailAdapter().fetch_code(
                _credential(),
                datetime(2026, 8, 2, 14, 29, 31, tzinfo=timezone.utc),
            )

        self.assertEqual(result.status, "not_found")
        self.assertIsNone(result.code)

    async def test_nested_json_uses_most_recent_message(self) -> None:
        response = httpx.Response(
            200,
            json={
                "data": {
                    "messages": [
                        {"subject": "Code 111111", "received_at": "2026-08-02T20:00:00Z"},
                        {"body": {"content": "Code 222222"}, "receivedDateTime": "2026-08-02T21:00:00Z"},
                    ]
                }
            },
            request=httpx.Request("GET", PICKUP_URL),
        )
        client = _mock_client(response)

        with patch("app.services.mail.httpx.AsyncClient", return_value=client):
            result = await UrlPickupMailAdapter().fetch_code(
                _credential(),
                datetime(2026, 8, 2, 19, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.code, "222222")

    async def test_request_error_does_not_expose_pickup_url_or_token(self) -> None:
        request = httpx.Request("GET", PICKUP_URL)
        error = httpx.ConnectError(f"connection failed for {PICKUP_URL}", request=request)
        client = _mock_client(error=error)

        with patch("app.services.mail.httpx.AsyncClient", return_value=client):
            result = await UrlPickupMailAdapter().fetch_messages(_credential(), "inbox", 10)

        self.assertEqual(result.status, "failed")
        self.assertNotIn(PICKUP_URL, result.error or "")
        self.assertNotIn("secret-token", result.error or "")
        self.assertIn("ConnectError", result.error or "")


if __name__ == "__main__":
    unittest.main()
