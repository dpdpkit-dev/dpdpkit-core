"""Data-mapping workbook and scan report for the readiness sprint (``pip install "dpdpkit-core[workbook]"``).

The workbook is an ``.xlsx`` spreadsheet filled in during the data-mapping workshop with the client's tech lead
and whoever owns compliance. One sheet per registry table (data items, data stores, processors, purposes), plus
the PII scan findings and a gap list of readiness checks mapped to dpdpkit features.

* :func:`new_workbook` writes a blank workbook, or one pre-filled from a purpose template or ``dpdpkit.yaml``.
* :func:`read_workbook` reads a filled workbook back and checks every row, listing all problems at once.
* :meth:`MappingWorkbook.to_config` turns it into ``dpdpkit.yaml`` keys that :func:`~dpdpkit.kit_from_config` accepts.
* :func:`scan_report` renders the client-facing scan report (Markdown) from the findings and gap list.

The workbook and report hold types, locations and counts only. Sampled values are never written to either.
"""

from __future__ import annotations

import datetime as dt
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any

from ..errors import ConfigError
from ..models import LegalBasis, RetentionTrigger
from ..registry import Registry

if TYPE_CHECKING:
    from openpyxl.worksheet.worksheet import Worksheet

__all__ = [
    "LEGAL_BASIS_LABELS",
    "READINESS_CHECKS",
    "Gap",
    "MappingWorkbook",
    "ScanFinding",
    "new_workbook",
    "read_workbook",
    "scan_report",
]

LEGAL_BASIS_LABELS: dict[LegalBasis, str] = {
    LegalBasis.CONSENT: "consent",
    LegalBasis.S7_A_VOLUNTARY: "s7_a: provided voluntarily for a specified purpose",
    LegalBasis.S7_B_STATE_BENEFIT: "s7_b: State subsidy, benefit, service, licence or permit",
    LegalBasis.S7_C_STATE_FUNCTION: "s7_c: sovereignty, integrity or security of the State",
    LegalBasis.S7_D_LEGAL_OBLIGATION: "s7_d: obligation under law",
    LegalBasis.S7_E_COURT_ORDER: "s7_e: judgment, decree or court order",
    LegalBasis.S7_F_MEDICAL_EMERGENCY: "s7_f: medical emergency",
    LegalBasis.S7_G_PUBLIC_HEALTH: "s7_g: epidemic or threat to public health",
    LegalBasis.S7_H_DISASTER: "s7_h: disaster or breakdown of public order",
    LegalBasis.S7_I_EMPLOYMENT: "s7_i: employment",
}
"""Dropdown labels for the Purposes sheet. Import reads the part before the colon."""

CONFIDENCE = ("high", "medium", "low")
ACTIONS = ("keep", "delete", "minimise", "false positive", "investigate")
GAP_STATUSES = ("met", "partly met", "gap", "not applicable", "not checked")
RISKS = ("high", "medium", "low")
STORE_KINDS = ("database", "object storage", "file share", "spreadsheet", "SaaS tool", "logs", "backups", "other")

# (area, check, source, dpdpkit feature). Sources follow dpdpkit-policies' rule_map.yaml.
READINESS_CHECKS: tuple[tuple[str, str, str, str], ...] = (
    (
        "Notice",
        "The notice lists each data item and purpose, and links to withdrawing consent, rights and Board complaints",
        "DPDP Act s.5; DPDP Rules 2025, Rule 3",
        "dpdpkit.notices, <ConsentNotice>",
    ),
    (
        "Notice",
        "The notice is offered in English and in the Eighth Schedule languages your users read",
        "DPDP Act s.5(3)",
        "notice translations, <LanguageSwitcher>",
    ),
    (
        "Consent",
        "Consent is asked per purpose, nothing is pre-ticked, and the service does not depend on optional purposes",
        "DPDP Act s.6(1)",
        "dpdpkit.consent, <ConsentNotice>, require_consent()",
    ),
    (
        "Consent",
        "Withdrawing consent is as easy as giving it",
        "DPDP Act s.6(4); DPDP Rules 2025, Rule 3(c)(i)",
        "<PreferenceCenter>, POST /consents/{purpose}/withdraw",
    ),
    (
        "Consent",
        "Every grant and withdrawal can be proved later",
        "DPDP Act s.6(10)",
        "consent ledger, signed receipts, dpdpkit ledger verify",
    ),
    (
        "Rights",
        "Principals can ask for access, correction, completion, updating and erasure, and can nominate someone",
        "DPDP Act ss.11-14; DPDP Rules 2025, Rule 14",
        "dpdpkit.rights, <RightsRequestForm>",
    ),
    (
        "Rights",
        "Grievances are answered within the published period, at most 90 days",
        "DPDP Act s.13; DPDP Rules 2025, Rule 14(3)",
        "rights SLA clocks from the policy pack",
    ),
    (
        "Retention",
        "Every purpose has a retention period, and data is erased when it ends",
        "DPDP Act s.8(7); DPDP Rules 2025, Rule 8",
        "dpdpkit.retention, erasure handlers",
    ),
    (
        "Retention",
        "Principals are told at least 48 hours before erasure",
        "DPDP Rules 2025, Rule 8(2)",
        "retention pre-erasure warnings, dpdpkit.notify",
    ),
    (
        "Retention",
        "Personal data, traffic data and processing logs are kept for at least one year",
        "DPDP Rules 2025, Rule 6(1)(e) and Rule 8(3)",
        "ledger and audit trail, retention log floor",
    ),
    (
        "Processors",
        "Every processor is under contract, and erasure requests reach them",
        "DPDP Act s.8(2), s.8(7)",
        "processor register, erasure fan-out webhooks",
    ),
    (
        "Children",
        "Users under 18 are identified and verifiable parental consent is collected",
        "DPDP Act s.9; DPDP Rules 2025, Rule 10",
        "dpdpkit.guardians (planned, v0.5)",
    ),
    (
        "Breach",
        "Breaches are reported to the Board and affected principals, with a detailed report within 72 hours",
        "DPDP Act s.8(6); DPDP Rules 2025, Rule 7",
        "dpdpkit.incidents (planned, v0.5)",
    ),
    (
        "Security",
        "Reasonable security safeguards: encryption, access control, monitoring and backups",
        "DPDP Act s.8(5); DPDP Rules 2025, Rule 6",
        "outside dpdpkit: the client's infrastructure",
    ),
    (
        "Contact",
        "A contact for privacy questions is published",
        "DPDP Act s.8(9); DPDP Rules 2025, Rule 9",
        "contact in dpdpkit.yaml, shown in the notice",
    ),
)

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0", ""}


