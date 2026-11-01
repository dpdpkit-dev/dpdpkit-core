from __future__ import annotations

import datetime as dt
import io
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from dpdpkit import ConfigError, InMemoryRepository, MemoryNotifier, Registry, kit_from_config, sync_notice
from dpdpkit.cli import main
from dpdpkit.templates import list_templates, load_template
from dpdpkit.workbook import READINESS_CHECKS, MappingWorkbook, new_workbook, read_workbook, scan_report

openpyxl = pytest.importorskip("openpyxl")

from .conftest import FakeClock  # noqa: E402


def _edit(data: bytes, rows: dict[str, list[dict[str, Any]]], *, engagement: dict[str, str] | None = None) -> bytes:
    """Append rows (keyed by header) to sheets, and set engagement values (keyed by field label)."""
    book = openpyxl.load_workbook(io.BytesIO(data))
    for title, new_rows in rows.items():
        ws = book[title]
        headers = [c.value for c in ws[2]]
        for row in new_rows:
            ws.append([row.get(h) for h in headers])
    for label, value in (engagement or {}).items():
        (row,) = [r for r in book["Engagement"].iter_rows(min_row=2) if r[0].value == label]
        row[1].value = value
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _set(data: bytes, title: str, column: str, value: Any, *, where: tuple[str, Any] | None = None) -> bytes:
    """Set ``column`` on every row of a sheet (or the rows whose ``where`` column matches)."""
    book = openpyxl.load_workbook(io.BytesIO(data))
    ws = book[title]
    headers = [c.value for c in ws[2]]
    for row in ws.iter_rows(min_row=3):
        if where is None or row[headers.index(where[0])].value == where[1]:
            row[headers.index(column)].value = value
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@pytest.mark.parametrize("template_id", [t.id for t in list_templates()])
def test_template_round_trips_through_the_workbook(template_id: str, clock: FakeClock) -> None:
    original = load_template(template_id).config()
    book = read_workbook(new_workbook(original))
    config = book.to_config(original)

    before, after = Registry.from_config(original), Registry.from_config(config)
    assert after.data_items() == before.data_items()
    assert after.processors() == before.processors()
    assert after.purposes() == before.purposes()
    assert config["contact"] == original["contact"]
    assert config["notice"] == original["notice"]  # kept from the base config
    assert book.unconfirmed_purposes == [p["id"] for p in original["purposes"]]

    kit = kit_from_config(config, InMemoryRepository(), notifier=MemoryNotifier(), clock=clock, signing_key="k")
    assert sync_notice(kit, config)


def test_blank_workbook_has_every_sheet_and_the_readiness_checks() -> None:
    book = openpyxl.load_workbook(io.BytesIO(new_workbook()))
    assert book.sheetnames == [
        *["Read me", "Engagement", "Data stores", "Scan findings"],
        *["Data items", "Processors", "Purposes", "Gaps", "Lists"],
    ]
    assert book["Lists"].sheet_state == "hidden"
    assert book["Purposes"].data_validations.dataValidation  # dropdowns
    parsed = read_workbook(new_workbook())
    assert parsed.purposes == [] and parsed.findings == []
    assert [(g.area, g.check) for g in parsed.gaps] == [(a, c) for a, c, _, _ in READINESS_CHECKS]
    assert all(g.status == "not checked" for g in parsed.gaps)
    assert parsed.engagement["policy"] == "dpdp-rules-2025.v1"


