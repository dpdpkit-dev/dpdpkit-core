"""Industry purpose templates: complete starter ``dpdpkit.yaml`` files for common sectors.

Each template lists the data items, processors, purposes, retention rules and an itemised notice (English and
Hindi) that a typical business in the sector starts from. They are a starting point for the data-mapping
workshop: every purpose, legal basis, retention period and line of notice wording is an assumption for you
and your counsel to confirm. ``dpdpkit init --template ID`` writes one to disk.

Each template's notice wording goes through a lawyer review, recorded in ``reviews.yaml`` next to the templates
and bound to the notice's content hashes: :meth:`PurposeTemplate.notice_review` reports whether the shipped
wording is reviewed, still awaiting review, or changed since it was reviewed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import yaml

from ..errors import ConfigError

__all__ = ["NoticeReview", "PurposeTemplate", "list_templates", "load_template"]

_TITLES = {
    "edtech": ("Ed-tech", "Learning apps, coaching and school platforms; learners under 18 and their parents"),
    "health": ("Health", "Clinics, telemedicine and diagnostics; health records marked sensitive"),
    "ecommerce": ("E-commerce", "D2C brands and marketplaces; orders, invoices, Third Schedule note"),
    "fintech": ("Fintech", "Payments, lending and wallets; KYC and transaction records kept under law"),
    "saas": ("SaaS", "B2B software; your users, billing and marketing, not your customers' data"),
}


REVIEW_STATUSES = ("pending", "reviewed")


@dataclass(frozen=True)
class NoticeReview:
    """The lawyer review of a template's notice wording."""

    status: str
    """``pending`` (not reviewed yet), ``reviewed`` (approved, and the wording is unchanged since) or ``stale``
    (approved, but the wording has changed since, so the approval no longer covers it)."""
    reviewer: str = ""
    firm: str = ""
    date: str = ""
    scope: str = ""
    hashes: Mapping[str, str] = field(default_factory=dict)
    """Notice content hash per locale of the wording that was approved."""

    def describe(self) -> str:
        if self.status == "pending":
            return "notice wording awaiting lawyer review"
        by = ", ".join(part for part in (self.reviewer, self.firm, self.date) if part)
        if self.status == "stale":
            return f"notice wording changed since its review ({by}); not covered by that review"
        return f"notice wording reviewed ({by})"


@dataclass(frozen=True)
class PurposeTemplate:
    id: str
    title: str
    summary: str
    text: str
    """The YAML file, comments included, as written by ``dpdpkit init --template``."""

    def config(self) -> dict[str, Any]:
        """The parsed configuration, ready for :func:`~dpdpkit.kit_from_config`."""
        data: dict[str, Any] = yaml.safe_load(self.text)
        return data

    def notice_hashes(self) -> dict[str, str]:
        """Content hash per locale of the notice this template publishes."""
        from ..review import notice_hashes, notice_translations_for

        return notice_hashes(notice_translations_for(self.config())[1])

    def notice_review(self) -> NoticeReview:
        """The recorded lawyer review of this template's notice, checked against the current wording."""
        entry = _reviews().get(self.id) or {"status": "pending"}
        status = entry.get("status")
        if status not in REVIEW_STATUSES:
            raise ConfigError(
                f"reviews.yaml: {self.id} has status {status!r} (use one of: {', '.join(REVIEW_STATUSES)})"
            )
        hashes = {str(k): str(v) for k, v in (entry.get("hashes") or {}).items()}
        if status == "reviewed":
            if not hashes or not entry.get("reviewer"):
                raise ConfigError(f"reviews.yaml: reviewed template {self.id} needs a reviewer and the approved hashes")
            if hashes != self.notice_hashes():
                status = "stale"
        return NoticeReview(
            status=str(status),
            reviewer=str(entry.get("reviewer", "")),
            firm=str(entry.get("firm", "")),
            date=str(entry.get("date", "")),
            scope=str(entry.get("scope", "")),
            hashes=hashes,
        )

    def notes(self) -> str:
        """The comment block at the top of the file: the sector notes and questions for counsel."""
        return _leading_comments(self.text)


def list_templates() -> list[PurposeTemplate]:
    """Every shipped template, in a stable order."""
    return [load_template(template_id) for template_id in _TITLES]


def load_template(template_id: str) -> PurposeTemplate:
    try:
        title, summary = _TITLES[template_id]
    except KeyError:
        known = ", ".join(_TITLES)
        raise ConfigError(f"unknown purpose template {template_id!r} (choose from: {known})") from None
    text = (resources.files(__name__) / f"{template_id}.yaml").read_text(encoding="utf-8")
    return PurposeTemplate(id=template_id, title=title, summary=summary, text=text)


def _reviews() -> dict[str, Any]:
    data = yaml.safe_load((resources.files(__name__) / "reviews.yaml").read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _leading_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[1:].removeprefix(" "))
    return "\n".join(lines).strip()
