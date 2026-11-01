"""Industry purpose templates: complete starter ``dpdpkit.yaml`` files for common sectors.

Each template lists the data items, processors, purposes, retention rules and an itemised notice (English and
Hindi) that a typical business in the sector starts from. They are a starting point for the data-mapping
workshop: every purpose, legal basis, retention period and line of notice wording is an assumption for you
and your counsel to confirm. ``dpdpkit init --template ID`` writes one to disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from ..errors import ConfigError

__all__ = ["PurposeTemplate", "list_templates", "load_template"]

_TITLES = {
    "edtech": ("Ed-tech", "Learning apps, coaching and school platforms; learners under 18 and their parents"),
    "health": ("Health", "Clinics, telemedicine and diagnostics; health records marked sensitive"),
    "ecommerce": ("E-commerce", "D2C brands and marketplaces; orders, invoices, Third Schedule note"),
    "fintech": ("Fintech", "Payments, lending and wallets; KYC and transaction records kept under law"),
    "saas": ("SaaS", "B2B software; your users, billing and marketing, not your customers' data"),
}


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
