from collections.abc import AsyncIterator
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.crypto import decrypt_text
from app.core.upstream_urls import canonicalize_upstream_url


class Base(DeclarativeBase):
    pass


settings = get_settings()

database_url = make_url(settings.database_url)
is_sqlite = database_url.drivername.startswith("sqlite")
connect_args = {"timeout": 30} if is_sqlite else {}
if is_sqlite:
    db_path = database_url.database
    if db_path and db_path != ":memory:":
        path = Path(db_path)
        if not path.is_absolute():
            path = settings.project_root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        database_url = database_url.set(database=path.as_posix())

engine = create_async_engine(database_url, echo=False, future=True, connect_args=connect_args)


if is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def _migration_consensus(
    rows: list[dict],
    field: str,
    *,
    ignored_values: set[str] | None = None,
) -> tuple[object | None, bool]:
    values: list[object] = []
    ignored = ignored_values or set()
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value or value.casefold() in ignored:
                continue
        if not any(value == existing for existing in values):
            values.append(value)
    return (values[0] if len(values) == 1 else None, len(values) > 1)


def _migration_encrypted_consensus(
    rows: list[dict],
    field: str,
) -> tuple[str | None, bool]:
    """Compare encrypted legacy values by plaintext without re-encrypting them."""

    values: dict[str, str] = {}
    invalid_ciphertext = False
    for row in rows:
        raw_value = row.get(field)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        ciphertext = raw_value.strip()
        plaintext = decrypt_text(ciphertext)
        if plaintext is None:
            invalid_ciphertext = True
            continue
        values.setdefault(plaintext, ciphertext)
    conflict = invalid_ciphertext or len(values) > 1
    if conflict or len(values) != 1:
        return None, conflict
    return next(iter(values.values())), False


async def _ensure_upstream_channel_reference_guards(conn: AsyncConnection) -> None:
    """Emulate the channel FK for SQLite databases upgraded via ADD COLUMN."""

    result = await conn.execute(text("PRAGMA foreign_key_list(upstream_account_configs)"))
    has_channel_foreign_key = any(
        str(row[2]) == "upstream_channels" and str(row[3]) == "channel_id"
        for row in result.fetchall()
    )
    if has_channel_foreign_key:
        return

    await conn.execute(
        text(
            "UPDATE upstream_account_configs SET channel_id = NULL "
            "WHERE channel_id IS NOT NULL AND channel_id NOT IN (SELECT id FROM upstream_channels)"
        )
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_upstream_account_channel_insert "
            "BEFORE INSERT ON upstream_account_configs "
            "WHEN NEW.channel_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM upstream_channels WHERE id = NEW.channel_id) "
            "BEGIN SELECT RAISE(ABORT, 'invalid upstream channel reference'); END"
        )
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_upstream_account_channel_update "
            "BEFORE UPDATE OF channel_id ON upstream_account_configs "
            "WHEN NEW.channel_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM upstream_channels WHERE id = NEW.channel_id) "
            "BEGIN SELECT RAISE(ABORT, 'invalid upstream channel reference'); END"
        )
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_upstream_channel_delete "
            "AFTER DELETE ON upstream_channels "
            "BEGIN UPDATE upstream_account_configs SET channel_id = NULL "
            "WHERE channel_id = OLD.id; END"
        )
    )


async def _backfill_normalized_upstream_entities(conn: AsyncConnection) -> None:
    """Create durable remote group/key rows without inferring identity from names."""

    required_tables = {"upstream_channels", "upstream_account_configs", "upstream_groups", "upstream_api_keys"}
    existing_tables = {
        str(row[0])
        for row in (
            await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        ).fetchall()
    }
    if not required_tables.issubset(existing_tables):
        return

    channels = (
        await conn.execute(
            text(
                "SELECT id, group_options, last_discovered_at "
                "FROM upstream_channels ORDER BY id"
            )
        )
    ).mappings().all()
    for channel in channels:
        raw_options = channel["group_options"]
        if isinstance(raw_options, str):
            try:
                raw_options = json.loads(raw_options)
            except (TypeError, ValueError):
                raw_options = []
        if not isinstance(raw_options, list):
            continue
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            remote_group_id = str(option.get("id") or "").strip()
            name = str(option.get("name") or "").strip()
            if not remote_group_id or not name:
                continue
            multiplier = option.get("multiplier")
            if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
                multiplier = None
            await conn.execute(
                text(
                    "INSERT INTO upstream_groups ("
                    "channel_id, remote_group_id, name, multiplier, available, "
                    "last_seen_at, deleted_at, created_at, updated_at) "
                    "VALUES (:channel_id, :remote_group_id, :name, :multiplier, 1, "
                    ":last_seen_at, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(channel_id, remote_group_id) DO UPDATE SET "
                    "name = excluded.name, multiplier = excluded.multiplier, available = 1, "
                    "last_seen_at = COALESCE(excluded.last_seen_at, upstream_groups.last_seen_at), "
                    "deleted_at = NULL, updated_at = CURRENT_TIMESTAMP"
                ),
                {
                    "channel_id": int(channel["id"]),
                    "remote_group_id": remote_group_id,
                    "name": name[:200],
                    "multiplier": multiplier,
                    "last_seen_at": channel["last_discovered_at"],
                },
            )

    accounts = (
        await conn.execute(
            text(
                "SELECT id, channel_id, upstream_api_key_record_id, remote_name, "
                "selected_group_id, selected_group_name, effective_group_multiplier, "
                "upstream_key_status, last_discovered_at "
                "FROM upstream_account_configs "
                "WHERE channel_id IS NOT NULL AND upstream_api_key_record_id IS NOT NULL "
                "ORDER BY id"
            )
        )
    ).mappings().all()
    for account in accounts:
        channel_id = int(account["channel_id"])
        group_row_id = None
        remote_group_id = str(account["selected_group_id"] or "").strip()
        if remote_group_id:
            group_name = str(account["selected_group_name"] or remote_group_id).strip()[:200]
            await conn.execute(
                text(
                    "INSERT INTO upstream_groups ("
                    "channel_id, remote_group_id, name, multiplier, available, "
                    "last_seen_at, deleted_at, created_at, updated_at) "
                    "VALUES (:channel_id, :remote_group_id, :name, :multiplier, 1, "
                    ":last_seen_at, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(channel_id, remote_group_id) DO UPDATE SET "
                    "name = excluded.name, multiplier = COALESCE(excluded.multiplier, upstream_groups.multiplier), "
                    "available = 1, last_seen_at = COALESCE(excluded.last_seen_at, upstream_groups.last_seen_at), "
                    "deleted_at = NULL, updated_at = CURRENT_TIMESTAMP"
                ),
                {
                    "channel_id": channel_id,
                    "remote_group_id": remote_group_id,
                    "name": group_name,
                    "multiplier": account["effective_group_multiplier"],
                    "last_seen_at": account["last_discovered_at"],
                },
            )
            group_row_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM upstream_groups "
                        "WHERE channel_id = :channel_id AND remote_group_id = :remote_group_id"
                    ),
                    {"channel_id": channel_id, "remote_group_id": remote_group_id},
                )
            ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO upstream_api_keys ("
                "channel_id, remote_key_id, remote_name, group_id, status, "
                "last_seen_at, deleted_at, created_at, updated_at) "
                "VALUES (:channel_id, :remote_key_id, :remote_name, :group_id, :status, "
                ":last_seen_at, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "ON CONFLICT(channel_id, remote_key_id) DO UPDATE SET "
                "remote_name = excluded.remote_name, group_id = excluded.group_id, "
                "status = excluded.status, last_seen_at = COALESCE(excluded.last_seen_at, upstream_api_keys.last_seen_at), "
                "deleted_at = NULL, updated_at = CURRENT_TIMESTAMP"
            ),
            {
                "channel_id": channel_id,
                "remote_key_id": int(account["upstream_api_key_record_id"]),
                "remote_name": account["remote_name"],
                "group_id": group_row_id,
                "status": account["upstream_key_status"] or "not_checked",
                "last_seen_at": account["last_discovered_at"],
            },
        )
        key_row_id = (
            await conn.execute(
                text(
                    "SELECT id FROM upstream_api_keys "
                    "WHERE channel_id = :channel_id AND remote_key_id = :remote_key_id"
                ),
                {
                    "channel_id": channel_id,
                    "remote_key_id": int(account["upstream_api_key_record_id"]),
                },
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE upstream_account_configs SET upstream_api_key_id = :key_row_id "
                "WHERE id = :account_id"
            ),
            {"key_row_id": int(key_row_id), "account_id": int(account["id"])},
        )

    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_upstream_account_configs_api_key_id "
            "ON upstream_account_configs (upstream_api_key_id) "
            "WHERE upstream_api_key_id IS NOT NULL"
        )
    )


