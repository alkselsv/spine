"""Provider-neutral context and retrieval contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBool, StrictInt, model_validator

from spine.domain.common import ActorRef, DefinitionModel, SpineModel


_DIGEST = r"[0-9a-f]{64}"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"


def _require_non_zero(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID")


class ProjectionOperation(str, Enum):
    PROJECT = "project"
    UPDATE = "update"
    REMOVE = "remove"
    REBUILD = "rebuild"
    RETRIEVE = "retrieve"


class ProjectionOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    FAILED = "failed"
    MISSING = "missing"
    STALE = "stale"
    TOMBSTONED = "tombstoned"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"


class ProjectionLifecycle(str, Enum):
    """Provider-neutral lifecycle of one projection command."""

    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    RECOVERED = "recovered"
    RECOVERABLE = "recoverable"
    FAILED = "failed"
    FENCED = "fenced"


class ProjectionEffectAction(str, Enum):
    """The only source-scoped physical mutations a projection may request."""

    UPSERT_SOURCE = "upsert_source"
    REMOVE_SOURCE = "remove_source"


class ProjectionEffectStatus(str, Enum):
    """Safe state returned for one physical effect."""

    PLANNED = "planned"
    APPLIED = "applied"
    REPLAYED = "replayed"
    FAILED = "failed"


class ProjectionFailureCode(str, Enum):
    INTEGRITY_CONFLICT = "integrity_conflict"
    MISSING_EVIDENCE = "missing_evidence"
    STALE_EVIDENCE = "stale_evidence"
    TOMBSTONED_SOURCE = "tombstoned_source"
    AMBIGUOUS_EVIDENCE = "ambiguous_evidence"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    NAMESPACE_UNAVAILABLE = "namespace_unavailable"
    REVISION_FENCED = "revision_fenced"
    PHYSICAL_FAILURE = "physical_failure"


class _ProjectionModel(DefinitionModel):
    """Immutable provider-neutral contract with a closed JSON shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StableLocator(_ProjectionModel):
    """A provider-neutral, source-revision-scoped evidence locator."""

    schema_version: Literal["stable-locator:v1"] = "stable-locator:v1"
    kind: Literal["page", "sheet_row", "message", "character_range", "record_field", "custom"]
    value: str = Field(min_length=1, max_length=512)


class ProjectionSourceRevision(_ProjectionModel):
    """The minimum source lineage an adapter may receive or return."""

    schema_version: Literal["projection-source-revision:v1"] = "projection-source-revision:v1"
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=_DIGEST)
    revision_number: int = Field(default=1, ge=1)
    status: Literal["current", "stale", "tombstoned", "missing", "ambiguous", "incomplete"] = "current"
    processing_receipt_id: UUID | None = None
    locator_contract_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_identity(self) -> "ProjectionSourceRevision":
        _require_non_zero(self.source_object_id, "source_object_id")
        _require_non_zero(self.source_revision_id, "source_revision_id")
        if self.processing_receipt_id is not None:
            _require_non_zero(self.processing_receipt_id, "processing_receipt_id")
        return self


class ProjectionConfigVersion(_ProjectionModel):
    """Pinned processing configuration; never a provider-owned setting."""

    schema_version: Literal["projection-config:v1"] = "projection-config:v1"
    config_id: UUID
    version: str = Field(min_length=1, max_length=128)
    config_digest: str = Field(pattern=_DIGEST)
    ontology_version: str = Field(min_length=1, max_length=128)
    adapter_contract_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_identity(self) -> "ProjectionConfigVersion":
        _require_non_zero(self.config_id, "config_id")
        return self


class CanonicalBoundary(_ProjectionModel):
    """The canonical input cut selected by the owning persistence layer."""

    schema_version: Literal["canonical-boundary:v1"] = "canonical-boundary:v1"
    boundary_id: UUID
    boundary_digest: str = Field(pattern=_DIGEST)
    accepted_through_generation: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_identity(self) -> "CanonicalBoundary":
        _require_non_zero(self.boundary_id, "boundary_id")
        return self


