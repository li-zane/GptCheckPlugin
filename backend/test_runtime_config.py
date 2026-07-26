import asyncio
from datetime import timedelta
from io import BytesIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.runtime_config as runtime_config
from app.api.settings import _available_test_models, router as settings_router
from app.core.config import Settings
from app.core.database import Base
from app.core.security import require_admin
from app.models import (
    AccountSnapshot,
    AccountSchedulingChangeLog,
    AppSetting,
    UpstreamAccountConfig,
    UpstreamChannelChangeEvent,
    UpstreamRateChangeLog,
    utcnow,
)
from app.schemas import AppSettingsUpdate
from app.services.runtime_config import (
    MAX_CONFIGURED_PROBE_RESPONSE_BYTES,
    ProbeHit,
    RuntimeConfigService,
    RuntimeConfigServiceError,
)


def _logo_bytes(
    image_format: str,
    *,
    size: tuple[int, int],
    metadata: bool = False,
) -> bytes:
    output = BytesIO()
    mode = "RGB" if image_format == "JPEG" else "RGBA"
    color = (12, 98, 170) if mode == "RGB" else (12, 98, 170, 255)
    with Image.new(mode, size, color) as image:
        options = {}
        if metadata and image_format == "PNG":
            png_info = PngImagePlugin.PngInfo()
            png_info.add_text("comment", "metadata-to-remove-" * 256)
            options["pnginfo"] = png_info
        image.save(output, format=image_format, **options)
    return output.getvalue()


