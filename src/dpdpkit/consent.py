"""Current consent per principal and purpose, backed by the hash-chained ledger."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ._util import canonical_json, hmac_sha256_hex, new_id
from .errors import ConsentRequired, InvalidTransition, NotFound, ValidationFailed
from .events import CONSENT_DENIED, CONSENT_GRANTED, CONSENT_WITHDRAWN
from .ledger import GENESIS
from .models import (
    ConsentAction,
    ConsentEvent,
    ConsentReceipt,
    ConsentState,
    ConsentStatus,
    LegalBasis,
    Notice,
)

if TYPE_CHECKING:
    from .kit import Kit

_STATUS = {
    ConsentAction.GRANT: ConsentStatus.GRANTED,
    ConsentAction.DENY: ConsentStatus.DENIED,
    ConsentAction.WITHDRAW: ConsentStatus.WITHDRAWN,
}
_EVENT = {
    ConsentAction.GRANT: CONSENT_GRANTED,
    ConsentAction.DENY: CONSENT_DENIED,
    ConsentAction.WITHDRAW: CONSENT_WITHDRAWN,
}


def _state(event: ConsentEvent) -> ConsentState:
    return ConsentState(
        tenant_id=event.tenant_id,
        principal=event.principal,
        purpose=event.purpose,
        status=_STATUS[event.action],
        notice_version=event.notice_version,
        updated_at=event.occurred_at,
        event_id=event.id,
    )


class ConsentService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit

    # ------------------------------------------------------------------ helpers

    def _consent_purpose(self, purpose: str) -> None:
        p = self._kit.registry.purpose(purpose)
        if p.legal_basis is not LegalBasis.CONSENT:
            raise ValidationFailed(f"purpose {purpose!r} relies on {p.legal_basis.value}, not consent", purpose=purpose)

    def _notice(self, notice_version: int | None, locale: str | None) -> Notice:
        try:
            if notice_version is None:
                return self._kit.notices.current(locale)
            return self._kit.notices.get(notice_version, locale)
        except NotFound as exc:
            raise ValidationFailed(f"consent must reference a published notice: {exc.message}") from exc

    def _record(
        self,
        principal: str,
        purpose: str,
        action: ConsentAction,
        *,
        notice: Notice | None,
        source: str,
        metadata: dict[str, Any] | None,
    ) -> ConsentState:
        event = self._kit.ledger.append_consent(
            principal=principal,
            purpose=purpose,
            action=action,
            notice_version=notice.version if notice else None,
            notice_hash=notice.content_hash if notice else None,
            locale=notice.locale if notice else None,
            source=source,
            metadata=metadata,
        )
        state = _state(event)
        self._kit.events.publish(
            _EVENT[action],
            self._kit.tenant_id,
            {"principal": principal, "purpose": purpose, "event_id": event.id, "notice_version": event.notice_version},
            event.occurred_at,
        )
        if action is ConsentAction.WITHDRAW:
            self._kit.retention.on_withdrawal(principal, purpose)
        return state

    # ------------------------------------------------------------------ public API

    def record(
        self,
        principal: str,
        decisions: Mapping[str, bool],
        *,
        notice_version: int | None = None,
        locale: str | None = None,
        source: str = "api",
        metadata: dict[str, Any] | None = None,
    ) -> list[ConsentState]:
        """Record one decision per purpose, all against the same notice (no bundled consent)."""
        if not decisions:
            raise ValidationFailed("at least one purpose decision is required")
        notice = self._notice(notice_version, locale)
        listed = {p.id for p in notice.content.purposes}
        for purpose in decisions:
            self._consent_purpose(purpose)
            if purpose not in listed:
                raise ValidationFailed(
                    f"purpose {purpose!r} is not itemised in notice version {notice.version}", purpose=purpose
                )
        states = []
        with self._kit.repository.transaction():
            for purpose, granted in decisions.items():
                action = ConsentAction.GRANT if granted else ConsentAction.DENY
                states.append(self._record(principal, purpose, action, notice=notice, source=source, metadata=metadata))
        return states

    def grant(
        self,
        principal: str,
        purpose: str,
        notice_version: int | str | None = None,
        *,
        locale: str | None = None,
        source: str = "api",
        metadata: dict[str, Any] | None = None,
    ) -> ConsentState:
        version = int(notice_version) if notice_version is not None else None
        return self.record(
            principal, {purpose: True}, notice_version=version, locale=locale, source=source, metadata=metadata
        )[0]

    def deny(
        self,
        principal: str,
        purpose: str,
        notice_version: int | str | None = None,
        *,
        locale: str | None = None,
        source: str = "api",
    ) -> ConsentState:
        version = int(notice_version) if notice_version is not None else None
        return self.record(principal, {purpose: False}, notice_version=version, locale=locale, source=source)[0]

    def withdraw(
        self, principal: str, purpose: str, *, source: str = "api", metadata: dict[str, Any] | None = None
    ) -> ConsentState:
        """Withdraw one purpose. Takes the same single call as granting (withdrawal parity)."""
        self._consent_purpose(purpose)
        current = self.state_for(principal, purpose)
        if current is None or current.status is not ConsentStatus.GRANTED:
            raise InvalidTransition(f"no active consent for {purpose!r} to withdraw", purpose=purpose)
        with self._kit.repository.transaction():
            return self._record(
                principal, purpose, ConsentAction.WITHDRAW, notice=None, source=source, metadata=metadata
            )

    def state_for(self, principal: str, purpose: str) -> ConsentState | None:
        event = self._kit.repository.latest_consent_event(self._kit.tenant_id, principal, purpose)
        return _state(event) if event else None

    def check(self, principal: str, purpose: str) -> bool:
        """True when processing for ``purpose`` is allowed for ``principal``.

        Purposes with a Section 7 legitimate use do not need consent and always return True.
        Unknown purposes raise :class:`~dpdpkit.errors.NotFound` (a programming error, not a denial).
        """
        p = self._kit.registry.purpose(purpose)
        if p.legal_basis is not LegalBasis.CONSENT:
            return True
        state = self.state_for(principal, purpose)
        return state is not None and state.status is ConsentStatus.GRANTED

    def require(self, principal: str, purpose: str) -> None:
        if not self.check(principal, purpose):
            raise ConsentRequired(purpose)

    def state(self, principal: str) -> list[ConsentState]:
        states = []
        for purpose in self._kit.registry.consent_purposes():
            s = self.state_for(principal, purpose.id)
            if s is not None:
                states.append(s)
        return states

    def history(self, principal: str) -> list[ConsentEvent]:
        return list(self._kit.ledger.consent_events(principal=principal))

    # ------------------------------------------------------------------ receipts

    def _signature(self, payload: dict[str, Any]) -> str:
        return hmac_sha256_hex(self._kit.signing_key, canonical_json(payload))

    def receipt(self, principal: str) -> ConsentReceipt:
        last = self._kit.repository.last_consent_event(self._kit.tenant_id)
        unsigned = {
            "tenant_id": self._kit.tenant_id,
            "receipt_id": new_id("rcpt"),
            "principal": principal,
            "issued_at": self._kit.now(),
            "consents": self.state(principal),
            "ledger_head": last.hash if last else GENESIS,
            "algorithm": "HMAC-SHA256",
        }
        payload = ConsentReceipt.model_validate({**unsigned, "signature": ""}).model_dump(
            mode="python", exclude={"signature"}
        )
        return ConsentReceipt.model_validate({**unsigned, "signature": self._signature(payload)})

    def verify_receipt(self, receipt: ConsentReceipt) -> bool:
        payload = receipt.model_dump(mode="python", exclude={"signature"})
        return hmac.compare_digest(self._signature(payload), receipt.signature)
