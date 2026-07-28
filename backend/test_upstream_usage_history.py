from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import (
    UpstreamAccountConfig,
    UpstreamChannel,
    UpstreamChannelDailyUsage,
    UpstreamChannelUsageTotal,
)
from app.schemas import UpstreamUsageHistoryOut
from app.services.upstream_usage_history import (
    finalize_yesterday_usage,
    hydrate_yesterday_usage,
    prune_upstream_usage_history,
    should_fetch_yesterday_usage,
    snapshot_today_usage,
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
        total = await self.db.get(UpstreamChannelUsageTotal, self.channel.id)
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
                channel_id=self.channel.id,
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
        total = await self.db.get(UpstreamChannelUsageTotal, self.channel.id)
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
