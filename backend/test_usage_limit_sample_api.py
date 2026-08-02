from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.api.accounts import delete_usage_limit_sample, delete_usage_limit_samples
from app.schemas import UsageLimitSampleDeleteRequest


class UsageLimitSampleApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_usage_limit_sample_commits_existing_row(self) -> None:
        sample = object()
        db = AsyncMock()
        db.get.return_value = sample

        response = await delete_usage_limit_sample(sample_id=17, _={}, db=db)

        self.assertEqual(response.message, "已删除额度样本 #17")
        db.get.assert_awaited_once()
        db.delete.assert_awaited_once_with(sample)
        db.commit.assert_awaited_once_with()

    async def test_delete_usage_limit_sample_returns_404_without_mutation(self) -> None:
        db = AsyncMock()
        db.get.return_value = None

        with self.assertRaises(HTTPException) as context:
            await delete_usage_limit_sample(sample_id=999, _={}, db=db)

        self.assertEqual(context.exception.status_code, 404)
        db.delete.assert_not_awaited()
        db.commit.assert_not_awaited()

    async def test_delete_usage_limit_samples_deduplicates_and_commits(self) -> None:
        db = AsyncMock()
        db.execute.return_value = SimpleNamespace(rowcount=2)
        payload = UsageLimitSampleDeleteRequest(sample_ids=[17, 18, 17])

        response = await delete_usage_limit_samples(payload=payload, _={}, db=db)

        self.assertEqual(response.requested_count, 2)
        self.assertEqual(response.deleted_count, 2)
        self.assertEqual(response.message, "已删除 2 条额度样本")
        db.execute.assert_awaited_once()
        db.commit.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
