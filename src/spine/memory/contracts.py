"""Provider-neutral context and retrieval contracts."""

from __future__ import annotations

from enum import Enum
from hashlib import sha256
import json
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

from spine.domain.common import ActorRef, DefinitionModel, SpineModel
from spine.domain.projections import (
    CanonicalAsOf,
    ProjectionActivation,
    ProjectionConfigVersion as DomainProjectionConfigVersion,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionReconciliationResult,
    ProjectionSnapshot,
    PublishedActivePublication,
)


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


class ProjectionNamespaceState(str, Enum):
    """Lifecycle of one provider-neutral physical namespace."""

    ACTIVE = "active"
    SHADOW = "shadow"
    REBUILDING = "rebuilding"
    FAILED = "failed"
    RETIRED = "retired"


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
    projection_kind: str = Field(
        default="context",
        min_length=1,
        max_length=64,
        pattern=_IDENTIFIER,
    )
    state: ProjectionNamespaceState = ProjectionNamespaceState.ACTIVE


class EvidenceReference(_ProjectionModel):
    """A structured, revision-specific pointer returned with projected data."""

    schema_version: Literal[
        "projection-evidence-reference:v1", "projection-evidence-reference:v2"
    ] = "projection-evidence-reference:v2"
    evidence_id: str = Field(min_length=1, max_length=128)
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str | None = Field(default=None, pattern=_DIGEST)
    revision_number: int | None = Field(default=None, ge=1)
    processing_receipt_id: UUID | None = None
    locator_contract_version: str | None = Field(default=None, min_length=1, max_length=128)
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
        if self.processing_receipt_id is not None:
            _require_non_zero(self.processing_receipt_id, "processing_receipt_id")
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
    namespace_digest: str | None = Field(default=None, pattern=_DIGEST)
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
    failure_code: ProjectionFailureCode | None = None
    workspace_id: UUID
    environment_id: UUID
    command_identity: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    command_digest: str | None = Field(default=None, pattern=_DIGEST)
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


class ProjectionRecoveryOperation(str, Enum):
    """Physical recovery operation reported to the publication seam."""

    CLEANUP = "cleanup"
    REPAIR = "repair"


class ProjectionRecoveryStatus(str, Enum):
    """Safe terminal or retryable state of a physical recovery attempt."""

    SUCCEEDED = "succeeded"
    REPLAYED = "replayed"
    RECOVERABLE = "recoverable"
    FAILED = "failed"
    FENCED = "fenced"


class ProjectionRecoveryFailureCode(str, Enum):
    """Failure vocabulary shared by cleanup and repair evidence."""

    INTEGRITY_CONFLICT = "integrity_conflict"
    CROSS_WORKSPACE = "cross_workspace"
    NAMESPACE_UNAVAILABLE = "namespace_unavailable"
    PHYSICAL_FAILURE = "physical_failure"
    REVISION_FENCED = "revision_fenced"
    TOMBSTONED_SOURCE = "tombstoned_source"


