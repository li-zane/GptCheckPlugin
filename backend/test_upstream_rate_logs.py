from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.upstream_accounts import router
from app.core.database import Base, get_db
from app.core.security import require_admin
from app.models import UpstreamRateChangeLog, utcnow
from app.schemas import UpstreamRateChangeLogOut
from app.services.upstream_rate_logs import (
    list_upstream_rate_change_logs,
    prune_upstream_rate_change_logs,
)


def _log(*, account_id: int, status: str, created_at=None) -> UpstreamRateChangeLog:
    return UpstreamRateChangeLog(
        sub2api_account_id=account_id,
        account_name=f"Account {account_id}",
        channel_id=3,
        channel_name="Example upstream",
        group_id="default",
        group_name="Default",
        old_group_multiplier=1.0,
        new_group_multiplier=1.25,
        old_upstream_multiplier=0.1,
        new_upstream_multiplier=0.125,
        old_upstream_recharge_multiplier=0.1,
        new_upstream_recharge_multiplier=0.1,
        upstream_recharge_multiplier=0.1,
        local_recharge_multiplier=0.1,
        old_target_rate=1.0,
        new_target_rate=1.25,
        old_current_rate=1.0,
        new_current_rate=1.25,
        reason="scheduled_sync",
        status=status,
        safe_error=None,
        created_at=created_at or utcnow(),
    )


class UpstreamRateChangeLogServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.session_factory()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    async def test_list_prunes_expired_rows_and_pages_by_id(self) -> None:
        expired = _log(
            account_id=1,
            status="observed",
            created_at=utcnow() - timedelta(days=31),
        )
        older = _log(account_id=2, status="observed")
        newest = _log(account_id=3, status="applied")
        self.db.add_all([expired, older, newest])
        await self.db.commit()

        first_page = await list_upstream_rate_change_logs(
            self.db,
            retention_days=30,
            limit=1,
        )
        self.assertEqual([item.id for item in first_page], [newest.id])

        second_page = await list_upstream_rate_change_logs(
            self.db,
            retention_days=30,
            limit=10,
            before_id=newest.id,
        )
        self.assertEqual([item.id for item in second_page], [older.id])
        count = await self.db.scalar(select(func.count()).select_from(UpstreamRateChangeLog))
        self.assertEqual(count, 2)

    async def test_invalid_service_bounds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await list_upstream_rate_change_logs(self.db, retention_days=0)
        with self.assertRaises(ValueError):
            await list_upstream_rate_change_logs(self.db, retention_days=30, limit=201)
        with self.assertRaises(ValueError):
            await list_upstream_rate_change_logs(
                self.db,
                retention_days=30,
                before_id=0,
            )
        with self.assertRaises(ValueError):
            await list_upstream_rate_change_logs(
                self.db,
                retention_days=30,
                start_at=utcnow(),
                end_at=utcnow() - timedelta(days=1),
            )

    async def test_list_only_returns_real_changes_within_date_window(self) -> None:
        now = utcnow()
        outside = _log(account_id=1, status="applied", created_at=now - timedelta(days=3))
        inside = _log(account_id=2, status="applied", created_at=now - timedelta(days=1))
        initial = _log(account_id=3, status="observed", created_at=now - timedelta(hours=12))
        initial.old_group_multiplier = None
        initial.old_upstream_multiplier = None
        initial.old_target_rate = None
        initial.old_current_rate = initial.new_current_rate
        unchanged = _log(account_id=4, status="observed", created_at=now - timedelta(hours=6))
        unchanged.new_group_multiplier = unchanged.old_group_multiplier
        unchanged.new_upstream_multiplier = unchanged.old_upstream_multiplier
        unchanged.new_target_rate = unchanged.old_target_rate
        unchanged.new_current_rate = unchanged.old_current_rate
        self.db.add_all([outside, inside, initial, unchanged])
        await self.db.commit()

        rows = await list_upstream_rate_change_logs(
            self.db,
            retention_days=30,
            start_at=now - timedelta(days=2),
            end_at=now,
        )

        self.assertEqual([row.sub2api_account_id for row in rows], [2])

    async def test_list_keeps_apply_failure_when_only_rate_drift_was_observed(self) -> None:
        failure = _log(account_id=5, status="apply_failed")
        failure.new_group_multiplier = failure.old_group_multiplier
        failure.new_upstream_multiplier = failure.old_upstream_multiplier
        failure.new_target_rate = failure.old_target_rate
        failure.new_current_rate = failure.old_current_rate
        failure.safe_error = "Unable to update and verify the sub2api account rate."
        unchanged = _log(account_id=6, status="observed")
        unchanged.new_group_multiplier = unchanged.old_group_multiplier
        unchanged.new_upstream_multiplier = unchanged.old_upstream_multiplier
        unchanged.new_target_rate = unchanged.old_target_rate
        unchanged.new_current_rate = unchanged.old_current_rate
        self.db.add_all([failure, unchanged])
        await self.db.commit()

        rows = await list_upstream_rate_change_logs(
            self.db,
            retention_days=30,
        )

        self.assertEqual([row.sub2api_account_id for row in rows], [5])
        self.assertEqual(rows[0].safe_error, failure.safe_error)

    async def test_list_returns_health_only_changes_and_disable_failures(self) -> None:
        health_change = _log(account_id=7, status="observed")
        health_change.new_group_multiplier = health_change.old_group_multiplier
        health_change.new_upstream_multiplier = health_change.old_upstream_multiplier
        health_change.new_target_rate = health_change.old_target_rate
        health_change.new_current_rate = health_change.old_current_rate
        health_change.old_upstream_key_status = "active"
        health_change.new_upstream_key_status = "disabled"
        health_change.old_upstream_group_status = "available"
        health_change.new_upstream_group_status = "available"
        health_change.old_remote_schedulable = True
        health_change.new_remote_schedulable = True
        disable_failure = _log(account_id=8, status="disable_failed")
        disable_failure.new_group_multiplier = disable_failure.old_group_multiplier
        disable_failure.new_upstream_multiplier = disable_failure.old_upstream_multiplier
        disable_failure.new_target_rate = disable_failure.old_target_rate
        disable_failure.new_current_rate = disable_failure.old_current_rate
        disable_failure.safe_error = "Unable to disable and verify the sub2api account."
        self.db.add_all([health_change, disable_failure])
        await self.db.commit()

        rows = await list_upstream_rate_change_logs(
            self.db,
            retention_days=30,
        )

        self.assertEqual(
            [row.sub2api_account_id for row in rows],
            [8, 7],
        )

    async def test_prune_commits_at_most_once_and_only_when_rows_are_deleted(self) -> None:
        db = AsyncMock()
        db.execute.return_value = Mock(rowcount=0)

        deleted = await prune_upstream_rate_change_logs(db, retention_days=30)

        self.assertEqual(deleted, 0)
        db.commit.assert_not_awaited()

        db.execute.return_value = Mock(rowcount=4)
        deleted = await prune_upstream_rate_change_logs(db, retention_days=30)

        self.assertEqual(deleted, 4)
        db.commit.assert_awaited_once_with()

    def test_public_schema_has_no_credential_or_raw_response_fields(self) -> None:
        forbidden = {
            "api_key",
            "access_token",
            "refresh_token",
            "credentials",
            "raw",
            "raw_response",
        }
        self.assertTrue(forbidden.isdisjoint(UpstreamRateChangeLogOut.model_fields))
        row = _log(account_id=1, status="observed")
        row.id = 1
        serialized = UpstreamRateChangeLogOut.model_validate(row).model_dump()
        self.assertEqual(serialized["old_upstream_multiplier"], 0.1)
        self.assertEqual(serialized["new_upstream_multiplier"], 0.125)
        self.assertIn("old_upstream_key_status", serialized)
        self.assertIn("new_upstream_group_status", serialized)
        self.assertIn("new_remote_schedulable", serialized)


