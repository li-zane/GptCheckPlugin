import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base, _migrate_notification_outbox
from app.models import NotificationOutbox
from app.services.notification_dispatcher import NotificationDispatcher
from app.services.notification_transports import (
    DiscordBotTransport,
    NotificationEnvelope,
    NotificationTransport,
    NotificationTransportError,
)
from app.services.notifications import (
    NotificationService,
    enqueue_account_state_changed,
    enqueue_upstream_token_invalid,
    enqueue_upstream_group_changed,
    enqueue_upstream_group_multiplier_changed,
)
from app.services.runtime_config import RuntimeConfigService


def _notification_config(**overrides):
    values = {
        "enabled": True,
        "oauth_account_disabled_enabled": True,
        "account_disabled_enabled": True,
        "account_enabled_enabled": True,
        "api_key_rate_changed_enabled": True,
        "upstream_balance_low_enabled": True,
        "upstream_token_invalid_enabled": False,
        "discord_bot_token": "bot-secret",
        "discord_channel_id": "123456",
    }
    values.update(overrides)
    return values


class _RuntimeConfig:
    def __init__(self, config=None) -> None:
        self.config = config or _notification_config()

    async def get_notification_config(self):
        return self.config


class _RecordingTransport(NotificationTransport):
    def __init__(self, error: NotificationTransportError | None = None) -> None:
        self.error = error
        self.notifications: list[NotificationEnvelope] = []

    async def send(self, notification: NotificationEnvelope) -> None:
        self.notifications.append(notification)
        if self.error is not None:
            raise self.error


