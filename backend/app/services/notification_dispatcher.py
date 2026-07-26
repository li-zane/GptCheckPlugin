from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import AsyncSessionLocal
from app.models import NotificationOutbox, utcnow
from app.services.notifications import EVENT_SWITCHES, NotificationService
from app.services.notification_transports import (
    DiscordBotTransport,
    NotificationEnvelope,
    NotificationTransport,
    NotificationTransportError,
)
from app.services.runtime_config import RuntimeConfigService, get_runtime_config_service


@dataclass(frozen=True)
class _ClaimedNotification:
    envelope: NotificationEnvelope
    claim_token: str


class NotificationDispatcher:
    def __init__(
        self,
        runtime_config: RuntimeConfigService | None = None,
        transport: NotificationTransport | None = None,
        *,
        session_factory: async_sessionmaker[AsyncSession] | Callable[[], Any] | None = None,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 25,
        base_retry_seconds: float = 5.0,
        max_retry_seconds: float = 3600.0,
        claim_lease_seconds: float = 120.0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.runtime_config = runtime_config or get_runtime_config_service()
        self.transport = transport or DiscordBotTransport(self.runtime_config)
        self.session_factory = session_factory or AsyncSessionLocal
        self.poll_interval_seconds = max(0.01, poll_interval_seconds)
        self.batch_size = max(1, batch_size)
        self.base_retry_seconds = max(0.01, base_retry_seconds)
        self.max_retry_seconds = max(self.base_retry_seconds, max_retry_seconds)
        self.claim_lease_seconds = max(1.0, claim_lease_seconds)
        self.now = now
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._run_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stop.clear()
            self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    async def run_once(self) -> int:
        async with self._run_lock:
            try:
                config = await self.runtime_config.get_notification_config()
            except asyncio.CancelledError:
                raise
            except Exception:
                return 0
            if not _config_bool(config, "enabled"):
                await self._cancel_unsent()
                return 0

            disabled_events = {
                event_type
                for event_type, key in EVENT_SWITCHES.items()
                if not _config_bool(config, key)
            }
            if disabled_events:
                await self._cancel_unsent(disabled_events)
            sent = 0
            for _ in range(self.batch_size):
                notifications = await self._claim_due(disabled_events, limit=1)
                if not notifications:
                    break
                claimed = notifications[0]
                if not await self._claim_is_active(claimed):
                    continue
                try:
                    await self.transport.send(claimed.envelope)
                except asyncio.CancelledError:
                    raise
                except NotificationTransportError as exc:
                    await self._mark_failed(claimed, exc)
                except Exception:
                    await self._mark_failed(
                        claimed,
                        NotificationTransportError("notification_transport_error"),
                    )
                else:
                    if await self._mark_sent(claimed):
                        sent += 1
            return sent

    async def send_test_notification(self) -> None:
        """Send a direct configuration probe through the active transport.

        This deliberately bypasses the outbox so the settings page can report
        credential/channel errors immediately without creating a persistent
        test event or competing with real alerts.
        """
        try:
            config = await self.runtime_config.get_notification_config()
        except Exception as exc:
            raise NotificationTransportError("notification_config_unavailable", retryable=False) from exc
        if not _config_bool(config, "enabled"):
            raise NotificationTransportError("notifications_disabled", retryable=False)
        await self.transport.send(
            NotificationEnvelope(
                id=0,
                event_type="notification_test",
                title="sub2api 通知测试",
                message="Discord Bot 通知配置有效。",
            )
        )

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A dispatcher failure must not terminate the application.
                # Keep the loop alive, but do not spin at full speed after a
                # broken database/configuration read.
                await asyncio.sleep(min(self.poll_interval_seconds, 5.0))
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._wait_for_wake_or_stop(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _wait_for_wake_or_stop(self) -> None:
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                {stop_task, wake_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
            wake_task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)
            self._wake.clear()

    async def _cancel_unsent(self, event_types: set[str] | None = None) -> None:
        async with self.session_factory() as db:
            await NotificationService(db, self.runtime_config).cancel_unsent(
                event_types=event_types
            )
            await db.commit()

    async def _claim_due(
        self,
        disabled_events: set[str],
        *,
        limit: int | None = None,
    ) -> list[_ClaimedNotification]:
        claim_limit = max(1, limit or self.batch_size)
        current = _aware_utc(self.now())
        statement = select(NotificationOutbox).where(
            NotificationOutbox.status.in_(("pending", "failed", "claimed"))
        )
        if disabled_events:
            statement = statement.where(
                NotificationOutbox.event_type.notin_(disabled_events)
            )
        statement = statement.order_by(
            NotificationOutbox.created_at,
            NotificationOutbox.id,
        )
        scan_page_size = max(self.batch_size * 20, 100)
        cursor_created_at: datetime | None = None
        cursor_id = 0
        claimed: list[_ClaimedNotification] = []
        while len(claimed) < claim_limit:
            page_statement = statement
            if cursor_created_at is not None:
                page_statement = page_statement.where(
                    or_(
                        NotificationOutbox.created_at > cursor_created_at,
                        and_(
                            NotificationOutbox.created_at == cursor_created_at,
                            NotificationOutbox.id > cursor_id,
                        ),
                    )
                )
            async with self.session_factory() as db:
                rows = list(
                    (
                        await db.scalars(
                            page_statement.limit(scan_page_size)
                        )
                    ).all()
                )
            if not rows:
                break
            cursor_created_at = rows[-1].created_at
            cursor_id = rows[-1].id
            for row in rows:
                if len(claimed) >= claim_limit:
                    break
                if not self._is_due(row, current):
                    continue
                item = await self._claim_one(row, current)
                if item is not None:
                    claimed.append(item)
        return claimed

    async def _claim_one(
        self,
        row: NotificationOutbox,
        current: datetime,
    ) -> _ClaimedNotification | None:
        claim_token = uuid4().hex
        lease_expires = current + timedelta(seconds=self.claim_lease_seconds)
        conditions = [
            NotificationOutbox.id == row.id,
            NotificationOutbox.status == row.status,
            NotificationOutbox.attempts == row.attempts,
        ]
        if row.status == "claimed":
            conditions.append(_nullable_equals(NotificationOutbox.claim_token, row.claim_token))
            conditions.append(
                or_(
                    NotificationOutbox.claim_expires_at.is_(None),
                    NotificationOutbox.claim_expires_at <= current,
                )
            )
        else:
            conditions.append(
                _nullable_equals(NotificationOutbox.last_attempt_at, row.last_attempt_at)
            )

        async with self.session_factory() as db:
            result = await db.execute(
                update(NotificationOutbox)
                .where(and_(*conditions))
                .values(
                    status="claimed",
                    claim_token=claim_token,
                    claimed_at=current,
                    claim_expires_at=lease_expires,
                    updated_at=current,
                )
            )
            if int(result.rowcount or 0) != 1:
                await db.rollback()
                return None
            claimed_row = await db.get(NotificationOutbox, row.id)
            if claimed_row is None:
                await db.rollback()
                return None
            envelope = NotificationEnvelope(
                id=claimed_row.id,
                event_type=claimed_row.event_type,
                title=claimed_row.title,
                message=claimed_row.message,
                details=(
                    dict(claimed_row.details)
                    if isinstance(claimed_row.details, dict)
                    else None
                ),
            )
            await db.commit()
        return _ClaimedNotification(envelope=envelope, claim_token=claim_token)

    def _is_due(self, row: NotificationOutbox, current: datetime) -> bool:
        if row.last_error and "permanent=1" in row.last_error:
            return False
        if row.status == "claimed":
            return row.claim_expires_at is None or current >= _aware_utc(
                row.claim_expires_at
            )
        if row.status == "pending" or row.last_attempt_at is None:
            return True
        exponent = min(max(0, row.attempts - 1), 30)
        delay = min(
            self.max_retry_seconds,
            self.base_retry_seconds * (2**exponent),
        )
        retry_after = _stored_retry_after(row.last_error)
        if retry_after is not None:
            delay = max(delay, retry_after)
        last_attempt = _aware_utc(row.last_attempt_at)
        return current >= last_attempt + timedelta(seconds=delay)

    async def _claim_is_active(self, claimed: _ClaimedNotification) -> bool:
        async with self.session_factory() as db:
            row = await db.get(NotificationOutbox, claimed.envelope.id)
            return bool(
                row is not None
                and row.status == "claimed"
                and row.claim_token == claimed.claim_token
            )

    async def _mark_sent(self, claimed: _ClaimedNotification) -> bool:
        async with self.session_factory() as db:
            current = self.now()
            result = await db.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == claimed.envelope.id,
                    NotificationOutbox.status == "claimed",
                    NotificationOutbox.claim_token == claimed.claim_token,
                )
                .values(
                    status="sent",
                    attempts=NotificationOutbox.attempts + 1,
                    last_error=None,
                    last_attempt_at=current,
                    claim_token=None,
                    claimed_at=None,
                    claim_expires_at=None,
                    sent_at=current,
                    updated_at=current,
                )
            )
            await db.commit()
            return int(result.rowcount or 0) == 1

    async def _mark_failed(
        self,
        claimed: _ClaimedNotification,
        error: NotificationTransportError,
    ) -> None:
        async with self.session_factory() as db:
            current = self.now()
            await db.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == claimed.envelope.id,
                    NotificationOutbox.status == "claimed",
                    NotificationOutbox.claim_token == claimed.claim_token,
                )
                .values(
                    status="failed" if error.retryable else "discarded",
                    attempts=NotificationOutbox.attempts + 1,
                    last_error=_safe_failure_value(error),
                    last_attempt_at=current,
                    claim_token=None,
                    claimed_at=None,
                    claim_expires_at=None,
                    updated_at=current,
                )
            )
            await db.commit()


def _nullable_equals(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


def _config_bool(config: Mapping[str, Any] | object, key: str) -> bool:
    value = config.get(key) if isinstance(config, Mapping) else getattr(config, key, False)
    return bool(value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _safe_failure_value(error: NotificationTransportError) -> str:
    parts = [error.code]
    if error.retry_after_seconds is not None:
        retry_after = min(max(error.retry_after_seconds, 0.0), 86_400.0)
        parts.append(f"retry_after={retry_after:.3f}")
    if not error.retryable:
        parts.append("permanent=1")
    return "|".join(parts)


def _stored_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    for part in value.split("|"):
        if not part.startswith("retry_after="):
            continue
        try:
            return min(max(float(part.partition("=")[2]), 0.0), 86_400.0)
        except ValueError:
            return None
    return None


_dispatcher: NotificationDispatcher | None = None


def get_notification_dispatcher() -> NotificationDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = NotificationDispatcher()
    return _dispatcher
