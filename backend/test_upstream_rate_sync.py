import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.api import settings as settings_api
from app.main import app, lifespan
from app.schemas import AppSettingsUpdate
from app.services.upstream_rate_sync import UpstreamRateSyncService


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class UpstreamRateSyncServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_discovers_only_when_enabled_and_not_paused(self) -> None:
        session = object()
        channel_service = SimpleNamespace(discover_all=AsyncMock())
        runtime_config = SimpleNamespace(
            get_upstream_rate_log_retention_days=AsyncMock(return_value=45),
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
        )
        service = UpstreamRateSyncService(runtime_config, channel_service)
        prune = AsyncMock(return_value=0)

        with (
            patch(
                "app.services.upstream_rate_sync.AsyncSessionLocal",
                return_value=_SessionContext(session),
            ),
            patch(
                "app.services.upstream_rate_sync.prune_upstream_rate_change_logs",
                new=prune,
            ),
        ):
            await service._run_once()

        runtime_config.get_upstream_rate_sync_enabled.assert_awaited_once_with()
        runtime_config.get_automation_paused.assert_awaited_once_with()
        channel_service.discover_all.assert_awaited_once_with(session)
        prune.assert_awaited_once_with(session, retention_days=45)

        for enabled, paused in ((False, False), (True, True)):
            with self.subTest(enabled=enabled, paused=paused):
                channel_service.discover_all.reset_mock()
                runtime_config.get_upstream_rate_sync_enabled = AsyncMock(return_value=enabled)
                runtime_config.get_automation_paused = AsyncMock(return_value=paused)
                with patch(
                    "app.services.upstream_rate_sync.prune_upstream_rate_change_logs",
                    new=prune,
                ):
                    await service._run_once()
                runtime_config.get_upstream_rate_sync_enabled.assert_awaited_once_with()
                runtime_config.get_automation_paused.assert_awaited_once_with()
                channel_service.discover_all.assert_not_awaited()
        self.assertEqual(prune.await_count, 3)
        self.assertEqual(runtime_config.get_upstream_rate_log_retention_days.await_count, 3)

    async def test_loop_can_wake_stop_and_isolates_credential_bearing_errors(self) -> None:
        channel_service = SimpleNamespace(
            discover_all=AsyncMock(
                side_effect=[RuntimeError("Bearer credential-must-not-be-recorded"), None]
            )
        )
        runtime_config = SimpleNamespace(
            get_monitor_interval_seconds=AsyncMock(return_value=3600),
            get_upstream_rate_log_retention_days=AsyncMock(return_value=30),
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
        )
        service = UpstreamRateSyncService(runtime_config, channel_service)
        event = AsyncMock()
        prune = AsyncMock(return_value=0)

        with (
            patch(
                "app.services.upstream_rate_sync.AsyncSessionLocal",
                side_effect=lambda: _SessionContext(object()),
            ),
            patch("app.services.upstream_rate_sync.record_event", event),
            patch(
                "app.services.upstream_rate_sync.prune_upstream_rate_change_logs",
                new=prune,
            ),
        ):
            service.start()
            service.wake()
            await _wait_until(lambda: channel_service.discover_all.await_count >= 1)
            service.wake()
            await _wait_until(lambda: channel_service.discover_all.await_count >= 2)
            await service.stop()

        self.assertIsNone(service._task)
        event.assert_awaited_once()
        self.assertEqual(event.await_args.args[1], "upstream_rate_sync_failed")
        self.assertEqual(
            event.await_args.args[2],
            "Scheduled upstream rate synchronization failed.",
        )
        self.assertNotIn("credential-must-not-be-recorded", str(event.await_args))
        self.assertEqual(prune.await_count, 2)

    async def test_stop_cancels_an_inflight_synchronization(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def discover_all(_session: object) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        channel_service = SimpleNamespace(discover_all=discover_all)
        runtime_config = SimpleNamespace(
            get_monitor_interval_seconds=AsyncMock(return_value=3600),
            get_upstream_rate_log_retention_days=AsyncMock(return_value=30),
            get_upstream_rate_sync_enabled=AsyncMock(return_value=True),
            get_automation_paused=AsyncMock(return_value=False),
        )
        service = UpstreamRateSyncService(runtime_config, channel_service)

        with (
            patch(
                "app.services.upstream_rate_sync.AsyncSessionLocal",
                side_effect=lambda: _SessionContext(object()),
            ),
            patch(
                "app.services.upstream_rate_sync.prune_upstream_rate_change_logs",
                new=AsyncMock(return_value=0),
            ),
        ):
            service.start()
            service.wake()
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.wait_for(service.stop(), timeout=1)

        self.assertTrue(cancelled.is_set())
        self.assertIsNone(service._task)


class UpstreamRateSyncWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_update_and_sub2api_scan_wake_rate_sync(self) -> None:
        runtime_config = SimpleNamespace(
            update_public_settings=AsyncMock(return_value={}),
            scan_sub2api_ports=AsyncMock(return_value={}),
        )
        monitor = SimpleNamespace(wake=Mock())
        rate_sync = SimpleNamespace(wake=Mock())
        usage_refresh = SimpleNamespace(wake=Mock())
        refresh = SimpleNamespace(wake_concurrency=AsyncMock())

        with (
            patch.object(settings_api, "get_runtime_config_service", return_value=runtime_config),
            patch.object(settings_api, "get_monitor_service", return_value=monitor),
            patch.object(settings_api, "get_upstream_rate_sync_service", return_value=rate_sync),
            patch.object(settings_api, "get_usage_refresh_service", return_value=usage_refresh),
            patch.object(settings_api, "get_refresh_service", return_value=refresh),
            patch.object(settings_api, "AppSettingsOut", side_effect=lambda **values: values),
            patch.object(settings_api, "Sub2ApiPortScanResult", side_effect=lambda **values: values),
        ):
            await settings_api.update_settings(
                AppSettingsUpdate(upstream_rate_sync_enabled=True),
                {},
            )
            await settings_api.scan_sub2api({})

        self.assertEqual(rate_sync.wake.call_count, 2)
        self.assertEqual(monitor.wake.call_count, 2)
        usage_refresh.wake.assert_called_once_with()
        refresh.wake_concurrency.assert_awaited_once_with()

    async def test_lifespan_starts_and_stops_rate_sync(self) -> None:
        monitor = SimpleNamespace(start=Mock(), stop=AsyncMock())
        rate_sync = SimpleNamespace(start=Mock(), stop=AsyncMock())
        usage_refresh = SimpleNamespace(start=Mock(), stop=AsyncMock())
        refresh = SimpleNamespace(cleanup_stale_jobs=AsyncMock())
        runtime_config = SimpleNamespace(auto_detect_sub2api=AsyncMock())

        with (
            patch("app.main.init_db", AsyncMock()),
            patch("app.main.get_refresh_service", return_value=refresh),
            patch("app.main.get_runtime_config_service", return_value=runtime_config),
            patch("app.main.get_monitor_service", return_value=monitor),
            patch("app.main.get_upstream_rate_sync_service", return_value=rate_sync),
            patch("app.main.get_usage_refresh_service", return_value=usage_refresh),
        ):
            async with lifespan(app):
                rate_sync.start.assert_called_once_with()

        rate_sync.stop.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
