from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from dpdpkit import (
    ClockState,
    DeliveryStatus,
    IncidentSeverity,
    IncidentStatus,
    InMemoryRepository,
    Kit,
    MemoryNotifier,
    NoticeStatus,
    Policy,
    ReportKind,
)
from dpdpkit.errors import ConfigError, InvalidTransition, NotFound, ValidationFailed
from dpdpkit.events import (
    INCIDENT_CLOSED,
    INCIDENT_PRINCIPALS_NOTIFIED,
    INCIDENT_RECORDED,
    INCIDENT_REPORTED,
)
from dpdpkit.repository import IncidentRepository


def _cert_in_kit(make_kit: Any) -> Kit:
    data = Policy.load("dpdp-rules-2025.v1").as_dict()
    data["id"] = "dpdp-rules-certin.v1"
    data.setdefault("breach", {})["cert_in_report_hours"] = 6
    base = make_kit()
    return Kit(
        InMemoryRepository(),
        Policy(data),
        registry=base.registry,
        notifier=base.notifier,
        clock=base._clock,
        signing_key="k",
        contact_resolver=base.contact_resolver,
    )


def test_record_defaults_and_validation(kit: Kit, clock: Any) -> None:
    inc = kit.incidents.record("DB leak", actor="dpo")
    assert inc.aware_at == inc.detected_at == clock.now
    assert inc.status is IncidentStatus.OPEN
    assert kit.incidents.get(inc.id) == inc
    assert kit.incidents.list_incidents() == [inc]
    with pytest.raises(ValidationFailed):
        kit.incidents.record("  ", actor="dpo")
    with pytest.raises(ValidationFailed):
        kit.incidents.record("x", actor="dpo", affected_count=-1)
    with pytest.raises(ValidationFailed):
        kit.incidents.record("x", actor="dpo", detected_at=clock.now + dt.timedelta(hours=1))
    with pytest.raises(NotFound):
        kit.incidents.get("nope")


def test_events_and_audit_have_no_principal_data(kit: Kit) -> None:
    seen: list[str] = []
    kit.events.subscribe("*", lambda e: seen.append(e.type))
    inc = kit.incidents.record("Leak", actor="dpo", description="d")
    kit.incidents.mark_reported(inc.id, ReportKind.BOARD_INITIAL, actor="dpo", reference="DPB-1")
    kit.incidents.notify_principals(inc.id, ["u1", "ghost"], actor="dpo")
    kit.incidents.close(inc.id, actor="dpo")
    assert seen == [INCIDENT_RECORDED, INCIDENT_REPORTED, INCIDENT_PRINCIPALS_NOTIFIED, INCIDENT_CLOSED]
    assert kit.incidents.get(inc.id).report_references == {"board_initial": "DPB-1"}
    dump = repr(kit.repository.__dict__.get("audit", "")) + repr(kit.ledger.verify())
    assert "u1@example.in" not in dump


def test_update_and_close(kit: Kit) -> None:
    inc = kit.incidents.record("Leak", actor="dpo")
    upd = kit.incidents.update(
        inc.id, actor="dpo", affected_count=10, data_items=("email",), severity=IncidentSeverity.HIGH
    )
    assert (upd.affected_count, upd.data_items, upd.severity) == (10, ["email"], IncidentSeverity.HIGH)
    with pytest.raises(ValidationFailed):
        kit.incidents.update(inc.id, actor="dpo", status="closed")
    with pytest.raises(ValidationFailed):
        kit.incidents.update(inc.id, actor="dpo", affected_count=-5)
    with pytest.raises(ValidationFailed):
        kit.incidents.update(inc.id, actor="dpo", title=" ")
    closed = kit.incidents.close(inc.id, actor="dpo")
    assert closed.closed_at is not None
    with pytest.raises(InvalidTransition):
        kit.incidents.close(inc.id, actor="dpo")
    with pytest.raises(InvalidTransition):
        kit.incidents.update(inc.id, actor="dpo", description="late")
    assert kit.incidents.list_incidents(open_only=True) == []


def test_clocks_board_only(kit: Kit, clock: Any) -> None:
    inc = kit.incidents.record("Leak", actor="dpo")
    initial, detailed = kit.incidents.clocks(inc.id)
    assert initial.kind is ReportKind.BOARD_INITIAL and initial.due_at is None
    assert initial.state is ClockState.PENDING
    assert detailed.due_at == inc.aware_at + kit.policy.board_report_window
    clock.advance(days=4)
    assert [c.state for c in kit.incidents.clocks(inc.id)] == [ClockState.PENDING, ClockState.OVERDUE]
    assert [(i.id, c.kind) for i, c in kit.incidents.overdue()] == [(inc.id, ReportKind.BOARD_DETAILED)]
    kit.incidents.mark_reported(inc.id, ReportKind.BOARD_DETAILED, actor="dpo")
    assert kit.incidents.overdue() == []
    with pytest.raises(InvalidTransition):
        kit.incidents.mark_reported(inc.id, ReportKind.BOARD_DETAILED, actor="dpo")
    with pytest.raises(InvalidTransition):
        kit.incidents.mark_reported(inc.id, ReportKind.CERT_IN, actor="dpo")


def test_clock_counts_from_awareness_not_detection(kit: Kit, clock: Any) -> None:
    aware = clock.now - dt.timedelta(hours=10)
    inc = kit.incidents.record("Leak", actor="dpo", detected_at=aware - dt.timedelta(hours=5), aware_at=aware)
    assert kit.incidents.clocks(inc.id)[1].due_at == aware + kit.policy.board_report_window


