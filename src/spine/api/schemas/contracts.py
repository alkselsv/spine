"""Versioned, transport-only contracts shared by R1 API slices.

These models describe the public control-plane conventions. They deliberately
do not import application services or persistence adapters, so future routes
can consume them without moving transport policy into the domain.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

API_SCHEMA_VERSION: Literal[1] = 1
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_CURSOR_LENGTH = 768
MAX_IDEMPOTENCY_KEY_LENGTH = 255
_CURSOR_PARTS = 2
_CURSOR_OPERATION = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_FILTER_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PayloadT = TypeVar("PayloadT")
_ItemT = TypeVar("ItemT")
_ResultT = TypeVar("ResultT")


class InvalidCursor(ValueError):
    """A cursor is malformed, tampered with, stale, or bound to another scope."""


class IdempotencyKey(BaseModel):
    """The canonical value of the HTTP ``Idempotency-Key`` header."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(
        min_length=1,
        max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        strict=True,
        description="Opaque caller-generated mutation key.",
    )

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in value
        ):
            raise ValueError("idempotency key must be a trimmed printable value")
        return value


class CollectionQuery(BaseModel):
    """Common bounded query parameters for every future collection route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(
        default=DEFAULT_PAGE_LIMIT,
        ge=1,
        le=MAX_PAGE_LIMIT,
        strict=True,
        description="Maximum number of records returned in one page.",
    )
    cursor: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CURSOR_LENGTH,
        description="Opaque scope-bound cursor from a previous page.",
    )


class ApiResponseEnvelope(BaseModel, Generic[_PayloadT]):
    """Immutable response metadata shared by a versioned API operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = API_SCHEMA_VERSION
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    data: _PayloadT

    @field_validator("workspace_id", "environment_id", "trace_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("scope and trace identities must be non-zero")
        return value


class ApiCollectionEnvelope(BaseModel, Generic[_ItemT]):
    """Bounded collection response with deterministic continuation semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = API_SCHEMA_VERSION
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    items: tuple[_ItemT, ...]
    limit: int = Field(ge=1, le=MAX_PAGE_LIMIT, strict=True)
    next_cursor: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_CURSOR_LENGTH,
    )

    @field_validator("workspace_id", "environment_id", "trace_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("scope and trace identities must be non-zero")
        return value


class CommandStatus(str, Enum):
    """Public lifecycle of a requested mutation."""

    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApiCommandResult(BaseModel, Generic[_ResultT]):
    """Typed, replay-safe result metadata for a mutation command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = API_SCHEMA_VERSION
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    command_id: UUID
    status: CommandStatus
    replay: bool = False
    result: _ResultT | None = None

    @field_validator("workspace_id", "environment_id", "trace_id", "command_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("command and scope identities must be non-zero")
        return value


class Cursor(BaseModel):
    """Validated cursor claims; the opaque wire value is handled by CursorCodec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = API_SCHEMA_VERSION
    workspace_id: UUID
    environment_id: UUID
    operation_id: str = Field(pattern=_CURSOR_OPERATION)
    limit: int = Field(ge=1, le=MAX_PAGE_LIMIT, strict=True)
    filters_digest: str = Field(pattern=_FILTER_DIGEST)
    position: str = Field(min_length=1, max_length=256)

    @field_validator("workspace_id", "environment_id")
    @classmethod
    def reject_zero_scope(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("cursor scope identities must be non-zero")
        return value


class CursorCodec:
    """Sign and validate opaque cursors bound to one operation and query scope."""

    __slots__ = ("_secret", "_max_length")

    def __init__(self, *, secret: bytes, max_length: int = MAX_CURSOR_LENGTH) -> None:
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("cursor secret must contain at least 16 bytes")
        if max_length < 128 or max_length > 4096:
            raise ValueError("cursor max length must be between 128 and 4096")
        self._secret = secret
        self._max_length = max_length

    def encode(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: str,
        limit: int,
        filters_digest: str,
        position: str,
    ) -> str:
        claims = Cursor(
            workspace_id=workspace_id,
            environment_id=environment_id,
            operation_id=operation_id,
            limit=limit,
            filters_digest=filters_digest,
            position=position,
        )
        payload = _urlsafe_encode(claims.model_dump(mode="json"))
        signature = _sign(self._secret, payload)
        cursor = f"{payload}.{signature}"
        if len(cursor) > self._max_length:
            raise ValueError("cursor exceeds the configured bound")
        return cursor

    def decode(
        self,
        cursor: str,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_id: str,
        limit: int,
        filters_digest: str,
    ) -> Cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > self._max_length:
                raise InvalidCursor()
            parts = cursor.split(".")
            if len(parts) != _CURSOR_PARTS:
                raise InvalidCursor()
            payload, signature = parts
            if not hmac.compare_digest(signature, _sign(self._secret, payload)):
                raise InvalidCursor()
            claims = Cursor.model_validate(_urlsafe_decode(payload))
            if (
                claims.workspace_id != workspace_id
                or claims.environment_id != environment_id
                or claims.operation_id != operation_id
                or claims.limit != limit
                or claims.filters_digest != filters_digest
            ):
                raise InvalidCursor()
            return claims
        except InvalidCursor:
            raise
        except (TypeError, ValueError, UnicodeError, binascii.Error):
            raise InvalidCursor() from None


def canonical_filter_digest(filters: Mapping[str, Any]) -> str:
    """Return a stable digest for the allowlisted endpoint filter object."""

    try:
        encoded = json.dumps(
            filters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("filters must be JSON-compatible") from None
    return hashlib.sha256(encoded).hexdigest()


def parse_idempotency_key_header(values: Sequence[str]) -> IdempotencyKey:
    """Parse exactly one HTTP ``Idempotency-Key`` value."""

    if len(values) != 1:
        raise ValueError("exactly one idempotency key is required")
    try:
        return IdempotencyKey(value=values[0])
    except (TypeError, ValueError):
        raise ValueError("idempotency key is invalid") from None


def _urlsafe_encode(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> Mapping[str, Any]:
    if not value or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise InvalidCursor()
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.b64decode(padded, altchars=b"-_")
    result = json.loads(decoded.decode("utf-8"))
    if not isinstance(result, dict):
        raise InvalidCursor()
    return result


def _sign(secret: bytes, payload: str) -> str:
    return (
        base64.urlsafe_b64encode(
            hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )


__all__ = [
    "API_SCHEMA_VERSION",
    "ApiCollectionEnvelope",
    "ApiCommandResult",
    "ApiResponseEnvelope",
    "CollectionQuery",
    "CommandStatus",
    "Cursor",
    "CursorCodec",
    "DEFAULT_PAGE_LIMIT",
    "IdempotencyKey",
    "InvalidCursor",
    "MAX_CURSOR_LENGTH",
    "MAX_IDEMPOTENCY_KEY_LENGTH",
    "MAX_PAGE_LIMIT",
    "canonical_filter_digest",
    "parse_idempotency_key_header",
]
