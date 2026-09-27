from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from dpdpkit import ConfigError, InMemoryRepository, Kit, NotFound, NoticeInvalid, Policy, PolicyError, Registry
from dpdpkit.config import sync_notice
from dpdpkit.notices import content_hash

BASE = "dpdp-rules-2025.v1"


def test_policy_accessors_come_from_pack() -> None:
    p = Policy.load(BASE)
    assert p.erasure_pre_notice == dt.timedelta(hours=48)
    assert p.min_log_retention == dt.timedelta(days=365)
    assert p.grievance_ceiling == dt.timedelta(days=90)
    assert p.request_target == dt.timedelta(days=30)
    assert p.age_of_majority == 18
    assert p.required_links == ["withdraw", "rights", "board_complaint"]
    min_users, period = p.third_schedule("ecommerce")
    assert min_users == 20_000_000 and period.days == 1096


def test_kit_refuses_to_start_without_valid_policy() -> None:
    with pytest.raises(PolicyError):
        Kit(InMemoryRepository(), "no-such-pack.v9", signing_key="k")
    with pytest.raises(PolicyError):
        Policy({"id": "broken.v1"})
    with pytest.raises(PolicyError):
        Policy.load(BASE).get("retention.not_a_key")


def test_policy_diff() -> None:
    old = Policy.load(BASE)
    data = old.as_dict()
    data["id"] = "dpdp-rules-2025.v2"
    data["retention"]["erasure_pre_notice_hours"] = 72
    changes = {c.key: (c.old, c.new) for c in old.diff(Policy(data))}
    assert changes == {"id": (BASE, "dpdp-rules-2025.v2"), "retention.erasure_pre_notice_hours": (48, 72)}


def test_registry_rejects_unknown_processor() -> None:
    with pytest.raises(ConfigError):
        Registry.from_config({"purposes": [{"id": "x", "title": "X", "processors": ["nope"]}]})
    with pytest.raises(NotFound):
        Registry().purpose("missing")


def test_notice_is_itemised_and_versioned(kit: Kit) -> None:
    notice = kit.notices.current()
    assert notice.version == 1
    ids = {p.id for p in notice.content.purposes}
    assert {"account", "marketing", "analytics", "billing"} <= ids
    marketing = next(p for p in notice.content.purposes if p.id == "marketing")
    assert marketing.data_items == ["Email address"]
    assert notice.content_hash == content_hash(notice.content)


def test_notice_locale_fallback(kit: Kit) -> None:
    assert kit.notices.current("hi").locale == "hi"
    assert kit.notices.current("hi-IN").locale == "hi"
    assert kit.notices.current("ta").locale == "en"
    assert kit.notices.locales() == ["en", "hi"]


def test_notice_requires_links_and_default_language(kit: Kit) -> None:
    content = kit.notices.build_content(title="t", links={"withdraw": "https://x", "rights": "https://y"})
    with pytest.raises(NoticeInvalid) as exc:
        kit.notices.publish({"en": content})
    assert "board_complaint" in str(exc.value.details)
    full = kit.notices.build_content(
        title="t", links={"withdraw": "https://x", "rights": "https://y", "board_complaint": "https://z"}
    )
    with pytest.raises(NoticeInvalid):
        kit.notices.publish({"hi": full})


def test_notice_must_itemise_every_consent_purpose(kit: Kit) -> None:
    content = kit.notices.build_content(
        title="t", links={"withdraw": "https://x", "rights": "https://y", "board_complaint": "https://z"}
    )
    content = content.model_copy(update={"purposes": [p for p in content.purposes if p.id != "marketing"]})
    assert "consent purpose not itemised: marketing" in kit.notices.problems(content)


def test_sync_notice_publishes_only_on_change(kit: Kit, config: dict[str, Any]) -> None:
    assert sync_notice(kit, config) is None
    config["notice"]["title"] = "Changed title"
    published = sync_notice(kit, config)
    assert published is not None and published[0].version == 2
    assert kit.notices.get(1).content.title != kit.notices.get(2).content.title