def _filled() -> bytes:
    data = new_workbook(load_template("ecommerce").config(), engagement={"prepared_by": "Implementer"})
    data = _edit(
        data,
        {
            "Data stores": [
                {"ID": "app_db", "Name": "Main Postgres", "Kind": "database", "Region": "in", "Scanned": "yes"},
                {"ID": "crm", "Name": "CRM exports", "Kind": "spreadsheet", "Scanned": "no"},
            ],
            "Scan findings": [
                {
                    "Data store": "app_db",
                    "Location": "users.email",
                    "Detected type": "email",
                    "Confidence": "high",
                    "Rows sampled": 500,
                    "Matches": 498,
                    "Data item": "email",
                    "Action": "keep",
                },
                {
                    "Data store": "app_db",
                    "Location": "users.pan_no",
                    "Detected type": "pan",
                    "Confidence": "high",
                    "Rows sampled": 500,
                    "Matches": 120,
                },
                {
                    "Data store": "app_db",
                    "Location": "orders.note",
                    "Detected type": "mobile",
                    "Confidence": "low",
                    "Rows sampled": 500,
                    "Matches": 3,
                },
                {
                    "Data store": "app_db",
                    "Location": "orders.ref",
                    "Detected type": "upi",
                    "Confidence": "medium",
                    "Matches": 7,
                    "Action": "false positive",
                },
            ],
        },
        engagement={"Workshop date": "2027-02-03", "Client tech lead": "Tech Lead | Platform"},
    )
    data = _set(data, "Purposes", "Counsel confirmed", "yes")
    data = _set(data, "Purposes", "Counsel confirmed", "no", where=("ID", "ad_audiences"))
    data = _set(data, "Gaps", "Status", "met")
    data = _set(data, "Gaps", "Status", "gap", where=("Area", "Children"))
    data = _set(data, "Gaps", "Risk", "high", where=("Area", "Children"))
    data = _set(data, "Gaps", "Finding", "No age gate", where=("Area", "Children"))
    return _set(data, "Gaps", "Status", "partly met", where=("Area", "Breach"))


def test_filled_workbook_reads_findings_gaps_and_stores() -> None:
    book = read_workbook(_filled())
    assert book.engagement["workshop_date"] == "2027-02-03"
    assert book.data_stores == [
        {"id": "app_db", "name": "Main Postgres", "kind": "database", "region": "IN"},
        {"id": "crm", "name": "CRM exports", "kind": "spreadsheet"},
    ]
    assert book.unscanned_stores == ["crm"]
    assert book.unconfirmed_purposes == ["ad_audiences"]
    email, pan, low, upi = book.findings
    assert (email.matches, email.rows_sampled, email.data_item, email.unregistered) == (498, 500, "email", False)
    assert pan.unregistered and not upi.unregistered  # false positives are not unregistered data
    assert low.confidence == "low"
    assert {g.area: g.status for g in book.gaps}["Children"] == "gap"
    config = book.to_config()
    assert config["data_stores"][0]["region"] == "IN"


def test_scan_report_lists_unregistered_data_and_gaps() -> None:
    report = scan_report(read_workbook(_filled()), today=dt.date(2027, 2, 10))
    assert report.startswith("# Personal data scan report: Example Retail Pvt Ltd\n")
    assert "Workshop: 2027-02-03. Report: 2027-02-10. Prepared by: Implementer." in report
    assert "- 2 data store(s) mapped, 1 scanned." in report
    assert (
        "- 2 finding(s) of personal data (1 more marked as false positives), "
        "plus 1 low-confidence finding(s) listed separately."
    ) in report
    assert "- 1 location(s) hold personal data that is not in the registry yet." in report
    assert "- 1 of 8 purpose(s) still need counsel's confirmation." in report
    assert "- 2 open gap(s) out of 15 readiness check(s); 0 not checked yet." in report
    assert "### Main Postgres (`app_db`)" in report
    assert "| users.email | email | high | 498 of 500 | Email address | keep |" in report
    assert "| Main Postgres | users.pan_no | pan | 120 of 500 | decide: register, delete or minimise |" in report
    assert "| Main Postgres | orders.note | mobile | 3 of 500 | **not registered** |  |" in report
    assert "| Phone" not in report and "| `email` |" not in report  # email was found
    assert "| Mobile number | `phone` | no |" in report  # registered, not found
    gaps = report.split("## Gap list")[1]
    assert gaps.index("| high | Children |") < gaps.index("| - | Breach |")  # highest risk first
    assert "No age gate" in gaps
    assert "Purposes awaiting counsel's confirmation: `ad_audiences`." in report
    assert "Tech Lead \\| Platform" in report  # pipes escaped in tables
    assert "orders.ref" not in report  # false positive
    assert "$" not in report