class ProjectionCandidate(_ProjectionModel):
    """Opaque candidate context for an isolated shadow namespace."""

    schema_version: Literal["projection-candidate:v1"] = "projection-candidate:v1"
    candidate_id: UUID
    run_id: UUID
    partition_id: UUID
    snapshot_id: UUID | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> "ProjectionCandidate":
        for field_name in ("candidate_id", "run_id", "partition_id", "snapshot_id"):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_zero(value, field_name)
        return self


class ProjectionNamespace(_ProjectionModel):
    """Opaque physical namespace selected by Spine, never by a provider."""

    schema_version: Literal["projection-namespace:v1"] = "projection-namespace:v1"
    namespace: str = Field(min_length=1, max_length=512)
    namespace_digest: str = Field(pattern=_DIGEST)


class EvidenceReference(_ProjectionModel):
    """A structured, revision-specific pointer returned with projected data."""

    schema_version: Literal["projection-evidence-reference:v1"] = "projection-evidence-reference:v1"
    evidence_id: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    locator: StableLocator
    evidence_digest: str = Field(pattern=_DIGEST)
    status: Literal["valid", "missing", "stale", "tombstoned", "ambiguous", "incomplete"] = "valid"

    @model_validator(mode="after")
    def validate_identity(self) -> "EvidenceReference":
        for field_name in (
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
        ):
            _require_non_zero(getattr(self, field_name), field_name)
        return self


class ProjectionChunk(_ProjectionModel):
    schema_version: Literal["projection-chunk:v1"] = "projection-chunk:v1"
    chunk_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class ProjectionEntity(_ProjectionModel):
    schema_version: Literal["projection-entity:v1"] = "projection-entity:v1"
    entity_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class ProjectionRelation(_ProjectionModel):
    schema_version: Literal["projection-relation:v1"] = "projection-relation:v1"
    relation_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=128)
    subject_entity_id: str = Field(min_length=1, max_length=128)
    object_entity_id: str = Field(min_length=1, max_length=128)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class ProjectionDiagnostic(_ProjectionModel):
    """Safe operational detail; it deliberately has no free-form provider text."""

    schema_version: Literal["projection-safe-diagnostic:v1"] = "projection-safe-diagnostic:v1"
    attempt: StrictInt | None = Field(default=None, ge=1)
    item_count: StrictInt | None = Field(default=None, ge=0)
    duration_ms: StrictInt | None = Field(default=None, ge=0)
    opaque_detail_ref: UUID | None = None
    excerpt: None = None

    @model_validator(mode="after")
    def validate_reference(self) -> "ProjectionDiagnostic":
        if self.opaque_detail_ref is not None:
            _require_non_zero(self.opaque_detail_ref, "opaque_detail_ref")
        return self


class ProjectionFailure(_ProjectionModel):
    schema_version: Literal["projection-failure:v1"] = "projection-failure:v1"
    code: ProjectionFailureCode
    retryable: StrictBool
    safe_detail: ProjectionDiagnostic


class ProjectionCount(_ProjectionModel):
    """One immutable, content-free adapter count."""

    schema_version: Literal["projection-count:v1"] = "projection-count:v1"
    name: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    value: StrictInt = Field(ge=0)


class ProjectionEffect(_ProjectionModel):
    """Deterministic, source-scoped work handed to a physical adapter."""

    schema_version: Literal["projection-effect:v1"] = "projection-effect:v1"
    effect_id: str = Field(min_length=1, max_length=256)
    operation: ProjectionOperation
    action: ProjectionEffectAction
    status: ProjectionEffectStatus = ProjectionEffectStatus.PLANNED
    workspace_id: UUID
    environment_id: UUID
    command_identity: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    projection_candidate: ProjectionCandidate
    projection_config: ProjectionConfigVersion
    canonical_boundary: CanonicalBoundary
    physical_namespace: ProjectionNamespace
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str = Field(pattern=_DIGEST)
    revision_number: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "ProjectionEffect":
        for field_name in (
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
        ):
            _require_non_zero(getattr(self, field_name), field_name)
        return self


