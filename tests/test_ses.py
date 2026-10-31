from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from dpdpkit import Kit, ScheduleStatus
from dpdpkit.models import Channel, DeliveryReceipt, DeliveryStatus, Message
from dpdpkit.ses import RECEIPT_TAG, SesNotifier, apply_ses_event, poll_ses_events, ses_event_outcome

from .conftest import FakeClock


class FakeSes:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def send_email(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if self.error:
            raise self.error
        return {"MessageId": f"ses-{len(self.requests)}"}


class FakeSqs:
    def __init__(self, bodies: list[Any]) -> None:
        self.messages = [
            {"Body": b if isinstance(b, str) else json.dumps(b), "ReceiptHandle": f"h{i}"} for i, b in enumerate(bodies)
        ]
        self.deleted: list[str] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        batch, self.messages = self.messages[:10], self.messages[10:]
        return {"Messages": batch} if batch else {}

    def delete_message(self, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(ReceiptHandle)


def ses_event(kind: str, receipt_id: str, **extra: Any) -> dict[str, Any]:
    return {"eventType": kind, "mail": {"messageId": "ses-1", "tags": {RECEIPT_TAG: [receipt_id]}}, **extra}


def sns(event: dict[str, Any]) -> dict[str, Any]:
    return {"Type": "Notification", "Message": json.dumps(event)}


def email(**kwargs: Any) -> Message:
    return Message(channel=Channel.EMAIL, to="asha@example.in", subject="विषय", body="नमस्ते", template="t", **kwargs)


def test_accepted_message_counts_as_delivered_by_default() -> None:
    ses = FakeSes()
    receipt = SesNotifier("privacy@example.in", configuration_set="dpdp", client=ses).send(email(principal="u1"))
    assert receipt.status is DeliveryStatus.DELIVERED and receipt.provider_ref == "ses-1"
    assert receipt.to_masked != "asha@example.in" and receipt.delivered_at is not None
    [req] = ses.requests
    assert req["Destination"] == {"ToAddresses": ["asha@example.in"]} and req["ConfigurationSetName"] == "dpdp"
    assert req["Content"]["Simple"]["Body"]["Text"] == {"Data": "नमस्ते", "Charset": "UTF-8"}
    assert req["EmailTags"] == [{"Name": RECEIPT_TAG, "Value": receipt.id}]


def test_refuses_sms_and_reports_errors() -> None:
    ses = FakeSes()
    notifier = SesNotifier("privacy@example.in", client=ses)
    sms = notifier.send(Message(channel=Channel.SMS, to="+919800000000", subject="s", body="b", template="t"))
    assert sms.status is DeliveryStatus.FAILED and ses.requests == []
    ses.error = RuntimeError("MessageRejected: Email address is not verified")
    failed = notifier.send(email())
    assert failed.status is DeliveryStatus.FAILED and "not verified" in (failed.error or "")


def test_event_parsing() -> None:
    assert ses_event_outcome(ses_event("Delivery", "dr_1")) == ("dr_1", True, None)
    bounce = ses_event("Bounce", "dr_1", bounce={"bounceType": "Permanent"})
    assert ses_event_outcome(sns(bounce)) == ("dr_1", False, "ses bounce: Permanent")
    assert ses_event_outcome({"notificationType": "Delivery", "mail": {"tags": {RECEIPT_TAG: ["dr_2"]}}})[1]  # type: ignore[index]
    assert ses_event_outcome(ses_event("DeliveryDelay", "dr_1")) is None
    assert ses_event_outcome(ses_event("Complaint", "dr_1")) is None
    assert ses_event_outcome({"eventType": "Delivery", "mail": {"tags": {}}}) is None


@pytest.fixture
def tracked(make_kit: Callable[..., Kit]) -> tuple[Kit, FakeSes]:
    ses = FakeSes()
    kit = make_kit(notifier=SesNotifier("privacy@example.in", client=ses, track_delivery=True))
    kit.retention.register_handler(lambda principal, purposes: None)
    return kit, ses


def _warned_schedule(kit: Kit, clock: FakeClock) -> Any:
    kit.retention.record_activity("u1")
    clock.advance(days=28)
    assert kit.retention.run().warning_pending == 1
    return next(s for s in kit.retention.schedules("u1") if s.purpose == "analytics")


def test_tracked_warning_waits_for_ses_delivery(tracked: tuple[Kit, FakeSes], clock: FakeClock) -> None:
    kit, _ = tracked
    pending = _warned_schedule(kit, clock)
    assert pending.status is ScheduleStatus.WARNING_PENDING
    clock.advance(days=5)
    assert kit.retention.run().erased == 0  # accepted by SES is not enough

    sqs = FakeSqs(
        [
            "not json",
            sns(ses_event("DeliveryDelay", pending.warning_receipt_id)),
            sns(ses_event("Delivery", pending.warning_receipt_id)),
        ]
    )
    assert poll_ses_events(kit, "https://sqs.example/q", client=sqs) == 1
    assert sqs.deleted == ["h0", "h1", "h2"]
    warned = kit.repository.get_schedule(kit.tenant_id, pending.id)
    assert warned is not None and warned.status is ScheduleStatus.WARNED
    assert warned.erase_at == clock.now + kit.policy.erasure_pre_notice  # full window from delivery
    receipt = kit.repository.get_delivery(kit.tenant_id, pending.warning_receipt_id)
    assert receipt is not None and receipt.status is DeliveryStatus.DELIVERED


def test_bounced_warning_needs_attention(tracked: tuple[Kit, FakeSes], clock: FakeClock) -> None:
    kit, _ = tracked
    pending = _warned_schedule(kit, clock)
    bounce = ses_event("Bounce", pending.warning_receipt_id, bounce={"bounceType": "Permanent"})
    assert apply_ses_event(kit, bounce) == pending.warning_receipt_id
    s = kit.repository.get_schedule(kit.tenant_id, pending.id)
    assert s is not None and s.status is ScheduleStatus.NEEDS_ATTENTION and "bounce" in (s.attention_reason or "")


def test_other_tracked_receipts_are_updated(tracked: tuple[Kit, FakeSes]) -> None:
    kit, ses = tracked
    receipt = SesNotifier("privacy@example.in", client=ses, track_delivery=True).send(email())
    kit.repository.save_delivery(receipt.model_copy(update={"tenant_id": kit.tenant_id}))
    assert apply_ses_event(kit, ses_event("Delivery", receipt.id)) == receipt.id
    stored = kit.repository.get_delivery(kit.tenant_id, receipt.id)
    assert isinstance(stored, DeliveryReceipt) and stored.status is DeliveryStatus.DELIVERED
    assert apply_ses_event(kit, ses_event("Delivery", receipt.id)) is None  # already final
    assert apply_ses_event(kit, ses_event("Delivery", "dr_unknown")) is None
