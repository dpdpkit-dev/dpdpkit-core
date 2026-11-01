from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from dpdpkit import ConfigError, InMemoryRepository, kit_from_config, sync_notice
from dpdpkit import templates as templates_module
from dpdpkit.cli import main
from dpdpkit.config import STARTER_CONFIG
from dpdpkit.review import notice_checks, notice_hashes, notice_translations_for, review_pack
from dpdpkit.templates import list_templates, load_template

TEMPLATE_IDS = [t.id for t in list_templates()]


def _statuses(config: dict[str, Any]) -> dict[str, tuple[str, str]]:
    kit, translations = notice_translations_for(config)
    return {c.id: (c.status, c.detail) for c in notice_checks(kit, translations)}


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_template_notice_passes_every_automated_check(template_id: str) -> None:
    statuses = _statuses(load_template(template_id).config())
    automated = {k: s for k, (s, _) in statuses.items() if s != "counsel"}
    # Templates ship example names and links on purpose; everything else must pass before counsel sees it.
    assert automated.pop("placeholders") == "warn"
    assert set(automated.values()) == {"pass"}, automated
    assert {"standalone", "necessity", "legal_basis", "children", "translation_fidelity"} <= set(statuses)


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_template_review_is_not_stale(template_id: str) -> None:
    """A recorded review covers only the wording it approved. If this fails, a template's notice changed after
    its review: get it reviewed again, or set it back to `status: pending` in templates/reviews.yaml."""
    review = load_template(template_id).notice_review()
    assert review.status in ("pending", "reviewed"), review.describe()


def test_review_hashes_match_the_published_notice() -> None:
    template = load_template("health")
    config = template.config()
    kit = kit_from_config(config, InMemoryRepository(), signing_key="k")
    published = sync_notice(kit, config)
    assert published is not None
    assert template.notice_hashes() == {n.locale: n.content_hash for n in published}
    assert notice_hashes(notice_translations_for(config)[1]) == template.notice_hashes()
    (entry,) = [e for e in kit.ledger.audit_entries() if e.action == "notice.published"]
    assert entry.data["locales"] == template.notice_hashes()


def test_hindi_notice_uses_translated_data_items() -> None:
    _, translations = notice_translations_for(load_template("fintech").config())
    kyc = next(p for p in translations["hi"].purposes if p.id == "kyc")
    assert "PAN (स्थायी खाता संख्या)" in kyc.data_items
    assert kyc.description.startswith("पहचान की जाँच")
    english = next(p for p in translations["en"].purposes if p.id == "kyc")
    assert "PAN" in english.data_items


def test_checks_catch_structural_problems() -> None:
    config = yaml.safe_load(STARTER_CONFIG)
    assert _statuses(config)["translation"][0] == "pass"

    broken = copy.deepcopy(config)
    del broken["notice"]["links"]["board_complaint"]
    broken["purposes"][1]["description"] = ""
    del broken["notice"]["translations"]["hi"]["data_items"]["email"]
    del broken["notice"]["translations"]["hi"]["purposes"]["marketing"]["description"]
    del broken["contact"]["email"]
    statuses = _statuses(broken)
    assert statuses["links"] == ("fail", "en: no board_complaint link; hi: no board_complaint link")
    assert statuses["itemised_purposes"][0] == "fail"
    assert "en: marketing has no description" in statuses["itemised_purposes"][1]
    assert statuses["translation"] == ("fail", "hi: data item 'Email address'")
    assert statuses["contact"] == ("fail", "contact has no email")

    untranslated = copy.deepcopy(config)
    del untranslated["notice"]["translations"]["hi"]["purposes"]["marketing"]["description"]
    assert statuses_detail(untranslated, "translation") == "hi: marketing description"

    english_only = copy.deepcopy(config)
    del english_only["notice"]["translations"]
    statuses = _statuses(english_only)
    assert statuses["languages"][0] == "warn"
    assert "translation_fidelity" not in statuses
    assert statuses["translation"] == ("pass", "only one locale")

    real = copy.deepcopy(config)
    real["contact"] = {"name": "Real Co Pvt Ltd", "email": "privacy@real.in"}
    real["notice"]["links"] = {k: f"https://real.in/{k}" for k in real["notice"]["links"]}
    assert _statuses(real)["placeholders"] == ("pass", "no example values found")


def statuses_detail(config: dict[str, Any], check: str) -> str:
    return _statuses(config)[check][1]


def test_legal_basis_question_lists_legitimate_uses() -> None:
    statuses = _statuses(load_template("fintech").config())
    assert statuses["legal_basis"] == ("counsel", "kyc: s7_d, transactions: s7_d")
    assert statuses["necessity"][1].startswith("required: account, credit_assessment")
    assert _statuses(yaml.safe_load(STARTER_CONFIG))["legal_basis"][1] == "every purpose relies on consent"


