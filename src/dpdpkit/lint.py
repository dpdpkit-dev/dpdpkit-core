"""``dpdpkit lint``: static checks of a ``dpdpkit.yaml`` for CI, with no database or network.

It fails when the notice or a retention rule is missing, and runs the automated notice checks from
:mod:`dpdpkit.review`. It checks that the configuration is complete, not that it is lawful.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_config
from .errors import DpdpkitError
from .review import notice_checks, notice_translations_for

FAIL, WARN, PASS = "fail", "warn", "pass"


@dataclass(frozen=True)
class Finding:
    id: str
    status: str
    detail: str


def lint_config(config: Mapping[str, Any]) -> list[Finding]:
    """Findings for one parsed configuration: notice present, retention on every purpose, notice checks."""
    findings: list[Finding] = []
    purposes = [p for p in config.get("purposes") or [] if isinstance(p, Mapping)]
    if not purposes:
        findings.append(Finding("purposes", FAIL, "no purposes are configured"))

    missing = []
    for p in purposes:
        rule = p.get("retention")
        if not isinstance(rule, Mapping) or (rule.get("period_days") is None and not rule.get("third_schedule_class")):
            missing.append(str(p.get("id", "?")))
    if missing:
        detail = f"no retention rule (period_days or third_schedule_class) for: {', '.join(missing)}"
        findings.append(Finding("retention", FAIL, detail))
    elif purposes:
        findings.append(Finding("retention", PASS, "every purpose has a retention rule"))

    if not config.get("notice"):
        findings.append(Finding("notice", FAIL, "no notice is configured"))
        return findings
    try:
        kit, translations = notice_translations_for(config)
        for c in notice_checks(kit, translations):
            if c.status != "counsel":
                findings.append(Finding(f"notice:{c.id}", c.status, c.detail))
    except DpdpkitError as exc:
        findings.append(Finding("notice", FAIL, str(exc)))
    return findings


def run_lint(paths: Sequence[str], *, strict: bool = False, fmt: str = "text") -> int:
    """Lint each config file; print findings; return 1 on any failure (or warning when ``strict``)."""
    bad = False
    for path in paths:
        try:
            findings = lint_config(load_config(path))
        except DpdpkitError as exc:
            findings = [Finding("config", FAIL, str(exc))]
        except Exception as exc:  # malformed YAML or a value the models reject
            findings = [Finding("config", FAIL, f"cannot lint {path}: {exc}")]
        for f in findings:
            if f.status == PASS and fmt == "github":
                continue
            if fmt == "github" and f.status in (FAIL, WARN):
                level = "error" if f.status == FAIL else "warning"
                message = f.detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                print(f"::{level} file={Path(path).as_posix()},title=dpdp-lint {f.id}::{message}")
            else:
                print(f"{f.status.upper():<5} {f.id:<22} {f.detail}  [{path}]")
            bad = bad or f.status == FAIL or (strict and f.status == WARN)
    if bad:
        print("dpdp-lint: failed", file=sys.stderr)
    return 1 if bad else 0


def main(argv: Sequence[str] | None = None) -> int:
    """The ``dpdp-lint`` command: shorthand for ``dpdpkit lint``."""
    from .cli import main as cli_main

    return cli_main(["lint", *(sys.argv[1:] if argv is None else argv)])
