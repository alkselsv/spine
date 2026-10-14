"""In-memory implementation of the canonical projection ledger port."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypeVar
from uuid import UUID

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
    ProjectionCandidateSealCommand,
    ProjectionMutationResult,
    ProjectionReceiptCommand,
    ProjectionRunCommand,
    ProjectionRepository,
    transition_allowed,
)
from spine.domain.projections import (
    OntologyVersion,
    ProjectionCandidateState,
    ProjectionConfigVersion,
    ProjectionPartition,
    ProjectionReceipt,
    ProjectionRun,
    ProjectionRunTransition,
    SourceProjectionReference,
    projection_evidence_digest,
)

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
                item.projection_candidate_id == reference.projection_candidate_id
                and item.source_object_id == reference.source_object_id
                and item.source_revision_id == reference.source_revision_id
                and item.segment_id == reference.segment_id
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
            if not candidate_partitions or not candidate_receipts:
                self._uow._fail(ConstraintConflictError("Projection candidate has incomplete evidence."))
            evidence_digest = projection_evidence_digest(
                partitions=tuple(sorted(candidate_partitions, key=lambda item: item.projection_partition_id.hex)),
                receipts=tuple(sorted(candidate_receipts, key=lambda item: item.projection_receipt_id.hex)),
                references=tuple(sorted(candidate_references, key=lambda item: item.source_projection_reference_id.hex)),
                transitions=tuple(sorted(candidate_transitions, key=lambda item: item.projection_run_transition_id.hex)),
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


__all__ = ["InMemoryProjectionRepository"]
