from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import (
    ApiAccount,
    ApiAccountDailyUsage,
    Upstream,
    UpstreamDailyUsage,
    UpstreamUsageTotal,
)
from app.schemas import UpstreamUsageHistoryOut
from app.services.upstream_usage_history import (
    finalize_cached_yesterday_usage,
    hydrate_yesterday_usage,
    import_sub2api_daily_stats,
    missing_finalized_usage_dates,
    prune_upstream_usage_history,
    snapshot_today_usage,
    upstream_id,
    upsert_historical_upstream_usage,
    usage_history,
)


class UpstreamUsageHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.upstream = Upstream(
            display_name="Usage upstream",
            api_endpoint_url="https://usage.example/v1",
            upstream_recharge_multiplier=2.0,
            last_known_recharge_multiplier=2.0,
            today_upstream_wallet_cost_usd=2.0,
            today_balance_unit="USD",
            today_balance_status="ok",
        )
        self.db.add(self.upstream)
        await self.db.flush()
        self.account = ApiAccount(
            management_account_id=7,
            upstream_id=self.upstream.id,
            remote_name="Key account",
            remote_identity_fingerprint="a" * 64,
            remote_upstream_api_key_id=101,
            upstream_recharge_multiplier=2.0,
            upstream_group_multiplier=9.0,
            today_upstream_wallet_cost_usd=1.0,
            today_upstream_usage_unit="USD",
            today_upstream_usage_status="ok",
            today_upstream_usage_source="upstream_api_key_actual_cost",
        )
        self.db.add(self.account)
        await self.db.commit()
        self.now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    async def test_snapshot_uses_frozen_recharge_multipliers_and_wallet_cost_only(self) -> None:
        await snapshot_today_usage(
            self.db,
            upstream=self.upstream,
            configs=[self.account],
            management_stats_by_account={7: {"cost": 8.0, "user_cost": 10.0}},
            management_recharge_multiplier=0.5,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        account_row = (await self.db.scalars(select(ApiAccountDailyUsage))).one()
        daily_row = (await self.db.scalars(select(UpstreamDailyUsage))).one()
        self.assertEqual(account_row.upstream_wallet_cost_usd, 1.0)
        self.assertEqual(account_row.upstream_recharge_multiplier, 2.0)
        self.assertEqual(account_row.upstream_actual_cost_cny, 2.0)
        self.assertEqual(account_row.management_account_cost_usd, 8.0)
        self.assertEqual(account_row.management_account_cost_cny, 4.0)
        self.assertEqual(account_row.actual_income_cny, 5.0)
        self.assertEqual(daily_row.upstream_actual_cost_cny, 2.0)
        self.assertEqual(daily_row.management_account_cost_cny, 4.0)
        self.assertEqual(daily_row.profit_cny, 1.0)

        self.account.upstream_recharge_multiplier = 0.1
        self.account.upstream_group_multiplier = 100.0
        self.account.today_upstream_wallet_cost_usd = 3.0
        await snapshot_today_usage(
            self.db,
            upstream=self.upstream,
            configs=[self.account],
            management_stats_by_account={7: {"cost": 9.0, "user_cost": 12.0}},
            management_recharge_multiplier=1.0,
            now=self.now + timedelta(minutes=30),
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()
        await self.db.refresh(account_row)
        self.assertEqual(account_row.upstream_recharge_multiplier, 2.0)
        self.assertEqual(account_row.upstream_actual_cost_cny, 6.0)
        self.assertEqual(account_row.management_recharge_multiplier, 0.5)
        self.assertEqual(account_row.management_account_cost_cny, 4.5)
        self.assertEqual(account_row.actual_income_cny, 6.0)

    async def test_sub2api_import_is_idempotent_and_clears_missing_user_charge(self) -> None:
        imported_day = self.now.date() - timedelta(days=1)
        imported = await import_sub2api_daily_stats(
            self.db,
            upstream=self.upstream,
            configs=[self.account],
            stats_by_account={
                7: {imported_day: {"cost": 8.0, "user_cost": 10.0}},
            },
            management_recharge_multiplier=0.5,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()
        self.assertEqual(imported, 1)

        await import_sub2api_daily_stats(
            self.db,
            upstream=self.upstream,
            configs=[self.account],
            stats_by_account={7: {imported_day: {"cost": 9.0}}},
            management_recharge_multiplier=1.0,
            now=self.now + timedelta(minutes=10),
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()
        account_row = (await self.db.scalars(select(ApiAccountDailyUsage))).one()
        daily_row = (await self.db.scalars(select(UpstreamDailyUsage))).one()
        total = await self.db.get(UpstreamUsageTotal, upstream_id(self.upstream))
        self.assertEqual(account_row.management_recharge_multiplier, 0.5)
        self.assertEqual(account_row.management_account_cost_cny, 4.5)
        self.assertIsNone(account_row.management_user_charge_usd)
        self.assertIsNone(account_row.actual_income_cny)
        self.assertIsNone(daily_row.actual_income_cny)
        self.assertEqual(total.total_management_account_cost_cny, 4.5)
        self.assertEqual(total.total_actual_income_cny, 0.0)

    async def test_history_aggregates_every_same_day_source_segment(self) -> None:
        usage_date = self.now.date() - timedelta(days=2)
        self.db.add_all(
            [
                UpstreamDailyUsage(
                    upstream_id=self.upstream.id,
                    upstream_name="Old name",
                    usage_date=usage_date,
                    source_segment=0,
                    upstream_wallet_cost_usd=1.0,
                    upstream_actual_cost_cny=0.1,
                    upstream_recharge_multiplier=0.1,
                    finalized=True,
                ),
                UpstreamDailyUsage(
                    upstream_id=self.upstream.id,
                    upstream_name="New name",
                    usage_date=usage_date,
                    source_segment=1,
                    upstream_wallet_cost_usd=2.0,
                    upstream_actual_cost_cny=0.2,
                    upstream_recharge_multiplier=0.1,
                    finalized=True,
                ),
                ApiAccountDailyUsage(
                    upstream_id=self.upstream.id,
                    management_account_id=7,
                    api_account_id=self.account.id,
                    usage_date=usage_date,
                    source_segment=0,
                    upstream_wallet_cost_usd=1.0,
                    upstream_actual_cost_cny=0.1,
                    upstream_recharge_multiplier=0.1,
                    management_account_cost_usd=2.0,
                    management_account_cost_cny=0.4,
                    management_user_charge_usd=3.0,
                    management_recharge_multiplier=0.2,
                    actual_income_cny=0.6,
                    finalized=True,
                ),
                ApiAccountDailyUsage(
                    upstream_id=self.upstream.id,
                    management_account_id=7,
                    api_account_id=self.account.id,
                    usage_date=usage_date,
                    source_segment=1,
                    upstream_wallet_cost_usd=2.0,
                    upstream_actual_cost_cny=0.2,
                    upstream_recharge_multiplier=0.1,
                    management_account_cost_usd=4.0,
                    management_account_cost_cny=0.8,
                    management_user_charge_usd=5.0,
                    management_recharge_multiplier=0.2,
                    actual_income_cny=1.0,
                    finalized=True,
                ),
            ]
        )
        await self.db.commit()

        history = await usage_history(
            self.db,
            upstream=self.upstream,
            start_date=usage_date,
            end_date=usage_date,
            time_zone="Asia/Shanghai",
        )
        day = history["days"][0]
        self.assertEqual(day["upstream_wallet_cost_usd"], 3.0)
        self.assertAlmostEqual(day["upstream_actual_cost_cny"], 0.3)
        self.assertAlmostEqual(day["management_account_cost_cny"], 1.2)
        self.assertEqual(day["actual_income_cny"], 1.6)
        self.assertAlmostEqual(day["profit_cny"], 0.4)
        self.assertEqual(len(day["api_accounts"]), 1)
        self.assertEqual(day["api_accounts"][0]["upstream_wallet_cost_usd"], 3.0)

    async def test_history_filter_uses_only_selected_api_account(self) -> None:
        second = ApiAccount(
            management_account_id=8,
            upstream_id=self.upstream.id,
            remote_name="Second account",
        )
        self.db.add(second)
        await self.db.flush()
        second.today_upstream_wallet_cost_usd = 4.0
        second.today_upstream_usage_unit = "USD"
        second.today_upstream_usage_status = "ok"
        second.upstream_recharge_multiplier = 0.1
        await snapshot_today_usage(
            self.db,
            upstream=self.upstream,
            configs=[self.account, second],
            management_stats_by_account={
                7: {"cost": 2.0, "user_cost": 3.0},
                8: {"cost": 4.0, "user_cost": 5.0},
            },
            management_recharge_multiplier=1.0,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        history = await usage_history(
            self.db,
            upstream=self.upstream,
            start_date=self.now.date(),
            end_date=self.now.date(),
            management_account_id=8,
            time_zone="Asia/Shanghai",
        )
        day = history["days"][0]
        self.assertEqual(day["upstream_wallet_cost_usd"], 4.0)
        self.assertEqual(day["upstream_actual_cost_cny"], 0.4)
        self.assertEqual(day["management_account_cost_cny"], 4.0)
        self.assertEqual(day["actual_income_cny"], 5.0)
        self.assertEqual([item["management_account_id"] for item in day["api_accounts"]], [8])

    async def test_upstream_url_change_keeps_uuid_and_history(self) -> None:
        await snapshot_today_usage(
            self.db,
            upstream=self.upstream,
            configs=[self.account],
            management_stats_by_account={7: {"cost": 2.0, "user_cost": 3.0}},
            management_recharge_multiplier=1.0,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        original_id = self.upstream.id
        self.upstream.api_endpoint_url = "https://replacement.example/v1"
        self.upstream.display_name = "Renamed upstream"
        await self.db.commit()

        history = await usage_history(
            self.db,
            upstream=self.upstream,
            start_date=self.now.date(),
            end_date=self.now.date(),
            time_zone="Asia/Shanghai",
        )
        self.assertEqual(self.upstream.id, original_id)
        self.assertEqual(history["upstream_id"], original_id)
        self.assertEqual(history["days"][0]["actual_income_cny"], 3.0)

    async def test_account_reassignment_keeps_history_on_original_upstream(self) -> None:
        await snapshot_today_usage(
            self.db,
            upstream=self.upstream,
            configs=[self.account],
            management_stats_by_account={7: {"cost": 2.0, "user_cost": 3.0}},
            management_recharge_multiplier=1.0,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        second_upstream = Upstream(
            display_name="Second upstream",
            api_endpoint_url="https://second.example",
            upstream_recharge_multiplier=1.0,
        )
        self.db.add(second_upstream)
        await self.db.flush()
        self.account.upstream_id = second_upstream.id
        self.account.today_upstream_wallet_cost_usd = 2.0
        self.account.upstream_recharge_multiplier = 1.0
        tomorrow = self.now + timedelta(days=1)
        await snapshot_today_usage(
            self.db,
            upstream=second_upstream,
            configs=[self.account],
            management_stats_by_account={7: {"cost": 4.0, "user_cost": 5.0}},
            management_recharge_multiplier=1.0,
            now=tomorrow,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        original_history = await usage_history(
            self.db,
            upstream=self.upstream,
            start_date=self.now.date(),
            end_date=self.now.date(),
        )
        reassigned_history = await usage_history(
            self.db,
            upstream=second_upstream,
            start_date=tomorrow.date(),
            end_date=tomorrow.date(),
        )
        self.assertEqual(original_history["days"][0]["actual_income_cny"], 3.0)
        self.assertEqual(reassigned_history["days"][0]["actual_income_cny"], 5.0)

    async def test_finalized_yesterday_hydrates_and_pruning_keeps_lifetime_total(self) -> None:
        yesterday = self.now.date() - timedelta(days=1)
        await upsert_historical_upstream_usage(
            self.db,
            upstream=self.upstream,
            usage_date=yesterday,
            upstream_wallet_cost_usd=6.0,
            upstream_recharge_multiplier=0.5,
            observed_at=self.now - timedelta(days=1),
        )
        await self.db.commit()
        self.upstream.yesterday_upstream_wallet_cost_usd = None
        hydrated = await hydrate_yesterday_usage(
            self.db,
            upstream=self.upstream,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        self.assertTrue(hydrated)
        self.assertEqual(self.upstream.yesterday_upstream_wallet_cost_usd, 6.0)
        self.assertEqual(self.upstream.yesterday_balance_status, "stored")

        await prune_upstream_usage_history(
            self.db,
            retention_days=1,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()
        self.assertEqual((await self.db.scalars(select(UpstreamDailyUsage))).all(), [])
        total = await self.db.get(UpstreamUsageTotal, self.upstream.id)
        self.assertEqual(total.total_upstream_wallet_cost_usd, 6.0)
        self.assertEqual(total.total_upstream_actual_cost_cny, 3.0)

    async def test_missing_dates_and_cached_finalization_consider_all_segments(self) -> None:
        yesterday = self.now.date() - timedelta(days=1)
        self.db.add_all(
            [
                UpstreamDailyUsage(
                    upstream_id=self.upstream.id,
                    usage_date=yesterday,
                    source_segment=0,
                    upstream_wallet_cost_usd=1.0,
                    finalized=False,
                ),
                UpstreamDailyUsage(
                    upstream_id=self.upstream.id,
                    usage_date=yesterday,
                    source_segment=1,
                    upstream_wallet_cost_usd=2.0,
                    finalized=False,
                ),
            ]
        )
        await self.db.commit()
        missing = await missing_finalized_usage_dates(
            self.db,
            upstream=self.upstream,
            start_date=yesterday,
            end_date=self.now.date(),
        )
        self.assertEqual(missing, [self.now.date()])
        finalized = await finalize_cached_yesterday_usage(
            self.db,
            upstream=self.upstream,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        self.assertTrue(finalized)
        self.assertEqual(self.upstream.yesterday_upstream_wallet_cost_usd, 3.0)

    async def test_historical_upsert_is_idempotent_and_preserves_first_multiplier(self) -> None:
        usage_date = self.now.date() - timedelta(days=3)
        await upsert_historical_upstream_usage(
            self.db,
            upstream=self.upstream,
            usage_date=usage_date,
            upstream_wallet_cost_usd=2.0,
            upstream_recharge_multiplier=0.1,
        )
        await upsert_historical_upstream_usage(
            self.db,
            upstream=self.upstream,
            usage_date=usage_date,
            upstream_wallet_cost_usd=3.0,
            upstream_recharge_multiplier=1.0,
        )
        await self.db.commit()
        rows = (await self.db.scalars(select(UpstreamDailyUsage))).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].upstream_recharge_multiplier, 0.1)
        self.assertAlmostEqual(rows[0].upstream_actual_cost_cny, 0.3)
        total = await self.db.get(UpstreamUsageTotal, self.upstream.id)
        self.assertEqual(total.total_upstream_wallet_cost_usd, 3.0)
        self.assertAlmostEqual(total.total_upstream_actual_cost_cny, 0.3)

    async def test_output_contract_contains_only_new_financial_fields(self) -> None:
        await snapshot_today_usage(
            self.db,
            upstream=self.upstream,
            configs=[self.account],
            management_stats_by_account={7: {"cost": 2.0, "user_cost": 3.0}},
            management_recharge_multiplier=1.0,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        history = await usage_history(
            self.db,
            upstream=self.upstream,
            start_date=self.now.date(),
            end_date=self.now.date(),
        )
        payload = UpstreamUsageHistoryOut(**history).model_dump()
        serialized = str(payload)
        self.assertIn("upstream_actual_cost_cny", payload["days"][0])
        self.assertIn("management_account_cost_cny", payload["days"][0])
        self.assertIn("actual_income_cny", payload["days"][0])
        for old_name in (
            "channel_id",
            "sub2api_cost",
            "local_recharge_multiplier",
            "income_actual_cost",
            "cost_adjusted",
        ):
            self.assertNotIn(old_name, serialized)


if __name__ == "__main__":
    unittest.main()