async def _migrate_upstream_channels(conn: AsyncConnection) -> None:
    result = await conn.execute(text("PRAGMA table_info(upstream_priority_intervals)"))
    priority_interval_columns = {str(row[1]) for row in result.fetchall()}
    allocation_strategy_missing = bool(priority_interval_columns) and (
        "allocation_strategy" not in priority_interval_columns
    )
    optional_priority_interval_columns = {
        "allocation_strategy": "VARCHAR(32) NOT NULL DEFAULT 'cost_optimized'",
        "rate_pause_enabled": "BOOLEAN NOT NULL DEFAULT 0",
        "rate_absolute_threshold": "FLOAT NOT NULL DEFAULT 1",
    }
    for column, column_type in optional_priority_interval_columns.items():
        if priority_interval_columns and column not in priority_interval_columns:
            await conn.execute(
                text(
                    f"ALTER TABLE upstream_priority_intervals ADD COLUMN {column} {column_type}"
                )
            )
            priority_interval_columns.add(column)
    if allocation_strategy_missing:
        # Existing intervals used the rank-based fixed-step allocator. Preserve
        # that behavior during upgrade; newly created intervals use the model default.
        await conn.execute(
            text(
                "UPDATE upstream_priority_intervals "
                "SET allocation_strategy = 'fixed_step'"
            )
        )
    elif "allocation_strategy" in priority_interval_columns:
        await conn.execute(
            text(
                "UPDATE upstream_priority_intervals "
                "SET allocation_strategy = 'cost_optimized' "
                "WHERE allocation_strategy IS NULL "
                "OR allocation_strategy NOT IN ('cost_optimized', 'fixed_step')"
            )
        )

    result = await conn.execute(text("PRAGMA table_info(upstream_channels)"))
    channel_columns = {str(row[1]) for row in result.fetchall()}
    if channel_columns and "probe_enabled" not in channel_columns:
        await conn.execute(
            text(
                "ALTER TABLE upstream_channels ADD COLUMN "
                "probe_enabled BOOLEAN NOT NULL DEFAULT 1"
            )
        )
    if channel_columns and "management_base_url" not in channel_columns:
        await conn.execute(
            text("ALTER TABLE upstream_channels ADD COLUMN management_base_url VARCHAR(500)")
        )
    if channel_columns and "encrypted_refresh_token" not in channel_columns:
        await conn.execute(
            text("ALTER TABLE upstream_channels ADD COLUMN encrypted_refresh_token TEXT")
        )
    optional_channel_columns = {
        "stable_id": "VARCHAR(36)",
        "deleted_at": "DATETIME",
        "archived_canonical_base_url": "VARCHAR(500)",
        "last_known_recharge_multiplier": "FLOAT",
        "today_balance_used": "FLOAT",
        "today_balance_unit": "VARCHAR(32)",
        "today_balance_status": "VARCHAR(32)",
        "today_balance_checked_at": "DATETIME",
        "yesterday_balance_used": "FLOAT",
        "yesterday_balance_unit": "VARCHAR(32)",
        "yesterday_balance_status": "VARCHAR(32)",
        "yesterday_balance_checked_at": "DATETIME",
        "balance_source": "VARCHAR(64)",
        "balance_guard_state": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "balance_guard_basis": "VARCHAR(32)",
        "balance_guard_value": "FLOAT",
        "balance_guard_checked_at": "DATETIME",
        "balance_guard_episode_id": "VARCHAR(64)",
        "balance_guard_paused_count": "INTEGER NOT NULL DEFAULT 0",
        "channel_monitors": "JSON",
        "channel_monitor_test_models": "JSON",
        "channel_monitor_count": "INTEGER NOT NULL DEFAULT 0",
        "channel_monitor_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "channel_monitor_message": "TEXT",
        "channel_monitor_checked_at": "DATETIME",
        "channel_monitor_guard_state": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "channel_monitor_unavailable_count": "INTEGER NOT NULL DEFAULT 0",
        "channel_monitor_recovery_count": "INTEGER NOT NULL DEFAULT 0",
        "channel_monitor_guard_checked_at": "DATETIME",
        "pending_group_options": "JSON",
        "pending_group_removal_count": "INTEGER NOT NULL DEFAULT 0",
        "pending_group_removal_checked_at": "DATETIME",
        "encrypted_login_username": "TEXT",
        "encrypted_login_password": "TEXT",
    }
    for column, column_type in optional_channel_columns.items():
        if channel_columns and column not in channel_columns:
            await conn.execute(
                text(f"ALTER TABLE upstream_channels ADD COLUMN {column} {column_type}")
            )
            channel_columns.add(column)

    if channel_columns and "stable_id" in channel_columns:
        rows = (
            await conn.execute(
                text("SELECT id, stable_id FROM upstream_channels ORDER BY id")
            )
        ).mappings().all()
        seen: set[str] = set()
        for row in rows:
            raw_value = str(row["stable_id"] or "").strip().lower()
            try:
                stable_id = str(UUID(raw_value))
            except (ValueError, AttributeError):
                stable_id = ""
            if not stable_id or stable_id in seen:
                stable_id = str(uuid4())
                await conn.execute(
                    text(
                        "UPDATE upstream_channels SET stable_id = :stable_id "
                        "WHERE id = :channel_id"
                    ),
                    {"stable_id": stable_id, "channel_id": int(row["id"])},
                )
            seen.add(stable_id)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_upstream_channels_stable_id ON upstream_channels (stable_id)"
            )
        )

    recharge_multiplier_columns = [
        column
        for column in (
            "effective_recharge_multiplier",
            "discovered_recharge_multiplier",
        )
        if column in channel_columns
    ]
    if channel_columns and "last_known_recharge_multiplier" in channel_columns and recharge_multiplier_columns:
        recharge_expression = (
            f"COALESCE({', '.join(recharge_multiplier_columns)})"
            if len(recharge_multiplier_columns) > 1
            else recharge_multiplier_columns[0]
        )
        await conn.execute(
            text(
                "UPDATE upstream_channels SET last_known_recharge_multiplier = "
                f"{recharge_expression} "
                "WHERE last_known_recharge_multiplier IS NULL "
                f"AND {recharge_expression} IS NOT NULL"
            )
        )

    result = await conn.execute(text("PRAGMA table_info(upstream_account_configs)"))
    columns = {str(row[1]) for row in result.fetchall()}
    if not columns:
        return
    if "resolved_upstream_type" not in columns:
        await conn.execute(
            text("ALTER TABLE upstream_account_configs ADD COLUMN resolved_upstream_type VARCHAR(32)")
        )
        columns.add("resolved_upstream_type")
    if "channel_id" not in columns:
        await conn.execute(text("ALTER TABLE upstream_account_configs ADD COLUMN channel_id INTEGER"))
        columns.add("channel_id")
    optional_account_columns = {
        "channel_auto_assign_disabled": "BOOLEAN NOT NULL DEFAULT 0",
        "priority_interval_id": "INTEGER",
        "desired_priority": "INTEGER",
        "priority_sync_status": "VARCHAR(32) NOT NULL DEFAULT 'unassigned'",
        "priority_sync_error": "TEXT",
        "last_priority_applied_at": "DATETIME",
        "priority_tiebreak_order": "INTEGER",
        "priority_tiebreak_multiplier": "FLOAT",
        "priority_assignment_when_disabled": "BOOLEAN",
        "rate_pause_policy": "VARCHAR(16) NOT NULL DEFAULT 'inherit'",
        "rate_absolute_threshold": "FLOAT",
        "remote_identity_fingerprint": "VARCHAR(64)",
        "upstream_api_key_record_id": "BIGINT",
        "upstream_api_key_id": "INTEGER",
        "upstream_identity_rebind_required": "BOOLEAN NOT NULL DEFAULT 0",
        "api_key_origin_rebind_required": "BOOLEAN NOT NULL DEFAULT 0",
        "remote_name": "VARCHAR(200)",
        "remote_platform": "VARCHAR(64)",
        "remote_account_type": "VARCHAR(32)",
        "remote_status": "VARCHAR(64)",
        "remote_schedulable": "BOOLEAN",
        "remote_priority": "INTEGER",
          "remote_snapshot": "JSON",
          "remote_snapshot_updated_at": "DATETIME",
          "remote_present": "BOOLEAN NOT NULL DEFAULT 1",
          "remote_missing_at": "DATETIME",
        "last_seen_at": "DATETIME",
        "deleted_at": "DATETIME",
        "base_url": "VARCHAR(500)",
        "encrypted_api_key": "TEXT",
        "encrypted_access_token": "TEXT",
        "upstream_user_id": "VARCHAR(128)",
        "selected_group_id": "VARCHAR(128)",
        "selected_group_name": "VARCHAR(200)",
        "upstream_key_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "upstream_group_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "upstream_health_invalid_count": "INTEGER NOT NULL DEFAULT 0",
        "upstream_key_checked_at": "DATETIME",
        "upstream_group_checked_at": "DATETIME",
        "availability_check_mode": "VARCHAR(32) NOT NULL DEFAULT 'disabled'",
        "availability_monitor_id": "INTEGER",
        "availability_test_model": "VARCHAR(160)",
        "available_models": "JSON",
        "available_models_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "available_models_checked_at": "DATETIME",
        "availability_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "availability_unavailable_count": "INTEGER NOT NULL DEFAULT 0",
        "availability_recovery_count": "INTEGER NOT NULL DEFAULT 0",
        "availability_checked_at": "DATETIME",
        "availability_source": "VARCHAR(32)",
        "availability_message": "TEXT",
        "auto_disabled_reason": "VARCHAR(64)",
        "last_auto_disabled_at": "DATETIME",
        "balance_guard_restore_eligible": "BOOLEAN NOT NULL DEFAULT 0",
        "balance_guard_channel_id": "INTEGER",
        "balance_guard_paused_at": "DATETIME",
        "balance_guard_operation": "VARCHAR(32)",
        "auto_pause_episode_id": "VARCHAR(64)",
        "pause_owned_by_plugin": "BOOLEAN NOT NULL DEFAULT 0",
        "auto_pause_channel_id": "INTEGER",
        "auto_paused_at": "DATETIME",
        "pause_operation": "VARCHAR(32)",
        "manual_group_multiplier": "FLOAT",
        "manual_recharge_multiplier": "FLOAT",
        "group_options": "JSON",
        "discovered_group_multiplier": "FLOAT",
        "effective_group_multiplier": "FLOAT",
        "group_multiplier_source": "VARCHAR(128)",
        "group_multiplier_status": "VARCHAR(32)",
        "discovered_recharge_multiplier": "FLOAT",
        "effective_recharge_multiplier": "FLOAT",
        "recharge_multiplier_source": "VARCHAR(128)",
        "recharge_multiplier_status": "VARCHAR(32)",
        "local_recharge_multiplier": "FLOAT",
        "local_recharge_source": "VARCHAR(32)",
        "local_recharge_status": "VARCHAR(32)",
        "target_rate": "FLOAT",
        "current_rate": "FLOAT",
        "balance_remaining": "FLOAT",
        "balance_total": "FLOAT",
        "balance_used": "FLOAT",
        "balance_unit": "VARCHAR(32)",
        "balance_status": "VARCHAR(32)",
        "balance_source": "VARCHAR(64)",
        "balance_message": "TEXT",
        "balance_checked_at": "DATETIME",
        "upstream_usage_amount": "FLOAT",
        "upstream_usage_unit": "VARCHAR(32)",
        "upstream_usage_checked_at": "DATETIME",
        "today_upstream_usage_amount": "FLOAT",
        "today_upstream_usage_unit": "VARCHAR(32)",
        "today_upstream_usage_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "today_upstream_usage_source": "VARCHAR(64)",
        "today_upstream_usage_checked_at": "DATETIME",
        "today_sub2api_cost_amount": "FLOAT",
        "today_sub2api_user_cost_amount": "FLOAT",
        "today_sub2api_stats_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
        "today_sub2api_stats_checked_at": "DATETIME",
        "last_error": "TEXT",
        "last_discovered_at": "DATETIME",
        "last_applied_at": "DATETIME",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    for column, column_type in optional_account_columns.items():
        if column not in columns:
            await conn.execute(
                text(
                    f"ALTER TABLE upstream_account_configs ADD COLUMN {column} {column_type}"
                )
            )
            columns.add(column)

    await _backfill_normalized_upstream_entities(conn)

    # Relative-increase policies cannot be converted to one absolute threshold
    # without an account-specific baseline. Disable them once during upgrade so
    # they are never silently reinterpreted as an absolute multiplier policy.
    migration_key = "migration.disable_relative_rate_pause.v1"
    completed = await conn.execute(
        text("SELECT value FROM app_settings WHERE key = :key"),
        {"key": migration_key},
    )
    if completed.scalar_one_or_none() is None:
        if "rate_pause_mode" in priority_interval_columns:
            await conn.execute(
                text(
                    "UPDATE upstream_priority_intervals SET rate_pause_enabled = 0 "
                    "WHERE rate_pause_enabled = 1 "
                    "AND COALESCE(rate_pause_mode, 'increase_percent') "
                    "<> 'absolute_multiplier'"
                )
            )
        if "rate_pause_mode" in columns:
            await conn.execute(
                text(
                    "UPDATE upstream_account_configs SET rate_pause_policy = 'disabled', "
                    "rate_absolute_threshold = NULL "
                    "WHERE rate_pause_policy = 'custom' "
                    "AND COALESCE(rate_pause_mode, 'increase_percent') "
                    "<> 'absolute_multiplier'"
                )
            )
        await conn.execute(
            text(
                "INSERT INTO app_settings (key, value, updated_at) "
                "VALUES (:key, '1', CURRENT_TIMESTAMP)"
            ),
            {"key": migration_key},
        )
    await conn.execute(
        text(
            "DROP INDEX IF EXISTS "
            "uq_upstream_account_configs_channel_record_id"
        )
    )
    await conn.execute(
        text(
            "UPDATE upstream_account_configs SET "
            "availability_monitor_id = NULL, "
            "availability_test_model = NULL, "
            "availability_status = 'disabled', "
            "availability_unavailable_count = 0, "
            "availability_recovery_count = 0, "
            "availability_checked_at = NULL, "
            "availability_source = NULL, "
            "availability_message = NULL "
            "WHERE availability_check_mode = 'disabled'"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_upstream_account_configs_channel_id "
            "ON upstream_account_configs (channel_id)"
        )
    )
    await conn.execute(
        text(
            "UPDATE upstream_account_configs SET "
            "pause_owned_by_plugin = 1, "
            "auto_pause_episode_id = COALESCE(auto_pause_episode_id, lower(hex(randomblob(16)))), "
            "auto_pause_channel_id = COALESCE(auto_pause_channel_id, balance_guard_channel_id, channel_id), "
            "auto_paused_at = COALESCE(auto_paused_at, balance_guard_paused_at, last_auto_disabled_at), "
            "pause_operation = COALESCE(pause_operation, balance_guard_operation, 'paused') "
            "WHERE COALESCE(balance_guard_restore_eligible, 0) = 1 "
            "OR auto_disabled_reason IN "
            "('upstream_key_unavailable', 'upstream_group_unavailable')"
        )
    )
    await conn.execute(
        text(
            "INSERT OR IGNORE INTO upstream_account_pause_holds "
            "(account_config_id, reason, active, scope_channel_id, triggered_at, "
            "resolved_at, recovery_mode, evidence_json, created_at, updated_at) "
            "SELECT id, 'upstream_balance_negative', 1, balance_guard_channel_id, "
            "COALESCE(balance_guard_paused_at, last_auto_disabled_at, CURRENT_TIMESTAMP), "
            "NULL, 'balance_positive', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM upstream_account_configs "
            "WHERE COALESCE(balance_guard_restore_eligible, 0) = 1"
        )
    )
    await conn.execute(
        text(
            "INSERT OR IGNORE INTO upstream_account_pause_holds "
            "(account_config_id, reason, active, scope_channel_id, triggered_at, "
            "resolved_at, recovery_mode, evidence_json, created_at, updated_at) "
            "SELECT id, auto_disabled_reason, 1, channel_id, "
            "COALESCE(last_auto_disabled_at, CURRENT_TIMESTAMP), NULL, "
            "'upstream_healthy', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM upstream_account_configs "
            "WHERE auto_disabled_reason IN "
            "('upstream_key_unavailable', 'upstream_group_unavailable')"
        )
    )
    await _ensure_upstream_channel_reference_guards(conn)

    migration_fields = (
        "id",
        "base_url",
        "upstream_type",
        "resolved_upstream_type",
        "encrypted_access_token",
        "upstream_user_id",
        "manual_recharge_multiplier",
        "discovered_recharge_multiplier",
        "effective_recharge_multiplier",
        "recharge_multiplier_source",
        "recharge_multiplier_status",
        "group_options",
        "last_discovered_at",
    )
    select_fields = [field if field in columns else f"NULL AS {field}" for field in migration_fields]
    result = await conn.execute(
        text(
            f"SELECT {', '.join(select_fields)} FROM upstream_account_configs "
            "WHERE channel_id IS NULL "
            "AND COALESCE(channel_auto_assign_disabled, 0) = 0 "
            "AND TRIM(COALESCE(base_url, '')) <> ''"
        )
    )
    grouped_rows: dict[str, list[dict]] = {}
    for raw_row in result.mappings().all():
        row = dict(raw_row)
        try:
            canonical_url = canonicalize_upstream_url(str(row.get("base_url") or ""))
        except (TypeError, ValueError):
            continue
        grouped_rows.setdefault(canonical_url, []).append(row)

    for canonical_url, rows in grouped_rows.items():
        result = await conn.execute(
            text("SELECT id FROM upstream_channels WHERE canonical_base_url = :canonical_base_url"),
            {"canonical_base_url": canonical_url},
        )
        channel_id = result.scalar_one_or_none()
        if channel_id is None:
            conflicts: list[str] = []

            upstream_type, upstream_type_conflict = _migration_consensus(
                rows,
                "upstream_type",
                ignored_values={"auto"},
            )
            if upstream_type_conflict:
                conflicts.append("upstream type")
            if upstream_type not in {"newapi", "sub2api"}:
                upstream_type = "auto"

            resolved_type, resolved_type_conflict = _migration_consensus(
                rows,
                "resolved_upstream_type",
            )
            if resolved_type_conflict:
                conflicts.append("resolved upstream type")
            if resolved_type not in {"newapi", "sub2api"}:
                resolved_type = None

            access_token_cipher, access_token_conflict = _migration_encrypted_consensus(
                rows,
                "encrypted_access_token",
            )
            if access_token_conflict:
                conflicts.append("access token")

            upstream_user_id, upstream_user_id_conflict = _migration_consensus(
                rows,
                "upstream_user_id",
            )
            if upstream_user_id_conflict:
                conflicts.append("upstream user id")

            manual_recharge, manual_recharge_conflict = _migration_consensus(
                rows,
                "manual_recharge_multiplier",
            )
            if manual_recharge_conflict:
                conflicts.append("manual recharge multiplier")

            recharge_fields = (
                "discovered_recharge_multiplier",
                "effective_recharge_multiplier",
                "recharge_multiplier_source",
                "recharge_multiplier_status",
            )
            recharge_values: dict[str, object | None] = {}
            recharge_conflict = False
            for field in recharge_fields:
                value, conflict = _migration_consensus(rows, field)
                recharge_values[field] = value
                recharge_conflict = recharge_conflict or conflict
            if recharge_conflict:
                conflicts.append("recharge discovery")
                recharge_values = {field: None for field in recharge_fields}

            group_options, group_options_conflict = _migration_consensus(rows, "group_options")
            if group_options_conflict:
                conflicts.append("group options")

            discovered_dates = [
                str(row["last_discovered_at"])
                for row in rows
                if row.get("last_discovered_at") is not None
            ]
            last_discovered_at = max(discovered_dates) if discovered_dates else None
            parsed_url = urlsplit(canonical_url)
            display_name = f"{parsed_url.netloc}{parsed_url.path}" or canonical_url
            last_error = None
            if conflicts:
                last_error = (
                    "Legacy channel migration left conflicting fields unset: "
                    + ", ".join(conflicts)
                    + "."
                )

            await conn.execute(
                text(
                    "INSERT INTO upstream_channels ("
                    "display_name, canonical_base_url, upstream_type, resolved_upstream_type, "
                    "encrypted_access_token, upstream_user_id, manual_recharge_multiplier, "
                    "discovered_recharge_multiplier, effective_recharge_multiplier, "
                    "recharge_multiplier_source, recharge_multiplier_status, group_options, "
                    "balance_status, last_error, last_discovered_at, created_at, updated_at"
                    ") VALUES ("
                    ":display_name, :canonical_base_url, :upstream_type, :resolved_upstream_type, "
                    ":encrypted_access_token, :upstream_user_id, :manual_recharge_multiplier, "
                    ":discovered_recharge_multiplier, :effective_recharge_multiplier, "
                    ":recharge_multiplier_source, :recharge_multiplier_status, :group_options, "
                    "'not_checked', :last_error, :last_discovered_at, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
                    ") ON CONFLICT(canonical_base_url) DO NOTHING"
                ),
                {
                    "display_name": display_name,
                    "canonical_base_url": canonical_url,
                    "upstream_type": upstream_type,
                    "resolved_upstream_type": resolved_type,
                    "encrypted_access_token": access_token_cipher,
                    "upstream_user_id": upstream_user_id,
                    "manual_recharge_multiplier": manual_recharge,
                    "discovered_recharge_multiplier": recharge_values["discovered_recharge_multiplier"],
                    "effective_recharge_multiplier": recharge_values["effective_recharge_multiplier"],
                    "recharge_multiplier_source": recharge_values["recharge_multiplier_source"],
                    "recharge_multiplier_status": recharge_values["recharge_multiplier_status"] or "not_discovered",
                    "group_options": group_options,
                    "last_error": last_error,
                    "last_discovered_at": last_discovered_at,
                },
            )
            result = await conn.execute(
                text("SELECT id FROM upstream_channels WHERE canonical_base_url = :canonical_base_url"),
                {"canonical_base_url": canonical_url},
            )
            channel_id = result.scalar_one()

        for row in rows:
            await conn.execute(
                text(
                    "UPDATE upstream_account_configs SET channel_id = :channel_id "
                    "WHERE id = :account_config_id AND channel_id IS NULL"
                ),
                {"channel_id": channel_id, "account_config_id": row["id"]},
            )

    duplicate_upstream_identity = (
        await conn.execute(
            text(
                "SELECT channel_id, upstream_api_key_record_id "
                "FROM upstream_account_configs "
                "WHERE channel_id IS NOT NULL "
                "AND upstream_api_key_record_id IS NOT NULL "
                "GROUP BY channel_id, upstream_api_key_record_id "
                "HAVING COUNT(*) > 1"
            )
        )
    ).mappings().all()
    for duplicate in duplicate_upstream_identity:
        await conn.execute(
            text(
                "UPDATE upstream_account_configs SET "
                "upstream_api_key_record_id = NULL, "
                "upstream_identity_rebind_required = 1, "
                "last_error = 'Multiple local accounts used the same upstream API key "
                "record ID; explicit identity confirmation is required.' "
                "WHERE channel_id = :channel_id "
                "AND upstream_api_key_record_id = :record_id"
            ),
            {
                "channel_id": duplicate["channel_id"],
                "record_id": duplicate["upstream_api_key_record_id"],
            },
        )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_upstream_account_configs_channel_record_id "
            "ON upstream_account_configs (channel_id, upstream_api_key_record_id) "
            "WHERE channel_id IS NOT NULL "
            "AND upstream_api_key_record_id IS NOT NULL"
        )
    )