# --------------------------------------------------------------------------- sheet layout


@dataclass(frozen=True)
class _Column:
    header: str
    key: str
    width: int = 18
    help: str = ""
    choices: str | None = None
    """Name of a dropdown list on the Lists sheet, or ``yesno``."""


@dataclass(frozen=True)
class _Sheet:
    title: str
    intro: str
    columns: tuple[_Column, ...]


_ENGAGEMENT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("client", "Client (legal name)", "Shown in the notice as the Data Fiduciary. Imported as contact.name."),
    ("email", "Privacy contact email", "Where principals send privacy questions. Imported as contact.email."),
    ("rights_url", "Rights page URL", "Page where principals make requests. Imported as contact.rights_url."),
    ("policy", "Policy pack", "Imported as policy. Leave as is unless counsel picked another pack."),
    ("overlays", "Overlays", "Comma-separated sector overlays, for example cert-in-2022. Imported as overlays."),
    ("template", "Purpose template", "Industry template the workshop started from, if any."),
    ("workshop_date", "Workshop date", ""),
    ("tech_lead", "Client tech lead", ""),
    ("compliance_owner", "Compliance owner", "Whoever owns compliance on the client side."),
    ("prepared_by", "Prepared by", "The implementer running the readiness sprint."),
)

_DATA_ITEMS = _Sheet(
    "Data items",
    "One row per kind of personal data. IDs are lowercase, for example email or pan_number.",
    (
        _Column("ID", "id", 18, "Lowercase letters, digits, _ or -. Purposes refer to this ID."),
        _Column("Name", "name", 32, "Plain-language name shown to principals in the notice."),
        _Column("Description", "description", 40),
        _Column("Category", "category", 16, "Free text, for example contact, identity, financial, health."),
        _Column(
            "Sensitive", "sensitive", 11, "yes for health, financial, biometric, children's data and the like.", "yesno"
        ),
        _Column("Collected from", "source", 22, "The principal, a third party, or generated by the system."),
        _Column("Where it lives", "locations", 32, "Data store IDs or table.column names. Not imported."),
        _Column("Notes", "notes", 40),
    ),
)

_DATA_STORES = _Sheet(
    "Data stores",
    "Every place personal data is kept: databases, buckets, spreadsheets, SaaS tools, logs and backups.",
    (
        _Column("ID", "id", 18, "Lowercase ID. Purposes and scan findings refer to it."),
        _Column("Name", "name", 32),
        _Column("Kind", "kind", 16, "", "store_kinds"),
        _Column("Region", "region", 10, "Country code where the data is held, for example IN."),
        _Column("Owner", "owner", 20, "Team or person responsible. Not imported."),
        _Column("Scanned", "scanned", 10, "Included in the PII scan. Not imported.", "yesno"),
        _Column("Notes", "notes", 40),
    ),
)