def test_review_pack_renders_everything_counsel_needs() -> None:
    template = load_template("fintech")
    pack = review_pack(
        template.config(), source="fintech", notes=template.notes(), prepared_by="Dev", today=dt.date(2027, 1, 4)
    )
    assert pack.startswith("# Notice review pack: How we use your personal and financial data\n")
    assert "Prepared: 2027-01-04 by Dev." in pack
    for digest in template.notice_hashes().values():
        assert pack.count(digest) == 2  # sign-off table and the locale heading
    assert "| Approved / approved with the changes noted / not approved |" in pack
    assert "| `placeholders` | WARN |" in pack
    assert "Automated checks: 6 passed, 0 failed, 1 to fix before go-live." in pack
    assert "> Sector law sets most retention here." in pack
    assert "**Verify your identity (KYC)** · `kyc` · no consent asked · legitimate use (s7_d)" in pack
    assert "**आपकी पहचान सत्यापित करना (KYC)**" in pack
    assert "   - _Processors (not part of the published notice):_ KYC and video-KYC provider" in pack
    assert "- withdraw: <https://example.in/privacy/preferences>" in pack
    assert "$" not in pack.replace("$$", "")  # every template placeholder was filled


def test_review_pack_flags_failed_checks() -> None:
    config = yaml.safe_load(STARTER_CONFIG)
    del config["notice"]["links"]["rights"]
    pack = review_pack(config)
    assert "Fix the failed checks before the review" in pack
    assert "_None._" in pack  # no notes given
    with pytest.raises(ConfigError, match="no notice"):
        review_pack({k: v for k, v in config.items() if k != "notice"})


def test_review_register(monkeypatch: pytest.MonkeyPatch) -> None:
    template = load_template("saas")
    assert template.notice_review().status == "pending"
    assert template.notice_review().describe() == "notice wording awaiting lawyer review"
    assert template.notes().startswith("dpdpkit purpose template: B2B SaaS")

    reviewed = {
        "status": "reviewed",
        "reviewer": "A. Advocate",
        "firm": "Example Chambers",
        "date": dt.date(2027, 1, 15),
        "hashes": template.notice_hashes(),
    }
    monkeypatch.setattr(templates_module, "_reviews", lambda: {"saas": reviewed})
    review = template.notice_review()
    assert review.status == "reviewed"
    assert review.describe() == "notice wording reviewed (A. Advocate, Example Chambers, 2027-01-15)"
    assert load_template("edtech").notice_review().status == "pending"  # no entry means pending

    stale = {**reviewed, "hashes": {**template.notice_hashes(), "hi": "0" * 64}}
    monkeypatch.setattr(templates_module, "_reviews", lambda: {"saas": stale})
    assert template.notice_review().status == "stale"
    assert "changed since its review" in template.notice_review().describe()

    monkeypatch.setattr(templates_module, "_reviews", lambda: {"saas": {"status": "approved"}})
    with pytest.raises(ConfigError, match="status 'approved'"):
        template.notice_review()
    monkeypatch.setattr(templates_module, "_reviews", lambda: {"saas": {"status": "reviewed", "reviewer": "A"}})
    with pytest.raises(ConfigError, match="needs a reviewer and the approved hashes"):
        template.notice_review()


def test_shipped_register_lists_every_template() -> None:
    assert sorted(templates_module._reviews()) == sorted(TEMPLATE_IDS)


def test_cli_notice_review_and_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "review.md"
    assert main(["notice", "review", "--template", "edtech", "--out", str(out), "--prepared-by", "Dev"]) == 0
    assert "awaiting lawyer review" in capsys.readouterr().err
    text = out.read_text(encoding="utf-8")
    assert "by Dev." in text and "Source: the `edtech` purpose template." in text
    assert main(["notice", "review", "--template", "edtech", "--out", str(out)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["notice", "review", "--template", "edtech", "--out", str(out), "--force"]) == 0
    assert main(["notice", "review", "cfg.yaml", "--template", "edtech"]) == 1
    assert "not both" in capsys.readouterr().err

    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    capsys.readouterr()
    assert main(["notice", "check"]) == 0
    printed = capsys.readouterr().out
    assert "PASS  links" in printed and "WARN  placeholders" in printed
    assert main(["notice", "check", "--strict"]) == 1
    capsys.readouterr()
    assert main(["notice", "review"]) == 0
    assert capsys.readouterr().out.startswith("# Notice review pack: How we use your personal data")

    config = yaml.safe_load(STARTER_CONFIG)
    del config["notice"]["translations"]["hi"]["data_items"]
    broken = tmp_path / "broken.yaml"
    broken.write_text("# Client X\n" + yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    assert main(["notice", "check", str(broken)]) == 1
    assert "FAIL  translation" in capsys.readouterr().out
    assert main(["notice", "review", str(broken)]) == 0
    captured = capsys.readouterr()
    assert "1 automated check(s) failed: translation" in captured.err
    assert "> Client X" in captured.out
    assert main(["notice", "review", str(broken), "--strict", "--out", str(tmp_path / "b.md")]) == 1


def test_cli_init_reports_template_review_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", "--list-templates"]) == 0
    assert capsys.readouterr().out.count("[notice wording awaiting lawyer review]") == len(TEMPLATE_IDS)
    assert main(["init", "--dir", str(tmp_path), "--template", "saas"]) == 0
    assert "note: notice wording awaiting lawyer review" in capsys.readouterr().out