class RuntimeConfigTests(unittest.TestCase):
    def test_fallback_model_chain_accepts_json_arrows_and_deduplicates(self) -> None:
        self.assertEqual(
            runtime_config._normalize_model_chain('["gpt-5.4-mini", "grok-4.5"]'),
            ["gpt-5.4-mini", "grok-4.5"],
        )
        self.assertEqual(
            runtime_config._normalize_model_chain("gpt-5.4-mini -> grok-4.5 → gpt-5.4-mini"),
            ["gpt-5.4-mini", "grok-4.5"],
        )
        payload = AppSettingsUpdate(
            channel_monitor_fallback_test_models=[
                " gpt-5.4-mini ",
                "grok-4.5",
                "gpt-5.4-mini",
            ]
        )
        self.assertEqual(
            payload.channel_monitor_fallback_test_models,
            ["gpt-5.4-mini", "grok-4.5"],
        )

    def test_settings_available_test_models_merge_cached_account_whitelists(self) -> None:
        asyncio.run(self._assert_settings_available_test_models_merge_cached_account_whitelists())

    async def _assert_settings_available_test_models_merge_cached_account_whitelists(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as db:
                db.add_all(
                    [
                        AccountSnapshot(
                            email="oauth@example.com",
                            sub2api_account_id="1",
                            available_models=[
                                {"id": "model-b", "display_name": "Model B"},
                                {"id": "model-a", "display_name": "OAuth Model A"},
                            ],
                        ),
                        UpstreamAccountConfig(
                            sub2api_account_id=2,
                            available_models=[
                                {"id": "model-a", "display_name": "API Model A"},
                                {"id": "model-c", "display_name": "Model C"},
                                {"id": "", "display_name": "ignored"},
                            ],
                        ),
                    ]
                )
                await db.commit()

                models = await _available_test_models(db)

            self.assertEqual(
                models,
                [
                    {"id": "model-a", "display_name": "OAuth Model A"},
                    {"id": "model-b", "display_name": "Model B"},
                    {"id": "model-c", "display_name": "Model C"},
                ],
            )
        finally:
            await engine.dispose()

    def test_logo_route_is_nosniff_and_immutable(self) -> None:
        app = FastAPI()
        app.include_router(settings_router, prefix="/api/settings")
        logo = _logo_bytes("PNG", size=(8, 8))
        service = SimpleNamespace(get_site_logo=AsyncMock(return_value=(logo, "image/png")))
        with (
            patch("app.api.settings.get_runtime_config_service", return_value=service),
            TestClient(app) as client,
        ):
            response = client.get("/api/settings/logo")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("immutable", response.headers["cache-control"])

    def test_logo_validation_decodes_bounds_and_normalizes(self) -> None:
        raw = _logo_bytes("PNG", size=(16, 16), metadata=True)
        normalized = runtime_config._normalize_logo_bytes(raw, "image/png")
        self.assertLess(len(normalized), len(raw))
        self.assertTrue(runtime_config._valid_logo_bytes(normalized, "image/png"))
        with Image.open(BytesIO(normalized)) as image:
            self.assertEqual(image.size, (16, 16))
            self.assertNotIn("comment", image.info)

        for image_format, mime in (
            ("PNG", "image/png"),
            ("JPEG", "image/jpeg"),
            ("WEBP", "image/webp"),
        ):
            with self.subTest(image_format=image_format):
                encoded = runtime_config._normalize_logo_bytes(
                    _logo_bytes(image_format, size=(12, 9)),
                    mime,
                )
                self.assertTrue(runtime_config._valid_logo_bytes(encoded, mime))

        with self.assertRaises(RuntimeConfigServiceError):
            runtime_config._normalize_logo_bytes(raw, "image/jpeg")
        with self.assertRaises(RuntimeConfigServiceError):
            runtime_config._normalize_logo_bytes(
                _logo_bytes("PNG", size=(2049, 1)),
                "image/png",
            )

    def test_settings_route_maps_credential_rebind_conflict_to_409(self) -> None:
        app = FastAPI()
        app.include_router(settings_router, prefix="/api/settings")
        service = SimpleNamespace(
            get_public_settings=AsyncMock(return_value=_route_public_settings_fixture()),
            update_public_settings=AsyncMock(
                side_effect=RuntimeConfigServiceError("credential rebind confirmation required", status_code=409)
            )
        )
        app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
        with (
            patch("app.api.settings.get_runtime_config_service", return_value=service),
            TestClient(app) as client,
        ):
            response = client.put(
                "/api/settings",
                json={"sub2api_base_url": "https://replacement.example/api/v1"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "credential rebind confirmation required"})

    def test_settings_route_does_not_wake_tasks_for_unchanged_automation_fields(self) -> None:
        before = _route_public_settings_fixture()
        after = {**before, "site_name": "Renamed App"}
        payload = {
            field_name: before[field_name]
            for field_name in AppSettingsUpdate.model_fields
            if field_name in before
        }
        payload["site_name"] = after["site_name"]
        response, services = _put_settings_route(before, after, payload)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["site_name"], "Renamed App")
        self.assertNotIn(
            "channel_monitor_unavailable_consecutive_threshold",
            response.json(),
        )
        self.assertNotIn(
            "channel_monitor_recovery_consecutive_threshold",
            response.json(),
        )
        self.assertEqual(response.json()["channel_monitor_recovery_test_attempts"], 1)
        services.monitor.wake.assert_not_called()
        services.upstream.wake.assert_not_called()
        services.upstream.wake_inventory.assert_not_called()
        services.upstream.wake_upstream.assert_not_called()
        services.upstream.wake_priority.assert_not_called()
        services.usage.wake.assert_not_called()
        services.refresh.wake_concurrency.assert_not_awaited()
        services.liveness.wake.assert_not_awaited()

    def test_settings_route_wakes_only_api_key_inventory_loop_for_inventory_change(self) -> None:
        before = _route_public_settings_fixture()
        after = {**before, "api_key_account_sync_enabled": False}
        response, services = _put_settings_route(
            before,
            after,
            {"api_key_account_sync_enabled": False},
        )

        self.assertEqual(response.status_code, 200, response.text)
        services.upstream.wake_inventory.assert_called_once_with()
        services.upstream.wake.assert_not_called()
        services.upstream.wake_upstream.assert_not_called()
        services.upstream.wake_priority.assert_not_called()
        services.monitor.wake.assert_not_called()
        services.usage.wake.assert_not_called()

    def test_settings_route_wakes_only_upstream_loop_for_upstream_change(self) -> None:
        before = _route_public_settings_fixture()
        after = {**before, "upstream_sync_interval_seconds": 1200}
        response, services = _put_settings_route(
            before,
            after,
            {"upstream_sync_interval_seconds": 1200},
        )

        self.assertEqual(response.status_code, 200, response.text)
        services.upstream.wake_upstream.assert_called_once_with()
        services.upstream.wake.assert_not_called()
        services.upstream.wake_inventory.assert_not_called()
        services.upstream.wake_priority.assert_not_called()
        services.monitor.wake.assert_not_called()
        services.usage.wake.assert_not_called()

    def test_settings_route_wakes_upstream_loop_for_policy_guard_changes(self) -> None:
        changes = {
            "api_key_auto_pause_on_channel_monitor_unavailable_enabled": True,
            "channel_monitor_auto_probe_enabled": False,
            "channel_monitor_fallback_test_attempts": 3,
            "channel_monitor_recovery_test_attempts": 5,
            "api_key_auto_pause_on_upstream_rate_increase_enabled": True,
            "upstream_rate_increase_threshold_percent": 25.5,
        }
        for field_name, value in changes.items():
            with self.subTest(field_name=field_name):
                before = _route_public_settings_fixture()
                after = {**before, field_name: value}
                response, services = _put_settings_route(
                    before,
                    after,
                    {field_name: value},
                )

                self.assertEqual(response.status_code, 200, response.text)
                services.upstream.wake_upstream.assert_called_once_with()
                services.upstream.wake.assert_not_called()
                services.upstream.wake_inventory.assert_not_called()
                services.upstream.wake_priority.assert_not_called()
                services.monitor.wake.assert_not_called()
                services.usage.wake.assert_not_called()

    def test_settings_route_wakes_only_model_whitelist_loop_for_model_whitelist_change(self) -> None:
        before = _route_public_settings_fixture()
        after = {**before, "account_model_whitelist_sync_enabled": True}
        response, services = _put_settings_route(
            before,
            after,
            {"account_model_whitelist_sync_enabled": True},
        )

        self.assertEqual(response.status_code, 200, response.text)
        services.upstream.wake_model_whitelist.assert_called_once_with()
        services.monitor.wake.assert_not_called()
        services.upstream.wake_inventory.assert_not_called()
        services.upstream.wake_upstream.assert_not_called()
        services.upstream.wake_priority.assert_not_called()
        services.upstream.wake.assert_not_called()
        services.usage.wake.assert_not_called()

    def test_settings_route_wakes_priority_loop_without_upstream_probe(self) -> None:
        before = _route_public_settings_fixture()
        after = {**before, "priority_share_same_composite_multiplier": True}
        response, services = _put_settings_route(
            before,
            after,
            {"priority_share_same_composite_multiplier": True},
        )

        self.assertEqual(response.status_code, 200, response.text)
        services.upstream.wake_priority.assert_called_once_with()
        services.upstream.wake_upstream.assert_not_called()
        services.upstream.wake_inventory.assert_not_called()
        services.upstream.wake.assert_not_called()

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

    def test_configured_sub2api_url_is_verified_with_its_credential(self) -> None:
        asyncio.run(self._assert_configured_sub2api_url_is_verified_with_its_credential())

    async def _assert_configured_sub2api_url_is_verified_with_its_credential(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"data": {"items": []}})

        real_async_client = httpx.AsyncClient

        def configured_client(**kwargs: object) -> httpx.AsyncClient:
            return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            service = RuntimeConfigService(_FakeSettings(Path(tmpdir)))
            config = SimpleNamespace(
                base_url="https://configured.example/api/v1",
                accounts_path="/admin/accounts",
                auth_token="configured-administrator-secret",
                auth_header="x-api-key",
                auth_scheme="",
            )
            with patch(
                "app.services.runtime_config.httpx.AsyncClient",
                side_effect=configured_client,
            ):
                hit = await service._probe_configured_sub2api(config)

        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.base_url, config.base_url)
        self.assertEqual(hit.port, 443)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url.path, "/api/v1/admin/accounts")
        self.assertEqual(captured[0].headers.get("x-api-key"), config.auth_token)

    def test_scan_prefers_the_current_configured_sub2api_url(self) -> None:
        asyncio.run(self._assert_scan_prefers_the_current_configured_sub2api_url())

    async def _assert_scan_prefers_the_current_configured_sub2api_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = RuntimeConfigService(_FakeSettings(Path(tmpdir)))
            config = SimpleNamespace(base_url="https://configured.example/api/v1")
            hit = ProbeHit(
                base_url=config.base_url,
                port=443,
                status="200",
                message="configured endpoint verified",
            )
            db = AsyncMock()
            service.get_sub2api_config = AsyncMock(return_value=config)  # type: ignore[method-assign]
            service._candidate_ports = Mock(return_value=[443, 18080])  # type: ignore[method-assign]
            service._probe_configured_sub2api = AsyncMock(return_value=hit)  # type: ignore[method-assign]
            service._find_sub2api = AsyncMock(  # type: ignore[method-assign]
                side_effect=AssertionError("port scan must not run after configured URL succeeds")
            )
            service._put = AsyncMock()  # type: ignore[method-assign]
            with patch.object(runtime_config, "AsyncSessionLocal", return_value=_SessionContext(db)):
                result = await service.scan_sub2api_ports(apply=False)

        self.assertTrue(result["found"])
        self.assertEqual(result["base_url"], config.base_url)
        self.assertEqual(result["port"], 443)
        self.assertEqual(result["checked_ports"], [443, 18080])
        service._find_sub2api.assert_not_awaited()  # type: ignore[attr-defined]

    def test_runtime_origin_change_requires_explicit_credential_rebind(self) -> None:
        asyncio.run(self._assert_runtime_origin_change_requires_explicit_credential_rebind())

    async def _assert_runtime_origin_change_requires_explicit_credential_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _FakeSettings(Path(tmpdir))
            settings.sub2api_auth_token = "retained-sub2api-administrator-key"
            settings.sub2api_auth_header = "x-api-key"
            settings.sub2api_auth_scheme = ""
            service = RuntimeConfigService(settings)
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            original_sessionmaker = runtime_config.AsyncSessionLocal
            runtime_config.AsyncSessionLocal = session_factory
            try:
                with self.assertRaises(RuntimeConfigServiceError) as context:
                    await service.update_public_settings(
                        {"sub2api_base_url": "https://replacement.example/api/v1"}
                    )
                self.assertEqual(context.exception.status_code, 409)

                updated = await service.update_public_settings(
                    {
                        "sub2api_base_url": "https://replacement.example/api/v1",
                        "confirm_sub2api_credential_rebind": True,
                    }
                )
                self.assertEqual(updated["sub2api_base_url"], "https://replacement.example/api/v1")
            finally:
                runtime_config.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()

    def test_scan_does_not_auto_apply_a_cross_origin_credential_rebind(self) -> None:
        asyncio.run(self._assert_scan_does_not_auto_apply_a_cross_origin_credential_rebind())

    async def _assert_scan_does_not_auto_apply_a_cross_origin_credential_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = RuntimeConfigService(_FakeSettings(Path(tmpdir)))
            config = SimpleNamespace(
                base_url="http://localhost:8080/api/v1",
                auth_token="retained-administrator-key",
            )
            hit = ProbeHit(
                base_url="http://127.0.0.1:18080/api/v1",
                port=18080,
                status="200",
                message="candidate detected",
            )
            db = AsyncMock()
            service.get_sub2api_config = AsyncMock(return_value=config)  # type: ignore[method-assign]
            service._candidate_ports = Mock(return_value=[8080, 18080])  # type: ignore[method-assign]
            service._probe_configured_sub2api = AsyncMock(return_value=None)  # type: ignore[method-assign]
            service._find_sub2api = AsyncMock(return_value=hit)  # type: ignore[method-assign]
            service._put = AsyncMock()  # type: ignore[method-assign]
            with patch.object(runtime_config, "AsyncSessionLocal", return_value=_SessionContext(db)):
                result = await service.scan_sub2api_ports(apply=True)

        self.assertTrue(result["found"])
        self.assertFalse(result["applied"])
        self.assertIn("明确确认", result["message"])
        persisted_keys = [args.args[1] for args in service._put.await_args_list]  # type: ignore[attr-defined]
        self.assertNotIn("sub2api_base_url", persisted_keys)

    def test_runtime_origin_change_can_clear_the_retained_credential(self) -> None:
        asyncio.run(self._assert_runtime_origin_change_can_clear_the_retained_credential())

    async def _assert_runtime_origin_change_can_clear_the_retained_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _FakeSettings(Path(tmpdir))
            settings.sub2api_auth_token = "legacy-sub2api-administrator-key"
            settings.sub2api_auth_header = "x-api-key"
            settings.sub2api_auth_scheme = ""
            service = RuntimeConfigService(settings)
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            original_sessionmaker = runtime_config.AsyncSessionLocal
            runtime_config.AsyncSessionLocal = session_factory
            try:
                await service.update_public_settings(
                    {
                        "sub2api_base_url": "https://replacement.example/api/v1",
                        "clear_sub2api_x_api_key": True,
                    }
                )
                effective = await service.get_sub2api_config()
                self.assertEqual(effective.base_url, "https://replacement.example/api/v1")
                self.assertEqual(effective.auth_token, "")
            finally:
                runtime_config.AsyncSessionLocal = original_sessionmaker
                await engine.dispose()

    def test_candidate_ports_are_bounded_to_configured_explicit_and_common_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _FakeSettings(Path(tmpdir))
            settings.sub2api_scan_ports = [28080]
            service = RuntimeConfigService(settings)
            ports = service._candidate_ports("http://localhost:18090/api/v1")

        self.assertEqual(ports[0:2], [18090, 28080])
        self.assertIn(8080, ports)
        self.assertIn(18080, ports)
        self.assertNotIn(3000, ports)
        self.assertNotIn(5432, ports)
        self.assertNotIn(6379, ports)
        self.assertNotIn(49152, ports)

    def test_configured_probe_stops_reading_after_raw_byte_limit(self) -> None:
        asyncio.run(self._assert_configured_probe_stops_reading_after_raw_byte_limit())

    async def _assert_configured_probe_stops_reading_after_raw_byte_limit(self) -> None:
        class CountingStream(httpx.AsyncByteStream):
            def __init__(self) -> None:
                self.chunks_read = 0

            async def __aiter__(self):
                for _ in range(100):
                    self.chunks_read += 1
                    yield b"x" * (MAX_CONFIGURED_PROBE_RESPONSE_BYTES // 4)

        stream = CountingStream()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=stream,
                request=request,
            )

        real_async_client = httpx.AsyncClient

        def configured_client(**kwargs: object) -> httpx.AsyncClient:
            return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            service = RuntimeConfigService(_FakeSettings(Path(tmpdir)))
            config = SimpleNamespace(
                base_url="https://configured.example/api/v1",
                accounts_path="/admin/accounts",
                auth_token="",
                auth_header="x-api-key",
                auth_scheme="",
            )
            with patch(
                "app.services.runtime_config.httpx.AsyncClient",
                side_effect=configured_client,
            ):
                hit = await service._probe_configured_sub2api(config)

        self.assertIsNone(hit)
        self.assertLess(stream.chunks_read, 100)

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

    def test_policy_guard_thresholds_reject_out_of_range_values(self) -> None:
        invalid_values = {
            "channel_monitor_fallback_test_attempts": (0, 6),
            "channel_monitor_recovery_test_attempts": (0, 6),
            "upstream_rate_increase_threshold_percent": (0, -1, 100_001, float("inf")),
            "upstream_rate_absolute_threshold": (0, -1, 1001, float("inf")),
            "upstream_balance_pause_threshold": (
                -1_000_000_001,
                1_000_000_001,
                float("inf"),
            ),
        }
        for field_name, values in invalid_values.items():
            for value in values:
                with (
                    self.subTest(field_name=field_name, value=value),
                    self.assertRaises(ValidationError),
                ):
                    AppSettingsUpdate(**{field_name: value})

    def test_upstream_rate_defaults_load_from_env_when_database_has_no_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "UPSTREAM_RATE_SYNC_ENABLED=true\n"
                "UPSTREAM_RATE_LOG_RETENTION_DAYS=180\n"
                "SHOW_STALE_NEGATIVE_BALANCE_ALERT=false\n"
                "API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED=true\n"
                "CHANNEL_MONITOR_UNAVAILABLE_CONSECUTIVE_THRESHOLD=3\n"
                "CHANNEL_MONITOR_RECOVERY_CONSECUTIVE_THRESHOLD=4\n"
                "API_KEY_AUTO_PAUSE_ON_UPSTREAM_RATE_INCREASE_ENABLED=true\n"
                "UPSTREAM_RATE_PAUSE_MODE=absolute_multiplier\n"
                "UPSTREAM_RATE_INCREASE_THRESHOLD_PERCENT=37.5\n"
                "UPSTREAM_RATE_ABSOLUTE_THRESHOLD=2.75\n"
                "UPSTREAM_BALANCE_PAUSE_THRESHOLD=12.5\n",
                encoding="utf-8",
            )
            settings = Settings(_env_file=env_path)
            service = RuntimeConfigService(settings)

            with patch.object(service, "_load_values", new=AsyncMock(return_value={})):
                enabled = asyncio.run(service.get_upstream_rate_sync_enabled())
                upstream_enabled = asyncio.run(service.get_upstream_sync_enabled())
                retention_days = asyncio.run(service.get_upstream_rate_log_retention_days())
                show_stale_alert = asyncio.run(
                    service.get_show_stale_negative_balance_alert()
                )
                auto_pause_on_monitor = asyncio.run(
                    service.get_api_key_auto_pause_on_channel_monitor_unavailable_enabled()
                )
                monitor_unavailable_threshold = asyncio.run(
                    service.get_channel_monitor_unavailable_consecutive_threshold()
                )
                monitor_recovery_threshold = asyncio.run(
                    service.get_channel_monitor_recovery_consecutive_threshold()
                )
                auto_pause_on_rate_increase = asyncio.run(
                    service.get_api_key_auto_pause_on_upstream_rate_increase_enabled()
                )
                rate_increase_threshold = asyncio.run(
                    service.get_upstream_rate_increase_threshold_percent()
                )
                rate_pause_mode = asyncio.run(service.get_upstream_rate_pause_mode())
                rate_absolute_threshold = asyncio.run(
                    service.get_upstream_rate_absolute_threshold()
                )
                balance_pause_threshold = asyncio.run(
                    service.get_upstream_balance_pause_threshold()
                )
                public = asyncio.run(service.get_public_settings())

        self.assertTrue(enabled)
        self.assertTrue(upstream_enabled)
        self.assertEqual(retention_days, 180)
        self.assertFalse(show_stale_alert)
        self.assertTrue(auto_pause_on_monitor)
        self.assertEqual(monitor_unavailable_threshold, 3)
        self.assertEqual(monitor_recovery_threshold, 4)
        self.assertTrue(auto_pause_on_rate_increase)
        self.assertEqual(rate_pause_mode, "absolute_multiplier")
        self.assertEqual(rate_increase_threshold, 37.5)
        self.assertEqual(rate_absolute_threshold, 2.75)
        self.assertEqual(balance_pause_threshold, 12.5)
        self.assertTrue(public["upstream_sync_enabled"])
        self.assertTrue(public["upstream_rate_sync_enabled"])
        self.assertEqual(public["upstream_rate_log_retention_days"], 180)
        self.assertFalse(public["show_stale_negative_balance_alert"])
        self.assertTrue(public["api_key_auto_pause_on_channel_monitor_unavailable_enabled"])
        self.assertEqual(public["channel_monitor_unavailable_consecutive_threshold"], 3)
        self.assertEqual(public["channel_monitor_recovery_consecutive_threshold"], 4)
        self.assertTrue(public["api_key_auto_pause_on_upstream_rate_increase_enabled"])
        self.assertEqual(public["upstream_rate_pause_mode"], "absolute_multiplier")
        self.assertEqual(public["upstream_rate_increase_threshold_percent"], 37.5)
        self.assertEqual(public["upstream_rate_absolute_threshold"], 2.75)
        self.assertEqual(public["upstream_balance_pause_threshold"], 12.5)

    def test_upstream_rate_settings_are_persisted(self) -> None:
        asyncio.run(self._assert_upstream_rate_settings_are_persisted())

    async def _assert_upstream_rate_settings_are_persisted(self) -> None:
        original_sessionmaker = runtime_config.AsyncSessionLocal
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / ".env").write_text(
                "CHANNEL_MONITOR_UNAVAILABLE_CONSECUTIVE_THRESHOLD=3\n"
                "CHANNEL_MONITOR_RECOVERY_CONSECUTIVE_THRESHOLD=4\n",
                encoding="utf-8",
            )
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
                self.assertFalse(
                    defaults["api_key_auto_pause_on_channel_monitor_unavailable_enabled"]
                )
                self.assertTrue(defaults["channel_monitor_auto_probe_enabled"])
                self.assertFalse(defaults["account_model_whitelist_sync_enabled"])
                self.assertEqual(defaults["account_model_whitelist_sync_interval_seconds"], 3600)
                self.assertFalse(defaults["account_model_whitelist_sync_each_time"])
                self.assertEqual(
                    defaults["channel_monitor_unavailable_consecutive_threshold"], 2
                )
                self.assertEqual(defaults["channel_monitor_recovery_consecutive_threshold"], 2)
                self.assertEqual(defaults["channel_monitor_recovery_test_attempts"], 1)
                self.assertFalse(defaults["api_key_auto_pause_on_upstream_rate_increase_enabled"])
                self.assertEqual(defaults["upstream_rate_pause_mode"], "increase_percent")
                self.assertEqual(defaults["upstream_rate_increase_threshold_percent"], 20.0)
                self.assertEqual(defaults["upstream_rate_absolute_threshold"], 1.0)
                self.assertEqual(defaults["upstream_balance_pause_threshold"], 0.0)

                settings = await service.update_public_settings(
                    {
                        "oauth_account_sync_enabled": False,
                        "api_key_account_sync_enabled": False,
                        "api_key_account_sync_interval_seconds": 600,
                        "upstream_sync_enabled": True,
                        "upstream_sync_interval_seconds": 1200,
                        "upstream_sync_max_concurrency": 2,
                        "upstream_rate_sync_enabled": True,
                        "upstream_priority_sync_enabled": False,
                        "api_key_auto_disable_on_upstream_unavailable": True,
                        "api_key_auto_pause_on_channel_monitor_unavailable_enabled": True,
                        "channel_monitor_auto_probe_enabled": False,
                        "account_model_whitelist_sync_enabled": True,
                        "account_model_whitelist_sync_interval_seconds": 1800,
                        "account_model_whitelist_sync_each_time": True,
                        "channel_monitor_recovery_test_attempts": 5,
                        "api_key_auto_pause_on_upstream_rate_increase_enabled": True,
                        "upstream_rate_pause_mode": "absolute_multiplier",
                        "upstream_rate_increase_threshold_percent": 37.5,
                        "upstream_rate_absolute_threshold": 2.75,
                        "upstream_balance_pause_threshold": 12.5,
                        "show_stale_negative_balance_alert": False,
                        "notify_account_enabled": True,
                        "upstream_rate_log_retention_days": 180,
                        "account_liveness_max_concurrency": 4,
                    }
                )

                self.assertFalse(settings["oauth_account_sync_enabled"])
                self.assertFalse(settings["api_key_account_sync_enabled"])
                self.assertEqual(settings["api_key_account_sync_interval_seconds"], 600)
                self.assertTrue(settings["upstream_sync_enabled"])
                self.assertEqual(settings["upstream_sync_interval_seconds"], 1200)
                self.assertEqual(settings["upstream_sync_max_concurrency"], 2)
                self.assertTrue(settings["upstream_rate_sync_enabled"])
                self.assertFalse(settings["upstream_priority_sync_enabled"])
                self.assertTrue(settings["api_key_auto_disable_on_upstream_unavailable"])
                self.assertTrue(
                    settings["api_key_auto_pause_on_channel_monitor_unavailable_enabled"]
                )
                self.assertFalse(settings["channel_monitor_auto_probe_enabled"])
                self.assertTrue(settings["account_model_whitelist_sync_enabled"])
                self.assertEqual(settings["account_model_whitelist_sync_interval_seconds"], 1800)
                self.assertTrue(settings["account_model_whitelist_sync_each_time"])
                self.assertEqual(
                    settings["channel_monitor_unavailable_consecutive_threshold"], 2
                )
                self.assertEqual(settings["channel_monitor_recovery_consecutive_threshold"], 2)
                self.assertEqual(settings["channel_monitor_recovery_test_attempts"], 5)
                self.assertTrue(settings["api_key_auto_pause_on_upstream_rate_increase_enabled"])
                self.assertEqual(settings["upstream_rate_pause_mode"], "absolute_multiplier")
                self.assertEqual(settings["upstream_rate_increase_threshold_percent"], 37.5)
                self.assertEqual(settings["upstream_rate_absolute_threshold"], 2.75)
                self.assertEqual(settings["upstream_balance_pause_threshold"], 12.5)
                self.assertFalse(settings["show_stale_negative_balance_alert"])
                self.assertTrue(settings["notify_account_enabled"])
                self.assertEqual(settings["account_liveness_max_concurrency"], 4)
                self.assertEqual(settings["upstream_rate_log_retention_days"], 180)
                self.assertTrue(await service.get_upstream_sync_enabled())
                self.assertTrue(await service.get_upstream_rate_sync_enabled())
                self.assertFalse(await service.get_upstream_priority_sync_enabled())
                self.assertTrue(await service.get_api_key_auto_disable_on_upstream_unavailable())
                self.assertTrue(
                    await service.get_api_key_auto_pause_on_channel_monitor_unavailable_enabled()
                )
                self.assertFalse(await service.get_channel_monitor_auto_probe_enabled())
                self.assertTrue(await service.get_account_model_whitelist_sync_enabled())
                self.assertEqual(await service.get_account_model_whitelist_sync_interval_seconds(), 1800)
                self.assertTrue(await service.get_account_model_whitelist_sync_each_time())
                self.assertEqual(
                    await service.get_channel_monitor_unavailable_consecutive_threshold(), 2
                )
                self.assertEqual(
                    await service.get_channel_monitor_recovery_consecutive_threshold(), 2
                )
                self.assertEqual(
                    await service.get_channel_monitor_recovery_test_attempts(), 5
                )
                self.assertTrue(
                    await service.get_api_key_auto_pause_on_upstream_rate_increase_enabled()
                )
                self.assertEqual(
                    await service.get_upstream_rate_pause_mode(), "absolute_multiplier"
                )
                self.assertEqual(
                    await service.get_upstream_rate_increase_threshold_percent(), 37.5
                )
                self.assertEqual(await service.get_upstream_rate_absolute_threshold(), 2.75)
                self.assertEqual(await service.get_upstream_balance_pause_threshold(), 12.5)
                self.assertFalse(await service.get_show_stale_negative_balance_alert())
                self.assertEqual(await service.get_upstream_rate_log_retention_days(), 180)

                async with runtime_config.AsyncSessionLocal() as db:
                    result = await db.execute(
                        select(AppSetting).where(
                            AppSetting.key.in_(
                                [
                                    "oauth_account_sync_enabled",
                                    "api_key_account_sync_enabled",
                                    "api_key_account_sync_interval_seconds",
                                    "upstream_sync_enabled",
                                    "upstream_sync_interval_seconds",
                                    "upstream_sync_max_concurrency",
                                    "upstream_rate_sync_enabled",
                                    "upstream_priority_sync_enabled",
                                    "api_key_auto_disable_on_upstream_unavailable",
                                    "api_key_auto_pause_on_channel_monitor_unavailable_enabled",
                                    "channel_monitor_auto_probe_enabled",
                                    "account_model_whitelist_sync_enabled",
                                    "account_model_whitelist_sync_interval_seconds",
                                    "account_model_whitelist_sync_each_time",
                                    "api_key_auto_pause_on_upstream_rate_increase_enabled",
                                    "upstream_rate_pause_mode",
                                    "upstream_rate_increase_threshold_percent",
                                    "upstream_rate_absolute_threshold",
                                    "upstream_balance_pause_threshold",
                                    "show_stale_negative_balance_alert",
                                    "notify_account_enabled",
                                    "upstream_rate_log_retention_days",
                                    "account_liveness_max_concurrency",
                                ]
                            )
                        )
                    )
                    values = {row.key: row.value for row in result.scalars().all()}

                self.assertEqual(values["oauth_account_sync_enabled"], "false")
                self.assertEqual(values["api_key_account_sync_enabled"], "false")
                self.assertEqual(values["upstream_sync_enabled"], "true")
                self.assertEqual(values["upstream_rate_sync_enabled"], "true")
                self.assertEqual(values["upstream_priority_sync_enabled"], "false")
                self.assertEqual(values["api_key_auto_disable_on_upstream_unavailable"], "true")
                self.assertEqual(
                    values["api_key_auto_pause_on_channel_monitor_unavailable_enabled"],
                    "true",
                )
                self.assertEqual(values["channel_monitor_auto_probe_enabled"], "false")
                self.assertEqual(values["account_model_whitelist_sync_enabled"], "true")
                self.assertEqual(values["account_model_whitelist_sync_interval_seconds"], "1800")
                self.assertEqual(values["account_model_whitelist_sync_each_time"], "true")
                self.assertNotIn("channel_monitor_unavailable_consecutive_threshold", values)
                self.assertNotIn("channel_monitor_recovery_consecutive_threshold", values)
                self.assertEqual(
                    values["api_key_auto_pause_on_upstream_rate_increase_enabled"], "true"
                )
                self.assertEqual(values["upstream_rate_pause_mode"], "absolute_multiplier")
                self.assertEqual(values["upstream_rate_increase_threshold_percent"], "37.5")
                self.assertEqual(values["upstream_rate_absolute_threshold"], "2.75")
                self.assertEqual(values["upstream_balance_pause_threshold"], "12.5")
                self.assertEqual(values["show_stale_negative_balance_alert"], "false")
                self.assertEqual(values["notify_account_enabled"], "true")
                self.assertEqual(values["upstream_rate_log_retention_days"], "180")
                env_text = (project_root / ".env").read_text(encoding="utf-8")
                self.assertIn("UPSTREAM_SYNC_ENABLED=true", env_text)
                self.assertIn("UPSTREAM_RATE_SYNC_ENABLED=true", env_text)
                self.assertIn("UPSTREAM_PRIORITY_SYNC_ENABLED=false", env_text)
                self.assertIn("API_KEY_AUTO_DISABLE_ON_UPSTREAM_UNAVAILABLE=true", env_text)
                self.assertIn(
                    "API_KEY_AUTO_PAUSE_ON_CHANNEL_MONITOR_UNAVAILABLE_ENABLED=true",
                    env_text,
                )
                self.assertIn("CHANNEL_MONITOR_AUTO_PROBE_ENABLED=false", env_text)
                self.assertIn("ACCOUNT_MODEL_WHITELIST_SYNC_EACH_TIME=true", env_text)
                self.assertNotIn("CHANNEL_MONITOR_UNAVAILABLE_CONSECUTIVE_THRESHOLD", env_text)
                self.assertNotIn("CHANNEL_MONITOR_RECOVERY_CONSECUTIVE_THRESHOLD", env_text)
                self.assertIn(
                    "API_KEY_AUTO_PAUSE_ON_UPSTREAM_RATE_INCREASE_ENABLED=true", env_text
                )
                self.assertIn("UPSTREAM_RATE_PAUSE_MODE=absolute_multiplier", env_text)
                self.assertIn("UPSTREAM_RATE_INCREASE_THRESHOLD_PERCENT=37.5", env_text)
                self.assertIn("UPSTREAM_RATE_ABSOLUTE_THRESHOLD=2.75", env_text)
                self.assertIn("UPSTREAM_BALANCE_PAUSE_THRESHOLD=12.5", env_text)
                self.assertIn("SHOW_STALE_NEGATIVE_BALANCE_ALERT=false", env_text)
                self.assertIn("NOTIFY_ACCOUNT_ENABLED=true", env_text)
                self.assertIn("ACCOUNT_LIVENESS_MAX_CONCURRENCY=4", env_text)
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
                            UpstreamChannelChangeEvent(
                                channel_id=1,
                                event_type="group_removed",
                                group_id="expired",
                                created_at=utcnow() - timedelta(days=31),
                            ),
                            UpstreamChannelChangeEvent(
                                channel_id=1,
                                event_type="group_added",
                                group_id="recent",
                                created_at=utcnow() - timedelta(days=29),
                            ),
                            AccountSchedulingChangeLog(
                                sub2api_account_id=3,
                                event_type="paused",
                                status="success",
                                created_at=utcnow() - timedelta(days=31),
                            ),
                            AccountSchedulingChangeLog(
                                sub2api_account_id=4,
                                event_type="restored",
                                status="success",
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
                    channel_event_ids = list(
                        (
                            await db.scalars(
                                select(UpstreamChannelChangeEvent.group_id)
                            )
                        ).all()
                    )
                    scheduling_account_ids = list(
                        (
                            await db.scalars(
                                select(AccountSchedulingChangeLog.sub2api_account_id)
                            )
                        ).all()
                    )
                    retention = await db.get(AppSetting, "upstream_rate_log_retention_days")

                self.assertEqual(account_ids, [2])
                self.assertEqual(channel_event_ids, ["recent"])
                self.assertEqual(scheduling_account_ids, [4])
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
        self.sub2api_scan_ports = [8080, 18080]
        self.automation_paused = False
        self.oauth_account_sync_enabled = True
        self.recovery_enabled = False
        self.monitor_interval_seconds = 300
        self.usage_refresh_enabled = False
        self.usage_refresh_interval_seconds = 3600
        self.usage_refresh_max_concurrency = 5
        self.api_key_account_sync_enabled = True
        self.api_key_account_sync_interval_seconds = 300
        self.upstream_sync_enabled = None
        self.upstream_sync_interval_seconds = 900
        self.upstream_sync_max_concurrency = 1
        self.upstream_rate_sync_enabled = False
        self.upstream_priority_sync_enabled = True
        self.priority_assign_disabled_api_key_accounts = False
        self.priority_share_same_composite_multiplier = False
        self.api_key_auto_disable_on_upstream_unavailable = False
        self.api_key_auto_pause_on_channel_monitor_unavailable_enabled = False
        self.channel_monitor_auto_probe_enabled = True
        self.account_model_whitelist_sync_enabled = False
        self.account_model_whitelist_sync_interval_seconds = 3600
        self.account_model_whitelist_sync_each_time = False
        self.channel_monitor_unavailable_consecutive_threshold = 2
        self.channel_monitor_recovery_consecutive_threshold = 2
        self.channel_monitor_recovery_test_attempts = 1
        self.api_key_auto_pause_on_upstream_rate_increase_enabled = False
        self.upstream_rate_pause_mode = "increase_percent"
        self.upstream_rate_increase_threshold_percent = 20.0
        self.upstream_rate_absolute_threshold = 1.0
        self.api_key_auto_pause_on_negative_balance_enabled = False
        self.upstream_negative_balance_basis = "wallet"
        self.upstream_balance_pause_threshold = 0.0
        self.show_stale_negative_balance_alert = True
        self.upstream_rate_log_retention_days = 90
        self.usage_limit_sample_five_hour_threshold_percent = 0.0
        self.usage_limit_sample_seven_day_threshold_percent = 0.0
        self.usage_limit_default_ranges_json = ""
        self.refresh_max_concurrency = 2
        self.protocol_refresh_max_concurrency = 2
        self.browser_refresh_max_concurrency = 1
        self.browser_min_available_memory_mb = 500
        self.subscription_refresh_batch_size = 3
        self.subscription_refresh_max_concurrency = 3
        self.account_liveness_max_concurrency = 3
        self.display_timezone = "Asia/Shanghai"


class _SessionContext:
    def __init__(self, db: object) -> None:
        self.db = db

    async def __aenter__(self) -> object:
        return self.db

    async def __aexit__(self, *_args: object) -> None:
        return None


def _public_settings_fixture() -> dict:
    return {
        "site_name": "Test App",
        "sub2api_base_url": "http://localhost:8080/api/v1",
        "sub2api_auto_recover_state": True,
        "automation_paused": False,
        "oauth_account_sync_enabled": True,
        "recovery_enabled": False,
        "monitor_interval_seconds": 300,
        "usage_refresh_enabled": False,
        "usage_refresh_interval_seconds": 3600,
        "usage_refresh_max_concurrency": 5,
        "api_key_account_sync_enabled": True,
        "api_key_account_sync_interval_seconds": 300,
        "upstream_sync_enabled": False,
        "upstream_sync_interval_seconds": 900,
        "upstream_sync_max_concurrency": 1,
        "upstream_rate_sync_enabled": False,
        "upstream_priority_sync_enabled": True,
        "priority_assign_disabled_api_key_accounts": False,
        "priority_share_same_composite_multiplier": False,
        "api_key_auto_disable_on_upstream_unavailable": False,
        "api_key_auto_pause_on_channel_monitor_unavailable_enabled": False,
        "channel_monitor_auto_probe_enabled": True,
        "account_model_whitelist_sync_enabled": False,
        "account_model_whitelist_sync_interval_seconds": 3600,
        "account_model_whitelist_sync_each_time": False,
        "channel_monitor_unavailable_consecutive_threshold": 2,
        "channel_monitor_recovery_consecutive_threshold": 2,
        "channel_monitor_recovery_test_attempts": 1,
        "api_key_auto_pause_on_upstream_rate_increase_enabled": False,
        "upstream_rate_pause_mode": "increase_percent",
        "upstream_rate_increase_threshold_percent": 20.0,
        "upstream_rate_absolute_threshold": 1.0,
        "api_key_auto_pause_on_negative_balance_enabled": False,
        "upstream_negative_balance_basis": "wallet",
        "upstream_balance_pause_threshold": 0.0,
        "show_stale_negative_balance_alert": True,
        "upstream_rate_log_retention_days": 90,
        "usage_limit_sample_five_hour_threshold_percent": 0.0,
        "usage_limit_sample_seven_day_threshold_percent": 0.0,
        "usage_limit_default_ranges": {},
        "refresh_max_concurrency": 2,
        "protocol_refresh_max_concurrency": 2,
        "browser_refresh_max_concurrency": 1,
        "browser_min_available_memory_mb": 500,
        "subscription_refresh_batch_size": 3,
        "subscription_refresh_max_concurrency": 3,
        "account_liveness_max_concurrency": 3,
        "display_timezone": "Asia/Shanghai",
    }


def _route_public_settings_fixture() -> dict:
    return {
        **_public_settings_fixture(),
        "sub2api_port": 8080,
        "sub2api_base_url_source": "manual",
        "sub2api_x_api_key_set": True,
        "sub2api_x_api_key_hint": "***configured***",
        "last_scan_at": None,
        "last_scan_status": None,
        "last_scan_message": None,
    }


def _put_settings_route(
    before: dict,
    after: dict,
    payload: dict,
) -> tuple[object, SimpleNamespace]:
    app = FastAPI()
    app.include_router(settings_router, prefix="/api/settings")
    runtime_service = SimpleNamespace(
        get_public_settings=AsyncMock(return_value=before),
        update_public_settings=AsyncMock(return_value=after),
    )
    services = SimpleNamespace(
        monitor=Mock(),
        upstream=Mock(),
        usage=Mock(),
        refresh=SimpleNamespace(wake_concurrency=AsyncMock()),
        liveness=SimpleNamespace(wake=AsyncMock()),
    )
    app.dependency_overrides[require_admin] = lambda: {"sub": "admin"}
    with (
        patch("app.api.settings.get_runtime_config_service", return_value=runtime_service),
        patch("app.api.settings.get_monitor_service", return_value=services.monitor),
        patch("app.api.settings.get_upstream_rate_sync_service", return_value=services.upstream),
        patch("app.api.settings.get_usage_refresh_service", return_value=services.usage),
        patch("app.api.settings.get_refresh_service", return_value=services.refresh),
        patch("app.api.settings.get_account_liveness_limiter", return_value=services.liveness),
        patch("app.api.settings._available_test_models", new=AsyncMock(return_value=[])),
        TestClient(app) as client,
    ):
        response = client.put("/api/settings", json=payload)
    return response, services


if __name__ == "__main__":
    unittest.main()
