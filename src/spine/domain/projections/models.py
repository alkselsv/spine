"""Immutable, tenant-scoped projection ledgers.

These models describe the canonical evidence owned by PostgreSQL.  They do
not describe Cognee nodes or physical indexes; those remain behind the
``ContextProjection`` port.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from spine.domain.common import DefinitionModel, FrozenDict, ValidatedDefinitionModel


_DIGEST = r"^[0-9a-f]{64}$"
_IDENTIFIER = r"^[a-z][a-z0-9_.:-]{0,63}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
PROJECTION_KIND_PATTERN = _IDENTIFIER
ProjectionKind = Annotated[str, Field(pattern=PROJECTION_KIND_PATTERN)]

ONTOLOGY_DEFINITION_SCHEMA = "ontology-definition:r1"
PROJECTION_CONFIG_SCHEMA = "projection-config:r1"
PROJECTION_EVIDENCE_SCHEMA = "projection-evidence:r1"


def _require_ids(value: object, *names: str) -> None:
    for name in names:
        item = getattr(value, name)
        if not isinstance(item, UUID) or item.int == 0:
            raise ValueError(f"{name} must be a non-zero UUID")


def _require_time(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _digest_payload(schema: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"schema": schema, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _freeze_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return FrozenDict(value)


def _validate_definition_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the deliberately small R1 ontology envelope."""

    if value.get("schema_version") != ONTOLOGY_DEFINITION_SCHEMA:
        raise ValueError("ontology definition schema is not registered")
    entity_types = value.get("entity_types")
    relation_types = value.get("relation_types")
    if not isinstance(entity_types, (list, tuple)) or not entity_types:
        raise ValueError("ontology definition must declare entity types")
    if not isinstance(relation_types, (list, tuple)):
        raise ValueError("ontology definition must declare relation types")
    entity_names: set[str] = set()
    for entity in entity_types:
        if not isinstance(entity, Mapping) or not isinstance(entity.get("name"), str):
            raise ValueError("ontology entity type is invalid")
        name = entity["name"]
        if not isinstance(name, str) or not re.fullmatch(_IDENTIFIER, name):
            raise ValueError("ontology entity type name is invalid")
        if name in entity_names:
            raise ValueError("ontology entity type names must be unique")
        entity_names.add(name)
    relation_names: set[str] = set()
    for relation in relation_types:
        if not isinstance(relation, Mapping):
            raise ValueError("ontology relation type is invalid")
        name = relation.get("name")
        source = relation.get("source_entity_type")
        target = relation.get("target_entity_type")
        if (
            not isinstance(name, str)
            or not re.fullmatch(_IDENTIFIER, name)
            or not isinstance(source, str)
            or not isinstance(target, str)
            or source not in entity_names
            or target not in entity_names
        ):
            raise ValueError("ontology relation type is invalid")
        if name in relation_names:
            raise ValueError("ontology relation type names must be unique")
        relation_names.add(name)
    return value


class ProjectionOperation(str, Enum):
    BUILD = "build"
    CATCH_UP = "catch_up"
    REBUILD = "rebuild"
    ROLLBACK = "rollback"


class ProjectionCandidateState(str, Enum):
    OPEN = "open"
    SEALED = "sealed"


class ProjectionRunState(str, Enum):
    REQUESTED = "requested"
    BUILDING = "building"
    VALIDATING = "validating"
    COMPLETE = "complete"
    FAILED = "failed"
    ABANDONED = "abandoned"


