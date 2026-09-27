from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dpdpkit import (
    ConsentRequired,
    ConsentStatus,
    InMemoryRepository,
    InvalidTransition,
    Kit,
    LedgerTampered,
    NotFound,
    ValidationFailed,
)
from dpdpkit.ledger import GENESIS, row_hash
from dpdpkit.models import ConsentAction, ScheduleStatus


def test_grant_check_withdraw(kit: Kit) -> None:
    assert kit.consent.check("u1", "marketing") is False
    state = kit.consent.grant("u1", "marketing")
    assert state.status is ConsentStatus.GRANTED and state.notice_version == 1
    assert kit.consent.check("u1", "marketing") is True
    kit.consent.withdraw("u1", "marketing")
    assert kit.consent.check("u1", "marketing") is False
    with pytest.raises(ConsentRequired) as exc:
        kit.consent.require("u1", "marketing")
    assert exc.value.purpose == "marketing"


def test_record_many_decisions_unbundled(kit: Kit) -> None:
    states = kit.consent.record("u1", {"marketing": True, "analytics": False})
    assert [s.status for s in states] == [ConsentStatus.GRANTED, ConsentStatus.DENIED]
    history = kit.consent.history("u1")
    assert [e.action for e in history] == [ConsentAction.GRANT, ConsentAction.DENY]
    assert all(e.notice_hash == kit.notices.current().content_hash for e in history)


def test_consent_needs_a_published_notice(kit: Kit) -> None:
    kit.repository.notices.clear()
    with pytest.raises(ValidationFailed):
        kit.consent.grant("u1", "marketing")


def test_legitimate_use_needs_no_consent(kit: Kit) -> None:
    assert kit.consent.check("u1", "billing") is True
    with pytest.raises(ValidationFailed):
        kit.consent.grant("u1", "billing")
    with pytest.raises(NotFound):
        kit.consent.check("u1", "nope")


def test_withdraw_without_grant_is_refused(kit: Kit) -> None:
    with pytest.raises(InvalidTransition):
        kit.consent.withdraw("u1", "marketing")


def test_withdrawal_schedules_erasure_with_warning_window(kit: Kit, clock) -> None:  # type: ignore[no-untyped-def]
    kit.consent.grant("u1", "marketing")
    kit.consent.withdraw("u1", "marketing")
    [schedule] = kit.retention.schedules("u1")
    assert schedule.purpose == "marketing" and schedule.status is ScheduleStatus.SCHEDULED
    assert schedule.erase_at - clock.now >= kit.policy.erasure_pre_notice


def test_receipt_is_signed_and_tamper_evident(kit: Kit) -> None:
    kit.consent.grant("u1", "marketing")
    receipt = kit.consent.receipt("u1")
    assert kit.consent.verify_receipt(receipt)
    assert receipt.ledger_head == kit.repository.last_consent_event(kit.tenant_id).hash
    forged = receipt.model_copy(update={"principal": "u2"})
    assert not kit.consent.verify_receipt(forged)


def test_ledger_chain_links(kit: Kit) -> None:
    kit.consent.grant("u1", "marketing")
    kit.consent.grant("u2", "marketing")
    events = kit.repository.list_consent_events(kit.tenant_id)
    assert events[0].prev_hash == GENESIS and events[1].prev_hash == events[0].hash
    assert [e.seq for e in events] == [1, 2]
    result = kit.ledger.verify()
    assert result.ok and result.checked == len(events) + len(kit.repository.audit)


def test_tampering_with_a_row_is_detected_and_named(kit: Kit) -> None:
    kit.consent.grant("u1", "marketing")
    kit.consent.grant("u2", "marketing")
    repo = kit.repository
    assert isinstance(repo, InMemoryRepository)
    target = repo.consent_events[0]
    repo.consent_events[0] = target.model_copy(update={"action": ConsentAction.DENY})
    with pytest.raises(LedgerTampered) as exc:
        kit.ledger.verify()
    assert exc.value.row_id == target.id and exc.value.chain == "consent"
    report = kit.ledger.verify_report()
    assert not report.ok and report.failed_row == target.id


def test_rehashed_edit_breaks_the_next_link(kit: Kit) -> None:
    kit.consent.grant("u1", "marketing")
    kit.consent.grant("u2", "marketing")
    repo = kit.repository
    edited = repo.consent_events[0].model_copy(update={"principal": "someone-else"})
    repo.consent_events[0] = edited.model_copy(update={"hash": row_hash(edited)})
    with pytest.raises(LedgerTampered) as exc:
        kit.ledger.verify()
    assert exc.value.row_id == repo.consent_events[1].id


def test_deleted_row_and_truncation_are_detected(kit: Kit) -> None:
    for p in ("u1", "u2", "u3"):
        kit.consent.grant(p, "marketing")
    anchor = kit.ledger.root_hash()
    repo = kit.repository
    removed = repo.consent_events.pop(1)
    with pytest.raises(LedgerTampered) as exc:
        kit.ledger.verify()
    assert "seq" in exc.value.reason
    repo.consent_events.insert(1, removed)
    repo.consent_events.pop()  # truncating the tail keeps the chain valid...
    kit.ledger.verify()
    with pytest.raises(LedgerTampered):  # ...but not against an exported root hash
        kit.ledger.check_root(anchor)


def test_audit_tampering_is_detected(kit: Kit) -> None:
    kit.rights.open("u1", "access")
    repo = kit.repository
    repo.audit[-1] = repo.audit[-1].model_copy(update={"actor": "mallory"})
    with pytest.raises(LedgerTampered) as exc:
        kit.ledger.verify()
    assert exc.value.chain == "audit"


ops = st.lists(
    st.tuples(st.sampled_from(["u1", "u2", "u3"]), st.sampled_from(["marketing", "analytics"]), st.booleans()),
    min_size=1,
    max_size=25,
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(ops=ops, data=st.data())
def test_property_any_edit_is_detected(make_kit, ops, data) -> None:  # type: ignore[no-untyped-def]
    kit = make_kit()
    for principal, purpose, grant in ops:
        if grant:
            kit.consent.grant(principal, purpose)
        elif kit.consent.check(principal, purpose):
            kit.consent.withdraw(principal, purpose)
        else:
            kit.consent.deny(principal, purpose)
    kit.ledger.verify()
    for principal, purpose, _ in ops:
        state = kit.consent.state_for(principal, purpose)
        last = [e for e in kit.consent.history(principal) if e.purpose == purpose][-1]
        assert state is not None and state.event_id == last.id

    rows = kit.repository.consent_events
    index = data.draw(st.integers(min_value=0, max_value=len(rows) - 1))
    field = data.draw(st.sampled_from(["principal", "purpose", "source"]))
    rows[index] = rows[index].model_copy(update={field: getattr(rows[index], field) + "-x"})
    with pytest.raises(LedgerTampered) as exc:
        kit.ledger.verify()
    assert exc.value.row_id == rows[index].id
