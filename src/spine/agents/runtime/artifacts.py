"""Typed Artifact conversion and persistence ports for local agent execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from spine.agents.runtime.contracts import AgentResponse, CapabilitySchemaIdentity
from spine.domain.artifacts import Artifact, ArtifactRef, validate_artifact_lineage
from spine.domain.common import ActorRef, SchemaRef


class ArtifactReadError(RuntimeError):
    """An input Artifact cannot be disclosed to this execution scope."""


class ArtifactDraft(BaseModel):
    """One validated immutable output awaiting an adapter-assigned identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: UUID
    environment_id: UUID
    work_item_id: UUID
    produced_by: ActorRef
    produced_by_agent_version_id: UUID
    artifact_schema: SchemaRef
    content: Any
    evidence_refs: tuple[str, ...]
    upstream_artifact_ids: tuple[UUID, ...]

    @field_validator(
        "workspace_id",
        "environment_id",
        "work_item_id",
        "produced_by_agent_version_id",
    )
    @classmethod
    def reject_zero_identifiers(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("artifact draft identifiers must be non-zero")
        return value

    @field_validator("upstream_artifact_ids")
    @classmethod
    def validate_upstream_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return validate_artifact_lineage(value)


@runtime_checkable
class ArtifactPersistence(Protocol):
    """Narrow canonical Artifact boundary used by a single runtime attempt.

    ``load`` applies the requested scope and access policy before returning a
    record. ``persist_outputs`` is atomic for the supplied batch: it either
    returns durable references for every draft or raises without reporting a
    successful completion. Durable replay/idempotency is owned by issue #112.
    """

    async def load(
        self,
        artifact_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> Artifact | None: ...

    async def verify_lineage(
        self,
        artifact: Artifact,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> None:
        """Verify all declared ancestors without disclosing their records."""

    async def persist_outputs(
        self,
        drafts: tuple[ArtifactDraft, ...],
        *,
        idempotency_key: str,
    ) -> tuple[ArtifactRef, ...]: ...


@dataclass(frozen=True, slots=True)
class CapabilityArtifactSchema:
    """Explicit typed mapping for one versioned Capability handoff."""

    schema_identity: CapabilitySchemaIdentity
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    assemble_input: Callable[[tuple[Artifact, ...]], Any]
    validate_metadata: Callable[[AgentResponse[Any]], None]

    def __post_init__(self) -> None:
        if not isinstance(self.schema_identity, CapabilitySchemaIdentity):
            raise TypeError("artifact schema identity is invalid")
        if not callable(self.assemble_input) or not callable(self.validate_metadata):
            raise TypeError("artifact schema adapters must be callable")
        for model in (self.input_model, self.output_model):
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise TypeError("artifact schemas must be Pydantic model types")
            if model.model_config.get("extra") != "forbid":
                raise ValueError("artifact schemas must forbid extra fields")

    def load_input(self, artifacts: tuple[Artifact, ...]) -> BaseModel:
        """Assemble and revalidate input without exposing storage records."""

        value = self.assemble_input(artifacts)
        return self.input_model.model_validate(value)

    def check_metadata(self, value: AgentResponse[Any]) -> None:
        """Apply the Capability-owned evidence/metrics/action policy."""

        self.validate_metadata(value)

    def load_output(self, value: Any) -> BaseModel:
        """Revalidate handler output even when it is already a model instance."""

        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        return self.output_model.model_validate(value)

    @staticmethod
    def dump(model: BaseModel) -> Any:
        return model.model_dump(mode="json")


__all__ = [
    "ArtifactDraft",
    "ArtifactPersistence",
    "ArtifactReadError",
    "CapabilityArtifactSchema",
]
