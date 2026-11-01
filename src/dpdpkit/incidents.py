"""Personal data breach register, reporting clocks, report drafts and bulk principal notices (DPDP Rules, Rule 7).

Fail-safe rules enforced here:

* **Drafts only.** :meth:`IncidentService.draft_report` returns text for a person to review and submit.
  Nothing in dpdpkit sends anything to the Board or to CERT-In. A person submits, then calls
  :meth:`IncidentService.mark_reported` to start the record straight.
* Every deadline counts from ``aware_at``, the moment the fiduciary became aware, not from detection.
* The Board's first intimation has no number of hours ("without delay"), so its clock never turns
  ``overdue``; the 72-hour detailed report does. The CERT-In clock exists only when the policy pack
  (normally the ``cert-in-2022`` overlay) sets ``breach.cert_in_report_hours``.
* Principal notices never raise on a bad address or a failing transport. Each outcome is stored, and a
  re-run retries only principals who have not been reached.
* Audit rows describe counts and ids, never the personal data of the principals notified.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ._util import new_id
from .errors import ConfigError, InvalidTransition, NotFound, ValidationFailed
from .events import INCIDENT_CLOSED, INCIDENT_PRINCIPALS_NOTIFIED, INCIDENT_RECORDED, INCIDENT_REPORTED
from .models import (
    Channel,
    ClockState,
    DeliveryReceipt,
    DeliveryStatus,
    Incident,
    IncidentClock,
    IncidentReport,
    IncidentSeverity,
    IncidentStatus,
    Message,
    NoticeStatus,
    PrincipalNotice,
    ReportKind,
)
from .notify import NullNotifier, render
from .repository import IncidentRepository

if TYPE_CHECKING:
    from .kit import Kit

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_UPDATABLE = {
    "title",
    "description",
    "severity",
    "affected_count",
    "data_items",
    "root_cause",
    "remediation",
    "likely_consequences",
    "safety_steps",
}
_REACHED = {NoticeStatus.SENT, NoticeStatus.QUEUED}
_TODO = "[TO COMPLETE: {}]"


def _ist(value: dt.datetime) -> str:
    return value.astimezone(IST).strftime("%d %b %Y %H:%M IST")


class IncidentService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit

    @property
    def _repo(self) -> IncidentRepository:
        repo = self._kit.repository
        if not isinstance(repo, IncidentRepository):
            raise ConfigError("repository does not implement dpdpkit.repository.IncidentRepository")
        return repo

    # ------------------------------------------------------------------ helpers

    def _audit(self, action: str, inc: Incident, actor: str, **data: object) -> None:
        self._kit.ledger.audit(
            action,
            subject_type="incident",
            subject_id=inc.id,
            actor=actor,
            data={"status": inc.status.value, **data},
        )

    def _store(self, inc: Incident, action: str, actor: str, **data: object) -> Incident:
        inc = inc.model_copy(update={"updated_at": self._kit.now()})
        with self._kit.repository.transaction():
            self._audit(action, inc, actor, **data)
            self._repo.save_incident(inc)
        return inc

    def _publish(self, event_type: str, inc: Incident, **data: object) -> None:
        self._kit.events.publish(
            event_type,
            self._kit.tenant_id,
            {"incident_id": inc.id, "severity": inc.severity.value, "status": inc.status.value, **data},
            self._kit.now(),
        )

    # ------------------------------------------------------------------ register

    def record(
        self,
        title: str,
        *,
        actor: str,
        detected_at: dt.datetime | None = None,
        aware_at: dt.datetime | None = None,
        severity: IncidentSeverity = IncidentSeverity.MEDIUM,
        description: str = "",
        affected_count: int | None = None,
        data_items: Iterable[str] = (),
        root_cause: str = "",
        remediation: str = "",
        likely_consequences: str = "",
        safety_steps: str = "",
    ) -> Incident:
        """Open a breach record. ``aware_at`` (default: ``detected_at``, default: now) starts every clock."""
        if not title.strip():
            raise ValidationFailed("an incident needs a title")
        if affected_count is not None and affected_count < 0:
            raise ValidationFailed("affected_count cannot be negative")
        now = self._kit.now()
        detected = detected_at or aware_at or now
        aware = aware_at or detected
        if max(detected, aware) > now:
            raise ValidationFailed("an incident cannot be detected or known in the future")
        inc = Incident(
            tenant_id=self._kit.tenant_id,
            id=new_id("inc"),
            title=title.strip(),
            description=description,
            severity=severity,
            detected_at=detected,
            aware_at=aware,
            affected_count=affected_count,
            data_items=list(data_items),
            root_cause=root_cause,
            remediation=remediation,
            likely_consequences=likely_consequences,
            safety_steps=safety_steps,
            created_by=actor,
            created_at=now,
            updated_at=now,
        )
        inc = self._store(inc, "incident.recorded", actor, severity=severity.value, aware_at=aware.isoformat())
        self._publish(INCIDENT_RECORDED, inc)
        return inc

    def get(self, incident_id: str) -> Incident:
        inc = self._repo.get_incident(self._kit.tenant_id, incident_id)
        if inc is None:
            raise NotFound(f"incident {incident_id} not found")
        return inc

    def list_incidents(self, *, open_only: bool = False) -> list[Incident]:
        status = IncidentStatus.OPEN if open_only else None
        return self._repo.list_incidents(self._kit.tenant_id, status=status)

    def update(self, incident_id: str, *, actor: str, **fields: object) -> Incident:
        """Fill in or correct facts as the investigation proceeds. Only descriptive fields can change."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValidationFailed(f"cannot update {', '.join(sorted(unknown))}")
        inc = self.get(incident_id)
        if inc.status is IncidentStatus.CLOSED:
            raise InvalidTransition(f"incident {incident_id} is closed")
        count = fields.get("affected_count")
        if isinstance(count, int) and count < 0:
            raise ValidationFailed("affected_count cannot be negative")
        if "title" in fields and not str(fields["title"]).strip():
            raise ValidationFailed("an incident needs a title")
        if "data_items" in fields:
            fields["data_items"] = list(fields["data_items"])  # type: ignore[call-overload]
        inc = inc.model_copy(update=fields)
        inc = Incident.model_validate(inc.model_dump())
        return self._store(inc, "incident.updated", actor, fields=sorted(fields))

    def close(self, incident_id: str, *, actor: str) -> Incident:
        inc = self.get(incident_id)
        if inc.status is IncidentStatus.CLOSED:
            raise InvalidTransition(f"incident {incident_id} is already closed")
        now = self._kit.now()
        inc = inc.model_copy(update={"status": IncidentStatus.CLOSED, "closed_at": now})
        inc = self._store(inc, "incident.closed", actor)
        self._publish(INCIDENT_CLOSED, inc)
        return inc

    # ------------------------------------------------------------------ clocks

    def clocks(self, incident_id: str, now: dt.datetime | None = None) -> list[IncidentClock]:
        return self._clocks(self.get(incident_id), now or self._kit.now())

    def _clocks(self, inc: Incident, now: dt.datetime) -> list[IncidentClock]:
        policy = self._kit.policy
        rows: list[tuple[ReportKind, dt.datetime | None, dt.datetime | None]] = [
            (ReportKind.BOARD_INITIAL, None, inc.board_initial_sent_at),
            (ReportKind.BOARD_DETAILED, inc.aware_at + policy.board_report_window, inc.board_detailed_sent_at),
        ]
        cert_hours = policy.get("breach.cert_in_report_hours", None)
        if cert_hours is not None:
            cert_due = inc.aware_at + dt.timedelta(hours=int(cert_hours))
            rows.append((ReportKind.CERT_IN, cert_due, inc.certin_reported_at))
        out = []
        for kind, due, done in rows:
            if done is not None:
                state, remaining = ClockState.DONE, None
            elif due is None:
                state, remaining = ClockState.PENDING, None
            else:
                remaining = due - now
                state = ClockState.OVERDUE if remaining < dt.timedelta(0) else ClockState.PENDING
            out.append(IncidentClock(kind=kind, due_at=due, done_at=done, state=state, remaining=remaining))
        return out

    def overdue(self, now: dt.datetime | None = None) -> list[tuple[Incident, IncidentClock]]:
        """Every open incident with a clock past its deadline, for a daily job or an admin dashboard."""
        now = now or self._kit.now()
        return [
            (inc, clock)
            for inc in self.list_incidents(open_only=True)
            for clock in self._clocks(inc, now)
            if clock.state is ClockState.OVERDUE
        ]

    def mark_reported(
        self,
        incident_id: str,
        kind: ReportKind,
        *,
        actor: str,
        at: dt.datetime | None = None,
        reference: str | None = None,
    ) -> Incident:
        """Record that a person has submitted a report. dpdpkit never submits anything itself."""
        inc = self.get(incident_id)
        field = {
            ReportKind.BOARD_INITIAL: "board_initial_sent_at",
            ReportKind.BOARD_DETAILED: "board_detailed_sent_at",
            ReportKind.CERT_IN: "certin_reported_at",
        }[kind]
        if kind is ReportKind.CERT_IN and self._kit.policy.get("breach.cert_in_report_hours", None) is None:
            raise InvalidTransition("this policy has no CERT-In clock; enable the cert-in-2022 overlay")
        if getattr(inc, field) is not None:
            raise InvalidTransition(f"{kind.value} already recorded for incident {incident_id}")
        when = at or self._kit.now()
        if when > self._kit.now():
            raise ValidationFailed("a report cannot have been sent in the future")
        if when < inc.aware_at:
            raise ValidationFailed("a report cannot have been sent before the fiduciary was aware of the breach")
        refs = {**inc.report_references, **({kind.value: reference} if reference else {})}
        inc = inc.model_copy(update={field: when, "report_references": refs})
        inc = self._store(inc, "incident.reported", actor, kind=kind.value, at=when.isoformat())
        self._publish(INCIDENT_REPORTED, inc, kind=kind.value)
        return inc

    # ------------------------------------------------------------------ report drafts

    def draft_report(self, incident_id: str, kind: ReportKind) -> IncidentReport:
        """A draft for review. Facts the record lacks appear as ``[TO COMPLETE: ...]`` and in ``missing``."""
        inc = self.get(incident_id)
        missing: list[str] = []

        def need(label: str, value: str) -> str:
            if value.strip():
                return value.strip()
            missing.append(label)
            return _TODO.format(label)

        fiduciary = self._kit.contact.get("name", "the data fiduciary")
        facts = [
            f"Incident: {inc.title} ({inc.id})",
            f"Detected: {_ist(inc.detected_at)}",
            f"Fiduciary became aware: {_ist(inc.aware_at)}",
            f"Severity: {inc.severity.value}",
        ]
        count = "unknown so far" if inc.affected_count is None else str(inc.affected_count)
        if inc.affected_count is None:
            missing.append("number of affected principals")
        items = ", ".join(inc.data_items) if inc.data_items else need("personal data involved", "")
        if kind is ReportKind.BOARD_INITIAL:
            title = "Intimation of personal data breach to the Data Protection Board of India"
            sections = [
                ("Nature, extent, timing and location", need("description of the breach", inc.description)),
                ("Personal data involved", items),
                ("Principals affected", count),
                ("Likely impact", need("likely impact on principals", inc.likely_consequences)),
            ]
        elif kind is ReportKind.BOARD_DETAILED:
            title = "Detailed report of personal data breach to the Data Protection Board of India"
            notices = self._repo.list_principal_notices(self._kit.tenant_id, inc.id)
            reached = len({n.principal for n in notices if n.status in _REACHED})
            if reached:
                intimation = f"{reached} affected principals were notified; job {inc.principal_notice_job}."
            else:
                intimation = need("intimation given to affected principals", "")
            sections = [
                ("Events, circumstances and reasons leading to the breach", need("description", inc.description)),
                ("Root cause and findings about who or what caused it", need("root cause", inc.root_cause)),
                ("Personal data involved", items),
                ("Principals affected", count),
                ("Measures implemented or proposed to mitigate risk", need("remediation", inc.remediation)),
                ("Intimation given to affected principals", intimation),
            ]
            if inc.board_initial_sent_at is None:
                missing.append("Board initial intimation not yet recorded")
                facts.append(_TODO.format("the initial intimation has not been recorded as sent"))
            else:
                facts.append(f"Initial intimation sent: {_ist(inc.board_initial_sent_at)}")
        else:
            title = "CERT-In incident report"
            sections = [
                ("Description of the incident", need("description of the incident", inc.description)),
                ("Information or systems affected", items),
                ("Impact", need("impact", inc.likely_consequences)),
                ("Actions taken", need("actions taken", inc.remediation)),
            ]
        lines = [f"{title}", f"Reporting entity: {fiduciary}", "", *facts, ""]
        for heading, text in sections:
            lines += [f"{heading}:", text, ""]
        contact = self._kit.contact.get("email")
        lines.append(f"Contact: {contact}" if contact else _TODO.format("contact for follow-up"))
        if not contact:
            missing.append("contact for follow-up")
        lines += ["", "DRAFT. Review before submitting. dpdpkit does not send this to any authority."]
        return IncidentReport(
            incident_id=inc.id,
            kind=kind,
            title=title,
            body="\n".join(lines),
            missing=missing,
            generated_at=self._kit.now(),
        )

    # ------------------------------------------------------------------ principal notices

    def notices(self, incident_id: str) -> list[PrincipalNotice]:
        self.get(incident_id)
        return self._repo.list_principal_notices(self._kit.tenant_id, incident_id)

    def notify_principals(self, incident_id: str, principals: Iterable[str], *, actor: str) -> list[PrincipalNotice]:
        """Tell each affected principal about the breach (Rule 7(1)). Safe to re-run with the same list:
        principals already reached are skipped, and ones that failed or had no contact details are retried.
        Returns the notices made in this run."""
        inc = self.get(incident_id)
        previous = self._repo.list_principal_notices(self._kit.tenant_id, inc.id)
        reached = {n.principal for n in previous if n.status in _REACHED}
        todo = list(dict.fromkeys(p for p in principals if p not in reached))
        if not todo:
            return []
        job_id = new_id("job")
        inc = inc.model_copy(update={"principal_notice_job": job_id})
        inc = self._store(inc, "incident.principals_notify.started", actor, job_id=job_id, count=len(todo))
        made = [self._notify_one(inc, p, job_id) for p in todo]
        counts = {s.value: sum(1 for n in made if n.status is s) for s in NoticeStatus}
        self._audit("incident.principals_notify.finished", inc, actor, job_id=job_id, **counts)
        self._publish(INCIDENT_PRINCIPALS_NOTIFIED, inc, job_id=job_id, **counts)
        return made

    def _notify_one(self, inc: Incident, principal: str, job_id: str) -> PrincipalNotice:
        now = self._kit.now()

        def notice(status: NoticeStatus, **extra: object) -> PrincipalNotice:
            n = PrincipalNotice(
                tenant_id=self._kit.tenant_id,
                id=new_id("pn"),
                incident_id=inc.id,
                job_id=job_id,
                principal=principal,
                status=status,
                created_at=now,
                **extra,
            )
            self._repo.save_principal_notice(n)
            return n

        resolver = self._kit.contact_resolver
        contact = resolver(principal) if resolver else None
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
            return notice(NoticeStatus.NO_CONTACT, error="no contact details")

        locale = (contact.locale if contact else None) or self._kit.policy.default_language
        pending = "not yet known"
        subject, body = render(
            "breach_notice",
            locale,
            {
                "fiduciary": self._kit.contact.get("name", "us"),
                "description": inc.description or pending,
                "data_items": ", ".join(inc.data_items) or pending,
                "aware_at": _ist(inc.aware_at),
                "consequences": inc.likely_consequences or pending,
                "mitigation": inc.remediation or "we are investigating and will update you",
                "safety_steps": inc.safety_steps or "watch for suspicious messages and change your passwords",
                "contact": self._kit.contact.get("email", ""),
            },
        )
        message = Message(
            channel=channel,
            to=to,
            subject=subject,
            body=body,
            template="breach_notice",
            locale=locale,
            principal=principal,
        )
        receipt = self._send(message)
        status = {
            DeliveryStatus.DELIVERED: NoticeStatus.SENT,
            DeliveryStatus.QUEUED: NoticeStatus.QUEUED,
        }.get(receipt.status, NoticeStatus.FAILED)
        return notice(
            status,
            channel=channel,
            receipt_id=receipt.id,
            error=receipt.error if status is NoticeStatus.FAILED else None,
        )

    def _send(self, message: Message) -> DeliveryReceipt:
        try:
            receipt = self._kit.notifier.send(message)
        except Exception as exc:
            receipt = NullNotifier().send(message).model_copy(update={"error": repr(exc), "provider": "error"})
        now = self._kit.now()
        receipt = receipt.model_copy(
            update={
                "tenant_id": self._kit.tenant_id,
                "created_at": now,
                "delivered_at": now if receipt.status is DeliveryStatus.DELIVERED else None,
            }
        )
        self._kit.repository.save_delivery(receipt)
        return receipt