class ProjectionDiagnostic(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-safe-diagnostic:v1"] = "projection-safe-diagnostic:v1"
    code: str = Field(pattern=_IDENTIFIER)
    attempt: int | None = Field(default=None, ge=1)
    item_count: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    opaque_detail_ref: UUID | None = None

    @model_validator(mode="after")
    def validate_reference(self) -> "ProjectionDiagnostic":
        if self.opaque_detail_ref is not None:
            _require_ids(self, "opaque_detail_ref")
        return self


class OntologyVersion(DefinitionModel):
    """An immutable, publication-eligible R1 ontology definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ontology_version_id: UUID
    workspace_id: UUID
    environment_id: UUID
    ontology_name: str = Field(pattern=_IDENTIFIER)
    version: int = Field(ge=1)
    predecessor_ontology_version_id: UUID | None = None
    definition_schema_version: Literal[ONTOLOGY_DEFINITION_SCHEMA] = ONTOLOGY_DEFINITION_SCHEMA
    definition_payload: Mapping[str, Any]
    definition_digest: str = Field(pattern=_DIGEST)
    publication_eligible: bool = False
    publication_decision_reference: str = Field(min_length=1, max_length=256)
    publisher_authority_reference: str = Field(min_length=1, max_length=256)
    audit_event_reference: str = Field(min_length=1, max_length=256)
    published_at: datetime

    _freeze = field_validator("definition_payload", mode="before")(_freeze_payload)

    @model_validator(mode="after")
    def validate_definition(self) -> "OntologyVersion":
        _require_ids(self, "ontology_version_id", "workspace_id", "environment_id")
        if self.predecessor_ontology_version_id is not None:
            _require_ids(self, "predecessor_ontology_version_id")
        _require_time(self.published_at, "published_at")
        _validate_definition_payload(self.definition_payload)
        expected = _digest_payload(ONTOLOGY_DEFINITION_SCHEMA, self.definition_payload)
        if self.definition_digest != expected:
            raise ValueError("ontology definition digest does not match payload")
        return self


class ProjectionConfigVersion(DefinitionModel):
    """Immutable processing inputs pinned to one published ontology."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_config_version_id: UUID
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    version: int = Field(ge=1)
    predecessor_projection_config_version_id: UUID | None = None
    ontology_version_id: UUID
    parser_contract_version: str = Field(pattern=_VERSION)
    locator_contract_version: str = Field(pattern=_VERSION)
    extraction_contract_version: str = Field(pattern=_VERSION)
    embedding_profile_version: str = Field(pattern=_VERSION)
    adapter_contract_version: str = Field(pattern=_VERSION)
    config_schema_version: Literal[PROJECTION_CONFIG_SCHEMA] = PROJECTION_CONFIG_SCHEMA
    config_payload: Mapping[str, Any] = Field(default_factory=dict)
    config_digest: str = Field(pattern=_DIGEST)
    created_at: datetime

    _freeze = field_validator("config_payload", mode="before")(_freeze_payload)

    @model_validator(mode="after")
    def validate_config(self) -> "ProjectionConfigVersion":
        _require_ids(
            self,
            "projection_config_version_id",
            "workspace_id",
            "environment_id",
            "ontology_version_id",
        )
        if self.predecessor_projection_config_version_id is not None:
            _require_ids(self, "predecessor_projection_config_version_id")
        _require_time(self.created_at, "created_at")
        expected = _digest_payload(PROJECTION_CONFIG_SCHEMA, self.config_payload)
        if self.config_digest != expected:
            raise ValueError("projection config digest does not match payload")
        return self


class ProjectionRun(DefinitionModel):
    """One immutable projection operation and its run-owned candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_run_id: UUID
    projection_candidate_id: UUID
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    operation: ProjectionOperation
    projection_config_version_id: UUID
    canonical_boundary_id: UUID
    predecessor_snapshot_id: UUID | None = None
    command_identity: str = Field(pattern=_IDENTIFIER)
    command_digest: str = Field(pattern=_DIGEST)
    idempotency_receipt_id: UUID
    candidate_state: ProjectionCandidateState = ProjectionCandidateState.OPEN
    candidate_evidence_digest: str | None = Field(default=None, pattern=_DIGEST)
    candidate_sealed_at: datetime | None = None
    started_at: datetime

    @property
    def candidate_id(self) -> UUID:
        return self.projection_candidate_id

    @model_validator(mode="after")
    def validate_candidate_state(self) -> "ProjectionRun":
        _require_ids(
            self,
            "projection_run_id",
            "projection_candidate_id",
            "workspace_id",
            "environment_id",
            "projection_config_version_id",
            "canonical_boundary_id",
            "idempotency_receipt_id",
        )
        if self.predecessor_snapshot_id is not None:
            _require_ids(self, "predecessor_snapshot_id")
        _require_time(self.started_at, "started_at")
        if self.candidate_state is ProjectionCandidateState.OPEN:
            if self.candidate_evidence_digest is not None or self.candidate_sealed_at is not None:
                raise ValueError("an open candidate cannot have seal evidence")
        else:
            if self.candidate_evidence_digest is None or self.candidate_sealed_at is None:
                raise ValueError("a sealed candidate requires evidence digest and seal time")
            _require_time(self.candidate_sealed_at, "candidate_sealed_at")
        return self


class ProjectionRunTransition(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_run_transition_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    workspace_id: UUID
    environment_id: UUID
    predecessor_transition_id: UUID | None = None
    state: ProjectionRunState
    attempt: int = Field(ge=1)
    reason_code: str | None = Field(default=None, pattern=_IDENTIFIER)
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_transition(self) -> "ProjectionRunTransition":
        _require_ids(
            self,
            "projection_run_transition_id",
            "projection_run_id",
            "projection_candidate_id",
            "workspace_id",
            "environment_id",
        )
        if self.predecessor_transition_id is not None:
            _require_ids(self, "predecessor_transition_id")
        _require_time(self.occurred_at, "occurred_at")
        return self


class ProjectionPartition(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_partition_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    workspace_id: UUID
    environment_id: UUID
    security_domain_reference: str = Field(min_length=1, max_length=256)
    security_domain_version: str = Field(pattern=_VERSION)
    partition_identity: str = Field(pattern=_IDENTIFIER)
    expected_command_identity: str = Field(pattern=_IDENTIFIER)
    expected_command_digest: str = Field(pattern=_DIGEST)
    created_at: datetime

    @model_validator(mode="after")
    def validate_partition(self) -> "ProjectionPartition":
        _require_ids(
            self,
            "projection_partition_id",
            "projection_run_id",
            "projection_candidate_id",
            "workspace_id",
            "environment_id",
        )
        _require_time(self.created_at, "created_at")
        return self


class ProjectionReceipt(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_receipt_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    projection_partition_id: UUID
    workspace_id: UUID
    environment_id: UUID
    command_identity: str = Field(pattern=_IDENTIFIER)
    command_digest: str = Field(pattern=_DIGEST)
    artifact_kind: str = Field(pattern=_IDENTIFIER)
    artifact_reference: str = Field(min_length=1, max_length=512)
    artifact_integrity_digest: str = Field(pattern=_DIGEST)
    counts: Mapping[str, int] = Field(default_factory=dict)
    warnings: tuple[ProjectionDiagnostic, ...] = ()
    failures: tuple[ProjectionDiagnostic, ...] = ()
    implementation_schema_version: str = Field(pattern=_VERSION)
    implementation_metadata: Mapping[str, Any] = Field(default_factory=dict)
    idempotency_receipt_id: UUID
    completed_at: datetime

    _freeze_counts = field_validator("counts", mode="before")(_freeze_payload)
    _freeze_metadata = field_validator("implementation_metadata", mode="before")(_freeze_payload)

    @model_validator(mode="after")
    def validate_receipt(self) -> "ProjectionReceipt":
        _require_ids(
            self,
            "projection_receipt_id",
            "projection_run_id",
            "projection_candidate_id",
            "projection_partition_id",
            "workspace_id",
            "environment_id",
            "idempotency_receipt_id",
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in self.counts.values()):
            raise ValueError("projection receipt counts must be non-negative integers")
        _require_time(self.completed_at, "completed_at")
        return self


class SourceProjectionReference(DefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_projection_reference_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    projection_receipt_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    processing_receipt_id: UUID | None = None
    segment_id: str = Field(pattern=_IDENTIFIER)
    locator_schema_version: str = Field(pattern=_VERSION)
    locator_reference: str = Field(min_length=1, max_length=512)
    locator_digest: str = Field(pattern=_DIGEST)
    projected_kind: str = Field(pattern=_IDENTIFIER)
    projected_object_id: str = Field(min_length=1, max_length=256)
    projection_config_version_id: UUID
    security_domain_reference: str = Field(min_length=1, max_length=256)
    created_at: datetime

    @model_validator(mode="after")
    def validate_reference(self) -> "SourceProjectionReference":
        _require_ids(
            self,
            "source_projection_reference_id",
            "projection_run_id",
            "projection_candidate_id",
            "projection_receipt_id",
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
            "projection_config_version_id",
        )
        if self.processing_receipt_id is not None:
            _require_ids(self, "processing_receipt_id")
        _require_time(self.created_at, "created_at")
        return self


class ProjectionValidationDecision(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


class ProjectionValidation(ValidatedDefinitionModel):
    """Immutable evidence for one publication gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_validation_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    workspace_id: UUID
    environment_id: UUID
    gate_kind: str = Field(pattern=_IDENTIFIER)
    gate_version: str = Field(pattern=_VERSION)
    input_digest: str = Field(pattern=_DIGEST)
    result_digest: str = Field(pattern=_DIGEST)
    decision: ProjectionValidationDecision
    metrics_schema_version: str = Field(pattern=_VERSION)
    metrics_payload: Mapping[str, Any] = Field(default_factory=dict)
    evaluator_reference: str = Field(min_length=1, max_length=256)
    evaluated_at: datetime

    _freeze_metrics = field_validator("metrics_payload", mode="before")(_freeze_payload)

    @model_validator(mode="after")
    def validate_validation(self) -> "ProjectionValidation":
        _require_ids(
            self,
            "projection_validation_id",
            "projection_run_id",
            "projection_candidate_id",
            "workspace_id",
            "environment_id",
        )
        _require_time(self.evaluated_at, "evaluated_at")
        return self


class ProjectionSnapshotState(str, Enum):
    ASSEMBLING = "assembling"
    FINALIZED = "finalized"


class ProjectionSnapshot(ValidatedDefinitionModel):
    """Immutable logical manifest of a sealed projection candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_snapshot_id: UUID
    projection_run_id: UUID
    projection_candidate_id: UUID
    workspace_id: UUID
    environment_id: UUID
    snapshot_state: ProjectionSnapshotState = ProjectionSnapshotState.ASSEMBLING
    projection_kind: str = Field(pattern=_IDENTIFIER)
    projection_config_version_id: UUID
    canonical_boundary_id: UUID
    predecessor_snapshot_id: UUID | None = None
    candidate_evidence_digest: str = Field(pattern=_DIGEST)
    manifest_digest: str = Field(pattern=_DIGEST)
    member_count: int = Field(ge=0)
    created_at: datetime

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ProjectionSnapshot":
        _require_ids(
            self,
            "projection_snapshot_id",
            "projection_run_id",
            "projection_candidate_id",
            "workspace_id",
            "environment_id",
            "projection_config_version_id",
            "canonical_boundary_id",
        )
        if self.predecessor_snapshot_id is not None:
            _require_ids(self, "predecessor_snapshot_id")
        _require_time(self.created_at, "created_at")
        if self.snapshot_state is ProjectionSnapshotState.FINALIZED and self.member_count < 0:
            raise ValueError("finalized snapshot member count is invalid")
        return self


class ProjectionSnapshotMemberDisposition(str, Enum):
    INCLUDED = "included"
    TOMBSTONED = "tombstoned"
    EXCLUDED = "excluded"


class ProjectionSnapshotMember(ValidatedDefinitionModel):
    """One exhaustive, immutable accounting entry for a boundary member."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_snapshot_member_id: UUID
    projection_snapshot_id: UUID
    canonical_boundary_id: UUID
    canonical_boundary_member_id: UUID
    workspace_id: UUID
    environment_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    disposition: ProjectionSnapshotMemberDisposition
    exclusion_code: str | None = Field(default=None, pattern=_IDENTIFIER)
    projection_partition_id: UUID | None = None
    projection_receipt_id: UUID | None = None

    @model_validator(mode="after")
    def validate_member(self) -> "ProjectionSnapshotMember":
        _require_ids(
            self,
            "projection_snapshot_member_id",
            "projection_snapshot_id",
            "canonical_boundary_id",
            "canonical_boundary_member_id",
            "workspace_id",
            "environment_id",
            "source_object_id",
            "source_revision_id",
        )
        for field_name in ("projection_partition_id", "projection_receipt_id"):
            value = getattr(self, field_name)
            if value is not None:
                _require_ids(self, field_name)
        if self.disposition is ProjectionSnapshotMemberDisposition.INCLUDED:
            if self.exclusion_code is not None or self.projection_partition_id is None or self.projection_receipt_id is None:
                raise ValueError("included snapshot members require a partition and receipt")
        elif self.disposition is ProjectionSnapshotMemberDisposition.TOMBSTONED:
            if self.exclusion_code is not None or self.projection_partition_id is not None or self.projection_receipt_id is not None:
                raise ValueError("tombstoned snapshot members cannot reference content artifacts")
        elif self.exclusion_code is None or self.projection_partition_id is not None or self.projection_receipt_id is not None:
            raise ValueError("excluded snapshot members require an exclusion code only")
        return self


class ProjectionReconciliationOutcome(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ProjectionReconciliationResult(ValidatedDefinitionModel):
    """Append-only runtime health evidence for one activated snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reconciliation_result_id: UUID
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    projection_snapshot_id: UUID
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    attempt_identity: str = Field(pattern=_IDENTIFIER)
    attempt_digest: str = Field(pattern=_DIGEST)
    predecessor_result_id: UUID | None = None
    outcome: ProjectionReconciliationOutcome
    findings_schema_version: str = Field(pattern=_VERSION)
    findings_payload: Mapping[str, Any] = Field(default_factory=dict)
    findings_digest: str = Field(pattern=_DIGEST)
    artifact_evidence_reference: str = Field(min_length=1, max_length=512)
    artifact_evidence_digest: str = Field(pattern=_DIGEST)
    idempotency_receipt_id: UUID
    audit_event_reference: str = Field(min_length=1, max_length=256)
    checked_at: datetime

    _freeze_findings = field_validator("findings_payload", mode="before")(_freeze_payload)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> "ProjectionReconciliationResult":
        _require_ids(
            self,
            "reconciliation_result_id",
            "workspace_id",
            "environment_id",
            "projection_snapshot_id",
            "projection_activation_id",
            "idempotency_receipt_id",
        )
        if self.predecessor_result_id is not None:
            _require_ids(self, "predecessor_result_id")
        _require_time(self.checked_at, "checked_at")
        if self.findings_digest != projection_reconciliation_findings_digest(
            findings_schema_version=self.findings_schema_version,
            findings_payload=self.findings_payload,
        ):
            raise ValueError("reconciliation findings digest does not match payload")
        return self


class ProjectionActivationKind(str, Enum):
    PUBLISH = "publish"
    ROLLBACK = "rollback"


class ProjectionActivation(ValidatedDefinitionModel):
    """Append-only PostgreSQL publication history for one snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_activation_id: UUID
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    projection_snapshot_id: UUID
    predecessor_snapshot_id: UUID | None = None
    activation_generation: int = Field(ge=1)
    activation_kind: ProjectionActivationKind
    command_identity: str = Field(min_length=1, max_length=256)
    command_digest: str = Field(pattern=_DIGEST)
    idempotency_receipt_id: UUID
    audit_event_id: UUID
    activated_at: datetime

    @model_validator(mode="after")
    def validate_activation(self) -> "ProjectionActivation":
        _require_ids(
            self,
            "projection_activation_id",
            "workspace_id",
            "environment_id",
            "projection_snapshot_id",
            "idempotency_receipt_id",
            "audit_event_id",
        )
        if self.predecessor_snapshot_id is not None:
            _require_ids(self, "predecessor_snapshot_id")
        _require_time(self.activated_at, "activated_at")
        return self


class ActiveProjection(ValidatedDefinitionModel):
    """The single mutable PostgreSQL pointer for a projection kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    projection_snapshot_id: UUID
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    cas_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_pointer(self) -> "ActiveProjection":
        _require_ids(
            self,
            "workspace_id",
            "environment_id",
            "projection_snapshot_id",
            "projection_activation_id",
        )
        return self


class ProjectionDriftDetectedBy(str, Enum):
    ACTIVATION = "activation"
    CANONICAL_ACCEPTANCE = "canonical_acceptance"


class ProjectionDrift(ValidatedDefinitionModel):
    """Immutable evidence that canonical state advanced beyond a publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_drift_id: UUID
    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    projection_snapshot_id: UUID
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    canonical_acceptance_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    accepted_generation: int = Field(ge=1)
    detected_by: ProjectionDriftDetectedBy
    detected_at: datetime

    @model_validator(mode="after")
    def validate_drift(self) -> "ProjectionDrift":
        _require_ids(
            self,
            "projection_drift_id",
            "workspace_id",
            "environment_id",
            "projection_snapshot_id",
            "projection_activation_id",
            "canonical_acceptance_id",
            "source_object_id",
            "source_revision_id",
        )
        if self.accepted_generation <= 0:
            raise ValueError("accepted_generation must be positive")
        _require_time(self.detected_at, "detected_at")
        return self


class CanonicalAsOf(ValidatedDefinitionModel):
    """Server-selected canonical cut used by active publication serving."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_boundary_id: UUID
    accepted_through_generation: int = Field(ge=0)
    boundary_time: datetime

    @model_validator(mode="after")
    def validate_as_of(self) -> "CanonicalAsOf":
        _require_ids(self, "canonical_boundary_id")
        _require_time(self.boundary_time, "boundary_time")
        return self


class ProjectionFreshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"


class ProjectionHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"


class PublishedActivePublication(ValidatedDefinitionModel):
    """One internally consistent active-publication resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    projection_snapshot: ProjectionSnapshot
    projection_activation: ProjectionActivation
    activation_generation: int = Field(ge=1)
    active_pointer_cas_version: int = Field(ge=1)
    canonical_boundary_id: UUID
    canonical_boundary_manifest_digest: str = Field(pattern=_DIGEST)
    accepted_through_generation: int = Field(ge=0)
    canonical_boundary_membership_ref: str = Field(min_length=1, max_length=512)
    as_of: CanonicalAsOf
    projection_config_version: ProjectionConfigVersion
    freshness: ProjectionFreshness
    health: ProjectionHealth
    effective_reconciliation: ProjectionReconciliationResult | None = None

    @model_validator(mode="after")
    def validate_publication(self) -> "PublishedActivePublication":
        _require_ids(self, "workspace_id", "environment_id", "canonical_boundary_id")
        if (
            self.projection_snapshot.projection_snapshot_id
            != self.projection_activation.projection_snapshot_id
            or self.projection_activation.activation_generation != self.activation_generation
            or self.projection_snapshot.canonical_boundary_id != self.canonical_boundary_id
            or self.as_of.canonical_boundary_id != self.canonical_boundary_id
            or self.as_of.accepted_through_generation != self.accepted_through_generation
        ):
            raise ValueError("active publication lineage is inconsistent")
        if self.effective_reconciliation is not None and (
            self.effective_reconciliation.projection_snapshot_id
            != self.projection_snapshot.projection_snapshot_id
            or self.effective_reconciliation.projection_activation_id
            != self.projection_activation.projection_activation_id
            or self.effective_reconciliation.activation_generation
            != self.activation_generation
        ):
            raise ValueError("reconciliation lineage is inconsistent")
        return self


class UnpublishedActivePublication(ValidatedDefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    status: Literal["unpublished"] = "unpublished"

    @model_validator(mode="after")
    def validate_scope(self) -> "UnpublishedActivePublication":
        _require_ids(self, "workspace_id", "environment_id")
        return self


class UnavailableActivePublication(ValidatedDefinitionModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    projection_kind: str = Field(pattern=_IDENTIFIER)
    status: Literal["unavailable"] = "unavailable"
    reason_code: str = Field(pattern=_IDENTIFIER)

    @model_validator(mode="after")
    def validate_scope(self) -> "UnavailableActivePublication":
        _require_ids(self, "workspace_id", "environment_id")
        return self


def projection_snapshot_manifest_digest(
    snapshot: ProjectionSnapshot,
    members: tuple[ProjectionSnapshotMember, ...],
) -> str:
    """Hash the frozen candidate evidence and canonical ordered membership."""

    ordered = tuple(sorted(members, key=lambda member: member.canonical_boundary_member_id.hex))
    if len({member.canonical_boundary_member_id for member in ordered}) != len(ordered):
        raise ValueError("snapshot members must have unique boundary member IDs")
    payload = {
        "schema": "projection-snapshot-manifest:r1",
        "snapshot_id": str(snapshot.projection_snapshot_id),
        "run_id": str(snapshot.projection_run_id),
        "candidate_id": str(snapshot.projection_candidate_id),
        "workspace_id": str(snapshot.workspace_id),
        "environment_id": str(snapshot.environment_id),
        "projection_kind": snapshot.projection_kind,
        "config_id": str(snapshot.projection_config_version_id),
        "boundary_id": str(snapshot.canonical_boundary_id),
        "candidate_evidence_digest": snapshot.candidate_evidence_digest,
        "members": [
            {
                "boundary_member_id": str(member.canonical_boundary_member_id),
                "source_object_id": str(member.source_object_id),
                "source_revision_id": str(member.source_revision_id),
                "disposition": member.disposition.value,
                "exclusion_code": member.exclusion_code,
                "partition_id": str(member.projection_partition_id) if member.projection_partition_id else None,
                "receipt_id": str(member.projection_receipt_id) if member.projection_receipt_id else None,
            }
            for member in ordered
        ],
    }
    return _digest_payload("projection-snapshot-manifest:r1", payload)


def projection_reconciliation_findings_digest(
    *, findings_schema_version: str, findings_payload: Mapping[str, Any]
) -> str:
    return _digest_payload(
        "projection-reconciliation-findings:r1",
        {"schema_version": findings_schema_version, "payload": findings_payload},
    )


def projection_evidence_digest(
    *,
    partitions: tuple[ProjectionPartition, ...],
    receipts: tuple[ProjectionReceipt, ...],
    references: tuple[SourceProjectionReference, ...],
    transitions: tuple[ProjectionRunTransition, ...] = (),
    validations: tuple[ProjectionValidation, ...] = (),
) -> str:
    """Hash the ordered, content-free evidence set for a candidate."""

    values: list[dict[str, Any]] = []
    for kind, records in (
        ("partition", partitions),
        ("receipt", receipts),
        ("reference", references),
        ("transition", transitions),
        ("validation", validations),
    ):
        for record in records:
            identifier = next(
                value for name, value in record.model_dump(mode="python").items() if name.endswith("_id")
            )
            values.append(
                {
                    "evidence_kind": kind,
                    "evidence_id": str(identifier),
                    "schema": record.__class__.__name__,
                    "digest": hashlib.sha256(
                        json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )
    values.sort(key=lambda value: (value["evidence_kind"], value["evidence_id"]))
    return _digest_payload(PROJECTION_EVIDENCE_SCHEMA, {"items": values})


__all__ = [
    "ONTOLOGY_DEFINITION_SCHEMA",
    "PROJECTION_CONFIG_SCHEMA",
    "PROJECTION_EVIDENCE_SCHEMA",
    "PROJECTION_KIND_PATTERN",
    "ProjectionKind",
    "OntologyVersion",
    "ActiveProjection",
    "CanonicalAsOf",
    "ProjectionCandidateState",
    "ProjectionConfigVersion",
    "ProjectionDiagnostic",
    "ProjectionActivation",
    "ProjectionActivationKind",
    "ProjectionDrift",
    "ProjectionDriftDetectedBy",
    "ProjectionOperation",
    "ProjectionPartition",
    "ProjectionReceipt",
    "ProjectionRun",
    "ProjectionRunState",
    "ProjectionRunTransition",
    "ProjectionReconciliationOutcome",
    "ProjectionReconciliationResult",
    "ProjectionSnapshot",
    "ProjectionSnapshotMember",
    "ProjectionSnapshotMemberDisposition",
    "ProjectionSnapshotState",
    "ProjectionValidation",
    "ProjectionValidationDecision",
    "ProjectionFreshness",
    "ProjectionHealth",
    "PublishedActivePublication",
    "UnpublishedActivePublication",
    "UnavailableActivePublication",
    "SourceProjectionReference",
    "projection_evidence_digest",
    "projection_reconciliation_findings_digest",
    "projection_snapshot_manifest_digest",
]
