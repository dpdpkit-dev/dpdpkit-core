"""Small helpers shared by every core module. Not public API."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from typing import Any

Clock = Callable[[], dt.datetime]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def as_utc(value: dt.datetime) -> dt.datetime:
    """Treat naive datetimes as UTC (some databases drop tzinfo) and convert aware ones to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical_default(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        # Fixed format so a value read back from any database hashes identically.
        return as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if hasattr(value, "value"):  # enums
        return value.value
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(data: Any) -> bytes:
    """Deterministic JSON used for hashing and signing."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_canonical_default
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256_hex(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def mask_address(value: str) -> str:
    """Mask an email address or phone number for receipts and logs."""
    if "@" in value:
        local, _, domain = value.partition("@")
        return f"{local[:1]}***@{domain}"
    digits = value.strip()
    return "*" * max(len(digits) - 4, 0) + digits[-4:]
