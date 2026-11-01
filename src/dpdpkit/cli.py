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

from . import telemetry
from .config import STARTER_CONFIG
from .errors import DpdpkitError, LedgerTampered
from .kit import Kit
from .ledger import FileRootSink
from .policy import Policy, available_packs
from .templates import list_templates, load_template


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
    if args.list_templates:
        for template in list_templates():
            print(f"{template.id:<10} {template.title}: {template.summary}")
        return 0
    text = load_template(args.template).text if args.template else STARTER_CONFIG
    target = Path(args.dir) / "dpdpkit.yaml"
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target}" + (f" from the {args.template} purpose template" if args.template else ""))
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
        if args.out:
            root = kit.ledger.export_root(FileRootSink(args.out))
            print(f"appended root {root.root[:16]}… to {args.out}")
        else:
            print(kit.ledger.root_hash().model_dump_json(indent=2))
        return 0
    if args.ledger_command == "check-root":
        if not args.anchors:
            print("error: --anchors FILE is required (a file written by `dpdpkit ledger root --out`)", file=sys.stderr)
            return 1
        anchor = FileRootSink(args.anchors).latest(kit.tenant_id)
        if anchor is None:
            print(f"error: {args.anchors} has no root hash for tenant {kit.tenant_id}", file=sys.stderr)
            return 1
        try:
            kit.ledger.check_root(anchor)
        except LedgerTampered as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 2
        print(
            f"ok: chains still contain the rows covered by the root exported {anchor.exported_at.isoformat()} "
            f"(consent seq {anchor.consent_seq}, audit seq {anchor.audit_seq})"
        )
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


def _cmd_telemetry(args: argparse.Namespace) -> int:
    if args.telemetry_command == "enable":
        current = telemetry.enable()
        print(f"telemetry enabled; install id {current.install_id} stored in {current.state_file}")
    elif args.telemetry_command == "disable":
        removed = telemetry.disable()
        current = telemetry.status()
        print("telemetry disabled; install id deleted" if removed else "telemetry was not enabled for this user")
    else:
        current = telemetry.status()
    if args.telemetry_command == "show":
        print("# the document sent when `dpdpkit ledger` runs; kit start-up sends `policy` instead", file=sys.stderr)
        print(json.dumps(telemetry.payload("cli", current.install_id, command="ledger"), indent=2))
        return 0
    print(f"status: {current.reason}")
    if current.enabled and current.endpoint is None:
        print("endpoint: none (set DPDPKIT_TELEMETRY_URL to an https:// collector); nothing is sent")
    elif current.endpoint:
        print(f"endpoint: {current.endpoint}")
    print("run `dpdpkit telemetry show` to see exactly what would be sent")
    return 0


def _cmd_workbook(args: argparse.Namespace) -> int:
    from .config import load_config
    from .workbook import _openpyxl, new_workbook, read_workbook, scan_report

    try:
        _openpyxl()
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    new = args.workbook_command == "new"
    if new and args.template and args.from_config:
        print("error: pass --template or --from, not both", file=sys.stderr)
        return 1
    out = Path(args.out) if args.out else Path("data-mapping.xlsx") if new else None
    if out is not None and out.exists() and not args.force:
        print(f"{out} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    if new:
        config = None
        if args.template:
            config = load_template(args.template).config()
        elif args.from_config:
            config = load_config(args.from_config)
        engagement = {"template": args.template} if args.template else None
        workbook = new_workbook(config, engagement=engagement)
        Path(args.out or "data-mapping.xlsx").write_bytes(workbook)
        print(f"wrote {args.out or 'data-mapping.xlsx'}")
        print("next: fill it in at the data-mapping workshop, then `dpdpkit workbook import` and `report`")
        return 0
    if not args.workbook:
        print(f"error: {args.workbook_command} needs the workbook file", file=sys.stderr)
        return 1
    book = read_workbook(args.workbook)
    if args.workbook_command == "report":
        text = scan_report(book)
    else:
        config = book.to_config(load_config(args.base) if args.base else None)
        header = (
            f"# Generated by `dpdpkit workbook import` from {Path(args.workbook).name}.\n"
            "# Purposes, legal bases, retention periods and notice wording are decisions for you and your counsel.\n"
        )
        text = header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=120)
        if book.unconfirmed_purposes:
            print(f"note: counsel has not confirmed {', '.join(book.unconfirmed_purposes)}", file=sys.stderr)
    if out is None:
        print(text, end="")
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dpdpkit", description="dpdpkit command line")
    parser.add_argument("--version", action="version", version=f"dpdpkit-core {_version()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter dpdpkit.yaml")
    p.add_argument("--dir", default=".")
    p.add_argument("--force", action="store_true")
    p.add_argument("--template", help="start from an industry purpose template (see --list-templates)")
    p.add_argument("--list-templates", action="store_true", help="list the industry purpose templates")
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

    p = sub.add_parser("ledger", help="verify the hash chain, export the root hash or check against one")
    p.add_argument("ledger_command", choices=["verify", "root", "check-root"])
    p.add_argument("--out", help="root: append the root hash to this JSON-lines file instead of printing it")
    p.add_argument("--anchors", help="check-root: JSON-lines file of exported root hashes")
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

    p = sub.add_parser("telemetry", help="opt in to or out of anonymous usage telemetry (off by default)")
    p.add_argument("telemetry_command", choices=["status", "enable", "disable", "show"], nargs="?", default="status")
    p.set_defaults(func=_cmd_telemetry)

    p = sub.add_parser("workbook", help="data-mapping workbook: new, import to dpdpkit.yaml, scan report")
    p.add_argument("workbook_command", choices=["new", "import", "report"])
    p.add_argument("workbook", nargs="?", help="import/report: the filled .xlsx workbook")
    p.add_argument("--template", help="new: pre-fill from an industry purpose template")
    p.add_argument("--from", dest="from_config", metavar="CONFIG", help="new: pre-fill from a dpdpkit.yaml")
    p.add_argument("--base", metavar="CONFIG", help="import: keep the notice and other keys from this dpdpkit.yaml")
    p.add_argument("--out", help="output file (new: data-mapping.xlsx; import/report: print when omitted)")
    p.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    p.set_defaults(func=_cmd_workbook)

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
    ping = None if args.command == "telemetry" else telemetry.record("cli", command=args.command)
    try:
        return int(args.func(args))
    except DpdpkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if ping is not None:
            ping.join(telemetry.TIMEOUT_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
