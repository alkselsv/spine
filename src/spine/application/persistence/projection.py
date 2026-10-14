"""Typed persistence ports for the Issue #87 projection ledgers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from spine.application.persistence.command_digest import CommandDigest, digest_command
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    OpaqueResultReference,
)
from spine.domain.projections import (
    OntologyVersion,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionConfigVersion,
    ProjectionDrift,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionReconciliationResult,
    ProjectionRun,
    ProjectionRunState,
    ProjectionRunTransition,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    PublishedActivePublication,
    ProjectionValidation,
    SourceProjectionReference,
    UnavailableActivePublication,
    UnpublishedActivePublication,
)


PROJECTION_RUN_SCHEMA_VERSION = 1
PROJECTION_RECEIPT_SCHEMA_VERSION = 1
PROJECTION_SEAL_SCHEMA_VERSION = 1
PROJECTION_RESULT_SCHEMA_VERSION = 1
PROJECTION_VALIDATION_SCHEMA_VERSION = 1
PROJECTION_SNAPSHOT_SCHEMA_VERSION = 1
PROJECTION_RECONCILIATION_SCHEMA_VERSION = 1
PROJECTION_ACTIVATION_SCHEMA_VERSION = 1
PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE = "projection.activated"
PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE = "projection.catch_up_requested"
PROJECTION_OUTBOX_SCHEMA_VERSION = 1


class ProjectionActivatedOutboxPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_activation_id: UUID
    projection_snapshot_id: UUID
    projection_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    activation_generation: int = Field(ge=1)
    activation_kind: ProjectionActivationKind
    canonical_boundary_id: UUID
    accepted_through_generation: int = Field(ge=0)
    drift_count: int = Field(ge=0)
    audit_event_id: UUID


class ProjectionCatchUpRequestedOutboxPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_drift_id: UUID
    projection_activation_id: UUID
    projection_kind: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    activation_generation: int = Field(ge=1)
    canonical_acceptance_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    accepted_generation: int = Field(ge=1)


def register_projection_outbox_events(registry: object) -> None:
    """Add the Issue #89 event schemas to an adapter-owned registry."""

    register = getattr(registry, "register")
    register(
        event_type=PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        payload_type=ProjectionActivatedOutboxPayload,
    )
    register(
        event_type=PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
        schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
        payload_type=ProjectionCatchUpRequestedOutboxPayload,
    )


