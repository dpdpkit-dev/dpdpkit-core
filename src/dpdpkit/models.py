"""Pydantic v2 models for every dpdpkit aggregate. Every model carries ``tenant_id``.

No ORM type ever crosses the repository boundary; adapters convert rows to these models.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_TENANT = "default"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


class TenantModel(_Model):
    tenant_id: str = DEFAULT_TENANT


# --------------------------------------------------------------------------- enums


class LegalBasis(str, Enum):
    """Why processing is lawful: consent, or one of the legitimate uses in DPDP Act s.7."""

    CONSENT = "consent"
    S7_A_VOLUNTARY = "s7_a"  # voluntarily provided for a specified purpose
    S7_B_STATE_BENEFIT = "s7_b"  # subsidy, benefit, service, certificate, licence or permit
    S7_C_STATE_FUNCTION = "s7_c"  # sovereignty, integrity, security of the State
    S7_D_LEGAL_OBLIGATION = "s7_d"  # obligation to disclose to the State under law
    S7_E_COURT_ORDER = "s7_e"  # judgment, decree or order
    S7_F_MEDICAL_EMERGENCY = "s7_f"
    S7_G_PUBLIC_HEALTH = "s7_g"  # epidemic, outbreak, threat to public health
    S7_H_DISASTER = "s7_h"  # disaster or breakdown of public order
    S7_I_EMPLOYMENT = "s7_i"


class ConsentAction(str, Enum):
    GRANT = "grant"
    DENY = "deny"
    WITHDRAW = "withdraw"


class ConsentStatus(str, Enum):
    GRANTED = "granted"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


class RequestKind(str, Enum):
    ACCESS = "access"
    CORRECTION = "correction"
    COMPLETION = "completion"
    UPDATING = "updating"
    ERASURE = "erasure"
    NOMINATION = "nomination"
    GRIEVANCE = "grievance"


class RequestStatus(str, Enum):
    RECEIVED = "received"
    IDENTITY_PENDING = "identity_pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"


OPEN_REQUEST_STATUSES = frozenset({RequestStatus.RECEIVED, RequestStatus.IDENTITY_PENDING, RequestStatus.IN_PROGRESS})


class ScheduleStatus(str, Enum):
    SCHEDULED = "scheduled"
    WARNING_PENDING = "warning_pending"  # warning handed to a transport that has not confirmed delivery
    WARNED = "warned"
    HELD = "held"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"
    ERASED = "erased"


ACTIVE_SCHEDULE_STATUSES = frozenset(
    {
        ScheduleStatus.SCHEDULED,
        ScheduleStatus.WARNING_PENDING,
        ScheduleStatus.WARNED,
        ScheduleStatus.HELD,
        ScheduleStatus.NEEDS_ATTENTION,
    }
)


class RetentionTrigger(str, Enum):
    LAST_ACTIVITY = "last_activity"
    PURPOSE_COMPLETE = "purpose_complete"
    WITHDRAWAL = "withdrawal"


class Channel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class DeliveryStatus(str, Enum):
    DELIVERED = "delivered"
    QUEUED = "queued"
    FAILED = "failed"


class Role(str, Enum):
    VIEWER = "viewer"
    HANDLER = "handler"
    ADMIN = "admin"

    def allows(self, needed: Role) -> bool:
        order = [Role.VIEWER, Role.HANDLER, Role.ADMIN]
        return order.index(self) >= order.index(needed)


# --------------------------------------------------------------------------- registry


class DataItem(TenantModel):
    id: str
    name: str
    description: str = ""
    category: str | None = None
    sensitive: bool = False


class Processor(TenantModel):
    id: str
    name: str
    country: str = "IN"
    contact: str | None = None
    erasure_webhook: str | None = None


class DataStore(TenantModel):
    id: str
    name: str
    region: str = "IN"
    kind: str = "database"


class RetentionRule(TenantModel):
    trigger: RetentionTrigger = RetentionTrigger.LAST_ACTIVITY
    period_days: int | None = Field(default=None, ge=0)
    third_schedule_class: str | None = None
    """When set, the period comes from the policy pack's Third Schedule entry for this class."""