async def _migrate_upstream_rate_change_logs(conn: AsyncConnection) -> None:
    result = await conn.execute(text("PRAGMA table_info(upstream_rate_change_logs)"))
    columns = {str(row[1]) for row in result.fetchall()}
    if not columns:
        return
    for column in (
        "old_upstream_multiplier",
        "new_upstream_multiplier",
        "old_upstream_recharge_multiplier",
        "new_upstream_recharge_multiplier",
    ):
        if column not in columns:
            await conn.execute(
                text(f"ALTER TABLE upstream_rate_change_logs ADD COLUMN {column} FLOAT")
            )
    result = await conn.execute(text("PRAGMA table_info(upstream_rate_change_logs)"))
    columns = {str(row[1]) for row in result.fetchall()}
    extra_columns = {
        "old_group_id": "VARCHAR(128)",
        "new_group_id": "VARCHAR(128)",
        "old_group_name": "VARCHAR(200)",
        "new_group_name": "VARCHAR(200)",
        "old_upstream_key_status": "VARCHAR(32)",
        "new_upstream_key_status": "VARCHAR(32)",
        "old_upstream_group_status": "VARCHAR(32)",
        "new_upstream_group_status": "VARCHAR(32)",
        "old_remote_schedulable": "BOOLEAN",
        "new_remote_schedulable": "BOOLEAN",
    }
    for column, column_type in extra_columns.items():
        if column not in columns:
            await conn.execute(
                text(f"ALTER TABLE upstream_rate_change_logs ADD COLUMN {column} {column_type}")
            )


async def _migrate_notification_outbox(conn: AsyncConnection) -> None:
    result = await conn.execute(text("PRAGMA table_info(notification_outbox)"))
    columns = {str(row[1]) for row in result.fetchall()}
    if not columns:
        return
    optional_columns = {
        "claim_token": "VARCHAR(64)",
        "claimed_at": "DATETIME",
        "claim_expires_at": "DATETIME",
    }
    for column, column_type in optional_columns.items():
        if column not in columns:
            await conn.execute(
                text(f"ALTER TABLE notification_outbox ADD COLUMN {column} {column_type}")
            )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_notification_outbox_claim_token "
            "ON notification_outbox (claim_token)"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_notification_outbox_claim_expires_at "
            "ON notification_outbox (claim_expires_at)"
        )
    )