def test_scan_report_without_a_scan() -> None:
    report = scan_report(MappingWorkbook(engagement={}), today=dt.date(2027, 2, 10))
    assert "# Personal data scan report: Client" in report
    assert "No personal data was found by the scan, or no scan was run." in report
    assert "No scan findings were recorded, so this check was not run." in report
    assert "No data stores were recorded." in report


def test_every_problem_is_reported_with_its_row() -> None:
    data = new_workbook(load_template("saas").config())
    data = _edit(
        data,
        {
            "Data items": [{"ID": "name", "Name": "Duplicate"}, {"ID": "Bad ID", "Name": "x", "Sensitive": "maybe"}],
            "Purposes": [
                {
                    "ID": "extra",
                    "Title": "",
                    "Data items": "name, ghost",
                    "Legal basis": "s9_z",
                    "Required": "sure",
                    "Processors": "nobody",
                    "Data stores": "nowhere",
                    "Retention trigger": "someday",
                    "Retention days": "a year",
                },
                {"ID": "no_retention", "Title": "No retention", "Data items": "name"},
                {"ID": "no_days", "Title": "No days", "Data items": "name", "Retention trigger": "withdrawal"},
            ],
            "Scan findings": [
                {
                    "Data store": "unknown_db",
                    "Location": "t.c",
                    "Confidence": "certain",
                    "Matches": "98765 43210",
                    "Data item": "ghost",
                    "Action": "shrug",
                },
                {"Location": "t.d"},
                {"Data store": "x", "Detected type": "pan"},
            ],
        },
    )
    data = _set(data, "Gaps", "Status", "done", where=("Area", "Breach"))
    data = _set(data, "Gaps", "Risk", "huge", where=("Area", "Breach"))
    with pytest.raises(ConfigError) as caught:
        read_workbook(data)
    problems = caught.value.details["problems"]
    expected = [
        "duplicate ID 'name'",
        "ID 'Bad ID' must be lowercase",
        "Sensitive must be yes or no, not 'maybe'",
        "Title is empty",
        "Data items ghost not on the Data items sheet",
        "unknown legal basis 's9_z'",
        "Required must be yes or no, not 'sure'",
        "Processors nobody not on the Processors sheet",
        "Data stores nowhere not on the Data stores sheet",
        "unknown retention trigger 'someday'",
        "Retention days must be a whole number of days, not 'a year'",
        "Retention days is empty",
        "data store 'unknown_db' not on the Data stores sheet",
        "Confidence must be high, medium or low",
        "Matches must be a count, not '98765 43210' (never paste sampled values)",
        "data item 'ghost' not on the Data items sheet",
        "Action must be one of",
        "Data store is empty",
        "Location is empty",
        "Status must be one of",
        "Risk must be high, medium or low",
    ]
    for fragment in expected:
        assert any(fragment in p for p in problems), fragment
    assert any(p.startswith("Purposes row ") and "(no_retention): no retention rule" in p for p in problems)
    assert str(caught.value).startswith(f"the workbook has {len(problems)} problem(s):")


def test_columns_are_matched_by_header_and_blank_rows_skipped() -> None:
    book = openpyxl.load_workbook(io.BytesIO(new_workbook(load_template("saas").config())))
    ws = book["Data items"]
    ws.insert_cols(1)
    ws.cell(row=2, column=1, value="Our own column")
    ws.cell(row=2, column=2, value="ID *")  # a marked-up header still matches
    ws.append([])
    ws.append(["ignored"])  # only an unknown column filled: not a data row
    out = io.BytesIO()
    book.save(out)
    parsed = read_workbook(out.getvalue())
    assert [i["id"] for i in parsed.data_items] == [i["id"] for i in load_template("saas").config()["data_items"]]