class Purpose(TenantModel):
    id: str
    title: str
    description: str = ""
    data_items: list[str] = Field(default_factory=list)
    legal_basis: LegalBasis = LegalBasis.CONSENT
    required: bool = False
    """Necessary to provide the service. Still itemised in the notice; never pre-ticked."""
    processors: list[str] = Field(default_factory=list)
    data_stores: list[str] = Field(default_factory=list)
    retention: RetentionRule | None = None


# --------------------------------------------------------------------------- notices


class NoticePurpose(_Model):
    id: str
    title: str
    description: str = ""
    data_items: list[str] = Field(default_factory=list)
    legal_basis: LegalBasis = LegalBasis.CONSENT
    required: bool = False


class NoticeContent(_Model):
    title: str
    body: str = ""
    purposes: list[NoticePurpose]
    links: dict[str, str]
    contact: dict[str, str] = Field(default_factory=dict)


class Notice(TenantModel):
    version: int = Field(ge=1)
    locale: str
    content: NoticeContent
    content_hash: str
    published_at: dt.datetime


# --------------------------------------------------------------------------- ledger


class ConsentEvent(TenantModel):
    id: str
    seq: int = Field(ge=1)
    principal: str
    purpose: str
    action: ConsentAction
    notice_version: int | None = None
    notice_hash: str | None = None
    locale: str | None = None
    source: str = "api"
    occurred_at: dt.datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str


class AuditEntry(TenantModel):
    id: str
    seq: int = Field(ge=1)
    actor: str
    action: str
    subject_type: str
    subject_id: str
    occurred_at: dt.datetime
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str


class ConsentState(TenantModel):
    principal: str
    purpose: str
    status: ConsentStatus
    notice_version: int | None
    updated_at: dt.datetime
    event_id: str


class ConsentReceipt(TenantModel):
    receipt_id: str
    principal: str
    issued_at: dt.datetime
    consents: list[ConsentState]
    ledger_head: str
    algorithm: Literal["HMAC-SHA256"] = "HMAC-SHA256"
    signature: str


class LedgerVerification(TenantModel):
    ok: bool
    checked: int
    consent_head: str
    audit_head: str
    failed_row: str | None = None
    chain: str | None = None
    reason: str | None = None
    verified_at: dt.datetime


class RootHash(TenantModel):
    consent_head: str
    consent_seq: int
    audit_head: str
    audit_seq: int
    root: str
    exported_at: dt.datetime


# --------------------------------------------------------------------------- rights


class RightsRequest(TenantModel):
    id: str
    principal: str
    kind: RequestKind
    status: RequestStatus
    opened_at: dt.datetime
    due_at: dt.datetime
    max_due_at: dt.datetime
    details: dict[str, Any] = Field(default_factory=dict)
    identity_verified: bool | None = None
    assignee: str | None = None
    response: str | None = None
    response_contact: dict[str, str] | None = None
    outcome: str | None = None
    closed_at: dt.datetime | None = None
    updated_at: dt.datetime


class Grievance(RightsRequest):
    """A rights request of kind ``grievance``; kept as a distinct type for adapters that want one."""

    kind: RequestKind = RequestKind.GRIEVANCE


class Nominee(TenantModel):
    id: str
    principal: str
    name: str
    contact: str
    relationship: str | None = None
    created_at: dt.datetime
    revoked_at: dt.datetime | None = None


class IdentityCheck(TenantModel):
    request_id: str
    verified: bool
    method: str | None = None
    checked_by: str
    checked_at: dt.datetime


class SlaStatus(_Model):
    due_at: dt.datetime
    max_due_at: dt.datetime
    remaining_seconds: int
    overdue: bool
    ceiling_breached: bool


# --------------------------------------------------------------------------- retention


class ErasureSchedule(TenantModel):
    id: str
    principal: str
    purpose: str
    trigger: RetentionTrigger
    status: ScheduleStatus
    erase_at: dt.datetime
    warn_at: dt.datetime
    reason: str = ""
    warning_receipt_id: str | None = None
    warned_at: dt.datetime | None = None
    attention_reason: str | None = None
    cancelled_reason: str | None = None
    erased_at: dt.datetime | None = None
    processor_fanout: dict[str, str] = Field(default_factory=dict)
    created_at: dt.datetime
    updated_at: dt.datetime


