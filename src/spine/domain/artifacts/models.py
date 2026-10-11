from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from spine.domain.common import ActorRef, SchemaRef, SpineModel


class ArtifactRef(SpineModel):
    id: UUID
    schema_name: str
    schema_version: str


class ArtifactState(str, Enum):
    """Whether an immutable artifact may participate in a new handoff."""

    ACTIVE = "active"
    STALE = "stale"
    TOMBSTONED = "tombstoned"


def validate_artifact_lineage(value: tuple[UUID, ...]) -> tuple[UUID, ...]:
    """Validate the direct immutable lineage shared by drafts and records."""

    if any(identifier.int == 0 for identifier in value):
        raise ValueError("upstream artifact identifiers must be non-zero")
    if len(set(value)) != len(value):
        raise ValueError("upstream artifact identifiers must be unique")
    return value


class Artifact(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    environment_id: UUID
    work_item_id: UUID
    produced_by: ActorRef
    produced_by_agent_version_id: UUID | None = None
    artifact_schema: SchemaRef
    content: Any
    evidence_refs: tuple[str, ...] = ()
    upstream_artifact_ids: tuple[UUID, ...] = ()
    state: ArtifactState = ArtifactState.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_legacy(
        cls,
        value: Mapping[str, Any],
        *,
        environment_id: UUID,
    ) -> Artifact:
        """Upgrade a pre-environment Artifact at an explicit adapter boundary."""

        if "environment_id" in value:
            raise ValueError("legacy artifact already carries an environment")
        return cls.model_validate({**value, "environment_id": environment_id})

    @field_validator(
        "id",
        "workspace_id",
        "environment_id",
        "work_item_id",
        "produced_by_agent_version_id",
    )
    @classmethod
    def reject_zero_identifiers(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("artifact identifiers must be non-zero")
        return value

    @field_validator("upstream_artifact_ids")
    @classmethod
    def validate_upstream_identifiers(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return validate_artifact_lineage(value)

    @model_validator(mode="after")
    def reject_lineage_cycle(self) -> Artifact:
        if self.id in self.upstream_artifact_ids:
            raise ValueError("artifact cannot include itself in upstream lineage")
        return self
