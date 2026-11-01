from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from dpdpkit import (
    CallbackVerifier,
    ConsentStatus,
    GuardianBasis,
    GuardianRelationship,
    GuardianStatus,
    InMemoryRepository,
    Kit,
    VerificationStatus,
    VerifierRequest,
    VerifierResult,
)
from dpdpkit.errors import ConfigError, GuardianRequired, InvalidTransition, NotFound, ValidationFailed
from dpdpkit.events import GUARDIAN_AGE_ASSESSED, GUARDIAN_REVOKED, GUARDIAN_VERIFICATION_FAILED, GUARDIAN_VERIFIED

OK = {"reviewer": "dpo", "approved": True, "case_reference": "C-1"}


def _verified(kit: Kit, principal: str = "kid") -> str:
    kit.guardians.assess_age(principal, actor="app", age=12)
    ver = kit.guardians.begin_verification(principal, "mum", actor="app", verifier="manual")
    return kit.guardians.confirm_verification(ver.id, OK, actor="dpo").id


def test_age_gate_from_age_and_birth_date(kit: Kit, clock: Any) -> None:
    today = clock.now.date()
    adult = kit.guardians.assess_age("a", actor="app", birth_date=today.replace(year=today.year - 18))
    assert not adult.needs_guardian and adult.basis is None
    child = kit.guardians.assess_age(
        "c", actor="app", birth_date=today.replace(year=today.year - 18) + dt.timedelta(days=1)
    )
    assert child.needs_guardian and child.basis is GuardianBasis.AGE
    assert kit.guardians.age_assertion("c") == child
    assert kit.guardians.status("a") is GuardianStatus.ADULT
    assert kit.guardians.status("c") is GuardianStatus.GUARDIAN_REQUIRED
    dumped = str([e.data for e in kit.repository.list_audit(kit.tenant_id)])
    assert "birth" not in dumped and str(today.year - 18) not in dumped


def test_age_gate_validation(kit: Kit, clock: Any) -> None:
    for kwargs in ({}, {"age": 5, "birth_date": clock.now.date()}):
        with pytest.raises(ValidationFailed):
            kit.guardians.assess_age("p", actor="app", **kwargs)
    with pytest.raises(ValidationFailed):
        kit.guardians.assess_age("p", actor="app", birth_date=clock.now.date() + dt.timedelta(days=1))
    with pytest.raises(ValidationFailed):
        kit.guardians.assess_age("p", actor="app", age=-1)
    with pytest.raises(ValidationFailed):
        kit.guardians.assess_age(" ", actor="app", age=5)
    with pytest.raises(ValidationFailed):
        kit.guardians.assess_age("p", actor="app", age=5, method=" ")


def test_unknown_age_is_refused(kit: Kit) -> None:
    assert kit.guardians.status("x") is GuardianStatus.UNKNOWN_AGE
    assert not kit.guardians.tracking_allowed("x")
    with pytest.raises(GuardianRequired) as err:
        kit.guardians.grant_consent("x", "analytics")
    assert err.value.principal_status == "unknown_age"
    assert err.value.http_status == 403


def test_adult_consents_directly(kit: Kit) -> None:
    kit.guardians.assess_age("a", actor="app", age=30)
    state = kit.guardians.grant_consent("a", "analytics")
    assert state.status is ConsentStatus.GRANTED
    assert kit.consent.history("a")[0].source == "api"
    assert kit.guardians.tracking_allowed("a")


def test_child_needs_verified_guardian(kit: Kit) -> None:
    kit.guardians.assess_age("kid", actor="app", age=12)
    with pytest.raises(GuardianRequired):
        kit.guardians.grant_consent("kid", "analytics")
    ver = kit.guardians.begin_verification("kid", "mum", actor="app", verifier="manual")
    assert kit.guardians.status("kid") is GuardianStatus.PENDING
    with pytest.raises(GuardianRequired):
        kit.guardians.grant_consent("kid", "analytics")
    done = kit.guardians.confirm_verification(ver.id, OK, actor="dpo")
    assert done.status is VerificationStatus.VERIFIED and done.reference == "C-1" and done.verified_at
    assert kit.guardians.status("kid") is GuardianStatus.VERIFIED
    state = kit.guardians.grant_consent("kid", "analytics", metadata={"x": 1})
    assert state.status is ConsentStatus.GRANTED
    event = kit.consent.history("kid")[0]
    assert event.source == "guardian"
    assert event.metadata["guardian_verification_id"] == ver.id
    assert event.metadata["given_by"] == "guardian" and event.metadata["x"] == 1
    assert not kit.guardians.tracking_allowed("kid")
    assert kit.guardians.active_verification("kid") == done
    assert kit.guardians.verifications("kid") == [done]


def test_manual_verifier_fails_closed(kit: Kit) -> None:
    kit.guardians.assess_age("kid", actor="app", age=9)
    for evidence in ({}, {"reviewer": "dpo"}, {"reviewer": "dpo", "approved": "yes"}):
        ver = kit.guardians.begin_verification("kid", "mum", actor="app", verifier="manual")
        out = kit.guardians.confirm_verification(ver.id, evidence, actor="dpo")
        assert out.status is VerificationStatus.FAILED and out.error
    assert kit.guardians.status("kid") is GuardianStatus.GUARDIAN_REQUIRED
    with pytest.raises(InvalidTransition):
        kit.guardians.confirm_verification(out.id, OK, actor="dpo")


