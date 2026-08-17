from __future__ import annotations

import hashlib
from datetime import datetime
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotificationOutbox
from app.models import utcnow
from app.services.runtime_config import get_runtime_config_service


EVENT_SWITCHES = {
    "oauth_account_disabled": "oauth_account_disabled_enabled",
    "account_disabled": "account_disabled_enabled",
    "account_enabled": "account_enabled_enabled",
    "api_key_rate_changed": "api_key_rate_changed_enabled",
    "upstream_group_changed": "upstream_group_changed_enabled",
    "upstream_group_multiplier_changed": "api_key_rate_changed_enabled",
    "upstream_balance_low": "upstream_balance_low_enabled",
    "upstream_token_invalid": "upstream_token_invalid_enabled",
}


class NotificationService:
    """Adds notifications to the caller's transaction without external I/O."""

    def __init__(self, db: AsyncSession, runtime_config: Any | None = None) -> None:
        self.db = db
        self.runtime_config = runtime_config or get_runtime_config_service()

    async def enqueue_if_enabled(
        self,
        event_type: str,
        dedupe_key: str,
        title: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> NotificationOutbox | None:
        if not await self.event_enabled(event_type):
            return None
        notification = await self.enqueue(
            event_type,
            dedupe_key,
            title,
            message,
            details,
        )
        if notification.status != "canceled":
            return notification
        current = utcnow()
        result = await self.db.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == notification.id,
                NotificationOutbox.status == "canceled",
            )
            .values(
                title=_required_text(title, "title", 200),
                message=_required_text(message, "message"),
                details=dict(details) if details is not None else None,
                status="pending",
                attempts=0,
                last_error=None,
                last_attempt_at=None,
                claim_token=None,
                claimed_at=None,
                claim_expires_at=None,
                sent_at=None,
                updated_at=current,
            )
        )
        if int(result.rowcount or 0) == 1:
            await self.db.refresh(notification)
        return notification

    async def event_enabled(self, event_type: str) -> bool:
        try:
            config = await self.runtime_config.get_notification_config()
        except Exception:
            return False
        if not _config_bool(config, "enabled"):
            return False
        switch = EVENT_SWITCHES.get(event_type)
        if switch is None:
            return False
        if (
            switch == "upstream_group_changed_enabled"
            and isinstance(config, Mapping)
            and switch not in config
        ):
            return _config_bool(config, "api_key_rate_changed_enabled")
        return _config_bool(config, switch)

    async def enqueue(
        self,
        event_type: str,
        dedupe_key: str,
        title: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> NotificationOutbox:
        values = {
            "event_type": _required_text(event_type, "event_type", 64),
            "dedupe_key": _required_text(dedupe_key, "dedupe_key", 256),
            "title": _required_text(title, "title", 200),
            "message": _required_text(message, "message"),
            "details": dict(details) if details is not None else None,
            "status": "pending",
            "attempts": 0,
        }

        bind = self.db.get_bind()
        if bind.dialect.name == "sqlite":
            statement = (
                sqlite_insert(NotificationOutbox)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["dedupe_key"])
            )
            await self.db.execute(statement)
        else:
            existing = await self.db.scalar(
                select(NotificationOutbox).where(
                    NotificationOutbox.dedupe_key == values["dedupe_key"]
                )
            )
            if existing is None:
                self.db.add(NotificationOutbox(**values))
                await self.db.flush()

        notification = await self.db.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.dedupe_key == values["dedupe_key"]
            )
        )
        if notification is None:
            # This is only reachable for an unsupported database with unusual
            # transaction isolation behavior.
            raise RuntimeError("notification_enqueue_failed")
        return notification

    async def cancel_unsent(
        self,
        *,
        dedupe_key: str | None = None,
        event_types: set[str] | None = None,
    ) -> int:
        statement = update(NotificationOutbox).where(
            NotificationOutbox.status.in_(("pending", "failed", "claimed"))
        )
        if dedupe_key is not None:
            statement = statement.where(NotificationOutbox.dedupe_key == dedupe_key)
        if event_types is not None:
            if not event_types:
                return 0
            statement = statement.where(NotificationOutbox.event_type.in_(event_types))
        current = utcnow()
        result = await self.db.execute(
            statement.values(
                status="canceled",
                claim_token=None,
                claimed_at=None,
                claim_expires_at=None,
                updated_at=current,
            )
        )
        return int(result.rowcount or 0)


