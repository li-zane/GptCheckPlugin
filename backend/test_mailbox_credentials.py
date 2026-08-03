import unittest

from fastapi import Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.mailboxes import export_mailboxes, get_mailbox_credentials, _parse_import
from app.core.crypto import encrypt_text
from app.core.database import Base
from app.models import MailboxCredential
from app.schemas import BulkDeleteRequest


class MailboxCredentialApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_detail_decrypts_every_credential_field_without_cache(self) -> None:
        async with self.session_factory() as db:
            credential = MailboxCredential(
                gpt_email="gpt@example.com",
                mailbox_email="mailbox@outlook.com",
                provider="outlook",
                encrypted_password=encrypt_text("mail-password"),
                encrypted_client_id=encrypt_text("client-id"),
                encrypted_refresh_token=encrypt_text("refresh-token"),
                encrypted_access_token=encrypt_text("access-token"),
                custom_fetch_url="https://mail.example/fetch",
                proxy_url="http://proxy.example:8080",
            )
            db.add(credential)
            await db.commit()
            response = Response()

            detail = await get_mailbox_credentials(credential.id, response, {}, db)

        self.assertEqual(detail.password, "mail-password")
        self.assertEqual(detail.client_id, "client-id")
        self.assertEqual(detail.refresh_token, "refresh-token")
        self.assertEqual(detail.access_token, "access-token")
        self.assertEqual(detail.custom_fetch_url, "https://mail.example/fetch")
        self.assertEqual(detail.proxy_url, "http://proxy.example:8080")
        self.assertIn("gpt@example.com----mailbox@outlook.com", detail.import_line)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    async def test_export_preserves_requested_order_and_round_trips_import_formats(self) -> None:
        pickup_url = "https://mail.example/messages/token/url%40example.com"
        async with self.session_factory() as db:
            outlook = MailboxCredential(
                gpt_email="gpt@example.com",
                mailbox_email="mailbox@outlook.com",
                provider="outlook",
                encrypted_password=encrypt_text("mail-password"),
                encrypted_client_id=encrypt_text("client-id"),
                encrypted_refresh_token=encrypt_text("refresh-token"),
                encrypted_access_token=encrypt_text("access-token"),
            )
            url_mailbox = MailboxCredential(
                gpt_email="url@example.com",
                mailbox_email="url@example.com",
                provider="url",
                custom_fetch_url=pickup_url,
            )
            db.add_all([outlook, url_mailbox])
            await db.commit()
            response = Response()

            result = await export_mailboxes(
                BulkDeleteRequest(ids=[url_mailbox.id, outlook.id]),
                response,
                {},
                db,
            )

        lines = result.content.splitlines()
        self.assertEqual(result.exported, 2)
        self.assertEqual(lines[0], f"url@example.com----{pickup_url}")
        self.assertTrue(lines[1].endswith("----outlook"))
        parsed = _parse_import(result.content, "auto")
        self.assertEqual([item.gpt_email for item in parsed], ["url@example.com", "gpt@example.com"])
        self.assertEqual(parsed[1].mailbox_email, "mailbox@outlook.com")
        self.assertEqual(parsed[1].password, "mail-password")
        self.assertEqual(parsed[1].client_id, "client-id")
        self.assertEqual(parsed[1].refresh_token, "refresh-token")
        self.assertEqual(parsed[1].access_token, "access-token")
        self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()