class ProjectionRecovery(_ProjectionModel):
    """Typed continuation data for a command interrupted by physical failure."""

    schema_version: Literal["projection-recovery:v1"] = "projection-recovery:v1"
    attempt: StrictInt = Field(ge=1)
    pending_effect_ids: tuple[str, ...] = Field(min_length=1)


class ProjectionEffectStore(Protocol):
    """Provider-neutral physical mutation port."""

    async def apply(self, effect: ProjectionEffect) -> ProjectionEffect: ...


class ProjectionCommand(_ProjectionModel):
    """Common immutable command envelope for every physical operation."""

    schema_version: Literal["context-projection-command:v1"] = "context-projection-command:v1"
    workspace_id: UUID
    environment_id: UUID
    projection_run_id: UUID
    projection_candidate: ProjectionCandidate
    projection_config: ProjectionConfigVersion
    canonical_boundary: CanonicalBoundary
    source_revisions: tuple[ProjectionSourceRevision, ...] = ()
    physical_namespace: ProjectionNamespace
    command_identity: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    command_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def validate_scope(self) -> "ProjectionCommand":
        for field_name in ("workspace_id", "environment_id", "projection_run_id"):
            _require_non_zero(getattr(self, field_name), field_name)
        if self.projection_candidate.run_id != self.projection_run_id:
            raise ValueError("projection candidate must belong to the command run")
        source_ids = [source.source_object_id for source in self.source_revisions]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("a command may contain one revision per source object")
        return self


class ProjectCommand(ProjectionCommand):
    operation: Literal[ProjectionOperation.PROJECT] = ProjectionOperation.PROJECT


class UpdateCommand(ProjectionCommand):
    operation: Literal[ProjectionOperation.UPDATE] = ProjectionOperation.UPDATE


class RemoveCommand(ProjectionCommand):
    operation: Literal[ProjectionOperation.REMOVE] = ProjectionOperation.REMOVE


class RebuildCommand(ProjectionCommand):
    operation: Literal[ProjectionOperation.REBUILD] = ProjectionOperation.REBUILD


class RetrieveCommand(ProjectionCommand):
    operation: Literal[ProjectionOperation.RETRIEVE] = ProjectionOperation.RETRIEVE
    query: str = Field(min_length=1, max_length=4096)
    limit: int = Field(default=15, ge=1, le=100)