def test_custom_and_broken_verifiers(kit: Kit) -> None:
    seen: list[VerifierRequest] = []

    def check(req: VerifierRequest, evidence: Any) -> VerifierResult | bool:
        seen.append(req)
        if evidence.get("boom"):
            raise RuntimeError("provider down: secret-detail")
        return VerifierResult(True, reference="tok") if evidence.get("token") == "good" else bool(evidence.get("ok"))

    kit.guardians.register_verifier(CallbackVerifier("digilocker", check))
    assert kit.guardians.verifiers() == ["digilocker", "manual"]
    kit.guardians.assess_age("kid", actor="app", age=10)

    def run(evidence: dict[str, Any]) -> Any:
        v = kit.guardians.begin_verification(
            "kid", "dad", actor="app", verifier="digilocker", relationship=GuardianRelationship.LAWFUL_GUARDIAN
        )
        return kit.guardians.confirm_verification(v.id, evidence, actor="app")

    boom = run({"boom": True})
    assert boom.status is VerificationStatus.FAILED and boom.error == "verifier error: RuntimeError"
    assert "secret" not in str(boom)
    assert run({}).status is VerificationStatus.FAILED
    assert run({"ok": True}).status is VerificationStatus.VERIFIED
    assert run({"token": "good"}).reference == "tok"
    assert seen[0].relationship is GuardianRelationship.LAWFUL_GUARDIAN and seen[0].basis is GuardianBasis.AGE


def test_verifier_validation(kit: Kit) -> None:
    with pytest.raises(ValidationFailed):
        kit.guardians.register_verifier(CallbackVerifier(" ", lambda r, e: True))
    with pytest.raises(InvalidTransition):  # no age gate yet
        kit.guardians.begin_verification("kid", "mum", actor="app", verifier="manual")
    kit.guardians.assess_age("adult", actor="app", age=40)
    with pytest.raises(InvalidTransition):
        kit.guardians.begin_verification("adult", "mum", actor="app", verifier="manual")
    kit.guardians.assess_age("kid", actor="app", age=10)
    with pytest.raises(ValidationFailed):
        kit.guardians.begin_verification("kid", "mum", actor="app", verifier="nope")
    with pytest.raises(ValidationFailed):
        kit.guardians.begin_verification("kid", "kid", actor="app", verifier="manual")
    with pytest.raises(ValidationFailed):
        kit.guardians.begin_verification("kid", " ", actor="app", verifier="manual")
    with pytest.raises(NotFound):
        kit.guardians.get_verification("nope")


def test_revoke_ends_standing(kit: Kit) -> None:
    vid = _verified(kit)
    pending = kit.guardians.begin_verification("kid", "dad", actor="a", verifier="manual")
    with pytest.raises(InvalidTransition):
        kit.guardians.revoke(pending.id, actor="dpo")
    out = kit.guardians.revoke(vid, actor="dpo", reason="custody change")
    assert out.status is VerificationStatus.REVOKED and out.revoked_at
    assert kit.guardians.status("kid") is GuardianStatus.PENDING  # dad's check is still open
    with pytest.raises(GuardianRequired):
        kit.guardians.grant_consent("kid", "analytics")
    assert kit.guardians.active_verification("kid") is None


def test_disability_guardian_and_age_cannot_lift_it(kit: Kit) -> None:
    out = kit.guardians.require_guardian_for_disability("p", actor="dpo")
    assert out.basis is GuardianBasis.DISABILITY and out.needs_guardian
    again = kit.guardians.assess_age("p", actor="app", age=40)
    assert again == out
    assert kit.guardians.status("p") is GuardianStatus.GUARDIAN_REQUIRED
    ver = kit.guardians.begin_verification(
        "p", "g", actor="app", verifier="manual", relationship=GuardianRelationship.LAWFUL_GUARDIAN
    )
    kit.guardians.confirm_verification(ver.id, OK, actor="dpo")
    assert kit.guardians.grant_consent("p", "analytics").status is ConsentStatus.GRANTED


def test_events_and_tenants(kit: Kit) -> None:
    seen: list[str] = []
    kit.events.subscribe("*", lambda e: seen.append(e.type))
    vid = _verified(kit)
    kit.guardians.revoke(vid, actor="dpo")
    ver = kit.guardians.begin_verification("kid", "dad", actor="a", verifier="manual")
    kit.guardians.confirm_verification(ver.id, {}, actor="dpo")
    assert seen == [GUARDIAN_AGE_ASSESSED, GUARDIAN_VERIFIED, GUARDIAN_REVOKED, GUARDIAN_VERIFICATION_FAILED]
    other = kit.for_tenant("t2")
    assert other.guardians.status("kid") is GuardianStatus.UNKNOWN_AGE
    assert other.guardians.verifiers() == kit.guardians.verifiers()


def test_repository_without_guardian_methods(make_kit: Any) -> None:
    class Bare(InMemoryRepository):
        save_age_assertion = None  # type: ignore[assignment]

    kit = make_kit(repository=Bare())
    with pytest.raises(ConfigError):
        kit.guardians.status("x")
