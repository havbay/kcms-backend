from email import message_from_string

import pytest

from kcms.notifications.contracts import Notification
from kcms.notifications.smtp import SmtpNotificationSender


class FakeSMTP:
    sent = []

    def __init__(self, host: str, port: int, timeout: int):
        self.host = host
        self.port = port
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def starttls(self):
        self.started_tls = True

    def login(self, username: str, password: str):
        self.credentials = (username, password)

    def send_message(self, message):
        self.sent.append(message.as_string())


@pytest.mark.asyncio
async def test_smtp_sender_uses_starttls_and_never_puts_credentials_in_the_message(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr("kcms.notifications.smtp.smtplib.SMTP", FakeSMTP)
    sender = SmtpNotificationSender(
        host="smtp.example.com",
        port=587,
        username="resend",
        password="secret-api-key",
        from_email="access@updates.kcms.example",
        from_name="KCMS",
    )

    result = await sender.send(
        Notification(
            recipient="client@example.com",
            subject="Your KCMS invitation",
            text="Use this one-time link: https://kcms.example/setup/one-time-token",
            kind="PILOT_APPROVED",
        )
    )

    assert result.status == "SENT"
    assert len(FakeSMTP.sent) == 1
    message = message_from_string(FakeSMTP.sent[0])
    assert message["To"] == "client@example.com"
    assert message["From"] == "KCMS <access@updates.kcms.example>"
    assert "secret-api-key" not in FakeSMTP.sent[0]


@pytest.mark.asyncio
async def test_disabled_sender_returns_a_manual_fallback_without_claiming_delivery():
    from kcms.notifications.smtp import DisabledNotificationSender

    result = await DisabledNotificationSender().send(
        Notification("client@example.com", "Subject", "Body", "PILOT_APPROVED")
    )

    assert result.status == "MANUAL_REQUIRED"
    assert result.detail == "SMTP is not configured"