class ProjectionRecoveryEvidence(_ProjectionModel):
    """Content-free physical recovery evidence with candidate/source lineage."""

    schema_version: Literal["projection-recovery-evidence:v1"] = (
        "projection-recovery-evidence:v1"
    )
    operation: ProjectionRecoveryOperation
    workspace_id: UUID
    environment_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    namespace_digest: str = Field(pattern=_DIGEST)
    status: ProjectionRecoveryStatus
    source_revisions: tuple[ProjectionSourceRevision, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    failure_code: ProjectionRecoveryFailureCode | None = None

    @model_validator(mode="after")
    def validate_lineage(self) -> "ProjectionRecoveryEvidence":
        _require_non_zero(self.workspace_id, "workspace_id")
        _require_non_zero(self.environment_id, "environment_id")
        _require_non_zero(self.projection_run_id, "projection_run_id")
        _require_non_zero(self.projection_candidate_id, "projection_candidate_id")
        source_ids = [source.source_object_id for source in self.source_revisions]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("recovery source revisions must be unique")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("recovery artifact IDs must be unique")
        return self


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


class ContextOutcome(str, Enum):
    """The closed, disclosure-safe outcome set for Context Broker v1."""

    ALLOWED = "allowed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    DEGRADED = "degraded"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    INSUFFICIENT = "insufficient"
    ABSTAINED = "abstained"


class ContextReasonCode(str, Enum):
    """Bounded reason codes that never disclose provider or source details."""

    ALLOWED = "allowed"
    INVALID_TRUSTED_CONTEXT = "invalid_trusted_context"
    AUTHORIZATION_DENIED = "authorization_denied"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
    STALE_CONTEXT = "stale_context"
    DEGRADED_CONTEXT = "degraded_context"
    PARTIAL_CONTEXT = "partial_context"
    CANCELLED = "cancelled"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BUNDLE_UNAVAILABLE = "bundle_unavailable"
    PUBLICATION_UNAVAILABLE = "publication_unavailable"


class ContextProfileRef(DefinitionModel):
    """The only public identity needed to pin a Context Profile."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": "spine/context-profile-ref:v1"},
    )

    profile_id: UUID
    profile_version: int = Field(ge=1)
    profile_digest: str = Field(pattern=_DIGEST)

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: UUID) -> UUID:
        _require_non_zero(value, "profile_id")
        return value


class ContextBundleRef(DefinitionModel):
    """Opaque identity of the exact bundle issued for one retrieval."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": "spine/context-bundle-ref:v1"},
    )

    bundle_id: UUID
    bundle_version: int = Field(ge=1)
    bundle_digest: str = Field(pattern=_DIGEST)
    publication_pin_id: UUID | None = None

    @field_validator("bundle_id")
    @classmethod
    def validate_bundle_id(cls, value: UUID) -> UUID:
        _require_non_zero(value, "bundle_id")
        return value

    @field_validator("publication_pin_id")
    @classmethod
    def validate_publication_pin_id(cls, value: UUID | None) -> UUID | None:
        if value is not None:
            _require_non_zero(value, "publication_pin_id")
        return value


class ContextProfile(_ProjectionModel):
    """Server-approved retrieval policy resolved from a profile reference."""

    schema_version: Literal["context-profile:v1"] = "context-profile:v1"
    ref: ContextProfileRef
    retrieval_strategy: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER)
    max_items: int = Field(default=15, ge=1, le=100)
    projection_kind: str = Field(default="qa", pattern=_IDENTIFIER)
    require_current_publication: bool = False
    require_healthy_publication: bool = False


