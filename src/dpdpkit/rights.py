"""Rights requests and grievances: access, correction, completion, updating, erasure, nomination.

Deadlines come from the policy pack: ``due_at`` is the published target, ``max_due_at`` the ceiling
(Rule 14(3) for grievances). Every transition writes an audit entry before the request is saved.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from typing import TYPE_CHECKING, Any, Protocol

from ._util import new_id
from .errors import InvalidTransition, NotFound, ValidationFailed
from .events import EXPORT_REQUESTED, REQUEST_CLOSED, REQUEST_OPENED, REQUEST_UPDATED
from .models import (
    OPEN_REQUEST_STATUSES,
    Nominee,
    RequestKind,
    RequestStatus,
    RightsRequest,
    SlaStatus,
)

if TYPE_CHECKING:
    from .kit import Kit


class IdentityVerifier(Protocol):
    """Hook called when a request opens. Start an out-of-band check (OTP, KYC, DigiLocker…),
    then report the result with :meth:`RightsService.verify_identity`."""

    def begin(self, request: RightsRequest) -> None: ...


_TRANSITIONS: dict[RequestStatus, set[RequestStatus]] = {
    RequestStatus.RECEIVED: {RequestStatus.IN_PROGRESS, RequestStatus.IDENTITY_PENDING, RequestStatus.REJECTED},
    RequestStatus.IDENTITY_PENDING: {RequestStatus.IN_PROGRESS, RequestStatus.REJECTED},
    RequestStatus.IN_PROGRESS: {RequestStatus.COMPLETED, RequestStatus.REJECTED},
    RequestStatus.COMPLETED: set(),
    RequestStatus.REJECTED: set(),
}


class RightsService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit

    # ------------------------------------------------------------------ helpers

    def _windows(self, kind: RequestKind) -> tuple[dt.timedelta, dt.timedelta]:
        policy = self._kit.policy
        ceiling = policy.grievance_ceiling if kind is RequestKind.GRIEVANCE else policy.request_ceiling
        return min(policy.request_target, ceiling), ceiling

    def _save(self, req: RightsRequest, action: str, actor: str, data: dict[str, Any] | None = None) -> RightsRequest:
        req = req.model_copy(update={"updated_at": self._kit.now()})
        with self._kit.repository.transaction():
            self._kit.ledger.audit(
                action,
                subject_type="rights_request",
                subject_id=req.id,
                actor=actor,
                data={"status": req.status.value, **(data or {})},
            )
            self._kit.repository.save_request(req)
        return req

    def _move(self, req: RightsRequest, to: RequestStatus) -> RightsRequest:
        if to not in _TRANSITIONS[req.status]:
            raise InvalidTransition(
                f"request {req.id} cannot move from {req.status.value} to {to.value}",
                request_id=req.id,
                status=req.status.value,
            )
        return req.model_copy(update={"status": to})

    def _publish(self, event_type: str, req: RightsRequest) -> None:
        self._kit.events.publish(
            event_type,
            self._kit.tenant_id,
            {"request_id": req.id, "principal": req.principal, "kind": req.kind.value, "status": req.status.value},
            self._kit.now(),
        )

    # ------------------------------------------------------------------ opening

    def open(
        self,
        principal: str,
        kind: RequestKind | str,
        details: dict[str, Any] | None = None,
        *,
        actor: str | None = None,
    ) -> RightsRequest:
        kind = RequestKind(kind)
        details = dict(details or {})
        if kind is RequestKind.NOMINATION:
            for field in ("name", "contact"):
                if not str(details.get(field, "")).strip():
                    raise ValidationFailed(f"a nomination needs the nominee's {field}", field=field)
        now = self._kit.now()
        target, ceiling = self._windows(kind)
        verifier = self._kit.identity_verifier
        req = RightsRequest(
            tenant_id=self._kit.tenant_id,
            id=new_id("rq"),
            principal=principal,
            kind=kind,
            status=RequestStatus.IDENTITY_PENDING if verifier else RequestStatus.RECEIVED,
            opened_at=now,
            due_at=now + target,
            max_due_at=now + ceiling,
            details=details,
            updated_at=now,
        )
        req = self._save(req, "rights.request.opened", actor or principal, {"kind": kind.value})
        self._publish(REQUEST_OPENED, req)
        if kind is RequestKind.ACCESS:
            self._kit.events.publish(
                EXPORT_REQUESTED, self._kit.tenant_id, {"request_id": req.id, "principal": principal}, now
            )
        if verifier:
            verifier.begin(req)
        elif kind is RequestKind.NOMINATION:
            # Recording a nominee needs no fiduciary action beyond storing it.
            req = self._record_nominee(req, actor or principal)
        return req

    def _record_nominee(self, req: RightsRequest, actor: str) -> RightsRequest:
        nominee = Nominee(
            tenant_id=self._kit.tenant_id,
            id=new_id("nm"),
            principal=req.principal,
            name=str(req.details["name"]),
            contact=str(req.details["contact"]),
            relationship=req.details.get("relationship"),
            created_at=self._kit.now(),
        )
        with self._kit.repository.transaction():
            self._kit.ledger.audit(
                "rights.nominee.recorded",
                subject_type="nominee",
                subject_id=nominee.id,
                actor=actor,
                data={"request_id": req.id},
            )
            self._kit.repository.save_nominee(nominee)
        if req.status is not RequestStatus.IN_PROGRESS:
            req = self._move(req, RequestStatus.IN_PROGRESS)
        req = self._move(req, RequestStatus.COMPLETED)
        req = req.model_copy(
            update={"outcome": "nominee_recorded", "closed_at": self._kit.now(), "response_contact": self._kit.contact}
        )
        req = self._save(req, "rights.request.completed", actor, {"outcome": "nominee_recorded"})
        self._publish(REQUEST_CLOSED, req)
        return req

    # ------------------------------------------------------------------ reading

    def get(self, request_id: str, principal: str | None = None) -> RightsRequest:
        req = self._kit.repository.get_request(self._kit.tenant_id, request_id)
        if req is None or (principal is not None and req.principal != principal):
            raise NotFound(f"request {request_id} not found", request_id=request_id)
        return req

    def list_requests(
        self,
        *,
        principal: str | None = None,
        statuses: Collection[RequestStatus] | None = None,
        open_only: bool = False,
    ) -> list[RightsRequest]:
        wanted = OPEN_REQUEST_STATUSES if open_only else statuses
        return self._kit.repository.list_requests(self._kit.tenant_id, principal=principal, statuses=wanted)

    def nominees(self, principal: str) -> list[Nominee]:
        return self._kit.repository.list_nominees(self._kit.tenant_id, principal)

    def sla(self, req: RightsRequest, now: dt.datetime | None = None) -> SlaStatus:
        current = now or self._kit.now()
        end = req.closed_at or current
        return SlaStatus(
            due_at=req.due_at,
            max_due_at=req.max_due_at,
            remaining_seconds=int((req.due_at - current).total_seconds()),
            overdue=req.closed_at is None and current > req.due_at,
            ceiling_breached=end > req.max_due_at,
        )

    def overdue(self, now: dt.datetime | None = None) -> list[RightsRequest]:
        current = now or self._kit.now()
        return [r for r in self.list_requests(open_only=True) if current > r.due_at]

    # ------------------------------------------------------------------ transitions

    def assign(self, request_id: str, assignee: str, *, actor: str) -> RightsRequest:
        req = self.get(request_id)
        if req.status not in OPEN_REQUEST_STATUSES:
            raise InvalidTransition(f"request {request_id} is closed", request_id=request_id)
        req = self._save(
            req.model_copy(update={"assignee": assignee}), "rights.request.assigned", actor, {"assignee": assignee}
        )
        self._publish(REQUEST_UPDATED, req)
        return req

    def verify_identity(
        self, request_id: str, verified: bool, *, actor: str, method: str | None = None
    ) -> RightsRequest:
        req = self.get(request_id)
        if req.status not in (RequestStatus.IDENTITY_PENDING, RequestStatus.RECEIVED):
            raise InvalidTransition(f"request {request_id} is not awaiting identity verification")
        if verified:
            req = self._move(req, RequestStatus.IN_PROGRESS).model_copy(update={"identity_verified": True})
            req = self._save(req, "rights.identity.verified", actor, {"method": method})
            self._publish(REQUEST_UPDATED, req)
            if req.kind is RequestKind.NOMINATION:
                return self._record_nominee(req, actor)
            return req
        req = self._move(req, RequestStatus.REJECTED).model_copy(
            update={
                "identity_verified": False,
                "outcome": "identity_not_verified",
                "closed_at": self._kit.now(),
                "response_contact": self._kit.contact,
            }
        )
        req = self._save(req, "rights.identity.failed", actor, {"method": method})
        self._publish(REQUEST_CLOSED, req)
        return req

    def start(self, request_id: str, *, actor: str) -> RightsRequest:
        req = self._move(self.get(request_id), RequestStatus.IN_PROGRESS)
        req = self._save(req, "rights.request.started", actor)
        self._publish(REQUEST_UPDATED, req)
        return req

    def respond(self, request_id: str, message: str, *, actor: str, outcome: str = "completed") -> RightsRequest:
        """Complete a request with a response. The fiduciary's contact details are always attached (Rule 9)."""
        if not message.strip():
            raise ValidationFailed("a response message is required")
        req = self.get(request_id)
        if req.status is RequestStatus.RECEIVED:
            req = self._move(req, RequestStatus.IN_PROGRESS)
        if req.kind is RequestKind.ERASURE:
            result = self._kit.retention.erase_now(req.principal, reason=f"request:{req.id}", actor=actor)
            if result.held_purposes:
                outcome = "partially_completed_legal_hold"
            if result.failed:
                raise InvalidTransition(
                    "erasure handlers failed; the request stays open", request_id=req.id, errors=result.failed
                )
        req = self._move(req, RequestStatus.COMPLETED).model_copy(
            update={
                "response": message,
                "response_contact": self._kit.contact,
                "outcome": outcome,
                "closed_at": self._kit.now(),
            }
        )
        req = self._save(req, "rights.request.completed", actor, {"outcome": outcome})
        self._publish(REQUEST_CLOSED, req)
        return req

    def reject(self, request_id: str, reason: str, *, actor: str) -> RightsRequest:
        if not reason.strip():
            raise ValidationFailed("a reason is required to reject a request")
        req = self._move(self.get(request_id), RequestStatus.REJECTED).model_copy(
            update={
                "response": reason,
                "response_contact": self._kit.contact,
                "outcome": "rejected",
                "closed_at": self._kit.now(),
            }
        )
        req = self._save(req, "rights.request.rejected", actor, {"reason": reason})
        self._publish(REQUEST_CLOSED, req)
        return req
