from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from kcms.notifications.contracts import DeliveryResult, Notification


class DisabledNotificationSender:
    async def send(self, notification: Notification) -> DeliveryResult:
        del notification
        return DeliveryResult("MANUAL_REQUIRED", "SMTP is not configured")


class SmtpNotificationSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_email: str,
        from_name: str,
        timeout: int = 15,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_email = from_email
        self.from_name = from_name
        self.timeout = timeout

    async def send(self, notification: Notification) -> DeliveryResult:
        try:
            await asyncio.to_thread(self._send_sync, notification)
        except Exception as exc:
            # Email is an output side effect. The caller records this failure
            # and keeps a manual invitation-link fallback available.
            return DeliveryResult("FAILED", type(exc).__name__)
        return DeliveryResult("SENT")

    def _send_sync(self, notification: Notification) -> None:
        message = EmailMessage()
        message["From"] = formataddr((self.from_name, self.from_email))
        message["To"] = notification.recipient
        message["Subject"] = notification.subject
        message.set_content(notification.text)

        if self.port in {465, 2465}:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as client:
                client.login(self.username, self.password)
                client.send_message(message)
            return

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
            client.starttls()
            client.login(self.username, self.password)
            client.send_message(message)
