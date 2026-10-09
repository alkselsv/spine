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
_COMMAND_DIGEST_CREATION_TOKEN = object()


class UnsupportedCommandValueError(ValueError):
    """A command payload contains an unsupported or ambiguous value."""


@dataclass(frozen=True, slots=True, init=False)
class CommandDigest:
    """SHA-256 digest over a supported canonical command representation version."""

    algorithm_version: str
    value: str
    operation: PersistenceOperation
    operation_schema_version: int

    def __init__(
        self,
        *,
        _creation_token: object,
        algorithm_version: str,
        canonical_bytes: bytes,
    ) -> None:
        if _creation_token is not _COMMAND_DIGEST_CREATION_TOKEN:
            raise TypeError("CommandDigest values must come from digest_command.")
        operation, operation_schema_version = _canonical_identity(canonical_bytes)
        object.__setattr__(self, "algorithm_version", algorithm_version)
        object.__setattr__(self, "value", hashlib.sha256(canonical_bytes).hexdigest())
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "operation_schema_version", operation_schema_version)
        self.__post_init__()

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
        if not isinstance(self.operation, PersistenceOperation):
            raise UnsupportedCommandValueError("Command digest operation is invalid.")
        if (
            not isinstance(self.operation_schema_version, int)
            or isinstance(self.operation_schema_version, bool)
            or self.operation_schema_version <= 0
        ):
            raise UnsupportedCommandValueError(
                "Command digest operation schema version must be a positive integer."
            )


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
        _creation_token=_COMMAND_DIGEST_CREATION_TOKEN,
        algorithm_version=COMMAND_DIGEST_ALGORITHM_VERSION,
        canonical_bytes=canonical,
    )


def _canonical_identity(canonical_bytes: bytes) -> tuple[PersistenceOperation, int]:
    if not isinstance(canonical_bytes, bytes):
        raise UnsupportedCommandValueError("Canonical command bytes must be UTF-8 bytes.")
    try:
        document = json.loads(canonical_bytes.decode("utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("canonical_version") != _CANONICAL_REPRESENTATION_VERSION
            or "payload" not in document
        ):
            raise ValueError
        operation = PersistenceOperation(document["operation"])
        operation_schema_version = document["operation_schema_version"]
    except (KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        raise UnsupportedCommandValueError("Canonical command representation is invalid.") from None
    if (
        not isinstance(operation_schema_version, int)
        or isinstance(operation_schema_version, bool)
        or operation_schema_version <= 0
    ):
        raise UnsupportedCommandValueError("Operation schema version must be a positive integer.")
    return operation, operation_schema_version


def _canonical_value(value: object) -> Any:
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, Enum):
        declared_value = _canonical_value(value.value)
        if declared_value["type"] in {"enum", "list", "object"}:
            raise UnsupportedCommandValueError(
                "Enum values must be supported scalar values."
            )
        result: dict[str, object] = {
            "type": "enum",
            "value": declared_value["value"],
        }
        if declared_value["type"] != "string":
            result["value_type"] = declared_value["type"]
            if "precision" in declared_value:
                result["precision"] = declared_value["precision"]
        return result
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
        normalized_key = unicodedata.normalize("NFC", key)
        if normalized_key in items:
            raise UnsupportedCommandValueError(
                "Command object keys must be unique after NFC normalization."
            )
        items[normalized_key] = _canonical_value(item)
    return {"type": "object", "value": {key: items[key] for key in sorted(items)}}


def _decimal_string(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")
