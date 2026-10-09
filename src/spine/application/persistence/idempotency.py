"""Application-facing idempotency receipt contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from spine.application.persistence.command_digest import CommandDigest
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import IdempotencyConflictError


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.:-]{0,63}")


def _require_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier.")
    return value


def _require_version(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer.")
    return value


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value or len(self.value) > 255:
            raise ValueError("Idempotency key must be a non-empty bounded string.")


@dataclass(frozen=True, slots=True)
class OpaqueResultReference:
    result_type: str
    result_id: UUID
    schema_version: int

    def __post_init__(self) -> None:
        _require_identifier(self.result_type, field_name="result type")
        if not isinstance(self.result_id, UUID) or self.result_id.int == 0:
            raise ValueError("result_id must be a non-zero UUID.")
        _require_version(self.schema_version, field_name="result schema version")


@dataclass(frozen=True, slots=True)
class OwnedIdempotencyClaim:
    kind: Literal["owned"]
    receipt_id: UUID
    operation: PersistenceOperation
    operation_schema_version: int
    key: IdempotencyKey
    digest: CommandDigest

    def __post_init__(self) -> None:
        _require_version(self.operation_schema_version, field_name="operation schema version")


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    kind: Literal["replay"]
    receipt_id: UUID
    operation: PersistenceOperation
    operation_schema_version: int
    key: IdempotencyKey
    result: OpaqueResultReference

    def __post_init__(self) -> None:
        _require_version(self.operation_schema_version, field_name="operation schema version")


IdempotencyClaimResult = OwnedIdempotencyClaim | IdempotencyReplay


class IdempotencyRepository(Protocol):
    async def claim(
        self,
        *,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> IdempotencyClaimResult: ...

    async def complete(
        self,
        claim: OwnedIdempotencyClaim,
        result: OpaqueResultReference,
    ) -> None: ...


def idempotency_conflict() -> IdempotencyConflictError:
    return IdempotencyConflictError("Idempotency key conflicts with existing command.")