async def _migrate_upstream_usage_history(conn: AsyncConnection) -> None:
    """Upgrade pre-identity usage tables without discarding accounting data."""

    channel_columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_channel_daily_usages)"))
        ).fetchall()
    }
    channel_identity_missing = bool(channel_columns) and "channel_identity" not in channel_columns
    if channel_identity_missing:
        await conn.execute(
            text(
                "ALTER TABLE upstream_channel_daily_usages "
                "ADD COLUMN channel_identity VARCHAR(500)"
            )
        )
    if channel_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_daily_usages SET channel_identity = COALESCE("
                "NULLIF(TRIM(channel_identity), ''), "
                "(SELECT canonical_base_url FROM upstream_channels "
                "WHERE upstream_channels.id = upstream_channel_daily_usages.channel_id), "
                "'channel:' || channel_id)"
            )
        )

    if channel_identity_missing:
        # SQLite cannot drop the legacy UNIQUE(channel_id, usage_date)
        # constraint. Rebuild the table so a deleted channel ID can be reused
        # without mixing or blocking the previous upstream's history.
        await conn.execute(text("DROP TABLE IF EXISTS upstream_channel_daily_usages_new"))
        await conn.execute(
            text(
                "CREATE TABLE upstream_channel_daily_usages_new ("
                "id INTEGER NOT NULL PRIMARY KEY, "
                "channel_id INTEGER NOT NULL, "
                "channel_identity VARCHAR(500) NOT NULL, "
                "channel_name VARCHAR(200), "
                "usage_date DATE NOT NULL, "
                "balance_used FLOAT, "
                "balance_used_adjusted FLOAT, "
                "balance_unit VARCHAR(32), "
                "recharge_multiplier FLOAT, "
                "upstream_api_key_usage FLOAT, "
                "upstream_api_key_cost_cny FLOAT, "
                "sub2api_cost FLOAT, "
                "sub2api_cost_cny FLOAT, "
                "sub2api_user_cost FLOAT, "
                "sub2api_actual_cost FLOAT, "
                "income FLOAT, "
                "income_recharge_multiplier FLOAT, "
                "income_unit VARCHAR(32), "
                "profit_cny FLOAT, "
                "finalized BOOLEAN NOT NULL DEFAULT 0, "
                "observed_at DATETIME, "
                "finalized_at DATETIME, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_upstream_channel_daily_usage_identity_date "
                "UNIQUE (channel_identity, usage_date))"
            )
        )
        await conn.execute(
            text(
                "INSERT OR REPLACE INTO upstream_channel_daily_usages_new ("
                "id, channel_id, channel_identity, channel_name, usage_date, "
                "balance_used, balance_used_adjusted, balance_unit, recharge_multiplier, "
                "upstream_api_key_usage, income, income_unit, finalized, observed_at, "
                "finalized_at, created_at, updated_at) "
                "SELECT id, channel_id, channel_identity, channel_name, usage_date, "
                "balance_used, balance_used_adjusted, balance_unit, recharge_multiplier, "
                "upstream_api_key_usage, income, income_unit, finalized, observed_at, "
                "finalized_at, created_at, updated_at "
                "FROM upstream_channel_daily_usages ORDER BY id"
            )
        )
        await conn.execute(text("DROP TABLE upstream_channel_daily_usages"))
        await conn.execute(
            text(
                "ALTER TABLE upstream_channel_daily_usages_new "
                "RENAME TO upstream_channel_daily_usages"
            )
        )

    channel_columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_channel_daily_usages)"))
        ).fetchall()
    }
    for column_name in (
        "upstream_api_key_cost_cny",
        "sub2api_cost",
        "sub2api_cost_cny",
        "sub2api_user_cost",
        "sub2api_actual_cost",
        "income_recharge_multiplier",
        "profit_cny",
    ):
        if channel_columns and column_name not in channel_columns:
            await conn.execute(
                text(
                    f"ALTER TABLE upstream_channel_daily_usages "
                    f"ADD COLUMN {column_name} FLOAT"
                )
            )
            channel_columns.add(column_name)

    if channel_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_daily_usages SET "
                "sub2api_cost = COALESCE(sub2api_cost, sub2api_actual_cost), "
                "sub2api_user_cost = COALESCE(sub2api_user_cost, sub2api_actual_cost), "
                "sub2api_cost_cny = COALESCE("
                "sub2api_cost_cny, sub2api_cost * income_recharge_multiplier)"
            )
        )
        if {"upstream_api_key_usage", "recharge_multiplier"}.issubset(channel_columns):
            await conn.execute(
                text(
                    "UPDATE upstream_channel_daily_usages SET "
                    "upstream_api_key_cost_cny = COALESCE("
                    "upstream_api_key_cost_cny, "
                    "upstream_api_key_usage * recharge_multiplier)"
                )
            )
    if channel_columns and "income" in channel_columns:
        upstream_cost_expression = (
            "COALESCE(upstream_api_key_cost_cny, balance_used_adjusted, 0)"
            if "balance_used_adjusted" in channel_columns
            else "COALESCE(upstream_api_key_cost_cny, 0)"
        )
        await conn.execute(
            text(
                "UPDATE upstream_channel_daily_usages SET profit_cny = COALESCE("
                "profit_cny, income - MAX("
                f"{upstream_cost_expression}, "
                "COALESCE(sub2api_cost_cny, 0))) WHERE income IS NOT NULL"
            )
        )

    if channel_columns:
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_upstream_channel_daily_usage_identity_date "
            "ON upstream_channel_daily_usages (channel_identity, usage_date)",
            "CREATE INDEX IF NOT EXISTS ix_upstream_channel_daily_usage_channel_date "
            "ON upstream_channel_daily_usages (channel_id, usage_date)",
            "CREATE INDEX IF NOT EXISTS ix_upstream_channel_daily_usages_channel_id "
            "ON upstream_channel_daily_usages (channel_id)",
            "CREATE INDEX IF NOT EXISTS ix_upstream_channel_daily_usages_channel_identity "
            "ON upstream_channel_daily_usages (channel_identity)",
            "CREATE INDEX IF NOT EXISTS ix_upstream_channel_daily_usages_usage_date "
            "ON upstream_channel_daily_usages (usage_date)",
            "CREATE INDEX IF NOT EXISTS ix_upstream_channel_daily_usages_finalized "
            "ON upstream_channel_daily_usages (finalized)",
        ):
            await conn.execute(text(statement))

    account_columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_account_daily_usages)"))
        ).fetchall()
    }
    account_config_columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_account_configs)"))
        ).fetchall()
    }
    if account_columns and "channel_identity" not in account_columns:
        await conn.execute(
            text(
                "ALTER TABLE upstream_account_daily_usages "
                "ADD COLUMN channel_identity VARCHAR(500)"
            )
        )
        account_columns.add("channel_identity")
    for column_name in ("api_account_id", "upstream_api_key_id"):
        if account_columns and column_name not in account_columns:
            await conn.execute(
                text(
                    "ALTER TABLE upstream_account_daily_usages "
                    f"ADD COLUMN {column_name} INTEGER"
                )
            )
            account_columns.add(column_name)
    for column_name in (
        "upstream_recharge_multiplier",
        "sub2api_cost",
        "sub2api_user_cost",
        "sub2api_actual_cost",
        "local_recharge_multiplier",
    ):
        if account_columns and column_name not in account_columns:
            await conn.execute(
                text(
                    f"ALTER TABLE upstream_account_daily_usages "
                    f"ADD COLUMN {column_name} FLOAT"
                )
            )
            account_columns.add(column_name)
    if account_columns:
        if (
            {"id", "sub2api_account_id"}.issubset(account_config_columns)
            and "api_account_id" in account_columns
        ):
            await conn.execute(
                text(
                    "UPDATE upstream_account_daily_usages SET api_account_id = "
                    "COALESCE(api_account_id, (SELECT id FROM upstream_account_configs "
                    "WHERE upstream_account_configs.sub2api_account_id = "
                    "upstream_account_daily_usages.sub2api_account_id LIMIT 1))"
                )
            )
        if (
            {"sub2api_account_id", "upstream_api_key_id"}.issubset(
                account_config_columns
            )
            and "upstream_api_key_id" in account_columns
        ):
            await conn.execute(
                text(
                    "UPDATE upstream_account_daily_usages SET upstream_api_key_id = "
                    "COALESCE(upstream_api_key_id, (SELECT upstream_api_key_id "
                    "FROM upstream_account_configs WHERE "
                    "upstream_account_configs.sub2api_account_id = "
                    "upstream_account_daily_usages.sub2api_account_id LIMIT 1))"
                )
            )
        await conn.execute(
            text(
                "UPDATE upstream_account_daily_usages SET "
                "sub2api_cost = COALESCE(sub2api_cost, sub2api_actual_cost), "
                "sub2api_user_cost = COALESCE(sub2api_user_cost, sub2api_actual_cost), "
                "channel_identity = COALESCE("
                "NULLIF(TRIM(channel_identity), ''), "
                "(SELECT canonical_base_url FROM upstream_channels "
                "WHERE upstream_channels.id = upstream_account_daily_usages.channel_id), "
                "(SELECT channel_identity FROM upstream_channel_daily_usages "
                "WHERE upstream_channel_daily_usages.channel_id = "
                "upstream_account_daily_usages.channel_id "
                "ORDER BY usage_date DESC, id DESC LIMIT 1), "
                "'channel:' || channel_id)"
            )
        )
        if "recharge_multiplier" in channel_columns:
            await conn.execute(
                text(
                    "UPDATE upstream_account_daily_usages SET "
                    "upstream_recharge_multiplier = COALESCE("
                    "upstream_recharge_multiplier, (SELECT recharge_multiplier "
                    "FROM upstream_channel_daily_usages WHERE "
                    "upstream_channel_daily_usages.channel_identity = "
                    "upstream_account_daily_usages.channel_identity AND "
                    "upstream_channel_daily_usages.usage_date = "
                    "upstream_account_daily_usages.usage_date LIMIT 1))"
                )
            )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_upstream_account_daily_usage_identity_date "
                "ON upstream_account_daily_usages (channel_identity, usage_date)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_upstream_account_daily_usages_channel_identity "
                "ON upstream_account_daily_usages (channel_identity)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_upstream_account_daily_usages_api_account_id "
                "ON upstream_account_daily_usages (api_account_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_upstream_account_daily_usages_upstream_api_key_id "
                "ON upstream_account_daily_usages (upstream_api_key_id)"
            )
        )

    total_columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_channel_usage_totals)"))
        ).fetchall()
    }
    if total_columns and "channel_identity" not in total_columns:
        await conn.execute(text("DROP TABLE IF EXISTS upstream_channel_usage_totals_new"))
        await conn.execute(
            text(
                "CREATE TABLE upstream_channel_usage_totals_new ("
                "channel_identity VARCHAR(500) NOT NULL PRIMARY KEY, "
                "channel_id INTEGER NOT NULL, "
                "channel_name VARCHAR(200), "
                "total_balance_used FLOAT NOT NULL, "
                "total_balance_used_adjusted FLOAT NOT NULL, "
                "total_upstream_api_key_usage FLOAT NOT NULL, "
                "total_upstream_api_key_cost_cny FLOAT NOT NULL DEFAULT 0, "
                "total_sub2api_cost FLOAT NOT NULL DEFAULT 0, "
                "total_sub2api_cost_cny FLOAT NOT NULL DEFAULT 0, "
                "total_sub2api_user_cost FLOAT NOT NULL DEFAULT 0, "
                "total_sub2api_actual_cost FLOAT NOT NULL DEFAULT 0, "
                "total_income FLOAT NOT NULL, "
                "total_profit_cny FLOAT NOT NULL DEFAULT 0, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO upstream_channel_usage_totals_new ("
                "channel_identity, channel_id, channel_name, total_balance_used, "
                "total_balance_used_adjusted, total_upstream_api_key_usage, "
                "total_upstream_api_key_cost_cny, total_sub2api_cost, "
                "total_sub2api_cost_cny, total_sub2api_user_cost, "
                "total_sub2api_actual_cost, total_income, total_profit_cny, "
                "created_at, updated_at) "
                "SELECT COALESCE((SELECT canonical_base_url FROM upstream_channels "
                "WHERE upstream_channels.id = upstream_channel_usage_totals.channel_id), "
                "'channel:' || channel_id), MAX(channel_id), MAX(channel_name), "
                "SUM(COALESCE(total_balance_used, 0)), "
                "SUM(COALESCE(total_balance_used_adjusted, 0)), "
                "SUM(COALESCE(total_upstream_api_key_usage, 0)), "
                "SUM(COALESCE(total_balance_used_adjusted, 0)), 0, 0, 0, 0, "
                "SUM(COALESCE(total_income, 0)), "
                "SUM(COALESCE(total_income, 0)) - "
                "SUM(COALESCE(total_balance_used_adjusted, 0)), "
                "MIN(created_at), MAX(updated_at) "
                "FROM upstream_channel_usage_totals "
                "GROUP BY COALESCE((SELECT canonical_base_url FROM upstream_channels "
                "WHERE upstream_channels.id = upstream_channel_usage_totals.channel_id), "
                "'channel:' || channel_id)"
            )
        )
        await conn.execute(text("DROP TABLE upstream_channel_usage_totals"))
        await conn.execute(
            text(
                "ALTER TABLE upstream_channel_usage_totals_new "
                "RENAME TO upstream_channel_usage_totals"
            )
        )
    total_columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_channel_usage_totals)"))
        ).fetchall()
    }
    added_total_columns: set[str] = set()
    for column_name in (
        "total_upstream_api_key_cost_cny",
        "total_sub2api_cost",
        "total_sub2api_cost_cny",
        "total_sub2api_user_cost",
        "total_sub2api_actual_cost",
        "total_profit_cny",
    ):
        if total_columns and column_name not in total_columns:
            await conn.execute(
                text(
                    "ALTER TABLE upstream_channel_usage_totals "
                    f"ADD COLUMN {column_name} FLOAT NOT NULL DEFAULT 0"
                )
            )
            total_columns.add(column_name)
            added_total_columns.add(column_name)
    if "total_sub2api_cost" in added_total_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_usage_totals SET "
                "total_sub2api_cost = COALESCE(total_sub2api_actual_cost, 0)"
            )
        )
    if "total_sub2api_user_cost" in added_total_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_usage_totals SET "
                "total_sub2api_user_cost = COALESCE(total_sub2api_actual_cost, 0)"
            )
        )
    if (
        "total_upstream_api_key_cost_cny" in added_total_columns
        and "total_balance_used_adjusted" in total_columns
    ):
        await conn.execute(
            text(
                "UPDATE upstream_channel_usage_totals SET "
                "total_upstream_api_key_cost_cny = COALESCE("
                "(SELECT SUM(upstream_api_key_cost_cny) "
                "FROM upstream_channel_daily_usages WHERE "
                "upstream_channel_daily_usages.channel_identity = "
                "upstream_channel_usage_totals.channel_identity), "
                "total_balance_used_adjusted, 0)"
            )
        )
    if "total_sub2api_cost_cny" in added_total_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_usage_totals SET "
                "total_sub2api_cost_cny = COALESCE("
                "(SELECT SUM(sub2api_cost_cny) "
                "FROM upstream_channel_daily_usages WHERE "
                "upstream_channel_daily_usages.channel_identity = "
                "upstream_channel_usage_totals.channel_identity), 0)"
            )
        )
    if "total_profit_cny" in added_total_columns and "total_income" in total_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_usage_totals SET "
                "total_profit_cny = COALESCE("
                "(SELECT SUM(profit_cny) FROM upstream_channel_daily_usages WHERE "
                "upstream_channel_daily_usages.channel_identity = "
                "upstream_channel_usage_totals.channel_identity), "
                "COALESCE(total_income, 0) - MAX("
                "COALESCE(total_upstream_api_key_cost_cny, 0), "
                "COALESCE(total_sub2api_cost_cny, 0)))"
            )
        )
    if total_columns:
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_upstream_channel_usage_totals_channel_id "
                "ON upstream_channel_usage_totals (channel_id)"
            )
        )

    upstream_columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_channels)"))
        ).fetchall()
    }

    if "stable_id" in upstream_columns:
        archived_candidates: dict[int, str | None] = {}
        for table_name, columns in (
            ("upstream_channel_daily_usages", channel_columns),
            ("upstream_account_daily_usages", account_columns),
            ("upstream_channel_usage_totals", total_columns),
        ):
            if "channel_id" not in columns:
                continue
            name_expression = "MAX(channel_name)" if "channel_name" in columns else "NULL"
            rows = (
                await conn.execute(
                    text(
                        f"SELECT channel_id, {name_expression} AS channel_name "
                        f"FROM {table_name} GROUP BY channel_id"
                    )
                )
            ).mappings().all()
            for row in rows:
                if row["channel_id"] is None:
                    continue
                archived_candidates.setdefault(
                    int(row["channel_id"]),
                    str(row["channel_name"] or "").strip() or None,
                )

        existing_ids = {
            int(value)
            for value in (
                await conn.execute(text("SELECT id FROM upstream_channels"))
            ).scalars()
        }
        for orphan_id, archived_name in sorted(archived_candidates.items()):
            if orphan_id in existing_ids:
                continue
            archived_uuid = str(uuid4())
            archived_values = {
                "id": orphan_id,
                "stable_id": archived_uuid,
                "display_name": (archived_name or f"Archived upstream #{orphan_id}")[:200],
                "canonical_base_url": (
                    f"archived://legacy-upstream/{orphan_id}/{archived_uuid}"
                ),
                "upstream_type": "auto",
                "probe_enabled": 0,
                "deleted_at": "CURRENT_TIMESTAMP",
                "created_at": "CURRENT_TIMESTAMP",
                "updated_at": "CURRENT_TIMESTAMP",
            }
            insert_columns = [
                column for column in archived_values if column in upstream_columns
            ]
            value_expressions = [
                archived_values[column]
                if archived_values[column] == "CURRENT_TIMESTAMP"
                else f":{column}"
                for column in insert_columns
            ]
            await conn.execute(
                text(
                    f"INSERT INTO upstream_channels ({', '.join(insert_columns)}) "
                    f"VALUES ({', '.join(value_expressions)})"
                ),
                {
                    column: value
                    for column, value in archived_values.items()
                    if column in insert_columns and value != "CURRENT_TIMESTAMP"
                },
            )
            existing_ids.add(orphan_id)

    # Move every non-conflicting history key from a mutable URL to the
    # immutable upstream UUID. Conflicting same-day legacy segments remain
    # addressable through channel_id and are included by the history service.
    if "stable_id" in upstream_columns and channel_columns and "channel_identity" in channel_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_daily_usages AS history "
                "SET channel_identity = (SELECT stable_id FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id) "
                "WHERE EXISTS (SELECT 1 FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id "
                "AND stable_id IS NOT NULL) "
                "AND NOT EXISTS (SELECT 1 FROM upstream_channel_daily_usages AS current "
                "WHERE current.id <> history.id "
                "AND current.channel_identity = (SELECT stable_id FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id) "
                "AND current.usage_date = history.usage_date)"
            )
        )
    if "stable_id" in upstream_columns and account_columns and "channel_identity" in account_columns:
        await conn.execute(
            text(
                "UPDATE upstream_account_daily_usages AS history "
                "SET channel_identity = (SELECT stable_id FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id) "
                "WHERE EXISTS (SELECT 1 FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id "
                "AND stable_id IS NOT NULL)"
            )
        )
    if "stable_id" in upstream_columns and total_columns and "channel_identity" in total_columns:
        await conn.execute(
            text(
                "UPDATE upstream_channel_usage_totals AS history "
                "SET channel_identity = (SELECT stable_id FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id) "
                "WHERE EXISTS (SELECT 1 FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id "
                "AND stable_id IS NOT NULL) "
                "AND NOT EXISTS (SELECT 1 FROM upstream_channel_usage_totals AS current "
                "WHERE current.channel_identity = (SELECT stable_id FROM upstream_channels "
                "WHERE upstream_channels.id = history.channel_id) "
                "AND current.channel_identity <> history.channel_identity)"
            )
        )