_PROCESSORS = _Sheet(
    "Processors",
    "Every outside party that processes personal data for the client: payment, email, hosting, analytics, support.",
    (
        _Column("ID", "id", 18, "Lowercase ID. Purposes refer to it."),
        _Column("Name", "name", 32),
        _Column("Country", "country", 10, "Country code where the processor handles the data."),
        _Column("Contact", "contact", 26, "Privacy or DPO contact at the processor."),
        _Column("Erasure webhook", "erasure_webhook", 30, "URL dpdpkit calls to pass erasure on, if any."),
        _Column("Contract in place", "contract", 12, "Not imported; feeds the gap list.", "yesno"),
        _Column("Notes", "notes", 40),
    ),
)

_PURPOSES = _Sheet(
    "Purposes",
    "One row per purpose. Lists are comma-separated IDs from the other sheets. Counsel confirms every row.",
    (
        _Column("ID", "id", 18, "Lowercase ID. Used in consent records and require_consent('id')."),
        _Column("Title", "title", 32, "Shown to principals in the notice."),
        _Column("Description", "description", 40),
        _Column("Data items", "data_items", 30, "Comma-separated data item IDs."),
        _Column("Legal basis", "legal_basis", 26, "consent, or a DPDP Act s.7 legitimate use.", "legal_bases"),
        _Column("Required", "required", 10, "yes if the service cannot run without it. Never pre-ticked.", "yesno"),
        _Column("Processors", "processors", 22, "Comma-separated processor IDs."),
        _Column("Data stores", "data_stores", 22, "Comma-separated data store IDs."),
        _Column("Retention trigger", "trigger", 18, "When the retention clock starts.", "triggers"),
        _Column("Retention days", "period_days", 10, "Days after the trigger before erasure. 0 erases at once."),
        _Column(
            "Third Schedule class",
            "third_schedule_class",
            14,
            "ecommerce, gaming or social_media if Rule 8(1) applies.",
        ),
        _Column("Retention reason", "retention_reason", 30, "Law or reason behind the period. Not imported."),
        _Column("Counsel confirmed", "confirmed", 12, "Not imported; listed in the scan report.", "yesno"),
        _Column("Notes", "notes", 40),
    ),
)

_FINDINGS = _Sheet(
    "Scan findings",
    "Output of the PII scan: types, locations and counts only. Never paste sampled values into this workbook.",
    (
        _Column("Data store", "store", 18, "Data store ID from the Data stores sheet."),
        _Column("Location", "location", 32, "table.column, file path or key."),
        _Column("Detected type", "detected_type", 18, "For example aadhaar, pan, mobile, email, dob."),
        _Column("Confidence", "confidence", 12, "Low-confidence findings are listed separately.", "confidence"),
        _Column("Rows sampled", "rows_sampled", 12),
        _Column("Matches", "matches", 10, "Sampled rows that matched. A count, never the values."),
        _Column("Data item", "data_item", 18, "Data item ID it maps to. Blank means not in the registry."),
        _Column("Action", "action", 14, "", "actions"),
        _Column("Notes", "notes", 40),
    ),
)

_GAPS = _Sheet(
    "Gaps",
    "Readiness checks mapped to dpdpkit features. Set a status for each; add rows for anything else found.",
    (
        _Column("Area", "area", 12),
        _Column("Check", "check", 60),
        _Column("Source", "source", 30, "Provision the check comes from. Counsel confirms how it applies."),
        _Column("dpdpkit feature", "feature", 34),
        _Column("Status", "status", 14, "", "gap_statuses"),
        _Column("Finding", "finding", 40, "What the workshop found."),
        _Column("Risk", "risk", 10, "", "risks"),
        _Column("Owner", "owner", 16),
        _Column("Due", "due", 12),
    ),
)

_LISTS: dict[str, tuple[str, ...]] = {
    "legal_bases": tuple(LEGAL_BASIS_LABELS.values()),
    "triggers": tuple(t.value for t in RetentionTrigger),
    "confidence": CONFIDENCE,
    "actions": ACTIONS,
    "gap_statuses": GAP_STATUSES,
    "risks": RISKS,
    "store_kinds": STORE_KINDS,
}

_README = """\
Data-mapping workbook

Fill this in during the data-mapping workshop with the client's tech lead and whoever owns compliance.

1. Engagement: client name, privacy contact, policy pack and who attended.
2. Data stores: every database, bucket, spreadsheet, SaaS tool, log and backup that holds personal data.
3. Scan findings: paste the counts from `dpdpkit scan` or a manual review. Types and counts only, never values.
4. Data items: one row per kind of personal data. Map every scan finding to a data item, or mark it for deletion.
5. Processors: every outside party that processes the data for the client.
6. Purposes: why each data item is processed, the legal basis, and how long it is kept.
7. Gaps: set a status for each readiness check and add anything else the workshop found.

Then:
  dpdpkit workbook import data-mapping.xlsx --base dpdpkit.yaml --out dpdpkit.yaml
  dpdpkit workbook report data-mapping.xlsx --out scan-report.md

Header comments explain each column. Columns marked "Not imported" are for the workshop and the report only.
Dropdowns come from the Lists sheet.

This workbook records engineering facts. Purposes, legal bases, retention periods and notice wording are
decisions for the client and their counsel; dpdpkit does not provide legal advice.
"""


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class ScanFinding:
    """One row of the Scan findings sheet. Counts only; the sampled values are never recorded."""

    store: str
    location: str
    detected_type: str
    confidence: str = "high"
    rows_sampled: int | None = None
    matches: int | None = None
    data_item: str | None = None
    action: str = ""
    notes: str = ""

    @property
    def unregistered(self) -> bool:
        """Personal data with no data item in the registry (false positives excluded)."""
        return self.data_item is None and self.action != "false positive"


