from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import (
    UpstreamAccountConfig,
    UpstreamAccountDailyUsage,
    UpstreamChannel,
    UpstreamChannelDailyUsage,
    UpstreamChannelUsageTotal,
)
from app.schemas import UpstreamUsageHistoryOut
from app.services.upstream_usage_history import (
    channel_identity,
    finalize_yesterday_usage,
    hydrate_yesterday_usage,
    prune_upstream_usage_history,
    should_fetch_yesterday_usage,
    snapshot_today_usage,
    upsert_historical_channel_usage,
    usage_history,
)


class UpstreamUsageHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.session_factory()
        self.channel = UpstreamChannel(
            display_name="Usage channel",
            canonical_base_url="https://usage.example/v1",
            effective_recharge_multiplier=2.0,
            last_known_recharge_multiplier=2.0,
            today_balance_used=2.0,
            today_balance_unit="USD",
            today_balance_status="ok",
        )
        self.config = UpstreamAccountConfig(
            sub2api_account_id=7,
            channel_id=None,
            remote_name="Key account",
            remote_identity_fingerprint="a" * 64,
            upstream_api_key_record_id=101,
            today_upstream_usage_amount=1.0,
            today_upstream_usage_unit="USD",
            today_upstream_usage_status="ok",
            today_upstream_usage_source="upstream_api_key_actual_cost",
        )
        self.db.add_all([self.channel, self.config])
        await self.db.flush()
        self.config.channel_id = self.channel.id
        await self.db.commit()
        self.now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    async def test_snapshot_replaces_current_day_and_updates_lifetime_by_delta(self) -> None:
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 3.0},
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        self.channel.today_balance_used = 4.0
        self.config.today_upstream_usage_amount = 2.0
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 5.0},
            now=self.now + timedelta(minutes=20),
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        daily = (
            await self.db.execute(select(UpstreamChannelDailyUsage))
        ).scalar_one()
        total = await self.db.get(UpstreamChannelUsageTotal, channel_identity(self.channel))
        self.assertEqual(daily.balance_used, 4.0)
        self.assertEqual(daily.balance_used_adjusted, 8.0)
        self.assertEqual(daily.upstream_api_key_usage, 2.0)
        self.assertEqual(daily.income, 5.0)
        self.assertEqual(total.total_balance_used, 4.0)
        self.assertEqual(total.total_balance_used_adjusted, 8.0)
        self.assertEqual(total.total_upstream_api_key_usage, 2.0)
        self.assertEqual(total.total_income, 5.0)

    async def test_finalized_yesterday_is_reused_and_detail_pruning_keeps_total(self) -> None:
        self.channel.yesterday_balance_used = 6.0
        self.channel.yesterday_balance_unit = "USD"
        self.channel.yesterday_balance_status = "ok"
        self.channel.yesterday_balance_checked_at = self.now
        finalized = await finalize_yesterday_usage(
            self.db,
            channel=self.channel,
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()
        self.assertTrue(finalized)
        self.assertFalse(
            await should_fetch_yesterday_usage(
                self.db,
                channel=self.channel,
                now=self.now,
                time_zone="Asia/Shanghai",
            )
        )

        self.channel.yesterday_balance_used = None
        self.channel.yesterday_balance_status = "error"
        self.assertTrue(
            await hydrate_yesterday_usage(
                self.db,
                channel=self.channel,
                now=self.now,
                time_zone="Asia/Shanghai",
            )
        )
        self.assertEqual(self.channel.yesterday_balance_used, 6.0)
        self.assertEqual(self.channel.yesterday_balance_status, "stored")

        await prune_upstream_usage_history(
            self.db,
            retention_days=1,
            now=self.now + timedelta(days=3),
        )
        await self.db.commit()
        self.assertEqual(
            list((await self.db.execute(select(UpstreamChannelDailyUsage))).scalars()),
            [],
        )
        total = await self.db.get(UpstreamChannelUsageTotal, channel_identity(self.channel))
        self.assertEqual(total.total_balance_used, 6.0)
        self.assertEqual(total.total_balance_used_adjusted, 12.0)

    async def test_history_uses_key_cost_when_an_account_filter_is_selected(self) -> None:
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 4.0},
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        history = await usage_history(
            self.db,
            channel=self.channel,
            start_date=self.now.astimezone().date(),
            end_date=self.now.astimezone().date(),
            api_key_account_id=7,
            time_zone="Asia/Shanghai",
        )
        day = history["days"][0]
        output = UpstreamUsageHistoryOut(**history)
        self.assertEqual(day["balance_used"], 2.0)
        self.assertEqual(day["cost"], 1.0)
        self.assertEqual(day["cost_adjusted"], 2.0)
        self.assertEqual(day["income"], 4.0)
        self.assertEqual(day["income_unit"], "CNY")
        self.assertEqual(day["api_key_accounts"][0]["sub2api_account_id"], 7)
        self.assertEqual(output.lifetime_totals.cost_adjusted, 4.0)

    async def test_key_filter_does_not_substitute_channel_cost_when_key_usage_is_unknown(self) -> None:
        self.config.today_upstream_usage_amount = None
        self.config.today_upstream_usage_status = "error"
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 4.0},
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        history = await usage_history(
            self.db,
            channel=self.channel,
            start_date=self.now.date(),
            end_date=self.now.date(),
            api_key_account_id=7,
            time_zone="Asia/Shanghai",
        )

        day = history["days"][0]
        self.assertEqual(day["balance_used"], 2.0)
        self.assertIsNone(day["cost"])
        self.assertIsNone(day["cost_adjusted"])
        self.assertEqual(day["income"], 4.0)

    async def test_finalization_updates_yesterday_account_income_before_locking_day(self) -> None:
        yesterday_now = self.now - timedelta(days=1)
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 3.0},
            now=yesterday_now,
            time_zone="Asia/Shanghai",
        )
        self.channel.yesterday_balance_used = 6.0
        self.channel.yesterday_balance_unit = "USD"
        self.channel.yesterday_balance_status = "ok"
        self.channel.yesterday_balance_checked_at = self.now
        self.config.today_upstream_usage_amount = 2.0
        await finalize_yesterday_usage(
            self.db,
            channel=self.channel,
            income_by_account={7: 5.0},
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        account_row = (
            await self.db.execute(select(UpstreamAccountDailyUsage))
        ).scalar_one()
        daily_row = (
            await self.db.execute(select(UpstreamChannelDailyUsage))
        ).scalar_one()
        total = await self.db.get(UpstreamChannelUsageTotal, channel_identity(self.channel))
        self.assertTrue(account_row.finalized)
        self.assertEqual(account_row.income, 5.0)
        self.assertEqual(daily_row.income, 5.0)
        self.assertEqual(total.total_income, 5.0)

    async def test_channel_identity_prevents_deleted_or_rebound_id_history_from_mixing(self) -> None:
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 3.0},
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        old_identity = channel_identity(self.channel)
        self.channel.canonical_base_url = "https://replacement.example/v1"
        self.channel.today_balance_used = 4.0
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 8.0},
            now=self.now + timedelta(minutes=1),
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        current_identity = channel_identity(self.channel)
        self.assertNotEqual(old_identity, current_identity)
        self.assertEqual(
            len(list((await self.db.execute(select(UpstreamChannelDailyUsage))).scalars())),
            2,
        )
        old_total = await self.db.get(UpstreamChannelUsageTotal, old_identity)
        current_total = await self.db.get(UpstreamChannelUsageTotal, current_identity)
        self.assertEqual(old_total.total_balance_used, 2.0)
        self.assertEqual(current_total.total_balance_used, 4.0)
        history = await usage_history(
            self.db,
            channel=self.channel,
            start_date=self.now.date(),
            end_date=self.now.date(),
            time_zone="Asia/Shanghai",
        )
        self.assertEqual(history["days"][0]["balance_used"], 4.0)
        self.assertEqual(history["days"][0]["income"], None)

    async def test_account_reassignment_keeps_one_daily_owner(self) -> None:
        second_channel = UpstreamChannel(
            display_name="Second channel",
            canonical_base_url="https://second.example/v1",
            effective_recharge_multiplier=1.0,
            today_balance_used=4.0,
            today_balance_unit="USD",
            today_balance_status="ok",
        )
        self.db.add(second_channel)
        await self.db.flush()
        await snapshot_today_usage(
            self.db,
            channel=self.channel,
            configs=[self.config],
            income_by_account={7: 3.0},
            now=self.now,
            time_zone="Asia/Shanghai",
        )
        self.config.channel_id = second_channel.id
        self.config.today_upstream_usage_amount = 5.0
        await snapshot_today_usage(
            self.db,
            channel=second_channel,
            configs=[self.config],
            income_by_account={7: 9.0},
            now=self.now + timedelta(minutes=1),
            time_zone="Asia/Shanghai",
        )
        await self.db.commit()

        account_rows = list((await self.db.execute(select(UpstreamAccountDailyUsage))).scalars())
        self.assertEqual(len(account_rows), 1)
        self.assertEqual(account_rows[0].channel_identity, channel_identity(self.channel))
        original = await usage_history(
            self.db,
            channel=self.channel,
            start_date=self.now.date(),
            end_date=self.now.date(),
            time_zone="Asia/Shanghai",
        )
        reassigned = await usage_history(
            self.db,
            channel=second_channel,
            start_date=self.now.date(),
            end_date=self.now.date(),
            time_zone="Asia/Shanghai",
        )
        self.assertEqual(original["days"][0]["income"], 3.0)
        self.assertIsNone(reassigned["days"][0]["income"])

    async def test_historical_backfill_upsert_updates_lifetime_by_delta(self) -> None:
        usage_date = self.now.date() - timedelta(days=1)
        await upsert_historical_channel_usage(
            self.db,
            channel=self.channel,
            usage_date=usage_date,
            balance_used=4.0,
            recharge_multiplier=2.0,
            observed_at=self.now,
        )
        await upsert_historical_channel_usage(
            self.db,
            channel=self.channel,
            usage_date=usage_date,
            balance_used=6.0,
            recharge_multiplier=2.0,
            observed_at=self.now,
        )
        await self.db.commit()

        total = await self.db.get(UpstreamChannelUsageTotal, channel_identity(self.channel))
        self.assertEqual(total.total_balance_used, 6.0)
        self.assertEqual(total.total_balance_used_adjusted, 12.0)
