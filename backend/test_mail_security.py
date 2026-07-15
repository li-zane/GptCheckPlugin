import socket
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.mail import (
    GmailImapAdapter,
    MailMessage,
    MailMessagesResult,
    OutlookGraphAdapter,
    _ProxyImap4Ssl,
    _external_message_to_mail_message,
    _imap_chunks_match_recipient,
    _imap_message_to_mail_message,
    _safe_graph_next_link,
    _normalize_graph_message,
)


class OutlookRecipientBindingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _credential() -> SimpleNamespace:
        return SimpleNamespace(gpt_email="victim@example.com")

    async def test_fetch_code_rejects_message_for_another_recipient(self) -> None:
        adapter = OutlookGraphAdapter()
        adapter.fetch_messages = AsyncMock(
            side_effect=[
                MailMessagesResult(
                    status="ok",
                    messages=[
                        MailMessage(
                            id="other",
                            folder="inbox",
                            subject="Your code is 654321",
                            sender_name="OpenAI",
                            sender_address="noreply@tm.openai.com",
                            body_preview=None,
                            received_at=datetime.now(timezone.utc),
                            code="654321",
                            recipients=("other@example.com",),
                            sender_authenticated=True,
                        )
                    ],
                ),
                MailMessagesResult(status="ok", messages=[]),
            ]
        )

        result = await adapter.fetch_code(self._credential(), datetime.min.replace(tzinfo=timezone.utc))

        self.assertEqual(result.status, "not_found")
        self.assertIsNone(result.code)

    async def test_fetch_code_accepts_matching_recipient(self) -> None:
        adapter = OutlookGraphAdapter()
        adapter.fetch_messages = AsyncMock(
            return_value=MailMessagesResult(
                status="ok",
                messages=[
                    MailMessage(
                        id="matching",
                        folder="inbox",
                        subject="Your code is 123456",
                        sender_name="OpenAI",
                        sender_address="noreply@tm.openai.com",
                        body_preview=None,
                        received_at=datetime.now(timezone.utc),
                        code="123456",
                        recipients=("victim@example.com",),
                        sender_authenticated=True,
                    )
                ],
            )
        )
        result = await adapter.fetch_code(self._credential(), datetime.min.replace(tzinfo=timezone.utc))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.code, "123456")

    async def test_fetch_code_rejects_spoofed_sender_with_matching_to_header(self) -> None:
        adapter = OutlookGraphAdapter()
        adapter.fetch_messages = AsyncMock(
            return_value=MailMessagesResult(
                status="ok",
                messages=[
                    MailMessage(
                        id="spoofed",
                        folder="inbox",
                        subject="Your code is 654321",
                        sender_name="OpenAI",
                        sender_address="noreply@tm.openai.com",
                        body_preview=None,
                        received_at=datetime.now(timezone.utc),
                        code="654321",
                        recipients=("victim@example.com",),
                        sender_authenticated=False,
                    )
                ],
            )
        )
        adapter._fetch_messages_for_code = AsyncMock(  # type: ignore[method-assign]
            return_value=MailMessagesResult(status="ok", messages=[])
        )

        result = await adapter.fetch_code(self._credential(), datetime.min.replace(tzinfo=timezone.utc))

        self.assertEqual(result.status, "not_found")
        self.assertIsNone(result.code)

    async def test_unverified_external_candidate_falls_through_to_verified_strategy(self) -> None:
        settings = SimpleNamespace(mail_read_timeout_seconds=5)
        adapter = OutlookGraphAdapter(settings)
        credential = SimpleNamespace(
            id=1,
            gpt_email="victim@example.com",
            mailbox_email="mailbox@example.com",
            encrypted_client_id="encrypted-client",
            encrypted_refresh_token="encrypted-refresh",
            encrypted_access_token=None,
        )
        unverified = MailMessagesResult(
            status="ok",
            messages=[
                MailMessage(
                    id="external",
                    folder="inbox",
                    subject="Your code is 654321",
                    sender_name="OpenAI",
                    sender_address="noreply@tm.openai.com",
                    body_preview=None,
                    received_at=datetime.now(timezone.utc),
                    code="654321",
                    recipients=("victim@example.com",),
                    sender_authenticated=False,
                )
            ],
        )
        verified = MailMessagesResult(
            status="ok",
            messages=[
                MailMessage(
                    id="graph",
                    folder="inbox",
                    subject="Your code is 123456",
                    sender_name="OpenAI",
                    sender_address="noreply@tm.openai.com",
                    body_preview=None,
                    received_at=datetime.now(timezone.utc),
                    code="123456",
                    recipients=("victim@example.com",),
                    sender_authenticated=True,
                )
            ],
        )
        adapter._strategy_order = lambda _key: ["external_api", "graph"]  # type: ignore[method-assign]
        adapter._fetch_with_strategy = AsyncMock(side_effect=[unverified, verified])  # type: ignore[method-assign]

        with patch("app.services.mail.decrypt_text", side_effect=["client-id", "refresh-token", None]):
            result = await adapter._fetch_messages_inner(
                credential,
                "inbox",
                10,
                target_recipient="victim@example.com",
            )

        self.assertEqual(result.messages[0].code, "123456")
        self.assertEqual(adapter._fetch_with_strategy.await_count, 2)

    def test_graph_normalizer_preserves_structured_and_original_recipients(self) -> None:
        message = _normalize_graph_message(
            {
                "id": "graph-message",
                "subject": "Code 123456",
                "receivedDateTime": "2026-07-15T00:00:00Z",
                "from": {"emailAddress": {"address": "noreply@tm.openai.com"}},
                "toRecipients": [
                    {"emailAddress": {"address": "Victim@Example.com"}},
                ],
                "internetMessageHeaders": [
                    {
                        "name": "Authentication-Results",
                        "value": "mx.google.com; dkim=pass header.i=@tm.openai.com; dmarc=pass header.from=openai.com",
                    },
                    {"name": "X-Original-To", "value": "forwarded@example.com"},
                    {"name": "X-Untrusted-Note", "value": "ignore@example.com"},
                ],
            },
            "inbox",
        )

        self.assertEqual(message.recipients, ("forwarded@example.com", "victim@example.com"))
        self.assertTrue(message.sender_authenticated)

    def test_graph_next_link_rejects_cross_origin_destination(self) -> None:
        with self.assertRaises(ValueError):
            _safe_graph_next_link("https://attacker.example/v1.0/me/messages")

        self.assertEqual(
            _safe_graph_next_link("/v1.0/me/messages?$skiptoken=next"),
            "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=next",
        )

    def test_outlook_imap_fetches_and_preserves_recipient_headers(self) -> None:
        adapter = OutlookGraphAdapter()

        class FakeImapClient:
            fetch_spec = ""

            def select(self, folder, readonly=True):
                return "OK", []

            def uid(self, command, *args):
                if command == "SEARCH":
                    return "OK", [b"42"]
                self.fetch_spec = str(args[-1])
                header_lines = [
                    b"From: OpenAI <noreply@openai.com>",
                    b"Authentication-Results: spf=pass; dkim=pass header.d=openai.com; dmarc=pass header.from=openai.com",
                    b"Subject: Your code is 123456",
                    b"Date: Wed, 15 Jul 2026 00:00:00 +0000",
                ]
                if " TO " in f" {self.fetch_spec} ":
                    header_lines.append(b"To: Victim <victim@example.com>")
                headers = b"\r\n".join(header_lines) + b"\r\n\r\n"
                return "OK", [
                    (b"1 (UID 42 BODY[HEADER.FIELDS (...)]", headers),
                    (b" BODY[TEXT]<0>", b"Your verification code is 123456"),
                    b")",
                ]

        client = FakeImapClient()
        messages = adapter._read_imap_messages(client, "inbox", 10)

        self.assertIn("DELIVERED-TO", client.fetch_spec)
        self.assertIn("X-ORIGINAL-TO", client.fetch_spec)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].recipients, ("victim@example.com",))
        self.assertTrue(messages[0].sender_authenticated)

    def test_imap_recipient_binding_ignores_subject_and_body_labels(self) -> None:
        raw_message = (
            b"From: attacker@example.com\r\n"
            b"Subject: To: victim@example.com code 654321\r\n"
            b"Date: Wed, 15 Jul 2026 00:00:00 +0000\r\n"
            b"\r\n"
            b"Delivered-To: victim@example.com\r\nYour code is 654321"
        )

        message = _imap_message_to_mail_message(raw_message, "42", "inbox")

        self.assertEqual(message.recipients, ())
        self.assertFalse(_imap_chunks_match_recipient([raw_message], "victim@example.com"))

    def test_sender_supplied_later_authentication_results_cannot_override_provider_failure(self) -> None:
        raw_message = (
            b"Authentication-Results: spf=fail; dkim=fail header.d=tm.openai.com; dmarc=fail header.from=openai.com\r\n"
            b"Authentication-Results: mx.google.com; dkim=pass header.i=@tm.openai.com\r\n"
            b"From: OpenAI <noreply@tm.openai.com>\r\n"
            b"To: victim@example.com\r\n"
            b"Subject: Your code is 654321\r\n"
            b"Date: Wed, 15 Jul 2026 00:00:00 +0000\r\n"
            b"\r\nYour code is 654321"
        )

        message = _imap_message_to_mail_message(raw_message, "42", "inbox")

        self.assertFalse(message.sender_authenticated)

    def test_dkim_pass_cannot_borrow_openai_identity_from_a_failed_signature(self) -> None:
        raw_message = (
            b"Authentication-Results: mx.google.com; dkim=pass header.d=attacker.example; "
            b"dkim=fail header.d=tm.openai.com; dmarc=fail header.from=openai.com\r\n"
            b"From: OpenAI <noreply@tm.openai.com>\r\n"
            b"To: victim@example.com\r\n"
            b"Subject: Your code is 654321\r\n"
            b"\r\nYour code is 654321"
        )

        message = _imap_message_to_mail_message(raw_message, "42", "inbox")

        self.assertFalse(message.sender_authenticated)

    def test_external_message_can_carry_provider_authentication_metadata(self) -> None:
        message = _external_message_to_mail_message(
            {
                "id": "external-1",
                "from": "OpenAI <noreply@tm.openai.com>",
                "to": "victim@example.com",
                "subject": "Your code is 123456",
                "authentication_results": (
                    "mx.google.com; dkim=pass header.i=@tm.openai.com; "
                    "dmarc=pass header.from=openai.com"
                ),
            },
            "inbox",
            0,
        )

        self.assertTrue(message.sender_authenticated)
        self.assertEqual(message.recipients, ("victim@example.com",))


class GmailRecipientBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_gmail_code_requires_matching_recipient_and_authenticated_sender(self) -> None:
        credential = SimpleNamespace(gpt_email="victim@example.com")
        valid = MailMessage(
            id="valid",
            folder="inbox",
            subject="Your code is 123456",
            sender_name="OpenAI",
            sender_address="noreply@tm.openai.com",
            body_preview=None,
            received_at=datetime.now(timezone.utc),
            code="123456",
            recipients=("victim@example.com",),
            sender_authenticated=True,
        )
        adapter = GmailImapAdapter(SimpleNamespace())
        adapter.fetch_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=MailMessagesResult(status="ok", messages=[valid])
        )

        result = await adapter.fetch_code(credential, datetime.min.replace(tzinfo=timezone.utc))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.code, "123456")

    async def test_gmail_rejects_unverified_matching_recipient(self) -> None:
        credential = SimpleNamespace(gpt_email="victim@example.com")
        spoofed = MailMessage(
            id="spoofed",
            folder="inbox",
            subject="Your code is 654321",
            sender_name="OpenAI",
            sender_address="noreply@tm.openai.com",
            body_preview=None,
            received_at=datetime.now(timezone.utc),
            code="654321",
            recipients=("victim@example.com",),
            sender_authenticated=False,
        )
        adapter = GmailImapAdapter(SimpleNamespace())
        adapter.fetch_messages = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                MailMessagesResult(status="ok", messages=[spoofed]),
                MailMessagesResult(status="ok", messages=[]),
            ]
        )

        result = await adapter.fetch_code(credential, datetime.min.replace(tzinfo=timezone.utc))

        self.assertEqual(result.status, "not_found")
        self.assertIsNone(result.code)


