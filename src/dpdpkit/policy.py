"""Load and query a policy pack. Core reads every legal number from here and nowhere else.

A missing or invalid pack raises :class:`~dpdpkit.errors.PolicyError`. There are no fallback defaults.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dpdpkit_policies as _packs

from .errors import PolicyError

_MISSING = object()


@dataclass(frozen=True)
class PolicyChange:
    key: str
    old: Any
    new: Any


class Policy:
    """An immutable, validated policy pack (with any overlays already merged)."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        try:
            _packs.validate_pack(data, str(data.get("id", "<pack>")))
        except _packs.PolicyValidationError as exc:
            raise PolicyError(str(exc), errors=exc.errors) from exc
        self._data: dict[str, Any] = copy.deepcopy(dict(data))

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, pack_id: str, overlays: Iterable[str] = ()) -> Policy:
        try:
            return cls(_packs.load_pack(pack_id, overlays))
        except (_packs.UnknownPolicyError, _packs.PolicyValidationError) as exc:
            raise PolicyError(str(exc)) from exc

    @classmethod
    def from_file(cls, path: str | Path, overlays: Iterable[str] = ()) -> Policy:
        try:
            data = _packs.read_policy_file(path)
            overlay_ids = list(overlays)
            if overlay_ids:
                data = _packs.merge_overlays(data, [_packs.load_overlay(o) for o in overlay_ids])
        except (OSError, _packs.UnknownPolicyError, _packs.PolicyValidationError) as exc:
            raise PolicyError(f"cannot load policy file {path}: {exc}") from exc
        return cls(data)

    # ------------------------------------------------------------------ raw access

    @property
    def id(self) -> str:
        return str(self._data["id"])

    @property
    def overlays(self) -> list[str]:
        return list(self._data.get("overlays", []))

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is _MISSING:
                    raise PolicyError(f"policy {self.id} has no value for {dotted!r}")
                return default
            node = node[part]
        return copy.deepcopy(node)

    # ------------------------------------------------------------------ typed accessors

    @property
    def erasure_pre_notice(self) -> dt.timedelta:
        return dt.timedelta(hours=int(self.get("retention.erasure_pre_notice_hours")))

    @property
    def min_log_retention(self) -> dt.timedelta:
        return dt.timedelta(days=int(self.get("retention.min_log_retention_days")))

    @property
    def grievance_ceiling(self) -> dt.timedelta:
        return dt.timedelta(days=int(self.get("rights.grievance_max_days")))

    @property
    def request_ceiling(self) -> dt.timedelta:
        days = self.get("rights.request_max_days", None)
        if days is None:
            days = self.get("rights.grievance_max_days")
        return dt.timedelta(days=int(days))

    @property
    def request_target(self) -> dt.timedelta:
        return dt.timedelta(days=int(self.get("rights.request_default_target_days")))

    @property
    def required_links(self) -> list[str]:
        return list(self.get("notice.required_links"))

    @property
    def languages(self) -> list[str]:
        return list(self.get("notice.languages"))

    @property
    def default_language(self) -> str:
        return str(self.get("notice.default_language", self.languages[0]))

    @property
    def age_of_majority(self) -> int:
        return int(self.get("children.age_of_majority"))

    @property
    def board_report_window(self) -> dt.timedelta:
        return dt.timedelta(hours=int(self.get("breach.board_detailed_report_hours")))

    @property
    def fiduciary_duties_start(self) -> dt.date:
        return dt.date.fromisoformat(str(self.get("commencement.fiduciary_duties")))

    def third_schedule(self, fiduciary_class: str) -> tuple[int, dt.timedelta]:
        """``(min_users, inactivity_period)`` for a Third Schedule class such as ``ecommerce``."""
        entry = self.get(f"retention.third_schedule.{fiduciary_class}")
        years = int(entry["inactivity_years"])
        # Calendar years, counted in days; 365.25 accounts for leap years across a multi-year window.
        return int(entry["min_users"]), dt.timedelta(days=round(365.25 * years))

    # ------------------------------------------------------------------ comparison

    def diff(self, other: Policy) -> list[PolicyChange]:
        """Leaf-level differences from ``self`` to ``other``."""
        changes: list[PolicyChange] = []
        old = dict(_flatten(self._data))
        new = dict(_flatten(other._data))
        for key in sorted(old.keys() | new.keys()):
            if old.get(key, _MISSING) != new.get(key, _MISSING):
                changes.append(
                    PolicyChange(key, old.get(key), new.get(key)),
                )
        return changes

    def __repr__(self) -> str:
        return f"Policy({self.id!r}, overlays={self.overlays!r})"


def _flatten(data: Mapping[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            yield from _flatten(value, path + ".")
        else:
            yield path, value


def available_packs() -> list[str]:
    return _packs.list_packs()
