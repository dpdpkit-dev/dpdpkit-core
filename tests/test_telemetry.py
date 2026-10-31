from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from dpdpkit import InMemoryRepository, Kit, Policy, telemetry
from dpdpkit.cli import main

URL = "https://collector.example/v1/events"
REAL_POST = telemetry._post  # conftest stubs it for every test


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(telemetry, "_post", lambda url, body, timeout: calls.append((url, json.loads(body))))
    return calls


def _wait(thread: threading.Thread | None) -> None:
    assert thread is not None
    thread.join(5)


def _kit(policy: Policy | str = "dpdp-rules-2025.v1") -> Kit:
    return Kit(repository=InMemoryRepository(), policy=policy, signing_key="k")


def test_off_by_default(sent: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DPDPKIT_TELEMETRY_URL", URL)  # an endpoint alone is not consent
    current = telemetry.status()
    assert (current.enabled, current.will_send, current.reason) == (False, False, "off (default)")
    assert current.install_id is None and not current.state_file.exists()
    assert telemetry.record("cli", command="ledger") is None
    _kit()
    assert main(["policy", "list"]) == 0
    assert sent == []


def test_enable_without_endpoint_sends_nothing(sent: list[tuple[str, dict[str, Any]]]) -> None:
    current = telemetry.enable()
    assert current.enabled and current.endpoint is None and not current.will_send
    assert json.loads(current.state_file.read_text())["install_id"] == current.install_id
    assert telemetry.record("cli", command="ledger") is None
    assert sent == []
    assert telemetry.enable().install_id == current.install_id  # re-enabling keeps the id


def test_opted_in_sends_once_per_event(sent: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    install_id = telemetry.enable().install_id
    monkeypatch.setenv("DPDPKIT_TELEMETRY_URL", URL)
    _wait(telemetry.record("kit_started", policy="dpdp-rules-2025.v1"))
    assert telemetry.record("kit_started", policy="dpdp-rules-2025.v1") is None
    _kit()  # same process: not sent again
    assert len(sent) == 1
    url, doc = sent[0]
    assert url == URL
    assert set(doc) == {"schema", "install_id", "event", "dpdpkit_core", "packages", "python", "os", "policy"}
    assert doc["install_id"] == install_id and doc["event"] == "kit_started"
    assert "dpdpkit-core" in doc["packages"]
    assert all(name == "dpdpkit" or name.startswith("dpdpkit-") for name in doc["packages"])


def test_kit_reports_shipped_pack_ids_only(
    sent: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DPDPKIT_TELEMETRY", "1")
    monkeypatch.setenv("DPDPKIT_TELEMETRY_URL", URL)
    data = Policy.load("dpdp-rules-2025.v1").as_dict()
    data["id"] = "acme-internal.v1"
    _kit(Policy(data))
    for thread in threading.enumerate():
        if thread.name == "dpdpkit-telemetry":
            thread.join(5)
    assert [doc["policy"] for _, doc in sent] == ["custom"]


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry.enable()
    monkeypatch.setenv("DPDPKIT_TELEMETRY_URL", URL)
    assert telemetry.status().will_send
    monkeypatch.setenv("DPDPKIT_TELEMETRY", "off")
    assert telemetry.status().reason == "off: DPDPKIT_TELEMETRY is set to off"
    monkeypatch.setenv("DPDPKIT_TELEMETRY", "maybe")
    assert not telemetry.status().enabled
    monkeypatch.setenv("DPDPKIT_TELEMETRY", "1")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    current = telemetry.status()
    assert (current.enabled, current.reason, current.endpoint) == (False, "off: DO_NOT_TRACK is set", None)
    monkeypatch.setenv("DO_NOT_TRACK", "0")
    assert telemetry.status().will_send


def test_env_opt_in_uses_process_id_and_https_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DPDPKIT_TELEMETRY", "true")
    current = telemetry.status()
    assert current.enabled and current.install_id == telemetry._process_id
    assert not current.state_file.exists()  # an env opt-in never writes files
    monkeypatch.setenv("DPDPKIT_TELEMETRY_URL", "http://collector.example/")
    assert telemetry.status().endpoint is None


def test_disable_deletes_install_id() -> None:
    path = telemetry.enable().state_file
    assert telemetry.disable() is True
    assert not path.exists() and not telemetry.status().enabled
    assert telemetry.disable() is False


@pytest.mark.parametrize("content", ["not json", "[]", '{"enabled": false, "install_id": "x"}', '{"enabled": true}'])
def test_unreadable_state_means_off(content: str) -> None:
    path = telemetry.config_dir() / telemetry.STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert not telemetry.status().enabled


def test_payload_refuses_unknown_events_and_fields() -> None:
    with pytest.raises(ValueError, match="unknown telemetry event"):
        telemetry.payload("principal_seen")
    with pytest.raises(ValueError, match="cannot carry"):
        telemetry.payload("cli", command="ledger", principal="u_42")
    assert telemetry.record("cli", principal="u_42") is None  # record never raises


def test_send_failure_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, body: bytes, timeout: float) -> None:
        raise OSError("network down")

    monkeypatch.setattr(telemetry, "_post", boom)
    monkeypatch.setenv("DPDPKIT_TELEMETRY", "1")
    monkeypatch.setenv("DPDPKIT_TELEMETRY_URL", URL)
    _wait(telemetry.record("cli", command="ledger"))


def test_post_sends_json() -> None:
    received: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers["Content-Type"], body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        REAL_POST(f"http://127.0.0.1:{server.server_port}/", b'{"a":1}', 5)
    finally:
        thread.join(5)
        server.server_close()
    assert received == [("application/json", b'{"a":1}')]


def test_config_dir_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DPDPKIT_CONFIG_DIR")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert telemetry.config_dir() == tmp_path / "roaming" / "dpdpkit"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert telemetry.config_dir() == tmp_path / "xdg" / "dpdpkit"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert telemetry.config_dir() == Path.home() / ".config" / "dpdpkit"


def test_cli_telemetry_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["telemetry"]) == 0
    assert "status: off (default)" in capsys.readouterr().out
    assert main(["telemetry", "enable"]) == 0
    out = capsys.readouterr().out
    assert "telemetry enabled; install id" in out and "nothing is sent" in out
    assert main(["telemetry", "show"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["event"] == "cli" and doc["install_id"] == telemetry.status().install_id
    assert main(["telemetry", "disable"]) == 0
    assert "install id deleted" in capsys.readouterr().out
    assert main(["telemetry", "disable"]) == 0
    assert "was not enabled" in capsys.readouterr().out


def test_cli_sends_command_name_only(
    sent: list[tuple[str, dict[str, Any]]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DPDPKIT_TELEMETRY", "1")
    monkeypatch.setenv("DPDPKIT_TELEMETRY_URL", URL)
    assert main(["telemetry", "status"]) == 0
    assert f"endpoint: {URL}" in capsys.readouterr().out
    assert sent == []  # managing telemetry is not itself reported
    assert main(["policy", "diff", "dpdp-rules-2025.v1", "dpdp-rules-2025.v1"]) == 0
    assert [doc["command"] for _, doc in sent] == ["policy"]
    assert "dpdp-rules" not in json.dumps(sent[0][1])