class PublicationPin(_ProjectionModel):
    """Immutable active-publication identity captured for one broker attempt."""

    schema_version: Literal["publication-pin:v1"] = "publication-pin:v1"
    pin_id: UUID
    pin_digest: str = Field(pattern=_DIGEST)
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    projection_snapshot: ProjectionSnapshot
    projection_activation: ProjectionActivation
    activation_generation: int = Field(ge=1)
    active_pointer_cas_version: int = Field(ge=1)
    canonical_boundary: CanonicalBoundary
    as_of: CanonicalAsOf
    projection_config_version: DomainProjectionConfigVersion
    freshness: ProjectionFreshness
    health: ProjectionHealth
    effective_reconciliation: ProjectionReconciliationResult | None = None

    @classmethod
    def from_publication(cls, publication: PublishedActivePublication) -> "PublicationPin":
        """Capture one resolver result without introducing a second authority."""

        identity = (
            f"spine:publication-pin:v1:{publication.workspace_id}:"
            f"{publication.environment_id}:{publication.projection_kind}:"
            f"{publication.projection_activation.projection_activation_id}:"
            f"{publication.activation_generation}"
        )
        pin_id = uuid5(NAMESPACE_URL, identity)
        canonical_boundary = CanonicalBoundary(
            boundary_id=publication.canonical_boundary_id,
            boundary_digest=publication.canonical_boundary_manifest_digest,
            accepted_through_generation=publication.accepted_through_generation,
        )
        payload = {
            "schema": "publication-pin:v1",
            "pin_id": str(pin_id),
            "publication": publication.model_dump(mode="json"),
            "canonical_boundary": canonical_boundary.model_dump(mode="json"),
        }
        pin_digest = sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(
            pin_id=pin_id,
            pin_digest=pin_digest,
            workspace_id=publication.workspace_id,
            environment_id=publication.environment_id,
            projection_kind=publication.projection_kind,
            projection_snapshot=publication.projection_snapshot,
            projection_activation=publication.projection_activation,
            activation_generation=publication.activation_generation,
            active_pointer_cas_version=publication.active_pointer_cas_version,
            canonical_boundary=canonical_boundary,
            as_of=publication.as_of,
            projection_config_version=publication.projection_config_version,
            freshness=publication.freshness,
            health=publication.health,
            effective_reconciliation=publication.effective_reconciliation,
        )

    @field_validator("pin_id", "workspace_id", "environment_id")
    @classmethod
    def validate_pin_ids(cls, value: UUID, info: object) -> UUID:
        _require_non_zero(value, getattr(info, "field_name", "identifier"))
        return value

    @model_validator(mode="after")
    def validate_lineage(self) -> "PublicationPin":
        if (
            self.projection_snapshot.workspace_id != self.workspace_id
            or self.projection_snapshot.environment_id != self.environment_id
            or self.projection_activation.workspace_id != self.workspace_id
            or self.projection_activation.environment_id != self.environment_id
            or self.projection_config_version.workspace_id != self.workspace_id
            or self.projection_config_version.environment_id != self.environment_id
            or self.projection_snapshot.projection_kind != self.projection_kind
            or self.projection_activation.projection_kind != self.projection_kind
            or self.projection_config_version.projection_kind != self.projection_kind
            or self.projection_snapshot.projection_snapshot_id
            != self.projection_activation.projection_snapshot_id
            or self.projection_activation.activation_generation != self.activation_generation
            or self.projection_snapshot.canonical_boundary_id
            != self.canonical_boundary.boundary_id
            or self.as_of.canonical_boundary_id != self.canonical_boundary.boundary_id
            or self.as_of.accepted_through_generation
            != self.canonical_boundary.accepted_through_generation
            or self.projection_snapshot.projection_config_version_id
            != self.projection_config_version.projection_config_version_id
        ):
            raise ValueError("publication pin lineage is inconsistent")
        if self.effective_reconciliation is not None and (
            self.effective_reconciliation.workspace_id != self.workspace_id
            or self.effective_reconciliation.environment_id != self.environment_id
            or self.effective_reconciliation.projection_kind != self.projection_kind
            or self.effective_reconciliation.projection_snapshot_id
            != self.projection_snapshot.projection_snapshot_id
            or self.effective_reconciliation.projection_activation_id
            != self.projection_activation.projection_activation_id
            or self.effective_reconciliation.activation_generation != self.activation_generation
        ):
            raise ValueError("publication pin reconciliation lineage is inconsistent")
        return self

    @property
    def publication_pin_id(self) -> UUID:
        """Stable audit-facing alias for the pin identity."""

        return self.pin_id

    @property
    def canonical_as_of(self) -> CanonicalAsOf:
        """Explicit alias used by attempt and bundle consumers."""

        return self.as_of


class TrustedContextDiagnostic(_ProjectionModel):
    """Content-free diagnostic metadata safe to return with any outcome."""

    schema_version: Literal["context-safe-diagnostic:v1"] = "context-safe-diagnostic:v1"
    retryable: StrictBool = False
    opaque_reference: UUID | None = None

    @field_validator("opaque_reference")
    @classmethod
    def validate_opaque_reference(cls, value: UUID | None) -> UUID | None:
        if value is not None:
            _require_non_zero(value, "opaque_reference")
        return value


class ContextRequest(_ProjectionModel):
    """Provider-neutral request with no client-controlled projection authority."""

    schema_version: Literal["context-request:v1"] = "context-request:v1"
    request_id: UUID
    workspace_id: UUID
    environment_id: UUID
    query: str = Field(min_length=1, max_length=16_384)
    profile_ref: ContextProfileRef
    trusted_request_context_ref: UUID
    authorization_context_ref: UUID
    limit: int = Field(default=15, ge=1, le=100)

    @field_validator(
        "request_id",
        "workspace_id",
        "environment_id",
        "trusted_request_context_ref",
        "authorization_context_ref",
    )
    @classmethod
    def validate_request_ids(cls, value: UUID, info: object) -> UUID:
        _require_non_zero(value, getattr(info, "field_name", "identifier"))
        return value


class ContextRetrievalResult(_ProjectionModel):
    """Content-free result from a retrieval port before bundle issuance."""

    schema_version: Literal["context-retrieval-result:v1"] = "context-retrieval-result:v1"
    outcome: ContextOutcome
    reason_code: ContextReasonCode
    diagnostic: TrustedContextDiagnostic = TrustedContextDiagnostic()
    publication_pin_id: UUID | None = None

    @field_validator("publication_pin_id")
    @classmethod
    def validate_publication_pin_id(cls, value: UUID | None) -> UUID | None:
        if value is not None:
            _require_non_zero(value, "publication_pin_id")
        return value

    @model_validator(mode="after")
    def validate_reason_for_outcome(self) -> "ContextRetrievalResult":
        if self.reason_code is not context_reason_for_outcome(self.outcome):
            raise ValueError("retrieval reason code does not match outcome")
        return self