class LegalHold(TenantModel):
    id: str
    principal: str
    purpose: str | None = None
    """``None`` holds every purpose for the principal."""
    reason: str
    placed_by: str
    placed_at: dt.datetime
    released_at: dt.datetime | None = None


class ErasureRun(TenantModel):
    id: str
    started_at: dt.datetime
    finished_at: dt.datetime
    warned: int = 0
    warning_pending: int = 0
    erased: int = 0
    held: int = 0
    needs_attention: int = 0
    cancelled: int = 0


class PreviewItem(_Model):
    schedule_id: str
    principal: str
    purpose: str
    action: Literal["warn", "erase"]
    at: dt.datetime
    status: ScheduleStatus


# --------------------------------------------------------------------------- notifications


class Contact(_Model):
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    locale: str | None = None


class Message(_Model):
    channel: Channel
    to: str
    subject: str
    body: str
    template: str
    locale: str = "en"
    principal: str | None = None


class DeliveryReceipt(TenantModel):
    id: str
    principal: str | None
    channel: Channel
    to_masked: str
    template: str
    status: DeliveryStatus
    provider: str
    provider_ref: str | None = None
    error: str | None = None
    created_at: dt.datetime
    delivered_at: dt.datetime | None = None


# --------------------------------------------------------------------------- incidents


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ReportKind(str, Enum):
    BOARD_INITIAL = "board_initial"
    BOARD_DETAILED = "board_detailed"
    CERT_IN = "cert_in"


class ClockState(str, Enum):
    PENDING = "pending"
    DONE = "done"
    OVERDUE = "overdue"


class NoticeStatus(str, Enum):
    SENT = "sent"
    QUEUED = "queued"
    FAILED = "failed"
    NO_CONTACT = "no_contact"


class Incident(TenantModel):
    id: str
    title: str
    description: str = ""
    severity: IncidentSeverity = IncidentSeverity.MEDIUM
    detected_at: dt.datetime
    aware_at: dt.datetime
    """When the fiduciary became aware of the breach. Every deadline counts from here."""
    affected_count: int | None = None
    data_items: list[str] = Field(default_factory=list)
    root_cause: str = ""
    remediation: str = ""
    likely_consequences: str = ""
    safety_steps: str = ""
    """What affected principals can do to protect themselves; goes into their notice."""
    status: IncidentStatus = IncidentStatus.OPEN
    board_initial_sent_at: dt.datetime | None = None
    board_detailed_sent_at: dt.datetime | None = None
    certin_reported_at: dt.datetime | None = None
    report_references: dict[str, str] = Field(default_factory=dict)
    """Reference numbers the authorities gave back, keyed by :class:`ReportKind` value."""
    principal_notice_job: str | None = None
    created_by: str = "system"
    created_at: dt.datetime
    updated_at: dt.datetime
    closed_at: dt.datetime | None = None


class IncidentClock(_Model):
    kind: ReportKind
    due_at: dt.datetime | None
    """``None`` when the rule says "without delay" and sets no number of hours."""
    done_at: dt.datetime | None
    state: ClockState
    remaining: dt.timedelta | None = None
    """Time left (negative when overdue); ``None`` when there is no fixed deadline or it is done."""


class PrincipalNotice(TenantModel):
    id: str
    incident_id: str
    job_id: str
    principal: str
    status: NoticeStatus
    channel: Channel | None = None
    receipt_id: str | None = None
    error: str | None = None
    created_at: dt.datetime


class IncidentReport(_Model):
    """A draft for a person to review and submit. dpdpkit never sends it to an authority."""

    incident_id: str
    kind: ReportKind
    title: str
    body: str
    missing: list[str] = Field(default_factory=list)
    """Fields the incident record does not hold yet; each appears as a TO COMPLETE marker in ``body``."""
    generated_at: dt.datetime
    draft: Literal[True] = True
