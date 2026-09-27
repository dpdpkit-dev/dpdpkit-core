"""``dpdpkit.yaml``: purposes, notice text, contact details and policy selection.

Adapters and the server build a :class:`~dpdpkit.Kit` from this file and call :func:`sync_notice`
at start-up, which publishes a new notice version only when the configured text changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, NotFound
from .kit import Kit
from .models import Notice, NoticeContent
from .notices import content_hash
from .registry import Registry
from .repository import Repository

STARTER_CONFIG = """\
# dpdpkit configuration.
# Purposes, notice wording and retention periods are decisions for you and your counsel.
# dpdpkit records and enforces what you configure; it does not decide what is lawful.
policy: dpdp-rules-2025.v1
overlays: []

contact:
  name: Example Pvt Ltd
  email: privacy@example.in
  rights_url: https://example.in/privacy/rights

data_items:
  - {id: email, name: Email address}
  - {id: phone, name: Mobile number}
  - {id: name, name: Full name}
  - {id: order_history, name: Order history}

processors:
  - {id: mailer, name: Email delivery provider, country: IN}

purposes:
  - id: account
    title: Create and run your account
    description: We use these details to sign you in and provide the service.
    data_items: [email, phone, name, order_history]
    required: true
    retention: {trigger: last_activity, period_days: 1095}
  - id: marketing
    title: Send you offers
    description: Occasional emails about products and discounts.
    data_items: [email]
    processors: [mailer]
    retention: {trigger: withdrawal, period_days: 0}

notice:
  title: How we use your personal data
  body: Choose what you agree to. You can change your choices at any time.
  links:
    withdraw: https://example.in/privacy/preferences
    rights: https://example.in/privacy/rights
    board_complaint: https://example.in/privacy/board-complaint   # replace with the Board's complaint page
  translations:
    hi:
      title: हम आपके व्यक्तिगत डेटा का उपयोग कैसे करते हैं
      body: चुनें कि आप किस बात से सहमत हैं। आप कभी भी अपनी पसंद बदल सकते हैं।
      purposes:
        account: {title: आपका खाता बनाना और चलाना}
        marketing: {title: आपको ऑफ़र भेजना}
"""


def load_config(path: str | Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    if "policy" not in data:
        raise ConfigError(f"{path} must name a policy pack (policy: dpdp-rules-2025.v1)")
    return data


def kit_from_config(config: Mapping[str, Any] | str | Path, repository: Repository, **kwargs: Any) -> Kit:
    """Build a :class:`Kit` from a config mapping or file. ``kwargs`` override or extend Kit arguments."""
    cfg = load_config(config) if isinstance(config, (str, Path)) else dict(config)
    tenant_id = str(kwargs.pop("tenant_id", cfg.get("tenant_id", "default")))
    return Kit(
        repository=repository,
        policy=str(cfg["policy"]),
        overlays=list(cfg.get("overlays") or []),
        registry=kwargs.pop("registry", None) or Registry.from_config(cfg, tenant_id),
        contact=kwargs.pop("contact", None) or cfg.get("contact") or {},
        tenant_id=tenant_id,
        **kwargs,
    )


def notice_translations(kit: Kit, notice_cfg: Mapping[str, Any]) -> dict[str, NoticeContent]:
    try:
        links = dict(notice_cfg["links"])
        title = str(notice_cfg["title"])
    except KeyError as exc:
        raise ConfigError(f"notice config is missing {exc}") from exc
    body = str(notice_cfg.get("body", ""))
    default = kit.policy.default_language
    out = {default: kit.notices.build_content(title=title, body=body, links=links)}
    for locale, text in (notice_cfg.get("translations") or {}).items():
        out[str(locale)] = kit.notices.build_content(
            title=str(text.get("title", title)),
            body=str(text.get("body", body)),
            links={**links, **(text.get("links") or {})},
            purposes=text.get("purposes") or {},
        )
    return out


def sync_notice(kit: Kit, config: Mapping[str, Any], *, actor: str = "config") -> list[Notice] | None:
    """Publish the configured notice if it differs from the current one. Returns the new notices or None."""
    notice_cfg = config.get("notice")
    if not notice_cfg:
        return None
    wanted = notice_translations(kit, notice_cfg)
    try:
        current = kit.notices.current()
        existing = {
            n.locale: n.content_hash for n in kit.repository.list_notices(kit.tenant_id, version=current.version)
        }
    except NotFound:
        existing = {}
    if existing == {loc: content_hash(c) for loc, c in wanted.items()}:
        return None
    return kit.notices.publish(wanted, actor=actor)
