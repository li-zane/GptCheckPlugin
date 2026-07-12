import asyncio
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.runtime_config as runtime_config
from app.core.database import Base
from app.models import AppSetting
from app.schemas import AppSettingsUpdate
from app.services.runtime_config import RuntimeConfigService


class RuntimeConfigTests(unittest.TestCase):
    def test_test_environment_never_persists_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            settings = _FakeSettings(project_root)
            settings.app_env = "test"

            RuntimeConfigService(settings)._persist_settings_file({}, None)

            self.assertFalse((project_root / ".env").exists())

    def test_usage_limit_default_ranges_reject_reversed_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            AppSettingsUpdate(
                usage_limit_default_ranges={
                    "k12": {
                        "five_hour": {"lower": 45, "upper": 30},
                        "seven_day": {"lower": 200, "upper": 260},
                        "monthly": {"lower": 500, "upper": 700},
                    }
                }
            )

    def test_usage_limit_sample_thresholds_are_persisted(self) -> None:
        asyncio.run(self._assert_usage_limit_sample_thresholds_are_persisted())

    async def _assert_usage_limit_sample_thresholds_are_persisted(self) -> None:
        original_sessionmaker = runtime_config.AsyncSessionLocal
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            db_path = project_root / "settings.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
            runtime_config.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

                service = RuntimeConfigService(_FakeSettings(project_root))
                settings = await service.update_public_settings(
                    {
                        "usage_limit_sample_five_hour_threshold_percent": 95,
                        "usage_limit_sample_seven_day_threshold_percent": 96.5,
                    }
                )

                self.assertEqual(settings["usage_limit_sample_five_hour_threshold_percent"], 95.0)
                self.assertEqual(settings["usage_limit_sample_seven_day_threshold_percent"], 96.5)

                async with runtime_config.AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(AppSetting).where(
                            AppSetting.key.in_(
                                [
                                    "usage_limit_sample_five_hour_threshold_percent",
                                    "usage_limit_sample_seven_day_threshold_percent",
                                ]
                            )
                        )
                    )
                    values = {row.key: row.value for row in result.scalars().all()}

                self.assertEqual(values["usage_limit_sample_five_hour_threshold_percent"], "95")
                self.assertEqual(values["usage_limit_sample_seven_day_threshold_percent"], "96.5")

                env_text = (project_root / ".env").read_text(encoding="utf-8")
                self.assertIn("USAGE_LIMIT_SAMPLE_FIVE_HOUR_THRESHOLD_PERCENT=95", env_text)
                self.assertIn("USAGE_LIMIT_SAMPLE_SEVEN_DAY_THRESHOLD_PERCENT=96.5", env_text)
            finally:
                runtime_config.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()

    def test_usage_limit_default_ranges_are_persisted(self) -> None:
        asyncio.run(self._assert_usage_limit_default_ranges_are_persisted())

    async def _assert_usage_limit_default_ranges_are_persisted(self) -> None:
        original_sessionmaker = runtime_config.AsyncSessionLocal
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            db_path = project_root / "settings.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
            runtime_config.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

                service = RuntimeConfigService(_FakeSettings(project_root))
                settings = await service.update_public_settings(
                    {
                        "usage_limit_default_ranges": {
                            "k12": {
                                "five_hour": {"lower": 30, "upper": 45},
                                "seven_day": {"lower": 200, "upper": 260},
                                "monthly": {"lower": 500, "upper": 700},
                            },
                            "enterprise": {
                                "five_hour": {"lower": 40, "upper": 60},
                                "seven_day": {"lower": 300, "upper": 400},
                                "monthly": {"lower": 800, "upper": 1000},
                            },
                        }
                    }
                )

                self.assertEqual(settings["usage_limit_default_ranges"]["k12"]["five_hour"]["lower"], 30.0)
                self.assertEqual(settings["usage_limit_default_ranges"]["k12"]["monthly"], {"lower": 800.0, "upper": 1040.0})
                self.assertEqual(
                    settings["usage_limit_default_ranges"]["enterprise"]["monthly"],
                    {"lower": 1200.0, "upper": 1600.0},
                )
                self.assertIn("unknown", settings["usage_limit_default_ranges"])

                async with runtime_config.AsyncSessionLocal() as db:
                    row = await db.get(AppSetting, "usage_limit_default_ranges")
                self.assertIsNotNone(row)
                self.assertIn('"k12"', row.value or "")

                env_text = (project_root / ".env").read_text(encoding="utf-8")
                self.assertIn("USAGE_LIMIT_DEFAULT_RANGES_JSON=", env_text)
            finally:
                runtime_config.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()


class _FakeSettings:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.app_env = "development"
        self.app_name = "Test App"
        self.sub2api_base_url = "http://localhost:8080/api/v1"
        self.sub2api_auth_token = ""
        self.sub2api_auth_header = "Authorization"
        self.sub2api_auth_scheme = "Bearer"
        self.sub2api_accounts_path = "/admin/accounts"
        self.sub2api_access_token_path = "credentials.access_token"
        self.sub2api_auto_clear_error = True
        self.sub2api_auto_recover_state = True
        self.automation_paused = False
        self.recovery_enabled = False
        self.monitor_interval_seconds = 300
        self.usage_refresh_enabled = False
        self.usage_refresh_interval_seconds = 3600
        self.usage_refresh_max_concurrency = 5
        self.usage_limit_sample_five_hour_threshold_percent = 0.0
        self.usage_limit_sample_seven_day_threshold_percent = 0.0
        self.usage_limit_default_ranges_json = ""
        self.refresh_max_concurrency = 1
        self.protocol_refresh_max_concurrency = 1
        self.browser_refresh_max_concurrency = 1
        self.browser_min_available_memory_mb = 500
        self.subscription_refresh_batch_size = 3
        self.subscription_refresh_max_concurrency = 3
        self.display_timezone = "Asia/Shanghai"


if __name__ == "__main__":
    unittest.main()