async def _migrate_upstream_priority_intervals(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS upstream_priority_intervals ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "name VARCHAR(100) NOT NULL UNIQUE, "
            "start_priority INTEGER NOT NULL, "
            "end_priority INTEGER NOT NULL, "
            "step INTEGER NOT NULL DEFAULT 1, "
            "created_at DATETIME, "
            "updated_at DATETIME, "
            "CONSTRAINT ck_upstream_priority_interval_start CHECK (start_priority >= 0), "
            "CONSTRAINT ck_upstream_priority_interval_end CHECK (end_priority > start_priority), "
            "CONSTRAINT ck_upstream_priority_interval_step CHECK (step >= 1)"
            ")"
        )
    )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_upstream_priority_intervals_name "
            "ON upstream_priority_intervals (name)"
        )
    )
    # Older databases created triggers that globally rejected overlapping
    # ranges. Ranges are intentionally allowed to overlap because different
    # account types can use the same priority band independently.
    await conn.execute(
        text("DROP TRIGGER IF EXISTS trg_upstream_priority_interval_no_overlap_insert")
    )
    await conn.execute(
        text("DROP TRIGGER IF EXISTS trg_upstream_priority_interval_no_overlap_update")
    )

    tables = await _sqlite_table_names(conn)
    account_table = (
        "api_accounts" if "api_accounts" in tables else "upstream_account_configs"
    )
    result = await conn.execute(text(f'PRAGMA table_info("{account_table}")'))
    columns = {str(row[1]) for row in result.fetchall()}
    if not columns:
        return
    priority_columns = {
        "priority_interval_id": "INTEGER",
        "desired_priority": "INTEGER",
        "priority_sync_status": "VARCHAR(32) NOT NULL DEFAULT 'unassigned'",
        "priority_sync_error": "TEXT",
        "last_priority_applied_at": "DATETIME",
        "priority_tiebreak_order": "INTEGER",
        "priority_tiebreak_multiplier": "FLOAT",
    }
    for column, column_type in priority_columns.items():
        if column not in columns:
            await conn.execute(
                text(f'ALTER TABLE "{account_table}" ADD COLUMN {column} {column_type}')
            )
            columns.add(column)
    await conn.execute(
        text(
            f'UPDATE "{account_table}" SET priority_sync_status = \'unassigned\' '
            "WHERE priority_sync_status IS NULL OR TRIM(priority_sync_status) = ''"
        )
    )
    await conn.execute(
        text(
            f'CREATE INDEX IF NOT EXISTS ix_{account_table}_priority_interval_id '
            f'ON "{account_table}" (priority_interval_id)'
        )
    )

    result = await conn.execute(text(f'PRAGMA foreign_key_list("{account_table}")'))
    has_interval_foreign_key = any(
        str(row[2]) == "upstream_priority_intervals"
        and str(row[3]) == "priority_interval_id"
        for row in result.fetchall()
    )
    # A real ON DELETE SET NULL foreign key only clears the reference. Run the
    # complete state cleanup before either the FK or the legacy AFTER trigger.
    await conn.execute(
        text("DROP TRIGGER IF EXISTS trg_upstream_priority_interval_cleanup_delete")
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_upstream_priority_interval_cleanup_delete "
            "BEFORE DELETE ON upstream_priority_intervals "
            f'BEGIN UPDATE "{account_table}" SET priority_interval_id = NULL, '
            "desired_priority = NULL, priority_tiebreak_order = NULL, "
            "priority_tiebreak_multiplier = NULL, priority_sync_status = 'unassigned', "
            "priority_sync_error = NULL WHERE priority_interval_id = OLD.id; END"
        )
    )
    if has_interval_foreign_key:
        return
    await conn.execute(
        text(
            f'UPDATE "{account_table}" SET priority_interval_id = NULL, '
            "desired_priority = NULL, priority_tiebreak_order = NULL, "
            "priority_tiebreak_multiplier = NULL, priority_sync_status = 'unassigned', "
            "priority_sync_error = NULL WHERE priority_interval_id IS NOT NULL "
            "AND priority_interval_id NOT IN (SELECT id FROM upstream_priority_intervals)"
        )
    )
    await conn.execute(text("DROP TRIGGER IF EXISTS trg_upstream_priority_interval_delete"))
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_upstream_account_priority_interval_insert "
            f'BEFORE INSERT ON "{account_table}" '
            "WHEN NEW.priority_interval_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM upstream_priority_intervals WHERE id = NEW.priority_interval_id) "
            "BEGIN SELECT RAISE(ABORT, 'invalid upstream priority interval reference'); END"
        )
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_upstream_account_priority_interval_update "
            f'BEFORE UPDATE OF priority_interval_id ON "{account_table}" '
            "WHEN NEW.priority_interval_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM upstream_priority_intervals WHERE id = NEW.priority_interval_id) "
            "BEGIN SELECT RAISE(ABORT, 'invalid upstream priority interval reference'); END"
        )
    )
    await conn.execute(
        text(
            "CREATE TRIGGER IF NOT EXISTS trg_upstream_priority_interval_delete "
            "AFTER DELETE ON upstream_priority_intervals "
            f'BEGIN UPDATE "{account_table}" SET priority_interval_id = NULL, '
            "desired_priority = NULL, priority_tiebreak_order = NULL, "
            "priority_tiebreak_multiplier = NULL, priority_sync_status = 'unassigned', "
            "priority_sync_error = NULL WHERE priority_interval_id = OLD.id; END"
        )
    )


def _redact_migration_secrets(value: object, secrets: set[str], *, limit: int = 200) -> str | None:
    if value is None:
        return None
    redacted = str(value)
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[redacted]")
    return redacted[:limit]


def _migration_secret_variants(*values: object) -> set[str]:
    secrets: set[str] = set()
    for value in values:
        if not value:
            continue
        secret = str(value).strip()
        if not secret:
            continue
        secrets.add(secret)
        if secret.lower().startswith("bearer ") and secret[7:].strip():
            secrets.add(secret[7:].strip())
    return secrets


async def _scrub_upstream_plaintext_secret_copies(conn: AsyncConnection) -> None:
    rows = (
        await conn.execute(
            text(
                "SELECT a.sub2api_account_id, a.remote_name, a.encrypted_api_key, "
                "a.encrypted_access_token AS account_access_token, "
                "c.encrypted_access_token AS channel_access_token, "
                "c.encrypted_refresh_token AS channel_refresh_token "
                "FROM upstream_account_configs a "
                "LEFT JOIN upstream_channels c ON c.id = a.channel_id"
            )
        )
    ).mappings().all()
    secrets_by_account: dict[int, set[str]] = {}
    for row in rows:
        secrets = _migration_secret_variants(
            *(
                plaintext
                for field in (
                    "encrypted_api_key",
                    "account_access_token",
                    "channel_access_token",
                    "channel_refresh_token",
                )
                if (plaintext := decrypt_text(row[field]))
            )
        )
        if not secrets:
            continue
        account_id = int(row["sub2api_account_id"])
        secrets_by_account[account_id] = secrets
        remote_name = _redact_migration_secrets(row["remote_name"], secrets)
        if remote_name != row["remote_name"]:
            await conn.execute(
                text(
                    "UPDATE upstream_account_configs SET remote_name = :remote_name "
                    "WHERE sub2api_account_id = :account_id"
                ),
                {
                    "remote_name": remote_name,
                    "account_id": account_id,
                },
            )
    if not secrets_by_account:
        return
    logs = (
        await conn.execute(
            text(
                "SELECT id, sub2api_account_id, account_name "
                "FROM upstream_rate_change_logs"
            )
        )
    ).mappings().all()
    for log in logs:
        secrets = secrets_by_account.get(int(log["sub2api_account_id"]))
        if not secrets:
            continue
        account_name = _redact_migration_secrets(log["account_name"], secrets)
        if account_name != log["account_name"]:
            await conn.execute(
                text(
                    "UPDATE upstream_rate_change_logs SET account_name = :account_name "
                    "WHERE id = :log_id"
                ),
                {"account_name": account_name, "log_id": log["id"]},
            )


async def _migrate_legacy_rate_logs_to_change_events(conn: AsyncConnection) -> None:
    """Expose pre-ledger rate history without turning it into new unread activity."""
    result = await conn.execute(text("PRAGMA table_info(upstream_channel_change_events)"))
    columns = {str(row[1]) for row in result.fetchall()}
    if "legacy_imported" not in columns:
        await conn.execute(
            text(
                "ALTER TABLE upstream_channel_change_events "
                "ADD COLUMN legacy_imported BOOLEAN NOT NULL DEFAULT 0"
            )
        )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_upstream_channel_change_events_legacy_imported "
            "ON upstream_channel_change_events (legacy_imported)"
        )
    )

    migration_key = "migration.legacy_rate_logs_to_change_events.v1"
    completed = await conn.execute(
        text("SELECT value FROM app_settings WHERE key = :key"),
        {"key": migration_key},
    )
    if completed.scalar_one_or_none() is not None:
        return

    cutoff_result = await conn.execute(
        text(
            "SELECT MIN(created_at) FROM upstream_channel_change_events "
            "WHERE legacy_imported = 0"
        )
    )
    cutoff = cutoff_result.scalar_one_or_none()
    statement = (
        "SELECT * FROM upstream_rate_change_logs "
        + ("WHERE created_at < :cutoff " if cutoff is not None else "")
        + "ORDER BY created_at, id"
    )
    legacy_logs = (
        await conn.execute(text(statement), {"cutoff": cutoff} if cutoff is not None else {})
    ).mappings().all()

    def changed(old_value: object, new_value: object) -> bool:
        return old_value is not None and new_value is not None and old_value != new_value

    for row in legacy_logs:
        base_details = {
            "account_id": row["sub2api_account_id"],
            "account_name": row["account_name"],
            "legacy_rate_log_id": row["id"],
            "reason": row["reason"],
            "status": row["status"],
        }
        events: list[dict[str, object | None]] = []
        if changed(row["old_current_rate"], row["new_current_rate"]):
            events.append(
                {
                    "event_type": "account_rate_changed",
                    "old_value": row["old_current_rate"],
                    "new_value": row["new_current_rate"],
                    "details": base_details,
                }
            )
        if changed(row["old_group_multiplier"], row["new_group_multiplier"]):
            events.append(
                {
                    "event_type": "group_multiplier_changed",
                    "old_value": row["old_group_multiplier"],
                    "new_value": row["new_group_multiplier"],
                    "details": base_details,
                }
            )
        if changed(
            row["old_upstream_recharge_multiplier"],
            row["new_upstream_recharge_multiplier"],
        ):
            events.append(
                {
                    "event_type": "channel_multiplier_changed",
                    "old_value": row["old_upstream_recharge_multiplier"],
                    "new_value": row["new_upstream_recharge_multiplier"],
                    "details": base_details,
                }
            )
        if changed(row["old_group_name"], row["new_group_name"]):
            events.append(
                {
                    "event_type": "group_name_changed",
                    "details": {
                        **base_details,
                        "old_name": row["old_group_name"],
                        "new_name": row["new_group_name"],
                    },
                }
            )
        for event_type, old_field, new_field in (
            (
                "upstream_key_status_changed",
                "old_upstream_key_status",
                "new_upstream_key_status",
            ),
            (
                "upstream_group_status_changed",
                "old_upstream_group_status",
                "new_upstream_group_status",
            ),
        ):
            if changed(row[old_field], row[new_field]):
                events.append(
                    {
                        "event_type": event_type,
                        "old_status": row[old_field],
                        "new_status": row[new_field],
                        "details": base_details,
                    }
                )

        for event_values in events:
            await conn.execute(
                text(
                    "INSERT INTO upstream_channel_change_events ("
                    "channel_id, channel_name, event_type, group_id, group_name, "
                    "old_value, new_value, old_status, new_status, details, "
                    "legacy_imported, created_at"
                    ") VALUES ("
                    ":channel_id, :channel_name, :event_type, :group_id, :group_name, "
                    ":old_value, :new_value, :old_status, :new_status, :details, 1, :created_at"
                    ")"
                ),
                {
                    "channel_id": row["channel_id"],
                    "channel_name": row["channel_name"],
                    "group_id": row["group_id"],
                    "group_name": row["group_name"],
                    "old_value": event_values.get("old_value"),
                    "new_value": event_values.get("new_value"),
                    "old_status": event_values.get("old_status"),
                    "new_status": event_values.get("new_status"),
                    "details": json.dumps(event_values["details"], ensure_ascii=True),
                    "created_at": row["created_at"],
                    "event_type": event_values["event_type"],
                },
            )

    await conn.execute(
        text(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (:key, :value, CURRENT_TIMESTAMP)"
        ),
        {"key": migration_key, "value": "1"},
    )


async def _migrate_recharge_multiplier_change_baselines(conn: AsyncConnection) -> None:
    """Keep failed probes from turning a retained multiplier into a fake change."""
    columns = {
        str(row[1])
        for row in (
            await conn.execute(text("PRAGMA table_info(upstream_channels)"))
        ).fetchall()
    }
    if "last_known_recharge_multiplier" not in columns:
        return

    # Some pre-baseline rows were already in a failed state when this upgrade
    # first ran. Recover their last known multiplier from the legacy ledger.
    await conn.execute(
        text(
            "UPDATE upstream_channels SET last_known_recharge_multiplier = ("
            "SELECT legacy.upstream_recharge_multiplier "
            "FROM upstream_rate_change_logs AS legacy "
            "WHERE legacy.channel_id = upstream_channels.id "
            "AND legacy.upstream_recharge_multiplier IS NOT NULL "
            "ORDER BY legacy.created_at DESC, legacy.id DESC LIMIT 1"
            ") WHERE last_known_recharge_multiplier IS NULL"
        )
    )

    migration_key = "migration.remove_recharge_baseline_events.v1"
    completed = await conn.execute(
        text("SELECT value FROM app_settings WHERE key = :key"),
        {"key": migration_key},
    )
    if completed.scalar_one_or_none() is not None:
        return

    # A first observed multiplier establishes a baseline; it is not a change.
    # Remove the entries created while failed discovery cleared that baseline.
    await conn.execute(
        text(
            "DELETE FROM upstream_channel_change_events "
            "WHERE event_type = 'channel_multiplier_changed' "
            "AND old_value IS NULL AND new_value IS NOT NULL "
            "AND COALESCE(legacy_imported, 0) = 0"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (:key, :value, CURRENT_TIMESTAMP)"
        ),
        {"key": migration_key, "value": "1"},
    )


async def _migrate_duplicate_group_deletion_events(conn: AsyncConnection) -> None:
    """Remove the account-health copy of a canonical group deletion event."""
    migration_key = "migration.remove_duplicate_group_deletions.v1"
    completed = await conn.execute(
        text("SELECT value FROM app_settings WHERE key = :key"),
        {"key": migration_key},
    )
    if completed.scalar_one_or_none() is not None:
        return

    await conn.execute(
        text(
            "DELETE FROM upstream_channel_change_events AS status_event "
            "WHERE status_event.event_type = 'upstream_group_status_changed' "
            "AND LOWER(COALESCE(status_event.new_status, '')) IN "
            "('deleted', 'removed', 'absent') "
            "AND EXISTS ("
            "SELECT 1 FROM upstream_channel_change_events AS removed_event "
            "WHERE removed_event.event_type = 'group_removed' "
            "AND removed_event.channel_id IS status_event.channel_id "
            "AND removed_event.created_at = status_event.created_at "
            "AND COALESCE(removed_event.legacy_imported, 0) = "
            "COALESCE(status_event.legacy_imported, 0) "
            "AND ("
            "(COALESCE(status_event.group_id, '') <> '' "
            "AND LOWER(removed_event.group_id) = LOWER(status_event.group_id)) "
            "OR (COALESCE(status_event.group_id, '') = '' "
            "AND COALESCE(status_event.group_name, '') <> '' "
            "AND LOWER(removed_event.group_name) = LOWER(status_event.group_name))"
            ")"
            ")"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO app_settings (key, value, updated_at) "
            "VALUES (:key, :value, CURRENT_TIMESTAMP)"
        ),
        {"key": migration_key, "value": "1"},
    )


