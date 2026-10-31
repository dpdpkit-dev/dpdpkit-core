"""Opt-in anonymous usage telemetry. **Off by default.**

Nothing is sent unless you opt in, and nothing is sent to anyone until an endpoint is configured:

* ``dpdpkit telemetry enable`` stores a random install id in ``telemetry.json`` under the dpdpkit config
  directory (``DPDPKIT_CONFIG_DIR``, else ``%APPDATA%\\dpdpkit`` on Windows, else
  ``$XDG_CONFIG_HOME/dpdpkit`` or ``~/.config/dpdpkit``). ``dpdpkit telemetry disable`` deletes the file.
* ``DPDPKIT_TELEMETRY=1`` opts in for one process or container (with a per-process id unless the file
  exists); ``DPDPKIT_TELEMETRY=0`` forces it off whatever the file says. ``DO_NOT_TRACK=1`` always wins.
* ``DPDPKIT_TELEMETRY_URL`` sets the HTTPS collector. There is no default collector yet
  (:data:`DEFAULT_ENDPOINT` is ``None``), so opting in sends nothing until one is set.

When enabled, one small JSON document is posted per event type per process, from a background thread
with a short timeout; failures are ignored and never affect the caller. ``dpdpkit telemetry show`` prints
exactly what would be sent. The document holds only the fields built by :func:`payload`: schema version,
install id, event name, dpdpkit package versions, Python major.minor and OS family, plus the CLI command
name or the shipped policy pack id. It never holds principals, tenants, config, notices, paths, host
names or any other data from your application.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import uuid
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_ENDPOINT: str | None = None
"""Collector used when ``DPDPKIT_TELEMETRY_URL`` is unset. None until the project runs one."""

TIMEOUT_SECONDS = 2.0
STATE_FILE = "telemetry.json"

EVENT_FIELDS: dict[str, frozenset[str]] = {
    "cli": frozenset({"command"}),
    "kit_started": frozenset({"policy"}),
}
"""Every event and the only extra fields it may carry. Anything else is refused."""

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

_lock = threading.Lock()
_sent: set[str] = set()
_process_id = uuid.uuid4().hex


@dataclass(frozen=True)
class TelemetryStatus:
    enabled: bool
    reason: str
    endpoint: str | None
    install_id: str | None
    state_file: Path

    @property
    def will_send(self) -> bool:
        return self.enabled and self.endpoint is not None


def config_dir() -> Path:
    override = os.environ.get("DPDPKIT_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "dpdpkit"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "dpdpkit"


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("enabled") is not True or not isinstance(data.get("install_id"), str):
        return None
    return data


def _endpoint() -> str | None:
    url = os.environ.get("DPDPKIT_TELEMETRY_URL") or DEFAULT_ENDPOINT
    return url if url and url.startswith("https://") else None


def status() -> TelemetryStatus:
    """Work out whether telemetry is on, and why. Reads only the environment and the local state file."""
    path = config_dir() / STATE_FILE
    endpoint = _endpoint()
    state = _read_state(path)
    stored_id = state["install_id"] if state else None

    def result(enabled: bool, reason: str) -> TelemetryStatus:
        install_id = (stored_id or _process_id) if enabled else stored_id
        return TelemetryStatus(enabled, reason, endpoint if enabled else None, install_id, path)

    if os.environ.get("DO_NOT_TRACK", "").strip().lower() not in _FALSE:
        return result(False, "off: DO_NOT_TRACK is set")
    env = os.environ.get("DPDPKIT_TELEMETRY")
    if env is not None:
        value = env.strip().lower()
        if value in _TRUE:
            return result(True, "on: DPDPKIT_TELEMETRY is set")
        if value in _FALSE:
            return result(False, "off: DPDPKIT_TELEMETRY is set to off")
        return result(False, f"off: unrecognised DPDPKIT_TELEMETRY value {env!r}")
    if state:
        return result(True, f"on: enabled in {path}")
    return result(False, "off (default)")


def enable() -> TelemetryStatus:
    """Opt in for this user: write the state file with a new random install id (kept if one exists)."""
    path = config_dir() / STATE_FILE
    state = _read_state(path) or {"enabled": True, "install_id": uuid.uuid4().hex}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return status()


def disable() -> bool:
    """Opt out for this user by deleting the state file and its install id. Returns True if one existed."""
    path = config_dir() / STATE_FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _packages() -> dict[str, str]:
    found: dict[str, str] = {}
    for dist in distributions():
        name = (dist.metadata["Name"] or "").lower().replace("_", "-")
        if name == "dpdpkit" or name.startswith("dpdpkit-"):
            found[name] = dist.version
    return dict(sorted(found.items()))


def payload(event: str, install_id: str | None = None, **fields: str) -> dict[str, Any]:
    """The exact document sent for ``event``. Raises ValueError for unknown events or fields."""
    allowed = EVENT_FIELDS.get(event)
    if allowed is None:
        raise ValueError(f"unknown telemetry event {event!r}")
    extra = set(fields) - allowed
    if extra:
        raise ValueError(f"telemetry event {event!r} cannot carry {sorted(extra)}")
    from . import __version__

    return {
        "schema": SCHEMA_VERSION,
        "install_id": install_id or _process_id,
        "event": event,
        "dpdpkit_core": __version__,
        "packages": _packages(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "os": platform.system() or "unknown",
        **{k: str(v) for k, v in sorted(fields.items())},
    }


def _post(url: str, body: bytes, timeout: float) -> None:
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 - _endpoint() only returns https:// URLs
        url, data=body, method="POST", headers={"Content-Type": "application/json", "User-Agent": "dpdpkit"}
    )
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310
        pass


def _deliver(url: str, body: bytes) -> None:
    try:
        _post(url, body, TIMEOUT_SECONDS)
    except Exception:  # telemetry must never surface an error
        pass


def record(event: str, **fields: str) -> threading.Thread | None:
    """Send ``event`` in the background if telemetry is on and an endpoint is set; at most once per process.

    Returns the sending thread (so a short-lived CLI can wait briefly for it), or None when nothing is sent.
    Never raises.
    """
    try:
        current = status()
        if not current.will_send or current.endpoint is None:
            return None
        with _lock:
            if event in _sent:
                return None
            _sent.add(event)
        body = json.dumps(payload(event, current.install_id, **fields)).encode("utf-8")
        thread = threading.Thread(target=_deliver, args=(current.endpoint, body), name="dpdpkit-telemetry", daemon=True)
        thread.start()
        return thread
    except Exception:
        return None