class _BlockingTransport(NotificationTransport):
    def __init__(self) -> None:
        self.notifications: list[NotificationEnvelope] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send(self, notification: NotificationEnvelope) -> None:
        self.notifications.append(notification)
        self.started.set()
        await self.release.wait()


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _enqueue(self, event_type="oauth_account_disabled", dedupe_key="event:1"):
        async with self.sessions() as db:
            notification = await NotificationService(db).enqueue(
                event_type,
                dedupe_key,
                "Account disabled",
                "example@example.com was disabled.",
                {"account_id": "1"},
            )
            await db.commit()
            return notification.id

    async def test_enqueue_is_transactional_and_deduplicates(self) -> None:
        async with self.sessions() as db:
            service = NotificationService(db)
            first = await service.enqueue("test", "same-key", "Title", "Message")
            second = await service.enqueue("test", "same-key", "Ignored", "Ignored")
            self.assertEqual(first.id, second.id)
            await db.commit()

        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(NotificationOutbox))
            row = await db.scalar(select(NotificationOutbox))
        self.assertEqual(count, 1)
        self.assertEqual(row.title, "Title")

        async with self.sessions() as db:
            await NotificationService(db).enqueue("test", "rolled-back", "Title", "Message")
            await db.rollback()
        async with self.sessions() as db:
            rolled_back = await db.scalar(
                select(NotificationOutbox).where(NotificationOutbox.dedupe_key == "rolled-back")
            )
        self.assertIsNone(rolled_back)

    async def test_account_state_events_include_type_identity_and_direction(self) -> None:
        runtime = _RuntimeConfig()
        observed_at = datetime(2026, 7, 21, 8, 30, tzinfo=timezone.utc)
        async with self.sessions() as db:
            disabled = await enqueue_account_state_changed(
                db,
                enabled=False,
                account_type="api_key",
                account_id=7,
                account_name="Upstream Seven",
                upstream_id=3,
                upstream_name="奶酪",
                reason="upstream_balance_negative",
                reason_details={
                    "balance": 4.99,
                    "threshold": 5.0,
                    "basis": "wallet",
                    "unit": "USD",
                    "ignored": "must-not-leak",
                },
                observed_at=observed_at,
                runtime_config=runtime,
            )
            enabled = await enqueue_account_state_changed(
                db,
                enabled=True,
                account_type="api_key",
                account_id=9,
                account_name="Recovered Nine",
                reason="All automatic pause conditions cleared.",
                reason_details={
                    "previous_pause_reasons": [
                        "upstream_balance_negative",
                        "upstream_monitor_unavailable",
                        "upstream_balance_negative",
                        "",
                    ],
                    "ignored": "must-not-leak",
                },
                observed_at=observed_at + timedelta(seconds=1),
                runtime_config=runtime,
            )
            await db.commit()

        self.assertEqual(disabled.event_type, "account_disabled")
        self.assertEqual(disabled.details["account_type"], "api_key")
        self.assertEqual(disabled.details["account_id"], 7)
        self.assertEqual(disabled.details["upstream_name"], "奶酪")
        self.assertEqual(disabled.details["balance"], 4.99)
        self.assertEqual(disabled.details["threshold"], 5.0)
        self.assertEqual(disabled.details["basis"], "wallet")
        self.assertNotIn("ignored", disabled.details)
        self.assertFalse(disabled.details["enabled"])
        self.assertEqual(enabled.event_type, "account_enabled")
        self.assertEqual(
            enabled.details["previous_pause_reasons"],
            ["upstream_balance_negative", "upstream_monitor_unavailable"],
        )
        self.assertNotIn("ignored", enabled.details)
        self.assertTrue(enabled.details["enabled"])

    async def test_group_multiplier_event_deduplicates_accounts_in_the_same_group(self) -> None:
        observed_at = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)
        async with self.sessions() as db:
            first = await enqueue_upstream_group_multiplier_changed(
                db,
                upstream_id=3,
                upstream_name="Upstream",
                group_id="vip",
                group_name="VIP",
                old_multiplier=1.0,
                new_multiplier=1.5,
                observed_at=observed_at,
                runtime_config=_RuntimeConfig(),
            )
            second = await enqueue_upstream_group_multiplier_changed(
                db,
                upstream_id=3,
                upstream_name="Upstream",
                group_id="vip",
                group_name="VIP",
                old_multiplier=1.0,
                new_multiplier=1.5,
                observed_at=observed_at,
                runtime_config=_RuntimeConfig(),
            )
            await db.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.event_type, "upstream_group_multiplier_changed")
        self.assertEqual(first.details["old_multiplier"], 1.0)
        self.assertEqual(first.details["new_multiplier"], 1.5)

    async def test_group_change_switch_is_independent_from_api_key_rate_switch(self) -> None:
        observed_at = datetime(2026, 7, 21, 9, 10, tzinfo=timezone.utc)
        async with self.sessions() as db:
            disabled = await enqueue_upstream_group_changed(
                db,
                upstream_id=3,
                upstream_name="Upstream",
                group_id="vip",
                group_name="VIP",
                change_type="added",
                new_status="available",
                observed_at=observed_at,
                runtime_config=_RuntimeConfig(
                    _notification_config(
                        api_key_rate_changed_enabled=True,
                        upstream_group_changed_enabled=False,
                    )
                ),
            )
            enabled = await enqueue_upstream_group_changed(
                db,
                upstream_id=3,
                upstream_name="Upstream",
                group_id="vip",
                group_name="VIP",
                change_type="added",
                new_status="available",
                observed_at=observed_at,
                runtime_config=_RuntimeConfig(
                    _notification_config(
                        api_key_rate_changed_enabled=False,
                        upstream_group_changed_enabled=True,
                    )
                ),
            )
            await db.commit()

        self.assertIsNone(disabled)
        self.assertIsNotNone(enabled)
        self.assertEqual(enabled.event_type, "upstream_group_changed")

    async def test_group_multiplier_switch_follows_rate_switch(self) -> None:
        observed_at = datetime(2026, 7, 21, 9, 20, tzinfo=timezone.utc)
        async with self.sessions() as db:
            disabled = await enqueue_upstream_group_multiplier_changed(
                db,
                upstream_id=3,
                upstream_name="Upstream",
                group_id="vip",
                group_name="VIP",
                old_multiplier=1.0,
                new_multiplier=1.5,
                observed_at=observed_at,
                runtime_config=_RuntimeConfig(
                    _notification_config(
                        api_key_rate_changed_enabled=False,
                        upstream_group_changed_enabled=True,
                    )
                ),
            )
            enabled = await enqueue_upstream_group_multiplier_changed(
                db,
                upstream_id=3,
                upstream_name="Upstream",
                group_id="vip",
                group_name="VIP",
                old_multiplier=1.0,
                new_multiplier=1.5,
                observed_at=observed_at + timedelta(seconds=1),
                runtime_config=_RuntimeConfig(
                    _notification_config(
                        api_key_rate_changed_enabled=True,
                        upstream_group_changed_enabled=False,
                    )
                ),
            )
            await db.commit()

        self.assertIsNone(disabled)
        self.assertIsNotNone(enabled)
        self.assertEqual(enabled.event_type, "upstream_group_multiplier_changed")

    async def test_upstream_token_invalid_event_deduplicates_by_credential(self) -> None:
        observed_at = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)
        runtime = RuntimeConfigService(
            SimpleNamespace(
                discord_bot_notifications_enabled=False,
                discord_bot_token="",
                discord_bot_channel_id="",
                notify_upstream_token_invalid=False,
            )
        )

        async def load_notification_values() -> dict[str, str]:
            return {
                "discord_bot_notifications_enabled": "true",
                "notify_upstream_token_invalid": "true",
            }

        runtime._load_values = load_notification_values
        config = await runtime.get_notification_config()
        self.assertTrue(config["upstream_token_invalid_enabled"])

        async with self.sessions() as db:
            first = await enqueue_upstream_token_invalid(
                db,
                upstream_id=7,
                upstream_name="哈基米",
                credential_fingerprint="credential-a",
                observed_at=observed_at,
                runtime_config=runtime,
            )
            second = await enqueue_upstream_token_invalid(
                db,
                upstream_id=7,
                upstream_name="哈基米",
                credential_fingerprint="credential-a",
                observed_at=observed_at + timedelta(minutes=5),
                runtime_config=runtime,
            )
            await db.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.event_type, "upstream_token_invalid")
        self.assertEqual(first.details["upstream_id"], 7)

    async def test_direct_test_notification_uses_configured_transport(self) -> None:
        transport = _RecordingTransport()
        dispatcher = NotificationDispatcher(
            _RuntimeConfig(),
            transport,
            session_factory=self.sessions,
        )

        await dispatcher.send_test_notification()

        self.assertEqual(len(transport.notifications), 1)
        self.assertEqual(transport.notifications[0].event_type, "notification_test")
        self.assertEqual(transport.notifications[0].title, "sub2api 通知测试")

    async def test_direct_test_notification_requires_global_enable(self) -> None:
        dispatcher = NotificationDispatcher(
            _RuntimeConfig(_notification_config(enabled=False)),
            _RecordingTransport(),
            session_factory=self.sessions,
        )

        with self.assertRaises(NotificationTransportError) as raised:
            await dispatcher.send_test_notification()

        self.assertEqual(raised.exception.code, "notifications_disabled")

    async def test_legacy_outbox_migration_adds_claim_columns_idempotently(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE notification_outbox ("
                        "id INTEGER NOT NULL PRIMARY KEY, status VARCHAR(32) NOT NULL)"
                    )
                )
                await _migrate_notification_outbox(connection)
                await _migrate_notification_outbox(connection)
                columns = {
                    str(row[1])
                    for row in (
                        await connection.execute(
                            text("PRAGMA table_info(notification_outbox)")
                        )
                    ).fetchall()
                }
                indexes = {
                    str(row[1])
                    for row in (
                        await connection.execute(
                            text("PRAGMA index_list(notification_outbox)")
                        )
                    ).fetchall()
                }
        finally:
            await engine.dispose()

        self.assertTrue(
            {"claim_token", "claimed_at", "claim_expires_at"}.issubset(columns)
        )
        self.assertIn("ix_notification_outbox_claim_token", indexes)
        self.assertIn("ix_notification_outbox_claim_expires_at", indexes)

    async def test_dispatcher_honors_global_and_event_switches(self) -> None:
        notification_id = await self._enqueue()
        transport = _RecordingTransport()
        runtime = _RuntimeConfig(_notification_config(oauth_account_disabled_enabled=False))
        dispatcher = NotificationDispatcher(
            runtime,
            transport,
            session_factory=self.sessions,
            base_retry_seconds=0.01,
        )

        self.assertEqual(await dispatcher.run_once(), 0)
        self.assertEqual(transport.notifications, [])
        async with self.sessions() as db:
            row = await db.get(NotificationOutbox, notification_id)
            self.assertEqual(row.status, "canceled")

        runtime.config["oauth_account_disabled_enabled"] = True
        notification_id = await self._enqueue(dedupe_key="event:enabled")
        self.assertEqual(await dispatcher.run_once(), 1)
        async with self.sessions() as db:
            row = await db.get(NotificationOutbox, notification_id)
            self.assertEqual(row.status, "sent")
            self.assertEqual(row.attempts, 1)
            self.assertIsNotNone(row.sent_at)

        await self._enqueue(dedupe_key="event:global-off")
        runtime.config["enabled"] = False
        self.assertEqual(await dispatcher.run_once(), 0)
        self.assertEqual(len(transport.notifications), 1)
        async with self.sessions() as db:
            global_off = await db.scalar(
                select(NotificationOutbox).where(
                    NotificationOutbox.dedupe_key == "event:global-off"
                )
            )
            self.assertEqual(global_off.status, "canceled")

    async def test_event_switch_prevents_new_outbox_backlog(self) -> None:
        runtime = _RuntimeConfig(
            _notification_config(oauth_account_disabled_enabled=False)
        )
        async with self.sessions() as db:
            result = await NotificationService(db, runtime).enqueue_if_enabled(
                "oauth_account_disabled",
                "disabled:event",
                "Title",
                "Message",
            )
            await db.commit()
            self.assertIsNone(result)

    async def test_unconfigured_event_type_is_disabled_by_default(self) -> None:
        async with self.sessions() as db:
            result = await NotificationService(db, _RuntimeConfig()).enqueue_if_enabled(
                "future_unconfigured_event",
                "future:1",
                "Future event",
                "This event has no settings switch yet.",
            )
        self.assertIsNone(result)
        async with self.sessions() as db:
            count = await db.scalar(select(func.count()).select_from(NotificationOutbox))
        self.assertEqual(count, 0)

    async def test_fresh_observation_reactivates_canceled_episode_after_switch_enabled(self) -> None:
        runtime = _RuntimeConfig()
        async with self.sessions() as db:
            service = NotificationService(db, runtime)
            notification = await service.enqueue_if_enabled(
                "upstream_balance_low",
                "balance:episode",
                "Low balance",
                "First observation",
            )
            await db.commit()
            notification_id = notification.id

        runtime.config["upstream_balance_low_enabled"] = False
        dispatcher = NotificationDispatcher(
            runtime,
            _RecordingTransport(),
            session_factory=self.sessions,
        )
        self.assertEqual(await dispatcher.run_once(), 0)

        runtime.config["upstream_balance_low_enabled"] = True
        async with self.sessions() as db:
            reactivated = await NotificationService(db, runtime).enqueue_if_enabled(
                "upstream_balance_low",
                "balance:episode",
                "Low balance",
                "Fresh observation",
            )
            await db.commit()
            self.assertEqual(reactivated.id, notification_id)
            self.assertEqual(reactivated.status, "pending")
            self.assertEqual(reactivated.message, "Fresh observation")

        self.assertEqual(await dispatcher.run_once(), 1)

    async def test_two_dispatchers_atomically_claim_one_notification(self) -> None:
        notification_id = await self._enqueue()
        transport = _BlockingTransport()
        first = NotificationDispatcher(
            _RuntimeConfig(),
            transport,
            session_factory=self.sessions,
        )
        second = NotificationDispatcher(
            _RuntimeConfig(),
            transport,
            session_factory=self.sessions,
        )

        first_run = asyncio.create_task(first.run_once())
        await asyncio.wait_for(transport.started.wait(), timeout=2)
        self.assertEqual(await second.run_once(), 0)
        self.assertEqual(len(transport.notifications), 1)
        transport.release.set()
        self.assertEqual(await first_run, 1)

        async with self.sessions() as db:
            row = await db.get(NotificationOutbox, notification_id)
            self.assertEqual(row.status, "sent")
            self.assertEqual(row.attempts, 1)
            self.assertIsNone(row.claim_token)

    async def test_dispatcher_reclaims_an_expired_lease(self) -> None:
        notification_id = await self._enqueue()
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with self.sessions() as db:
            row = await db.get(NotificationOutbox, notification_id)
            row.status = "claimed"
            row.claim_token = "abandoned-claim"
            row.claimed_at = current - timedelta(minutes=2)
            row.claim_expires_at = current - timedelta(seconds=1)
            await db.commit()

        transport = _RecordingTransport()
        dispatcher = NotificationDispatcher(
            _RuntimeConfig(),
            transport,
            session_factory=self.sessions,
            now=lambda: current,
        )
        self.assertEqual(await dispatcher.run_once(), 1)
        self.assertEqual(len(transport.notifications), 1)
        async with self.sessions() as db:
            row = await db.get(NotificationOutbox, notification_id)
            self.assertEqual(row.status, "sent")
            self.assertEqual(row.attempts, 1)

    async def test_dispatcher_records_safe_failure_and_retries_with_backoff(self) -> None:
        notification_id = await self._enqueue()
        current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        transport = _RecordingTransport(
            NotificationTransportError(
                "discord_rate_limited",
                retry_after_seconds=30,
            )
        )
        dispatcher = NotificationDispatcher(
            _RuntimeConfig(),
            transport,
            session_factory=self.sessions,
            base_retry_seconds=5,
            now=lambda: current[0],
        )

        self.assertEqual(await dispatcher.run_once(), 0)
        async with self.sessions() as db:
            row = await db.get(NotificationOutbox, notification_id)
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.attempts, 1)
            self.assertEqual(row.last_error, "discord_rate_limited|retry_after=30.000")

        current[0] += timedelta(seconds=29)
        await dispatcher.run_once()
        self.assertEqual(len(transport.notifications), 1)

        current[0] += timedelta(seconds=1)
        transport.error = None
        self.assertEqual(await dispatcher.run_once(), 1)
        self.assertEqual(len(transport.notifications), 2)

    async def test_dispatcher_scans_past_backoff_rows_for_new_pending_notifications(self) -> None:
        current = datetime(2026, 1, 1, tzinfo=timezone.utc)
        async with self.sessions() as db:
            db.add_all(
                [
                    NotificationOutbox(
                        event_type="oauth_account_disabled",
                        dedupe_key=f"backoff:{index}",
                        title="Delayed",
                        message="Waiting for retry.",
                        status="failed",
                        attempts=1,
                        last_error="discord_rate_limited|retry_after=3600.000",
                        last_attempt_at=current,
                        created_at=current - timedelta(hours=1, seconds=200 - index),
                    )
                    for index in range(105)
                ]
                + [
                    NotificationOutbox(
                        event_type="oauth_account_disabled",
                        dedupe_key="new:pending",
                        title="New notification",
                        message="Send this now.",
                        status="pending",
                        created_at=current,
                    )
                ]
            )
            await db.commit()

        transport = _RecordingTransport()
        dispatcher = NotificationDispatcher(
            _RuntimeConfig(),
            transport,
            session_factory=self.sessions,
            batch_size=1,
            now=lambda: current,
        )

        self.assertEqual(await dispatcher.run_once(), 1)
        self.assertEqual(
            [item.title for item in transport.notifications],
            ["New notification"],
        )

    async def test_dispatcher_discards_permanent_failures(self) -> None:
        notification_id = await self._enqueue()
        transport = _RecordingTransport(
            NotificationTransportError("discord_unauthorized", retryable=False)
        )
        dispatcher = NotificationDispatcher(
            _RuntimeConfig(),
            transport,
            session_factory=self.sessions,
            base_retry_seconds=0.01,
        )

        self.assertEqual(await dispatcher.run_once(), 0)
        self.assertEqual(await dispatcher.run_once(), 0)
        self.assertEqual(len(transport.notifications), 1)
        async with self.sessions() as db:
            row = await db.get(NotificationOutbox, notification_id)
            self.assertEqual(row.status, "discarded")
            self.assertEqual(row.attempts, 1)
            self.assertEqual(row.last_error, "discord_unauthorized|permanent=1")

    async def test_dispatcher_start_and_stop_are_idempotent(self) -> None:
        dispatcher = NotificationDispatcher(
            _RuntimeConfig(_notification_config(enabled=False)),
            _RecordingTransport(),
            session_factory=self.sessions,
            poll_interval_seconds=60,
        )
        dispatcher.start()
        task = dispatcher._task
        dispatcher.start()
        self.assertIs(dispatcher._task, task)
        await asyncio.sleep(0)
        await dispatcher.stop()
        self.assertIsNone(dispatcher._task)
        await dispatcher.stop()


class DiscordTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_posts_bot_message_and_disables_mentions(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(1, "test", "Title", "Hello @everyone")
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0].url,
            httpx.URL("https://discord.com/api/v10/channels/123456/messages"),
        )
        self.assertEqual(requests[0].headers["Authorization"], "Bot bot-secret")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("content", payload)
        self.assertEqual(payload["embeds"][0]["title"], "Title")
        self.assertEqual(payload["embeds"][0]["description"], "Hello @everyone")
        self.assertEqual(payload["embeds"][0]["footer"]["text"], "管理站点 · 自动通知")

    async def test_discord_uses_localized_event_embed_with_structured_fields(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                1,
                "upstream_group_multiplier_changed",
                "Upstream group multiplier changed",
                "Cheese / VIP: 1 -> 1.5.",
                {
                    "upstream_id": 3,
                    "upstream_name": "奶酪",
                    "group_id": "vip",
                    "group_name": "Codex Plus 稳定版",
                    "old_multiplier": 1,
                    "new_multiplier": 1.5,
                    "observed_at": "2026-07-21T09:00:00+00:00",
                },
            )
        )

        payload = json.loads(requests[0].content)
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "倍率变化")
        self.assertEqual(embed["color"], 0xE67E22)
        self.assertEqual(embed["timestamp"], "2026-07-21T09:00:00+00:00")
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["上游"], "奶酪")
        self.assertEqual(fields["上游分组"], "Codex Plus 稳定版")
        self.assertEqual(fields["原倍率"], "1×")
        self.assertEqual(fields["新倍率"], "1.5×")
        self.assertEqual(fields["变化幅度"], "↑ +50.00%")

    async def test_discord_group_rate_embed_lists_all_affected_accounts(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                1,
                "upstream_group_multiplier_changed",
                "Group multiplier changed",
                "VIP changed.",
                {
                    "upstream_name": "哈基米",
                    "group_id": "codex-pro",
                    "group_name": "codex pro",
                    "old_multiplier": 1.0,
                    "new_multiplier": 1.2,
                    "affected_accounts": [
                        {
                            "account_id": 7,
                            "account_name": "哈基米 pro",
                            "old_rate": 0.10,
                            "new_rate": 0.12,
                            "status": "applied",
                        },
                        {
                            "account_id": 8,
                            "account_name": "哈基米 pro+",
                            "old_rate": 0.20,
                            "new_rate": 0.24,
                            "status": "observed",
                        },
                    ],
                },
            )
        )

        embed = json.loads(requests[0].content)["embeds"][0]
        self.assertEqual(embed["title"], "倍率变化")
        affected = next(
            field for field in embed["fields"] if field["name"] == "关联账号 · 2"
        )
        self.assertIn("哈基米 pro · 0.1× → 0.12× · 已应用", affected["value"])
        self.assertIn("哈基米 pro+ · 0.2× → 0.24× · 已确认", affected["value"])

    async def test_discord_renders_all_upstream_group_change_types(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        cases = [
            ("added", {"new_status": "available", "new_multiplier": 1.0}, "新增"),
            ("removed", {"old_status": "available", "new_status": "removed"}, "删除"),
            (
                "renamed",
                {"old_name": "Old", "new_name": "New"},
                "名称变化",
            ),
            (
                "multiplier",
                {"old_multiplier": 1.0, "new_multiplier": 1.5},
                "倍率变化",
            ),
        ]
        for index, (change_type, extra, expected_label) in enumerate(cases, start=1):
            with self.subTest(change_type=change_type):
                await transport.send(
                    NotificationEnvelope(
                        index,
                        "upstream_group_changed",
                        "Upstream group changed",
                        f"VIP: {change_type}",
                        {
                            "upstream_id": 3,
                            "upstream_name": "奶酪",
                            "group_id": "vip",
                            "group_name": "VIP",
                            "change_type": change_type,
                            "observed_at": "2026-07-21T09:00:00+00:00",
                            **extra,
                        },
                    )
                )
                embed = json.loads(requests[-1].content)["embeds"][0]
                fields = {field["name"]: field["value"] for field in embed["fields"]}
                self.assertEqual(embed["title"], "上游分组变化")
                self.assertEqual(fields["上游"], "奶酪")
                self.assertEqual(fields["上游分组"], "VIP")
                self.assertEqual(fields["变化类型"], expected_label)

        self.assertEqual(len(requests), 4)

    async def test_discord_token_invalid_event_uses_localized_alert(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                2,
                "upstream_token_invalid",
                "Upstream channel token invalid",
                "哈基米 token invalid.",
                {
                    "upstream_id": 7,
                    "upstream_name": "哈基米",
                    "observed_at": "2026-07-21T09:30:00+00:00",
                },
            )
        )

        embed = json.loads(requests[0].content)["embeds"][0]
        self.assertEqual(embed["title"], "Token 失效")
        self.assertEqual(embed["color"], 0xED4245)
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["上游"], "哈基米")
        self.assertEqual(fields["当前状态"], "🔴 Token 失效")

    async def test_discord_account_embed_includes_state_and_reason(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                2,
                "account_disabled",
                "Account disabled",
                "Upstream Seven (api_key) was disabled.",
                {
                    "account_type": "api_key",
                    "account_id": 7,
                    "account_name": "Upstream Seven",
                    "upstream_id": 3,
                    "reason": "Account state changed manually.",
                },
            )
        )

        embed = json.loads(requests[0].content)["embeds"][0]
        self.assertEqual(embed["title"], "调度变化")
        self.assertEqual(embed["color"], 0xED4245)
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["账号"], "Upstream Seven")
        self.assertEqual(fields["类型"], "API Key")
        self.assertEqual(fields["当前状态"], "🔴 已停用")
        self.assertEqual(fields["关联上游"], "上游 #3")
        self.assertEqual(fields["触发原因"], "手动修改账号状态")

    async def test_discord_account_pause_embed_labels_every_automatic_pause_reason(self) -> None:
        expected_labels = {
            "upstream_balance_negative": "上游余额低于配置阈值",
            "upstream_monitor_unavailable": "上游监控异常",
            "upstream_rate_increase": "上游倍率涨幅超过停用阈值",
            "upstream_key_unavailable": "上游 API Key 不可用",
            "upstream_group_unavailable": "上游分组不可用",
        }

        for reason, expected_label in expected_labels.items():
            requests: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                requests.append(request)
                return httpx.Response(204)

            def client_factory(**kwargs):
                return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

            transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
            await transport.send(
                NotificationEnvelope(
                    10,
                    "account_disabled",
                    "Account disabled",
                    "API Key account was disabled.",
                    {
                        "account_type": "api_key",
                        "account_id": 7,
                        "account_name": "Upstream Seven",
                        "reason": reason,
                    },
                )
            )

            fields = {
                field["name"]: field["value"]
                for field in json.loads(requests[0].content)["embeds"][0]["fields"]
            }
            self.assertEqual(fields["触发原因"], expected_label)

    async def test_discord_account_recovery_embed_includes_previous_pause_reasons(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                3,
                "account_enabled",
                "Account enabled",
                "Upstream Seven (api_key) was enabled.",
                {
                    "account_type": "api_key",
                    "account_id": 7,
                    "account_name": "Upstream Seven",
                    "upstream_name": "奶酪",
                    "reason": "All automatic pause conditions cleared.",
                    "previous_pause_reasons": [
                        "upstream_balance_negative",
                        "upstream_monitor_unavailable",
                    ],
                },
            )
        )

        embed = json.loads(requests[0].content)["embeds"][0]
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["当前状态"], "🟢 已启用")
        self.assertEqual(fields["恢复说明"], "所有自动暂停条件均已解除")
        self.assertEqual(
            fields["原暂停原因"],
            "• 上游余额低于配置阈值\n• 上游监控异常",
        )

    async def test_discord_balance_alert_uses_configured_threshold(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                3,
                "upstream_balance_low",
                "Upstream channel balance is below the configured threshold",
                "奶酪 balance is 4.99 USD; threshold is 5.00.",
                {
                    "upstream_id": 6,
                    "upstream_name": "奶酪",
                    "balance": 4.99,
                    "threshold": 5.0,
                    "basis": "wallet",
                    "unit": "USD",
                },
            )
        )

        embed = json.loads(requests[0].content)["embeds"][0]
        fields = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(fields["当前余额"], "🔴 4.99 USD")
        self.assertEqual(fields["配置阈值"], "5.00 USD")
        self.assertEqual(fields["检测口径"], "上游钱包余额")

    async def test_discord_balance_pause_reason_uses_threshold_not_zero(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                4,
                "account_disabled",
                "Account disabled",
                "Upstream Seven was disabled.",
                {
                    "account_type": "api_key",
                    "account_id": 7,
                    "account_name": "Upstream Seven",
                    "upstream_name": "奶酪",
                    "reason": "upstream_balance_negative",
                    "balance": 2.0,
                    "threshold": 3.0,
                    "basis": "recharge_adjusted",
                    "unit": "CNY",
                },
            )
        )

        fields = {
            field["name"]: field["value"]
            for field in json.loads(requests[0].content)["embeds"][0]["fields"]
        }
        self.assertEqual(fields["触发原因"], "上游余额低于配置阈值")
        self.assertEqual(fields["配置阈值"], "3.00 CNY")
        self.assertNotIn("低于 0", " ".join(fields.values()))

    async def test_discord_falls_back_to_text_when_embeds_are_rejected(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(403 if len(requests) == 1 else 204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(NotificationEnvelope(1, "test", "Title", "Message"))

        self.assertEqual(len(requests), 2)
        fallback = json.loads(requests[1].content)
        self.assertEqual(fallback["content"], "**Title**\nMessage")
        self.assertEqual(fallback["allowed_mentions"], {"parse": []})

    async def test_discord_recovery_text_fallback_keeps_previous_pause_reasons(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(403 if len(requests) == 1 else 204)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        await transport.send(
            NotificationEnvelope(
                5,
                "account_enabled",
                "API Key account enabled",
                "Upstream Seven was restored.",
                {
                    "previous_pause_reasons": [
                        "upstream_balance_negative",
                        "upstream_rate_increase",
                    ]
                },
            )
        )

        self.assertEqual(len(requests), 2)
        fallback = json.loads(requests[1].content)
        self.assertIn("**调度变化**", fallback["content"])
        self.assertIn("账号已启用", fallback["content"])
        self.assertIn("上游余额低于配置阈值", fallback["content"])
        self.assertIn("上游倍率涨幅超过停用阈值", fallback["content"])
        self.assertEqual(fallback["allowed_mentions"], {"parse": []})

    async def test_discord_errors_do_not_expose_token_or_response_body(self) -> None:
        cases = [
            (401, {}, "top-secret-response", "discord_unauthorized", None),
            (429, {"Retry-After": "12"}, "top-secret-response", "discord_rate_limited", 12.0),
        ]
        for status, headers, body, code, retry_after in cases:
            with self.subTest(status=status):
                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(status, headers=headers, text=body)

                def client_factory(**kwargs):
                    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

                transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
                with self.assertRaises(NotificationTransportError) as raised:
                    await transport.send(NotificationEnvelope(1, "test", "Title", "Message"))
                rendered = str(raised.exception)
                self.assertEqual(rendered, code)
                self.assertNotIn("bot-secret", rendered)
                self.assertNotIn(body, rendered)
                self.assertEqual(raised.exception.retry_after_seconds, retry_after)

    async def test_discord_permission_errors_are_actionable_and_safe(self) -> None:
        cases = [
            (403, 50001, "discord_missing_access"),
            (403, 50013, "discord_missing_permissions"),
            (404, 10003, "discord_channel_not_found"),
        ]
        for status, discord_code, expected_code in cases:
            with self.subTest(discord_code=discord_code):
                def handler(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        status,
                        json={"code": discord_code, "message": "top-secret-response"},
                    )

                def client_factory(**kwargs):
                    return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

                transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
                with self.assertRaises(NotificationTransportError) as raised:
                    await transport.send(NotificationEnvelope(1, "test", "Title", "Message"))
                self.assertEqual(str(raised.exception), expected_code)
                self.assertNotIn("top-secret-response", str(raised.exception))
                self.assertFalse(raised.exception.retryable)

    async def test_discord_timeout_has_a_safe_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("secret timeout detail", request=request)

        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

        transport = DiscordBotTransport(_RuntimeConfig(), client_factory=client_factory)
        with self.assertRaises(NotificationTransportError) as raised:
            await transport.send(NotificationEnvelope(1, "test", "Title", "Message"))
        self.assertEqual(str(raised.exception), "discord_timeout")


if __name__ == "__main__":
    unittest.main()