def test_cert_in_clock(make_kit: Any, clock: Any) -> None:
    kit = _cert_in_kit(make_kit)
    inc = kit.incidents.record("Leak", actor="dpo")
    cert = kit.incidents.clocks(inc.id)[2]
    assert cert.kind is ReportKind.CERT_IN and cert.due_at == inc.aware_at + dt.timedelta(hours=6)
    clock.advance(hours=7)
    assert any(c.kind is ReportKind.CERT_IN for _, c in kit.incidents.overdue())
    with pytest.raises(ValidationFailed):
        kit.incidents.mark_reported(inc.id, ReportKind.CERT_IN, actor="dpo", at=clock.now + dt.timedelta(hours=1))
    with pytest.raises(ValidationFailed):
        kit.incidents.mark_reported(inc.id, ReportKind.CERT_IN, actor="dpo", at=inc.aware_at - dt.timedelta(hours=1))
    kit.incidents.mark_reported(inc.id, ReportKind.CERT_IN, actor="dpo")
    assert kit.incidents.clocks(inc.id)[2].state is ClockState.DONE


def test_drafts_flag_missing_facts(kit: Kit) -> None:
    inc = kit.incidents.record("Leak", actor="dpo")
    for kind in ReportKind:
        report = kit.incidents.draft_report(inc.id, kind)
        assert report.draft is True and report.missing
        assert "TO COMPLETE" in report.body and "does not send" in report.body
    kit.incidents.update(
        inc.id,
        actor="dpo",
        description="Open bucket",
        affected_count=3,
        data_items=["email"],
        likely_consequences="phishing",
        root_cause="misconfig",
        remediation="locked",
    )
    initial = kit.incidents.draft_report(inc.id, ReportKind.BOARD_INITIAL)
    assert "Open bucket" in initial.body and "phishing" in initial.body
    assert not [m for m in initial.missing if m != "contact for follow-up"]
    detailed = kit.incidents.draft_report(inc.id, ReportKind.BOARD_DETAILED)
    assert "Board initial intimation not yet recorded" in detailed.missing
    kit.incidents.mark_reported(inc.id, ReportKind.BOARD_INITIAL, actor="dpo")
    kit.incidents.notify_principals(inc.id, ["u1"], actor="dpo")
    detailed = kit.incidents.draft_report(inc.id, ReportKind.BOARD_DETAILED)
    assert "1 affected principals were notified" in detailed.body
    assert "Initial intimation sent" in detailed.body


def test_notify_principals_outcomes_and_idempotence(kit: Kit, notifier: MemoryNotifier) -> None:
    inc = kit.incidents.record("Leak", actor="dpo", description="Open bucket")
    made = kit.incidents.notify_principals(inc.id, ["u1", "u2", "ghost", "u1"], actor="dpo")
    assert {n.principal: n.status for n in made} == {
        "u1": NoticeStatus.SENT,
        "u2": NoticeStatus.SENT,
        "ghost": NoticeStatus.NO_CONTACT,
    }
    assert len(notifier.sent) == 2 and "Open bucket" in notifier.sent[0].body
    assert kit.incidents.get(inc.id).principal_notice_job == made[0].job_id
    again = kit.incidents.notify_principals(inc.id, ["u1", "u2", "ghost"], actor="dpo")
    assert [n.principal for n in again] == ["ghost"]
    assert kit.incidents.notify_principals(inc.id, ["u1"], actor="dpo") == []
    assert len(kit.incidents.notices(inc.id)) == 4


def test_notify_failures_do_not_raise_and_are_retried(make_kit: Any) -> None:
    class Boom:
        def send(self, message: Any) -> Any:
            raise RuntimeError("smtp down")

    kit = make_kit(notifier=Boom())
    inc = kit.incidents.record("Leak", actor="dpo")
    made = kit.incidents.notify_principals(inc.id, ["u1"], actor="dpo")
    assert made[0].status is NoticeStatus.FAILED and "smtp down" in (made[0].error or "")

    failing = MemoryNotifier(DeliveryStatus.FAILED)
    kit.notifier = failing
    assert kit.incidents.notify_principals(inc.id, ["u1"], actor="dpo")[0].status is NoticeStatus.FAILED
    kit.notifier = MemoryNotifier(DeliveryStatus.QUEUED)
    assert kit.incidents.notify_principals(inc.id, ["u1"], actor="dpo")[0].status is NoticeStatus.QUEUED
    assert kit.incidents.notify_principals(inc.id, ["u1"], actor="dpo") == []


def test_notify_without_resolver(make_kit: Any) -> None:
    kit = make_kit()
    kit.contact_resolver = None
    inc = kit.incidents.record("Leak", actor="dpo")
    assert kit.incidents.notify_principals(inc.id, ["u1"], actor="dpo")[0].status is NoticeStatus.NO_CONTACT


def test_repository_without_incident_support(make_kit: Any) -> None:
    repo = InMemoryRepository()
    assert isinstance(repo, IncidentRepository)

    class NoIncidents:
        def __getattr__(self, name: str) -> Any:
            if "incident" in name or "principal_notice" in name:
                raise AttributeError(name)
            return getattr(repo, name)

    kit = make_kit()
    kit.repository = NoIncidents()  # type: ignore[assignment]
    with pytest.raises(ConfigError):
        kit.incidents.list_incidents()


def test_tenants_are_isolated(kit: Kit) -> None:
    inc = kit.incidents.record("Leak", actor="dpo")
    other = kit.for_tenant("other")
    assert other.incidents.list_incidents() == []
    with pytest.raises(NotFound):
        other.incidents.get(inc.id)
