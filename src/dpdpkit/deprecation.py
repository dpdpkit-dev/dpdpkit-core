"""Deprecation helpers used by every dpdpkit package (see "Deprecation policy" in the docs).

A deprecated API keeps working for at least two minor releases (and at least 90 days) after the release
that deprecates it, warns with :class:`DpdpkitDeprecationWarning` every time it is used, and is listed
under "Deprecated" in the changelog.

Functions, methods and classes are marked with PEP 702 so type checkers report callers too. The message
must be a string literal (mypy ignores computed messages) in the standard wording::

    from typing_extensions import deprecated

    @deprecated(
        "Ledger.export_roots() is deprecated since dpdpkit 0.3 and will be removed in 0.5; "
        "use Ledger.export_root() instead",
        category=DpdpkitDeprecationWarning,
    )
    def export_roots(self): ...

Parameters, config keys and renamed module attributes use :func:`warn_deprecated` and :func:`moved`.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import Any

MESSAGE = re.compile(
    r"^(?P<name>\S.*) is deprecated since dpdpkit (?P<since>\d+\.\d+) and will be removed in "
    r"(?P<removed_in>\d+\.\d+)(?:; use (?P<use>.+) instead)?$"
)
"""The standard wording; ``tests/test_deprecation.py`` checks every ``@deprecated`` in the source against it."""


class DpdpkitDeprecationWarning(DeprecationWarning):
    """Something you use will be removed in a later dpdpkit release. The message says what replaces it.

    Python hides ``DeprecationWarning`` outside ``__main__`` and test runners; to see these everywhere,
    run with ``-W always::dpdpkit.DpdpkitDeprecationWarning``.
    """


def deprecation_message(name: str, *, since: str, removed_in: str, use: str | None = None) -> str:
    text = f"{name} is deprecated since dpdpkit {since} and will be removed in {removed_in}"
    return f"{text}; use {use} instead" if use else text


def warn_deprecated(name: str, *, since: str, removed_in: str, use: str | None = None, stacklevel: int = 3) -> None:
    """Warn about a deprecated parameter, config key or value from inside the code that still accepts it.

    ``stacklevel=3`` points at the caller of the function that calls this helper.
    """
    message = deprecation_message(name, since=since, removed_in=removed_in, use=use)
    warnings.warn(message, DpdpkitDeprecationWarning, stacklevel=stacklevel)


def moved(module_name: str, renames: dict[str, tuple[str, Any, str, str]]) -> Callable[[str], Any]:
    """Build a module ``__getattr__`` that keeps old names importable with a warning.

    ``renames`` maps an old attribute name to ``(new dotted name, object, since, removed_in)``::

        __getattr__ = moved(__name__, {"OldName": ("dpdpkit.models.NewName", NewName, "0.3", "0.5")})
    """

    def __getattr__(attr: str) -> Any:
        if attr in renames:
            new_name, obj, since, removed_in = renames[attr]
            warn_deprecated(f"{module_name}.{attr}", since=since, removed_in=removed_in, use=new_name, stacklevel=3)
            return obj
        raise AttributeError(f"module {module_name!r} has no attribute {attr!r}")

    return __getattr__
