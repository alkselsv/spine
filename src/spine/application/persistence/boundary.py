"""Application contracts for immutable canonical boundary capture."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol
from uuid import UUID

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.domain.sources import CanonicalBoundary, CanonicalBoundaryMember


CANONICAL_BOUNDARY_SCHEMA_VERSION = 1
CANONICAL_BOUNDARY_RESULT_TYPE = "canonical_boundary"
_BOUNDARY_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


def _validate_identifier(value: str, field_name: str) -> str:
    if _BOUNDARY_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


@dataclass(frozen=True, slots=True)
class CanonicalBoundaryCaptureCommand:
    """Client intent; boundary identity, time and generation stay server-side."""

    projection_kind: str
    scope_version: str
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    def __post_init__(self) -> None:
        _validate_identifier(self.projection_kind, "projection_kind")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.scope_version) is None:
            raise ValueError("scope_version must be a bounded version identifier")

    def expected_digest(self) -> CommandDigest:
        return self.create(
            projection_kind=self.projection_kind,
            scope_version=self.scope_version,
            idempotency_key=self.idempotency_key,
        ).digest

    @classmethod
    def create(
        cls,
        *,
        projection_kind: str,
        scope_version: str,
        idempotency_key: IdempotencyKey,
    ) -> "CanonicalBoundaryCaptureCommand":
        operation = PersistenceOperation("canonical_boundary_capture")
        digest = digest_command(
            operation=operation,
            operation_schema_version=CANONICAL_BOUNDARY_SCHEMA_VERSION,
            payload={
                "projection_kind": projection_kind,
                "scope_version": scope_version,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            projection_kind=projection_kind,
            scope_version=scope_version,
            idempotency_key=idempotency_key,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class CanonicalBoundaryCaptureResult:
    boundary: CanonicalBoundary
    members: tuple[CanonicalBoundaryMember, ...]
    replay: bool
    claim: IdempotencyClaimResult
    result_reference: OpaqueResultReference


@dataclass(frozen=True, slots=True)
class CanonicalBoundaryClaimCaptureResult:
    """A boundary captured as one effect of an already-owned command claim."""

    boundary: CanonicalBoundary
    members: tuple[CanonicalBoundaryMember, ...]


class CanonicalBoundaryRepository(Protocol):
    async def capture(
        self, command: CanonicalBoundaryCaptureCommand
    ) -> CanonicalBoundaryCaptureResult: ...

    async def capture_for_claim(
        self,
        *,
        projection_kind: str,
        scope_version: str,
        claim: OwnedIdempotencyClaim,
    ) -> CanonicalBoundaryClaimCaptureResult: ...

    async def resolve(self, boundary_id: UUID) -> CanonicalBoundary | None: ...

    async def resolve_members(
        self, boundary_id: UUID
    ) -> tuple[CanonicalBoundaryMember, ...]: ...


__all__ = [
    "CANONICAL_BOUNDARY_RESULT_TYPE",
    "CANONICAL_BOUNDARY_SCHEMA_VERSION",
    "CanonicalBoundaryCaptureCommand",
    "CanonicalBoundaryClaimCaptureResult",
    "CanonicalBoundaryCaptureResult",
    "CanonicalBoundaryRepository",
]
