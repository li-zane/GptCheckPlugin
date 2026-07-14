import asyncio
from datetime import timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.runtime_config as runtime_config
from app.core.config import Settings
from app.core.database import Base
from app.models import AppSetting, UpstreamRateChangeLog, utcnow
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

    def test_runtime_x_api_key_is_not_copied_to_plaintext_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            service = RuntimeConfigService(_FakeSettings(project_root))

            service._persist_settings_file(_public_settings_fixture(), "runtime-secret-value")

            env_text = (project_root / ".env").read_text(encoding="utf-8")
            self.assertNotIn("runtime-secret-value", env_text)
            self.assertNotIn("SUB2API_AUTH_TOKEN", env_text)

    def test_clearing_runtime_x_api_key_removes_legacy_env_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            env_path = project_root / ".env"
            env_path.write_text(
                "SUB2API_AUTH_TOKEN=legacy-secret\n"
                "SUB2API_AUTH_HEADER=x-api-key\n"
                "SUB2API_AUTH_SCHEME=\n",
                encoding="utf-8",
            )
            service = RuntimeConfigService(_FakeSettings(project_root))

            service._persist_settings_file(_public_settings_fixture(), "")

            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn("legacy-secret", env_text)
            self.assertNotIn("SUB2API_AUTH_TOKEN", env_text)
            self.assertNotIn("SUB2API_AUTH_HEADER", env_text)
            self.assertNotIn("SUB2API_AUTH_SCHEME", env_text)

    def test_rotating_runtime_x_api_key_removes_replaced_plaintext_env_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            env_path = project_root / ".env"
            env_path.write_text(
                "SUB2API_AUTH_TOKEN=legacy-plaintext-secret\n"
                "SUB2API_AUTH_HEADER=x-api-key\n"
                "SUB2API_AUTH_SCHEME=\n",
                encoding="utf-8",
            )
            service = RuntimeConfigService(_FakeSettings(project_root))

            service._persist_settings_file(
                _public_settings_fixture(),
                "new-encrypted-runtime-secret",
                remove_legacy_x_api_key=True,
            )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn("legacy-plaintext-secret", env_text)
            self.assertNotIn("new-encrypted-runtime-secret", env_text)
            self.assertNotIn("SUB2API_AUTH_TOKEN", env_text)

    def test_local_service_discovery_never_sends_the_admin_token(self) -> None:
        asyncio.run(self._assert_local_service_discovery_never_sends_the_admin_token())

    async def _assert_local_service_discovery_never_sends_the_admin_token(self) -> None:
        captured_headers: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(request.headers)
            return httpx.Response(401, json={"detail": "Authorization required"})

        with tempfile.TemporaryDirectory() as tmpdir:
            service = RuntimeConfigService(_FakeSettings(Path(tmpdir)))
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            config = SimpleNamespace(
                auth_token="administrator-secret",
                auth_header="x-api-key",
                auth_scheme="",
            )
            with patch("app.services.runtime_config.httpx.AsyncClient", return_value=client):
                hit = await service._find_sub2api([18082], config)

        self.assertIsNotNone(hit)
        self.assertTrue(captured_headers)
        self.assertTrue(all("x-api-key" not in headers for headers in captured_headers))
        self.assertTrue(all("authorization" not in headers for headers in captured_headers))

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

    def test_upstream_rate_log_retention_rejects_out_of_range_values(self) -> None:
        for value in (0, 3651):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AppSettingsUpdate(upstream_rate_log_retention_days=value)

    def test_upstream_rate_defaults_load_from_env_when_database_has_no_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "UPSTREAM_RATE_SYNC_ENABLED=true\n"
                "UPSTREAM_RATE_LOG_RETENTION_DAYS=180\n",
                encoding="utf-8",
            )
            settings = Settings(_env_file=env_path)
            service = RuntimeConfigService(settings)

            with patch.object(service, "_load_values", new=AsyncMock(return_value={})):
                enabled = asyncio.run(service.get_upstream_rate_sync_enabled())
                retention_days = asyncio.run(service.get_upstream_rate_log_retention_days())
                public = asyncio.run(service.get_public_settings())

        self.assertTrue(enabled)
        self.assertEqual(retention_days, 180)
        self.assertTrue(public["upstream_rate_sync_enabled"])
        self.assertEqual(public["upstream_rate_log_retention_days"], 180)

    def test_upstream_rate_settings_are_persisted(self) -> None:
        asyncio.run(self._assert_upstream_rate_settings_are_persisted())

    async def _assert_upstream_rate_settings_are_persisted(self) -> None:
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
                defaults = await service.get_public_settings()
                self.assertFalse(defaults["upstream_rate_sync_enabled"])
                self.assertEqual(defaults["upstream_rate_log_retention_days"], 90)

                settings = await service.update_public_settings(
                    {
                        "upstream_rate_sync_enabled": True,
                        "upstream_rate_log_retention_days": 180,
                    }
                )

                self.assertTrue(settings["upstream_rate_sync_enabled"])
                self.assertEqual(settings["upstream_rate_log_retention_days"], 180)
                self.assertTrue(await service.get_upstream_rate_sync_enabled())
                self.assertEqual(await service.get_upstream_rate_log_retention_days(), 180)

                async with runtime_config.AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(AppSetting).where(
                            AppSetting.key.in_(
                                [
                                    "upstream_rate_sync_enabled",
                                    "upstream_rate_log_retention_days",
                                ]
                            )
                        )
                    )
                    values = {row.key: row.value for row in result.scalars().all()}

                self.assertEqual(values["upstream_rate_sync_enabled"], "true")
                self.assertEqual(values["upstream_rate_log_retention_days"], "180")
                env_text = (project_root / ".env").read_text(encoding="utf-8")
                self.assertIn("UPSTREAM_RATE_SYNC_ENABLED=true", env_text)
                self.assertIn("UPSTREAM_RATE_LOG_RETENTION_DAYS=180", env_text)
            finally:
                runtime_config.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()

    def test_retention_setting_prunes_expired_logs_in_the_settings_transaction(self) -> None:
        asyncio.run(self._assert_retention_setting_prunes_expired_logs())

    async def _assert_retention_setting_prunes_expired_logs(self) -> None:
        original_sessionmaker = runtime_config.AsyncSessionLocal
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            db_path = project_root / "retention.db"
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}", future=True)
            runtime_config.AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)

                async with runtime_config.AsyncSessionLocal() as db:
                    db.add_all(
                        [
                            UpstreamRateChangeLog(
                                sub2api_account_id=1,
                                reason="test",
                                status="observed",
                                created_at=utcnow() - timedelta(days=31),
                            ),
                            UpstreamRateChangeLog(
                                sub2api_account_id=2,
                                reason="test",
                                status="observed",
                                created_at=utcnow() - timedelta(days=29),
                            ),
                        ]
                    )
                    await db.commit()

                service = RuntimeConfigService(_FakeSettings(project_root))
                await service.update_public_settings(
                    {"upstream_rate_log_retention_days": 30}
                )

                async with runtime_config.AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(UpstreamRateChangeLog.sub2api_account_id).order_by(
                            UpstreamRateChangeLog.sub2api_account_id
                        )
                    )
                    account_ids = list(result.scalars().all())
                    retention = await db.get(AppSetting, "upstream_rate_log_retention_days")

                self.assertEqual(account_ids, [2])
                self.assertIsNotNone(retention)
                self.assertEqual(retention.value, "30")
            finally:
                runtime_config.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()

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
        self.sub2api_scan_timeout_seconds = 0.5
        self.automation_paused = False
        self.recovery_enabled = False
        self.monitor_interval_seconds = 300
        self.usage_refresh_enabled = False
        self.usage_refresh_interval_seconds = 3600
        self.usage_refresh_max_concurrency = 5
        self.upstream_rate_sync_enabled = False
        self.upstream_rate_log_retention_days = 90
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


def _public_settings_fixture() -> dict:
    return {
        "site_name": "Test App",
        "sub2api_base_url": "http://localhost:8080/api/v1",
        "sub2api_auto_recover_state": True,
        "automation_paused": False,
        "recovery_enabled": False,
        "monitor_interval_seconds": 300,
        "usage_refresh_enabled": False,
        "usage_refresh_interval_seconds": 3600,
        "usage_refresh_max_concurrency": 5,
        "upstream_rate_sync_enabled": False,
        "upstream_rate_log_retention_days": 90,
        "usage_limit_sample_five_hour_threshold_percent": 0.0,
        "usage_limit_sample_seven_day_threshold_percent": 0.0,
        "usage_limit_default_ranges": {},
        "refresh_max_concurrency": 1,
        "protocol_refresh_max_concurrency": 1,
        "browser_refresh_max_concurrency": 1,
        "browser_min_available_memory_mb": 500,
        "subscription_refresh_batch_size": 3,
        "subscription_refresh_max_concurrency": 3,
        "display_timezone": "Asia/Shanghai",
    }


if __name__ == "__main__":
    unittest.main()
