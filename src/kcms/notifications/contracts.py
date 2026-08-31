from dataclasses import dataclass
from typing import Literal, Protocol

DeliveryStatus = Literal["SENT", "FAILED", "MANUAL_REQUIRED"]


@dataclass(frozen=True)
class Notification:
    recipient: str
    subject: str
    text: str
    kind: str


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    detail: str | None = None


class NotificationSender(Protocol):
    async def send(self, notification: Notification) -> DeliveryResult: ...
