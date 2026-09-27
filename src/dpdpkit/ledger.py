"""Append-only, hash-chained consent ledger and audit trail.

Each tenant has two chains: ``consent`` (grant/deny/withdraw events) and ``audit`` (every other state
change). A row's hash is SHA-256 over its canonical JSON (all fields except ``hash``), which includes
``prev_hash`` and ``seq``. Editing a row breaks its own hash; deleting one breaks ``seq`` continuity;
truncating the tail is caught by comparing with an exported :class:`~dpdpkit.models.RootHash`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, TypeVar

from ._util import canonical_json, new_id, sha256_hex
from .errors import LedgerTampered, SequenceConflict
from .models import AuditEntry, ConsentAction, ConsentEvent, LedgerVerification, RootHash

if TYPE_CHECKING:
    from .kit import Kit
    from .repository import Repository

GENESIS = "0" * 64
_BATCH = 500
_MAX_RETRIES = 5

Row = TypeVar("Row", ConsentEvent, AuditEntry)


def row_hash(row: ConsentEvent | AuditEntry) -> str:
    return sha256_hex(canonical_json(row.model_dump(mode="python", exclude={"hash"})))


class Ledger:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit

    @property
    def _repo(self) -> Repository:
        return self._kit.repository

    # ------------------------------------------------------------------ writing

    def _append(
        self,
        last: Callable[[], Row | None],
        build: Callable[[int, str], Row],
        store: Callable[[Row], None],
    ) -> Row:
        for _ in range(_MAX_RETRIES):
            prev = last()
            seq = prev.seq + 1 if prev else 1
            prev_hash = prev.hash if prev else GENESIS
            row = build(seq, prev_hash)
            row = row.model_copy(update={"hash": row_hash(row)})
            try:
                store(row)
                return row
            except SequenceConflict:
                continue
        raise SequenceConflict("could not append to ledger after retries; too much contention")

    def append_consent(
        self,
        *,
        principal: str,
        purpose: str,
        action: ConsentAction,
        notice_version: int | None,
        notice_hash: str | None,
        locale: str | None,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> ConsentEvent:
        tenant = self._kit.tenant_id
        now = self._kit.now()

        def build(seq: int, prev_hash: str) -> ConsentEvent:
            return ConsentEvent(
                tenant_id=tenant,
                id=new_id("ce"),
                seq=seq,
                principal=principal,
                purpose=purpose,
                action=action,
                notice_version=notice_version,
                notice_hash=notice_hash,
                locale=locale,
                source=source,
                occurred_at=now,
                metadata=metadata or {},
                prev_hash=prev_hash,
                hash="",
            )

        return self._append(lambda: self._repo.last_consent_event(tenant), build, self._repo.append_consent_event)

    def audit(
        self,
        action: str,
        *,
        subject_type: str,
        subject_id: str,
        actor: str = "system",
        data: dict[str, Any] | None = None,
    ) -> AuditEntry:
        tenant = self._kit.tenant_id
        now = self._kit.now()

        def build(seq: int, prev_hash: str) -> AuditEntry:
            return AuditEntry(
                tenant_id=tenant,
                id=new_id("au"),
                seq=seq,
                actor=actor,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                occurred_at=now,
                data=data or {},
                prev_hash=prev_hash,
                hash="",
            )

        return self._append(lambda: self._repo.last_audit(tenant), build, self._repo.append_audit)

    # ------------------------------------------------------------------ reading

    def consent_events(self, principal: str | None = None) -> Iterator[ConsentEvent]:
        after = 0
        while True:
            batch = self._repo.list_consent_events(
                self._kit.tenant_id, principal=principal, after_seq=after, limit=_BATCH
            )
            yield from batch
            if len(batch) < _BATCH:
                return
            after = batch[-1].seq

    def audit_entries(self, subject_id: str | None = None) -> Iterator[AuditEntry]:
        after = 0
        while True:
            batch = self._repo.list_audit(self._kit.tenant_id, subject_id=subject_id, after_seq=after, limit=_BATCH)
            yield from batch
            if len(batch) < _BATCH:
                return
            after = batch[-1].seq

    # ------------------------------------------------------------------ verification

    @staticmethod
    def _verify_chain(chain: str, rows: Iterator[ConsentEvent] | Iterator[AuditEntry]) -> tuple[int, str]:
        expected_prev = GENESIS
        expected_seq = 1
        count = 0
        for row in rows:
            if row.seq != expected_seq:
                raise LedgerTampered(row.id, chain, f"expected seq {expected_seq}, found {row.seq} (row missing)")
            if row.prev_hash != expected_prev:
                raise LedgerTampered(row.id, chain, "prev_hash does not match the previous row")
            if row_hash(row) != row.hash:
                raise LedgerTampered(row.id, chain, "row contents do not match its hash")
            expected_prev = row.hash
            expected_seq += 1
            count += 1
        return count, expected_prev

    def verify(self) -> LedgerVerification:
        """Check both chains. Raises :class:`LedgerTampered` naming the first bad row."""
        consent_count, consent_head = self._verify_chain("consent", self.consent_events())
        audit_count, audit_head = self._verify_chain("audit", self.audit_entries())
        return LedgerVerification(
            tenant_id=self._kit.tenant_id,
            ok=True,
            checked=consent_count + audit_count,
            consent_head=consent_head,
            audit_head=audit_head,
            verified_at=self._kit.now(),
        )

    def verify_report(self) -> LedgerVerification:
        """Like :meth:`verify` but returns a failed report instead of raising."""
        try:
            return self.verify()
        except LedgerTampered as exc:
            last_consent = self._repo.last_consent_event(self._kit.tenant_id)
            last_audit = self._repo.last_audit(self._kit.tenant_id)
            return LedgerVerification(
                tenant_id=self._kit.tenant_id,
                ok=False,
                checked=0,
                consent_head=last_consent.hash if last_consent else GENESIS,
                audit_head=last_audit.hash if last_audit else GENESIS,
                failed_row=exc.row_id,
                chain=exc.chain,
                reason=exc.reason,
                verified_at=self._kit.now(),
            )

    def root_hash(self) -> RootHash:
        """Current heads of both chains, for periodic export to somewhere the operator cannot rewrite."""
        last_consent = self._repo.last_consent_event(self._kit.tenant_id)
        last_audit = self._repo.last_audit(self._kit.tenant_id)
        consent_head = last_consent.hash if last_consent else GENESIS
        audit_head = last_audit.hash if last_audit else GENESIS
        return RootHash(
            tenant_id=self._kit.tenant_id,
            consent_head=consent_head,
            consent_seq=last_consent.seq if last_consent else 0,
            audit_head=audit_head,
            audit_seq=last_audit.seq if last_audit else 0,
            root=sha256_hex(f"{self._kit.tenant_id}:{consent_head}:{audit_head}".encode()),
            exported_at=self._kit.now(),
        )

    def check_root(self, anchor: RootHash) -> None:
        """Confirm the chains still contain the rows an earlier root export covered."""
        for chain, seq, head, rows in (
            ("consent", anchor.consent_seq, anchor.consent_head, self.consent_events()),
            ("audit", anchor.audit_seq, anchor.audit_head, self.audit_entries()),
        ):
            if seq == 0:
                continue
            found = next((r for r in rows if r.seq == seq), None)
            if found is None:
                raise LedgerTampered(f"seq:{seq}", chain, "row covered by an exported root hash is missing")
            if found.hash != head:
                raise LedgerTampered(found.id, chain, "row differs from the exported root hash")
