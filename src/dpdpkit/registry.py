"""Purposes, data items, legal basis, processors and data stores for one fiduciary."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .errors import ConfigError, NotFound
from .models import DataItem, DataStore, LegalBasis, Processor, Purpose


class Registry:
    def __init__(
        self,
        purposes: Iterable[Purpose] = (),
        data_items: Iterable[DataItem] = (),
        processors: Iterable[Processor] = (),
        data_stores: Iterable[DataStore] = (),
    ) -> None:
        self._purposes: dict[str, Purpose] = {}
        self._items: dict[str, DataItem] = {}
        self._processors: dict[str, Processor] = {}
        self._stores: dict[str, DataStore] = {}
        for item in data_items:
            self.add_data_item(item)
        for proc in processors:
            self.add_processor(proc)
        for store in data_stores:
            self.add_data_store(store)
        for purpose in purposes:
            self.add_purpose(purpose)

    # ------------------------------------------------------------------ building

    def add_data_item(self, item: DataItem) -> None:
        self._items[item.id] = item

    def add_processor(self, processor: Processor) -> None:
        self._processors[processor.id] = processor

    def add_data_store(self, store: DataStore) -> None:
        self._stores[store.id] = store

    def add_purpose(self, purpose: Purpose) -> None:
        for item in purpose.data_items:
            if item not in self._items:
                # Undeclared data items are registered with their id as name so notices stay itemised.
                self._items[item] = DataItem(id=item, name=item.replace("_", " "), tenant_id=purpose.tenant_id)
        for proc in purpose.processors:
            if proc not in self._processors:
                raise ConfigError(f"purpose {purpose.id!r} references unknown processor {proc!r}")
        for store in purpose.data_stores:
            if store not in self._stores:
                raise ConfigError(f"purpose {purpose.id!r} references unknown data store {store!r}")
        self._purposes[purpose.id] = purpose

    @classmethod
    def from_config(cls, config: Mapping[str, Any], tenant_id: str = "default") -> Registry:
        """Build from the ``purposes`` / ``data_items`` / ``processors`` / ``data_stores`` config keys."""

        def rows(key: str) -> list[dict[str, Any]]:
            value = config.get(key) or []
            if not isinstance(value, list):
                raise ConfigError(f"{key} must be a list")
            return [{"tenant_id": tenant_id, **row} for row in value]

        try:
            return cls(
                data_items=[DataItem(**r) for r in rows("data_items")],
                processors=[Processor(**r) for r in rows("processors")],
                data_stores=[DataStore(**r) for r in rows("data_stores")],
                purposes=[Purpose(**r) for r in rows("purposes")],
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid registry configuration: {exc}") from exc

    # ------------------------------------------------------------------ queries

    def purpose(self, purpose_id: str) -> Purpose:
        try:
            return self._purposes[purpose_id]
        except KeyError:
            raise NotFound(f"unknown purpose {purpose_id!r}", purpose=purpose_id) from None

    def has_purpose(self, purpose_id: str) -> bool:
        return purpose_id in self._purposes

    def purposes(self) -> list[Purpose]:
        return list(self._purposes.values())

    def consent_purposes(self) -> list[Purpose]:
        return [p for p in self._purposes.values() if p.legal_basis is LegalBasis.CONSENT]

    def data_item(self, item_id: str) -> DataItem:
        try:
            return self._items[item_id]
        except KeyError:
            raise NotFound(f"unknown data item {item_id!r}") from None

    def data_items(self) -> list[DataItem]:
        return list(self._items.values())

    def data_items_for(self, purpose_id: str) -> list[DataItem]:
        return [self._items[i] for i in self.purpose(purpose_id).data_items]

    def processors(self) -> list[Processor]:
        return list(self._processors.values())

    def processors_for(self, purpose_id: str) -> list[Processor]:
        return [self._processors[p] for p in self.purpose(purpose_id).processors]

    def data_stores(self) -> list[DataStore]:
        return list(self._stores.values())
