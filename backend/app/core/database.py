from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

database_url = make_url(settings.database_url)
if database_url.drivername.startswith("sqlite"):
    db_path = database_url.database
    if db_path and db_path != ":memory:":
        path = Path(db_path)
        if not path.is_absolute():
            path = settings.project_root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        database_url = database_url.set(database=path.as_posix())

engine = create_async_engine(database_url, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if settings.database_url.startswith("sqlite"):
            result = await conn.execute(text("PRAGMA table_info(mailbox_credentials)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "encrypted_access_token" not in columns:
                await conn.execute(text("ALTER TABLE mailbox_credentials ADD COLUMN encrypted_access_token TEXT"))
            result = await conn.execute(text("PRAGMA table_info(refresh_jobs)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "memory_peak_rss_bytes" not in columns:
                await conn.execute(text("ALTER TABLE refresh_jobs ADD COLUMN memory_peak_rss_bytes INTEGER"))
            result = await conn.execute(text("PRAGMA table_info(account_snapshots)"))
            columns = {str(row[1]) for row in result.fetchall()}
            if "usage_estimate_enabled" not in columns:
                await conn.execute(
                    text("ALTER TABLE account_snapshots ADD COLUMN usage_estimate_enabled BOOLEAN DEFAULT 1")
                )
            await conn.execute(
                text("UPDATE account_snapshots SET usage_estimate_enabled = 1 WHERE usage_estimate_enabled IS NULL")
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
