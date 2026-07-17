import unittest

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import models  # noqa: F401
from app.core.crypto import decrypt_text, encrypt_text
from app.core.database import (
    Base,
    _migrate_upstream_channels,
    _migrate_upstream_rate_change_logs,
    _scrub_upstream_plaintext_secret_copies,
)


LEGACY_ACCOUNT_TABLE = """
CREATE TABLE upstream_account_configs (
    id INTEGER NOT NULL PRIMARY KEY,
    sub2api_account_id INTEGER NOT NULL UNIQUE,
    base_url VARCHAR(500),
    encrypted_access_token TEXT,
    upstream_type VARCHAR(32) NOT NULL,
    resolved_upstream_type VARCHAR(32),
    upstream_user_id VARCHAR(128),
    manual_recharge_multiplier FLOAT,
    discovered_recharge_multiplier FLOAT,
    effective_recharge_multiplier FLOAT,
    recharge_multiplier_source VARCHAR(128),
    recharge_multiplier_status VARCHAR(32),
    group_options JSON,
    balance_remaining FLOAT,
    last_discovered_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


class UpstreamChannelMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.shared_token = "shared-migration-token"
        shared_cipher_one = encrypt_text(self.shared_token)
        shared_cipher_two = encrypt_text(self.shared_token)
        conflict_cipher_one = encrypt_text("first-conflicting-token")
        conflict_cipher_two = encrypt_text("second-conflicting-token")
        self.assertNotEqual(shared_cipher_one, shared_cipher_two)
        async with self.engine.begin() as conn:
            await conn.execute(text(LEGACY_ACCOUNT_TABLE))
            await conn.execute(
                text(
                    "INSERT INTO upstream_account_configs ("
                    "id, sub2api_account_id, base_url, encrypted_access_token, upstream_type, "
                    "resolved_upstream_type, upstream_user_id, manual_recharge_multiplier, "
                    "discovered_recharge_multiplier, effective_recharge_multiplier, "
                    "recharge_multiplier_source, recharge_multiplier_status, group_options, "
                    "balance_remaining, last_discovered_at, created_at, updated_at"
                    ") VALUES "
                    "(1, 101, 'https://Same.Example.com', :shared_cipher_one, 'newapi', 'newapi', "
                    " '42', 0.1, 0.1, 0.1, 'status.price', 'ok', '[{\"id\":\"g\"}]', "
                    " 10, '2026-07-13 01:00:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),"
                    "(2, 102, 'https://same.example.com/v1/', :shared_cipher_two, 'auto', 'newapi', "
                    " '42', 0.1, 0.1, 0.1, 'status.price', 'ok', '[{\"id\":\"g\"}]', "
                    " 20, '2026-07-13 02:00:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),"
                    "(3, 201, 'https://conflict.example.com', :conflict_cipher_one, 'newapi', NULL, "
                    " '7', 0.1, NULL, NULL, NULL, NULL, NULL, "
                    " 30, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),"
                    "(4, 202, 'https://conflict.example.com/api/v1', :conflict_cipher_two, 'sub2api', NULL, "
                    " '8', 0.2, NULL, NULL, NULL, NULL, NULL, "
                    " 40, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "shared_cipher_one": shared_cipher_one,
                    "shared_cipher_two": shared_cipher_two,
                    "conflict_cipher_one": conflict_cipher_one,
                    "conflict_cipher_two": conflict_cipher_two,
                },
            )
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_migrates_channels_without_promoting_account_balance(self) -> None:
        async with self.engine.begin() as conn:
            await _migrate_upstream_channels(conn)

            channels = (
                await conn.execute(
                    text(
                        "SELECT * FROM upstream_channels "
                        "ORDER BY canonical_base_url"
                    )
                )
            ).mappings().all()
            bindings = (
                await conn.execute(
                    text(
                        "SELECT sub2api_account_id, channel_id, balance_remaining "
                        "FROM upstream_account_configs ORDER BY sub2api_account_id"
                    )
                )
            ).mappings().all()

        self.assertEqual(len(channels), 2)
        by_url = {row["canonical_base_url"]: row for row in channels}

        migrated = by_url["https://same.example.com"]
        self.assertEqual(migrated["upstream_type"], "newapi")
        self.assertEqual(migrated["resolved_upstream_type"], "newapi")
        self.assertEqual(decrypt_text(migrated["encrypted_access_token"]), self.shared_token)
        self.assertEqual(migrated["upstream_user_id"], "42")
        self.assertEqual(migrated["manual_recharge_multiplier"], 0.1)
        self.assertEqual(migrated["balance_status"], "not_checked")
        self.assertIsNone(migrated["balance_remaining"])
        self.assertEqual(str(migrated["last_discovered_at"]), "2026-07-13 02:00:00")

        conflicted = by_url["https://conflict.example.com"]
        self.assertEqual(conflicted["upstream_type"], "auto")
        self.assertIsNone(conflicted["encrypted_access_token"])
        self.assertIsNone(conflicted["upstream_user_id"])
        self.assertIsNone(conflicted["manual_recharge_multiplier"])
        self.assertIn("access token", conflicted["last_error"])
        self.assertIn("upstream type", conflicted["last_error"])
        self.assertNotIn("first-conflicting-token", conflicted["last_error"])
        self.assertNotIn("second-conflicting-token", conflicted["last_error"])

        self.assertTrue(all(row["channel_id"] is not None for row in bindings))
        self.assertEqual([row["balance_remaining"] for row in bindings], [10, 20, 30, 40])

    async def test_migration_is_idempotent_and_preserves_legacy_not_null_column(self) -> None:
        async with self.engine.begin() as conn:
            await _migrate_upstream_channels(conn)
            await _migrate_upstream_channels(conn)
            channel_count = (
                await conn.execute(text("SELECT COUNT(*) FROM upstream_channels"))
            ).scalar_one()
            index_count = (
                await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM sqlite_master "
                        "WHERE type = 'index' AND name = 'ix_upstream_account_configs_channel_id'"
                    )
                )
            ).scalar_one()
            columns = {
                row[1]: row
                for row in (
                    await conn.execute(text("PRAGMA table_info(upstream_account_configs)"))
                ).fetchall()
            }
            channel_columns = {
                row[1]: row
                for row in (
                    await conn.execute(text("PRAGMA table_info(upstream_channels)"))
                ).fetchall()
            }

        self.assertEqual(channel_count, 2)
        self.assertEqual(index_count, 1)
        self.assertEqual(columns["upstream_type"][3], 1)
        self.assertIn("channel_id", columns)
        self.assertIn("probe_enabled", channel_columns)
        self.assertEqual(channel_columns["probe_enabled"][3], 1)
        self.assertEqual(str(channel_columns["probe_enabled"][4]).strip("'\""), "1")
        self.assertIn("balance_status", columns)
        self.assertIn("remote_identity_fingerprint", columns)
        self.assertEqual(columns["remote_identity_fingerprint"][3], 0)
        self.assertIn("api_key_origin_rebind_required", columns)
        self.assertEqual(columns["api_key_origin_rebind_required"][3], 1)
        self.assertEqual(str(columns["api_key_origin_rebind_required"][4]), "0")
        self.assertEqual(columns["channel_auto_assign_disabled"][3], 1)
        self.assertTrue(
            set(models.UpstreamAccountConfig.__table__.columns.keys()).issubset(columns)
        )

        async with AsyncSession(self.engine) as session:
            migrated_rows = list(
                (await session.execute(select(models.UpstreamAccountConfig))).scalars().all()
            )
        self.assertEqual(len(migrated_rows), 4)
        self.assertTrue(all(not row.channel_auto_assign_disabled for row in migrated_rows))
        self.assertTrue(all(row.remote_identity_fingerprint is None for row in migrated_rows))
        self.assertTrue(all(not row.api_key_origin_rebind_required for row in migrated_rows))
        self.assertTrue(all(row.balance_status is None for row in migrated_rows))
        self.assertTrue(all(row.upstream_key_status == "not_checked" for row in migrated_rows))
        self.assertTrue(all(row.upstream_group_status == "not_checked" for row in migrated_rows))
        self.assertTrue(all(row.upstream_health_invalid_count == 0 for row in migrated_rows))

    async def test_explicitly_unassigned_account_stays_unassigned_on_migration_rerun(self) -> None:
        async with self.engine.begin() as conn:
            await _migrate_upstream_channels(conn)
            await conn.execute(
                text(
                    "UPDATE upstream_account_configs "
                    "SET channel_id = NULL, channel_auto_assign_disabled = 1 "
                    "WHERE sub2api_account_id = 101"
                )
            )
            await _migrate_upstream_channels(conn)
            row = (
                await conn.execute(
                    text(
                        "SELECT channel_id, channel_auto_assign_disabled "
                        "FROM upstream_account_configs WHERE sub2api_account_id = 101"
                    )
                )
            ).one()

        self.assertIsNone(row.channel_id)
        self.assertEqual(row.channel_auto_assign_disabled, 1)

    async def test_legacy_channel_reference_guards_reject_orphans_and_null_on_delete(self) -> None:
        async with self.engine.begin() as conn:
            await _migrate_upstream_channels(conn)
            trigger_names = {
                row[0]
                for row in (
                    await conn.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type = 'trigger' AND name LIKE 'trg_upstream_%'"
                        )
                    )
                ).all()
            }
            channel_id = (
                await conn.execute(
                    text(
                        "SELECT channel_id FROM upstream_account_configs "
                        "WHERE sub2api_account_id = 101"
                    )
                )
            ).scalar_one()

        self.assertEqual(
            trigger_names,
            {
                "trg_upstream_account_channel_insert",
                "trg_upstream_account_channel_update",
                "trg_upstream_channel_delete",
            },
        )

        with self.assertRaises(IntegrityError):
            async with self.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE upstream_account_configs SET channel_id = 999999 "
                        "WHERE sub2api_account_id = 101"
                    )
                )

        async with self.engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM upstream_channels WHERE id = :channel_id"),
                {"channel_id": channel_id},
            )
            remaining_reference = (
                await conn.execute(
                    text(
                        "SELECT channel_id FROM upstream_account_configs "
                        "WHERE sub2api_account_id = 101"
                    )
                )
            ).scalar_one()

        self.assertIsNone(remaining_reference)

    async def test_adds_refresh_token_column_to_existing_channel_table_idempotently(self) -> None:
        legacy_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with legacy_engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_channels ("
                        "id INTEGER NOT NULL PRIMARY KEY, "
                        "encrypted_access_token TEXT"
                        ")"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_channels (id, encrypted_access_token) "
                        "VALUES (1, 'legacy-access-cipher')"
                    )
                )

                await _migrate_upstream_channels(conn)
                await _migrate_upstream_channels(conn)

                columns = {
                    row[1]
                    for row in (
                        await conn.execute(text("PRAGMA table_info(upstream_channels)"))
                    ).fetchall()
                }
                row = (
                    await conn.execute(
                        text(
                            "SELECT encrypted_access_token, encrypted_refresh_token "
                            "FROM upstream_channels WHERE id = 1"
                        )
                    )
                ).one()
        finally:
            await legacy_engine.dispose()

        self.assertIn("encrypted_refresh_token", columns)
        self.assertEqual(row[0], "legacy-access-cipher")
        self.assertIsNone(row[1])

    async def test_adds_nullable_normalized_multiplier_log_columns_idempotently(self) -> None:
        legacy_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with legacy_engine.begin() as conn:
                await conn.execute(
                    text(
                        "CREATE TABLE upstream_rate_change_logs ("
                        "id INTEGER NOT NULL PRIMARY KEY, "
                        "sub2api_account_id INTEGER NOT NULL"
                        ")"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_rate_change_logs (id, sub2api_account_id) "
                        "VALUES (1, 101)"
                    )
                )

                await _migrate_upstream_rate_change_logs(conn)
                await _migrate_upstream_rate_change_logs(conn)

                columns = {
                    row[1]: row
                    for row in (
                        await conn.execute(text("PRAGMA table_info(upstream_rate_change_logs)"))
                    ).fetchall()
                }
                row = (
                    await conn.execute(
                        text(
                            "SELECT old_upstream_multiplier, new_upstream_multiplier, "
                            "old_upstream_recharge_multiplier, new_upstream_recharge_multiplier, "
                            "old_upstream_key_status, new_upstream_key_status, "
                            "old_upstream_group_status, new_upstream_group_status, "
                            "old_remote_schedulable, new_remote_schedulable "
                            "FROM upstream_rate_change_logs WHERE id = 1"
                        )
                    )
                ).one()
        finally:
            await legacy_engine.dispose()

        self.assertIn("old_upstream_multiplier", columns)
        self.assertIn("new_upstream_multiplier", columns)
        self.assertIn("old_upstream_recharge_multiplier", columns)
        self.assertIn("new_upstream_recharge_multiplier", columns)
        self.assertIn("old_upstream_key_status", columns)
        self.assertIn("new_upstream_key_status", columns)
        self.assertIn("old_upstream_group_status", columns)
        self.assertIn("new_upstream_group_status", columns)
        self.assertIn("old_remote_schedulable", columns)
        self.assertIn("new_remote_schedulable", columns)
        self.assertEqual(columns["old_upstream_multiplier"][3], 0)
        self.assertEqual(columns["new_upstream_multiplier"][3], 0)
        self.assertEqual(columns["old_upstream_recharge_multiplier"][3], 0)
        self.assertEqual(columns["new_upstream_recharge_multiplier"][3], 0)
        self.assertEqual(tuple(row), (None,) * 10)

    async def test_scrubs_plaintext_secret_copies_from_names_and_rate_logs(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        api_key = "sk-migration-secret"
        channel_token = "Bearer at-migration-secret"
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text(
                        "INSERT INTO upstream_channels ("
                        "id, display_name, canonical_base_url, upstream_type, "
                        "encrypted_access_token, group_options, recharge_multiplier_status, "
                        "balance_status, created_at, updated_at"
                        ") VALUES ("
                        "1, 'Channel', 'https://upstream.example', 'auto', :channel_token, "
                        "'[]', 'not_discovered', 'not_checked', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
                        ")"
                    ),
                    {"channel_token": encrypt_text(channel_token)},
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_account_configs ("
                        "sub2api_account_id, channel_id, remote_name, encrypted_api_key, "
                        "upstream_type, group_options, channel_auto_assign_disabled, "
                        "api_key_origin_rebind_required, created_at, updated_at"
                        ") VALUES ("
                        "7, 1, :remote_name, :api_key, 'auto', '[]', 0, 0, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
                        ")"
                    ),
                    {
                        "remote_name": f"account-{api_key}",
                        "api_key": encrypt_text(api_key),
                    },
                )
                await conn.execute(
                    text(
                        "INSERT INTO upstream_rate_change_logs ("
                        "sub2api_account_id, account_name, reason, status, created_at"
                        ") VALUES (7, :account_name, 'test', 'observed', CURRENT_TIMESTAMP)"
                    ),
                    {"account_name": f"rate-{channel_token[7:]}"},
                )

                await _scrub_upstream_plaintext_secret_copies(conn)
                await _scrub_upstream_plaintext_secret_copies(conn)

                remote_name = (
                    await conn.execute(
                        text(
                            "SELECT remote_name FROM upstream_account_configs "
                            "WHERE sub2api_account_id = 7"
                        )
                    )
                ).scalar_one()
                account_name = (
                    await conn.execute(
                        text(
                            "SELECT account_name FROM upstream_rate_change_logs "
                            "WHERE sub2api_account_id = 7"
                        )
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

        self.assertEqual(remote_name, "account-[redacted]")
        self.assertEqual(account_name, "rate-[redacted]")


if __name__ == "__main__":
    unittest.main()
