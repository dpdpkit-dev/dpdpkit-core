from __future__ import annotations

import datetime as dt

import pytest

from dpdpkit import InvalidTransition, Kit, NotFound, RequestStatus, ValidationFailed
from dpdpkit.models import RightsRequest

from .conftest import T0


def test_open_sets_deadlines_from_policy(kit: Kit) -> None:
    req = kit.rights.open("u1", "access")
    assert req.status is RequestStatus.RECEIVED
    assert req.due_at == T0 + dt.timedelta(days=30)
    assert req.max_due_at == T0 + dt.timedelta(days=90)
    grievance = kit.rights.open("u1", "grievance", {"text": "no reply"})
    assert grievance.max_due_at == T0 + dt.timedelta(days=90)
    assert [a.action for a in kit.ledger.audit_entries(subject_id=req.id)] == ["rights.request.opened"]


def test_lifecycle_attaches_contact_on_response(kit: Kit) -> None:
    req = kit.rights.open("u1", "correction", {"field": "name"})
    kit.rights.assign(req.id, "handler@x", actor="admin")
    kit.rights.start(req.id, actor="handler@x")
    done = kit.rights.respond(req.id, "Corrected.", actor="handler@x")
    assert done.status is RequestStatus.COMPLETED and done.closed_at is not None
    assert done.response_contact == kit.contact
    with pytest.raises(InvalidTransition):
        kit.rights.respond(req.id, "again", actor="handler@x")
    actions = [a.action for a in kit.ledger.audit_entries(subject_id=req.id)]
    assert actions == [
        "rights.request.opened",
        "rights.request.assigned",
        "rights.request.started",
        "rights.request.completed",
    ]


def test_reject_needs_reason_and_principal_scoping(kit: Kit) -> None:
    req = kit.rights.open("u1", "updating")
    with pytest.raises(ValidationFailed):
        kit.rights.reject(req.id, " ", actor="a")
    kit.rights.reject(req.id, "duplicate", actor="a")
    with pytest.raises(NotFound):
        kit.rights.get(req.id, principal="u2")
    assert kit.rights.get(req.id, principal="u1").outcome == "rejected"


def test_identity_verification_hook(make_kit) -> None:  # type: ignore[no-untyped-def]
    started: list[str] = []

    class Verifier:
        def begin(self, request: RightsRequest) -> None:
            started.append(request.id)

    kit = make_kit(identity_verifier=Verifier())
    req = kit.rights.open("u1", "access")
    assert req.status is RequestStatus.IDENTITY_PENDING and started == [req.id]
    ok = kit.rights.verify_identity(req.id, True, actor="otp", method="email-otp")
    assert ok.status is RequestStatus.IN_PROGRESS and ok.identity_verified
    other = kit.rights.open("u1", "erasure")
    failed = kit.rights.verify_identity(other.id, False, actor="otp")
    assert failed.status is RequestStatus.REJECTED and failed.outcome == "identity_not_verified"


def test_nomination_records_nominee(kit: Kit) -> None:
    with pytest.raises(ValidationFailed):
        kit.rights.open("u1", "nomination", {"name": "Asha"})
    req = kit.rights.open("u1", "nomination", {"name": "Asha", "contact": "asha@example.in", "relationship": "sister"})
    assert req.status is RequestStatus.COMPLETED and req.outcome == "nominee_recorded"
    [nominee] = kit.rights.nominees("u1")
    assert nominee.name == "Asha" and nominee.relationship == "sister"


def test_erasure_request_respects_holds_and_legal_retention(kit: Kit, erased) -> None:  # type: ignore[no-untyped-def]
    kit.retention.place_hold("u1", "dispute", actor="dpo", purpose="marketing")
    req = kit.rights.open("u1", "erasure")
    done = kit.rights.respond(req.id, "Erased what we can.", actor="dpo")
    assert done.outcome == "partially_completed_legal_hold"
    [(principal, purposes)] = erased
    assert principal == "u1"
    assert "marketing" not in purposes and "billing" not in purposes and "account" in purposes


def test_failed_erasure_keeps_request_open(kit: Kit) -> None:
    def broken(principal: str, purposes: list[str]) -> None:
        raise RuntimeError("nope")

    kit.retention.register_handler(broken)
    req = kit.rights.open("u1", "erasure")
    with pytest.raises(InvalidTransition):
        kit.rights.respond(req.id, "done", actor="dpo")
    assert kit.rights.get(req.id).closed_at is None


def test_overdue_and_sla(kit: Kit, clock) -> None:  # type: ignore[no-untyped-def]
    req = kit.rights.open("u1", "access")
    assert kit.rights.overdue() == []
    clock.advance(days=31)
    assert [r.id for r in kit.rights.overdue()] == [req.id]
    sla = kit.rights.sla(req)
    assert sla.overdue and not sla.ceiling_breached and sla.remaining_seconds < 0
    clock.advance(days=60)
    assert kit.rights.sla(req).ceiling_breached
