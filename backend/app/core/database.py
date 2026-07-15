from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

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


async def _migrate_upstream_channels(conn: AsyncConnection) -> None:
    result = await conn.execute(text("PRAGMA table_info(upstream_channels)"))
    channel_columns = {str(row[1]) for row in result.fetchall()}
    if channel_columns and "management_base_url" not in channel_columns:
        await conn.execute(
            text("ALTER TABLE upstream_channels ADD COLUMN management_base_url VARCHAR(500)")
        )
    if channel_columns and "encrypted_refresh_token" not in channel_columns:
        await conn.execute(
            text("ALTER TABLE upstream_channels ADD COLUMN encrypted_refresh_token TEXT")
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
        "remote_identity_fingerprint": "VARCHAR(64)",
        "api_key_origin_rebind_required": "BOOLEAN NOT NULL DEFAULT 0",
        "remote_name": "VARCHAR(200)",
        "remote_platform": "VARCHAR(64)",
        "remote_account_type": "VARCHAR(32)",
        "base_url": "VARCHAR(500)",
        "encrypted_api_key": "TEXT",
        "encrypted_access_token": "TEXT",
        "upstream_user_id": "VARCHAR(128)",
        "selected_group_id": "VARCHAR(128)",
        "selected_group_name": "VARCHAR(200)",
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
        "balance_message": "TEXT",
        "balance_checked_at": "DATETIME",
        "last_error": "TEXT",
        "last_discovered_at": "DATETIME",
        "last_applied_at": "DATETIME",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    for column, column_type in optional_account_columns.items():
        if column not in columns:
            await conn.execute(
                text(f"ALTER TABLE upstream_account_configs ADD COLUMN {column} {column_type}")
            )
            columns.add(column)
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_upstream_account_configs_channel_id "
            "ON upstream_account_configs (channel_id)"
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


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        if is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
        if is_sqlite:
            await _migrate_upstream_channels(conn)
            await _migrate_upstream_rate_change_logs(conn)
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
                    "sub2api_account_id VARCHAR(64), "
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
                "sub2api_account_id": "VARCHAR(64)",
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
                text("CREATE INDEX IF NOT EXISTS ix_account_exception_records_sub2api_account_id ON account_exception_records (sub2api_account_id)")
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
                    "sub2api_account_id VARCHAR(64), "
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
                text("CREATE INDEX IF NOT EXISTS ix_usage_window_states_sub2api_account_id ON usage_window_states (sub2api_account_id)")
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
                    "sub2api_account_id VARCHAR(64), "
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
                text("CREATE INDEX IF NOT EXISTS ix_usage_limit_samples_sub2api_account_id ON usage_limit_samples (sub2api_account_id)")
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
                    "sub2api_account_id VARCHAR(64), "
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
                text("CREATE INDEX IF NOT EXISTS ix_usage_token_windows_sub2api_account_id ON usage_token_windows (sub2api_account_id)")
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
