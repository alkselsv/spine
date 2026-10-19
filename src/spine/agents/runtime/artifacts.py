"""Typed Artifact conversion and persistence ports for local agent execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from spine.agents.runtime.contracts import AgentResponse, CapabilitySchemaIdentity
from spine.application.diagnostics import AuditEvent
from spine.application.persistence import (
    CommandDigest,
    IdempotencyClaimResult,
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.domain.agents import AgentResult, AgentRunStatus
from spine.domain.artifacts import Artifact, ArtifactRef, validate_artifact_lineage
from spine.domain.common import ActorRef, FrozenDict, SchemaRef


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


class InvocationOutputCheckpoint(BaseModel):
    """Durable validated handler output awaiting Artifact completion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    drafts: tuple[ArtifactDraft, ...]
    evidence_refs: tuple[str, ...]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    structured_metrics: Mapping[str, float] = Field(default_factory=FrozenDict)
    trace_ref: str

    @field_validator("drafts")
    @classmethod
    def require_drafts(
        cls,
        value: tuple[ArtifactDraft, ...],
    ) -> tuple[ArtifactDraft, ...]:
        if not value:
            raise ValueError("output checkpoint requires Artifact drafts")
        return value

    @field_validator("structured_metrics", mode="after")
    @classmethod
    def freeze_metrics(cls, value: Mapping[str, float]) -> Mapping[str, float]:
        return FrozenDict(value)

    def build_result(self, references: tuple[ArtifactRef, ...]) -> AgentResult:
        if len(references) != len(self.drafts):
            raise ValueError("output checkpoint references are invalid")
        return AgentResult(
            status=AgentRunStatus.SUCCEEDED,
            output_artifact_ids=tuple(reference.id for reference in references),
            evidence_refs=self.evidence_refs,
            confidence=self.confidence,
            structured_metrics=self.structured_metrics,
            trace_ref=self.trace_ref,
        )


class InvocationReplayRecord(BaseModel):
    """Versioned record referenced by one immutable idempotency receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    state: Literal["checkpoint", "completed"]
    checkpoint: InvocationOutputCheckpoint | None = None
    result: AgentResult | None = None

    @model_validator(mode="after")
    def require_state_value(self) -> InvocationReplayRecord:
        if self.state == "checkpoint":
            if self.checkpoint is None or self.result is not None:
                raise ValueError("checkpoint replay record is invalid")
        elif self.result is None or self.checkpoint is not None:
            raise ValueError("completed replay record is invalid")
        return self

    @classmethod
    def from_checkpoint(
        cls,
        checkpoint: InvocationOutputCheckpoint,
    ) -> InvocationReplayRecord:
        return cls(state="checkpoint", checkpoint=checkpoint)

    @classmethod
    def from_result(cls, result: AgentResult) -> InvocationReplayRecord:
        return cls(state="completed", result=result)


@dataclass(frozen=True, slots=True)
class PersistedAgentInvocationResult:
    """Actual runtime result and its canonical persisted invocation reference."""

    result: AgentResult
    reference: OpaqueResultReference


@runtime_checkable
class ArtifactPersistence(Protocol):
    """Narrow canonical Artifact boundary used by a single runtime attempt.

    ``load`` applies the requested scope and access policy before returning a
    record. Invocation claims reuse the canonical Issue #8 receipt types.
    ``checkpoint_outputs`` atomically stores validated handler output and
    completes the canonical receipt. ``persist_outputs`` then atomically commits
    the Artifact batch, AgentResult and required Audit Event. If either commit
    acknowledgement is lost, the next claim returns ``IdempotencyReplay`` and
    resolves either the stable result or the checkpoint without rerunning the
    handler.
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

    async def claim_invocation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> IdempotencyClaimResult: ...

    async def load_invocation_result(
        self,
        replay: IdempotencyReplay,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> InvocationReplayRecord | None: ...

    async def checkpoint_outputs(
        self,
        checkpoint: InvocationOutputCheckpoint,
        *,
        claim: OwnedIdempotencyClaim,
        result_reference: OpaqueResultReference,
    ) -> InvocationOutputCheckpoint: ...

    async def persist_outputs(
        self,
        checkpoint: InvocationOutputCheckpoint,
        *,
        claim: OwnedIdempotencyClaim | IdempotencyReplay,
        result_reference: OpaqueResultReference,
        audit_event: AuditEvent,
        validate_references: Callable[[tuple[ArtifactRef, ...]], None],
    ) -> AgentResult: ...

    async def persist_result(
        self,
        result: AgentResult,
        *,
        claim: OwnedIdempotencyClaim,
        result_reference: OpaqueResultReference,
        audit_event: AuditEvent,
    ) -> AgentResult:
        """Atomically persist a terminal result, Audit Event and completion."""


@dataclass(frozen=True, slots=True)
class CapabilityArtifactSchema:
    """Explicit typed mapping for one versioned Capability handoff."""

    schema_identity: CapabilitySchemaIdentity
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    assemble_input: Callable[[tuple[Artifact, ...]], Any]
    validate_metadata: Callable[[AgentResponse[Any]], None]
    validate_handoff: Callable[[BaseModel, BaseModel, AgentResponse[Any]], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.schema_identity, CapabilitySchemaIdentity):
            raise TypeError("artifact schema identity is invalid")
        if not callable(self.assemble_input) or not callable(self.validate_metadata):
            raise TypeError("artifact schema adapters must be callable")
        if self.validate_handoff is not None and not callable(self.validate_handoff):
            raise TypeError("artifact handoff validator must be callable")
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

    def check_handoff(
        self,
        input_model: BaseModel,
        output_model: BaseModel,
        response: AgentResponse[Any],
    ) -> None:
        """Validate relationships that span the typed input and output.

        Most capabilities only need independent input/output schema checks.
        Evidence-bearing capabilities can use this optional hook to enforce
        that output lineage belongs to the exact input handoff without making
        the generic runtime aware of their domain model.
        """

        if self.validate_handoff is not None:
            self.validate_handoff(input_model, output_model, response)

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
    "InvocationOutputCheckpoint",
    "InvocationReplayRecord",
]
