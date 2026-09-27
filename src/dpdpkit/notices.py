"""Versioned, itemised notices with a content hash, locale fallback and a required-link check."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ._util import canonical_json, sha256_hex
from .errors import NotFound, NoticeInvalid
from .events import NOTICE_PUBLISHED
from .models import Notice, NoticeContent, NoticePurpose

if TYPE_CHECKING:
    from .kit import Kit


def content_hash(content: NoticeContent) -> str:
    return sha256_hex(canonical_json(content.model_dump(mode="json")))


class NoticeService:
    def __init__(self, kit: Kit) -> None:
        self._kit = kit

    # ------------------------------------------------------------------ building

    def build_content(
        self,
        *,
        title: str,
        links: Mapping[str, str],
        body: str = "",
        purposes: Mapping[str, Mapping[str, str]] | None = None,
    ) -> NoticeContent:
        """Itemise every registered purpose. ``purposes`` overrides title/description per purpose
        (for translations); data item names come from the registry."""
        overrides = purposes or {}
        items: list[NoticePurpose] = []
        for purpose in self._kit.registry.purposes():
            text = overrides.get(purpose.id, {})
            items.append(
                NoticePurpose(
                    id=purpose.id,
                    title=text.get("title", purpose.title),
                    description=text.get("description", purpose.description),
                    data_items=[self._kit.registry.data_item(i).name for i in purpose.data_items],
                    legal_basis=purpose.legal_basis,
                    required=purpose.required,
                )
            )
        return NoticeContent(
            title=title,
            body=body,
            purposes=items,
            links=dict(links),
            contact=dict(self._kit.contact),
        )

    def problems(self, content: NoticeContent) -> list[str]:
        """Everything that stops this content from being published under the active policy."""
        issues = [
            f"missing required link: {link}"
            for link in self._kit.policy.required_links
            if not content.links.get(link, "").strip()
        ]
        listed = {p.id for p in content.purposes}
        for purpose in self._kit.registry.consent_purposes():
            if purpose.id not in listed:
                issues.append(f"consent purpose not itemised: {purpose.id}")
        for pid in listed:
            if not self._kit.registry.has_purpose(pid):
                issues.append(f"unknown purpose in notice: {pid}")
        for p in content.purposes:
            if not p.data_items:
                issues.append(f"purpose {p.id} lists no personal data items")
        return issues

    # ------------------------------------------------------------------ publishing

    def publish(self, translations: Mapping[str, NoticeContent], *, actor: str = "system") -> list[Notice]:
        """Publish a new notice version with one content per locale.

        The policy's default language must be present. Every locale must pass :meth:`problems`.
        """
        if not translations:
            raise NoticeInvalid("a notice needs at least one locale")
        default = self._kit.policy.default_language
        if default not in translations:
            raise NoticeInvalid(f"notice must include the default language {default!r}", locale=default)
        problems = {loc: self.problems(c) for loc, c in translations.items()}
        problems = {loc: p for loc, p in problems.items() if p}
        if problems:
            raise NoticeInvalid("notice failed validation", problems=problems)

        existing = self._kit.repository.list_notices(self._kit.tenant_id)
        version = max((n.version for n in existing), default=0) + 1
        now = self._kit.now()
        notices = [
            Notice(
                tenant_id=self._kit.tenant_id,
                version=version,
                locale=locale,
                content=content,
                content_hash=content_hash(content),
                published_at=now,
            )
            for locale, content in sorted(translations.items())
        ]
        with self._kit.repository.transaction():
            self._kit.ledger.audit(
                "notice.published",
                subject_type="notice",
                subject_id=str(version),
                actor=actor,
                data={"locales": {n.locale: n.content_hash for n in notices}},
            )
            for notice in notices:
                self._kit.repository.save_notice(notice)
        self._kit.events.publish(
            NOTICE_PUBLISHED, self._kit.tenant_id, {"version": version, "locales": sorted(translations)}, now
        )
        return notices

    # ------------------------------------------------------------------ reading

    def _pick(self, notices: list[Notice], locale: str | None) -> Notice:
        by_locale = {n.locale: n for n in notices}
        chain = []
        if locale:
            chain += [locale, locale.split("-")[0]]
        chain.append(self._kit.policy.default_language)
        for candidate in chain:
            if candidate in by_locale:
                return by_locale[candidate]
        return notices[0]

    def current(self, locale: str | None = None) -> Notice:
        notices = self._kit.repository.list_notices(self._kit.tenant_id)
        if not notices:
            raise NotFound("no notice has been published")
        latest = max(n.version for n in notices)
        return self._pick([n for n in notices if n.version == latest], locale)

    def get(self, version: int, locale: str | None = None) -> Notice:
        notices = self._kit.repository.list_notices(self._kit.tenant_id, version=version)
        if not notices:
            raise NotFound(f"notice version {version} does not exist", version=version)
        return self._pick(notices, locale)

    def locales(self, version: int | None = None) -> list[str]:
        v = version if version is not None else self.current().version
        return sorted(n.locale for n in self._kit.repository.list_notices(self._kit.tenant_id, version=v))

    def history(self) -> list[Notice]:
        return self._kit.repository.list_notices(self._kit.tenant_id)
