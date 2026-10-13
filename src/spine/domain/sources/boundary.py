"""Immutable canonical cuts of accepted source heads."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Annotated
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from spine.domain.common import DefinitionModel, VersionIdentifier

from .models import RevisionKind


BoundaryIdentifier = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$"),
]
BoundaryDigest = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class CanonicalBoundary(DefinitionModel):
    """A server-selected, commit-consistent accepted-current cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary_id: UUID
    workspace_id: UUID
    environment_id: UUID
    projection_kind: BoundaryIdentifier
    scope_version: VersionIdentifier
    accepted_through_generation: int = Field(ge=0)
    boundary_time: datetime
    manifest_digest: BoundaryDigest
    idempotency_receipt_id: UUID

    @model_validator(mode="after")
    def validate_boundary(self) -> "CanonicalBoundary":
        for field_name in (
            "boundary_id",
            "workspace_id",
            "environment_id",
            "idempotency_receipt_id",
        ):
            if getattr(self, field_name).int == 0:
                raise ValueError(f"{field_name} must be non-zero")
        if self.boundary_time.tzinfo is None or self.boundary_time.utcoffset() is None:
            raise ValueError("boundary_time must be timezone-aware")
        return self


class CanonicalBoundaryMember(DefinitionModel):
    """One immutable source membership entry in a canonical boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    boundary_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    acceptance_id: UUID
    workspace_id: UUID
    environment_id: UUID
    revision_kind: RevisionKind
    accepted_generation: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_member(self) -> "CanonicalBoundaryMember":
        for field_name in (
            "boundary_id",
            "source_object_id",
            "source_revision_id",
            "acceptance_id",
            "workspace_id",
            "environment_id",
        ):
            if getattr(self, field_name).int == 0:
                raise ValueError(f"{field_name} must be non-zero")
        return self


def canonical_boundary_manifest_digest(
    boundary: CanonicalBoundary,
    members: tuple[CanonicalBoundaryMember, ...],
) -> str:
    """Hash the immutable header and ordered exact membership."""

    ordered = tuple(sorted(members, key=lambda member: member.source_object_id.hex))
    if len({member.source_object_id for member in ordered}) != len(ordered):
        raise ValueError("canonical boundary members must have unique source IDs")
    payload = {
        "schema": "canonical-boundary-manifest:v1",
        "boundary_id": str(boundary.boundary_id),
        "workspace_id": str(boundary.workspace_id),
        "environment_id": str(boundary.environment_id),
        "projection_kind": boundary.projection_kind,
        "scope_version": boundary.scope_version,
        "accepted_through_generation": boundary.accepted_through_generation,
        "boundary_time": boundary.boundary_time.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "members": [
            {
                "source_object_id": str(member.source_object_id),
                "source_revision_id": str(member.source_revision_id),
                "acceptance_id": str(member.acceptance_id),
                "revision_kind": member.revision_kind.value,
                "accepted_generation": member.accepted_generation,
            }
            for member in ordered
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CanonicalBoundary",
    "CanonicalBoundaryMember",
    "canonical_boundary_manifest_digest",
]
