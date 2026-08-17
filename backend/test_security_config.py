from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError
from starlette.responses import Response

from app.core.config import Settings
from app.core.security import issue_session
import run_production


class ProductionSecurityConfigTests(unittest.TestCase):
    def _secure_production_values(self) -> dict[str, object]:
        return {
            "app_env": "production",
            "app_admin_key": "admin-" + "a" * 40,
            "app_session_secret": "session-" + "b" * 40,
            "app_encryption_key": "encryption-" + "c" * 40,
            "cookie_secure": True,
            "_env_file": None,
        }

    def test_development_keeps_local_defaults(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(settings.app_env, "development")
        self.assertFalse(settings.cookie_secure)

    def test_legacy_environment_names_keep_management_and_availability_settings(self) -> None:
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "SUB2API_BASE_URL=http://127.0.0.1:18080/api/v1\n"
                "API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED=true\n"
                "CHANNEL_MONITOR_FALLBACK_TEST_MODEL=legacy-model\n"
                "MANUAL_UPSTREAM_SYNC_CHANNEL_MONITORS_ENABLED=false\n",
                encoding="utf-8",
            )

            settings = Settings(_env_file=env_path)

        self.assertEqual(settings.management_site_base_url, "http://127.0.0.1:18080/api/v1")
        self.assertTrue(settings.api_account_auto_pause_on_upstream_monitor_unavailable_enabled)
        self.assertEqual(settings.upstream_monitor_fallback_test_model, "legacy-model")
        self.assertFalse(settings.manual_upstream_monitor_sync_enabled)

    def test_production_rejects_default_and_example_secrets(self) -> None:
        placeholders = {
            "app_admin_key": (
                "change-me-now",
                "replace-with-a-long-random-admin-key",
            ),
            "app_session_secret": (
                "change-me-session-secret",
                "replace-with-a-long-random-session-secret",
            ),
            "app_encryption_key": (
                "change-me-encryption-key",
                "replace-with-a-long-random-encryption-key",
            ),
        }
        for field, values in placeholders.items():
            for placeholder in values:
                config = self._secure_production_values()
                config[field] = placeholder
                with self.subTest(field=field, placeholder=placeholder), self.assertRaises(ValidationError):
                    Settings(**config)

    def test_production_requires_secure_cookie(self) -> None:
        config = self._secure_production_values()
        config["cookie_secure"] = False
        with self.assertRaises(ValidationError):
            Settings(**config)

    def test_production_rejects_short_secrets(self) -> None:
        for field in (
            "app_admin_key",
            "app_session_secret",
            "app_encryption_key",
        ):
            config = self._secure_production_values()
            config[field] = "x" * 31
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Settings(**config)

    def test_secure_production_configuration_is_accepted(self) -> None:
        settings = Settings(**self._secure_production_values())
        self.assertEqual(settings.app_env, "production")
        self.assertTrue(settings.cookie_secure)

    def test_secure_cookie_is_never_downgraded_by_the_backend_request_scheme(self) -> None:
        response = Response()
        settings = SimpleNamespace(
            app_session_secret="session-secret-for-cookie-test",
            app_session_ttl_seconds=3600,
            session_cookie_name="guardian_session",
            cookie_secure=True,
        )

        with patch("app.core.security.get_settings", return_value=settings):
            issue_session(response, request=None)

        cookie = response.headers["set-cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)

    def test_production_runner_sets_environment_before_starting_uvicorn(self) -> None:
        fake_uvicorn = ModuleType("uvicorn")
        fake_uvicorn.run = Mock()  # type: ignore[attr-defined]
        with (
            patch.dict(os.environ, {"APP_ENV": "development"}),
            patch.dict(sys.modules, {"uvicorn": fake_uvicorn}),
        ):
            run_production.main()
            self.assertEqual(os.environ["APP_ENV"], "production")

        fake_uvicorn.run.assert_called_once()  # type: ignore[attr-defined]
        kwargs = fake_uvicorn.run.call_args.kwargs  # type: ignore[attr-defined]
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 5173)

    def test_package_production_script_uses_fail_closed_runner(self) -> None:
        package_path = Path(__file__).resolve().parents[1] / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        self.assertEqual(package["scripts"]["backend:prod"], "python backend/run_production.py")
        self.assertEqual(
            package["scripts"]["dev"],
            "npm run frontend:build && npm run backend:dev",
        )


if __name__ == "__main__":
    unittest.main()
