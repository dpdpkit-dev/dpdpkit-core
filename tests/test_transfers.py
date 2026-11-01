from __future__ import annotations

import pytest

from dpdpkit import Kit, Processor
from dpdpkit.errors import ConfigError, NotFound, TransferRestricted, ValidationFailed
from dpdpkit.events import TRANSFER_ENDED, TRANSFER_RECORDED, TRANSFER_RESTRICTIONS_CHANGED
from dpdpkit.models import DataStore


def _seen(kit: Kit) -> list[str]:
    seen: list[str] = []
    kit.events.subscribe("*", lambda e: seen.append(e.type))
    return seen


def test_check_and_denylist(kit: Kit) -> None:
    assert kit.transfers.restricted_countries() == []
    assert kit.transfers.check("us").allowed
    assert not kit.transfers.check("IN").cross_border
    seen = _seen(kit)
    assert kit.transfers.set_restricted_countries(["xx", " yy "], actor="dpo", reason="notified") == ["XX", "YY"]
    kit.transfers.set_restricted_countries(["XX", "YY"], actor="dpo")  # no change, no event
    assert seen == [TRANSFER_RESTRICTIONS_CHANGED]
    result = kit.transfers.check("xx")
    assert result.restricted and not result.allowed and result.cross_border
    with pytest.raises(TransferRestricted) as exc:
        kit.transfers.require_allowed("YY")
    assert exc.value.country == "YY"
    assert kit.transfers.require_allowed("US").allowed
    assert kit.ledger.verify()


def test_bad_country_codes(kit: Kit) -> None:
    for bad in ("", "USA", "1A", "é"):
        with pytest.raises(ValidationFailed):
            kit.transfers.check(bad)
    with pytest.raises(ValidationFailed):
        kit.transfers.set_restricted_countries(["IN"], actor="dpo")


def test_policy_denylist_applies(kit: Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    real = kit.policy.get
    monkeypatch.setattr(
        kit.policy, "get", lambda k, *a: ["zz"] if k == "transfers.restricted_countries" else real(k, *a)
    )
    assert kit.transfers.restricted_countries() == ["ZZ"]
    assert kit.transfers.check("ZZ").restricted


def test_record_from_registry_and_end(kit: Kit) -> None:
    kit.registry.add_processor(Processor(id="mail", name="Mailer", country="us"))
    kit.registry.add_data_store(DataStore(id="eu_db", name="EU DB", region="DE"))
    seen = _seen(kit)
    rec = kit.transfers.record(
        actor="dpo", processor_id="mail", purposes=["marketing"], data_items=["email"], safeguards=" DPA cl.7 "
    )
    assert rec.destination == "US" and rec.active and rec.safeguards == "DPA cl.7"
    store = kit.transfers.record(actor="dpo", data_store_id="eu_db", destination="de")
    assert store.destination == "DE"
    assert kit.transfers.get(rec.id) == rec
    assert [t.id for t in kit.transfers.records()] == [rec.id, store.id]
    ended = kit.transfers.end(rec.id, actor="dpo", reason="vendor dropped")
    assert not ended.active and ended.end_reason == "vendor dropped" and ended.ended_by == "dpo"
    assert [t.id for t in kit.transfers.records(active_only=True)] == [store.id]
    with pytest.raises(ValidationFailed):
        kit.transfers.end(rec.id, actor="dpo")
    assert seen == [TRANSFER_RECORDED, TRANSFER_RECORDED, TRANSFER_ENDED]
    assert kit.ledger.verify()


def test_record_validation(kit: Kit) -> None:
    kit.registry.add_processor(Processor(id="mail", name="Mailer", country="US"))
    kit.registry.add_processor(Processor(id="local", name="Local", country="IN"))
    with pytest.raises(ValidationFailed):
        kit.transfers.record(actor="dpo")
    with pytest.raises(NotFound):
        kit.transfers.record(actor="dpo", processor_id="nope")
    with pytest.raises(NotFound):
        kit.transfers.record(actor="dpo", data_store_id="nope")
    with pytest.raises(NotFound):
        kit.transfers.get("nope")
    with pytest.raises(ValidationFailed):
        kit.transfers.record(actor="dpo", processor_id="mail", destination="GB")
    with pytest.raises(ValidationFailed):
        kit.transfers.record(actor="dpo", processor_id="local")
    assert kit.transfers.records() == []


def test_restricted_destination_refused_and_violations(kit: Kit) -> None:
    kit.registry.add_processor(Processor(id="mail", name="Mailer", country="US"))
    rec = kit.transfers.record(actor="dpo", processor_id="mail")
    assert kit.transfers.violations() == []
    kit.transfers.set_restricted_countries(["US"], actor="dpo")
    assert [t.id for t in kit.transfers.violations()] == [rec.id]
    with pytest.raises(TransferRestricted):
        kit.transfers.record(actor="dpo", destination="US")
    kit.transfers.end(rec.id, actor="dpo")
    assert kit.transfers.violations() == []


def test_review_registry(kit: Kit) -> None:
    kit.registry.add_processor(Processor(id="mail", name="Mailer", country="US"))
    kit.registry.add_processor(Processor(id="sms", name="SMS", country="SG"))
    kit.registry.add_data_store(DataStore(id="eu_db", name="EU DB", region="DE"))
    kit.registry.add_data_store(DataStore(id="in_db", name="IN DB", region="IN"))
    kit.transfers.record(actor="dpo", processor_id="mail")
    kit.transfers.set_restricted_countries(["DE"], actor="dpo")
    found = {(f.kind, f.id): f for f in kit.transfers.review()}
    assert set(found) == {("processor", "mail"), ("processor", "sms"), ("data_store", "eu_db")}
    assert not found[("processor", "mail")].problem
    assert found[("processor", "sms")].problem and not found[("processor", "sms")].registered
    assert found[("data_store", "eu_db")].restricted and found[("data_store", "eu_db")].problem


def test_tenant_views_share_denylist_but_not_register(kit: Kit) -> None:
    kit.registry.add_processor(Processor(id="mail", name="Mailer", country="US"))
    kit.transfers.set_restricted_countries(["GB"], actor="dpo")
    other = kit.for_tenant("acme")
    assert other.transfers.check("GB").restricted
    kit.transfers.record(actor="dpo", processor_id="mail")
    assert other.transfers.records() == []


def test_repository_without_transfer_support(kit: Kit) -> None:
    class Bare:
        pass

    kit.repository = Bare()  # type: ignore[assignment]
    with pytest.raises(ConfigError):
        kit.transfers.records()
