import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.models import (
    AppSetting,
    AccountSchedulingChangeLog,
    UpstreamAccountDataArchive,
    UpstreamChannelChangeEvent,
    utcnow,
)
from app.services.change_logs import (
    change_log_unread_counts,
    delete_expired_change_logs,
    list_account_scheduling_changes,
    list_upstream_channel_changes,
    mark_account_scheduling_changes_read,
    mark_upstream_changes_read,
    record_upstream_channel_changes,
)


class ChangeLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.sessions()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await self.engine.dispose()

    async def test_channel_diff_records_multiplier_name_and_group_existence_changes(self) -> None:
        observed_at = datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc)
        changes = record_upstream_channel_changes(
            self.db,
            channel_id=9,
            channel_name="Example",
            previous_recharge_multiplier=1.0,
            current_recharge_multiplier=None,
            previous_groups=[
                {"id": "kept", "name": "Old name", "multiplier": 1.0},
                {"id": "removed", "name": "Removed", "multiplier": 2.0},
            ],
            current_groups=[
                {"id": "kept", "name": "Renamed", "multiplier": None},
                {"id": "added", "name": "Added", "multiplier": 3.0},
            ],
            observed_at=observed_at,
        )
        await self.db.commit()

        self.assertEqual(
            [change["change_type"] for change in changes],
            ["added", "renamed", "multiplier", "removed"],
        )
        removed_change = next(
            change for change in changes if change["change_type"] == "removed"
        )
        self.assertEqual(removed_change["old_status"], "available")
        self.assertEqual(removed_change["new_status"], "removed")
        renamed_change = next(
            change for change in changes if change["change_type"] == "renamed"
        )
        self.assertEqual(
            renamed_change["details"],
            {
                "old_recharge_multiplier": 1.0,
                "old_name": "Old name",
                "new_name": "Renamed",
            },
        )

        events = list(
            (
                await self.db.scalars(
                    select(UpstreamChannelChangeEvent).order_by(
                        UpstreamChannelChangeEvent.id
                    )
                )
            ).all()
        )
        self.assertEqual(
            [event.event_type for event in events],
            [
                "group_added",
                "group_name_changed",
                "group_multiplier_changed",
                "group_removed",
            ],
        )
        kept = next(
            event
            for event in events
            if event.group_id == "kept"
            and event.event_type == "group_multiplier_changed"
        )
        self.assertEqual(kept.old_value, 1.0)
        self.assertIsNone(kept.new_value)
        self.assertEqual(kept.group_name, "Renamed")
        renamed = next(
            event for event in events if event.event_type == "group_name_changed"
        )
        self.assertEqual(renamed.details["old_name"], "Old name")
        self.assertEqual(renamed.details["new_name"], "Renamed")
        self.assertEqual(renamed.old_value, 1.0)
        self.assertIsNone(renamed.new_value)
        self.assertEqual(renamed.details["old_recharge_multiplier"], 1.0)
        for event in events:
            created_at = event.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            self.assertEqual(created_at, observed_at)

    async def test_channel_multiplier_baseline_does_not_create_a_change_event(self) -> None:
        changes = record_upstream_channel_changes(
            self.db,
            channel_id=9,
            channel_name="Example",
            previous_recharge_multiplier=None,
            current_recharge_multiplier=1.0,
            previous_groups=[],
            current_groups=[],
            observed_at=datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc),
        )
        await self.db.commit()

        self.assertEqual(changes, [])
        events = list((await self.db.scalars(select(UpstreamChannelChangeEvent))).all())
        self.assertEqual(events, [])

    async def test_group_events_preserve_recharge_multiplier_snapshots(self) -> None:
        record_upstream_channel_changes(
            self.db,
            channel_id=9,
            channel_name="Example",
            previous_recharge_multiplier=0.25,
            current_recharge_multiplier=0.25,
            previous_groups=[{"id": "changed", "name": "Changed", "multiplier": 2.0}],
            current_groups=[
                {"id": "changed", "name": "Changed", "multiplier": 3.0},
                {"id": "added", "name": "Added", "multiplier": 4.0},
            ],
            observed_at=datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc),
        )
        await self.db.commit()

        events = list((await self.db.scalars(select(UpstreamChannelChangeEvent))).all())
        self.assertEqual(
            {event.event_type for event in events},
            {"group_added", "group_multiplier_changed"},
        )
        for event in events:
            self.assertEqual(event.details, {
                "old_recharge_multiplier": 0.25,
                "new_recharge_multiplier": 0.25,
            })

    async def test_identity_reset_archives_follow_the_configured_retention(self) -> None:
        self.db.add_all(
            [
                UpstreamAccountDataArchive(
                    sub2api_account_id=7,
                    account_name="old",
                    reason="remote_identity_mismatch",
                    snapshot={"upstream": {"today_usage_amount": 1.25}},
                    created_at=utcnow() - timedelta(days=91),
                ),
                UpstreamAccountDataArchive(
                    sub2api_account_id=8,
                    account_name="recent",
                    reason="channel_identity_changed",
                    snapshot={"upstream": {"today_usage_amount": 2.5}},
                    created_at=utcnow() - timedelta(days=89),
                ),
            ]
        )
        await self.db.commit()

        await delete_expired_change_logs(self.db, retention_days=90)
        await self.db.commit()

        archives = list(
            (await self.db.scalars(select(UpstreamAccountDataArchive))).all()
        )
        self.assertEqual([item.sub2api_account_id for item in archives], [8])

    async def test_unread_cursors_are_independent_monotonic_and_cannot_hide_future_rows(self) -> None:
        self.db.add_all(
            [
                UpstreamChannelChangeEvent(
                    channel_id=1,
                    channel_name="Channel",
                    event_type="group_added",
                    group_id=f"group-{index}",
                )
                for index in range(3)
            ]
            + [
                AccountSchedulingChangeLog(
                    sub2api_account_id=10 + index,
                    event_type="paused",
                    reason="upstream_balance_negative",
                    status="success",
                )
                for index in range(2)
            ]
        )
        await self.db.commit()

        page, cursor, unread = await list_upstream_channel_changes(
            self.db,
            retention_days=90,
            limit=2,
        )
        self.assertEqual([event.id for event in page], [3, 2])
        self.assertEqual(cursor, 0)
        self.assertEqual(unread, 3)
        scheduling_page, scheduling_cursor, scheduling_unread = (
            await list_account_scheduling_changes(
                self.db,
                retention_days=90,
                limit=10,
            )
        )
        self.assertEqual(len(scheduling_page), 2)
        self.assertEqual(scheduling_cursor, 0)
        self.assertEqual(scheduling_unread, 2)

        self.assertEqual(await mark_upstream_changes_read(self.db, 999_999), 3)
        self.assertEqual(await change_log_unread_counts(self.db), (3, 0, 2))

        self.db.add(
            UpstreamChannelChangeEvent(
                channel_id=1,
                channel_name="Channel",
                event_type="group_removed",
                group_id="future-group",
            )
        )
        await self.db.commit()
        self.assertEqual(await change_log_unread_counts(self.db), (4, 0, 2))

        self.assertEqual(await mark_upstream_changes_read(self.db, 999_999), 4)
        await self.db.execute(delete(UpstreamChannelChangeEvent))
        await self.db.commit()
        replacement = UpstreamChannelChangeEvent(
            channel_id=1,
            channel_name="Channel",
            event_type="group_added",
            group_id="replacement-after-prune",
        )
        self.db.add(replacement)
        await self.db.commit()
        self.assertLess(replacement.id, 4)
        self.assertEqual(await change_log_unread_counts(self.db), (1, 0, 2))

        first_scheduling_id = min(event.id for event in scheduling_page)
        self.assertEqual(
            await mark_account_scheduling_changes_read(self.db, first_scheduling_id),
            first_scheduling_id,
        )
        self.assertEqual(await change_log_unread_counts(self.db), (1, 0, 1))
        self.assertEqual(
            await mark_account_scheduling_changes_read(self.db, 999_999),
            max(event.id for event in scheduling_page),
        )
        self.assertEqual(await change_log_unread_counts(self.db), (1, 0, 0))

    async def test_imported_history_is_visible_in_time_order_but_not_unread(self) -> None:
        self.db.add_all(
            [
                UpstreamChannelChangeEvent(
                    channel_id=1,
                    event_type="account_rate_changed",
                    old_value=1.0,
                    new_value=1.5,
                    legacy_imported=True,
                    created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                ),
                UpstreamChannelChangeEvent(
                    channel_id=1,
                    event_type="account_rate_changed",
                    old_value=1.5,
                    new_value=2.0,
                    created_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
                ),
            ]
        )
        await self.db.commit()

        page, cursor, unread = await list_upstream_channel_changes(
            self.db,
            retention_days=90,
            limit=1,
        )
        self.assertFalse(page[0].legacy_imported)
        self.assertEqual((cursor, unread), (0, 1))
        older, _, older_unread = await list_upstream_channel_changes(
            self.db,
            retention_days=90,
            limit=1,
            before_id=page[0].id,
        )
        self.assertTrue(older[0].legacy_imported)
        self.assertEqual(older_unread, 1)

    async def test_read_paths_do_not_prune_history_or_repair_persisted_cursors(self) -> None:
        old_event = UpstreamChannelChangeEvent(
            channel_id=1,
            event_type="group_added",
            group_id="old-group",
            created_at=utcnow() - timedelta(days=60),
        )
        self.db.add_all([
            old_event,
            AppSetting(key="upstream_change_event_last_read_id", value="999"),
        ])
        await self.db.commit()

        page, cursor, unread = await list_upstream_channel_changes(
            self.db,
            retention_days=1,
            limit=10,
        )
        self.assertEqual([event.id for event in page], [old_event.id])
        self.assertEqual((cursor, unread), (0, 1))
        self.assertIsNotNone(await self.db.get(UpstreamChannelChangeEvent, old_event.id))
        cursor_setting = await self.db.get(AppSetting, "upstream_change_event_last_read_id")
        self.assertEqual(cursor_setting.value, "999")

        self.assertEqual(
            await change_log_unread_counts(self.db, retention_days=1),
            (1, 0, 0),
        )
        self.assertIsNotNone(await self.db.get(UpstreamChannelChangeEvent, old_event.id))
        self.assertEqual(cursor_setting.value, "999")


if __name__ == "__main__":
    unittest.main()