async def enqueue_notification(
    db: AsyncSession,
    event_type: str,
    dedupe_key: str,
    title: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> NotificationOutbox | None:
    return await NotificationService(db).enqueue_if_enabled(
        event_type,
        dedupe_key,
        title,
        message,
        details,
    )


async def enqueue_oauth_account_disabled(
    db: AsyncSession,
    email: str,
    reason: str,
    *,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    return await enqueue_account_state_changed(
        db,
        enabled=False,
        account_type="oauth",
        email=email,
        reason=reason,
        runtime_config=runtime_config,
    )


async def enqueue_oauth_account_enabled(
    db: AsyncSession,
    email: str,
    reason: str,
    *,
    account_id: int | str | None = None,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    return await enqueue_account_state_changed(
        db,
        enabled=True,
        account_type="oauth",
        account_id=account_id,
        email=email,
        reason=reason,
        runtime_config=runtime_config,
    )


async def enqueue_api_key_account_state_changed(
    db: AsyncSession,
    *,
    enabled: bool,
    account_id: int,
    account_name: str | None,
    reason: str,
    upstream_id: str | None = None,
    upstream_name: str | None = None,
    reason_details: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    return await enqueue_account_state_changed(
        db,
        enabled=enabled,
        account_type="api_key",
        account_id=account_id,
        account_name=account_name,
        reason=reason,
        upstream_id=upstream_id,
        upstream_name=upstream_name,
        reason_details=reason_details,
        observed_at=observed_at,
        runtime_config=runtime_config,
    )


async def enqueue_account_state_changed(
    db: AsyncSession,
    *,
    enabled: bool,
    account_type: str,
    reason: str,
    account_id: int | str | None = None,
    account_name: str | None = None,
    email: str | None = None,
    upstream_id: str | None = None,
    upstream_name: str | None = None,
    reason_details: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    observed = observed_at or utcnow()
    normalized_type = account_type.strip().lower() or "unknown"
    normalized_email = str(email or "").strip().lower() or None
    normalized_name = str(account_name or "").strip() or None
    display_name = (
        normalized_name
        or normalized_email
        or (f"Account #{account_id}" if account_id is not None else "Account")
    )
    event_type = "account_enabled" if enabled else "account_disabled"
    state_label = "enabled" if enabled else "disabled"
    identity = f"{normalized_type}:{account_id}:{normalized_email}:{normalized_name}"
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    details: dict[str, Any] = {
        "account_type": normalized_type,
        "account_id": account_id,
        "account_name": normalized_name,
        "email": normalized_email,
        "upstream_id": upstream_id,
        "upstream_name": str(upstream_name or "").strip() or None,
        "enabled": enabled,
        "reason": str(reason or "")[:500],
        "observed_at": observed.isoformat(),
    }
    if isinstance(reason_details, dict):
        for key in ("balance", "basis", "threshold", "unit"):
            if key in reason_details:
                details[key] = reason_details[key]
        previous_pause_reasons = reason_details.get("previous_pause_reasons")
        if isinstance(previous_pause_reasons, list):
            normalized_reasons: list[str] = []
            for item in previous_pause_reasons:
                reason_value = str(item or "").strip()[:64]
                if reason_value and reason_value not in normalized_reasons:
                    normalized_reasons.append(reason_value)
                if len(normalized_reasons) >= 8:
                    break
            if normalized_reasons:
                details["previous_pause_reasons"] = normalized_reasons
    return await NotificationService(db, runtime_config).enqueue_if_enabled(
        event_type,
        f"account-state:{state_label}:{identity_hash}:{observed.isoformat()}",
        f"Account {state_label}",
        f"{display_name} ({normalized_type}) was {state_label}.",
        details,
    )


async def enqueue_api_key_rate_changed(
    db: AsyncSession,
    *,
    account_id: int,
    account_name: str | None,
    old_rate: float,
    new_rate: float,
    observed_at: datetime,
    reason: str,
    upstream_id: str | None = None,
    upstream_name: str | None = None,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    display_name = (account_name or "").strip() or f"Account #{account_id}"
    return await NotificationService(db, runtime_config).enqueue_if_enabled(
        "api_key_rate_changed",
        (
            f"api-key-rate:{account_id}:{observed_at.isoformat()}:"
            f"{old_rate:.8g}:{new_rate:.8g}"
        ),
        "API key account rate changed",
        f"{display_name}: {old_rate:.4g} -> {new_rate:.4g}.",
        {
            "account_id": account_id,
            "account_name": account_name,
            "upstream_id": upstream_id,
            "upstream_name": upstream_name,
            "old_rate": old_rate,
            "new_rate": new_rate,
            "reason": reason,
            "observed_at": observed_at.isoformat(),
        },
    )


async def enqueue_upstream_group_multiplier_changed(
    db: AsyncSession,
    *,
    upstream_id: str,
    upstream_name: str | None,
    group_id: str | None,
    group_name: str | None,
    old_multiplier: float,
    new_multiplier: float,
    observed_at: datetime,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    return await enqueue_upstream_group_changed(
        db,
        upstream_id=upstream_id,
        upstream_name=upstream_name,
        group_id=group_id,
        group_name=group_name,
        change_type="multiplier",
        old_multiplier=old_multiplier,
        new_multiplier=new_multiplier,
        notification_event_type="upstream_group_multiplier_changed",
        observed_at=observed_at,
        runtime_config=runtime_config,
    )


async def enqueue_upstream_group_changed(
    db: AsyncSession,
    *,
    upstream_id: str,
    upstream_name: str | None,
    group_id: str | None,
    group_name: str | None,
    change_type: str,
    old_multiplier: float | None = None,
    new_multiplier: float | None = None,
    old_status: str | None = None,
    new_status: str | None = None,
    details: dict[str, Any] | None = None,
    notification_event_type: str = "upstream_group_changed",
    observed_at: datetime,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    display_channel = str(upstream_name or "").strip() or f"Upstream #{upstream_id}"
    display_group = str(group_name or "").strip() or str(group_id or "").strip() or "Unknown group"
    normalized_change_type = str(change_type or "changed").strip().lower() or "changed"
    identity = (
        f"{upstream_id}:{group_id}:{display_group}:{normalized_change_type}:"
        f"{old_multiplier!r}:{new_multiplier!r}:{old_status}:{new_status}"
    )
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    notification_details: dict[str, Any] = {
        "upstream_id": upstream_id,
        "upstream_name": upstream_name,
        "group_id": group_id,
        "group_name": group_name,
        "change_type": normalized_change_type,
        "old_multiplier": old_multiplier,
        "new_multiplier": new_multiplier,
        "old_status": old_status,
        "new_status": new_status,
        "observed_at": observed_at.isoformat(),
    }
    if isinstance(details, dict):
        notification_details.update(details)
    return await NotificationService(db, runtime_config).enqueue_if_enabled(
        notification_event_type,
        f"upstream-group-change:{identity_hash}:{observed_at.isoformat()}",
        "Upstream group changed",
        f"{display_channel} / {display_group}: {normalized_change_type}.",
        notification_details,
    )


async def enqueue_upstream_token_invalid(
    db: AsyncSession,
    *,
    upstream_id: str,
    upstream_name: str | None,
    credential_fingerprint: str,
    observed_at: datetime,
    runtime_config: Any | None = None,
) -> NotificationOutbox | None:
    display_channel = str(upstream_name or "").strip() or f"Upstream #{upstream_id}"
    fingerprint = hashlib.sha256(
        f"{upstream_id}:{credential_fingerprint}".encode("utf-8")
    ).hexdigest()[:24]
    return await NotificationService(db, runtime_config).enqueue_if_enabled(
        "upstream_token_invalid",
        f"upstream-token-invalid:{fingerprint}",
        "Upstream channel token invalid",
        f"{display_channel}: the saved login token was rejected and could not be refreshed.",
        {
            "upstream_id": upstream_id,
            "upstream_name": upstream_name,
            "reason": "upstream_token_invalid",
            "observed_at": observed_at.isoformat(),
        },
    )


def _required_text(value: str, field: str, max_length: int | None = None) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{field} is required")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return normalized


def _config_bool(config: Mapping[str, Any] | object, key: str) -> bool:
    value = config.get(key) if isinstance(config, Mapping) else getattr(config, key, False)
    return bool(value)
