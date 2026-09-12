from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field

from spine.domain.common import ActorRef, SchemaRef, SpineModel


class ArtifactRef(SpineModel):
    id: UUID
    schema_name: str
    schema_version: str


class Artifact(SpineModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    work_item_id: UUID
    produced_by: ActorRef
    artifact_schema: SchemaRef
    content: Any
    evidence_refs: tuple[str, ...] = ()
    upstream_artifact_ids: tuple[UUID, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
