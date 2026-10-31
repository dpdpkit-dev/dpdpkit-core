"""Amazon SES email transport (``pip install "dpdpkit-core[ses]"``).

By default a message SES accepts is reported as delivered, like :class:`~dpdpkit.notify.SmtpNotifier`.
With ``track_delivery=True`` it is reported as *queued* and tagged with the dpdpkit receipt id; the final
outcome comes from SES event publishing (configuration set → SNS → SQS) and is applied with
:func:`poll_ses_events`. A pre-erasure warning then only counts once SES reports the mailbox accepted it.

Events are read from SQS with your AWS credentials rather than received on a public URL: a forged
"delivered" event would start an erasure clock, so the channel must be authenticated.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ._util import mask_address, new_id, utcnow
from .errors import NotFound
from .models import Channel, DeliveryReceipt, DeliveryStatus, Message

if TYPE_CHECKING:
    from .kit import Kit

RECEIPT_TAG = "dpdpkit_receipt"
_FAILED_EVENTS = {"Bounce", "Reject", "Rendering Failure", "RenderingFailure"}


def _client(service: str, region: str | None) -> Any:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError('SES support needs boto3: pip install "dpdpkit-core[ses]"') from exc
    return boto3.client(service, region_name=region)


class SesNotifier:
    """Send email with the SES v2 API. SMS and WhatsApp messages are refused (failed receipt)."""

    def __init__(
        self,
        sender: str,
        *,
        region: str | None = None,
        configuration_set: str | None = None,
        track_delivery: bool = False,
        client: Any = None,
    ) -> None:
        self.sender = sender
        self.configuration_set = configuration_set
        self.track_delivery = track_delivery
        self._client = client
        self._region = region

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = _client("sesv2", self._region)
        return self._client

    def send(self, message: Message) -> DeliveryReceipt:
        receipt_id = new_id("dr")
        now = utcnow()

        def receipt(status: DeliveryStatus, ref: str | None = None, error: str | None = None) -> DeliveryReceipt:
            return DeliveryReceipt(
                id=receipt_id,
                principal=message.principal,
                channel=message.channel,
                to_masked=mask_address(message.to),
                template=message.template,
                status=status,
                provider="ses",
                provider_ref=ref,
                error=error,
                created_at=now,
                delivered_at=now if status is DeliveryStatus.DELIVERED else None,
            )

        if message.channel is not Channel.EMAIL:
            return receipt(DeliveryStatus.FAILED, error=f"ses cannot send {message.channel.value}")
        request: dict[str, Any] = {
            "FromEmailAddress": self.sender,
            "Destination": {"ToAddresses": [message.to]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": message.body, "Charset": "UTF-8"}},
                }
            },
            "EmailTags": [{"Name": RECEIPT_TAG, "Value": receipt_id}],
        }
        if self.configuration_set:
            request["ConfigurationSetName"] = self.configuration_set
        try:
            response = self.client.send_email(**request)
        except Exception as exc:  # botocore ClientError, credentials, network
            return receipt(DeliveryStatus.FAILED, error=repr(exc))
        status = DeliveryStatus.QUEUED if self.track_delivery else DeliveryStatus.DELIVERED
        return receipt(status, ref=str(response.get("MessageId", "")) or None)


def ses_event_outcome(event: Mapping[str, Any]) -> tuple[str, bool, str | None] | None:
    """Read an SES event (or an SNS envelope around one). Returns ``(receipt_id, delivered, error)``, or
    ``None`` when the event is not final (delay, open, complaint) or was not sent by dpdpkit."""
    if "Message" in event and isinstance(event["Message"], str):  # SNS envelope
        event = json.loads(event["Message"])
    kind = str(event.get("eventType") or event.get("notificationType") or "")
    tags = (event.get("mail") or {}).get("tags") or {}
    receipt_ids = tags.get(RECEIPT_TAG) or []
    if not receipt_ids:
        return None
    receipt_id = str(receipt_ids[0])
    if kind == "Delivery":
        return receipt_id, True, None
    if kind in _FAILED_EVENTS:
        detail = event.get("bounce") or event.get("reject") or event.get("failure") or {}
        reason = detail.get("bounceType") or detail.get("reason") or detail.get("errorMessage") or ""
        return receipt_id, False, f"ses {kind.lower()}" + (f": {reason}" if reason else "")
    return None


def apply_ses_event(kit: Kit, event: Mapping[str, Any]) -> str | None:
    """Record the final outcome of a tracked message. Pending pre-erasure warnings are confirmed or
    moved to ``needs_attention``; other receipts are updated in place. Returns the receipt id, if any."""
    outcome = ses_event_outcome(event)
    if outcome is None:
        return None
    receipt_id, delivered, error = outcome
    try:
        kit.retention.confirm_warning_delivery(receipt_id, delivered, error=error)
        return receipt_id
    except NotFound:
        pass  # not an erasure warning awaiting confirmation
    receipt = kit.repository.get_delivery(kit.tenant_id, receipt_id)
    if receipt is None or receipt.status is not DeliveryStatus.QUEUED:
        return None
    now = kit.now()
    kit.repository.save_delivery(
        receipt.model_copy(
            update={
                "status": DeliveryStatus.DELIVERED if delivered else DeliveryStatus.FAILED,
                "delivered_at": now if delivered else None,
                "error": error,
            }
        )
    )
    return receipt_id


def poll_ses_events(
    kit: Kit,
    queue_url: str,
    *,
    region: str | None = None,
    client: Any = None,
    max_batches: int = 10,
    wait_seconds: int = 0,
) -> int:
    """Drain SES events from an SQS queue (subscribed to the configuration set's SNS topic, raw or not).
    A message is deleted only after it has been applied. Returns how many outcomes were recorded."""
    sqs = client or _client("sqs", region)
    applied = 0
    for _ in range(max_batches):
        batch = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=wait_seconds)
        messages = batch.get("Messages") or []
        if not messages:
            break
        for msg in messages:
            try:
                body = json.loads(msg["Body"])
            except (KeyError, ValueError):
                body = {}
            if isinstance(body, Mapping) and apply_ses_event(kit, body):
                applied += 1
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
    return applied
