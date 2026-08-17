import unittest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import models  # noqa: F401
from app.core.database import (
    Base,
    _migrate_persisted_domain_values,
    _migrate_upstream_domain_v2,
    _prepare_upstream_domain_v2,
    _rename_management_account_columns,
    _rename_upstream_domain_v2_columns,
)


UPSTREAM_ID = "01361f2a-897c-4d75-ae8a-b6228d0dc903"


class UpstreamDomainV2MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_pause_reasons_and_invalidates_legacy_json_cache(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE TABLE api_account_pause_holds ("
                        "id INTEGER PRIMARY KEY, api_account_id INTEGER NOT NULL, "
                        "reason VARCHAR(64) NOT NULL, active BOOLEAN NOT NULL, "
                        "resolved_at DATETIME, UNIQUE(api_account_id, reason))"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO api_account_pause_holds VALUES "
                        "(1, 7, 'channel_monitor_unavailable', 1, NULL), "
                        "(2, 7, 'upstream_monitor_unavailable', 0, CURRENT_TIMESTAMP), "
                        "(3, 8, 'channel_monitor_unavailable', 1, NULL)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE TABLE usage_estimate_cache ("
                        "key VARCHAR(32) PRIMARY KEY, payload JSON NOT NULL)"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO usage_estimate_cache VALUES "
                        "('latest', '{\"accounts\":[{\"sub2api_account_id\":\"7\"}]}')"
                    )
                )

                await _migrate_persisted_domain_values(conn)
                await _migrate_persisted_domain_values(conn)

                holds = (
                    await conn.execute(
                        text(
                            "SELECT api_account_id, reason, active, resolved_at "
                            "FROM api_account_pause_holds ORDER BY api_account_id"
                        )
                    )
                ).all()
                self.assertEqual(len(holds), 2)
                self.assertEqual(
                    [(row[0], row[1], bool(row[2])) for row in holds],
                    [
                        (7, "upstream_monitor_unavailable", True),
                        (8, "upstream_monitor_unavailable", True),
                    ],
                )
                self.assertTrue(all(row[3] is None for row in holds))
                self.assertEqual(
                    await conn.scalar(text("SELECT COUNT(*) FROM usage_estimate_cache")),
                    0,
                )
        finally:
            await engine.dispose()

    async def test_prepare_drops_legacy_explicit_indexes_before_archiving(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE upstream_channels (id INTEGER PRIMARY KEY)"))
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_rate_change_logs ("
                        "id INTEGER PRIMARY KEY, status VARCHAR(32))"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE INDEX ix_upstream_rate_change_logs_status "
                        "ON upstream_rate_change_logs (status)"
                    )
                )

                self.assertTrue(await _prepare_upstream_domain_v2(conn))

                tables = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text("SELECT name FROM sqlite_master WHERE type = 'table'")
                        )
                    ).fetchall()
                }
                indexes = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text("SELECT name FROM sqlite_master WHERE type = 'index'")
                        )
                    ).fetchall()
                }
                self.assertIn("legacy_upstream_rate_change_logs_v1", tables)
                self.assertNotIn("ix_upstream_rate_change_logs_status", indexes)
        finally:
            await engine.dispose()

    async def test_renames_management_account_columns_idempotently(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        table_names = (
            "account_snapshots",
            "refresh_jobs",
            "account_exception_records",
            "usage_window_states",
            "usage_limit_samples",
            "usage_token_windows",
        )
        try:
            async with engine.begin() as conn:
                for table_name in table_names:
                    await conn.execute(
                        text(
                            f'CREATE TABLE "{table_name}" ('
                            "id INTEGER PRIMARY KEY, sub2api_account_id VARCHAR(64))"
                        )
                    )
                    await conn.execute(
                        text(
                            f'INSERT INTO "{table_name}" '
                            "(id, sub2api_account_id) VALUES (1, '42')"
                        )
                    )

                await _rename_management_account_columns(conn)
                await _rename_management_account_columns(conn)

                for table_name in table_names:
                    columns = {
                        row[1]
                        for row in (
                            await conn.execute(text(f'PRAGMA table_info("{table_name}")'))
                        ).fetchall()
                    }
                    self.assertIn("management_account_id", columns)
                    self.assertNotIn("sub2api_account_id", columns)
                    self.assertEqual(
                        await conn.scalar(
                            text(
                                f'SELECT management_account_id FROM "{table_name}" '
                                "WHERE id = 1"
                            )
                        ),
                        "42",
                    )
        finally:
            await engine.dispose()

    async def test_renames_intermediate_v2_columns_idempotently(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE TABLE upstreams ("
                        "id VARCHAR(36) PRIMARY KEY, canonical_base_url VARCHAR(500), "
                        "management_base_url VARCHAR(500), upstream_type VARCHAR(32), "
                        "manual_recharge_multiplier FLOAT, deleted_at DATETIME)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE TABLE api_accounts ("
                        "id INTEGER PRIMARY KEY, upstream_id VARCHAR(36), "
                        "upstream_api_key_record_id BIGINT, base_url VARCHAR(500), "
                        "upstream_type VARCHAR(32), manual_group_multiplier FLOAT, "
                        "manual_recharge_multiplier FLOAT)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE TABLE api_account_daily_usages ("
                        "id INTEGER PRIMARY KEY, upstream_api_key_record_id BIGINT)"
                    )
                )

                await _rename_upstream_domain_v2_columns(conn)
                await _rename_upstream_domain_v2_columns(conn)

                upstream_columns = {
                    row[1]
                    for row in (
                        await conn.execute(text("PRAGMA table_info(upstreams)"))
                    ).fetchall()
                }
                account_columns = {
                    row[1]
                    for row in (
                        await conn.execute(text("PRAGMA table_info(api_accounts)"))
                    ).fetchall()
                }
                history_columns = {
                    row[1]
                    for row in (
                        await conn.execute(
                            text("PRAGMA table_info(api_account_daily_usages)")
                        )
                    ).fetchall()
                }

                self.assertIn("api_endpoint_url", upstream_columns)
                self.assertIn("management_url", upstream_columns)
                self.assertIn("platform_type", upstream_columns)
                self.assertIn("upstream_recharge_multiplier_override", upstream_columns)
                self.assertNotIn("canonical_base_url", upstream_columns)
                self.assertIn("remote_upstream_api_key_id", account_columns)
                self.assertIn("api_endpoint_url", account_columns)
                self.assertIn("platform_type", account_columns)
                self.assertIn("upstream_group_multiplier_override", account_columns)
                self.assertNotIn("base_url", account_columns)
                self.assertIn("remote_upstream_api_key_id", history_columns)
        finally:
            await engine.dispose()

    async def test_migrates_identity_history_orphans_amounts_and_is_idempotent(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.execute(text("PRAGMA foreign_keys=ON"))
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_channels ("
                        "id INTEGER PRIMARY KEY, stable_id VARCHAR(36), "
                        "display_name VARCHAR(200) NOT NULL, "
                        "canonical_base_url VARCHAR(500) NOT NULL UNIQUE, "
                        "upstream_type VARCHAR(32) NOT NULL, "
                        "effective_recharge_multiplier FLOAT, "
                        "balance_remaining FLOAT, created_at DATETIME NOT NULL, "
                        "updated_at DATETIME NOT NULL, deleted_at DATETIME)"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_channels VALUES "
                        "(7, :upstream_id, 'Primary', 'https://upstream.example', "
                        "'newapi', 0.1, 12, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)"
                    ),
                    {"upstream_id": UPSTREAM_ID},
                )
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_account_configs ("
                        "id INTEGER PRIMARY KEY, sub2api_account_id BIGINT NOT NULL UNIQUE, "
                        "channel_id INTEGER, upstream_type VARCHAR(32) NOT NULL, "
                        "api_key_origin_rebind_required BOOLEAN NOT NULL DEFAULT 0, "
                        "channel_auto_assign_disabled BOOLEAN NOT NULL DEFAULT 0, "
                        "effective_group_multiplier FLOAT, effective_recharge_multiplier FLOAT, "
                        "management_recharge_multiplier FLOAT, target_rate FLOAT, current_rate FLOAT, "
                        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_account_configs VALUES "
                        "(3, 101, 7, 'newapi', 0, 0, 1, 0.1, 0.2, 0.5, 0.4, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_account_pause_holds ("
                        "id INTEGER PRIMARY KEY, account_config_id INTEGER NOT NULL, "
                        "reason VARCHAR(64) NOT NULL, active BOOLEAN NOT NULL, "
                        "scope_channel_id INTEGER, triggered_at DATETIME NOT NULL, "
                        "resolved_at DATETIME, recovery_mode VARCHAR(64) NOT NULL, "
                        "evidence_json JSON, created_at DATETIME NOT NULL, "
                        "updated_at DATETIME NOT NULL)"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_account_pause_holds VALUES "
                        "(1, 3, 'channel_monitor_unavailable', 1, 7, CURRENT_TIMESTAMP, "
                        "NULL, 'automatic', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "(2, 3, 'upstream_monitor_unavailable', 0, 7, CURRENT_TIMESTAMP, "
                        "CURRENT_TIMESTAMP, 'automatic', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_channel_daily_usages ("
                        "id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
                        "channel_identity VARCHAR(500) NOT NULL, channel_name VARCHAR(200), "
                        "usage_date DATE NOT NULL, balance_used FLOAT, "
                        "balance_used_adjusted FLOAT, recharge_multiplier FLOAT, "
                        "sub2api_cost FLOAT, sub2api_cost_cny FLOAT, "
                        "sub2api_user_cost FLOAT, income FLOAT, "
                        "income_recharge_multiplier FLOAT, profit_cny FLOAT, "
                        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_channel_daily_usages VALUES "
                        "(1, 7, 'https://old-a.example', 'Primary', '2026-08-10', "
                        "1, 0.1, 0.1, 2, 0.4, 3, 0.6, 0.2, 0.2, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                        "(2, 7, 'https://old-b.example', 'Primary', '2026-08-10', "
                        "2, 0.2, 0.1, 4, 0.8, 6, 1.2, 0.2, 0.4, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_account_daily_usages ("
                        "id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
                        "channel_identity VARCHAR(500) NOT NULL, "
                        "sub2api_account_id BIGINT NOT NULL, api_account_id INTEGER, "
                        "upstream_api_key_id INTEGER, usage_date DATE NOT NULL, "
                        "upstream_usage FLOAT, upstream_recharge_multiplier FLOAT, "
                        "sub2api_cost FLOAT, sub2api_user_cost FLOAT, "
                        "management_recharge_multiplier FLOAT, income FLOAT, "
                        "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_account_daily_usages VALUES "
                        "(9, 999, 'orphan.example', 909, NULL, NULL, '2026-08-10', "
                        "5, 0.1, 7, 9, 0.2, 1.8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )

                self.assertTrue(await _prepare_upstream_domain_v2(conn))
                await conn.run_sync(Base.metadata.create_all)
                await _migrate_upstream_domain_v2(conn)
                await _migrate_persisted_domain_values(conn)

                tables = {
                    row[0]
                    for row in (
                        await conn.execute(
                            text("SELECT name FROM sqlite_master WHERE type = 'table'")
                        )
                    ).fetchall()
                }
                self.assertNotIn("upstream_channels", tables)
                self.assertNotIn("upstream_account_configs", tables)
                self.assertIn("upstreams", tables)
                self.assertIn("api_accounts", tables)

                account = (
                    await conn.execute(
                        text(
                            "SELECT id, management_account_id, upstream_id, "
                            "upstream_group_multiplier, upstream_recharge_multiplier, "
                            "management_recharge_multiplier, "
                            "expected_management_billing_multiplier, "
                            "management_billing_multiplier FROM api_accounts"
                        )
                    )
                ).one()
                self.assertEqual(tuple(account), (3, 101, UPSTREAM_ID, 1, 0.1, 0.2, 0.5, 0.4))

                pause_holds = (
                    await conn.execute(
                        text(
                            "SELECT reason, active, resolved_at FROM api_account_pause_holds "
                            "WHERE api_account_id = 3"
                        )
                    )
                ).all()
                self.assertEqual(len(pause_holds), 1)
                self.assertEqual(pause_holds[0][0], "upstream_monitor_unavailable")
                self.assertTrue(bool(pause_holds[0][1]))
                self.assertIsNone(pause_holds[0][2])

                segments = (
                    await conn.execute(
                        text(
                            "SELECT source_segment, upstream_wallet_cost_usd, "
                            "upstream_actual_cost_cny FROM upstream_daily_usages "
                            "WHERE upstream_id = :upstream_id ORDER BY source_segment"
                        ),
                        {"upstream_id": UPSTREAM_ID},
                    )
                ).all()
                self.assertEqual([tuple(row) for row in segments], [(0, 1, 0.1), (1, 2, 0.2)])

                orphan = (
                    await conn.execute(
                        text(
                            "SELECT history.upstream_actual_cost_cny, "
                            "history.management_account_cost_cny, upstream.deleted_at "
                            "FROM api_account_daily_usages AS history "
                            "JOIN upstreams AS upstream ON upstream.id = history.upstream_id "
                            "WHERE history.management_account_id = 909"
                        )
                    )
                ).one()
                self.assertAlmostEqual(orphan[0], 0.5)
                self.assertAlmostEqual(orphan[1], 1.4)
                self.assertIsNotNone(orphan[2])
                self.assertEqual(
                    (await conn.execute(text("PRAGMA foreign_key_check"))).fetchall(),
                    [],
                )

                await _migrate_upstream_domain_v2(conn)
                self.assertEqual(
                    await conn.scalar(text("SELECT COUNT(*) FROM upstream_daily_usages")),
                    2,
                )
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
