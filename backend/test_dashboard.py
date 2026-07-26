import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.dashboard import dashboard_summary
from app.core.database import Base
from app.models import UpstreamChannel, utcnow


class DashboardBalanceWarningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_stored_upstream_wallet_below_threshold_warns_even_when_guard_is_disabled(self) -> None:
        async with self.sessions() as db:
            db.add_all(
                [
                    UpstreamChannel(
                        display_name="Low wallet",
                        canonical_base_url="https://negative.example/api/v1",
                        balance_remaining=4.75,
                        balance_unit="USD",
                        balance_status="ok",
                        balance_source="upstream_wallet",
                        balance_checked_at=utcnow(),
                        balance_guard_state="disabled",
                    ),
                    UpstreamChannel(
                        display_name="Local fallback",
                        canonical_base_url="https://fallback.example/api/v1",
                        balance_remaining=4.0,
                        balance_unit="USD",
                        balance_status="ok",
                        balance_source="local_api_key",
                        balance_guard_state="disabled",
                    ),
                    UpstreamChannel(
                        display_name="Failed stale probe",
                        canonical_base_url="https://stale.example/api/v1",
                        balance_remaining=3.0,
                        balance_unit="USD",
                        balance_status="error",
                        balance_source=None,
                        balance_checked_at=utcnow(),
                        balance_guard_state="unavailable",
                    ),
                ]
            )
            await db.commit()

            runtime = SimpleNamespace(
                get_show_stale_negative_balance_alert=AsyncMock(return_value=True),
                get_upstream_negative_balance_basis=AsyncMock(return_value="wallet"),
                get_upstream_balance_pause_threshold=AsyncMock(return_value=5.0),
            )
            with patch(
                "app.api.dashboard.get_runtime_config_service",
                return_value=runtime,
            ):
                summary = await dashboard_summary({}, db)

        self.assertEqual(summary.low_balance_channel_count, 1)
        warnings = {item["name"]: item for item in summary.low_balance_channels}
        self.assertEqual(warnings["Low wallet"]["balance"], 4.75)
        self.assertEqual(warnings["Low wallet"]["basis"], "wallet")
        self.assertEqual(warnings["Low wallet"]["threshold"], 5.0)
        self.assertNotIn("Failed stale probe", warnings)

    async def test_stale_warning_can_be_hidden_without_hiding_active_guard_alert(self) -> None:
        async with self.sessions() as db:
            db.add_all(
                [
                    UpstreamChannel(
                        display_name="Historical negative",
                        canonical_base_url="https://historical.example/api/v1",
                        balance_remaining=-11,
                        balance_unit="USD",
                        balance_status="error",
                        balance_checked_at=utcnow(),
                        balance_guard_state="unavailable",
                    ),
                    UpstreamChannel(
                        display_name="Active balance guard",
                        canonical_base_url="https://active.example/api/v1",
                        balance_remaining=-7,
                        balance_unit="USD",
                        balance_status="ok",
                        balance_source="upstream_wallet",
                        balance_checked_at=utcnow(),
                        balance_guard_state="insufficient",
                        balance_guard_basis="recharge_adjusted",
                        balance_guard_value=-3.5,
                        balance_guard_checked_at=utcnow(),
                        balance_guard_paused_count=2,
                    ),
                ]
            )
            await db.commit()

            runtime = SimpleNamespace(
                get_show_stale_negative_balance_alert=AsyncMock(return_value=False),
                get_upstream_negative_balance_basis=AsyncMock(return_value="wallet"),
                get_upstream_balance_pause_threshold=AsyncMock(return_value=0.0),
            )
            with patch(
                "app.api.dashboard.get_runtime_config_service",
                return_value=runtime,
            ):
                summary = await dashboard_summary({}, db)

        self.assertEqual(summary.low_balance_channel_count, 1)
        self.assertEqual(summary.low_balance_channels[0]["name"], "Active balance guard")
        self.assertEqual(summary.low_balance_channels[0]["balance"], -3.5)
        self.assertEqual(summary.low_balance_channels[0]["basis"], "recharge_adjusted")
        self.assertEqual(summary.low_balance_channels[0]["unit"], "CNY")
        self.assertEqual(summary.low_balance_channels[0]["paused_accounts"], 2)

    async def test_stale_recharge_adjusted_warning_uses_adjusted_value_and_threshold(self) -> None:
        async with self.sessions() as db:
            db.add_all(
                [
                    UpstreamChannel(
                        display_name="Adjusted low",
                        canonical_base_url="https://adjusted-low.example/api/v1",
                        balance_remaining=4.0,
                        effective_recharge_multiplier=0.5,
                        balance_unit="USD",
                        balance_status="ok",
                        balance_source="upstream_wallet",
                        balance_checked_at=utcnow(),
                        balance_guard_state="disabled",
                    ),
                    UpstreamChannel(
                        display_name="Adjusted healthy",
                        canonical_base_url="https://adjusted-healthy.example/api/v1",
                        balance_remaining=4.0,
                        effective_recharge_multiplier=1.0,
                        balance_unit="USD",
                        balance_status="ok",
                        balance_source="upstream_wallet",
                        balance_checked_at=utcnow(),
                        balance_guard_state="disabled",
                    ),
                ]
            )
            await db.commit()

            runtime = SimpleNamespace(
                get_show_stale_negative_balance_alert=AsyncMock(return_value=True),
                get_upstream_negative_balance_basis=AsyncMock(
                    return_value="recharge_adjusted"
                ),
                get_upstream_balance_pause_threshold=AsyncMock(return_value=3.0),
            )
            with patch(
                "app.api.dashboard.get_runtime_config_service",
                return_value=runtime,
            ):
                summary = await dashboard_summary({}, db)

        self.assertEqual(summary.low_balance_channel_count, 1)
        warning = summary.low_balance_channels[0]
        self.assertEqual(warning["name"], "Adjusted low")
        self.assertEqual(warning["balance"], 2.0)
        self.assertEqual(warning["basis"], "recharge_adjusted")
        self.assertEqual(warning["unit"], "CNY")
        self.assertEqual(warning["threshold"], 3.0)


if __name__ == "__main__":
    unittest.main()
