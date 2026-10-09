"""Versioned canonical command digests for idempotent mutations."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Final
from uuid import UUID

from spine.application.persistence.context import PersistenceOperation


COMMAND_DIGEST_ALGORITHM_VERSION: Final[str] = "spine.command-digest.v1"
_CANONICAL_REPRESENTATION_VERSION: Final[str] = "spine.canonical-command.v1"
_UTC_PRECISION: Final[str] = "microseconds"
_ALGORITHM_VERSION = re.compile(r"spine\.command-digest\.v([1-9][0-9]*)\Z")
_CURRENT_VERSION_NUMBER: Final[int] = int(COMMAND_DIGEST_ALGORITHM_VERSION.rsplit("v", 1)[1])


class UnsupportedCommandValueError(ValueError):
    """A command payload contains an unsupported or ambiguous value."""


@dataclass(frozen=True, slots=True)
class CommandDigest:
    """SHA-256 digest over a supported canonical command representation version."""

    algorithm_version: str
    value: str

    def __post_init__(self) -> None:
        version_match = (
            _ALGORITHM_VERSION.fullmatch(self.algorithm_version)
            if isinstance(self.algorithm_version, str)
            else None
        )
        if version_match is None or int(version_match.group(1)) > _CURRENT_VERSION_NUMBER:
            raise UnsupportedCommandValueError("Unsupported command digest algorithm.")
        if not isinstance(self.value, str) or len(self.value) != 64:
            raise UnsupportedCommandValueError("Command digest must be a SHA-256 hex value.")
        try:
            int(self.value, 16)
        except ValueError:
            raise UnsupportedCommandValueError("Command digest must be a SHA-256 hex value.") from None


def canonical_command_bytes(
    *,
    operation: PersistenceOperation,
    operation_schema_version: int,
    payload: Mapping[str, object],
) -> bytes:
    """Serialize a validated command payload into deterministic UTF-8 bytes."""

    if not isinstance(operation, PersistenceOperation):
        raise UnsupportedCommandValueError("Operation must be a persistence operation.")
    if (
        not isinstance(operation_schema_version, int)
        or isinstance(operation_schema_version, bool)
        or operation_schema_version <= 0
    ):
        raise UnsupportedCommandValueError("Operation schema version must be a positive integer.")
    canonical = {
        "canonical_version": _CANONICAL_REPRESENTATION_VERSION,
        "operation": operation.value,
        "operation_schema_version": operation_schema_version,
        "payload": _canonical_value(payload),
    }
    return json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest_command(
    *,
    operation: PersistenceOperation,
    operation_schema_version: int,
    payload: Mapping[str, object],
) -> CommandDigest:
    """Return the approved SHA-256 digest for a validated semantic command."""

    canonical = canonical_command_bytes(
        operation=operation,
        operation_schema_version=operation_schema_version,
        payload=payload,
    )
    return CommandDigest(
        algorithm_version=COMMAND_DIGEST_ALGORITHM_VERSION,
        value=hashlib.sha256(canonical).hexdigest(),
    )


def _canonical_value(value: object) -> Any:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, Enum):
        enum_value = value.value
        if not isinstance(enum_value, str):
            raise UnsupportedCommandValueError("Enum values must be strings.")
        return {"type": "enum", "value": unicodedata.normalize("NFC", enum_value)}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, str):
        return {"type": "string", "normalization": "NFC", "value": unicodedata.normalize("NFC", value)}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise UnsupportedCommandValueError("Decimal values must be finite.")
        return {"type": "decimal", "value": _decimal_string(value)}
    if isinstance(value, UUID):
        return {"type": "uuid", "value": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise UnsupportedCommandValueError("Datetime values must be timezone-aware.")
        normalized = value.astimezone(timezone.utc)
        return {
            "type": "datetime",
            "precision": _UTC_PRECISION,
            "value": normalized.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
    if isinstance(value, Mapping):
        return _canonical_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"type": "list", "value": [_canonical_value(item) for item in value]}
    if isinstance(value, (float, set, frozenset, bytes, bytearray)):
        raise UnsupportedCommandValueError("Unsupported command payload value.")
    raise UnsupportedCommandValueError("Unsupported command payload value.")


def _canonical_mapping(value: Mapping[object, object]) -> dict[str, object]:
    items: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise UnsupportedCommandValueError("Command object keys must be strings.")
        items[key] = _canonical_value(item)
    return {"type": "object", "value": {key: items[key] for key in sorted(items)}}


def _decimal_string(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")
