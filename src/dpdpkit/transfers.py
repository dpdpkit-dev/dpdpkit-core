"""Data-store regions, the transfer register and the restricted-country check (DPDP Act s.16; Rules 2025, Rule 15).

Transfer abroad is allowed unless the Central Government has restricted the destination by notification. dpdpkit
does not know that list; you do. Fail-safe rules enforced here:

* **The denylist is yours to keep current.** It is the union of the policy pack's ``transfers.restricted_countries``
  (when present) and the countries set with :meth:`TransferService.set_restricted_countries`. Both start empty.
* **A restricted destination cannot be registered.** :meth:`TransferService.record` raises
  :class:`~dpdpkit.errors.TransferRestricted`. A country added to the list later does not rewrite history:
  :meth:`TransferService.violations` lists the active transfers that now point at it.
* **The registry is checked, not trusted.** :meth:`TransferService.review` walks ``Processor.country`` and
  ``DataStore.region`` and reports every foreign entry that is restricted or has no active register entry.
* Country codes are ISO 3166-1 alpha-2, upper-cased. ``IN`` is domestic.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._util import new_id
from .errors import ConfigError, NotFound, TransferRestricted, ValidationFailed
from .events import TRANSFER_ENDED, TRANSFER_RECORDED, TRANSFER_RESTRICTIONS_CHANGED
from .models import TransferRecord
from .repository import TransferRepository

if TYPE_CHECKING:
    from .kit import Kit

DOMESTIC = "IN"
POLICY_KEY = "transfers.restricted_countries"


def normalise_country(code: str) -> str:
    code = code.strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise ValidationFailed(f"{code!r} is not an ISO 3166-1 alpha-2 country code", country=code)
    return code


@dataclass(frozen=True)
class TransferCheck:
    country: str
    cross_border: bool
    restricted: bool

    @property
    def allowed(self) -> bool:
        return not self.restricted


@dataclass(frozen=True)
class TransferFinding:
    """A foreign processor or data store found in the registry."""

    kind: str
    """``processor`` or ``data_store``."""
    id: str
    country: str
    restricted: bool
    registered: bool
    """An active register entry covers it."""

    @property
    def problem(self) -> bool:
        return self.restricted or not self.registered


class TransferService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit
        self._restricted: set[str] = set()

    @property
    def _repo(self) -> TransferRepository:
        repo = self._kit.repository
        if not isinstance(repo, TransferRepository):
            raise ConfigError("repository does not implement dpdpkit.repository.TransferRepository")
        return repo

    # ------------------------------------------------------------------ restricted countries

    def restricted_countries(self) -> list[str]:
        from_policy = self._kit.policy.get(POLICY_KEY, [])
        return sorted({normalise_country(str(c)) for c in from_policy} | self._restricted)

    def set_restricted_countries(self, countries: Iterable[str], *, actor: str, reason: str = "") -> list[str]:
        """Replace the locally kept denylist (the policy pack's entries always apply on top)."""
        new = {normalise_country(c) for c in countries}
        if DOMESTIC in new:
            raise ValidationFailed("India cannot be a restricted destination")
        before = self.restricted_countries()
        self._restricted.clear()  # in place: tenant views share the set
        self._restricted.update(new)
        after = self.restricted_countries()
        if before != after:
            self._kit.ledger.audit(
                "transfer.restrictions_changed",
                subject_type="transfer_restrictions",
                subject_id=self._kit.tenant_id,
                actor=actor,
                data={
                    "added": sorted(set(after) - set(before)),
                    "removed": sorted(set(before) - set(after)),
                    "reason": reason,
                },
            )
            self._kit.events.publish(
                TRANSFER_RESTRICTIONS_CHANGED, self._kit.tenant_id, {"restricted": after}, self._kit.now()
            )
        return after

    def check(self, country: str) -> TransferCheck:
        """Is moving personal data to ``country`` permitted? Domestic destinations are never restricted."""
        code = normalise_country(country)
        cross = code != DOMESTIC
        return TransferCheck(code, cross, cross and code in self.restricted_countries())

    def require_allowed(self, country: str) -> TransferCheck:
        result = self.check(country)
        if result.restricted:
            raise TransferRestricted(result.country)
        return result

    # ------------------------------------------------------------------ register

    def record(
        self,
        *,
        actor: str,
        processor_id: str | None = None,
        data_store_id: str | None = None,
        destination: str | None = None,
        purposes: Iterable[str] = (),
        data_items: Iterable[str] = (),
        safeguards: str = "",
    ) -> TransferRecord:
        """Register a transfer abroad. Name a processor or a data store from the registry (the destination is then
        its country or region) or give ``destination`` yourself. Raises :class:`TransferRestricted` for a
        restricted destination."""
        if processor_id is None and data_store_id is None and destination is None:
            raise ValidationFailed("give a processor_id, a data_store_id or a destination")
        derived: set[str] = set()
        registry = self._kit.registry
        if processor_id is not None:
            proc = next((p for p in registry.processors() if p.id == processor_id), None)
            if proc is None:
                raise NotFound(f"unknown processor {processor_id!r}", processor=processor_id)
            derived.add(normalise_country(proc.country))
        if data_store_id is not None:
            store = next((s for s in registry.data_stores() if s.id == data_store_id), None)
            if store is None:
                raise NotFound(f"unknown data store {data_store_id!r}", data_store=data_store_id)
            derived.add(normalise_country(store.region))
        if destination is not None:
            derived.add(normalise_country(destination))
        if len(derived) != 1:
            raise ValidationFailed(
                "processor, data store and destination are in different countries", countries=sorted(derived)
            )
        code = derived.pop()
        if code == DOMESTIC:
            raise ValidationFailed("the destination is in India; only transfers abroad are registered")
        self.require_allowed(code)
        record = TransferRecord(
            id=new_id("xfer"),
            tenant_id=self._kit.tenant_id,
            destination=code,
            processor_id=processor_id,
            data_store_id=data_store_id,
            purposes=list(purposes),
            data_items=list(data_items),
            safeguards=safeguards.strip(),
            created_by=actor,
            created_at=self._kit.now(),
        )
        with self._kit.repository.transaction():
            self._kit.ledger.audit(
                "transfer.recorded",
                subject_type="transfer",
                subject_id=record.id,
                actor=actor,
                data={"destination": code, "processor_id": processor_id, "data_store_id": data_store_id},
            )
            self._repo.save_transfer(record)
        self._kit.events.publish(
            TRANSFER_RECORDED, self._kit.tenant_id, {"transfer_id": record.id, "destination": code}, self._kit.now()
        )
        return record

    def end(self, transfer_id: str, *, actor: str, reason: str = "") -> TransferRecord:
        """Mark a transfer as stopped. The entry stays in the register."""
        record = self.get(transfer_id)
        if not record.active:
            raise ValidationFailed("transfer has already ended", transfer_id=transfer_id)
        record = record.model_copy(
            update={"ended_at": self._kit.now(), "ended_by": actor, "end_reason": reason or None}
        )
        with self._kit.repository.transaction():
            self._kit.ledger.audit(
                "transfer.ended",
                subject_type="transfer",
                subject_id=record.id,
                actor=actor,
                data={"destination": record.destination, "reason": reason},
            )
            self._repo.save_transfer(record)
        self._kit.events.publish(
            TRANSFER_ENDED,
            self._kit.tenant_id,
            {"transfer_id": record.id, "destination": record.destination},
            self._kit.now(),
        )
        return record

    def get(self, transfer_id: str) -> TransferRecord:
        record = self._repo.get_transfer(self._kit.tenant_id, transfer_id)
        if record is None:
            raise NotFound(f"no transfer {transfer_id!r}", transfer_id=transfer_id)
        return record

    def records(self, *, active_only: bool = False) -> list[TransferRecord]:
        return self._repo.list_transfers(self._kit.tenant_id, active_only=active_only)

    # ------------------------------------------------------------------ review

    def violations(self) -> list[TransferRecord]:
        """Active register entries whose destination is restricted now."""
        restricted = set(self.restricted_countries())
        return [t for t in self.records(active_only=True) if t.destination in restricted]

    def review(self) -> list[TransferFinding]:
        """Every foreign processor and data store in the registry, with whether it is restricted and whether an
        active register entry covers it. Filter on :attr:`TransferFinding.problem` for what needs attention."""
        active = self.records(active_only=True)
        restricted = set(self.restricted_countries())
        findings: list[TransferFinding] = []
        for proc in self._kit.registry.processors():
            code = normalise_country(proc.country)
            if code != DOMESTIC:
                covered = any(t.processor_id == proc.id for t in active)
                findings.append(TransferFinding("processor", proc.id, code, code in restricted, covered))
        for store in self._kit.registry.data_stores():
            code = normalise_country(store.region)
            if code != DOMESTIC:
                covered = any(t.data_store_id == store.id for t in active)
                findings.append(TransferFinding("data_store", store.id, code, code in restricted, covered))
        return findings
