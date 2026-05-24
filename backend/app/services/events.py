from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppEvent


async def record_event(
    db: AsyncSession,
    kind: str,
    message: str,
    email: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(AppEvent(kind=kind, email=email, message=message, details=details))
    await db.commit()
