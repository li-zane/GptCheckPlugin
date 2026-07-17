from time import perf_counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppEvent


def elapsed_ms(started_at: float, ended_at: float | None = None) -> int:
    """Return a stable, non-negative elapsed duration for event details."""

    finished_at = perf_counter() if ended_at is None else ended_at
    return max(0, round((finished_at - started_at) * 1000))


async def record_event(
    db: AsyncSession,
    kind: str,
    message: str,
    email: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(AppEvent(kind=kind, email=email, message=message, details=details))
    await db.commit()
