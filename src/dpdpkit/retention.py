"""Erasure schedules, pre-erasure warnings, legal holds and processor fan-out (DPDP Rules, Rule 8).

Fail-safe rules enforced here:

* An erasure runs only after its warning is recorded as **delivered**, and only once the full warning
  period (``retention.erasure_pre_notice_hours``) has passed since delivery.
* A failed or impossible warning, or a failing erasure handler, moves the schedule to
  ``needs_attention``. Nothing is retried destructively without a person resolving it.
* Activity by the principal after the warning cancels an inactivity-based erasure.
* A legal hold blocks warning and erasure until it is released.
* Ledger and audit rows are never erased; they are kept at least ``min_log_retention_days``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ._util import new_id
from .errors import InvalidTransition, NotFound, ValidationFailed
from .events import (
    ERASURE_COMPLETED,
    ERASURE_DUE,
    ERASURE_NEEDS_ATTENTION,
    ERASURE_PROCESSOR_NOTIFIED,
    ERASURE_WARNING_SENT,
)
from .models import (
    ACTIVE_SCHEDULE_STATUSES,
    Channel,
    DeliveryReceipt,
    DeliveryStatus,
    ErasureRun,
    ErasureSchedule,
    LegalBasis,
    LegalHold,
    Message,
    PreviewItem,
    Purpose,
    RetentionTrigger,
    ScheduleStatus,
)
from .notify import NullNotifier, render

if TYPE_CHECKING:
    from .kit import Kit
    from .repository import Repository

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ErasureHandler = Callable[[str, list[str]], None]
"""``handler(principal, purposes)`` deletes or anonymises the principal's data. Raise to signal failure."""

_RESCHEDULE_TOLERANCE = dt.timedelta(hours=1)
_RETAINED_BASES = {LegalBasis.S7_D_LEGAL_OBLIGATION, LegalBasis.S7_E_COURT_ORDER}