@dataclass(frozen=True)
class Gap:
    area: str
    check: str
    source: str = ""
    feature: str = ""
    status: str = "not checked"
    finding: str = ""
    risk: str = ""
    owner: str = ""
    due: str = ""


@dataclass
class MappingWorkbook:
    """A filled data-mapping workbook, checked row by row."""

    engagement: dict[str, str]
    data_items: list[dict[str, Any]] = field(default_factory=list)
    data_stores: list[dict[str, Any]] = field(default_factory=list)
    processors: list[dict[str, Any]] = field(default_factory=list)
    purposes: list[dict[str, Any]] = field(default_factory=list)
    findings: list[ScanFinding] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    unconfirmed_purposes: list[str] = field(default_factory=list)
    """Purpose IDs whose "Counsel confirmed" cell is not yes."""
    unscanned_stores: list[str] = field(default_factory=list)
    """Data store IDs whose "Scanned" cell is not yes."""

    def to_config(self, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``dpdpkit.yaml`` keys from the workbook. Keys the workbook does not cover (notice...) come from ``base``."""
        cfg: dict[str, Any] = dict(base or {})
        cfg["policy"] = self.engagement.get("policy") or cfg.get("policy") or "dpdp-rules-2025.v1"
        overlays = _split(self.engagement.get("overlays", ""))
        cfg["overlays"] = overlays if overlays or "overlays" not in cfg else cfg["overlays"]
        contact = dict(cfg.get("contact") or {})
        for key, cfg_key in (("client", "name"), ("email", "email"), ("rights_url", "rights_url")):
            if self.engagement.get(key):
                contact[cfg_key] = self.engagement[key]
        if contact:
            cfg["contact"] = contact
        cfg["data_items"] = [dict(row) for row in self.data_items]
        cfg["processors"] = [dict(row) for row in self.processors]
        cfg["data_stores"] = [dict(row) for row in self.data_stores]
        cfg["purposes"] = [dict(row) for row in self.purposes]
        Registry.from_config(cfg)  # the same check kit_from_config runs
        return cfg


# --------------------------------------------------------------------------- writing


def _openpyxl() -> Any:
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError('the data-mapping workbook needs openpyxl: pip install "dpdpkit-core[workbook]"') from exc
    return openpyxl


def new_workbook(config: Mapping[str, Any] | None = None, *, engagement: Mapping[str, str] | None = None) -> bytes:
    """A data-mapping workbook as ``.xlsx`` bytes, pre-filled from ``config`` (a template or ``dpdpkit.yaml``)."""
    openpyxl = _openpyxl()
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    cfg = dict(config or {})
    contact = cfg.get("contact") or {}
    values: dict[str, str] = {
        "client": str(contact.get("name", "")),
        "email": str(contact.get("email", "")),
        "rights_url": str(contact.get("rights_url", "")),
        "policy": str(cfg.get("policy") or "dpdp-rules-2025.v1"),
        "overlays": ", ".join(str(o) for o in cfg.get("overlays") or []),
        **(engagement or {}),
    }

    book = openpyxl.Workbook()
    bold = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDE7F0")
    wrap = Alignment(wrap_text=True, vertical="top")

    readme = book.active
    readme.title = "Read me"
    for row, line in enumerate(_README.splitlines(), start=1):
        readme.cell(row=row, column=1, value=line)
    readme["A1"].font = Font(bold=True, size=14)
    readme.column_dimensions["A"].width = 110

    sheet = book.create_sheet("Engagement")
    sheet.append(["Field", "Value", "Notes"])
    for key, label, note in _ENGAGEMENT_FIELDS:
        sheet.append([label, values.get(key, ""), note])
    for letter, width in zip("ABC", (26, 40, 70), strict=True):
        sheet.column_dimensions[letter].width = width
    for cell in sheet[1]:
        cell.font, cell.fill = bold, header_fill

    lists = book.create_sheet("Lists")
    for col, (name, options) in enumerate(_LISTS.items(), start=1):
        lists.cell(row=1, column=col, value=name).font = bold
        for row, option in enumerate(options, start=2):
            lists.cell(row=row, column=col, value=option)
        lists.column_dimensions[_letter(col)].width = max(14, *(len(o) for o in options))
    lists.sheet_state = "hidden"

    def ranges(name: str) -> str:
        col = _letter(list(_LISTS).index(name) + 1)
        return f"=Lists!${col}$2:${col}${len(_LISTS[name]) + 1}"

    def table(spec: _Sheet, rows: Iterable[Mapping[str, Any]]) -> None:
        ws = book.create_sheet(spec.title)
        ws.append([spec.intro])
        ws["A1"].font = Font(italic=True)
        ws.append([c.header for c in spec.columns])
        for col, column in enumerate(spec.columns, start=1):
            cell = ws.cell(row=2, column=col)
            cell.font, cell.fill = bold, header_fill
            if column.help:
                cell.comment = Comment(column.help, "dpdpkit")
            ws.column_dimensions[_letter(col)].width = column.width
            if column.choices:
                formula = '"yes,no"' if column.choices == "yesno" else ranges(column.choices)
                validation = DataValidation(type="list", formula1=formula, allow_blank=True)
                validation.add(f"{_letter(col)}3:{_letter(col)}1000")
                ws.add_data_validation(validation)
        for row in rows:
            ws.append([_cell(row.get(c.key)) for c in spec.columns])
            for cell in ws[ws.max_row]:
                cell.alignment = wrap
        ws.freeze_panes = "B3"

    table(_DATA_STORES, ({**s, "kind": s.get("kind", "database")} for s in cfg.get("data_stores") or []))
    table(_FINDINGS, ())
    table(_DATA_ITEMS, cfg.get("data_items") or [])
    table(_PROCESSORS, cfg.get("processors") or [])
    table(_PURPOSES, (_purpose_row(p) for p in cfg.get("purposes") or []))
    table(_GAPS, ({"area": a, "check": c, "source": s, "feature": f} for a, c, s, f in READINESS_CHECKS))
    book.move_sheet("Lists", offset=len(book.sheetnames))

    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _purpose_row(purpose: Mapping[str, Any]) -> dict[str, Any]:
    retention = purpose.get("retention") or {}
    basis = LegalBasis(purpose.get("legal_basis", LegalBasis.CONSENT.value))
    return {
        **purpose,
        "legal_basis": LEGAL_BASIS_LABELS[basis],
        "trigger": None
        if retention.get("third_schedule_class") or not retention
        else retention.get("trigger", RetentionTrigger.LAST_ACTIVITY.value),
        "period_days": retention.get("period_days"),
        "third_schedule_class": retention.get("third_schedule_class"),
    }


def _cell(value: Any) -> Any:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return str(value)


def _letter(col: int) -> str:
    from openpyxl.utils import get_column_letter

    return str(get_column_letter(col))


# --------------------------------------------------------------------------- reading


def read_workbook(source: str | Path | bytes) -> MappingWorkbook:
    """Read and check a filled workbook. Raises :class:`ConfigError` listing every problem with its sheet and row."""
    openpyxl = _openpyxl()
    try:
        book = openpyxl.load_workbook(io.BytesIO(source) if isinstance(source, bytes) else source, data_only=True)
    except (OSError, ValueError, KeyError) as exc:
        raise ConfigError(f"cannot read the workbook: {exc}") from exc
    except Exception as exc:  # zipfile.BadZipFile and openpyxl's own errors for files that are not xlsx
        raise ConfigError(f"cannot read the workbook (is it an .xlsx file?): {exc}") from exc

    errors: list[str] = []
    missing = [s.title for s in (_DATA_ITEMS, _PURPOSES) if s.title not in book.sheetnames]
    if "Engagement" not in book.sheetnames or missing:
        sheets = ", ".join((["Engagement"] if "Engagement" not in book.sheetnames else []) + missing)
        raise ConfigError(f"not a dpdpkit data-mapping workbook: missing sheet(s) {sheets}")

    engagement = _read_engagement(book["Engagement"])
    result = MappingWorkbook(engagement=engagement)

    def sheet_rows(spec: _Sheet) -> list[tuple[int, dict[str, Any]]]:
        return _rows(book[spec.title], spec, errors) if spec.title in book.sheetnames else []

    def check_id(where: str, value: Any, seen: set[str]) -> str | None:
        ident = _text(value)
        if not ident:
            errors.append(f"{where}: ID is empty")
            return None
        if not _ID.match(ident):
            errors.append(f"{where}: ID {ident!r} must be lowercase letters, digits, _ or -")
        if ident in seen:
            errors.append(f"{where}: duplicate ID {ident!r}")
        seen.add(ident)
        return ident

    def flag(where: str, column: str, value: Any) -> bool:
        text = _text(value).lower()
        if text in _YES:
            return True
        if text not in _NO:
            errors.append(f"{where}: {column} must be yes or no, not {text!r}")
        return False

    stores: set[str] = set()
    for row_no, row in sheet_rows(_DATA_STORES):
        where = f"Data stores row {row_no}"
        ident = check_id(where, row.get("id"), stores)
        if ident is None:
            continue
        store: dict[str, Any] = {"id": ident, "name": _text(row.get("name")) or ident}
        if _text(row.get("kind")):
            store["kind"] = _text(row.get("kind"))
        if _text(row.get("region")):
            store["region"] = _text(row.get("region")).upper()
        result.data_stores.append(store)
        if not flag(where, "Scanned", row.get("scanned")):
            result.unscanned_stores.append(ident)

    items: set[str] = set()
    for row_no, row in sheet_rows(_DATA_ITEMS):
        where = f"Data items row {row_no}"
        ident = check_id(where, row.get("id"), items)
        if ident is None:
            continue
        item: dict[str, Any] = {"id": ident, "name": _text(row.get("name"))}
        if not item["name"]:
            errors.append(f"{where}: Name is empty (principals see it in the notice)")
        for key in ("description", "category"):
            if _text(row.get(key)):
                item[key] = _text(row.get(key))
        if flag(where, "Sensitive", row.get("sensitive")):
            item["sensitive"] = True
        result.data_items.append(item)

    processors: set[str] = set()
    for row_no, row in sheet_rows(_PROCESSORS):
        where = f"Processors row {row_no}"
        ident = check_id(where, row.get("id"), processors)
        if ident is None:
            continue
        processor: dict[str, Any] = {"id": ident, "name": _text(row.get("name")) or ident}
        processor["country"] = (_text(row.get("country")) or "IN").upper()
        for key in ("contact", "erasure_webhook"):
            if _text(row.get(key)):
                processor[key] = _text(row.get(key))
        flag(where, "Contract in place", row.get("contract"))
        result.processors.append(processor)

    purposes: set[str] = set()
    for row_no, row in sheet_rows(_PURPOSES):
        where = f"Purposes row {row_no}"
        ident = check_id(where, row.get("id"), purposes)
        if ident is None:
            continue
        where = f"{where} ({ident})"
        purpose = _read_purpose(where, ident, row, errors, items=items, processors=processors, stores=stores)
        result.purposes.append(purpose)
        if not flag(where, "Counsel confirmed", row.get("confirmed")):
            result.unconfirmed_purposes.append(ident)

    for row_no, row in sheet_rows(_FINDINGS):
        finding = _read_finding(f"Scan findings row {row_no}", row, errors, items=items, stores=stores)
        if finding is not None:
            result.findings.append(finding)

    for row_no, row in sheet_rows(_GAPS):
        where = f"Gaps row {row_no}"
        status = _text(row.get("status")).lower() or "not checked"
        risk = _text(row.get("risk")).lower()
        if status not in GAP_STATUSES:
            errors.append(f"{where}: Status must be one of {', '.join(GAP_STATUSES)}")
        if risk and risk not in RISKS:
            errors.append(f"{where}: Risk must be high, medium or low")
        result.gaps.append(
            Gap(
                area=_text(row.get("area")),
                check=_text(row.get("check")),
                source=_text(row.get("source")),
                feature=_text(row.get("feature")),
                status=status,
                finding=_text(row.get("finding")),
                risk=risk,
                owner=_text(row.get("owner")),
                due=_text(row.get("due")),
            )
        )

    if errors:
        raise ConfigError(f"the workbook has {len(errors)} problem(s):\n  " + "\n  ".join(errors), problems=errors)
    return result


def _read_engagement(ws: Worksheet) -> dict[str, str]:
    by_label = {label.lower(): key for key, label, _ in _ENGAGEMENT_FIELDS}
    out: dict[str, str] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        key = by_label.get(_text(row[0]).lower())
        if key is not None and len(row) > 1:
            out[key] = _text(row[1])
    return out


def _rows(ws: Worksheet, spec: _Sheet, errors: list[str]) -> list[tuple[int, dict[str, Any]]]:
    """Rows below the header (row 2), keyed by column; columns are matched by header so they may be reordered."""
    headers = [_text(c.value).rstrip("* ").lower() for c in ws[2]] if ws.max_row >= 2 else []
    by_header = {c.header.lower(): c.key for c in spec.columns}
    keys = [by_header.get(h) for h in headers]
    for column in spec.columns:
        if column.key in ("id", "check", "location") and column.key not in keys:
            errors.append(f"{spec.title}: header row 2 has no {column.header!r} column")
            return []
    out = []
    for row_no, values in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        row = {k: v for k, v in zip(keys, values, strict=False) if k is not None}
        if any(_text(v) for v in row.values()):
            out.append((row_no, row))
    return out


def _read_purpose(
    where: str,
    ident: str,
    row: Mapping[str, Any],
    errors: list[str],
    *,
    items: set[str],
    processors: set[str],
    stores: set[str],
) -> dict[str, Any]:
    purpose: dict[str, Any] = {"id": ident, "title": _text(row.get("title"))}
    if not purpose["title"]:
        errors.append(f"{where}: Title is empty (principals see it in the notice)")
    if _text(row.get("description")):
        purpose["description"] = _text(row.get("description"))

    for key, column, known, sheet in (
        ("data_items", "Data items", items, "Data items"),
        ("processors", "Processors", processors, "Processors"),
        ("data_stores", "Data stores", stores, "Data stores"),
    ):
        refs = _split(row.get(key))
        unknown = [r for r in refs if r not in known]
        if unknown:
            errors.append(f"{where}: {column} {', '.join(unknown)} not on the {sheet} sheet")
        if refs:
            purpose[key] = refs
    if not purpose.get("data_items"):
        errors.append(f"{where}: Data items is empty")

    basis_text = _text(row.get("legal_basis")).split(":")[0].strip().lower() or LegalBasis.CONSENT.value
    try:
        basis = LegalBasis(basis_text)
    except ValueError:
        errors.append(f"{where}: unknown legal basis {basis_text!r} (use the dropdown)")
        basis = LegalBasis.CONSENT
    if basis is not LegalBasis.CONSENT:
        purpose["legal_basis"] = basis.value

    required = _text(row.get("required")).lower()
    if required in _YES:
        purpose["required"] = True
    elif required not in _NO:
        errors.append(f"{where}: Required must be yes or no, not {required!r}")

    trigger = _text(row.get("trigger")).lower()
    days_text = _text(row.get("period_days"))
    schedule = _text(row.get("third_schedule_class"))
    if schedule:
        purpose["retention"] = {"third_schedule_class": schedule}
    elif trigger or days_text:
        retention: dict[str, Any] = {}
        if trigger:
            if trigger in {t.value for t in RetentionTrigger}:
                retention["trigger"] = trigger
            else:
                errors.append(f"{where}: unknown retention trigger {trigger!r} (use the dropdown)")
        if days_text:
            try:
                days = float(days_text)
                if days < 0 or days != int(days):
                    raise ValueError
                retention["period_days"] = int(days)
            except ValueError:
                errors.append(f"{where}: Retention days must be a whole number of days, not {days_text!r}")
        else:
            errors.append(f"{where}: Retention days is empty")
        purpose["retention"] = retention
    else:
        errors.append(f"{where}: no retention rule (set a trigger and days, or a Third Schedule class)")
    return purpose


def _read_finding(
    where: str, row: Mapping[str, Any], errors: list[str], *, items: set[str], stores: set[str]
) -> ScanFinding | None:
    store, location = _text(row.get("store")), _text(row.get("location"))
    if not location:
        errors.append(f"{where}: Location is empty")
        return None
    if not store:
        errors.append(f"{where}: Data store is empty")
    elif store not in stores:
        errors.append(f"{where}: data store {store!r} not on the Data stores sheet")
    confidence = _text(row.get("confidence")).lower() or "high"
    if confidence not in CONFIDENCE:
        errors.append(f"{where}: Confidence must be high, medium or low")
    action = _text(row.get("action")).lower()
    if action and action not in ACTIONS:
        errors.append(f"{where}: Action must be one of {', '.join(ACTIONS)}")
    data_item = _text(row.get("data_item")) or None
    if data_item is not None and data_item not in items:
        errors.append(f"{where}: data item {data_item!r} not on the Data items sheet")
    counts: dict[str, int | None] = {}
    for key, column in (("rows_sampled", "Rows sampled"), ("matches", "Matches")):
        text = _text(row.get(key))
        if not text:
            counts[key] = None
            continue
        try:
            number = float(text)
            if number < 0 or number != int(number):
                raise ValueError
            counts[key] = int(number)
        except ValueError:
            errors.append(f"{where}: {column} must be a count, not {text!r} (never paste sampled values)")
            counts[key] = None
    return ScanFinding(
        store=store,
        location=location,
        detected_type=_text(row.get("detected_type")) or "unknown",
        confidence=confidence,
        rows_sampled=counts["rows_sampled"],
        matches=counts["matches"],
        data_item=data_item,
        action=action,
        notes=_text(row.get("notes")),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        return value.date().isoformat() if value.time() == dt.time() else value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def _split(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\n]", _text(value)) if part.strip()]


# --------------------------------------------------------------------------- scan report

_RISK_ORDER = {"high": 0, "medium": 1, "low": 2, "": 3}


def scan_report(book: MappingWorkbook, *, today: dt.date | None = None) -> str:
    """The client-facing scan report (Markdown) for a filled workbook. Types, locations and counts only."""
    template = Template((resources.files(__name__) / "scan-report.md").read_text(encoding="utf-8"))
    eng = book.engagement
    names = {s["id"]: s["name"] for s in book.data_stores}
    item_names = {i["id"]: i["name"] for i in book.data_items}
    low = [f for f in book.findings if f.confidence == "low"]
    false_positives = [f for f in book.findings if f.confidence != "low" and f.action == "false positive"]
    confident = [f for f in book.findings if f not in low and f not in false_positives]
    unregistered = [f for f in confident if f.unregistered]
    found_items = {f.data_item for f in book.findings if f.data_item}
    not_found = [i for i in book.data_items if i["id"] not in found_items]
    open_gaps = sorted(
        (g for g in book.gaps if g.status in ("gap", "partly met")), key=lambda g: (_RISK_ORDER.get(g.risk, 3), g.area)
    )
    unchecked = [g for g in book.gaps if g.status == "not checked"]

    summary = [
        f"{len(book.data_stores)} data store(s) mapped, {len(book.data_stores) - len(book.unscanned_stores)} scanned.",
        f"{len(confident)} finding(s) of personal data ({len(false_positives)} more marked as false positives), "
        f"plus {len(low)} low-confidence finding(s) listed separately.",
        f"{len(unregistered)} location(s) hold personal data that is not in the registry yet.",
        f"{len(book.data_items)} data item(s), {len(book.purposes)} purpose(s) and {len(book.processors)} "
        "processor(s) recorded.",
        f"{len(book.unconfirmed_purposes)} of {len(book.purposes)} purpose(s) still need counsel's confirmation.",
        f"{len(open_gaps)} open gap(s) out of {len(book.gaps)} readiness check(s); {len(unchecked)} not checked yet.",
    ]

    def row(f: ScanFinding) -> list[Any]:
        item = item_names.get(f.data_item, f.data_item) if f.data_item else "**not registered**"
        return [f.location, f.detected_type, _count(f.matches, f.rows_sampled), item, f.action]

    by_store: list[str] = []
    for store_id in dict.fromkeys(f.store for f in confident):
        rows = [[*row(f)[:2], f.confidence, *row(f)[2:]] for f in confident if f.store == store_id]
        by_store.append(f"### {names.get(store_id, store_id)} (`{store_id}`)")
        by_store.append(_table(["Location", "Type", "Confidence", "Matches", "Data item", "Action"], rows))

    stores = _table(
        ["Data store", "ID", "Kind", "Region", "Scanned"],
        [
            [
                s["name"],
                f"`{s['id']}`",
                s.get("kind", "database"),
                s.get("region", "IN"),
                "no" if s["id"] in book.unscanned_stores else "yes",
            ]
            for s in book.data_stores
        ],
    )
    gaps = _table(
        ["Risk", "Area", "Check", "Status", "Finding", "dpdpkit feature", "Owner", "Due"],
        [[g.risk or "-", g.area, g.check, g.status, g.finding, g.feature, g.owner, g.due] for g in open_gaps],
    )
    by_store_text = "\n\n".join(by_store) if by_store else "No personal data was found by the scan, or no scan was run."
    return template.substitute(
        client=_md(eng.get("client") or "Client"),
        prepared_by=_md(eng.get("prepared_by") or "-"),
        date=(today or dt.date.today()).isoformat(),
        workshop_date=_md(eng.get("workshop_date") or "-"),
        tech_lead=_md(eng.get("tech_lead") or ""),
        compliance_owner=_md(eng.get("compliance_owner") or ""),
        summary="\n".join(f"- {line}" for line in summary),
        stores=stores if book.data_stores else "No data stores were recorded.",
        findings=by_store_text,
        unregistered=_or_none(
            _table(
                ["Data store", "Location", "Type", "Matches", "Action"],
                [
                    [
                        names.get(f.store, f.store),
                        f.location,
                        f.detected_type,
                        _count(f.matches, f.rows_sampled),
                        f.action or "decide: register, delete or minimise",
                    ]
                    for f in unregistered
                ],
            ),
            unregistered,
            "Every finding maps to a registered data item.",
        ),
        not_found=_or_none(
            _table(
                ["Data item", "ID", "Sensitive"],
                [[i["name"], f"`{i['id']}`", "yes" if i.get("sensitive") else "no"] for i in not_found],
            ),
            not_found if book.findings else [],
            "Every registered data item was found by the scan."
            if book.findings
            else "No scan findings were recorded, so this check was not run.",
        ),
        low=_or_none(
            _table(
                ["Data store", "Location", "Type", "Matches", "Data item", "Action"],
                [[names.get(f.store, f.store), *row(f)] for f in low],
            ),
            low,
            "None.",
        ),
        unconfirmed=", ".join(f"`{p}`" for p in book.unconfirmed_purposes) or "none",
        gaps=_or_none(gaps, open_gaps, "No open gaps were recorded."),
    )


def _count(matches: int | None, sampled: int | None) -> str:
    if matches is None:
        return "-"
    return f"{matches} of {sampled}" if sampled is not None else str(matches)


def _or_none(text: str, rows: Sequence[Any], empty: str) -> str:
    return text if rows else empty


def _md(value: Any) -> str:
    """Workbook text made safe for a Markdown table cell or a single line."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(_md(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)
