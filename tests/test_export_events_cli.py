from __future__ import annotations

import io
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

from dpdpkit import Kit
from dpdpkit.cli import main
from dpdpkit.events import EventBus, WebhookDispatcher, WebhookEndpoint, sign_payload, verify_signature
from dpdpkit.models import Channel, DeliveryStatus, Message
from dpdpkit.notify import SmtpNotifier, render


def test_principal_export_and_html(kit: Kit) -> None:
    kit.consent.grant("u1", "marketing")
    kit.rights.open("u1", "access")
    kit.export.register_collector("orders", lambda p: {"count": 2, "note": "<script>"})
    data = kit.export.principal("u1")
    assert data["consents"][0]["purpose"] == "marketing"
    assert data["requests"][0]["kind"] == "access"
    assert data["data"]["orders"]["count"] == 2
    analytics = next(p for p in data["purposes"] if p["id"] == "analytics")
    assert analytics["shared_with"] == [{"name": "CRM vendor", "country": "IN"}]
    html = kit.export.principal_html("u1")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_evidence_pack(kit: Kit) -> None:
    kit.consent.grant("u1", "marketing")
    kit.rights.open("u1", "grievance")
    blob = kit.export.evidence_pack()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        assert {"consent_events.csv", "audit_entries.csv", "summary.pdf", "root_hash.json"} <= names
        assert zf.read("summary.pdf").startswith(b"%PDF-1.4")
        assert json.loads(zf.read("ledger_verification.json"))["ok"] is True
        assert "u1" in zf.read("consent_events.csv").decode()


def test_webhook_signature_roundtrip() -> None:
    header = sign_payload("s3cret", b'{"a":1}', timestamp=1000)
    assert verify_signature("s3cret", b'{"a":1}', header, now=1100)
    assert not verify_signature("s3cret", b'{"a":2}', header, now=1100)
    assert not verify_signature("other", b'{"a":1}', header, now=1100)
    assert not verify_signature("s3cret", b'{"a":1}', header, now=5000)
    assert not verify_signature("s3cret", b'{"a":1}', "garbage")


def test_dispatcher_retries_then_dead_letters() -> None:
    calls: list[str] = []

    def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        calls.append(url)
        assert verify_signature("k", body, headers["X-DPDPKit-Signature"])
        return 500

    bus = EventBus()
    dispatcher = WebhookDispatcher(
        [WebhookEndpoint(id="w1", url="https://hooks.example.in", secret="k", events=["consent.withdrawn"])],
        sender=sender,
        retries=3,
        sleep=lambda s: None,
    )
    dispatcher.attach(bus)
    bus.publish("consent.granted", "default", {})
    assert calls == []
    bus.publish("consent.withdrawn", "default", {"principal": "u1"})
    assert len(calls) == 3 and len(dispatcher.dead_letters) == 1


def test_event_handler_failure_does_not_break_flow(kit: Kit) -> None:
    kit.events.subscribe("consent.granted", lambda e: 1 / 0)
    kit.consent.grant("u1", "marketing")
    assert kit.consent.check("u1", "marketing")
    assert len(kit.events.failures) == 1


def test_templates_and_smtp_refuses_sms() -> None:
    subject, body = render("erasure_warning", "hi", {"fiduciary": "Acme", "purpose": "Ads"})
    assert "Acme" in subject and "{erase_at}" in body
    receipt = SmtpNotifier("localhost", sender="a@b").send(
        Message(channel=Channel.SMS, to="+919800000000", subject="s", body="b", template="t")
    )
    assert receipt.status is DeliveryStatus.FAILED and receipt.to_masked.endswith("0000")


def test_cli_init_policy_and_kit_commands(
    tmp_path: Path, kit: Kit, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["init", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "dpdpkit.yaml").exists()
    assert main(["init", "--dir", str(tmp_path)]) == 1
    assert main(["policy", "list"]) == 0
    assert main(["policy", "show"]) == 0
    assert "erasure_pre_notice_hours: 48" in capsys.readouterr().out

    module = types.ModuleType("fake_app")
    module.kit = kit  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_app", module)
    kit.consent.grant("u1", "marketing")
    kit.retention.record_activity("u1")
    assert main(["ledger", "verify", "--kit", "fake_app:kit"]) == 0
    assert main(["retention", "preview", "--kit", "fake_app:kit", "--hours", "800"]) == 0
    assert "analytics" in capsys.readouterr().out
    out = tmp_path / "u1.html"
    assert main(["export", "--principal", "u1", "--format", "html", "--out", str(out), "--kit", "fake_app:kit"]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    kit.repository.consent_events[0] = kit.repository.consent_events[0].model_copy(update={"source": "x"})
    assert main(["ledger", "verify", "--kit", "fake_app:kit"]) == 2
