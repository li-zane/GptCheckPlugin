from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import (
    Base,
    _migrate_management_site_setting_keys,
    _migrate_upstream_domain_v2,
    _prepare_upstream_domain_v2,
)
from app.models import Upstream, UpstreamApiKey, UpstreamGroup


class UpstreamSchemaUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_current_schema_initialization_is_idempotent_and_has_no_legacy_tables(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(text("PRAGMA foreign_keys=ON"))
            await connection.run_sync(Base.metadata.create_all)
            self.assertFalse(await _prepare_upstream_domain_v2(connection))
            await _migrate_upstream_domain_v2(connection)
            await connection.run_sync(Base.metadata.create_all)
            tables = {
                row[0]
                for row in (
                    await connection.execute(
                        text("SELECT name FROM sqlite_master WHERE type = 'table'")
                    )
                ).all()
            }
            self.assertIn("upstreams", tables)
            self.assertIn("api_accounts", tables)
            self.assertIn("upstream_api_keys", tables)
            self.assertIn("upstream_groups", tables)
            self.assertNotIn("upstream_channels", tables)
            self.assertNotIn("upstream_account_configs", tables)
            self.assertEqual(
                (await connection.execute(text("PRAGMA foreign_key_check"))).all(),
                [],
            )

    async def test_soft_deleted_url_is_released_without_reusing_upstream_uuid(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with session_factory() as db:
            archived = Upstream(
                display_name="Archived",
                api_endpoint_url="https://same.example",
                deleted_at=datetime.now(timezone.utc),
            )
            active = Upstream(
                display_name="Active",
                api_endpoint_url="https://same.example",
            )
            db.add_all([archived, active])
            await db.commit()
            self.assertNotEqual(archived.id, active.id)

            db.add(
                Upstream(
                    display_name="Duplicate active",
                    api_endpoint_url="https://same.example",
                )
            )
            with self.assertRaises(IntegrityError):
                await db.commit()

    async def test_remote_key_and_group_ids_are_unique_only_within_one_upstream(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with session_factory() as db:
            first = Upstream(display_name="First", api_endpoint_url="https://first.example")
            second = Upstream(display_name="Second", api_endpoint_url="https://second.example")
            db.add_all([first, second])
            await db.flush()
            db.add_all(
                [
                    UpstreamApiKey(upstream_id=first.id, remote_key_id=7),
                    UpstreamApiKey(upstream_id=second.id, remote_key_id=7),
                    UpstreamGroup(upstream_id=first.id, remote_group_id="vip", name="VIP"),
                    UpstreamGroup(upstream_id=second.id, remote_group_id="vip", name="VIP"),
                ]
            )
            await db.commit()

            db.add(UpstreamApiKey(upstream_id=first.id, remote_key_id=7))
            with self.assertRaises(IntegrityError):
                await db.commit()

    async def test_legacy_availability_setting_names_are_migrated_without_overwriting_new_values(self) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE app_settings ("
                    "key VARCHAR(128) PRIMARY KEY, value TEXT, updated_at DATETIME)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO app_settings (key, value) VALUES "
                    "('api_key_auto_pause_on_channel_monitor_unavailable_enabled', 'true'), "
                    "('channel_monitor_fallback_test_models', :models), "
                    "('api_account_auto_pause_on_upstream_monitor_unavailable_enabled', 'false')"
                ),
                {"models": '["model-a"]'},
            )

            await _migrate_management_site_setting_keys(connection)
            rows = {
                row[0]: row[1]
                for row in (
                    await connection.execute(
                        text("SELECT key, value FROM app_settings")
                    )
                ).all()
            }

        self.assertEqual(
            rows["api_account_auto_pause_on_upstream_monitor_unavailable_enabled"],
            "false",
        )
        self.assertEqual(rows["upstream_monitor_fallback_test_models"], '["model-a"]')
        self.assertNotIn(
            "api_key_auto_pause_on_channel_monitor_unavailable_enabled",
            rows,
        )


if __name__ == "__main__":
    unittest.main()