class UpstreamRateChangeLogApiTests(unittest.TestCase):
    def test_route_uses_retention_setting_and_cursor(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        fake_db = AsyncMock()

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        row = _log(account_id=7, status="applied")
        row.id = 11
        runtime = SimpleNamespace(
            get_upstream_rate_log_retention_days=AsyncMock(return_value=45)
        )
        list_logs = AsyncMock(return_value=[row])

        with (
            patch(
                "app.api.upstream_accounts.get_runtime_config_service",
                return_value=runtime,
            ),
            patch(
                "app.api.upstream_accounts.list_upstream_rate_change_logs",
                new=list_logs,
            ),
            TestClient(app) as client,
        ):
            response = client.get(
                "/api/upstream-accounts/rate-change-logs?limit=25&before_id=20"
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]["id"], 11)
        runtime.get_upstream_rate_log_retention_days.assert_awaited_once_with()
        list_logs.assert_awaited_once_with(
            fake_db,
            retention_days=45,
            limit=25,
            before_id=20,
            start_at=None,
            end_at=None,
        )
        self.assertNotIn("api_key", response.text)
        self.assertNotIn("access_token", response.text)

    def test_route_rejects_limit_above_200(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")

        async def db_override():
            yield ANY

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        with TestClient(app) as client:
            response = client.get("/api/upstream-accounts/rate-change-logs?limit=201")

        self.assertEqual(response.status_code, 422)

    def test_upstream_change_log_alias_preserves_the_old_contract(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        fake_db = AsyncMock()

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        runtime = SimpleNamespace(
            get_upstream_rate_log_retention_days=AsyncMock(return_value=30)
        )
        list_logs = AsyncMock(return_value=[])
        with (
            patch(
                "app.api.upstream_accounts.get_runtime_config_service",
                return_value=runtime,
            ),
            patch(
                "app.api.upstream_accounts.list_upstream_rate_change_logs",
                new=list_logs,
            ),
            TestClient(app) as client,
        ):
            response = client.get("/api/upstream-accounts/upstream-change-logs")

        self.assertEqual(response.status_code, 200, response.text)
        list_logs.assert_awaited_once()

    def test_route_converts_inclusive_local_dates_to_utc_bounds(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        fake_db = AsyncMock()

        async def db_override():
            yield fake_db

        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = db_override
        runtime = SimpleNamespace(
            get_upstream_rate_log_retention_days=AsyncMock(return_value=90)
        )
        list_logs = AsyncMock(return_value=[])

        with (
            patch(
                "app.api.upstream_accounts.get_runtime_config_service",
                return_value=runtime,
            ),
            patch(
                "app.api.upstream_accounts.list_upstream_rate_change_logs",
                new=list_logs,
            ),
            TestClient(app) as client,
        ):
            response = client.get(
                "/api/upstream-accounts/rate-change-logs"
                "?start_date=2026-07-14&end_date=2026-07-14&time_zone=Asia%2FShanghai"
            )

        self.assertEqual(response.status_code, 200, response.text)
        list_logs.assert_awaited_once_with(
            fake_db,
            retention_days=90,
            limit=100,
            before_id=None,
            start_at=datetime(2026, 7, 13, 16, tzinfo=timezone.utc),
            end_at=datetime(2026, 7, 14, 16, tzinfo=timezone.utc),
        )

    def test_route_rejects_invalid_date_range_and_time_zone(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/upstream-accounts")
        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        app.dependency_overrides[get_db] = lambda: ANY
        with TestClient(app) as client:
            reversed_range = client.get(
                "/api/upstream-accounts/rate-change-logs"
                "?start_date=2026-07-15&end_date=2026-07-14"
            )
            invalid_zone = client.get(
                "/api/upstream-accounts/rate-change-logs?time_zone=Not%2FAZone"
            )

        self.assertEqual(reversed_range.status_code, 422)
        self.assertEqual(invalid_zone.status_code, 422)


if __name__ == "__main__":
    unittest.main()
