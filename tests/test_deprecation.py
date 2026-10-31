from __future__ import annotations

import ast
import types
import warnings
from pathlib import Path

import pytest
from typing_extensions import deprecated

from dpdpkit import DpdpkitDeprecationWarning
from dpdpkit.deprecation import MESSAGE, deprecation_message, moved, warn_deprecated

SRC = Path(__file__).resolve().parents[1] / "src" / "dpdpkit"


@deprecated(
    "old_function() is deprecated since dpdpkit 0.3 and will be removed in 0.5; use new_function() instead",
    category=DpdpkitDeprecationWarning,
)
def old_function(x: int) -> int:
    return x * 2


class Service:
    def configure(self, *, timeout: int | None = None, old_timeout: int | None = None) -> int:
        if old_timeout is not None:
            warn_deprecated("Service.configure(old_timeout=)", since="0.3", removed_in="0.5", use="timeout=")
            timeout = old_timeout
        return timeout or 0


def test_pep702_marker_warns_at_the_call_site() -> None:
    with pytest.warns(DpdpkitDeprecationWarning, match=r"old_function\(\) is deprecated since dpdpkit 0.3") as rec:
        assert old_function(2) == 4
    assert rec[0].filename == __file__  # points at the caller, not at the library
    assert MESSAGE.match(str(rec[0].message))


def test_deprecated_parameter_warns_at_the_caller() -> None:
    with pytest.warns(DpdpkitDeprecationWarning, match=r"old_timeout=\) .* use timeout= instead") as rec:
        assert Service().configure(old_timeout=5) == 5
    assert rec[0].filename == __file__
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert Service().configure(timeout=3) == 3


def test_moved_names_stay_importable() -> None:
    module = types.ModuleType("fake_mod")
    module.__getattr__ = moved("fake_mod", {"OldName": ("dpdpkit.Kit", int, "0.3", "0.5")})  # type: ignore[attr-defined]
    with pytest.warns(DpdpkitDeprecationWarning, match="fake_mod.OldName .* use dpdpkit.Kit instead"):
        assert module.OldName is int
    with pytest.raises(AttributeError):
        _ = module.Missing


def test_message_format() -> None:
    assert MESSAGE.match(deprecation_message("a()", since="0.3", removed_in="0.5"))
    assert MESSAGE.match(deprecation_message("a()", since="0.3", removed_in="0.5", use="b()"))
    assert not MESSAGE.match("a() is going away soon")
    assert issubclass(DpdpkitDeprecationWarning, DeprecationWarning)


def test_every_deprecation_in_the_source_follows_the_policy() -> None:
    """Each @deprecated in dpdpkit uses a literal message in the standard wording and our warning class."""
    found = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "deprecated":
                continue
            where = f"{path.name}:{node.lineno}"
            found.append(where)
            assert node.args and isinstance(node.args[0], ast.Constant), f"{where}: message must be a literal"
            message = node.args[0].value
            assert isinstance(message, str) and MESSAGE.match(message), f"{where}: non-standard wording"
            category = {k.arg: k.value for k in node.keywords}.get("category")
            assert isinstance(category, ast.Name) and category.id == "DpdpkitDeprecationWarning", where
            since, removed = (MESSAGE.match(message) or {}).group("since", "removed_in")  # type: ignore[union-attr]
            major_minor = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731
            assert major_minor(removed) >= (major_minor(since)[0], major_minor(since)[1] + 2), (
                f"{where}: must survive at least two minor releases"
            )
    assert isinstance(found, list)  # none yet; the check guards future deprecations