def test_not_a_workbook(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"is it an \.xlsx file"):
        read_workbook(b"not a spreadsheet")
    with pytest.raises(ConfigError, match="cannot read the workbook"):
        read_workbook(tmp_path / "missing.xlsx")
    other = openpyxl.Workbook()
    out = io.BytesIO()
    other.save(out)
    with pytest.raises(ConfigError, match=r"missing sheet\(s\) Engagement, Data items, Purposes"):
        read_workbook(out.getvalue())
    book = openpyxl.load_workbook(io.BytesIO(new_workbook()))
    book["Purposes"].delete_rows(2)
    out = io.BytesIO()
    book.save(out)
    with pytest.raises(ConfigError, match="Purposes: header row 2 has no 'ID' column"):
        read_workbook(out.getvalue())


def test_engagement_overrides_base_contact_and_overlays() -> None:
    base = load_template("health").config()
    data = new_workbook(base)
    data = _edit(data, {}, engagement={"Client (legal name)": "Real Clinic LLP", "Overlays": "cert-in-2022"})
    config = read_workbook(data).to_config({**base, "overlays": ["old"]})
    assert config["contact"] == {**base["contact"], "name": "Real Clinic LLP"}
    assert config["overlays"] == ["cert-in-2022"]
    blank_overlays = read_workbook(new_workbook(base)).to_config({**base, "overlays": ["kept"]})
    assert blank_overlays["overlays"] == ["kept"]
    assert read_workbook(new_workbook()).to_config()["policy"] == "dpdp-rules-2025.v1"


def test_cli_workbook_new_import_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["workbook", "new", "--template", "fintech"]) == 0
    assert "wrote data-mapping.xlsx" in capsys.readouterr().out
    assert read_workbook(tmp_path / "data-mapping.xlsx").engagement["template"] == "fintech"
    assert main(["workbook", "new"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["workbook", "new", "--template", "saas", "--from", "x.yaml"]) == 1
    assert "not both" in capsys.readouterr().err

    (tmp_path / "dpdpkit.yaml").write_text(load_template("fintech").text, encoding="utf-8")
    assert main(["workbook", "new", "--from", "dpdpkit.yaml", "--out", "from-config.xlsx"]) == 0
    capsys.readouterr()

    assert main(["workbook", "import", "data-mapping.xlsx", "--base", "dpdpkit.yaml", "--out", "dpdpkit.yaml"]) == 1
    assert "already exists" in capsys.readouterr().err
    args = ["workbook", "import", "from-config.xlsx", "--base", "dpdpkit.yaml", "--out", "dpdpkit.yaml", "--force"]
    assert main(args) == 0
    assert "counsel has not confirmed account, kyc," in capsys.readouterr().err
    text = (tmp_path / "dpdpkit.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Generated by `dpdpkit workbook import` from from-config.xlsx.\n")
    written = yaml.safe_load(text)
    assert written["notice"] == load_template("fintech").config()["notice"]

    assert main(["workbook", "import", "data-mapping.xlsx"]) == 0
    assert "purposes:" in capsys.readouterr().out
    assert main(["workbook", "report", "data-mapping.xlsx", "--out", "scan-report.md"]) == 0
    assert (tmp_path / "scan-report.md").read_text(encoding="utf-8").startswith("# Personal data scan report: ")
    assert main(["workbook", "report"]) == 1
    assert "needs the workbook file" in capsys.readouterr().err

    (tmp_path / "broken.xlsx").write_bytes(_edit(new_workbook(), {"Purposes": [{"ID": "x", "Title": "X"}]}))
    assert main(["workbook", "import", "broken.xlsx"]) == 1
    assert "Purposes row 3 (x): Data items is empty" in capsys.readouterr().err


def test_cli_explains_the_missing_extra(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "openpyxl", None)  # import fails as if the extra were not installed
    assert main(["workbook", "new", "--out", "never.xlsx"]) == 1
    assert 'pip install "dpdpkit-core[workbook]"' in capsys.readouterr().err
