"""In-process event bus and HMAC-signed outbound webhooks.

Event handlers run after the ledger write they describe. A failing handler is logged and recorded in
``EventBus.failures``; it never rolls back or blocks the state change.
"""

from __future__ import annotations

import datetime as dt
import hmac
import logging
import time
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, Field

from ._util import canonical_json, hmac_sha256_hex, new_id, utcnow

log = logging.getLogger("dpdpkit.events")

SIGNATURE_HEADER = "X-DPDPKit-Signature"
EVENT_HEADER = "X-DPDPKit-Event"

# Event types emitted by core.
CONSENT_GRANTED = "consent.granted"
CONSENT_DENIED = "consent.denied"
CONSENT_WITHDRAWN = "consent.withdrawn"
NOTICE_PUBLISHED = "notice.published"
REQUEST_OPENED = "request.opened"
REQUEST_UPDATED = "request.updated"
REQUEST_CLOSED = "request.closed"
ERASURE_WARNING_SENT = "erasure.warning_sent"
ERASURE_DUE = "erasure.due"
ERASURE_COMPLETED = "erasure.completed"
ERASURE_NEEDS_ATTENTION = "erasure.needs_attention"
ERASURE_PROCESSOR_NOTIFIED = "erasure.processor_notified"
EXPORT_REQUESTED = "export.requested"


class Event(BaseModel):
    id: str
    type: str
    tenant_id: str
    occurred_at: dt.datetime
    data: dict[str, Any] = Field(default_factory=dict)


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        self.failures: list[tuple[Event, str]] = []

    def subscribe(self, event_type: str, handler: Handler) -> Handler:
        """Subscribe to one type, or ``"*"`` for all. Returns the handler so it works as a decorator."""
        self._handlers[event_type].append(handler)
        return handler

    def on(self, event_type: str) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            return self.subscribe(event_type, handler)

        return decorator

    def publish(self, event_type: str, tenant_id: str, data: dict[str, Any], at: dt.datetime | None = None) -> Event:
        event = Event(id=new_id("ev"), type=event_type, tenant_id=tenant_id, occurred_at=at or utcnow(), data=data)
        for handler in [*self._handlers.get(event_type, []), *self._handlers.get("*", [])]:
            try:
                handler(event)
            except Exception as exc:
                log.exception("event handler failed for %s", event_type)
                self.failures.append((event, repr(exc)))
        return event


# --------------------------------------------------------------------------- webhook signing


def sign_payload(secret: str | bytes, body: bytes, timestamp: int | None = None) -> str:
    """Return the signature header value: ``t=<unix>,v1=<hex hmac of "t.body">``."""
    key = secret.encode() if isinstance(secret, str) else secret
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={hmac_sha256_hex(key, f'{ts}.'.encode() + body)}"


def verify_signature(
    secret: str | bytes, body: bytes, header: str, *, tolerance: int = 300, now: int | None = None
) -> bool:
    """Verify a webhook signature header; rejects anything older than ``tolerance`` seconds."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
        given = parts["v1"]
    except (KeyError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - ts) > tolerance:
        return False
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.compare_digest(given, hmac_sha256_hex(key, f"{ts}.".encode() + body))


class WebhookEndpoint(BaseModel):
    id: str
    url: str
    secret: str
    events: list[str] = Field(default_factory=lambda: ["*"])
    active: bool = True

    def wants(self, event_type: str) -> bool:
        return self.active and ("*" in self.events or event_type in self.events)


Sender = Callable[[str, bytes, dict[str, str]], int]


def urllib_sender(url: str, body: bytes, headers: dict[str, str]) -> int:
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"unsupported webhook URL scheme: {url}")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return int(resp.status)


class WebhookDispatcher:
    """Delivers events to customer-configured endpoints. Failed deliveries land in ``dead_letters``.

    Adapters that need durable retries persist ``dead_letters`` and call :meth:`redeliver`.
    """

    def __init__(
        self,
        endpoints: Iterable[WebhookEndpoint] = (),
        *,
        sender: Sender = urllib_sender,
        retries: int = 3,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.endpoints = list(endpoints)
        self.sender = sender
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        self.dead_letters: list[tuple[WebhookEndpoint, Event, str]] = []

    def attach(self, bus: EventBus) -> None:
        bus.subscribe("*", self.dispatch)

    def dispatch(self, event: Event) -> None:
        for endpoint in self.endpoints:
            if endpoint.wants(event.type):
                self.deliver(endpoint, event)

    def deliver(self, endpoint: WebhookEndpoint, event: Event) -> bool:
        body = canonical_json(event.model_dump(mode="json"))
        headers = {
            "Content-Type": "application/json",
            EVENT_HEADER: event.type,
            SIGNATURE_HEADER: sign_payload(endpoint.secret, body),
        }
        error = ""
        for attempt in range(self.retries):
            try:
                status = self.sender(endpoint.url, body, headers)
                if 200 <= status < 300:
                    return True
                error = f"HTTP {status}"
            except Exception as exc:
                error = repr(exc)
            if attempt + 1 < self.retries:
                self.sleep(self.backoff_seconds * (2**attempt))
        log.warning("webhook %s failed for %s: %s", endpoint.id, event.type, error)
        self.dead_letters.append((endpoint, event, error))
        return False

    def redeliver(self) -> int:
        pending, self.dead_letters = self.dead_letters, []
        return sum(1 for endpoint, event, _ in pending if self.deliver(endpoint, event))
