"""Storage interface. Each adapter implements :class:`Repository` with its ORM.

Rules for implementers:

* Only the models in :mod:`dpdpkit.models` cross this boundary — never ORM rows.
* Ledger and audit tables are append-only. ``append_*`` must raise
  :class:`~dpdpkit.errors.SequenceConflict` when ``(tenant_id, seq)`` already exists.
* Datetimes must round-trip with microsecond precision; naive values are read back as UTC.
* ``transaction()`` groups an audit entry with the state change it describes.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import threading
from collections.abc import Collection, Iterable, Iterator
from contextlib import AbstractContextManager
from typing import Protocol, TypeVar, runtime_checkable

from .errors import SequenceConflict
from .models import (
    ACTIVE_SCHEDULE_STATUSES,
    AuditEntry,
    ConsentEvent,
    DeliveryReceipt,
    ErasureSchedule,
    LegalHold,
    Nominee,
    Notice,
    RequestStatus,
    RightsRequest,
    ScheduleStatus,
)


@runtime_checkable
class Repository(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...

    # consent ledger (append-only, hash-chained)
    def append_consent_event(self, event: ConsentEvent) -> None: ...
    def last_consent_event(self, tenant_id: str) -> ConsentEvent | None: ...
    def latest_consent_event(self, tenant_id: str, principal: str, purpose: str) -> ConsentEvent | None: ...
    def list_consent_events(
        self, tenant_id: str, *, principal: str | None = None, after_seq: int = 0, limit: int | None = None
    ) -> list[ConsentEvent]: ...

    # audit trail (append-only, hash-chained)
    def append_audit(self, entry: AuditEntry) -> None: ...
    def last_audit(self, tenant_id: str) -> AuditEntry | None: ...
    def list_audit(
        self, tenant_id: str, *, subject_id: str | None = None, after_seq: int = 0, limit: int | None = None
    ) -> list[AuditEntry]: ...

    # notices
    def save_notice(self, notice: Notice) -> None: ...
    def list_notices(self, tenant_id: str, *, version: int | None = None) -> list[Notice]: ...

    # rights
    def save_request(self, req: RightsRequest) -> None: ...
    def get_request(self, tenant_id: str, request_id: str) -> RightsRequest | None: ...
    def list_requests(
        self,
        tenant_id: str,
        *,
        principal: str | None = None,
        statuses: Collection[RequestStatus] | None = None,
    ) -> list[RightsRequest]: ...
    def save_nominee(self, nominee: Nominee) -> None: ...
    def list_nominees(self, tenant_id: str, principal: str) -> list[Nominee]: ...

    # retention
    def save_schedule(self, schedule: ErasureSchedule) -> None: ...
    def get_schedule(self, tenant_id: str, schedule_id: str) -> ErasureSchedule | None: ...
    def list_schedules(
        self,
        tenant_id: str,
        *,
        principal: str | None = None,
        statuses: Collection[ScheduleStatus] | None = None,
    ) -> list[ErasureSchedule]: ...
    def due_erasures(self, tenant_id: str, now: dt.datetime) -> Iterable[ErasureSchedule]: ...
    def save_hold(self, hold: LegalHold) -> None: ...
    def list_holds(
        self, tenant_id: str, *, principal: str | None = None, active_only: bool = True
    ) -> list[LegalHold]: ...
    def record_activity(self, tenant_id: str, principal: str, at: dt.datetime) -> None: ...
    def last_activity(self, tenant_id: str, principal: str) -> dt.datetime | None: ...

    # notifications
    def save_delivery(self, receipt: DeliveryReceipt) -> None: ...
    def get_delivery(self, tenant_id: str, receipt_id: str) -> DeliveryReceipt | None: ...


M = TypeVar("M", ConsentEvent, AuditEntry)


class InMemoryRepository:
    """Thread-safe in-memory repository for tests, examples and scripts. Not durable."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.consent_events: list[ConsentEvent] = []
        self.audit: list[AuditEntry] = []
        self.notices: list[Notice] = []
        self.requests: dict[str, RightsRequest] = {}
        self.nominees: dict[str, Nominee] = {}
        self.schedules: dict[str, ErasureSchedule] = {}
        self.holds: dict[str, LegalHold] = {}
        self.activity: dict[tuple[str, str], dt.datetime] = {}
        self.deliveries: dict[str, DeliveryReceipt] = {}

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            yield

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _copy(model: M) -> M:
        return model.model_copy(deep=True)

    def _append(self, rows: list[M], row: M) -> None:
        with self._lock:
            if any(r.tenant_id == row.tenant_id and r.seq == row.seq for r in rows):
                raise SequenceConflict(f"sequence {row.seq} already exists for tenant {row.tenant_id}")
            rows.append(self._copy(row))

    # ------------------------------------------------------------------ consent ledger

    def append_consent_event(self, event: ConsentEvent) -> None:
        self._append(self.consent_events, event)

    def last_consent_event(self, tenant_id: str) -> ConsentEvent | None:
        rows = [e for e in self.consent_events if e.tenant_id == tenant_id]
        return self._copy(max(rows, key=lambda e: e.seq)) if rows else None

    def latest_consent_event(self, tenant_id: str, principal: str, purpose: str) -> ConsentEvent | None:
        rows = [
            e
            for e in self.consent_events
            if e.tenant_id == tenant_id and e.principal == principal and e.purpose == purpose
        ]
        return self._copy(max(rows, key=lambda e: e.seq)) if rows else None

    def list_consent_events(
        self, tenant_id: str, *, principal: str | None = None, after_seq: int = 0, limit: int | None = None
    ) -> list[ConsentEvent]:
        rows = sorted(
            (
                e
                for e in self.consent_events
                if e.tenant_id == tenant_id and e.seq > after_seq and (principal is None or e.principal == principal)
            ),
            key=lambda e: e.seq,
        )
        return [self._copy(e) for e in rows[:limit]]

    # ------------------------------------------------------------------ audit

    def append_audit(self, entry: AuditEntry) -> None:
        self._append(self.audit, entry)

    def last_audit(self, tenant_id: str) -> AuditEntry | None:
        rows = [a for a in self.audit if a.tenant_id == tenant_id]
        return self._copy(max(rows, key=lambda a: a.seq)) if rows else None

    def list_audit(
        self, tenant_id: str, *, subject_id: str | None = None, after_seq: int = 0, limit: int | None = None
    ) -> list[AuditEntry]:
        rows = sorted(
            (
                a
                for a in self.audit
                if a.tenant_id == tenant_id and a.seq > after_seq and (subject_id is None or a.subject_id == subject_id)
            ),
            key=lambda a: a.seq,
        )
        return [self._copy(a) for a in rows[:limit]]

    # ------------------------------------------------------------------ notices

    def save_notice(self, notice: Notice) -> None:
        with self._lock:
            self.notices = [
                n
                for n in self.notices
                if not (n.tenant_id == notice.tenant_id and n.version == notice.version and n.locale == notice.locale)
            ]
            self.notices.append(notice.model_copy(deep=True))

    def list_notices(self, tenant_id: str, *, version: int | None = None) -> list[Notice]:
        return [
            n.model_copy(deep=True)
            for n in sorted(self.notices, key=lambda n: (n.version, n.locale))
            if n.tenant_id == tenant_id and (version is None or n.version == version)
        ]

    # ------------------------------------------------------------------ rights

    def save_request(self, req: RightsRequest) -> None:
        self.requests[f"{req.tenant_id}:{req.id}"] = req.model_copy(deep=True)

    def get_request(self, tenant_id: str, request_id: str) -> RightsRequest | None:
        req = self.requests.get(f"{tenant_id}:{request_id}")
        return req.model_copy(deep=True) if req else None

    def list_requests(
        self,
        tenant_id: str,
        *,
        principal: str | None = None,
        statuses: Collection[RequestStatus] | None = None,
    ) -> list[RightsRequest]:
        return [
            r.model_copy(deep=True)
            for r in sorted(self.requests.values(), key=lambda r: r.opened_at)
            if r.tenant_id == tenant_id
            and (principal is None or r.principal == principal)
            and (statuses is None or r.status in statuses)
        ]

    def save_nominee(self, nominee: Nominee) -> None:
        self.nominees[f"{nominee.tenant_id}:{nominee.id}"] = nominee.model_copy(deep=True)

    def list_nominees(self, tenant_id: str, principal: str) -> list[Nominee]:
        return [
            n.model_copy(deep=True)
            for n in self.nominees.values()
            if n.tenant_id == tenant_id and n.principal == principal
        ]

    # ------------------------------------------------------------------ retention

    def save_schedule(self, schedule: ErasureSchedule) -> None:
        self.schedules[f"{schedule.tenant_id}:{schedule.id}"] = schedule.model_copy(deep=True)

    def get_schedule(self, tenant_id: str, schedule_id: str) -> ErasureSchedule | None:
        s = self.schedules.get(f"{tenant_id}:{schedule_id}")
        return s.model_copy(deep=True) if s else None

    def list_schedules(
        self,
        tenant_id: str,
        *,
        principal: str | None = None,
        statuses: Collection[ScheduleStatus] | None = None,
    ) -> list[ErasureSchedule]:
        return [
            s.model_copy(deep=True)
            for s in sorted(self.schedules.values(), key=lambda s: (s.erase_at, s.id))
            if s.tenant_id == tenant_id
            and (principal is None or s.principal == principal)
            and (statuses is None or s.status in statuses)
        ]

    def due_erasures(self, tenant_id: str, now: dt.datetime) -> list[ErasureSchedule]:
        return [
            s
            for s in self.list_schedules(tenant_id, statuses=ACTIVE_SCHEDULE_STATUSES)
            if s.warn_at <= now or s.erase_at <= now
        ]

    def save_hold(self, hold: LegalHold) -> None:
        self.holds[f"{hold.tenant_id}:{hold.id}"] = hold.model_copy(deep=True)

    def list_holds(self, tenant_id: str, *, principal: str | None = None, active_only: bool = True) -> list[LegalHold]:
        return [
            h.model_copy(deep=True)
            for h in self.holds.values()
            if h.tenant_id == tenant_id
            and (principal is None or h.principal == principal)
            and (not active_only or h.released_at is None)
        ]

    def record_activity(self, tenant_id: str, principal: str, at: dt.datetime) -> None:
        key = (tenant_id, principal)
        previous = self.activity.get(key)
        if previous is None or at > previous:
            self.activity[key] = at

    def last_activity(self, tenant_id: str, principal: str) -> dt.datetime | None:
        return self.activity.get((tenant_id, principal))

    # ------------------------------------------------------------------ notifications

    def save_delivery(self, receipt: DeliveryReceipt) -> None:
        self.deliveries[f"{receipt.tenant_id}:{receipt.id}"] = receipt.model_copy(deep=True)

    def get_delivery(self, tenant_id: str, receipt_id: str) -> DeliveryReceipt | None:
        d = self.deliveries.get(f"{tenant_id}:{receipt_id}")
        return d.model_copy(deep=True) if d else None