class ProjectionResult(_ProjectionModel):
    schema_version: Literal["projection-result:v1"] = "projection-result:v1"
    operation: ProjectionOperation
    outcome: ProjectionOutcome
    command_identity: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    command_digest: str = Field(pattern=_DIGEST)
    lifecycle: ProjectionLifecycle = ProjectionLifecycle.SUCCEEDED
    artifact_reference: str | None = Field(default=None, min_length=1, max_length=256)
    counts: tuple[ProjectionCount, ...] = ()
    effects: tuple[ProjectionEffect, ...] = ()
    recovery: ProjectionRecovery | None = None
    failure: ProjectionFailure | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "ProjectionResult":
        if self.lifecycle in {
            ProjectionLifecycle.SUCCEEDED,
            ProjectionLifecycle.RECOVERED,
        }:
            if self.outcome is not ProjectionOutcome.SUCCEEDED:
                raise ValueError("successful lifecycle requires a succeeded outcome")
            if self.failure is not None or self.recovery is not None:
                raise ValueError("successful lifecycle cannot contain failure state")
            if any(
                effect.status
                not in {
                    ProjectionEffectStatus.APPLIED,
                    ProjectionEffectStatus.REPLAYED,
                }
                for effect in self.effects
            ):
                raise ValueError("successful lifecycle cannot contain failed effects")
        elif self.lifecycle is ProjectionLifecycle.RECOVERABLE:
            if self.outcome is not ProjectionOutcome.FAILED:
                raise ValueError("recoverable lifecycle requires a failed outcome")
            if (
                self.failure is None
                or self.failure.code is not ProjectionFailureCode.PHYSICAL_FAILURE
                or self.recovery is None
            ):
                raise ValueError("recoverable lifecycle requires failure and recovery")
            pending_effect_ids = {
                effect.effect_id
                for effect in self.effects
                if effect.status
                not in {
                    ProjectionEffectStatus.APPLIED,
                    ProjectionEffectStatus.REPLAYED,
                }
            }
            if (
                not pending_effect_ids
                or set(self.recovery.pending_effect_ids) != pending_effect_ids
            ):
                raise ValueError("recovery must list exactly the pending effects")
        elif self.lifecycle is ProjectionLifecycle.REPLAYED:
            if (
                self.outcome is not ProjectionOutcome.REPLAYED
                or self.failure is not None
                or self.recovery is not None
                or any(
                    effect.status is not ProjectionEffectStatus.REPLAYED
                    for effect in self.effects
                )
            ):
                raise ValueError("replayed lifecycle requires a replayed outcome")
        elif self.lifecycle is ProjectionLifecycle.FENCED:
            if self.outcome is not ProjectionOutcome.STALE:
                raise ValueError("fenced lifecycle requires a stale outcome")
            if (
                self.failure is None
                or self.failure.code is not ProjectionFailureCode.REVISION_FENCED
                or self.effects
            ):
                raise ValueError("fenced lifecycle must contain only a revision fence")
        elif self.lifecycle is ProjectionLifecycle.FAILED:
            if self.failure is None:
                raise ValueError("failed lifecycle requires a failure")
            if self.outcome in {
                ProjectionOutcome.SUCCEEDED,
                ProjectionOutcome.REPLAYED,
            }:
                raise ValueError("failed lifecycle cannot have a success outcome")
        return self


class ProjectionRetrievalResult(_ProjectionModel):
    schema_version: Literal["projection-retrieval-result:v1"] = "projection-retrieval-result:v1"
    operation: Literal[ProjectionOperation.RETRIEVE] = ProjectionOperation.RETRIEVE
    outcome: ProjectionOutcome
    command_identity: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    command_digest: str = Field(pattern=_DIGEST)
    chunks: tuple[ProjectionChunk, ...] = ()
    entities: tuple[ProjectionEntity, ...] = ()
    relations: tuple[ProjectionRelation, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    failure: ProjectionFailure | None = None


class ContextProjection(Protocol):
    """Replaceable physical projection port; it has no publication authority."""

    async def project(self, command: ProjectCommand) -> ProjectionResult: ...

    async def update(self, command: UpdateCommand) -> ProjectionResult: ...

    async def remove(self, command: RemoveCommand) -> ProjectionResult: ...

    async def rebuild(self, command: RebuildCommand) -> ProjectionResult: ...

    async def retrieve(self, command: RetrieveCommand) -> ProjectionRetrievalResult: ...


class RetrievalQuery(SpineModel):
    workspace_id: UUID
    environment_id: UUID
    query: str
    requested_by: ActorRef
    context_profile: str
    scopes: tuple[str, ...] = ()
    limit: int = Field(default=15, ge=1, le=100)


class ContextReference(SpineModel):
    source_id: str
    source_revision_id: UUID
    workspace_id: UUID
    environment_id: UUID
    evidence_id: str
    locator: dict[str, Any] = Field(default_factory=dict)
    excerpt: str | None = None


class RetrievalResult(SpineModel):
    context: tuple[str, ...] = ()
    entities: tuple[dict[str, Any], ...] = ()
    relations: tuple[dict[str, Any], ...] = ()
    references: tuple[ContextReference, ...] = ()
    retrieval_strategy: str
    index_version: str | None = None


class ContextBroker(Protocol):
    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult: ...
