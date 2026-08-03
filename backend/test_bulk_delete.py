import unittest

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.mailboxes import bulk_delete_mailboxes
from app.api.phones import bulk_delete_phones
from app.core.database import Base
from app.models import MailboxCredential, PhoneAccountBinding, PhoneNumber
from app.schemas import BulkDeleteRequest


class BulkDeleteRequestTests(unittest.TestCase):
    def test_ids_are_deduplicated_in_input_order(self) -> None:
        self.assertEqual(BulkDeleteRequest(ids=[3, 1, 3, 2]).ids, [3, 1, 2])

    def test_ids_must_be_positive(self) -> None:
        with self.assertRaises(ValidationError):
            BulkDeleteRequest(ids=[0])


class BulkDeleteApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_bulk_delete_mailboxes_reports_actual_delete_count(self) -> None:
        async with self.session_factory() as db:
            rows = [
                MailboxCredential(gpt_email="one@example.com", mailbox_email="one@example.com"),
                MailboxCredential(gpt_email="two@example.com", mailbox_email="two@example.com"),
                MailboxCredential(gpt_email="keep@example.com", mailbox_email="keep@example.com"),
            ]
            db.add_all(rows)
            await db.commit()

            result = await bulk_delete_mailboxes(
                BulkDeleteRequest(ids=[rows[0].id, rows[1].id, 999_999, rows[0].id]),
                {},
                db,
            )
            remaining = list((await db.execute(select(MailboxCredential.gpt_email))).scalars())

        self.assertEqual(result.requested_count, 3)
        self.assertEqual(result.deleted_count, 2)
        self.assertEqual(remaining, ["keep@example.com"])

    async def test_bulk_delete_phones_removes_bindings(self) -> None:
        async with self.session_factory() as db:
            deleted_phone = PhoneNumber(
                phone_key="phone-one",
                phone_number="+15550000001",
                sms_url="https://sms.example/one",
            )
            kept_phone = PhoneNumber(
                phone_key="phone-two",
                phone_number="+15550000002",
                sms_url="https://sms.example/two",
            )
            db.add_all([deleted_phone, kept_phone])
            await db.flush()
            db.add(PhoneAccountBinding(phone_id=deleted_phone.id, account_email="one@example.com"))
            await db.commit()

            result = await bulk_delete_phones(
                BulkDeleteRequest(ids=[deleted_phone.id, 999_999]),
                {},
                db,
            )
            remaining_phones = list((await db.execute(select(PhoneNumber.phone_number))).scalars())
            remaining_bindings = list((await db.execute(select(PhoneAccountBinding.id))).scalars())

        self.assertEqual(result.requested_count, 2)
        self.assertEqual(result.deleted_count, 1)
        self.assertEqual(remaining_phones, ["+15550000002"])
        self.assertEqual(remaining_bindings, [])


if __name__ == "__main__":
    unittest.main()
