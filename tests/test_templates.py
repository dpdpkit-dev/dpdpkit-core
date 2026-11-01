from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dpdpkit import ConfigError, InMemoryRepository, Kit, LegalBasis, MemoryNotifier, kit_from_config, sync_notice
from dpdpkit.cli import main
from dpdpkit.models import RetentionTrigger
from dpdpkit.templates import list_templates, load_template

from .conftest import FakeClock

TEMPLATE_IDS = ["edtech", "health", "ecommerce", "fintech", "saas"]


def _kit(config: dict[str, Any], clock: FakeClock) -> Kit:
    kit = kit_from_config(
        config, InMemoryRepository(), notifier=MemoryNotifier(), clock=clock, signing_key="test-signing-key"
    )
    sync_notice(kit, config)
    return kit


def test_every_sector_ships_a_template() -> None:
    assert [t.id for t in list_templates()] == TEMPLATE_IDS
    with pytest.raises(ConfigError, match="unknown purpose template 'banking'"):
        load_template("banking")


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_template_is_a_complete_config(template_id: str) -> None:
    template = load_template(template_id)
    assert template.text.startswith("# dpdpkit purpose template: ")
    assert "not legal advice" in template.text
    cfg = template.config()
    declared_items = {i["id"] for i in cfg["data_items"]}
    declared_processors = {p["id"] for p in cfg["processors"]}
    used_items: set[str] = set()
    used_processors: set[str] = set()
    for purpose in cfg["purposes"]:
        # Undeclared items would be registered under their id and show up badly named in the notice.
        assert set(purpose["data_items"]) <= declared_items, purpose["id"]
        assert purpose.get("retention"), f"{purpose['id']} has no retention rule"
        used_items.update(purpose["data_items"])
        used_processors.update(purpose.get("processors", []))
    assert used_items == declared_items
    assert used_processors == declared_processors
    hindi = cfg["notice"]["translations"]["hi"]["purposes"]
    assert set(hindi) == {p["id"] for p in cfg["purposes"]}
    # An unquoted comma in a YAML flow mapping silently splits a title into extra keys.
    assert all(list(text) == ["title"] and isinstance(text["title"], str) for text in hindi.values())


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_template_runs_end_to_end(template_id: str, clock: FakeClock) -> None:
    kit = _kit(load_template(template_id).config(), clock)
    assert {n.locale for n in kit.repository.list_notices(kit.tenant_id)} == {"en", "hi"}

    consent = kit.registry.consent_purposes()
    optional = [p for p in consent if not p.required]
    assert optional, "every template offers at least one optional purpose"
    assert any(p.id == "marketing" for p in optional)
    for purpose in consent:
        kit.consent.grant("u1", purpose.id)
    for purpose in optional:
        # Optional consent ends with withdrawal and erases on it.
        assert purpose.retention is not None and purpose.retention.trigger is RetentionTrigger.WITHDRAWAL
        kit.consent.withdraw("u1", purpose.id)
        assert not kit.consent.check("u1", purpose.id)
    assert all(kit.consent.check("u1", p.id) for p in consent if p.required)

    assert kit.retention.record_activity("u1")
    for purpose in kit.registry.purposes():
        if purpose.retention and purpose.retention.trigger is RetentionTrigger.PURPOSE_COMPLETE:
            assert kit.retention.purpose_completed("u1", purpose.id) is not None
    kit.ledger.verify()


def test_legal_retention_purposes_use_a_section_7_basis(clock: FakeClock) -> None:
    kit = _kit(load_template("fintech").config(), clock)
    assert kit.registry.purpose("kyc").legal_basis is LegalBasis.S7_D_LEGAL_OBLIGATION
    assert kit.consent.check("someone", "kyc")  # no consent needed
    health = _kit(load_template("health").config(), clock)
    assert health.registry.purpose("emergency_care").legal_basis is LegalBasis.S7_F_MEDICAL_EMERGENCY
    assert all(i.sensitive for i in health.registry.data_items() if i.category == "health")


def test_ecommerce_third_schedule_alternative_is_valid(clock: FakeClock) -> None:
    template = load_template("ecommerce")
    assert "retention: {third_schedule_class: ecommerce}" in template.text
    cfg = template.config()
    account = next(p for p in cfg["purposes"] if p["id"] == "account")
    account["retention"] = yaml.safe_load("{third_schedule_class: ecommerce}")
    kit = _kit(cfg, clock)
    (schedule,) = [s for s in kit.retention.record_activity("u1") if s.purpose == "account"]
    _, period = kit.policy.third_schedule("ecommerce")
    assert schedule.erase_at - clock.now == period


def test_cli_init_from_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", "--list-templates"]) == 0
    listing = capsys.readouterr().out
    assert all(template_id in listing for template_id in TEMPLATE_IDS)

    assert main(["init", "--dir", str(tmp_path), "--template", "health"]) == 0
    assert "health purpose template" in capsys.readouterr().out
    written = (tmp_path / "dpdpkit.yaml").read_text(encoding="utf-8")
    assert written == load_template("health").text

    assert main(["init", "--dir", str(tmp_path / "new"), "--template", "banking"]) == 1
    assert "choose from: edtech, health" in capsys.readouterr().err
    assert not (tmp_path / "new").exists()
