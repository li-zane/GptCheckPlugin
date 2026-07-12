from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from app.api.accounts import delete_usage_limit_sample


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


if __name__ == "__main__":
    unittest.main()