_LEGACY_UPSTREAM_COLLISIONS = {
    "upstream_groups": "legacy_upstream_groups_v1",
    "upstream_api_keys": "legacy_upstream_api_keys_v1",
    "upstream_rate_change_logs": "legacy_upstream_rate_change_logs_v1",
    "account_scheduling_change_logs": "legacy_account_scheduling_change_logs_v1",
}


async def _sqlite_table_names(conn: AsyncConnection) -> set[str]:
    rows = await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
    return {str(row[0]) for row in rows.fetchall()}


async def _prepare_upstream_domain_v2(conn: AsyncConnection) -> bool:
    """Move colliding v1 tables aside before SQLAlchemy creates the v2 schema."""

    tables = await _sqlite_table_names(conn)
    legacy_present = "upstream_channels" in tables
    if not legacy_present:
        return False
    for source, archived in _LEGACY_UPSTREAM_COLLISIONS.items():
        if source in tables and archived not in tables:
            indexes = await conn.execute(text(f'PRAGMA index_list("{source}")'))
            for index in indexes.fetchall():
                index_name = str(index[1])
                index_origin = str(index[3]) if len(index) > 3 else ""
                if index_origin == "c" and not index_name.startswith("sqlite_autoindex_"):
                    escaped_name = index_name.replace('"', '""')
                    await conn.execute(text(f'DROP INDEX "{escaped_name}"'))
            await conn.execute(text(f'ALTER TABLE "{source}" RENAME TO "{archived}"'))
    return True


async def _rename_upstream_domain_v2_columns(conn: AsyncConnection) -> None:
    """Finish v2 naming for databases created during the domain upgrade."""

    tables = await _sqlite_table_names(conn)
    mappings = {
        "upstreams": {
            "canonical_base_url": "api_endpoint_url",
            "archived_canonical_base_url": "archived_api_endpoint_url",
            "management_base_url": "management_url",
            "upstream_type": "platform_type",
            "resolved_upstream_type": "resolved_platform_type",
            "manual_recharge_multiplier": "upstream_recharge_multiplier_override",
            "discovered_recharge_multiplier": "discovered_upstream_recharge_multiplier",
        },
        "api_accounts": {
            "upstream_api_key_record_id": "remote_upstream_api_key_id",
            "base_url": "api_endpoint_url",
            "upstream_type": "platform_type",
            "resolved_upstream_type": "resolved_platform_type",
            "manual_group_multiplier": "upstream_group_multiplier_override",
            "manual_recharge_multiplier": "upstream_recharge_multiplier_override",
            "discovered_group_multiplier": "discovered_upstream_group_multiplier",
            "discovered_recharge_multiplier": "discovered_upstream_recharge_multiplier",
            "today_management_stats_status": "today_management_site_stats_status",
            "today_management_stats_checked_at": "today_management_site_stats_checked_at",
        },
        "api_account_daily_usages": {
            "upstream_api_key_record_id": "remote_upstream_api_key_id",
        },
    }
    for table_name, column_mapping in mappings.items():
        if table_name not in tables:
            continue
        columns = await _table_columns(conn, table_name)
        for old_name, new_name in column_mapping.items():
            if old_name not in columns or new_name in columns:
                continue
            await conn.execute(
                text(
                    f'ALTER TABLE "{table_name}" RENAME COLUMN '
                    f'"{old_name}" TO "{new_name}"'
                )
            )
            columns.remove(old_name)
            columns.add(new_name)

    if "upstreams" in tables:
        await conn.execute(text("DROP INDEX IF EXISTS uq_upstreams_active_canonical_base_url"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_upstreams_active_api_endpoint_url "
                "ON upstreams (api_endpoint_url) WHERE deleted_at IS NULL"
            )
        )
    if "api_accounts" in tables:
        await conn.execute(text("DROP INDEX IF EXISTS uq_api_accounts_upstream_record_id"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_accounts_upstream_remote_key_id "
                "ON api_accounts (upstream_id, remote_upstream_api_key_id)"
            )
        )
    if "upstream_change_events" in tables:
        await conn.execute(
            text(
                "UPDATE upstream_change_events "
                "SET event_type = 'upstream_recharge_multiplier_changed' "
                "WHERE event_type IN ('channel_multiplier_changed', "
                "'upstream_multiplier_changed')"
            )
            )


async def _rename_management_account_columns(conn: AsyncConnection) -> None:
    """Rename management-site account identifiers outside the upstream domain."""

    tables = await _sqlite_table_names(conn)
    table_names = (
        "account_snapshots",
        "refresh_jobs",
        "account_exception_records",
        "usage_window_states",
        "usage_limit_samples",
        "usage_token_windows",
    )
    for table_name in table_names:
        if table_name not in tables:
            continue
        columns = await _table_columns(conn, table_name)
        if "sub2api_account_id" in columns and "management_account_id" not in columns:
            await conn.execute(
                text(
                    f'ALTER TABLE "{table_name}" RENAME COLUMN '
                    '"sub2api_account_id" TO "management_account_id"'
                )
            )

    for table_name in table_names:
        await conn.execute(
            text(f"DROP INDEX IF EXISTS ix_{table_name}_sub2api_account_id")
        )
        if table_name in tables:
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table_name}_management_account_id "
                    f'ON "{table_name}" (management_account_id)'
                )
            )


async def _migrate_persisted_domain_values(conn: AsyncConnection) -> None:
    """Normalize renamed enum values and invalidate stale JSON contracts."""

    tables = await _sqlite_table_names(conn)
    if "api_account_pause_holds" in tables:
        await conn.execute(
            text(
                "UPDATE api_account_pause_holds AS current SET "
                "active = MAX(current.active, COALESCE((SELECT legacy.active "
                "FROM api_account_pause_holds AS legacy "
                "WHERE legacy.api_account_id = current.api_account_id "
                "AND legacy.reason = 'channel_monitor_unavailable' LIMIT 1), 0)), "
                "resolved_at = CASE WHEN current.active = 1 OR EXISTS ("
                "SELECT 1 FROM api_account_pause_holds AS legacy "
                "WHERE legacy.api_account_id = current.api_account_id "
                "AND legacy.reason = 'channel_monitor_unavailable' "
                "AND legacy.active = 1) THEN NULL ELSE current.resolved_at END "
                "WHERE current.reason = 'upstream_monitor_unavailable'"
            )
        )
        await conn.execute(
            text(
                "DELETE FROM api_account_pause_holds AS legacy "
                "WHERE legacy.reason = 'channel_monitor_unavailable' AND EXISTS ("
                "SELECT 1 FROM api_account_pause_holds AS current "
                "WHERE current.api_account_id = legacy.api_account_id "
                "AND current.reason = 'upstream_monitor_unavailable')"
            )
        )
        await conn.execute(
            text(
                "UPDATE api_account_pause_holds "
                "SET reason = 'upstream_monitor_unavailable' "
                "WHERE reason = 'channel_monitor_unavailable'"
            )
        )
    if "usage_estimate_cache" in tables:
        await conn.execute(
            text(
                "DELETE FROM usage_estimate_cache "
                "WHERE CAST(payload AS TEXT) LIKE '%\"sub2api_account_id\"%'"
            )
        )


async def _table_columns(conn: AsyncConnection, table_name: str) -> set[str]:
    rows = await conn.execute(text(f'PRAGMA table_info("{table_name}")'))
    return {str(row[1]) for row in rows.fetchall()}


async def _insert_dict(conn: AsyncConnection, table_name: str, values: dict) -> None:
    columns = await _table_columns(conn, table_name)
    payload = {key: value for key, value in values.items() if key in columns}
    names = list(payload)
    if not names:
        return
    quoted = ", ".join(f'"{name}"' for name in names)
    parameters = ", ".join(f":{name}" for name in names)
    await conn.execute(
        text(f'INSERT INTO "{table_name}" ({quoted}) VALUES ({parameters})'),
        payload,
    )


