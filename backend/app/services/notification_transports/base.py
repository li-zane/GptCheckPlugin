from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NotificationEnvelope:
    id: int
    event_type: str
    title: str
    message: str
    details: dict[str, Any] | None = None


class NotificationTransportError(RuntimeError):
    """A transport failure whose string representation is safe to persist."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = True,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class NotificationTransport(ABC):
    @abstractmethod
    async def send(self, notification: NotificationEnvelope) -> None:
        raise NotImplementedError