@dataclass
class ErasureResult:
    erased_purposes: list[str] = field(default_factory=list)
    held_purposes: list[str] = field(default_factory=list)
    retained_purposes: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class RetentionService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit
        self._handlers: list[ErasureHandler] = []

    # ------------------------------------------------------------------ configuration

    def register_handler(self, handler: ErasureHandler) -> ErasureHandler:
        """Register a function that erases a principal's data for the given purposes."""
        self._handlers.append(handler)
        return handler

    @property
    def _repo(self) -> Repository:
        return self._kit.repository

    @property
    def _pre_notice(self) -> dt.timedelta:
        return self._kit.policy.erasure_pre_notice

    # ------------------------------------------------------------------ helpers

    def _erase_at(self, purpose: Purpose, anchor: dt.datetime) -> dt.datetime | None:
        rule = purpose.retention
        if rule is None:
            return None
        if rule.third_schedule_class:
            _, period = self._kit.policy.third_schedule(rule.third_schedule_class)
            start = dt.datetime.combine(self._kit.policy.fiduciary_duties_start, dt.time(0), tzinfo=IST)
            return max(anchor, start) + period
        if rule.period_days is None:
            return None
        return anchor + dt.timedelta(days=rule.period_days)

    def _audit(self, action: str, s: ErasureSchedule, actor: str = "system", **data: object) -> None:
        self._kit.ledger.audit(
            action,
            subject_type="erasure_schedule",
            subject_id=s.id,
            actor=actor,
            data={"principal": s.principal, "purpose": s.purpose, "status": s.status.value, **data},
        )

    def _store(self, s: ErasureSchedule, action: str, actor: str = "system", **data: object) -> ErasureSchedule:
        s = s.model_copy(update={"updated_at": self._kit.now()})
        with self._repo.transaction():
            self._audit(action, s, actor, **data)
            self._repo.save_schedule(s)
        return s

    def _publish(self, event_type: str, s: ErasureSchedule, **data: object) -> None:
        self._kit.events.publish(
            event_type,
            self._kit.tenant_id,
            {"schedule_id": s.id, "principal": s.principal, "purpose": s.purpose, **data},
            self._kit.now(),
        )

    def _create(
        self, principal: str, purpose: str, trigger: RetentionTrigger, erase_at: dt.datetime, reason: str, actor: str
    ) -> ErasureSchedule:
        now = self._kit.now()
        # A warning must always fit before erasure, however short the configured period.
        erase_at = max(erase_at, now + self._pre_notice)
        s = ErasureSchedule(
            tenant_id=self._kit.tenant_id,
            id=new_id("es"),
            principal=principal,
            purpose=purpose,
            trigger=trigger,
            status=ScheduleStatus.SCHEDULED,
            erase_at=erase_at,
            warn_at=erase_at - self._pre_notice,
            reason=reason,
            created_at=now,
            updated_at=now,
        )
        return self._store(s, "retention.scheduled", actor, erase_at=erase_at.isoformat(), reason=reason)

    def _active(self, principal: str, purpose: str | None = None) -> list[ErasureSchedule]:
        return [
            s
            for s in self._repo.list_schedules(
                self._kit.tenant_id, principal=principal, statuses=ACTIVE_SCHEDULE_STATUSES
            )
            if purpose is None or s.purpose == purpose
        ]

    def _cancel(self, s: ErasureSchedule, reason: str, actor: str = "system") -> ErasureSchedule:
        s = s.model_copy(update={"status": ScheduleStatus.CANCELLED, "cancelled_reason": reason})
        return self._store(s, "retention.erasure.cancelled", actor, reason=reason)

    def _needs_attention(self, s: ErasureSchedule, reason: str) -> ErasureSchedule:
        s = s.model_copy(update={"status": ScheduleStatus.NEEDS_ATTENTION, "attention_reason": reason})
        s = self._store(s, "retention.needs_attention", reason=reason)
        self._publish(ERASURE_NEEDS_ATTENTION, s, reason=reason)
        return s

    # ------------------------------------------------------------------ triggers

    def record_activity(self, principal: str, at: dt.datetime | None = None) -> list[ErasureSchedule]:
        """The principal logged in or contacted the fiduciary. Restarts inactivity clocks and cancels
        any inactivity erasure that has already been warned about (Rule 8(2))."""
        at = at or self._kit.now()
        self._repo.record_activity(self._kit.tenant_id, principal, at)
        out: list[ErasureSchedule] = []
        for purpose in self._kit.registry.purposes():
            rule = purpose.retention
            if rule is None or rule.trigger is not RetentionTrigger.LAST_ACTIVITY:
                continue
            erase_at = self._erase_at(purpose, at)
            if erase_at is None:
                continue
            active = [s for s in self._active(principal, purpose.id) if s.trigger is RetentionTrigger.LAST_ACTIVITY]
            if not active:
                out.append(self._create(principal, purpose.id, rule.trigger, erase_at, "inactivity", principal))
                continue
            for s in active:
                warned = s.status in (
                    ScheduleStatus.WARNED,
                    ScheduleStatus.WARNING_PENDING,
                    ScheduleStatus.NEEDS_ATTENTION,
                ) or (s.status is ScheduleStatus.HELD and s.warned_at is not None)
                if warned:
                    self._cancel(s, "principal activity", principal)
                    out.append(self._create(principal, purpose.id, rule.trigger, erase_at, "inactivity", principal))
                elif abs(s.erase_at - erase_at) > _RESCHEDULE_TOLERANCE:
                    # Moving an unwarned clock forward is routine and not audited row-by-row.
                    moved = s.model_copy(
                        update={"erase_at": erase_at, "warn_at": erase_at - self._pre_notice, "updated_at": at}
                    )
                    self._repo.save_schedule(moved)
                    out.append(moved)
        return out

    def on_withdrawal(self, principal: str, purpose_id: str) -> ErasureSchedule | None:
        purpose = self._kit.registry.purpose(purpose_id)
        rule = purpose.retention
        if rule is None or rule.trigger is not RetentionTrigger.WITHDRAWAL:
            return None
        erase_at = self._kit.now() + dt.timedelta(days=rule.period_days or 0)
        return self._create(principal, purpose_id, rule.trigger, erase_at, "consent withdrawn", principal)

    def purpose_completed(self, principal: str, purpose_id: str, *, actor: str = "system") -> ErasureSchedule | None:
        purpose = self._kit.registry.purpose(purpose_id)
        rule = purpose.retention
        if rule is None or rule.trigger is not RetentionTrigger.PURPOSE_COMPLETE:
            return None
        erase_at = self._erase_at(purpose, self._kit.now()) or self._kit.now()
        return self._create(principal, purpose_id, rule.trigger, erase_at, "purpose completed", actor)

    def schedule(
        self, principal: str, purpose_id: str, erase_at: dt.datetime, *, reason: str, actor: str
    ) -> ErasureSchedule:
        self._kit.registry.purpose(purpose_id)
        return self._create(principal, purpose_id, RetentionTrigger.PURPOSE_COMPLETE, erase_at, reason, actor)

    # ------------------------------------------------------------------ legal holds

    def place_hold(self, principal: str, reason: str, *, actor: str, purpose: str | None = None) -> LegalHold:
        if not reason.strip():
            raise ValidationFailed("a legal hold needs a reason")
        if purpose is not None:
            self._kit.registry.purpose(purpose)
        hold = LegalHold(
            tenant_id=self._kit.tenant_id,
            id=new_id("lh"),
            principal=principal,
            purpose=purpose,
            reason=reason,
            placed_by=actor,
            placed_at=self._kit.now(),
        )
        with self._repo.transaction():
            self._kit.ledger.audit(
                "retention.hold.placed",
                subject_type="legal_hold",
                subject_id=hold.id,
                actor=actor,
                data={"principal": principal, "purpose": purpose, "reason": reason},
            )
            self._repo.save_hold(hold)
        return hold

    def release_hold(self, hold_id: str, *, actor: str) -> LegalHold:
        hold = next((h for h in self._repo.list_holds(self._kit.tenant_id) if h.id == hold_id), None)
        if hold is None:
            raise NotFound(f"active legal hold {hold_id} not found")
        hold = hold.model_copy(update={"released_at": self._kit.now()})
        with self._repo.transaction():
            self._kit.ledger.audit(
                "retention.hold.released",
                subject_type="legal_hold",
                subject_id=hold.id,
                actor=actor,
                data={"principal": hold.principal, "purpose": hold.purpose},
            )
            self._repo.save_hold(hold)
        return hold

    def holds(self, principal: str | None = None) -> list[LegalHold]:
        return self._repo.list_holds(self._kit.tenant_id, principal=principal)

    @staticmethod
    def _held(purpose: str, holds: Iterable[LegalHold]) -> bool:
        return any(h.purpose is None or h.purpose == purpose for h in holds)

    # ------------------------------------------------------------------ the run

    def run(self, now: dt.datetime | None = None) -> ErasureRun:
        """Send due warnings and perform due erasures. Safe to call as often as you like."""
        now = now or self._kit.now()
        run = ErasureRun(tenant_id=self._kit.tenant_id, id=new_id("er"), started_at=now, finished_at=now)
        counts = {"warned": 0, "warning_pending": 0, "erased": 0, "held": 0, "needs_attention": 0, "cancelled": 0}
        for s in list(self._repo.due_erasures(self._kit.tenant_id, now)):
            holds = self.holds(s.principal)
            if self._held(s.purpose, holds):
                if s.status is not ScheduleStatus.HELD:
                    s = self._store(s.model_copy(update={"status": ScheduleStatus.HELD}), "retention.held")
                counts["held"] += 1
                continue
            if s.status is ScheduleStatus.HELD:
                resumed = ScheduleStatus.WARNED if s.warned_at else ScheduleStatus.SCHEDULED
                s = self._store(s.model_copy(update={"status": resumed}), "retention.hold_cleared")
            if s.status is ScheduleStatus.SCHEDULED and now >= s.warn_at:
                s = self._warn(s, now)
            elif s.status is ScheduleStatus.WARNED and now >= s.erase_at:
                s = self._erase(s, now)
            else:
                continue
            key = {
                ScheduleStatus.WARNED: "warned",
                ScheduleStatus.WARNING_PENDING: "warning_pending",
                ScheduleStatus.ERASED: "erased",
                ScheduleStatus.NEEDS_ATTENTION: "needs_attention",
                ScheduleStatus.CANCELLED: "cancelled",
            }.get(s.status)
            if key:
                counts[key] += 1
        run = run.model_copy(update={**counts, "finished_at": self._kit.now()})
        if any(counts.values()):
            self._kit.ledger.audit("retention.run", subject_type="erasure_run", subject_id=run.id, data=dict(counts))
        return run

    def _warn(self, s: ErasureSchedule, now: dt.datetime) -> ErasureSchedule:
        resolver = self._kit.contact_resolver
        contact = resolver(s.principal) if resolver else None
        channel, to = None, None
        if contact is not None:
            for ch, addr in (
                (Channel.EMAIL, contact.email),
                (Channel.SMS, contact.phone),
                (Channel.WHATSAPP, contact.whatsapp),
            ):
                if addr:
                    channel, to = ch, addr
                    break
        if channel is None or to is None:
            return self._needs_attention(s, "no contact details to send the pre-erasure warning")

        purpose = self._kit.registry.purpose(s.purpose)
        locale = (contact.locale if contact else None) or self._kit.policy.default_language
        subject, body = render(
            "erasure_warning",
            locale,
            {
                "fiduciary": self._kit.contact.get("name", "us"),
                "purpose": purpose.title,
                "erase_at": s.erase_at.astimezone(IST).strftime("%d %b %Y %H:%M IST"),
                "contact": self._kit.contact.get("email", ""),
                "rights_url": self._kit.contact.get("rights_url", ""),
            },
        )
        message = Message(
            channel=channel,
            to=to,
            subject=subject,
            body=body,
            template="erasure_warning",
            locale=locale,
            principal=s.principal,
        )
        receipt = self._send(message)
        if receipt.status is DeliveryStatus.DELIVERED:
            return self._mark_warned(s, receipt.delivered_at or now, receipt)
        if receipt.status is DeliveryStatus.QUEUED:
            s = s.model_copy(update={"status": ScheduleStatus.WARNING_PENDING, "warning_receipt_id": receipt.id})
            return self._store(s, "retention.warning.queued", receipt_id=receipt.id)
        s = s.model_copy(update={"warning_receipt_id": receipt.id})
        return self._needs_attention(s, f"pre-erasure warning not delivered: {receipt.error or 'unknown error'}")

    def _send(self, message: Message) -> DeliveryReceipt:
        try:
            receipt = self._kit.notifier.send(message)
        except Exception as exc:
            receipt = NullNotifier().send(message).model_copy(update={"error": repr(exc), "provider": "error"})
        # The kit's clock is authoritative for every deadline, so receipts are stamped with it.
        now = self._kit.now()
        receipt = receipt.model_copy(
            update={
                "tenant_id": self._kit.tenant_id,
                "created_at": now,
                "delivered_at": now if receipt.status is DeliveryStatus.DELIVERED else None,
            }
        )
        self._repo.save_delivery(receipt)
        return receipt

    def _mark_warned(self, s: ErasureSchedule, delivered_at: dt.datetime, receipt: DeliveryReceipt) -> ErasureSchedule:
        s = s.model_copy(
            update={
                "status": ScheduleStatus.WARNED,
                "warned_at": delivered_at,
                "warning_receipt_id": receipt.id,
                # The principal always gets the full warning period from the moment of delivery.
                "erase_at": max(s.erase_at, delivered_at + self._pre_notice),
            }
        )
        s = self._store(s, "retention.warning.delivered", receipt_id=receipt.id, erase_at=s.erase_at.isoformat())
        self._publish(ERASURE_WARNING_SENT, s, erase_at=s.erase_at.isoformat())
        return s

    def confirm_warning_delivery(
        self, receipt_id: str, delivered: bool, *, at: dt.datetime | None = None, error: str | None = None
    ) -> ErasureSchedule:
        """Called when a queued transport reports the final outcome of a warning."""
        pending = self._repo.list_schedules(self._kit.tenant_id, statuses=[ScheduleStatus.WARNING_PENDING])
        s = next((x for x in pending if x.warning_receipt_id == receipt_id), None)
        if s is None:
            raise NotFound(f"no pending warning with receipt {receipt_id}")
        receipt = self._repo.get_delivery(self._kit.tenant_id, receipt_id)
        when = at or self._kit.now()
        if receipt is not None:
            receipt = receipt.model_copy(
                update={
                    "status": DeliveryStatus.DELIVERED if delivered else DeliveryStatus.FAILED,
                    "delivered_at": when if delivered else None,
                    "error": error,
                }
            )
            self._repo.save_delivery(receipt)
        if delivered and receipt is not None:
            return self._mark_warned(s, when, receipt)
        return self._needs_attention(s, f"pre-erasure warning not delivered: {error or 'transport reported failure'}")

    def _erase(self, s: ErasureSchedule, now: dt.datetime) -> ErasureSchedule:
        if s.warned_at is None or now < s.warned_at + self._pre_notice:
            return s  # defensive: never erase inside the warning window
        if s.trigger is RetentionTrigger.LAST_ACTIVITY:
            last = self._repo.last_activity(self._kit.tenant_id, s.principal)
            if last is not None and last > s.warned_at:
                return self._cancel(s, "principal activity after warning")
        self._audit("retention.erasure.started", s)
        self._publish(ERASURE_DUE, s)
        errors = self._run_handlers(s.principal, [s.purpose])
        if errors:
            return self._needs_attention(s, "erasure handler failed: " + "; ".join(errors))
        fanout = self._fanout(s.principal, s.purpose)
        s = s.model_copy(
            update={"status": ScheduleStatus.ERASED, "erased_at": self._kit.now(), "processor_fanout": fanout}
        )
        s = self._store(s, "retention.erasure.completed", processors=sorted(fanout))
        self._publish(ERASURE_COMPLETED, s)
        return s

    def _run_handlers(self, principal: str, purposes: list[str]) -> list[str]:
        if not self._handlers:
            return ["no erasure handler registered"]
        errors = []
        for handler in self._handlers:
            try:
                handler(principal, list(purposes))
            except Exception as exc:
                errors.append(f"{getattr(handler, '__name__', 'handler')}: {exc!r}")
        return errors

    def _fanout(self, principal: str, purpose: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for proc in self._kit.registry.processors_for(purpose):
            self._kit.events.publish(
                ERASURE_PROCESSOR_NOTIFIED,
                self._kit.tenant_id,
                {"principal": principal, "purpose": purpose, "processor": proc.id, "webhook": proc.erasure_webhook},
                self._kit.now(),
            )
            out[proc.id] = "notified"
        return out

    # ------------------------------------------------------------------ erasure on request

    def erase_now(
        self, principal: str, *, reason: str, actor: str, purposes: Iterable[str] | None = None
    ) -> ErasureResult:
        """Erase immediately because the principal asked (rights request). No warning is needed,
        but legal holds and purposes retained under a legal obligation are respected."""
        wanted = list(purposes) if purposes is not None else [p.id for p in self._kit.registry.purposes()]
        holds = self.holds(principal)
        result = ErasureResult()
        for pid in wanted:
            purpose = self._kit.registry.purpose(pid)
            if self._held(pid, holds):
                result.held_purposes.append(pid)
            elif purpose.legal_basis in _RETAINED_BASES:
                result.retained_purposes.append(pid)
            else:
                result.erased_purposes.append(pid)
        if not result.erased_purposes:
            return result
        self._kit.ledger.audit(
            "retention.erasure.started",
            subject_type="principal",
            subject_id=principal,
            actor=actor,
            data={"purposes": result.erased_purposes, "reason": reason},
        )
        result.failed = self._run_handlers(principal, result.erased_purposes)
        if result.failed:
            self._kit.ledger.audit(
                "retention.erasure.failed",
                subject_type="principal",
                subject_id=principal,
                actor=actor,
                data={"errors": result.failed},
            )
            return result
        for pid in result.erased_purposes:
            self._fanout(principal, pid)
            for s in self._active(principal, pid):
                self._cancel(s, f"erased on request ({reason})", actor)
        self._kit.ledger.audit(
            "retention.erasure.completed",
            subject_type="principal",
            subject_id=principal,
            actor=actor,
            data={
                "purposes": result.erased_purposes,
                "held": result.held_purposes,
                "retained": result.retained_purposes,
            },
        )
        return result

    # ------------------------------------------------------------------ admin views

    def preview(
        self, now: dt.datetime | None = None, horizon: dt.timedelta = dt.timedelta(days=7)
    ) -> list[PreviewItem]:
        now = now or self._kit.now()
        until = now + horizon
        items: list[PreviewItem] = []
        for s in self._repo.list_schedules(self._kit.tenant_id, statuses=ACTIVE_SCHEDULE_STATUSES):
            if s.status is ScheduleStatus.SCHEDULED and s.warn_at <= until:
                items.append(
                    PreviewItem(
                        schedule_id=s.id,
                        principal=s.principal,
                        purpose=s.purpose,
                        action="warn",
                        at=max(s.warn_at, now),
                        status=s.status,
                    )
                )
            if s.status in (ScheduleStatus.SCHEDULED, ScheduleStatus.WARNED) and s.erase_at <= until:
                items.append(
                    PreviewItem(
                        schedule_id=s.id,
                        principal=s.principal,
                        purpose=s.purpose,
                        action="erase",
                        at=max(s.erase_at, now),
                        status=s.status,
                    )
                )
        return sorted(items, key=lambda i: (i.at, i.action))

    def schedules(self, principal: str | None = None, *, active_only: bool = True) -> list[ErasureSchedule]:
        statuses = ACTIVE_SCHEDULE_STATUSES if active_only else None
        return self._repo.list_schedules(self._kit.tenant_id, principal=principal, statuses=statuses)

    def needs_attention(self) -> list[ErasureSchedule]:
        return self._repo.list_schedules(self._kit.tenant_id, statuses=[ScheduleStatus.NEEDS_ATTENTION])

    def resolve(
        self, schedule_id: str, action: Literal["retry", "cancel"], *, actor: str, reason: str = ""
    ) -> ErasureSchedule:
        """A person decides what happens to a ``needs_attention`` schedule."""
        s = self._repo.get_schedule(self._kit.tenant_id, schedule_id)
        if s is None:
            raise NotFound(f"schedule {schedule_id} not found")
        if s.status is not ScheduleStatus.NEEDS_ATTENTION:
            raise InvalidTransition(f"schedule {schedule_id} is {s.status.value}, not needs_attention")
        if action == "cancel":
            return self._cancel(s, reason or "cancelled by administrator", actor)
        now = self._kit.now()
        # Retrying starts over with a fresh warning; erase time moves so the full period is kept.
        s = s.model_copy(
            update={
                "status": ScheduleStatus.SCHEDULED,
                "attention_reason": None,
                "warned_at": None,
                "warning_receipt_id": None,
                "warn_at": now,
                "erase_at": max(s.erase_at, now + self._pre_notice),
            }
        )
        return self._store(s, "retention.retry", actor, reason=reason)

    # ------------------------------------------------------------------ log floor

    def log_floor(self, occurred_at: dt.datetime) -> dt.datetime:
        """Earliest time a ledger, audit or processing-log row written at ``occurred_at`` may be purged."""
        return occurred_at + self._kit.policy.min_log_retention

    def log_purgeable(self, occurred_at: dt.datetime, now: dt.datetime | None = None) -> bool:
        return (now or self._kit.now()) >= self.log_floor(occurred_at)
