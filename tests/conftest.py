from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
import yaml

from dpdpkit import Contact, InMemoryRepository, Kit, MemoryNotifier, kit_from_config, sync_notice
from dpdpkit.config import STARTER_CONFIG

T0 = dt.datetime(2027, 6, 1, 9, 0, tzinfo=dt.timezone.utc)


class FakeClock:
    def __init__(self, start: dt.datetime = T0) -> None:
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kwargs: float) -> dt.datetime:
        self.now += dt.timedelta(**kwargs)
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def config() -> dict[str, Any]:
    cfg = yaml.safe_load(STARTER_CONFIG)
    cfg["processors"].append({"id": "crm", "name": "CRM vendor", "country": "IN"})
    cfg["purposes"].append(
        {
            "id": "analytics",
            "title": "Improve the product",
            "data_items": ["order_history"],
            "processors": ["crm"],
            "retention": {"trigger": "last_activity", "period_days": 30},
        }
    )
    cfg["purposes"].append({"id": "billing", "title": "Tax invoices", "data_items": ["name"], "legal_basis": "s7_d"})
    return cfg


@pytest.fixture
def notifier() -> MemoryNotifier:
    return MemoryNotifier()


@pytest.fixture
def contacts() -> dict[str, Contact]:
    return {"u1": Contact(email="u1@example.in"), "u2": Contact(phone="+919800000002")}


@pytest.fixture
def make_kit(
    clock: FakeClock, config: dict[str, Any], notifier: MemoryNotifier, contacts: dict[str, Contact]
) -> Callable[..., Kit]:
    def factory(**kwargs: Any) -> Kit:
        kwargs.setdefault("notifier", notifier)
        kwargs.setdefault("clock", clock)
        kwargs.setdefault("signing_key", "test-signing-key")
        kwargs.setdefault("contact_resolver", contacts.get)
        repo = kwargs.pop("repository", None) or InMemoryRepository()
        kit = kit_from_config(kwargs.pop("config", config), repo, **kwargs)
        sync_notice(kit, config)
        return kit

    return factory


@pytest.fixture
def kit(make_kit: Callable[..., Kit]) -> Kit:
    return make_kit()


@pytest.fixture
def erased(kit: Kit) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []
    kit.retention.register_handler(lambda principal, purposes: calls.append((principal, purposes)))
    return calls
