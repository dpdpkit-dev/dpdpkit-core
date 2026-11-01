"""Age gate, verifiable parental consent and guardian verification (DPDP Act s.9; Rules 2025, Rules 10 and 11).

Fail-safe rules enforced here:

* **Unknown age is treated as a child.** Until an age gate has run for a principal, :meth:`GuardianService.status`
  is ``UNKNOWN_AGE`` and :meth:`GuardianService.grant_consent` refuses.
* **Verification is pluggable and never guessed.** dpdpkit does not decide who is a parent. A
  :class:`GuardianVerifier` (a DigiLocker token check, a school's records, a manual review) does. A verifier that
  raises, or answers anything but "verified", leaves the guardian unverified.
* **Only a verified guardian's consent counts.** :meth:`GuardianService.grant_consent` writes the grant to the
  consent ledger with the verification id as evidence; for a child it raises :class:`GuardianRequired` otherwise.
* **No tracking or targeted advertising for children** (s.9(3)): :meth:`GuardianService.tracking_allowed` is
  ``False`` for anyone who is not a confirmed adult, whoever has consented.
* The date of birth is used to compute the assertion and then dropped. Audit rows hold ids, methods and
  outcomes, never ages, birth dates or guardian contact details.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ._util import new_id
from .errors import ConfigError, GuardianRequired, InvalidTransition, NotFound, ValidationFailed
from .events import (
    GUARDIAN_AGE_ASSESSED,
    GUARDIAN_REVOKED,
    GUARDIAN_VERIFICATION_FAILED,
    GUARDIAN_VERIFIED,
)
from .models import (
    AgeAssertion,
    ConsentState,
    GuardianBasis,
    GuardianRelationship,
    GuardianStatus,
    GuardianVerification,
    VerificationStatus,
)
from .repository import GuardianRepository

if TYPE_CHECKING:
    from .kit import Kit

SOURCE = "guardian"


@dataclass(frozen=True)
class VerifierRequest:
    """What a verifier is told. No birth date and no contact details."""

    principal: str
    guardian_ref: str
    relationship: GuardianRelationship
    basis: GuardianBasis


@dataclass(frozen=True)
class VerifierResult:
    verified: bool
    reference: str | None = None
    """A token or case number to keep; the evidence itself stays with the verifier."""
    detail: str | None = None
    """Why it failed, when it did. Stored on the verification, so keep personal data out."""


@runtime_checkable
class GuardianVerifier(Protocol):
    """A way to establish that someone is the parent or lawful guardian of a principal."""

    name: str

    def verify(self, request: VerifierRequest, evidence: Mapping[str, Any]) -> VerifierResult: ...


@dataclass
class CallbackVerifier:
    """Wrap a function as a verifier, for a school's own records or a call to an identity provider."""

    name: str
    func: Callable[[VerifierRequest, Mapping[str, Any]], VerifierResult | bool]

    def verify(self, request: VerifierRequest, evidence: Mapping[str, Any]) -> VerifierResult:
        out = self.func(request, evidence)
        return out if isinstance(out, VerifierResult) else VerifierResult(verified=bool(out))


class ManualVerifier:
    """A person checked documents offline. The evidence must name the reviewer; the case reference is kept."""

    name = "manual"

    def verify(self, request: VerifierRequest, evidence: Mapping[str, Any]) -> VerifierResult:
        reviewer = str(evidence.get("reviewer") or "").strip()
        if not reviewer:
            return VerifierResult(False, detail="manual verification needs a named reviewer")
        if evidence.get("approved") is not True:
            return VerifierResult(False, detail="reviewer did not approve")
        ref = evidence.get("case_reference")
        return VerifierResult(True, reference=str(ref) if ref else None)