@dataclass(frozen=True, slots=True)
class ProjectionRunCommand:
    run: ProjectionRun
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(cls, *, run: ProjectionRun, idempotency_key: IdempotencyKey) -> "ProjectionRunCommand":
        digest = digest_command(
            operation=PersistenceOperation("projection_run"),
            operation_schema_version=PROJECTION_RUN_SCHEMA_VERSION,
            payload={
                "run_id": run.projection_run_id,
                "candidate_id": run.projection_candidate_id,
                "projection_kind": run.projection_kind,
                "operation": run.operation,
                "projection_config_version_id": run.projection_config_version_id,
                "canonical_boundary_id": run.canonical_boundary_id,
                "predecessor_snapshot_id": run.predecessor_snapshot_id,
                "command_identity": run.command_identity,
                "command_digest": run.command_digest,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(run=run, idempotency_key=idempotency_key, digest=digest)


@dataclass(frozen=True, slots=True)
class ProjectionReceiptCommand:
    receipt: ProjectionReceipt
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls, *, receipt: ProjectionReceipt, idempotency_key: IdempotencyKey
    ) -> "ProjectionReceiptCommand":
        digest = digest_command(
            operation=PersistenceOperation("projection_receipt"),
            operation_schema_version=PROJECTION_RECEIPT_SCHEMA_VERSION,
            payload={
                "receipt_id": receipt.projection_receipt_id,
                "run_id": receipt.projection_run_id,
                "candidate_id": receipt.projection_candidate_id,
                "partition_id": receipt.projection_partition_id,
                "command_identity": receipt.command_identity,
                "command_digest": receipt.command_digest,
                "artifact_kind": receipt.artifact_kind,
                "artifact_reference": receipt.artifact_reference,
                "artifact_integrity_digest": receipt.artifact_integrity_digest,
                "counts": receipt.counts,
                "warnings": receipt.warnings,
                "failures": receipt.failures,
                "implementation_schema_version": receipt.implementation_schema_version,
                "implementation_metadata": receipt.implementation_metadata,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(receipt=receipt, idempotency_key=idempotency_key, digest=digest)


@dataclass(frozen=True, slots=True)
class ProjectionCandidateSealCommand:
    projection_run_id: UUID
    projection_candidate_id: UUID
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls,
        *,
        projection_run_id: UUID,
        projection_candidate_id: UUID,
        idempotency_key: IdempotencyKey,
    ) -> "ProjectionCandidateSealCommand":
        digest = digest_command(
            operation=PersistenceOperation("projection_candidate_seal"),
            operation_schema_version=PROJECTION_SEAL_SCHEMA_VERSION,
            payload={
                "projection_run_id": projection_run_id,
                "projection_candidate_id": projection_candidate_id,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            projection_run_id=projection_run_id,
            projection_candidate_id=projection_candidate_id,
            idempotency_key=idempotency_key,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class ProjectionValidationCommand:
    validation: ProjectionValidation
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls, *, validation: ProjectionValidation, idempotency_key: IdempotencyKey
    ) -> "ProjectionValidationCommand":
        digest = digest_command(
            operation=PersistenceOperation("projection_validation"),
            operation_schema_version=PROJECTION_VALIDATION_SCHEMA_VERSION,
            payload={
                "validation_id": validation.projection_validation_id,
                "run_id": validation.projection_run_id,
                "candidate_id": validation.projection_candidate_id,
                "gate_kind": validation.gate_kind,
                "gate_version": validation.gate_version,
                "input_digest": validation.input_digest,
                "result_digest": validation.result_digest,
                "decision": validation.decision,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(validation=validation, idempotency_key=idempotency_key, digest=digest)


@dataclass(frozen=True, slots=True)
class ProjectionSnapshotCommand:
    snapshot: ProjectionSnapshot
    members: tuple[ProjectionSnapshotMember, ...]
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls,
        *,
        snapshot: ProjectionSnapshot,
        members: tuple[ProjectionSnapshotMember, ...],
        idempotency_key: IdempotencyKey,
    ) -> "ProjectionSnapshotCommand":
        digest = digest_command(
            operation=PersistenceOperation("projection_snapshot"),
            operation_schema_version=PROJECTION_SNAPSHOT_SCHEMA_VERSION,
            payload={
                "snapshot_id": snapshot.projection_snapshot_id,
                "run_id": snapshot.projection_run_id,
                "candidate_id": snapshot.projection_candidate_id,
                "candidate_evidence_digest": snapshot.candidate_evidence_digest,
                "manifest_digest": snapshot.manifest_digest,
                "member_ids": tuple(member.projection_snapshot_member_id for member in members),
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(snapshot=snapshot, members=members, idempotency_key=idempotency_key, digest=digest)


ProjectionSnapshotAssemblyCommand = ProjectionSnapshotCommand


@dataclass(frozen=True, slots=True)
class ProjectionReconciliationCommand:
    result: ProjectionReconciliationResult
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls, *, result: ProjectionReconciliationResult, idempotency_key: IdempotencyKey
    ) -> "ProjectionReconciliationCommand":
        digest = digest_command(
            operation=PersistenceOperation("projection_reconciliation"),
            operation_schema_version=PROJECTION_RECONCILIATION_SCHEMA_VERSION,
            payload={
                "result_id": result.reconciliation_result_id,
                "snapshot_id": result.projection_snapshot_id,
                "activation_id": result.projection_activation_id,
                "activation_generation": result.activation_generation,
                "attempt_identity": result.attempt_identity,
                "attempt_digest": result.attempt_digest,
                "predecessor_result_id": result.predecessor_result_id,
                "outcome": result.outcome,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(result=result, idempotency_key=idempotency_key, digest=digest)


@dataclass(frozen=True, slots=True)
class ProjectionActivationCommand:
    projection_kind: str
    projection_snapshot_id: UUID
    activation_kind: ProjectionActivationKind
    expected_predecessor_snapshot_id: UUID | None
    expected_predecessor_activation_generation: int | None
    expected_active_pointer_cas_version: int | None
    command_identity: str
    idempotency_key: IdempotencyKey
    digest: CommandDigest

    @classmethod
    def create(
        cls,
        *,
        projection_kind: str,
        projection_snapshot_id: UUID,
        activation_kind: ProjectionActivationKind,
        expected_predecessor_snapshot_id: UUID | None,
        expected_predecessor_activation_generation: int | None,
        expected_active_pointer_cas_version: int | None,
        command_identity: str,
        idempotency_key: IdempotencyKey,
    ) -> "ProjectionActivationCommand":
        digest = digest_command(
            operation=PersistenceOperation("projection_activation"),
            operation_schema_version=PROJECTION_ACTIVATION_SCHEMA_VERSION,
            payload={
                "projection_kind": projection_kind,
                "projection_snapshot_id": projection_snapshot_id,
                "activation_kind": activation_kind,
                "expected_predecessor_snapshot_id": expected_predecessor_snapshot_id,
                "expected_predecessor_activation_generation": expected_predecessor_activation_generation,
                "expected_active_pointer_cas_version": expected_active_pointer_cas_version,
                "command_identity": command_identity,
                "idempotency_key": idempotency_key.value,
            },
        )
        return cls(
            projection_kind=projection_kind,
            projection_snapshot_id=projection_snapshot_id,
            activation_kind=activation_kind,
            expected_predecessor_snapshot_id=expected_predecessor_snapshot_id,
            expected_predecessor_activation_generation=expected_predecessor_activation_generation,
            expected_active_pointer_cas_version=expected_active_pointer_cas_version,
            command_identity=command_identity,
            idempotency_key=idempotency_key,
            digest=digest,
        )


@dataclass(frozen=True, slots=True)
class ProjectionMutationResult:
    value: object
    replay: bool
    claim: IdempotencyClaimResult
    result_reference: OpaqueResultReference


@dataclass(frozen=True, slots=True)
class ProjectionActivationCommitted:
    activation: ProjectionActivation
    drift_count: int
    result_reference: OpaqueResultReference
    replay: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ProjectionActivationReplay:
    activation: ProjectionActivation
    drift_count: int
    result_reference: OpaqueResultReference
    replay: Literal[True] = True


@dataclass(frozen=True, slots=True)
class ProjectionActivationRejected:
    code: Literal[
        "expected_predecessor_conflict",
        "candidate_not_finalized",
        "candidate_ineligible",
        "candidate_integrity_conflict",
        "boundary_regression",
        "unsupported_scope",
        "idempotency_digest_conflict",
        "idempotency_claim_incomplete",
        "tenant_or_environment_conflict",
    ]
    retryable: bool


@dataclass(frozen=True, slots=True)
class ProjectionActivationUnavailable:
    code: Literal[
        "required_snapshot_unavailable",
        "required_boundary_unavailable",
        "required_configuration_unavailable",
        "persistence_unavailable",
        "publication_unavailable",
    ]
    retryable: bool


ProjectionActivationResult = (
    ProjectionActivationCommitted
    | ProjectionActivationReplay
    | ProjectionActivationRejected
    | ProjectionActivationUnavailable
)

ProjectionPublicationResolution = (
    PublishedActivePublication | UnpublishedActivePublication | UnavailableActivePublication
)


class ProjectionRepository(Protocol):
    async def add_ontology_version(self, ontology: OntologyVersion) -> OntologyVersion: ...

    async def resolve_ontology_version(self, ontology_version_id: UUID) -> OntologyVersion | None: ...

    async def add_config_version(self, config: ProjectionConfigVersion) -> ProjectionConfigVersion: ...

    async def resolve_config_version(self, config_id: UUID) -> ProjectionConfigVersion | None: ...

    async def create_run(self, command: ProjectionRunCommand) -> ProjectionMutationResult: ...

    async def resolve_run(self, projection_run_id: UUID) -> ProjectionRun | None: ...

    async def append_transition(self, transition: ProjectionRunTransition) -> ProjectionRunTransition: ...

    async def list_transitions(self, projection_run_id: UUID) -> tuple[ProjectionRunTransition, ...]: ...

    async def add_partition(self, partition: ProjectionPartition) -> ProjectionPartition: ...

    async def record_receipt(self, command: ProjectionReceiptCommand) -> ProjectionMutationResult: ...

    async def add_source_reference(self, reference: SourceProjectionReference) -> SourceProjectionReference: ...

    async def list_partitions(self, projection_run_id: UUID) -> tuple[ProjectionPartition, ...]: ...

    async def list_receipts(self, projection_run_id: UUID) -> tuple[ProjectionReceipt, ...]: ...

    async def list_source_references(self, projection_run_id: UUID) -> tuple[SourceProjectionReference, ...]: ...

    async def seal_candidate(self, command: ProjectionCandidateSealCommand) -> ProjectionMutationResult: ...

    async def record_validation(self, command: ProjectionValidationCommand) -> ProjectionMutationResult: ...

    async def list_validations(self, projection_run_id: UUID) -> tuple[ProjectionValidation, ...]: ...

    async def assemble_snapshot(self, command: ProjectionSnapshotCommand) -> ProjectionMutationResult: ...

    async def resolve_snapshot(self, projection_snapshot_id: UUID) -> ProjectionSnapshot | None: ...

    async def list_snapshot_members(
        self, projection_snapshot_id: UUID
    ) -> tuple[ProjectionSnapshotMember, ...]: ...

    async def record_reconciliation(
        self, command: ProjectionReconciliationCommand
    ) -> ProjectionMutationResult: ...

    async def list_reconciliation_results(
        self, projection_snapshot_id: UUID
    ) -> tuple[ProjectionReconciliationResult, ...]: ...

    async def activate(
        self, command: ProjectionActivationCommand
    ) -> ProjectionActivationResult: ...

    async def resolve_active_publication(
        self, projection_kind: str
    ) -> ProjectionPublicationResolution: ...

    async def list_activations(
        self, projection_kind: str
    ) -> tuple[ProjectionActivation, ...]: ...

    async def list_drifts(
        self, projection_activation_id: UUID
    ) -> tuple[ProjectionDrift, ...]: ...


def transition_allowed(previous: ProjectionRunState | None, current: ProjectionRunState) -> bool:
    transitions = {
        None: {ProjectionRunState.REQUESTED},
        ProjectionRunState.REQUESTED: {ProjectionRunState.BUILDING, ProjectionRunState.ABANDONED},
        ProjectionRunState.BUILDING: {ProjectionRunState.VALIDATING, ProjectionRunState.FAILED, ProjectionRunState.ABANDONED},
        ProjectionRunState.VALIDATING: {ProjectionRunState.COMPLETE, ProjectionRunState.FAILED, ProjectionRunState.ABANDONED},
        ProjectionRunState.COMPLETE: set(),
        ProjectionRunState.FAILED: set(),
        ProjectionRunState.ABANDONED: set(),
    }
    return current in transitions[previous]


def require_expected_candidate(run: ProjectionRun, candidate_id: UUID) -> None:
    if run.projection_candidate_id != candidate_id:
        raise OptimisticConflictError("Projection candidate does not belong to the run.")


__all__ = [
    "PROJECTION_RECEIPT_SCHEMA_VERSION",
    "PROJECTION_RESULT_SCHEMA_VERSION",
    "PROJECTION_RUN_SCHEMA_VERSION",
    "PROJECTION_SEAL_SCHEMA_VERSION",
    "PROJECTION_RECONCILIATION_SCHEMA_VERSION",
    "PROJECTION_SNAPSHOT_SCHEMA_VERSION",
    "PROJECTION_VALIDATION_SCHEMA_VERSION",
    "PROJECTION_ACTIVATION_SCHEMA_VERSION",
    "ProjectionCandidateSealCommand",
    "ProjectionActivationCommand",
    "ProjectionActivationCommitted",
    "ProjectionActivationRejected",
    "ProjectionActivationReplay",
    "ProjectionActivationResult",
    "ProjectionActivationUnavailable",
    "ProjectionMutationResult",
    "ProjectionReconciliationCommand",
    "ProjectionReceiptCommand",
    "ProjectionRepository",
    "ProjectionRunCommand",
    "ProjectionSnapshotAssemblyCommand",
    "ProjectionSnapshotCommand",
    "ProjectionValidationCommand",
    "ProjectionPublicationResolution",
    "require_expected_candidate",
    "transition_allowed",
]