class GmailProxyIsolationTests(unittest.TestCase):
    def test_https_proxy_is_rejected_before_connection(self) -> None:
        client = object.__new__(_ProxyImap4Ssl)
        client._proxy_url = "https://proxy-user:proxy-password@proxy.example:443"

        with self.assertRaisesRegex(ValueError, "TLS-to-proxy"):
            client._create_socket(2)

    def test_proxy_configuration_does_not_mutate_process_socket_globals(self) -> None:
        adapter = GmailImapAdapter(
            SimpleNamespace(outlook_imap_timeout_seconds=2, mail_read_timeout_seconds=5)
        )
        credential = SimpleNamespace(
            encrypted_password="encrypted",
            mailbox_email="mailbox@gmail.com",
            gpt_email="victim@example.com",
            proxy_url="socks5h://127.0.0.1:1080",
        )
        captured: dict[str, object] = {}

        class FakeProxyClient:
            def __init__(self, host, port, *, proxy_url, timeout):
                captured.update(host=host, port=port, proxy_url=proxy_url, timeout=timeout)

            def login(self, email, password):
                captured.update(email=email, password=password)

            def logout(self):
                captured["logged_out"] = True

        original_socket = socket.socket
        original_getaddrinfo = socket.getaddrinfo
        fake_socks = SimpleNamespace(
            set_default_proxy=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("global proxy mutation")
            )
        )
        with (
            patch("app.services.mail.decrypt_text", return_value="mail-password"),
            patch("app.services.mail._ProxyImap4Ssl", FakeProxyClient),
            patch.object(adapter, "_read_filtered_messages", return_value=[]),
            patch.dict(sys.modules, {"socks": fake_socks}),
        ):
            result = adapter._fetch_messages_sync(credential, "inbox", 10)

        self.assertEqual(result.status, "ok")
        self.assertIs(socket.socket, original_socket)
        self.assertIs(socket.getaddrinfo, original_getaddrinfo)
        self.assertEqual(captured["proxy_url"], credential.proxy_url)
        self.assertTrue(captured["logged_out"])

    def test_gmail_logout_oserror_does_not_override_success(self) -> None:
        adapter = GmailImapAdapter(
            SimpleNamespace(outlook_imap_timeout_seconds=2, mail_read_timeout_seconds=5)
        )
        credential = SimpleNamespace(
            encrypted_password="encrypted",
            mailbox_email="mailbox@gmail.com",
            gpt_email="victim@example.com",
            proxy_url=None,
        )

        class FakeImapClient:
            def __init__(self, host, port, *, timeout):
                pass

            def login(self, email, password):
                pass

            def logout(self):
                raise OSError("socket already closed")

        with (
            patch("app.services.mail.decrypt_text", return_value="mail-password"),
            patch("app.services.mail.imaplib.IMAP4_SSL", FakeImapClient),
            patch.object(adapter, "_read_filtered_messages", return_value=[]),
        ):
            result = adapter._fetch_messages_sync(credential, "inbox", 10)

        self.assertEqual(result.status, "ok")


class OutlookImapCleanupTests(unittest.TestCase):
    def test_logout_oserror_does_not_override_read_error(self) -> None:
        adapter = OutlookGraphAdapter(
            SimpleNamespace(
                outlook_imap_hosts=["outlook.office365.com"],
                outlook_imap_host=None,
                outlook_imap_port=993,
                outlook_imap_timeout_seconds=2,
                outlook_imap_failure_cooldown_seconds=30,
            )
        )
        credential = SimpleNamespace(mailbox_email="mailbox@outlook.com")

        class FakeImapClient:
            def __init__(self, host, port, *, timeout):
                pass

            def authenticate(self, mechanism, callback):
                pass

            def logout(self):
                raise OSError("logout failed")

        with (
            patch("app.services.mail.imaplib.IMAP4_SSL", FakeImapClient),
            patch.object(adapter, "_read_imap_messages", side_effect=OSError("read failed")),
        ):
            result = adapter._fetch_imap_messages_sync(
                credential,
                "access-token",
                "inbox",
                10,
            )

        self.assertEqual(result.status, "failed")
        self.assertIn("read failed", result.error or "")
        self.assertNotIn("logout failed", result.error or "")


if __name__ == "__main__":
    unittest.main()