class GuardianService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit
        self._verifiers: dict[str, GuardianVerifier] = {"manual": ManualVerifier()}

    @property
    def _repo(self) -> GuardianRepository:
        repo = self._kit.repository
        if not isinstance(repo, GuardianRepository):
            raise ConfigError("repository does not implement dpdpkit.repository.GuardianRepository")
        return repo

    # ------------------------------------------------------------------ helpers

    def _audit(self, action: str, subject_type: str, subject_id: str, actor: str, **data: object) -> None:
        self._kit.ledger.audit(action, subject_type=subject_type, subject_id=subject_id, actor=actor, data=data)

    def _publish(self, event_type: str, principal: str, **data: object) -> None:
        self._kit.events.publish(event_type, self._kit.tenant_id, {"principal": principal, **data}, self._kit.now())

    @staticmethod
    def _need(value: str, what: str) -> str:
        value = value.strip()
        if not value:
            raise ValidationFailed(f"{what} is required")
        return value

    # ------------------------------------------------------------------ verifiers

    def register_verifier(self, verifier: GuardianVerifier) -> None:
        """Add or replace a verifier by its ``name``. ``manual`` is built in."""
        if not verifier.name.strip():
            raise ValidationFailed("a verifier needs a name")
        self._verifiers[verifier.name] = verifier

    def verifiers(self) -> list[str]:
        return sorted(self._verifiers)

    # ------------------------------------------------------------------ age gate

    def assess_age(
        self,
        principal: str,
        *,
        actor: str,
        birth_date: dt.date | None = None,
        age: int | None = None,
        method: str = "declared",
    ) -> AgeAssertion:
        """Run the age gate from a birth date or a stated age. ``method`` says how the age was established
        (``declared``, ``document``, ``school_record``); a self-declaration is a weak method and recorded as such."""
        principal = self._need(principal, "principal")
        if (birth_date is None) == (age is None):
            raise ValidationFailed("give exactly one of birth_date or age")
        now = self._kit.now()
        if birth_date is not None:
            today = now.date()
            if birth_date > today:
                raise ValidationFailed("birth_date is in the future")
            age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
        if age is None or age < 0:
            raise ValidationFailed("age must be zero or more")
        majority = self._kit.policy.age_of_majority
        previous = self._repo.latest_age_assertion(self._kit.tenant_id, principal)
        if previous is not None and previous.basis is GuardianBasis.DISABILITY:
            return previous  # an adult age does not lift a recorded need for a lawful guardian
        child = age < majority
        assertion = AgeAssertion(
            id=new_id("age"),
            tenant_id=self._kit.tenant_id,
            principal=principal,
            needs_guardian=child,
            basis=GuardianBasis.AGE if child else None,
            method=self._need(method, "method"),
            age_of_majority=majority,
            asserted_by=actor,
            asserted_at=now,
        )
        self._store_assertion(assertion)
        return assertion

    def require_guardian_for_disability(
        self, principal: str, *, actor: str, method: str = "court_order"
    ) -> AgeAssertion:
        """Record that a person with a disability has a lawful guardian who must act for them (Rule 11)."""
        principal = self._need(principal, "principal")
        assertion = AgeAssertion(
            id=new_id("age"),
            tenant_id=self._kit.tenant_id,
            principal=principal,
            needs_guardian=True,
            basis=GuardianBasis.DISABILITY,
            method=self._need(method, "method"),
            age_of_majority=self._kit.policy.age_of_majority,
            asserted_by=actor,
            asserted_at=self._kit.now(),
        )
        self._store_assertion(assertion)
        return assertion

    def _store_assertion(self, assertion: AgeAssertion) -> None:
        with self._kit.repository.transaction():
            self._audit(
                "guardian.age_assessed",
                "principal",
                assertion.principal,
                assertion.asserted_by,
                assertion_id=assertion.id,
                needs_guardian=assertion.needs_guardian,
                basis=assertion.basis.value if assertion.basis else None,
                method=assertion.method,
            )
            self._repo.save_age_assertion(assertion)
        self._publish(
            GUARDIAN_AGE_ASSESSED, assertion.principal, needs_guardian=assertion.needs_guardian, method=assertion.method
        )

    def age_assertion(self, principal: str) -> AgeAssertion | None:
        return self._repo.latest_age_assertion(self._kit.tenant_id, principal)

    # ------------------------------------------------------------------ verification

    def begin_verification(
        self,
        principal: str,
        guardian_ref: str,
        *,
        actor: str,
        verifier: str,
        relationship: GuardianRelationship = GuardianRelationship.PARENT,
    ) -> GuardianVerification:
        principal = self._need(principal, "principal")
        guardian_ref = self._need(guardian_ref, "guardian_ref")
        if verifier not in self._verifiers:
            raise ValidationFailed(f"no verifier named {verifier!r}", verifier=verifier)
        assertion = self.age_assertion(principal)
        if assertion is None or not assertion.needs_guardian:
            raise InvalidTransition("principal has no recorded need for a guardian; run the age gate first")
        if guardian_ref == principal:
            raise ValidationFailed("a principal cannot be their own guardian")
        ver = GuardianVerification(
            id=new_id("gv"),
            tenant_id=self._kit.tenant_id,
            principal=principal,
            guardian_ref=guardian_ref,
            relationship=relationship,
            verifier=verifier,
            created_by=actor,
            created_at=self._kit.now(),
        )
        with self._kit.repository.transaction():
            self._audit("guardian.verification_started", "guardian_verification", ver.id, actor, verifier=verifier)
            self._repo.save_guardian_verification(ver)
        return ver

    def confirm_verification(
        self, verification_id: str, evidence: Mapping[str, Any] | None = None, *, actor: str
    ) -> GuardianVerification:
        """Ask the verifier. Never raises for a failed check: the outcome is stored and the guardian stays
        unverified. A verifier that raises counts as a failure."""
        ver = self.get_verification(verification_id)
        if ver.status is not VerificationStatus.PENDING:
            raise InvalidTransition(f"verification is already {ver.status.value}", status=ver.status.value)
        assertion = self.age_assertion(ver.principal)
        request = VerifierRequest(
            principal=ver.principal,
            guardian_ref=ver.guardian_ref,
            relationship=ver.relationship,
            basis=(assertion.basis if assertion and assertion.basis else GuardianBasis.AGE),
        )
        try:
            result = self._verifiers[ver.verifier].verify(request, dict(evidence or {}))
        except Exception as exc:  # a broken verifier must fail closed
            result = VerifierResult(False, detail=f"verifier error: {type(exc).__name__}")
        verified = result.verified is True
        now = self._kit.now()
        ver = ver.model_copy(
            update={
                "status": VerificationStatus.VERIFIED if verified else VerificationStatus.FAILED,
                "reference": result.reference,
                "error": None if verified else (result.detail or "not verified"),
                "verified_at": now if verified else None,
            }
        )
        with self._kit.repository.transaction():
            self._audit(
                "guardian.verified" if verified else "guardian.verification_failed",
                "guardian_verification",
                ver.id,
                actor,
                principal=ver.principal,
                verifier=ver.verifier,
            )
            self._repo.save_guardian_verification(ver)
        self._publish(
            GUARDIAN_VERIFIED if verified else GUARDIAN_VERIFICATION_FAILED, ver.principal, verification_id=ver.id
        )
        return ver

    def revoke(self, verification_id: str, *, actor: str, reason: str = "") -> GuardianVerification:
        """Withdraw a guardian's standing. Consent they gave stays on the ledger; withdraw it separately."""
        ver = self.get_verification(verification_id)
        if ver.status is not VerificationStatus.VERIFIED:
            raise InvalidTransition(f"only a verified guardian can be revoked, not {ver.status.value}")
        ver = ver.model_copy(update={"status": VerificationStatus.REVOKED, "revoked_at": self._kit.now()})
        with self._kit.repository.transaction():
            self._audit(
                "guardian.revoked", "guardian_verification", ver.id, actor, principal=ver.principal, reason=reason
            )
            self._repo.save_guardian_verification(ver)
        self._publish(GUARDIAN_REVOKED, ver.principal, verification_id=ver.id)
        return ver

    def get_verification(self, verification_id: str) -> GuardianVerification:
        ver = self._repo.get_guardian_verification(self._kit.tenant_id, verification_id)
        if ver is None:
            raise NotFound(f"no guardian verification {verification_id!r}", verification_id=verification_id)
        return ver

    def verifications(self, principal: str) -> list[GuardianVerification]:
        return self._repo.list_guardian_verifications(self._kit.tenant_id, principal)

    def active_verification(self, principal: str) -> GuardianVerification | None:
        """The most recent verified guardian, if any."""
        verified = [v for v in self.verifications(principal) if v.status is VerificationStatus.VERIFIED]
        return verified[-1] if verified else None

    # ------------------------------------------------------------------ gating

    def status(self, principal: str) -> GuardianStatus:
        assertion = self.age_assertion(principal)
        if assertion is None:
            return GuardianStatus.UNKNOWN_AGE
        if not assertion.needs_guardian:
            return GuardianStatus.ADULT
        if self.active_verification(principal) is not None:
            return GuardianStatus.VERIFIED
        if any(v.status is VerificationStatus.PENDING for v in self.verifications(principal)):
            return GuardianStatus.PENDING
        return GuardianStatus.GUARDIAN_REQUIRED

    def tracking_allowed(self, principal: str) -> bool:
        """Tracking, behavioural monitoring and targeted advertising are barred for children (s.9(3)),
        whatever a guardian has agreed. True only for a principal the age gate confirmed as an adult."""
        return self.status(principal) is GuardianStatus.ADULT

    def grant_consent(
        self,
        principal: str,
        purpose: str,
        notice_version: int | str | None = None,
        *,
        locale: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConsentState:
        """Grant consent through the age gate. Adults go straight to the ledger; a child needs a verified
        guardian, whose verification id is recorded with the grant. Raises :class:`GuardianRequired` otherwise."""
        status = self.status(principal)
        if status is GuardianStatus.ADULT:
            return self._kit.consent.grant(principal, purpose, notice_version, locale=locale, metadata=metadata)
        ver = self.active_verification(principal)
        if status is not GuardianStatus.VERIFIED or ver is None:
            raise GuardianRequired(status.value, principal=principal, purpose=purpose)
        meta = {
            **(metadata or {}),
            "given_by": "guardian",
            "guardian_verification_id": ver.id,
            "guardian_ref": ver.guardian_ref,
        }
        return self._kit.consent.grant(principal, purpose, notice_version, locale=locale, source=SOURCE, metadata=meta)
