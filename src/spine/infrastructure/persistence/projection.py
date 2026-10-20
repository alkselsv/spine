"""In-memory implementation of the canonical projection ledger port."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID, uuid4

from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    OptimisticConflictError,
)
from spine.application.persistence.idempotency import (
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.application.persistence.projection import (
    PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
    PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    ProjectionActivatedOutboxPayload,
    ProjectionCatchUpRequestedOutboxPayload,
    ProjectionActivationCommand,
    ProjectionActivationCommitted,
    ProjectionActivationReplay,
    ProjectionActivationResult,
    ProjectionCandidateSealCommand,
    ProjectionMutationResult,
    ProjectionReconciliationCommand,
    ProjectionReceiptCommand,
    ProjectionRunCommand,
    ProjectionSnapshotCommand,
    ProjectionValidationCommand,
    ProjectionRepository,
    ProjectionPublicationResolution,
    has_complete_physical_validation,
    transition_allowed,
)
from spine.application.persistence.outbox import OpaqueObjectReference
from spine.domain.projections import (
    ActiveProjection,
    CanonicalAsOf,
    OntologyVersion,
    ProjectionCandidateState,
    ProjectionConfigVersion,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionReconciliationResult,
    ProjectionRun,
    ProjectionRunTransition,
    ProjectionSnapshot,
    ProjectionSnapshotMember,
    ProjectionActivation,
    ProjectionActivationKind,
    ProjectionDrift,
    ProjectionDriftDetectedBy,
    ProjectionFreshness,
    ProjectionHealth,
    PublishedActivePublication,
    UnavailableActivePublication,
    UnpublishedActivePublication,
    ProjectionSnapshotState,
    ProjectionValidation,
    SourceProjectionReference,
    projection_evidence_digest,
    projection_snapshot_manifest_digest,
)
from spine.domain.audit import AuditObjectReference, AuditOutcome, CanonicalTransitionAuditPayload

if TYPE_CHECKING:
    from spine.infrastructure.persistence.in_memory import InMemoryUnitOfWork


_ModelT = TypeVar("_ModelT")


class InMemoryProjectionRepository(ProjectionRepository):
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    def _scope(self) -> tuple[UUID, UUID]:
        scope = self._uow.scope
        if not hasattr(scope, "environment_id"):
            raise ConstraintConflictError("Projection persistence requires an environment scope.")
        return scope.workspace_id, scope.environment_id

    def _all(self, base: str, pending: str) -> dict:
        values = dict(getattr(self._uow, base))
        values.update(getattr(self._uow, pending))
        return values

    def _copy(self, value: _ModelT) -> _ModelT:
        return value.model_copy(deep=True)  # type: ignore[union-attr,return-value]

    def _check_scope(self, value: object) -> None:
        workspace_id, environment_id = self._scope()
        if (getattr(value, "workspace_id"), getattr(value, "environment_id")) != (
            workspace_id,
            environment_id,
        ):
            self._uow._fail(ConstraintConflictError("Projection record is outside the tenant scope."))

    def _run(self, run_id: UUID) -> ProjectionRun | None:
        value = self._all("_base_projection_runs", "_pending_projection_runs").get(run_id)
        if value is None:
            return None
        self._check_scope(value)
        return self._copy(value)

    async def add_ontology_version(self, ontology: OntologyVersion) -> OntologyVersion:
        self._uow._guard_active()
        def operation() -> OntologyVersion:
            self._check_scope(ontology)
            existing = self._all("_base_ontology_versions", "_pending_ontology_versions").get(
                ontology.ontology_version_id
            )
            if existing is not None:
                if existing != ontology:
                    self._uow._fail(ConstraintConflictError("Ontology version identity conflicts."))
                return self._copy(existing)
            values = self._all("_base_ontology_versions", "_pending_ontology_versions").values()
            if any(
                item.ontology_name == ontology.ontology_name
                and item.version == ontology.version
                for item in values
            ) or any(
                item.ontology_name == ontology.ontology_name
                and item.definition_digest == ontology.definition_digest
                for item in values
            ):
                self._uow._fail(ConstraintConflictError("Ontology version is already registered."))
            if ontology.predecessor_ontology_version_id is not None:
                predecessor = self._all(
                    "_base_ontology_versions", "_pending_ontology_versions"
                ).get(ontology.predecessor_ontology_version_id)
                if (
                    predecessor is None
                    or predecessor.workspace_id != ontology.workspace_id
                    or predecessor.environment_id != ontology.environment_id
                    or predecessor.ontology_name != ontology.ontology_name
                    or predecessor.version >= ontology.version
                ):
                    self._uow._fail(ConstraintConflictError("Ontology predecessor is invalid."))
            self._uow._pending_ontology_versions[ontology.ontology_version_id] = self._copy(ontology)
            return self._copy(ontology)
        return self._uow._repository_call(operation)

    async def resolve_ontology_version(self, ontology_version_id: UUID) -> OntologyVersion | None:
        self._uow._guard_active()
        value = self._all("_base_ontology_versions", "_pending_ontology_versions").get(ontology_version_id)
        return self._copy(value) if value is not None and self._in_scope(value) else None

    def _in_scope(self, value: object) -> bool:
        workspace_id, environment_id = self._scope()
        return (getattr(value, "workspace_id"), getattr(value, "environment_id")) == (
            workspace_id,
            environment_id,
        )

    async def add_config_version(self, config: ProjectionConfigVersion) -> ProjectionConfigVersion:
        self._uow._guard_active()
        def operation() -> ProjectionConfigVersion:
            self._check_scope(config)
            existing = self._all("_base_projection_configs", "_pending_projection_configs").get(
                config.projection_config_version_id
            )
            if existing is not None:
                if existing != config:
                    self._uow._fail(ConstraintConflictError("Projection config identity conflicts."))
                return self._copy(existing)
            ontology = self._all("_base_ontology_versions", "_pending_ontology_versions").get(
                config.ontology_version_id
            )
            if (
                ontology is None
                or not ontology.publication_eligible
                or not self._in_scope(ontology)
            ):
                self._uow._fail(ConstraintConflictError("Projection config requires a published ontology."))
            values = self._all("_base_projection_configs", "_pending_projection_configs").values()
            if any(
                item.projection_kind == config.projection_kind and item.version == config.version
                for item in values
            ) or any(
                item.projection_kind == config.projection_kind
                and item.config_digest == config.config_digest
                for item in values
            ):
                self._uow._fail(ConstraintConflictError("Projection config version is already registered."))
            if config.predecessor_projection_config_version_id is not None:
                predecessor = self._all(
                    "_base_projection_configs", "_pending_projection_configs"
                ).get(config.predecessor_projection_config_version_id)
                if (
                    predecessor is None
                    or not self._in_scope(predecessor)
                    or predecessor.projection_kind != config.projection_kind
                    or predecessor.version >= config.version
                ):
                    self._uow._fail(ConstraintConflictError("Projection config predecessor is invalid."))
            self._uow._pending_projection_configs[config.projection_config_version_id] = self._copy(config)
            return self._copy(config)
        return self._uow._repository_call(operation)

    async def resolve_config_version(self, config_id: UUID) -> ProjectionConfigVersion | None:
        self._uow._guard_active()
        value = self._all("_base_projection_configs", "_pending_projection_configs").get(config_id)
        return self._copy(value) if value is not None and self._in_scope(value) else None

    async def create_run(self, command: ProjectionRunCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            run = await self.resolve_run(claim.result.result_id)
            if run is None:
                self._uow._fail(ConstraintConflictError("Projection run replay is unavailable."))
            return ProjectionMutationResult(run, True, claim, claim.result)
        def operation() -> ProjectionMutationResult:
            run = command.run.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
            self._check_scope(run)
            if run.candidate_state is not ProjectionCandidateState.OPEN:
                self._uow._fail(ConstraintConflictError("A new projection run must have an open candidate."))
            config = self._all("_base_projection_configs", "_pending_projection_configs").get(
                run.projection_config_version_id
            )
            boundary = self._all("_base_canonical_boundaries", "_pending_canonical_boundaries").get(
                run.canonical_boundary_id
            )
            if (
                config is None
                or boundary is None
                or not self._in_scope(config)
                or not self._in_scope(boundary)
                or config.projection_kind != run.projection_kind
                or boundary.projection_kind != run.projection_kind
            ):
                self._uow._fail(ConstraintConflictError("Projection run lineage is invalid."))
            if run.predecessor_snapshot_id is not None:
                predecessor = self._all(
                    "_base_projection_snapshots", "_pending_projection_snapshots"
                ).get(run.predecessor_snapshot_id)
                if (
                    predecessor is None
                    or predecessor.projection_kind != run.projection_kind
                    or predecessor.snapshot_state is not ProjectionSnapshotState.FINALIZED
                    or not self._in_scope(predecessor)
                ):
                    self._uow._fail(ConstraintConflictError("Projection run predecessor is invalid."))
            runs = self._all("_base_projection_runs", "_pending_projection_runs")
            if run.projection_run_id in runs or any(
                item.projection_candidate_id == run.projection_candidate_id
                or (
                    item.workspace_id == run.workspace_id
                    and item.environment_id == run.environment_id
                    and item.command_identity == run.command_identity
                )
                for item in runs.values()
            ):
                self._uow._fail(ConstraintConflictError("Projection run identity conflicts."))
            self._uow._pending_projection_runs[run.projection_run_id] = self._copy(run)
            result = OpaqueResultReference("projection_run", run.projection_run_id, 1)
            # complete is deliberately the final repository operation in this branch.
            self._uow._pending_projection_run_claims[run.projection_run_id] = claim
            return ProjectionMutationResult(self._copy(run), False, claim, result)
        result = self._uow._repository_call(operation)
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def resolve_run(self, projection_run_id: UUID) -> ProjectionRun | None:
        self._uow._guard_active()
        return self._run(projection_run_id)

    async def list_runs_for_source_revision(
        self, source_object_id: UUID, source_revision_id: UUID
    ) -> tuple[ProjectionRun, ...]:
        self._uow._guard_active()
        boundaries = self._all(
            "_base_canonical_boundary_members", "_pending_canonical_boundary_members"
        )
        boundary_ids = {
            boundary_id
            for (boundary_id, member_source_id), member in boundaries.items()
            if member_source_id == source_object_id
            and member.source_revision_id == source_revision_id
            and self._in_scope(member)
        }
        runs = (
            run
            for run in self._all(
                "_base_projection_runs", "_pending_projection_runs"
            ).values()
            if run.canonical_boundary_id in boundary_ids and self._in_scope(run)
        )
        return tuple(
            self._copy(run)
            for run in sorted(
                runs,
                key=lambda value: (value.started_at, value.projection_run_id.hex),
            )
        )

    async def append_transition(self, transition: ProjectionRunTransition) -> ProjectionRunTransition:
        self._uow._guard_active()
        def operation() -> ProjectionRunTransition:
            self._check_scope(transition)
            run = self._run(transition.projection_run_id)
            if run is None:
                self._uow._fail(ConstraintConflictError("Projection run does not exist."))
            if run.projection_candidate_id != transition.projection_candidate_id:
                self._uow._fail(ConstraintConflictError("Projection transition candidate is invalid."))
            if run.candidate_state is ProjectionCandidateState.SEALED:
                self._uow._fail(OptimisticConflictError("Sealed projection candidates are immutable."))
            transitions = self._all("_base_projection_transitions", "_pending_projection_transitions")
            if transition.projection_run_transition_id in transitions:
                self._uow._fail(ConstraintConflictError("Projection transition identity conflicts."))
            predecessor = (
                transitions.get(transition.predecessor_transition_id)
                if transition.predecessor_transition_id is not None
                else None
            )
            previous_state = predecessor.state if predecessor is not None else None
            if not transition_allowed(previous_state, transition.state):
                self._uow._fail(OptimisticConflictError("Projection run transition is not legal."))
            if predecessor is not None and (
                predecessor.projection_run_id != transition.projection_run_id
                or predecessor.projection_candidate_id != transition.projection_candidate_id
            ):
                self._uow._fail(ConstraintConflictError("Projection transition predecessor is invalid."))
            if any(
                item.projection_run_id == transition.projection_run_id
                and item.predecessor_transition_id == transition.predecessor_transition_id
                for item in transitions.values()
            ):
                self._uow._fail(ConstraintConflictError("Projection transition predecessor already has a successor."))
            self._uow._pending_projection_transitions[transition.projection_run_transition_id] = self._copy(transition)
            return self._copy(transition)
        return self._uow._repository_call(operation)

    def _require_open_run(self, run_id: UUID, candidate_id: UUID) -> ProjectionRun:
        run = self._run(run_id)
        if run is None or run.projection_candidate_id != candidate_id:
            self._uow._fail(ConstraintConflictError("Projection candidate lineage is invalid."))
        if run.candidate_state is ProjectionCandidateState.SEALED:
            self._uow._fail(OptimisticConflictError("Projection candidate is sealed."))
        return run

    async def add_partition(self, partition: ProjectionPartition) -> ProjectionPartition:
        self._uow._guard_active()
        def operation() -> ProjectionPartition:
            self._check_scope(partition)
            self._require_open_run(partition.projection_run_id, partition.projection_candidate_id)
            partitions = self._all("_base_projection_partitions", "_pending_projection_partitions")
            if partition.projection_partition_id in partitions or any(
                item.projection_candidate_id == partition.projection_candidate_id
                and item.partition_identity == partition.partition_identity
                for item in partitions.values()
            ):
                self._uow._fail(ConstraintConflictError("Projection partition identity conflicts."))
            self._uow._pending_projection_partitions[partition.projection_partition_id] = self._copy(partition)
            return self._copy(partition)
        return self._uow._repository_call(operation)

    async def list_transitions(self, projection_run_id: UUID) -> tuple[ProjectionRunTransition, ...]:
        self._uow._guard_active()
        return tuple(
            self._copy(item)
            for item in sorted(
                self._all("_base_projection_transitions", "_pending_projection_transitions").values(),
                key=lambda value: value.projection_run_transition_id.hex,
            )
            if item.projection_run_id == projection_run_id and self._in_scope(item)
        )

    async def record_receipt(self, command: ProjectionReceiptCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        receipts = self._all("_base_projection_receipts", "_pending_projection_receipts")
        if isinstance(claim, IdempotencyReplay):
            receipt = receipts.get(claim.result.result_id)
            if receipt is None:
                self._uow._fail(ConstraintConflictError("Projection receipt replay is unavailable."))
            return ProjectionMutationResult(self._copy(receipt), True, claim, claim.result)
        def operation() -> ProjectionMutationResult:
            receipt = command.receipt.model_copy(update={"idempotency_receipt_id": claim.receipt_id})
            self._check_scope(receipt)
            run = self._require_open_run(receipt.projection_run_id, receipt.projection_candidate_id)
            partition = self._all("_base_projection_partitions", "_pending_projection_partitions").get(
                receipt.projection_partition_id
            )
            if (
                partition is None
                or partition.projection_run_id != run.projection_run_id
                or partition.projection_candidate_id != run.projection_candidate_id
                or partition.expected_command_identity != receipt.command_identity
                or partition.expected_command_digest != receipt.command_digest
            ):
                self._uow._fail(ConstraintConflictError("Projection receipt lineage is invalid."))
            if receipt.projection_receipt_id in receipts:
                self._uow._fail(ConstraintConflictError("Projection receipt identity conflicts."))
            if any(
                item.workspace_id == receipt.workspace_id
                and item.environment_id == receipt.environment_id
                and item.command_identity == receipt.command_identity
                and item.command_digest != receipt.command_digest
                for item in receipts.values()
            ):
                self._uow._fail(IdempotencyConflictError("Projection command identity conflicts."))
            self._uow._pending_projection_receipts[receipt.projection_receipt_id] = self._copy(receipt)
            result = OpaqueResultReference("projection_receipt", receipt.projection_receipt_id, 1)
            return ProjectionMutationResult(self._copy(receipt), False, claim, result)
        result = self._uow._repository_call(operation)
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def add_source_reference(self, reference: SourceProjectionReference) -> SourceProjectionReference:
        self._uow._guard_active()
        def operation() -> SourceProjectionReference:
            self._check_scope(reference)
            run = self._require_open_run(reference.projection_run_id, reference.projection_candidate_id)
            receipt = self._all("_base_projection_receipts", "_pending_projection_receipts").get(
                reference.projection_receipt_id
            )
            config = self._all("_base_projection_configs", "_pending_projection_configs").get(
                reference.projection_config_version_id
            )
            if (
                receipt is None
                or receipt.projection_run_id != run.projection_run_id
                or receipt.projection_candidate_id != run.projection_candidate_id
                or config is None
                or config.projection_config_version_id != run.projection_config_version_id
            ):
                self._uow._fail(ConstraintConflictError("Source projection reference lineage is invalid."))
            source = self._all("_base_source_revisions", "_pending_source_revisions").get(
                reference.source_revision_id
            )
            if (
                source is None
                or source.source_object_id != reference.source_object_id
                or source.workspace_id != reference.workspace_id
                or source.environment_id != reference.environment_id
            ):
                self._uow._fail(ConstraintConflictError("Source projection reference is fabricated or cross-tenant."))
            if reference.processing_receipt_id is not None:
                processing_receipt = (
                    self._uow._base_processing_receipts
                    | self._uow._pending_processing_receipts
                ).get(reference.processing_receipt_id)
                if (
                    processing_receipt is None
                    or processing_receipt.workspace_id != reference.workspace_id
                    or processing_receipt.environment_id != reference.environment_id
                    or processing_receipt.source_object_id != reference.source_object_id
                    or processing_receipt.source_revision_id != reference.source_revision_id
                ):
                    self._uow._fail(ConstraintConflictError("Processing receipt lineage is invalid."))
            values = self._all("_base_projection_references", "_pending_projection_references")
            if reference.source_projection_reference_id in values or any(
                item.workspace_id == reference.workspace_id
                and item.environment_id == reference.environment_id
                and item.projection_candidate_id == reference.projection_candidate_id
                and item.projected_kind == reference.projected_kind
                and item.projected_object_id == reference.projected_object_id
                for item in values.values()
            ):
                self._uow._fail(ConstraintConflictError("Source projection reference identity conflicts."))
            self._uow._pending_projection_references[reference.source_projection_reference_id] = self._copy(reference)
            return self._copy(reference)
        return self._uow._repository_call(operation)

    async def list_partitions(self, projection_run_id: UUID) -> tuple[ProjectionPartition, ...]:
        self._uow._guard_active()
        return tuple(
            self._copy(item)
            for item in sorted(
                self._all("_base_projection_partitions", "_pending_projection_partitions").values(),
                key=lambda value: value.projection_partition_id.hex,
            )
            if item.projection_run_id == projection_run_id and self._in_scope(item)
        )

    async def list_receipts(self, projection_run_id: UUID) -> tuple[ProjectionReceipt, ...]:
        self._uow._guard_active()
        return tuple(
            self._copy(item)
            for item in sorted(
                self._all("_base_projection_receipts", "_pending_projection_receipts").values(),
                key=lambda value: value.projection_receipt_id.hex,
            )
            if item.projection_run_id == projection_run_id and self._in_scope(item)
        )

    async def list_source_references(self, projection_run_id: UUID) -> tuple[SourceProjectionReference, ...]:
        self._uow._guard_active()
        return tuple(
            self._copy(item)
            for item in sorted(
                self._all("_base_projection_references", "_pending_projection_references").values(),
                key=lambda value: value.source_projection_reference_id.hex,
            )
            if item.projection_run_id == projection_run_id and self._in_scope(item)
        )

    async def seal_candidate(self, command: ProjectionCandidateSealCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            run = await self.resolve_run(claim.result.result_id)
            if run is None:
                self._uow._fail(ConstraintConflictError("Projection candidate replay is unavailable."))
            return ProjectionMutationResult(run, True, claim, claim.result)
        def operation() -> ProjectionMutationResult:
            run = self._run(command.projection_run_id)
            if run is None or run.projection_candidate_id != command.projection_candidate_id:
                self._uow._fail(ConstraintConflictError("Projection candidate does not exist."))
            config = self._all("_base_projection_configs", "_pending_projection_configs").get(
                run.projection_config_version_id
            )
            if config is None or not self._in_scope(config):
                self._uow._fail(ConstraintConflictError("Projection config version is unavailable."))
            if run.candidate_state is ProjectionCandidateState.SEALED:
                self._uow._fail(OptimisticConflictError("Projection candidate is already sealed."))
            partitions = tuple(
                self._all("_base_projection_partitions", "_pending_projection_partitions").values()
            )
            receipts = tuple(self._all("_base_projection_receipts", "_pending_projection_receipts").values())
            references = tuple(self._all("_base_projection_references", "_pending_projection_references").values())
            transitions = tuple(self._all("_base_projection_transitions", "_pending_projection_transitions").values())
            candidate_partitions = tuple(item for item in partitions if item.projection_run_id == run.projection_run_id)
            candidate_receipts = tuple(item for item in receipts if item.projection_run_id == run.projection_run_id)
            candidate_references = tuple(item for item in references if item.projection_run_id == run.projection_run_id)
            candidate_transitions = tuple(item for item in transitions if item.projection_run_id == run.projection_run_id)
            validations = tuple(
                item
                for item in self._all(
                    "_base_projection_validations", "_pending_projection_validations"
                ).values()
                if item.projection_run_id == run.projection_run_id
            )
            if not candidate_partitions or not candidate_receipts:
                self._uow._fail(ConstraintConflictError("Projection candidate has incomplete evidence."))
            if (
                any(item.decision.value != "passed" for item in validations)
                or not has_complete_physical_validation(
                    validations,
                    candidate_partitions,
                    candidate_receipts,
                    candidate_references,
                    run,
                    config,
                )
            ):
                self._uow._fail(
                    ConstraintConflictError(
                        "Projection candidate physical validation evidence is incomplete."
                    )
                )
            evidence_digest = projection_evidence_digest(
                partitions=tuple(sorted(candidate_partitions, key=lambda item: item.projection_partition_id.hex)),
                receipts=tuple(sorted(candidate_receipts, key=lambda item: item.projection_receipt_id.hex)),
                references=tuple(sorted(candidate_references, key=lambda item: item.source_projection_reference_id.hex)),
                transitions=tuple(sorted(candidate_transitions, key=lambda item: item.projection_run_transition_id.hex)),
                validations=tuple(sorted(validations, key=lambda item: item.projection_validation_id.hex)),
            )
            values = run.model_dump(mode="python")
            values.update(
                candidate_state=ProjectionCandidateState.SEALED,
                candidate_evidence_digest=evidence_digest,
                candidate_sealed_at=datetime.now(timezone.utc),
            )
            sealed = ProjectionRun(**values)
            self._uow._pending_projection_runs[run.projection_run_id] = sealed
            result = OpaqueResultReference("projection_run", run.projection_run_id, 1)
            return ProjectionMutationResult(sealed, False, claim, result)
        result = self._uow._repository_call(operation)
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def record_validation(self, command: ProjectionValidationCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            value = self._all("_base_projection_validations", "_pending_projection_validations").get(
                claim.result.result_id
            )
            if value is None:
                self._uow._fail(ConstraintConflictError("Projection validation replay is unavailable."))
            return ProjectionMutationResult(self._copy(value), True, claim, claim.result)

        def operation() -> ProjectionMutationResult:
            validation = command.validation.model_copy(
                update={"projection_validation_id": command.validation.projection_validation_id}
            )
            self._check_scope(validation)
            run = self._run(validation.projection_run_id)
            if run is None or run.projection_candidate_id != validation.projection_candidate_id:
                self._uow._fail(ConstraintConflictError("Projection validation lineage is invalid."))
            if run.candidate_state is ProjectionCandidateState.SEALED:
                self._uow._fail(OptimisticConflictError("Projection candidate is sealed."))
            validations = self._all("_base_projection_validations", "_pending_projection_validations")
            existing = validations.get(validation.projection_validation_id)
            if existing is not None:
                if existing != validation:
                    self._uow._fail(ConstraintConflictError("Projection validation identity conflicts."))
                return ProjectionMutationResult(
                    self._copy(existing), False, claim,
                    OpaqueResultReference("projection_validation", validation.projection_validation_id, 1),
                )
            if any(
                item.projection_candidate_id == validation.projection_candidate_id
                and item.gate_kind == validation.gate_kind
                and item.gate_version == validation.gate_version
                and item.input_digest == validation.input_digest
                for item in validations.values()
            ):
                self._uow._fail(ConstraintConflictError("Projection validation gate already exists."))
            self._uow._pending_projection_validations[validation.projection_validation_id] = self._copy(validation)
            return ProjectionMutationResult(
                self._copy(validation), False, claim,
                OpaqueResultReference("projection_validation", validation.projection_validation_id, 1),
            )

        result = self._uow._repository_call(operation)
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def list_validations(self, projection_run_id: UUID) -> tuple[ProjectionValidation, ...]:
        self._uow._guard_active()
        values = self._all("_base_projection_validations", "_pending_projection_validations").values()
        return tuple(
            self._copy(value)
            for value in sorted(values, key=lambda item: item.projection_validation_id.hex)
            if value.projection_run_id == projection_run_id and self._in_scope(value)
        )

    async def assemble_snapshot(self, command: ProjectionSnapshotCommand) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            value = self._all("_base_projection_snapshots", "_pending_projection_snapshots").get(
                claim.result.result_id
            )
            if value is None:
                self._uow._fail(ConstraintConflictError("Projection snapshot replay is unavailable."))
            return ProjectionMutationResult(self._copy(value), True, claim, claim.result)

        def operation() -> ProjectionMutationResult:
            requested = command.snapshot
            self._check_scope(requested)
            run = self._run(requested.projection_run_id)
            if (
                run is None
                or run.projection_candidate_id != requested.projection_candidate_id
                or run.candidate_state is not ProjectionCandidateState.SEALED
                or run.candidate_evidence_digest != requested.candidate_evidence_digest
                or run.projection_kind != requested.projection_kind
                or run.projection_config_version_id != requested.projection_config_version_id
                or run.canonical_boundary_id != requested.canonical_boundary_id
            ):
                self._uow._fail(ConstraintConflictError("Projection snapshot candidate lineage is invalid."))
            config = self._all("_base_projection_configs", "_pending_projection_configs").get(
                run.projection_config_version_id
            )
            if config is None or not self._in_scope(config):
                self._uow._fail(ConstraintConflictError("Projection config version is unavailable."))
            snapshots = self._all("_base_projection_snapshots", "_pending_projection_snapshots")
            existing = next(
                (item for item in snapshots.values() if item.projection_candidate_id == requested.projection_candidate_id),
                None,
            )
            if existing is not None:
                if existing.manifest_digest != requested.manifest_digest:
                    self._uow._fail(ConstraintConflictError("Projection snapshot identity conflicts."))
                return ProjectionMutationResult(
                    self._copy(existing), False, claim,
                    OpaqueResultReference("projection_snapshot", existing.projection_snapshot_id, 1),
                )
            if requested.snapshot_state is not ProjectionSnapshotState.ASSEMBLING:
                self._uow._fail(ConstraintConflictError("Projection snapshot must begin assembling."))
            if requested.predecessor_snapshot_id is not None:
                predecessor = snapshots.get(requested.predecessor_snapshot_id)
                if (
                    predecessor is None
                    or predecessor.projection_kind != requested.projection_kind
                    or predecessor.workspace_id != requested.workspace_id
                    or predecessor.environment_id != requested.environment_id
                    or predecessor.snapshot_state is not ProjectionSnapshotState.FINALIZED
                ):
                    self._uow._fail(ConstraintConflictError("Projection snapshot predecessor is invalid."))
            boundary = self._all("_base_canonical_boundaries", "_pending_canonical_boundaries").get(
                requested.canonical_boundary_id
            )
            if boundary is None or not self._in_scope(boundary) or boundary.projection_kind != requested.projection_kind:
                self._uow._fail(ConstraintConflictError("Projection snapshot boundary is invalid."))
            boundary_members = tuple(
                value
                for value in self._all(
                    "_base_canonical_boundary_members", "_pending_canonical_boundary_members"
                ).values()
                if value.boundary_id == requested.canonical_boundary_id and self._in_scope(value)
            )
            candidate_partitions = tuple(
                value
                for value in self._all(
                    "_base_projection_partitions", "_pending_projection_partitions"
                ).values()
                if value.projection_run_id == run.projection_run_id
                and self._in_scope(value)
            )
            candidate_receipts = tuple(
                value
                for value in self._all(
                    "_base_projection_receipts", "_pending_projection_receipts"
                ).values()
                if value.projection_run_id == run.projection_run_id
                and self._in_scope(value)
            )
            candidate_references = tuple(
                value
                for value in self._all(
                    "_base_projection_references", "_pending_projection_references"
                ).values()
                if value.projection_run_id == run.projection_run_id
                and self._in_scope(value)
            )
            members = tuple(command.members)
            if len(members) != len(boundary_members) or {
                item.source_object_id for item in members
            } != {item.source_object_id for item in boundary_members}:
                self._uow._fail(ConstraintConflictError("Projection snapshot membership is incomplete."))
            by_source = {item.source_object_id: item for item in boundary_members}
            for member in members:
                self._check_scope(member)
                boundary_member = by_source.get(member.source_object_id)
                if (
                    boundary_member is None
                    or member.projection_snapshot_id != requested.projection_snapshot_id
                    or member.canonical_boundary_id != requested.canonical_boundary_id
                    or member.canonical_boundary_member_id != boundary_member.source_object_id
                    or member.source_revision_id != boundary_member.source_revision_id
                ):
                    self._uow._fail(ConstraintConflictError("Projection snapshot member lineage is invalid."))
                boundary_kind = boundary_member.revision_kind.value
                if (
                    (boundary_kind == "tombstone" and member.disposition.value != "tombstoned")
                    or (boundary_kind == "content" and member.disposition.value == "tombstoned")
                ):
                    self._uow._fail(ConstraintConflictError("Projection snapshot disposition does not match the boundary revision."))
                if member.disposition.value == "included":
                    receipts = self._all("_base_projection_receipts", "_pending_projection_receipts")
                    partitions = self._all("_base_projection_partitions", "_pending_projection_partitions")
                    receipt = receipts.get(member.projection_receipt_id)
                    partition = partitions.get(member.projection_partition_id)
                    if (
                        receipt is None
                        or partition is None
                        or receipt.projection_run_id != run.projection_run_id
                        or receipt.projection_candidate_id != run.projection_candidate_id
                        or receipt.projection_partition_id != partition.projection_partition_id
                        or receipt.command_identity != partition.expected_command_identity
                        or receipt.command_digest != partition.expected_command_digest
                    ):
                        self._uow._fail(ConstraintConflictError("Projection snapshot artifact evidence is invalid."))
            validations = tuple(
                value
                for value in self._all(
                    "_base_projection_validations", "_pending_projection_validations"
                ).values()
                if value.projection_run_id == run.projection_run_id and self._in_scope(value)
            )
            if (
                not validations
                or any(item.decision.value != "passed" for item in validations)
                or not has_complete_physical_validation(
                    validations,
                    candidate_partitions,
                    candidate_receipts,
                    tuple(candidate_references),
                    run,
                    config,
                )
            ):
                self._uow._fail(ConstraintConflictError("Projection snapshot validation evidence is incomplete."))
            if requested.member_count != len(members):
                self._uow._fail(ConstraintConflictError("Projection snapshot member count is invalid."))
            expected_manifest = projection_snapshot_manifest_digest(requested, members)
            if requested.manifest_digest != expected_manifest:
                self._uow._fail(ConstraintConflictError("Projection snapshot manifest digest is invalid."))
            finalized = requested.model_copy(update={"snapshot_state": ProjectionSnapshotState.FINALIZED})
            self._uow._pending_projection_snapshots[finalized.projection_snapshot_id] = self._copy(finalized)
            for member in members:
                self._uow._pending_projection_snapshot_members[member.projection_snapshot_member_id] = self._copy(member)
            return ProjectionMutationResult(
                self._copy(finalized), False, claim,
                OpaqueResultReference("projection_snapshot", finalized.projection_snapshot_id, 1),
            )

        result = self._uow._repository_call(operation)
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def resolve_snapshot(self, projection_snapshot_id: UUID) -> ProjectionSnapshot | None:
        self._uow._guard_active()
        value = self._all("_base_projection_snapshots", "_pending_projection_snapshots").get(projection_snapshot_id)
        return self._copy(value) if value is not None and self._in_scope(value) else None

    async def list_snapshot_members(self, projection_snapshot_id: UUID) -> tuple[ProjectionSnapshotMember, ...]:
        self._uow._guard_active()
        values = self._all("_base_projection_snapshot_members", "_pending_projection_snapshot_members").values()
        return tuple(
            self._copy(value)
            for value in sorted(values, key=lambda item: item.canonical_boundary_member_id.hex)
            if value.projection_snapshot_id == projection_snapshot_id and self._in_scope(value)
        )

    async def record_reconciliation(
        self, command: ProjectionReconciliationCommand
    ) -> ProjectionMutationResult:
        self._uow._guard_active()
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            value = self._all(
                "_base_projection_reconciliation_results", "_pending_projection_reconciliation_results"
            ).get(claim.result.result_id)
            if value is None:
                self._uow._fail(ConstraintConflictError("Projection reconciliation replay is unavailable."))
            return ProjectionMutationResult(self._copy(value), True, claim, claim.result)

        def operation() -> ProjectionMutationResult:
            result = command.result
            self._check_scope(result)
            snapshot = self._all(
                "_base_projection_snapshots", "_pending_projection_snapshots"
            ).get(result.projection_snapshot_id)
            if snapshot is None or snapshot.snapshot_state is not ProjectionSnapshotState.FINALIZED:
                self._uow._fail(ConstraintConflictError("Reconciliation requires a finalized snapshot."))
            results = self._all(
                "_base_projection_reconciliation_results", "_pending_projection_reconciliation_results"
            )
            if result.reconciliation_result_id in results:
                self._uow._fail(ConstraintConflictError("Reconciliation result identity conflicts."))
            if any(
                item.attempt_identity == result.attempt_identity
                and item.attempt_digest != result.attempt_digest
                for item in results.values()
            ):
                self._uow._fail(IdempotencyConflictError("Reconciliation attempt identity conflicts."))
            if any(
                item.projection_kind == result.projection_kind
                and item.activation_generation == result.activation_generation
                and item.projection_snapshot_id == result.projection_snapshot_id
                and item.projection_activation_id == result.projection_activation_id
                and item.predecessor_result_id == result.predecessor_result_id
                for item in results.values()
            ):
                self._uow._fail(ConstraintConflictError("Reconciliation root or successor already exists."))
            if result.predecessor_result_id is not None:
                predecessor = results.get(result.predecessor_result_id)
                if (
                    predecessor is None
                    or predecessor.projection_kind != result.projection_kind
                    or predecessor.activation_generation != result.activation_generation
                    or predecessor.projection_snapshot_id != result.projection_snapshot_id
                    or predecessor.projection_activation_id != result.projection_activation_id
                ):
                    self._uow._fail(ConstraintConflictError("Reconciliation predecessor lineage is invalid."))
            self._uow._pending_projection_reconciliation_results[result.reconciliation_result_id] = self._copy(result)
            return ProjectionMutationResult(
                self._copy(result), False, claim,
                OpaqueResultReference("projection_reconciliation", result.reconciliation_result_id, 1),
            )

        result = self._uow._repository_call(operation)
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def list_reconciliation_results(
        self, projection_snapshot_id: UUID
    ) -> tuple[ProjectionReconciliationResult, ...]:
        self._uow._guard_active()
        values = self._all(
            "_base_projection_reconciliation_results", "_pending_projection_reconciliation_results"
        ).values()
        return tuple(
            self._copy(value)
            for value in sorted(values, key=lambda item: item.checked_at)
            if value.projection_snapshot_id == projection_snapshot_id and self._in_scope(value)
        )

    async def activate(
        self, command: ProjectionActivationCommand
    ) -> ProjectionActivationResult:
        """Atomically publish a finalized snapshot in the in-memory test adapter."""

        self._uow._guard_active()
        if command.digest != command.expected_digest():
            self._uow._fail(IdempotencyConflictError("Projection activation command digest is invalid."))
        claim = await self._uow.idempotency.claim(
            operation_schema_version=command.digest.operation_schema_version,
            key=command.idempotency_key,
            digest=command.digest,
        )
        if isinstance(claim, IdempotencyReplay):
            activation = self._all(
                "_base_projection_activations", "_pending_projection_activations"
            ).get(claim.result.result_id)
            if activation is None:
                self._uow._fail(ConstraintConflictError("Projection activation replay is unavailable."))
            return ProjectionActivationReplay(
                activation=self._copy(activation),
                drift_count=len(await self.list_drifts(activation.projection_activation_id)),
                result_reference=claim.result,
            )

        snapshot = self._all(
            "_base_projection_snapshots", "_pending_projection_snapshots"
        ).get(command.projection_snapshot_id)
        if snapshot is None or not self._in_scope(snapshot):
            self._uow._fail(ConstraintConflictError("Projection snapshot is unavailable."))
        if snapshot.snapshot_state is not ProjectionSnapshotState.FINALIZED:
            self._uow._fail(ConstraintConflictError("Projection snapshot is not finalized."))
        if snapshot.projection_kind != command.projection_kind:
            self._uow._fail(ConstraintConflictError("Projection activation kind is invalid."))
        boundary = self._all(
            "_base_canonical_boundaries", "_pending_canonical_boundaries"
        ).get(snapshot.canonical_boundary_id)
        if boundary is None or not self._in_scope(boundary):
            self._uow._fail(ConstraintConflictError("Projection boundary is unavailable."))
        if boundary.scope_version != "r1":
            self._uow._fail(ConstraintConflictError("Projection boundary scope is unsupported."))
        run = self._all("_base_projection_runs", "_pending_projection_runs").get(
            snapshot.projection_run_id
        )
        if (
            run is None
            or run.projection_candidate_id != snapshot.projection_candidate_id
            or run.candidate_state is not ProjectionCandidateState.SEALED
            or run.candidate_evidence_digest != snapshot.candidate_evidence_digest
            or run.projection_kind != snapshot.projection_kind
            or run.projection_config_version_id != snapshot.projection_config_version_id
            or run.canonical_boundary_id != snapshot.canonical_boundary_id
        ):
            self._uow._fail(ConstraintConflictError("Projection snapshot run lineage is invalid."))
        if command.activation_kind is ProjectionActivationKind.ROLLBACK and run.operation.value != "rollback":
            self._uow._fail(ConstraintConflictError("Rollback requires a rollback projection run."))
        if command.activation_kind is ProjectionActivationKind.PUBLISH and run.operation.value == "rollback":
            self._uow._fail(ConstraintConflictError("Publish cannot use a rollback projection run."))
        key = (snapshot.workspace_id, snapshot.environment_id, snapshot.projection_kind)
        current = self._all("_base_active_projections", "_pending_active_projections").get(key)
        if current is None:
            if any(
                value is not None
                for value in (
                    command.expected_predecessor_snapshot_id,
                    command.expected_predecessor_activation_generation,
                    command.expected_active_pointer_cas_version,
                )
            ) or snapshot.predecessor_snapshot_id is not None:
                self._uow._fail(OptimisticConflictError("First publication predecessor is not empty."))
            generation, cas_version = 1, 1
        else:
            if (
                command.expected_predecessor_snapshot_id != current.projection_snapshot_id
                or command.expected_predecessor_activation_generation != current.activation_generation
                or command.expected_active_pointer_cas_version != current.cas_version
                or snapshot.predecessor_snapshot_id != current.projection_snapshot_id
            ):
                self._uow._fail(OptimisticConflictError("Active projection predecessor is stale."))
            active_snapshot = self._all(
                "_base_projection_snapshots", "_pending_projection_snapshots"
            ).get(current.projection_snapshot_id)
            active_boundary = self._all(
                "_base_canonical_boundaries", "_pending_canonical_boundaries"
            ).get(active_snapshot.canonical_boundary_id if active_snapshot else None)
            if active_boundary is None or boundary.accepted_through_generation < active_boundary.accepted_through_generation:
                self._uow._fail(OptimisticConflictError("Projection boundary regressed."))
            generation, cas_version = current.activation_generation + 1, current.cas_version + 1

        generation_key = (snapshot.workspace_id, snapshot.environment_id)
        canonical_generation = max(
            self._uow._base_canonical_generations.get(generation_key, 0),
            self._uow._pending_canonical_generations.get(generation_key, 0),
        )
        activation_id = uuid4()
        acceptances = tuple(
            value
            for value in self._all(
                "_base_canonical_acceptances", "_pending_canonical_acceptances"
            ).values()
            if value.workspace_id == snapshot.workspace_id
            and value.environment_id == snapshot.environment_id
            and value.accepted_generation > boundary.accepted_through_generation
            and value.accepted_generation <= canonical_generation
        )
        drift_values = [
            ProjectionDrift(
                projection_drift_id=uuid4(),
                workspace_id=snapshot.workspace_id,
                environment_id=snapshot.environment_id,
                projection_kind=snapshot.projection_kind,
                projection_snapshot_id=snapshot.projection_snapshot_id,
                projection_activation_id=activation_id,
                activation_generation=generation,
                canonical_acceptance_id=acceptance.canonical_acceptance_id,
                source_object_id=acceptance.source_object_id,
                source_revision_id=acceptance.source_revision_id,
                accepted_generation=acceptance.accepted_generation,
                detected_by=ProjectionDriftDetectedBy.ACTIVATION,
                detected_at=datetime.now(timezone.utc),
            )
            for acceptance in sorted(acceptances, key=lambda item: item.accepted_generation)
        ]

        context = self._uow._context_snapshot_active()
        audit = self._uow._audit_events.build_event(
            audit_event_id=None,
            workspace_id=snapshot.workspace_id,
            environment_id=snapshot.environment_id,
            event_type="canonical.transition",
            schema_version=1,
            payload=CanonicalTransitionAuditPayload(transition="projection.activated"),
            origin=context.origin,
            acting_subject_id=context.acting_subject_id,
            service_principal_id=context.service_principal_id,
            trace_id=context.trace_id,
            occurred_at=datetime.now(timezone.utc),
            outcome=AuditOutcome.COMMITTED,
            reason="projection_activation_committed",
            target=AuditObjectReference(
                object_type="projection_activation", object_id=activation_id, schema_version=1
            ),
            producer_deduplication_id=f"projection-activated:{activation_id}",
        )
        audit_id = await self._uow.audit.append(audit)
        activation = ProjectionActivation(
            projection_activation_id=activation_id,
            workspace_id=snapshot.workspace_id,
            environment_id=snapshot.environment_id,
            projection_kind=snapshot.projection_kind,
            projection_snapshot_id=snapshot.projection_snapshot_id,
            predecessor_snapshot_id=current.projection_snapshot_id if current else None,
            activation_generation=generation,
            activation_kind=command.activation_kind,
            command_identity=command.command_identity,
            command_digest=command.digest.value,
            idempotency_receipt_id=claim.receipt_id,
            audit_event_id=audit_id,
            activated_at=datetime.now(timezone.utc),
        )
        pointer = ActiveProjection(
            workspace_id=snapshot.workspace_id,
            environment_id=snapshot.environment_id,
            projection_kind=snapshot.projection_kind,
            projection_snapshot_id=snapshot.projection_snapshot_id,
            projection_activation_id=activation_id,
            activation_generation=generation,
            cas_version=cas_version,
        )
        self._uow._pending_projection_activations[activation_id] = activation
        self._uow._pending_active_projections[key] = pointer
        for drift in drift_values:
            self._uow._pending_projection_drifts[drift.projection_drift_id] = drift
        await self._uow.outbox.append(
            self._uow._outbox_events.build_intent(
                event_id=uuid4(),
                workspace_id=snapshot.workspace_id,
                environment_id=snapshot.environment_id,
                event_type=PROJECTION_ACTIVATED_OUTBOX_EVENT_TYPE,
                schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
                payload=ProjectionActivatedOutboxPayload(
                    projection_activation_id=activation_id,
                    projection_snapshot_id=snapshot.projection_snapshot_id,
                    projection_kind=snapshot.projection_kind,
                    activation_generation=generation,
                    activation_kind=command.activation_kind,
                    canonical_boundary_id=boundary.boundary_id,
                    accepted_through_generation=boundary.accepted_through_generation,
                    drift_count=len(drift_values),
                    audit_event_id=audit_id,
                ),
                aggregate=OpaqueObjectReference(
                    object_type="projection_activation",
                    object_id=activation_id,
                    schema_version=1,
                ),
                producer_deduplication_id=f"projection-activated:{activation_id}",
                trace_id=context.trace_id,
            )
        )
        for drift in drift_values:
            await self._uow.outbox.append(
                self._uow._outbox_events.build_intent(
                    event_id=uuid4(),
                    workspace_id=snapshot.workspace_id,
                    environment_id=snapshot.environment_id,
                    event_type=PROJECTION_CATCH_UP_OUTBOX_EVENT_TYPE,
                    schema_version=PROJECTION_OUTBOX_SCHEMA_VERSION,
                    payload=ProjectionCatchUpRequestedOutboxPayload(
                        projection_drift_id=drift.projection_drift_id,
                        projection_activation_id=activation_id,
                        projection_kind=snapshot.projection_kind,
                        activation_generation=generation,
                        canonical_acceptance_id=drift.canonical_acceptance_id,
                        source_object_id=drift.source_object_id,
                        source_revision_id=drift.source_revision_id,
                        accepted_generation=drift.accepted_generation,
                    ),
                    aggregate=OpaqueObjectReference(
                        object_type="projection_drift",
                        object_id=drift.projection_drift_id,
                        schema_version=1,
                    ),
                    producer_deduplication_id=f"projection-catch-up:{drift.projection_drift_id}",
                    trace_id=context.trace_id,
                )
            )
        result = ProjectionActivationCommitted(
            activation=activation,
            drift_count=len(drift_values),
            result_reference=OpaqueResultReference("projection_activation", activation_id, 1),
        )
        await self._uow.idempotency.complete(claim, result.result_reference)
        return result

    async def resolve_active_publication(
        self, projection_kind: str
    ) -> ProjectionPublicationResolution:
        self._uow._guard_active()
        scope = self._scope()
        key = (scope[0], scope[1], projection_kind)
        pointer = self._all("_base_active_projections", "_pending_active_projections").get(key)
        if pointer is None:
            return UnpublishedActivePublication(
                workspace_id=scope[0], environment_id=scope[1], projection_kind=projection_kind
            )
        snapshots = self._all("_base_projection_snapshots", "_pending_projection_snapshots")
        snapshot = snapshots.get(pointer.projection_snapshot_id)
        activations = self._all("_base_projection_activations", "_pending_projection_activations")
        activation = activations.get(pointer.projection_activation_id)
        boundary = self._all("_base_canonical_boundaries", "_pending_canonical_boundaries").get(
            snapshot.canonical_boundary_id if snapshot else None
        )
        config = self._all("_base_projection_configs", "_pending_projection_configs").get(
            snapshot.projection_config_version_id if snapshot else None
        )
        if snapshot is None or activation is None or boundary is None or config is None:
            return UnavailableActivePublication(
                workspace_id=scope[0], environment_id=scope[1], projection_kind=projection_kind,
                reason_code="active_publication_integrity_conflict",
            )
        drifts = await self.list_drifts(activation.projection_activation_id)
        reconciliations = await self.list_reconciliation_results(snapshot.projection_snapshot_id)
        effective = reconciliations[-1] if reconciliations else None
        return PublishedActivePublication(
            workspace_id=scope[0],
            environment_id=scope[1],
            projection_kind=projection_kind,
            projection_snapshot=snapshot,
            projection_activation=activation,
            activation_generation=pointer.activation_generation,
            active_pointer_cas_version=pointer.cas_version,
            canonical_boundary_id=boundary.boundary_id,
            canonical_boundary_manifest_digest=boundary.manifest_digest,
            accepted_through_generation=boundary.accepted_through_generation,
            canonical_boundary_membership_ref=f"canonical-boundary-members:{boundary.boundary_id}",
            as_of=CanonicalAsOf(
                canonical_boundary_id=boundary.boundary_id,
                accepted_through_generation=boundary.accepted_through_generation,
                boundary_time=boundary.boundary_time,
            ),
            projection_config_version=config,
            freshness=ProjectionFreshness.STALE if drifts else ProjectionFreshness.CURRENT,
            health=ProjectionHealth.HEALTHY
            if effective is not None and effective.outcome.value == "healthy"
            else ProjectionHealth.DEGRADED,
            effective_reconciliation=effective,
        )

    async def list_activations(self, projection_kind: str) -> tuple[ProjectionActivation, ...]:
        self._uow._guard_active()
        values = self._all("_base_projection_activations", "_pending_projection_activations").values()
        return tuple(
            self._copy(value)
            for value in sorted(values, key=lambda item: item.activation_generation)
            if value.projection_kind == projection_kind and self._in_scope(value)
        )

    async def list_drifts(self, projection_activation_id: UUID) -> tuple[ProjectionDrift, ...]:
        self._uow._guard_active()
        values = self._all("_base_projection_drifts", "_pending_projection_drifts").values()
        return tuple(
            self._copy(value)
            for value in sorted(values, key=lambda item: item.accepted_generation)
            if value.projection_activation_id == projection_activation_id and self._in_scope(value)
        )


__all__ = ["InMemoryProjectionRepository"]