def _valid_uuid(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _renamed_row(row: dict, mapping: dict[str, str]) -> dict:
    values = dict(row)
    for old_name, new_name in mapping.items():
        if old_name in row:
            values[new_name] = row[old_name]
        values.pop(old_name, None)
    return values


async def _migrate_upstream_domain_v2(conn: AsyncConnection) -> None:
    """Replace URL/int-based upstream storage with the final UUID domain schema."""

    tables = await _sqlite_table_names(conn)
    if "upstream_channels" not in tables:
        return

    now = datetime.now(timezone.utc).isoformat()
    upstream_rows = (
        await conn.execute(text("SELECT * FROM upstream_channels ORDER BY id"))
    ).mappings().all()
    upstream_ids: dict[int, str] = {}
    identity_ids: dict[str, str] = {}
    upstream_mapping = {
        "canonical_base_url": "api_endpoint_url",
        "archived_canonical_base_url": "archived_api_endpoint_url",
        "management_base_url": "management_url",
        "upstream_type": "platform_type",
        "resolved_upstream_type": "resolved_platform_type",
        "manual_recharge_multiplier": "upstream_recharge_multiplier_override",
        "discovered_recharge_multiplier": "discovered_upstream_recharge_multiplier",
        "effective_recharge_multiplier": "upstream_recharge_multiplier",
        "balance_remaining": "wallet_balance_usd",
        "balance_total": "wallet_total_usd",
        "balance_used": "wallet_used_usd",
        "today_balance_used": "today_upstream_wallet_cost_usd",
        "yesterday_balance_used": "yesterday_upstream_wallet_cost_usd",
        "channel_monitors": "upstream_monitors",
        "channel_monitor_test_models": "upstream_monitor_test_models",
        "channel_monitor_count": "upstream_monitor_count",
        "channel_monitor_status": "upstream_monitor_status",
        "channel_monitor_message": "upstream_monitor_message",
        "channel_monitor_checked_at": "upstream_monitor_checked_at",
        "channel_monitor_guard_state": "upstream_monitor_guard_state",
        "channel_monitor_unavailable_count": "upstream_monitor_unavailable_count",
        "channel_monitor_recovery_count": "upstream_monitor_recovery_count",
        "channel_monitor_guard_checked_at": "upstream_monitor_guard_checked_at",
    }
    for row_view in upstream_rows:
        row = dict(row_view)
        upstream_id = _valid_uuid(row.get("stable_id")) or str(uuid4())
        old_id = int(row["id"])
        upstream_ids[old_id] = upstream_id
        for identity in (row.get("stable_id"), row.get("canonical_base_url")):
            normalized = str(identity or "").strip()
            if normalized:
                identity_ids[normalized] = upstream_id
        values = _renamed_row(row, upstream_mapping)
        values["id"] = upstream_id
        values.pop("stable_id", None)
        await _insert_dict(conn, "upstreams", values)

    async def archived_upstream(
        old_id: object,
        identity: object,
        display_name: object = None,
    ) -> str:
        try:
            numeric_id = int(old_id) if old_id is not None else None
        except (TypeError, ValueError):
            numeric_id = None
        if numeric_id is not None and numeric_id in upstream_ids:
            return upstream_ids[numeric_id]
        normalized_identity = str(identity or "").strip()
        if normalized_identity and normalized_identity in identity_ids:
            return identity_ids[normalized_identity]
        archive_key = normalized_identity or f"legacy-channel:{numeric_id or 'unknown'}"
        archive_id = str(uuid5(UUID("87bc812a-2a1e-4b0d-aa80-791c8b2ac88e"), archive_key))
        identity_ids[archive_key] = archive_id
        if numeric_id is not None:
            upstream_ids[numeric_id] = archive_id
        existing = await conn.scalar(
            text("SELECT id FROM upstreams WHERE id = :id"), {"id": archive_id}
        )
        if existing is None:
            await _insert_dict(
                conn,
                "upstreams",
                {
                    "id": archive_id,
                    "display_name": str(display_name or "归档上游")[:200],
                    "api_endpoint_url": f"archived://{archive_id}",
                    "platform_type": "auto",
                    "probe_enabled": 0,
                    "deleted_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return archive_id

    for source, target, id_column in (
        ("legacy_upstream_groups_v1", "upstream_groups", "channel_id"),
        ("legacy_upstream_api_keys_v1", "upstream_api_keys", "channel_id"),
    ):
        if source not in tables:
            continue
        rows = (await conn.execute(text(f'SELECT * FROM "{source}" ORDER BY id'))).mappings()
        for row_view in rows:
            row = dict(row_view)
            values = dict(row)
            values["upstream_id"] = await archived_upstream(
                row.get(id_column), None, row.get("name") or row.get("remote_name")
            )
            values.pop(id_column, None)
            await _insert_dict(conn, target, values)

    account_rows = (
        await conn.execute(text("SELECT * FROM upstream_account_configs ORDER BY id"))
    ).mappings().all()
    account_mapping = {
        "sub2api_account_id": "management_account_id",
        "channel_auto_assign_disabled": "upstream_auto_assign_disabled",
        "upstream_api_key_record_id": "remote_upstream_api_key_id",
        "base_url": "api_endpoint_url",
        "upstream_type": "platform_type",
        "resolved_upstream_type": "resolved_platform_type",
        "manual_group_multiplier": "upstream_group_multiplier_override",
        "manual_recharge_multiplier": "upstream_recharge_multiplier_override",
        "discovered_group_multiplier": "discovered_upstream_group_multiplier",
        "discovered_recharge_multiplier": "discovered_upstream_recharge_multiplier",
        "effective_group_multiplier": "upstream_group_multiplier",
        "effective_recharge_multiplier": "upstream_recharge_multiplier",
        "local_recharge_multiplier": "management_recharge_multiplier",
        "local_recharge_source": "management_recharge_source",
        "local_recharge_status": "management_recharge_status",
        "target_rate": "expected_management_billing_multiplier",
        "current_rate": "management_billing_multiplier",
        "balance_remaining": "wallet_balance_usd",
        "balance_total": "wallet_total_usd",
        "balance_used": "wallet_used_usd",
        "upstream_usage_amount": "upstream_wallet_cost_usd",
        "today_upstream_usage_amount": "today_upstream_wallet_cost_usd",
        "today_sub2api_cost_amount": "today_management_account_cost_usd",
        "today_sub2api_user_cost_amount": "today_management_user_charge_usd",
        "today_sub2api_stats_status": "today_management_site_stats_status",
        "today_sub2api_stats_checked_at": "today_management_site_stats_checked_at",
        "balance_guard_channel_id": "balance_guard_upstream_id",
        "auto_pause_channel_id": "auto_pause_upstream_id",
    }
    for row_view in account_rows:
        row = dict(row_view)
        values = _renamed_row(row, account_mapping)
        values["upstream_id"] = (
            await archived_upstream(row.get("channel_id"), None, row.get("remote_name"))
            if row.get("channel_id") is not None
            else None
        )
        if values.get("balance_guard_upstream_id") is not None:
            values["balance_guard_upstream_id"] = await archived_upstream(
                row.get("balance_guard_channel_id"), None
            )
        if values.get("auto_pause_upstream_id") is not None:
            values["auto_pause_upstream_id"] = await archived_upstream(
                row.get("auto_pause_channel_id"), None
            )
        if values.get("availability_check_mode") == "channel_monitor":
            values["availability_check_mode"] = "upstream_monitor"
        values.pop("channel_id", None)
        await _insert_dict(conn, "api_accounts", values)

    api_account_ids = {int(row["id"]) for row in account_rows}
    upstream_api_key_ids = {
        int(row[0])
        for row in (
            await conn.execute(text("SELECT id FROM upstream_api_keys"))
        ).fetchall()
    }

    if "upstream_account_pause_holds" in tables:
        rows = (
            await conn.execute(text("SELECT * FROM upstream_account_pause_holds ORDER BY id"))
        ).mappings()
        for row_view in rows:
            row = dict(row_view)
            values = _renamed_row(
                row,
                {
                    "account_config_id": "api_account_id",
                    "scope_channel_id": "scope_upstream_id",
                },
            )
            if row.get("scope_channel_id") is not None:
                values["scope_upstream_id"] = await archived_upstream(
                    row.get("scope_channel_id"), None
                )
            await _insert_dict(conn, "api_account_pause_holds", values)

    daily_segments: dict[tuple[str, object], int] = {}
    channel_history_count = 0
    if "upstream_channel_daily_usages" in tables:
        rows = (
            await conn.execute(text("SELECT * FROM upstream_channel_daily_usages ORDER BY id"))
        ).mappings()
        for row_view in rows:
            row = dict(row_view)
            upstream_id = await archived_upstream(
                row.get("channel_id"), row.get("channel_identity"), row.get("channel_name")
            )
            key = (upstream_id, row.get("usage_date"))
            segment = daily_segments.get(key, 0)
            daily_segments[key] = segment + 1
            values = _renamed_row(
                row,
                {
                    "channel_name": "upstream_name",
                    "balance_used": "upstream_wallet_cost_usd",
                    "balance_used_adjusted": "upstream_actual_cost_cny",
                    "recharge_multiplier": "upstream_recharge_multiplier",
                    "sub2api_cost": "management_account_cost_usd",
                    "sub2api_cost_cny": "management_account_cost_cny",
                    "sub2api_user_cost": "management_user_charge_usd",
                    "income_recharge_multiplier": "management_recharge_multiplier",
                    "income": "actual_income_cny",
                },
            )
            values["upstream_id"] = upstream_id
            values["source_segment"] = segment
            values["management_user_charge_usd"] = values.get(
                "management_user_charge_usd", row.get("sub2api_actual_cost")
            )
            values.pop("channel_id", None)
            values.pop("channel_identity", None)
            values.pop("sub2api_actual_cost", None)
            await _insert_dict(conn, "upstream_daily_usages", values)
            channel_history_count += 1

    account_segments: dict[tuple[int, str, object], int] = {}
    account_history_count = 0
    if "upstream_account_daily_usages" in tables:
        rows = (
            await conn.execute(text("SELECT * FROM upstream_account_daily_usages ORDER BY id"))
        ).mappings()
        for row_view in rows:
            row = dict(row_view)
            upstream_id = await archived_upstream(
                row.get("channel_id"), row.get("channel_identity"), row.get("account_name")
            )
            management_id = int(row["sub2api_account_id"])
            key = (management_id, upstream_id, row.get("usage_date"))
            segment = account_segments.get(key, 0)
            account_segments[key] = segment + 1
            upstream_multiplier = row.get("upstream_recharge_multiplier")
            management_multiplier = row.get("local_recharge_multiplier")
            if management_multiplier is None:
                management_multiplier = row.get("management_recharge_multiplier")
            upstream_cost = row.get("upstream_usage")
            management_cost = row.get("sub2api_cost")
            user_charge = row.get("sub2api_user_cost")
            values = _renamed_row(
                row,
                {
                    "sub2api_account_id": "management_account_id",
                    "upstream_api_key_record_id": "remote_upstream_api_key_id",
                    "upstream_usage": "upstream_wallet_cost_usd",
                    "sub2api_cost": "management_account_cost_usd",
                    "sub2api_user_cost": "management_user_charge_usd",
                    "local_recharge_multiplier": "management_recharge_multiplier",
                    "income": "actual_income_cny",
                },
            )
            values.update(
                {
                    "upstream_id": upstream_id,
                    "source_segment": segment,
                    "upstream_actual_cost_cny": (
                        float(upstream_cost) * float(upstream_multiplier)
                        if upstream_cost is not None and upstream_multiplier is not None
                        else None
                    ),
                    "management_account_cost_cny": (
                        float(management_cost) * float(management_multiplier)
                        if management_cost is not None and management_multiplier is not None
                        else None
                    ),
                    "management_user_charge_usd": user_charge
                    if user_charge is not None
                    else row.get("sub2api_actual_cost"),
                }
            )
            if values.get("api_account_id") not in api_account_ids:
                values["api_account_id"] = None
            if values.get("upstream_api_key_id") not in upstream_api_key_ids:
                values["upstream_api_key_id"] = None
            values.pop("channel_id", None)
            values.pop("channel_identity", None)
            values.pop("sub2api_actual_cost", None)
            await _insert_dict(conn, "api_account_daily_usages", values)
            account_history_count += 1

    if "upstream_channel_usage_totals" in tables:
        rows = (
            await conn.execute(text("SELECT * FROM upstream_channel_usage_totals"))
        ).mappings()
        for row_view in rows:
            row = dict(row_view)
            upstream_id = await archived_upstream(
                row.get("channel_id"), row.get("channel_identity"), row.get("channel_name")
            )
            values = _renamed_row(
                row,
                {
                    "channel_name": "upstream_name",
                    "total_balance_used": "total_upstream_wallet_cost_usd",
                    "total_balance_used_adjusted": "total_upstream_actual_cost_cny",
                    "total_sub2api_cost": "total_management_account_cost_usd",
                    "total_sub2api_cost_cny": "total_management_account_cost_cny",
                    "total_sub2api_user_cost": "total_management_user_charge_usd",
                    "total_income": "total_actual_income_cny",
                },
            )
            values["upstream_id"] = upstream_id
            values.pop("channel_id", None)
            values.pop("channel_identity", None)
            await _insert_dict(conn, "upstream_usage_totals", values)

    log_mappings = {
        "sub2api_account_id": "management_account_id",
        "channel_name": "upstream_name",
        "local_recharge_multiplier": "management_recharge_multiplier",
        "old_target_rate": "old_expected_management_billing_multiplier",
        "new_target_rate": "new_expected_management_billing_multiplier",
        "old_current_rate": "old_management_billing_multiplier",
        "new_current_rate": "new_management_billing_multiplier",
    }
    for source, target, mapping in (
        ("legacy_upstream_rate_change_logs_v1", "upstream_rate_change_logs", log_mappings),
        (
            "legacy_account_scheduling_change_logs_v1",
            "account_scheduling_change_logs",
            {"sub2api_account_id": "management_account_id", "channel_name": "upstream_name"},
        ),
        (
            "upstream_account_data_archives",
            "api_account_data_archives",
            {"sub2api_account_id": "management_account_id", "channel_name": "upstream_name"},
        ),
        (
            "upstream_channel_change_events",
            "upstream_change_events",
            {"channel_name": "upstream_name"},
        ),
    ):
        if source not in tables:
            continue
        rows = (await conn.execute(text(f'SELECT * FROM "{source}" ORDER BY id'))).mappings()
        for row_view in rows:
            row = dict(row_view)
            values = _renamed_row(row, mapping)
            values["upstream_id"] = (
                await archived_upstream(row.get("channel_id"), None, row.get("channel_name"))
                if row.get("channel_id") is not None
                else None
            )
            values.pop("channel_id", None)
            if values.get("reason") == "channel_monitor_unavailable":
                values["reason"] = "upstream_monitor_unavailable"
            if values.get("event_type") == "channel_multiplier_changed":
                values["event_type"] = "upstream_recharge_multiplier_changed"
            await _insert_dict(conn, target, values)

    expected_accounts = len(account_rows)
    actual_accounts = int(await conn.scalar(text("SELECT COUNT(*) FROM api_accounts")) or 0)
    actual_channel_history = int(
        await conn.scalar(text("SELECT COUNT(*) FROM upstream_daily_usages")) or 0
    )
    actual_account_history = int(
        await conn.scalar(text("SELECT COUNT(*) FROM api_account_daily_usages")) or 0
    )
    if (
        actual_accounts != expected_accounts
        or actual_channel_history != channel_history_count
        or actual_account_history != account_history_count
    ):
        raise RuntimeError("upstream domain v2 migration row-count validation failed")
    foreign_key_errors = (await conn.execute(text("PRAGMA foreign_key_check"))).fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"upstream domain v2 migration foreign-key validation failed: {foreign_key_errors!r}")
    old_wallet_total = await conn.scalar(
        text("SELECT COALESCE(SUM(balance_used), 0) FROM upstream_channel_daily_usages")
    )
    new_wallet_total = await conn.scalar(
        text("SELECT COALESCE(SUM(upstream_wallet_cost_usd), 0) FROM upstream_daily_usages")
    )
    if abs(float(old_wallet_total or 0) - float(new_wallet_total or 0)) > 1e-8:
        raise RuntimeError("upstream domain v2 migration amount validation failed")

    drop_order = (
        "upstream_account_pause_holds",
        "upstream_account_daily_usages",
        "upstream_channel_daily_usages",
        "upstream_channel_usage_totals",
        "upstream_account_data_archives",
        "legacy_upstream_rate_change_logs_v1",
        "legacy_account_scheduling_change_logs_v1",
        "upstream_channel_change_events",
        "upstream_account_configs",
        "legacy_upstream_api_keys_v1",
        "legacy_upstream_groups_v1",
        "upstream_channels",
    )
    for table_name in drop_order:
        if table_name in await _sqlite_table_names(conn):
            await conn.execute(text(f'DROP TABLE "{table_name}"'))


async def _migrate_management_site_setting_keys(conn: AsyncConnection) -> None:
    """Move persisted legacy setting names to the current runtime contract."""

    key_map = {
        "sub2api_base_url": "management_site_base_url",
        "sub2api_base_url_source": "management_site_base_url_source",
        "sub2api_x_api_key": "management_site_x_api_key",
        "sub2api_auto_recover_state": "management_site_auto_recover_state",
        "sub2api_last_scan_at": "management_site_last_scan_at",
        "sub2api_last_scan_status": "management_site_last_scan_status",
        "sub2api_last_scan_message": "management_site_last_scan_message",
        # The availability/monitoring domain was renamed from channel-scoped
        # to upstream/account-scoped. Preserve values written by deployments
        # before that rename instead of silently falling back to defaults.
        "api_key_auto_pause_on_channel_monitor_unavailable_enabled": (
            "api_account_auto_pause_on_upstream_monitor_unavailable_enabled"
        ),
        "channel_monitor_auto_probe_enabled": "upstream_monitor_auto_probe_enabled",
        "channel_monitor_unavailable_consecutive_threshold": (
            "upstream_monitor_unavailable_consecutive_threshold"
        ),
        "channel_monitor_recovery_consecutive_threshold": (
            "upstream_monitor_recovery_consecutive_threshold"
        ),
        "channel_monitor_fallback_without_monitor_enabled": (
            "upstream_monitor_fallback_without_monitor_enabled"
        ),
        "channel_monitor_fallback_test_models": "upstream_monitor_fallback_test_models",
        "channel_monitor_fallback_test_model": "upstream_monitor_fallback_test_model",
        "channel_monitor_fallback_test_attempts": "upstream_monitor_fallback_test_attempts",
        "channel_monitor_recovery_test_attempts": "upstream_monitor_recovery_test_attempts",
        "channel_monitor_test_attempt_interval_seconds": (
            "upstream_monitor_test_attempt_interval_seconds"
        ),
        "manual_upstream_sync_channel_monitors_enabled": "manual_upstream_monitor_sync_enabled",
        "priority_share_same_composite_multiplier": (
            "priority_share_same_upstream_actual_multiplier"
        ),
    }
    for old_key, new_key in key_map.items():
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO app_settings (key, value, updated_at) "
                "SELECT :new_key, value, updated_at FROM app_settings WHERE key = :old_key"
            ),
            {"old_key": old_key, "new_key": new_key},
        )
        await conn.execute(
            text("DELETE FROM app_settings WHERE key = :old_key"),
            {"old_key": old_key},
        )


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        legacy_upstream_domain = False
        if is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            legacy_upstream_domain = "upstream_channels" in await _sqlite_table_names(conn)
            if legacy_upstream_domain:
                await _migrate_upstream_channels(conn)
                await _migrate_upstream_usage_history(conn)
                await _migrate_upstream_rate_change_logs(conn)
                await _migrate_legacy_rate_logs_to_change_events(conn)
                await _migrate_recharge_multiplier_change_baselines(conn)
                await _migrate_duplicate_group_deletion_events(conn)
                await _migrate_upstream_priority_intervals(conn)
                await _scrub_upstream_plaintext_secret_copies(conn)
                await _prepare_upstream_domain_v2(conn)
            await _rename_upstream_domain_v2_columns(conn)
            await _rename_management_account_columns(conn)
        await conn.run_sync(Base.metadata.create_all)
        if is_sqlite:
            upstream_columns = await _table_columns(conn, "upstreams")
            for column in (
                "encrypted_login_username",
                "encrypted_login_password",
            ):
                if column not in upstream_columns:
                    await conn.execute(
                        text(f'ALTER TABLE "upstreams" ADD COLUMN "{column}" TEXT')
                    )
                    upstream_columns.add(column)
            await _migrate_persisted_domain_values(conn)
            await _migrate_management_site_setting_keys(conn)
            if legacy_upstream_domain:
                await _migrate_upstream_domain_v2(conn)
                # Domain-v2 tables are populated after the first normalization pass.
                # Normalize copied enum values once the new tables contain legacy rows.
                await _migrate_persisted_domain_values(conn)
            await _migrate_notification_outbox(conn)
            await _migrate_upstream_priority_intervals(conn)
            result = await conn.execute(text("PRAGMA table_info(mailbox_credentials)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "encrypted_access_token" not in columns:
                await conn.execute(text("ALTER TABLE mailbox_credentials ADD COLUMN encrypted_access_token TEXT"))
            if "proxy_url" not in columns:
                await conn.execute(text("ALTER TABLE mailbox_credentials ADD COLUMN proxy_url TEXT"))
            result = await conn.execute(text("PRAGMA table_info(refresh_jobs)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "memory_peak_rss_bytes" not in columns:
                await conn.execute(text("ALTER TABLE refresh_jobs ADD COLUMN memory_peak_rss_bytes INTEGER"))
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS account_exception_records ("
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "fingerprint VARCHAR(384) NOT NULL, "
                    "email VARCHAR(320), "
                    "management_account_id VARCHAR(64), "
                    "source VARCHAR(32) NOT NULL, "
                    "status VARCHAR(32) NOT NULL, "
                    "message TEXT NOT NULL, "
                    "details JSON, "
                    "created_at DATETIME, "
                    "updated_at DATETIME, "
                    "CONSTRAINT uq_account_exception_records_fingerprint UNIQUE (fingerprint)"
                    ")"
                )
            )
            result = await conn.execute(text("PRAGMA table_info(account_exception_records)"))
            account_exception_columns = {str(row[1]) for row in result.fetchall()}
            account_exception_missing_columns = {
                "fingerprint": "VARCHAR(384)",
                "email": "VARCHAR(320)",
                "management_account_id": "VARCHAR(64)",
                "source": "VARCHAR(32) NOT NULL DEFAULT 'account'",
                "status": "VARCHAR(32) NOT NULL DEFAULT 'error'",
                "message": "TEXT NOT NULL DEFAULT ''",
                "details": "JSON",
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            }
            for column, column_type in account_exception_missing_columns.items():
                if column not in account_exception_columns:
                    await conn.execute(text(f"ALTER TABLE account_exception_records ADD COLUMN {column} {column_type}"))
            await conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_fingerprint ON account_exception_records (fingerprint)")
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_email ON account_exception_records (email)"))
            await conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_management_account_id ON account_exception_records (management_account_id)")
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_source ON account_exception_records (source)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_status ON account_exception_records (status)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_created_at ON account_exception_records (created_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_updated_at ON account_exception_records (updated_at)"))
            result = await conn.execute(text("PRAGMA table_info(account_snapshots)"))
            account_snapshot_columns = {str(row[1]) for row in result.fetchall()}
            if "usage_estimate_enabled" not in account_snapshot_columns:
                await conn.execute(
                    text("ALTER TABLE account_snapshots ADD COLUMN usage_estimate_enabled BOOLEAN DEFAULT 1")
                )
            if "auto_refresh_locked" not in account_snapshot_columns:
                await conn.execute(
                    text("ALTER TABLE account_snapshots ADD COLUMN auto_refresh_locked BOOLEAN DEFAULT 0")
                )
            result = await conn.execute(text("PRAGMA table_info(phone_numbers)"))
            phone_number_columns = {str(row[1]) for row in result.fetchall()}
            if "sms_cdk" not in phone_number_columns:
                await conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN sms_cdk VARCHAR(256)"))
            if "sms_recharge_url" not in phone_number_columns:
                await conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN sms_recharge_url TEXT"))
            if "sms_status" not in phone_number_columns:
                await conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN sms_status VARCHAR(32)"))
            if "sms_error" not in phone_number_columns:
                await conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN sms_error TEXT"))
            if "sms_checked_at" not in phone_number_columns:
                await conn.execute(text("ALTER TABLE phone_numbers ADD COLUMN sms_checked_at DATETIME"))
            await conn.execute(
                text(
                    "UPDATE phone_numbers SET sms_cdk = substr(sms_url, 5), sms_url = '接码链接不存在' "
                    "WHERE COALESCE(sms_cdk, '') = '' AND lower(COALESCE(sms_url, '')) LIKE 'cdk:%'"
                )
            )
            subscription_columns = {
                "subscription_starts_at": "VARCHAR(128)",
                "subscription_expires_at": "VARCHAR(128)",
                "subscription_renews_at": "VARCHAR(128)",
                "subscription_cancels_at": "VARCHAR(128)",
                "subscription_billing_period": "VARCHAR(64)",
                "subscription_plan": "VARCHAR(128)",
                "has_active_subscription": "BOOLEAN",
                "subscription_checked_at": "DATETIME",
                "available_models": "JSON",
                "available_models_status": "VARCHAR(32) NOT NULL DEFAULT 'not_checked'",
                "available_models_checked_at": "DATETIME",
                "phone_note_sync_marker": "VARCHAR(64)",
                "encrypted_openai_refresh_token": "TEXT",
                "encrypted_openai_access_token": "TEXT",
                "encrypted_openai_id_token": "TEXT",
                "encrypted_openai_client_id": "TEXT",
                "openai_token_expires_at": "DATETIME",
            }
            for column, column_type in subscription_columns.items():
                if column not in account_snapshot_columns:
                    await conn.execute(text(f"ALTER TABLE account_snapshots ADD COLUMN {column} {column_type}"))
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS usage_window_states ("
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "account_key VARCHAR(384) NOT NULL, "
                    "email VARCHAR(320), "
                    "management_account_id VARCHAR(64), "
                     "window_key VARCHAR(32) NOT NULL, "
                     "baseline_spent FLOAT, "
                     "estimate_uses_spent_delta BOOLEAN DEFAULT 0, "
                     "last_raw_spent FLOAT, "
                     "last_used_percent FLOAT, "
                     "last_reset_at VARCHAR(128), "
                    "last_remaining_seconds INTEGER, "
                    "created_at DATETIME, "
                    "updated_at DATETIME, "
                    "CONSTRAINT uq_usage_window_state_account_window UNIQUE (account_key, window_key)"
                    ")"
                )
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_window_states_account_key ON usage_window_states (account_key)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_window_states_email ON usage_window_states (email)"))
            await conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_usage_window_states_management_account_id ON usage_window_states (management_account_id)")
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_window_states_window_key ON usage_window_states (window_key)"))
            result = await conn.execute(text("PRAGMA table_info(usage_window_states)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "estimate_uses_spent_delta" not in columns:
                await conn.execute(
                    text("ALTER TABLE usage_window_states ADD COLUMN estimate_uses_spent_delta BOOLEAN DEFAULT 0")
                )
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS usage_limit_samples ("
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "account_key VARCHAR(384) NOT NULL, "
                    "email VARCHAR(320), "
                    "management_account_id VARCHAR(64), "
                    "plan_cohort VARCHAR(32) NOT NULL DEFAULT 'unknown', "
                    "window_key VARCHAR(32) NOT NULL, "
                    "reset_key VARCHAR(256) NOT NULL, "
                    "reset_at VARCHAR(128), "
                    "observed_limit FLOAT NOT NULL, "
                    "raw_spent FLOAT NOT NULL, "
                    "used_percent FLOAT NOT NULL, "
                    "created_at DATETIME, "
                    "updated_at DATETIME, "
                    "CONSTRAINT uq_usage_limit_sample_account_window_reset UNIQUE (account_key, window_key, reset_key)"
                    ")"
                )
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_account_key ON usage_limit_samples (account_key)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_email ON usage_limit_samples (email)"))
            await conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_management_account_id ON usage_limit_samples (management_account_id)")
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_window_key ON usage_limit_samples (window_key)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_reset_key ON usage_limit_samples (reset_key)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_observed_limit ON usage_limit_samples (observed_limit)"))
            result = await conn.execute(text("PRAGMA table_info(usage_limit_samples)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "plan_cohort" not in columns:
                await conn.execute(
                    text("ALTER TABLE usage_limit_samples ADD COLUMN plan_cohort VARCHAR(32) NOT NULL DEFAULT 'unknown'")
                )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'unknown' "
                    "WHERE TRIM(COALESCE(plan_cohort, '')) = ''"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'plus' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) IN ('plus', 'chatgptplusplan')"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'team' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) IN ('team', 'chatgptteamplan')"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'pro' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) IN ('pro', 'chatgptproplan')"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'free' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) = 'free'"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'plus' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) = 'unknown' "
                    "AND LOWER(COALESCE(email, '')) IN ("
                    "SELECT LOWER(email) FROM account_snapshots "
                    "WHERE LOWER(COALESCE(subscription_plan, '')) IN ('plus', 'chatgptplusplan')"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'team' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) = 'unknown' "
                    "AND LOWER(COALESCE(email, '')) IN ("
                    "SELECT LOWER(email) FROM account_snapshots "
                    "WHERE LOWER(COALESCE(subscription_plan, '')) IN ('team', 'chatgptteamplan')"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'pro' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) = 'unknown' "
                    "AND LOWER(COALESCE(email, '')) IN ("
                    "SELECT LOWER(email) FROM account_snapshots "
                    "WHERE LOWER(COALESCE(subscription_plan, '')) IN ('pro', 'chatgptproplan')"
                    ")"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_limit_samples SET plan_cohort = 'free' "
                    "WHERE LOWER(COALESCE(plan_cohort, '')) = 'unknown' "
                    "AND LOWER(COALESCE(email, '')) IN ("
                    "SELECT LOWER(email) FROM account_snapshots "
                    "WHERE LOWER(COALESCE(subscription_plan, '')) = 'free'"
                    ")"
                )
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_plan_cohort ON usage_limit_samples (plan_cohort)"))
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS usage_token_windows ("
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "account_key VARCHAR(384) NOT NULL, "
                    "email VARCHAR(320), "
                    "management_account_id VARCHAR(64), "
                    "window_key VARCHAR(32) NOT NULL, "
                    "window_reset_key VARCHAR(256) NOT NULL, "
                    "window_start_at VARCHAR(128), "
                    "reset_at VARCHAR(128), "
                    "spent FLOAT NOT NULL DEFAULT 0, "
                    "tokens INTEGER NOT NULL DEFAULT 0, "
                    "first_observed_at DATETIME, "
                    "last_observed_at DATETIME, "
                    "created_at DATETIME, "
                    "updated_at DATETIME, "
                    "CONSTRAINT uq_usage_token_window_account_window_reset UNIQUE (account_key, window_key, window_reset_key)"
                    ")"
                )
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_token_windows_account_key ON usage_token_windows (account_key)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_token_windows_email ON usage_token_windows (email)"))
            await conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_usage_token_windows_management_account_id ON usage_token_windows (management_account_id)")
            )
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_token_windows_window_key ON usage_token_windows (window_key)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_token_windows_window_reset_key ON usage_token_windows (window_reset_key)"))
            result = await conn.execute(text("PRAGMA table_info(usage_token_windows)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "spent" not in columns:
                await conn.execute(text("ALTER TABLE usage_token_windows ADD COLUMN spent FLOAT NOT NULL DEFAULT 0"))
            await conn.execute(
                text(
                    "UPDATE usage_window_states SET estimate_uses_spent_delta = 0 "
                    "WHERE estimate_uses_spent_delta IS NULL"
                )
            )
            await conn.execute(
                text(
                    "UPDATE usage_window_states SET estimate_uses_spent_delta = 1 "
                    "WHERE estimate_uses_spent_delta = 0 "
                    "AND COALESCE(baseline_spent, 0) > 0 "
                    "AND COALESCE(last_raw_spent, 0) >= COALESCE(baseline_spent, 0) "
                    "AND COALESCE(last_used_percent, 0) <= 1"
                )
            )
            await conn.execute(
                text("UPDATE account_snapshots SET usage_estimate_enabled = 1 WHERE usage_estimate_enabled IS NULL")
            )
            await conn.execute(
                text("UPDATE account_snapshots SET auto_refresh_locked = 0 WHERE auto_refresh_locked IS NULL")
            )
            migration_key = "migration.deactive_usage_estimate_default_false"
            result = await conn.execute(
                text("SELECT value FROM app_settings WHERE key = :key"),
                {"key": migration_key},
            )
            if result.scalar_one_or_none() is None:
                await conn.execute(
                    text("UPDATE account_snapshots SET usage_estimate_enabled = 0 WHERE deactive = 1")
                )
                await conn.execute(
                    text("INSERT INTO app_settings (key, value, updated_at) VALUES (:key, :value, CURRENT_TIMESTAMP)"),
                    {"key": migration_key, "value": "1"},
                )


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
