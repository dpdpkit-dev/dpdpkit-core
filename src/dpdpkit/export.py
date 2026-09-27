"""Principal data exports (JSON + readable HTML) and audit evidence packs (CSV + PDF in a zip)."""

from __future__ import annotations

import csv
import html
import io
import json
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .models import ACTIVE_SCHEDULE_STATUSES

if TYPE_CHECKING:
    from .kit import Kit

Collector = Callable[[str], Mapping[str, Any]]
"""``collector(principal)`` returns the app's own data about the principal, for the access export."""


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class ExportService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit
        self._collectors: dict[str, Collector] = {}

    def register_collector(self, name: str, collector: Collector) -> Collector:
        self._collectors[name] = collector
        return collector

    # ------------------------------------------------------------------ principal export

    def principal(self, principal: str) -> dict[str, Any]:
        """Summary of personal data and processing for one principal (DPDP Act s.11)."""
        kit = self._kit
        purposes = []
        for p in kit.registry.purposes():
            purposes.append(
                {
                    "id": p.id,
                    "title": p.title,
                    "legal_basis": p.legal_basis.value,
                    "data_items": [kit.registry.data_item(i).name for i in p.data_items],
                    "shared_with": [
                        {"name": proc.name, "country": proc.country} for proc in kit.registry.processors_for(p.id)
                    ],
                }
            )
        app_data: dict[str, Any] = {}
        for name, collector in self._collectors.items():
            try:
                app_data[name] = dict(collector(principal))
            except Exception as exc:
                app_data[name] = {"error": f"collector failed: {exc!r}"}
        result: dict[str, Any] = json.loads(
            json.dumps(
                {
                    "principal": principal,
                    "generated_at": kit.now(),
                    "fiduciary": kit.contact,
                    "consents": kit.consent.state(principal),
                    "consent_history": kit.consent.history(principal),
                    "requests": kit.rights.list_requests(principal=principal),
                    "nominees": kit.rights.nominees(principal),
                    "purposes": purposes,
                    "erasure_schedules": kit.retention.schedules(principal),
                    "data": app_data,
                },
                default=_json_default,
            )
        )
        return result

    def principal_html(self, principal: str) -> str:
        data = self.principal(principal)
        e = html.escape

        def table(rows: Sequence[Mapping[str, Any]], cols: Sequence[str]) -> str:
            if not rows:
                return "<p>None.</p>"
            head = "".join(f"<th>{e(c)}</th>" for c in cols)
            body = "".join("<tr>" + "".join(f"<td>{e(_cell(r.get(c)))}</td>" for c in cols) + "</tr>" for r in rows)
            return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

        sections = [
            ("Your consents", table(data["consents"], ["purpose", "status", "notice_version", "updated_at"])),
            ("What we hold and why", table(data["purposes"], ["title", "legal_basis", "data_items", "shared_with"])),
            ("Your requests", table(data["requests"], ["id", "kind", "status", "opened_at", "due_at"])),
            ("Your nominees", table(data["nominees"], ["name", "relationship", "created_at"])),
            ("Consent history", table(data["consent_history"], ["occurred_at", "purpose", "action", "notice_version"])),
        ]
        for name, value in data["data"].items():
            sections.append((f"Data: {name}", f"<pre>{e(json.dumps(value, indent=2, ensure_ascii=False))}</pre>"))
        body = "".join(f"<h2>{e(title)}</h2>{content}" for title, content in sections)
        contact = data["fiduciary"]
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<title>Your personal data summary</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}"
            "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:.4rem;text-align:left;"
            "vertical-align:top}th{background:#f4f4f4}</style></head><body>"
            f"<h1>Your personal data summary</h1><p>Generated {e(str(data['generated_at']))} "
            f"by {e(str(contact.get('name', 'the data fiduciary')))}. Contact: {e(str(contact.get('email', '')))}</p>"
            f"{body}</body></html>"
        )

    # ------------------------------------------------------------------ evidence pack

    def evidence_pack(self) -> bytes:
        """Zip with CSVs of the ledger, audit trail, requests and schedules, plus a summary (HTML + PDF)."""
        kit = self._kit
        verification = kit.ledger.verify_report()
        root = kit.ledger.root_hash()
        requests = kit.rights.list_requests()
        schedules = kit.retention.schedules(active_only=False)
        consent_rows = [e.model_dump(mode="json") for e in kit.ledger.consent_events()]
        audit_rows = [a.model_dump(mode="json") for a in kit.ledger.audit_entries()]
        overdue = kit.rights.overdue()
        summary = [
            f"dpdpkit evidence pack - tenant {kit.tenant_id}",
            f"Generated: {kit.now().isoformat()}",
            f"Policy pack: {kit.policy.id} overlays={kit.policy.overlays}",
            "",
            f"Ledger verification: {'OK' if verification.ok else 'FAILED'} ({verification.checked} rows checked)",
            f"Failed row: {verification.failed_row} ({verification.reason})" if not verification.ok else "",
            f"Root hash: {root.root}",
            f"Consent events: {len(consent_rows)}   Audit entries: {len(audit_rows)}",
            "",
            f"Rights requests: {len(requests)} total, {sum(1 for r in requests if r.closed_at is None)} open, "
            f"{len(overdue)} overdue",
            f"Erasure schedules: {len(schedules)} total, "
            f"{sum(1 for s in schedules if s.status in ACTIVE_SCHEDULE_STATUSES)} active, "
            f"{len(kit.retention.needs_attention())} need attention",
            "",
            "This pack records what dpdpkit did. It is not a statement of legal compliance.",
        ]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("consent_events.csv", _csv(consent_rows))
            zf.writestr("audit_entries.csv", _csv(audit_rows))
            zf.writestr("rights_requests.csv", _csv([r.model_dump(mode="json") for r in requests]))
            zf.writestr("erasure_schedules.csv", _csv([s.model_dump(mode="json") for s in schedules]))
            zf.writestr("ledger_verification.json", verification.model_dump_json(indent=2))
            zf.writestr("root_hash.json", root.model_dump_json(indent=2))
            zf.writestr("policy.json", json.dumps(kit.policy.as_dict(), indent=2))
            zf.writestr(
                "summary.html",
                "<!doctype html><meta charset='utf-8'><title>Evidence pack</title><pre>"
                + html.escape("\n".join(summary))
                + "</pre>",
            )
            zf.writestr("summary.pdf", simple_pdf("dpdpkit evidence pack", summary))
        return buf.getvalue()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_cell(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {_cell(v)}" for k, v in value.items())
    return str(value)


def _csv(rows: Iterable[Mapping[str, Any]]) -> str:
    rows = list(rows)
    out = io.StringIO()
    if not rows:
        return ""
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()})
    return out.getvalue()


def simple_pdf(title: str, lines: Sequence[str], lines_per_page: int = 48) -> bytes:
    """Minimal text-only PDF (Helvetica, A4). Enough for a readable summary without a dependency."""

    def esc(text: str) -> str:
        safe = text.encode("latin-1", "replace").decode("latin-1")
        return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    all_lines = [title, "", *lines]
    pages = [all_lines[i : i + lines_per_page] for i in range(0, len(all_lines), lines_per_page)] or [[]]
    objects: list[bytes] = []
    n_pages = len(pages)
    page_ids = [4 + 2 * i for i in range(n_pages)]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, page in enumerate(pages):
        text = ["BT", "/F1 10 Tf", "14 TL", "50 790 Td"]
        for line in page:
            text.append(f"({esc(line)}) Tj T*")
        text.append("ET")
        stream = "\n".join(text).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_ids[i] + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()
