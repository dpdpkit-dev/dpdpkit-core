"""Notification interface (email, SMS, WhatsApp) with delivery receipts.

Core never talks to a provider directly. A :class:`Notifier` turns a :class:`Message` into a
:class:`DeliveryReceipt`. The default :class:`NullNotifier` reports every message as failed, so
nothing that depends on a delivered warning (such as erasure) can run until a real transport is set.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

from ._util import mask_address, new_id, utcnow
from .models import Channel, DeliveryReceipt, DeliveryStatus, Message

# Built-in English templates. Adapters and Cloud can replace them per locale.
TEMPLATES: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "erasure_warning": (
            "Your data with {fiduciary} will be erased",
            'Your personal data held by {fiduciary} for "{purpose}" is scheduled to be erased on {erase_at}.\n'
            "If you want to keep your account, log in or contact us before then: {contact}.\n"
            "You can also exercise your rights at {rights_url}.",
        ),
        "request_opened": (
            "We received your {kind} request",
            "Reference {request_id}. We aim to respond by {due_at}. Contact: {contact}.",
        ),
        "request_closed": (
            "Your {kind} request is {status}",
            "Reference {request_id}.\n\n{response}\n\nContact: {contact}. "
            "If you are not satisfied you may complain to the Data Protection Board of India.",
        ),
    }
}


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, locale: str, context: Mapping[str, object]) -> tuple[str, str]:
    """Render a template in ``locale``, falling back to English."""
    table = TEMPLATES.get(locale) or TEMPLATES.get(locale.split("-")[0]) or TEMPLATES["en"]
    subject, body = table.get(template) or TEMPLATES["en"][template]
    values = _SafeDict({k: str(v) for k, v in context.items()})
    return subject.format_map(values), body.format_map(values)


@runtime_checkable
class Notifier(Protocol):
    def send(self, message: Message) -> DeliveryReceipt: ...


def _receipt(
    message: Message,
    status: DeliveryStatus,
    provider: str,
    provider_ref: str | None = None,
    error: str | None = None,
) -> DeliveryReceipt:
    now = utcnow()
    return DeliveryReceipt(
        id=new_id("dr"),
        principal=message.principal,
        channel=message.channel,
        to_masked=mask_address(message.to),
        template=message.template,
        status=status,
        provider=provider,
        provider_ref=provider_ref,
        error=error,
        created_at=now,
        delivered_at=now if status is DeliveryStatus.DELIVERED else None,
    )


class NullNotifier:
    """No transport configured: every send fails, which keeps fail-safe paths closed."""

    def send(self, message: Message) -> DeliveryReceipt:
        return _receipt(message, DeliveryStatus.FAILED, "null", error="no notification transport configured")


class MemoryNotifier:
    """Records messages in ``sent``. ``status`` controls the outcome (for tests)."""

    def __init__(self, status: DeliveryStatus = DeliveryStatus.DELIVERED) -> None:
        self.status = status
        self.sent: list[Message] = []

    def send(self, message: Message) -> DeliveryReceipt:
        self.sent.append(message)
        error = "simulated failure" if self.status is DeliveryStatus.FAILED else None
        return _receipt(message, self.status, "memory", error=error)


class ConsoleNotifier:
    """Prints messages. For local development only; it reports delivery without real delivery."""

    def __init__(self, write: Callable[[str], object] = print) -> None:
        self.write = write

    def send(self, message: Message) -> DeliveryReceipt:
        self.write(f"[dpdpkit:{message.channel.value}] to={message.to} subject={message.subject}\n{message.body}")
        return _receipt(message, DeliveryStatus.DELIVERED, "console")


class SmtpNotifier:
    """Email over SMTP (stdlib). SMS and WhatsApp messages are refused (failed receipt)."""

    def __init__(
        self,
        host: str,
        port: int = 587,
        *,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.starttls = starttls
        self.use_ssl = use_ssl
        self.timeout = timeout

    def send(self, message: Message) -> DeliveryReceipt:
        if message.channel is not Channel.EMAIL:
            return _receipt(message, DeliveryStatus.FAILED, "smtp", error=f"smtp cannot send {message.channel.value}")
        email = EmailMessage()
        email["From"] = self.sender
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.body)
        try:
            context = ssl.create_default_context()
            smtp: smtplib.SMTP
            if self.use_ssl:
                smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            with smtp:
                if self.starttls and not self.use_ssl:
                    smtp.starttls(context=context)
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                refused = smtp.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            return _receipt(message, DeliveryStatus.FAILED, "smtp", error=repr(exc))
        if refused:
            return _receipt(message, DeliveryStatus.FAILED, "smtp", error=f"refused: {sorted(refused)}")
        # Accepted by the relay. That is the strongest receipt SMTP gives.
        return _receipt(message, DeliveryStatus.DELIVERED, "smtp", provider_ref=email.get("Message-ID"))
