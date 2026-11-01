"""Legal review packs for notice wording.

dpdpkit records and enforces the notice you configure; whether its wording meets DPDP Act s.5 and Rule 3 is a
decision for counsel. This module prepares that review:

* :func:`notice_checks` runs the checks software can make (required links, itemised data and purposes,
  untranslated text, placeholders) and lists the questions only counsel can answer.
* :func:`notice_hashes` gives the content hash of each locale. It is the same hash dpdpkit records in the ledger
  when the notice is published (``notice.published``), so a signed review can be matched to the exact
  published version.
* :func:`review_pack` renders the notice, the checks and a sign-off table as Markdown for the reviewer.

``dpdpkit notice review`` runs it from the command line, for a ``dpdpkit.yaml`` or a purpose template.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from string import Template
from typing import TYPE_CHECKING, Any

from ..models import LegalBasis, NoticeContent
from ..notices import content_hash

if TYPE_CHECKING:
    from ..kit import Kit

__all__ = ["CHECK_STATUSES", "NoticeCheck", "notice_checks", "notice_hashes", "notice_translations_for", "review_pack"]

CHECK_STATUSES = ("pass", "fail", "warn", "counsel")
"""``pass`` and ``fail`` are automated results; ``warn`` needs fixing before go-live but not before review
(placeholder values); ``counsel`` is a question for the reviewer."""

PLACEHOLDER_MARKERS = ("example.", "Example ")
"""Text that marks a value copied from a starter file or purpose template."""

_LOCALE_NAMES = {"en": "English", "hi": "Hindi"}


@dataclass(frozen=True)
class NoticeCheck:
    id: str
    source: str
    """Where the requirement comes from (DPDP Act section or Rule)."""
    requirement: str
    status: str
    """One of :data:`CHECK_STATUSES`."""
    detail: str = ""


def notice_translations_for(config: Mapping[str, Any]) -> tuple[Kit, dict[str, NoticeContent]]:
    """Build a throwaway in-memory kit for ``config`` and the notice content it would publish, per locale."""
    from ..config import kit_from_config, notice_translations
    from ..errors import ConfigError
    from ..repository import InMemoryRepository

    if not config.get("notice"):
        raise ConfigError("the configuration has no notice to review")
    kit = kit_from_config(config, InMemoryRepository(), signing_key="notice-review")
    return kit, notice_translations(kit, config["notice"])


def notice_hashes(translations: Mapping[str, NoticeContent]) -> dict[str, str]:
    """Content hash per locale, as recorded when the notice is published."""
    return {locale: content_hash(content) for locale, content in sorted(translations.items())}


def notice_checks(kit: Kit, translations: Mapping[str, NoticeContent]) -> list[NoticeCheck]:
    """Automated checks of the notice against the active policy pack, then the questions for counsel."""
    default = kit.policy.default_language
    base = translations.get(default)
    checks: list[NoticeCheck] = []

    def check(id: str, source: str, requirement: str, problems: Sequence[str], ok: str, bad: str = "fail") -> None:
        status = bad if problems else "pass"
        checks.append(NoticeCheck(id, source, requirement, status, "; ".join(problems) if problems else ok))

    links = kit.policy.required_links
    check(
        "links",
        "DPDP Act s.5(1), s.6(4); Rules 2025, Rule 3(c)",
        "Links to withdraw consent, exercise rights and complain to the Board",
        [f"{loc}: no {link} link" for loc, c in translations.items() for link in links if not c.links.get(link)],
        f"every locale links to {', '.join(links)}",
    )
    check(
        "itemised_data",
        "Rules 2025, Rule 3(b)(i)",
        "An itemised description of the personal data processed",
        [
            f"{loc}: {p.id} lists no personal data"
            for loc, c in translations.items()
            for p in c.purposes
            if not p.data_items
        ],
        "every purpose lists its personal data items",
    )
    consent_ids = {p.id for p in kit.registry.consent_purposes()}
    missing = [
        f"{loc}: {pid} not itemised"
        for loc, c in translations.items()
        for pid in sorted(consent_ids - {p.id for p in c.purposes})
    ]
    missing += [
        f"{loc}: {p.id} has no description"
        for loc, c in translations.items()
        for p in c.purposes
        if not p.description.strip()
    ]
    check(
        "itemised_purposes",
        "DPDP Act s.5(1)(i); Rules 2025, Rule 3(b)(ii)",
        "Each specified purpose, with a description of the goods, services or uses it enables",
        missing,
        "every consent purpose is itemised with a description",
    )
    unshipped = [lang for lang in kit.policy.languages if lang not in translations]
    check(
        "languages",
        "DPDP Act s.5(3)",
        "The principal can read the notice in English or an Eighth Schedule language",
        [f"the policy pack ships {', '.join(kit.policy.languages)}; the notice has no {', '.join(unshipped)}"]
        if unshipped
        else [],
        f"notice provided in {', '.join(_name(loc) for loc in sorted(translations))}",
        bad="warn",
    )
    untranslated: list[str] = []
    if base is not None:
        for loc, content in sorted(translations.items()):
            if loc != default:
                untranslated += [f"{loc}: {where}" for where in _same_text(base, content)]
    check(
        "translation",
        "DPDP Act s.5(3); Rules 2025, Rule 3(a)",
        f"Every translation is complete: no text falls back to {_name(default)}",
        untranslated,
        "every translated locale has its own text for the title, body, purposes and data items"
        if len(translations) > 1
        else "only one locale",
    )
    contact = kit.contact
    check(
        "contact",
        "DPDP Act s.8(9); Rules 2025, Rule 9",
        "Contact details of the person who answers questions about processing",
        [f"contact has no {key}" for key in ("name", "email") if not str(contact.get(key, "")).strip()],
        ", ".join(f"{k}: {v}" for k, v in contact.items()),
    )
    placeholders = sorted(
        {f"{where}: {value}" for where, value in _values(base, contact) if any(m in value for m in PLACEHOLDER_MARKERS)}
    )
    check(
        "placeholders",
        "dpdpkit",
        "No example names, addresses or links left from a starter file or template",
        placeholders,
        "no example values found",
        bad="warn",
    )

    def counsel(id: str, source: str, question: str, detail: str = "") -> None:
        checks.append(NoticeCheck(id, source, question, "counsel", detail))

    counsel(
        "standalone",
        "Rules 2025, Rule 3(a)",
        "Is the notice presented on its own, apart from terms of service or marketing, and in clear and plain "
        "language the principal can understand without other information?",
    )
    counsel(
        "informed_consent",
        "DPDP Act s.6(1); Rules 2025, Rule 3(b)",
        "Does each purpose description give a fair account of what it enables, so that consent is free, specific, "
        "informed and unambiguous?",
    )
    required = [p.id for p in kit.registry.consent_purposes() if p.required]
    counsel(
        "necessity",
        "DPDP Act s.6(1)",
        "Is each purpose marked required really necessary for the service? Consent is limited to the personal data "
        "necessary for the specified purpose.",
        f"required: {', '.join(required)}" if required else "no purpose is marked required",
    )
    others = [p for p in kit.registry.purposes() if p.legal_basis is not LegalBasis.CONSENT]
    counsel(
        "legal_basis",
        "DPDP Act s.4, s.7",
        "Is the stated legitimate use right for each purpose that does not rely on consent, and is it described "
        "accurately in the notice?",
        ", ".join(f"{p.id}: {p.legal_basis.value}" for p in others) if others else "every purpose relies on consent",
    )
    counsel(
        "withdrawal",
        "DPDP Act s.6(4)-(6); Rules 2025, Rule 3(c)(i)",
        "Does the notice describe how to withdraw consent with ease comparable to giving it, and what withdrawal "
        "means for the service?",
    )
    counsel(
        "children",
        "DPDP Act s.9; Rules 2025, Rule 10",
        "If children or persons with a disability who have a lawful guardian may use the service, is the notice "
        "addressed to the parent or guardian, and is tracking or targeted advertising directed at children excluded?",
    )
    counsel(
        "existing_principals",
        "DPDP Act s.5(2)",
        "Principals who consented before the Act commenced must get this notice as soon as reasonably practicable. "
        "Is there a plan to send it to them?",
    )
    if len(translations) > 1:
        counsel(
            "translation_fidelity",
            "DPDP Act s.5(3)",
            f"Does each translation say the same as the {_name(default)} text? Have a native speaker check it.",
            ", ".join(_name(loc) for loc in sorted(translations) if loc != default),
        )
    return checks


def review_pack(
    config: Mapping[str, Any],
    *,
    source: str = "dpdpkit.yaml",
    notes: str = "",
    prepared_by: str = "",
    today: dt.date | None = None,
) -> str:
    """The review pack (Markdown) for the notice in ``config``: summary, sign-off with content hashes, checks and
    the full notice in every locale. ``notes`` is shown as context for the reviewer (for example the comment block
    at the top of a purpose template)."""
    kit, translations = notice_translations_for(config)
    checks = notice_checks(kit, translations)
    hashes = notice_hashes(translations)
    default = kit.policy.default_language
    automated = [c for c in checks if c.status != "counsel"]
    counts = {s: sum(c.status == s for c in checks) for s in CHECK_STATUSES}
    summary = [
        f"{len(kit.registry.purposes())} purpose(s), {len(kit.registry.consent_purposes())} relying on consent, "
        f"in {len(translations)} locale(s): {', '.join(_locale(loc) for loc in sorted(translations))}.",
        f"Automated checks: {counts['pass']} passed, {counts['fail']} failed, {counts['warn']} to fix before go-live.",
        f"{counts['counsel']} question(s) for counsel.",
    ]
    if counts["fail"]:
        summary.append("Fix the failed checks before the review: the wording would change after sign-off.")
    template = Template((resources.files(__name__) / "notice-review.md").read_text(encoding="utf-8"))
    return template.substitute(
        title=translations[default].title,
        source=_md(source),
        policy=kit.policy.id,
        date=(today or dt.date.today()).isoformat(),
        prepared_by=_md(prepared_by) or "—",
        summary="\n".join(f"- {line}" for line in summary),
        hashes=_table(["Locale", "Content hash (SHA-256)"], [[_locale(loc), f"`{h}`"] for loc, h in hashes.items()]),
        automated=_table(
            ["Check", "Result", "Requirement", "Source", "Detail"],
            [[f"`{c.id}`", c.status.upper(), c.requirement, c.source, c.detail] for c in automated],
        ),
        questions="\n".join(
            f"{n}. **{c.requirement}** ({c.source})" + (f"<br>\n   _{_md(c.detail)}_" if c.detail else "")
            for n, c in enumerate((c for c in checks if c.status == "counsel"), start=1)
        ),
        notes=_quote(notes) if notes.strip() else "_None._",
        notices="\n\n".join(
            _render(loc, translations[loc], hashes[loc], kit) for loc in _ordered(translations, default)
        ),
    )


# ---------------------------------------------------------------------- helpers


def _name(locale: str) -> str:
    name = _LOCALE_NAMES.get(locale)
    return f"{name} ({locale})" if name else locale


def _locale(locale: str) -> str:
    name = _LOCALE_NAMES.get(locale)
    return f"{name} (`{locale}`)" if name else f"`{locale}`"


def _ordered(translations: Mapping[str, NoticeContent], default: str) -> list[str]:
    return sorted(translations, key=lambda loc: (loc != default, loc))


def _same_text(base: NoticeContent, other: NoticeContent) -> list[str]:
    """Places where ``other`` shows the same text as ``base``: what a missing translation falls back to."""
    found = [
        field for field in ("title", "body") if getattr(base, field) and getattr(base, field) == getattr(other, field)
    ]
    theirs = {p.id: p for p in other.purposes}
    for mine in base.purposes:
        their = theirs.get(mine.id)
        if their is None:
            continue
        found += [
            f"{mine.id} {f}"
            for f in ("title", "description")
            if getattr(mine, f) and getattr(mine, f) == getattr(their, f)
        ]
        found += [f"data item '{name}'" for name in mine.data_items if name in their.data_items]
    return list(dict.fromkeys(found))


def _values(content: NoticeContent | None, contact: Mapping[str, str]) -> Iterable[tuple[str, str]]:
    yield from ((f"contact {k}", str(v)) for k, v in contact.items())
    if content is not None:
        yield from ((f"{k} link", v) for k, v in content.links.items())
        yield "title", content.title
        yield "body", content.body
        for p in content.purposes:
            yield f"{p.id} description", p.description


def _render(locale: str, content: NoticeContent, digest: str, kit: Kit) -> str:
    lines = [f"### {_locale(locale)}", "", f"Content hash: `{digest}`", "", f"#### {content.title}", ""]
    if content.body:
        lines += [content.body, ""]
    for n, p in enumerate(content.purposes, start=1):
        basis = "consent" if p.legal_basis is LegalBasis.CONSENT else f"legitimate use ({p.legal_basis.value})"
        kind = "required" if p.required else "optional" if p.legal_basis is LegalBasis.CONSENT else "no consent asked"
        lines.append(f"{n}. **{p.title}** · `{p.id}` · {kind} · {basis}")
        if p.description:
            lines.append(f"   - {p.description}")
        lines.append(f"   - _Personal data:_ {', '.join(p.data_items)}")
        processors = kit.registry.processors_for(p.id) if kit.registry.has_purpose(p.id) else []
        if processors:
            names = ", ".join(proc.name for proc in processors)
            lines.append(f"   - _Processors (not part of the published notice):_ {names}")
        lines.append("")
    lines += ["**Links**", ""] + [f"- {name}: <{url}>" for name, url in content.links.items()]
    if content.contact:
        lines += ["", "**Contact**", ""] + [f"- {k}: {v}" for k, v in content.contact.items()]
    return "\n".join(lines)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}".rstrip() for line in text.strip().splitlines())


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(_md(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)
