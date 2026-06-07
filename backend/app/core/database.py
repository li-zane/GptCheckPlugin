from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


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


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        if is_sqlite:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
        if is_sqlite:
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
