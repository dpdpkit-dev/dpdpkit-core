"""The ``dpdpkit`` command.

Commands that need data (``ledger``, ``retention``, ``export``) load your kit with
``--kit module:attribute`` or the ``DPDPKIT_KIT`` environment variable. The attribute may be a
:class:`~dpdpkit.Kit`, an object with a ``.kit`` attribute (such as an adapter), or a zero-argument
callable returning either.

Adapters add subcommands through the ``dpdpkit.cli`` entry-point group: each entry point is a
callable receiving the argparse sub-parsers object.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import sys
from collections.abc import Sequence
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import yaml

from .config import STARTER_CONFIG
from .errors import DpdpkitError, LedgerTampered
from .kit import Kit
from .policy import Policy, available_packs


def load_kit(spec: str | None) -> Kit:
    spec = spec or os.environ.get("DPDPKIT_KIT")
    if not spec or ":" not in spec:
        raise SystemExit("error: pass --kit module:attribute (or set DPDPKIT_KIT) to load your kit")
    module_name, _, attr = spec.partition(":")
    sys.path.insert(0, os.getcwd())
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if callable(obj) and not isinstance(obj, Kit) and not hasattr(obj, "kit"):
        obj = obj()
    if hasattr(obj, "kit") and not isinstance(obj, Kit):
        obj = obj.kit
    if not isinstance(obj, Kit):
        raise SystemExit(f"error: {spec} is not a dpdpkit Kit")
    return obj


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir) / "dpdpkit.yaml"
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STARTER_CONFIG, encoding="utf-8")
    print(f"wrote {target}")
    print("next: edit purposes and notice text, then wire a repository (see your adapter's quickstart)")
    return 0


def _policy_from(ref: str, overlays: Sequence[str]) -> Policy:
    return Policy.from_file(ref, overlays) if Path(ref).is_file() else Policy.load(ref, overlays)


def _cmd_policy(args: argparse.Namespace) -> int:
    if args.policy_command == "list":
        for pack_id in available_packs():
            print(pack_id)
        return 0
    if args.policy_command == "show":
        pack = _policy_from(args.pack or available_packs()[-1], args.overlay)
        print(yaml.safe_dump(pack.as_dict(), sort_keys=False, allow_unicode=True), end="")
        return 0
    old = _policy_from(args.old, [])
    new = _policy_from(args.new, [])
    changes = old.diff(new)
    if not changes:
        print(f"no differences between {old.id} and {new.id}")
        return 0
    print(f"{old.id} -> {new.id}")
    for change in changes:
        print(f"  {change.key}: {json.dumps(change.old)} -> {json.dumps(change.new)}")
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    kit = load_kit(args.kit)
    if args.ledger_command == "root":
        print(kit.ledger.root_hash().model_dump_json(indent=2))
        return 0
    try:
        result = kit.ledger.verify()
    except LedgerTampered as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    print(
        f"ok: {result.checked} rows verified; consent head {result.consent_head[:16]}…, "
        f"audit head {result.audit_head[:16]}…"
    )
    return 0


def _cmd_retention(args: argparse.Namespace) -> int:
    kit = load_kit(args.kit)
    if args.retention_command == "run":
        run = kit.retention.run()
        print(run.model_dump_json(indent=2))
        return 0
    items = kit.retention.preview(horizon=dt.timedelta(hours=args.hours))
    if not items:
        print(f"nothing due in the next {args.hours} hours")
    for item in items:
        print(f"{item.at.isoformat()}  {item.action:<5}  {item.principal}  {item.purpose}  ({item.status.value})")
    attention = kit.retention.needs_attention()
    if attention:
        print(f"\n{len(attention)} schedule(s) need attention:")
        for s in attention:
            print(f"  {s.id}  {s.principal}  {s.purpose}: {s.attention_reason}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    kit = load_kit(args.kit)
    if args.evidence:
        data = kit.export.evidence_pack()
        out = Path(args.out or "dpdpkit-evidence.zip")
        out.write_bytes(data)
        print(f"wrote {out}")
        return 0
    if not args.principal:
        print("error: --principal is required (or use --evidence)", file=sys.stderr)
        return 1
    text = (
        kit.export.principal_html(args.principal)
        if args.format == "html"
        else json.dumps(kit.export.principal(args.principal), indent=2, ensure_ascii=False)
    )
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dpdpkit", description="dpdpkit command line")
    parser.add_argument("--version", action="version", version=f"dpdpkit-core {_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter dpdpkit.yaml")
    p.add_argument("--dir", default=".")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("policy", help="show, list or diff policy packs")
    psub = p.add_subparsers(dest="policy_command", required=True)
    psub.add_parser("list")
    show = psub.add_parser("show")
    show.add_argument("--pack", help="pack id or file (default: newest shipped pack)")
    show.add_argument("--overlay", action="append", default=[])
    diff = psub.add_parser("diff")
    diff.add_argument("old")
    diff.add_argument("new")
    p.set_defaults(func=_cmd_policy)

    p = sub.add_parser("ledger", help="verify the hash chain or print the root hash")
    p.add_argument("ledger_command", choices=["verify", "root"])
    p.add_argument("--kit")
    p.set_defaults(func=_cmd_ledger)

    p = sub.add_parser("retention", help="preview or run erasure schedules")
    p.add_argument("retention_command", choices=["preview", "run"])
    p.add_argument("--hours", type=int, default=168)
    p.add_argument("--kit")
    p.set_defaults(func=_cmd_retention)

    p = sub.add_parser("export", help="export a principal's data, or an evidence pack")
    p.add_argument("--principal")
    p.add_argument("--evidence", action="store_true")
    p.add_argument("--format", choices=["json", "html"], default="json")
    p.add_argument("--out")
    p.add_argument("--kit")
    p.set_defaults(func=_cmd_export)

    for ep in entry_points(group="dpdpkit.cli"):
        try:
            ep.load()(sub)
        except Exception as exc:  # a broken plugin must not break the core CLI
            print(f"warning: dpdpkit CLI plugin {ep.name} failed to load: {exc}", file=sys.stderr)
    return parser


def _version() -> str:
    from . import __version__

    return __version__


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DpdpkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