class ContextResponse(_ProjectionModel):
    """Typed broker result; protected context is represented only by a bundle ref."""

    schema_version: Literal["context-response:v1"] = "context-response:v1"
    outcome: ContextOutcome
    context_bundle_ref: ContextBundleRef | None = None
    reason_code: ContextReasonCode
    diagnostic: TrustedContextDiagnostic = TrustedContextDiagnostic()
    publication_pin_id: UUID | None = None

    @field_validator("publication_pin_id")
    @classmethod
    def validate_publication_pin_id(cls, value: UUID | None) -> UUID | None:
        if value is not None:
            _require_non_zero(value, "publication_pin_id")
        return value

    @model_validator(mode="after")
    def validate_bundle_for_outcome(self) -> "ContextResponse":
        bundle_outcomes = {
            ContextOutcome.ALLOWED,
            ContextOutcome.DEGRADED,
            ContextOutcome.PARTIAL,
        }
        required_bundle_outcomes = {ContextOutcome.ALLOWED, ContextOutcome.PARTIAL}
        if self.outcome in required_bundle_outcomes and self.context_bundle_ref is None:
            raise ValueError("bundle outcomes require a context bundle reference")
        if self.outcome not in bundle_outcomes and self.context_bundle_ref is not None:
            raise ValueError("non-bundle outcomes cannot carry a context bundle reference")
        allowed_reasons = {
            ContextOutcome.DENIED: {
                ContextReasonCode.INVALID_TRUSTED_CONTEXT,
                ContextReasonCode.AUTHORIZATION_DENIED,
            },
            ContextOutcome.UNAVAILABLE: {
                ContextReasonCode.PROFILE_UNAVAILABLE,
                ContextReasonCode.RETRIEVAL_UNAVAILABLE,
                ContextReasonCode.BUNDLE_UNAVAILABLE,
                ContextReasonCode.PUBLICATION_UNAVAILABLE,
            },
            ContextOutcome.STALE: {ContextReasonCode.STALE_CONTEXT},
            ContextOutcome.DEGRADED: {ContextReasonCode.DEGRADED_CONTEXT},
            ContextOutcome.PARTIAL: {ContextReasonCode.PARTIAL_CONTEXT},
            ContextOutcome.CANCELLED: {ContextReasonCode.CANCELLED},
            ContextOutcome.INSUFFICIENT: {ContextReasonCode.INSUFFICIENT_EVIDENCE},
            ContextOutcome.ABSTAINED: {ContextReasonCode.INSUFFICIENT_EVIDENCE},
            ContextOutcome.ALLOWED: {ContextReasonCode.ALLOWED},
        }
        if self.reason_code not in allowed_reasons[self.outcome]:
            raise ValueError("response reason code does not match outcome")
        return self


def context_reason_for_outcome(outcome: ContextOutcome) -> ContextReasonCode:
    return {
        ContextOutcome.ALLOWED: ContextReasonCode.ALLOWED,
        ContextOutcome.DENIED: ContextReasonCode.AUTHORIZATION_DENIED,
        ContextOutcome.UNAVAILABLE: ContextReasonCode.RETRIEVAL_UNAVAILABLE,
        ContextOutcome.STALE: ContextReasonCode.STALE_CONTEXT,
        ContextOutcome.DEGRADED: ContextReasonCode.DEGRADED_CONTEXT,
        ContextOutcome.PARTIAL: ContextReasonCode.PARTIAL_CONTEXT,
        ContextOutcome.CANCELLED: ContextReasonCode.CANCELLED,
        ContextOutcome.INSUFFICIENT: ContextReasonCode.INSUFFICIENT_EVIDENCE,
        ContextOutcome.ABSTAINED: ContextReasonCode.INSUFFICIENT_EVIDENCE,
    }[outcome]


ContextDiagnostic = TrustedContextDiagnostic


ContextRequestV1 = ContextRequest
ContextResponseV1 = ContextResponse
ContextProfileRefV1 = ContextProfileRef
ContextBundleRefV1 = ContextBundleRef


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
    outcome: ContextOutcome | None = None


class ContextBroker(Protocol):
    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult: ...
