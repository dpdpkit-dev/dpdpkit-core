"""Exceptions raised by dpdpkit. Each carries a stable ``code`` used in REST error bodies."""

from __future__ import annotations

from typing import Any


class DpdpkitError(Exception):
    code = "dpdpkit_error"
    http_status = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class PolicyError(DpdpkitError):
    """The policy pack is missing or invalid. The kit refuses to start."""

    code = "policy_invalid"


class ConfigError(DpdpkitError):
    code = "config_invalid"


class NotFound(DpdpkitError):
    code = "not_found"
    http_status = 404


class ValidationFailed(DpdpkitError):
    code = "invalid_request"
    http_status = 422


class NoticeInvalid(ValidationFailed):
    code = "notice_invalid"


class InvalidTransition(DpdpkitError):
    code = "invalid_transition"
    http_status = 409


class ConsentRequired(DpdpkitError):
    code = "consent_required"
    http_status = 403

    def __init__(self, purpose: str) -> None:
        super().__init__(f"consent required for purpose {purpose!r}", purpose=purpose)
        self.purpose = purpose


class SequenceConflict(DpdpkitError):
    """Raised by a repository when two writers race for the same ledger sequence number."""

    code = "sequence_conflict"
    http_status = 409


class LedgerTampered(DpdpkitError):
    """``verify()`` found a row whose hash or chain link does not match."""

    code = "ledger_tampered"

    def __init__(self, row_id: str, chain: str, reason: str) -> None:
        super().__init__(f"{chain} ledger row {row_id} failed verification: {reason}")
        self.row_id = row_id
        self.chain = chain
        self.reason = reason
        self.details = {"row_id": row_id, "chain": chain, "reason": reason}
