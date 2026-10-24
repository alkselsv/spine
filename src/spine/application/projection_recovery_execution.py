"""Execute committed projection recovery requests through the worker boundary.

The consumer deliberately owns no canonical projection state.  A canonical
resolver selects the immutable rebuild command after the outbox intent commits;
the physical adapter runs outside a PostgreSQL unit of work; and a recorder
hands the result back to the existing canonical validation/publication path.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from spine.application.diagnostics import DiagnosticContext
from spine.application.outbox import (
    OutboxConsumerSuccess,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.projection import (
    PROJECTION_OUTBOX_SCHEMA_VERSION,
    PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
    PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
    ProjectionRecoveryAction,
    ProjectionRecoveryRequestedOutboxPayload,
    ProjectionSuccessorRequestedOutboxPayload,
    ProjectionRepository,
    ProjectionRunCommand,
)
from spine.application.persistence.canonical import CanonicalAcceptanceRepository
from spine.application.persistence.boundary import CanonicalBoundaryRepository
from spine.application.persistence.ingestion import IngestionRepository
from spine.application.persistence.repositories import SourceObservationRepository
from spine.application.persistence.unit_of_work_activity import unit_of_work_is_active
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationOutcome,
    OperationalOperation,
    OperationalStage,
    SafeOperationalFailureCode,
)
from spine.application.projection_result_mapping import (
    AdapterProjectionReceipt,
    ProjectionMappingConflictError,
    ProjectionMappingError,
    ProjectionMappingResult,
    ProjectionPreparationStatus,
)
from spine.application.projection_publication_faults import (
    ProjectionPublicationFaultInjected,
    ProjectionPublicationFaultRejected,
    ProjectionPublicationQualificationGuard,
)
from spine.application.projection_request_execution import ProjectionRequestCompletion
from spine.application.projection_identity import projection_recovery_entity_id
from spine.memory.contracts import (
    CanonicalBoundary,
    ContextProjection,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionFailureCode,
    ProjectionNamespaceState,
    ProjectionResult,
    ProjectionSourceRevision,
    RebuildCommand,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter
from spine.domain.projections import (
    ProjectionOperation,
    ProjectionPartition,
    ProjectionRun,
    ProjectionRunState,
    ProjectionRunTransition,
    PublishedActivePublication,
)
from spine.domain.ingestion import ProcessingCoverage
from spine.domain.projections import ProjectionConfigVersion as DomainProjectionConfigVersion
from spine.domain.sources import (
    CanonicalBoundary as DomainCanonicalBoundary,
    CanonicalBoundaryMember,
    RevisionKind,
)


class ProjectionRecoveryRequestEnvelope(BaseModel):
    """Trusted, content-free worker input reconstructed from one outbox intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-recovery-request-envelope:v1"] = (
        "projection-recovery-request-envelope:v1"
    )
    event_id: UUID
    workspace_id: UUID
    environment_id: UUID
    payload: (
        ProjectionRecoveryRequestedOutboxPayload
        | ProjectionSuccessorRequestedOutboxPayload
    )


class CanonicalProjectionRecoveryPlan(BaseModel):
    """Canonical immutable inputs selected by the recovery worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["canonical-projection-recovery-plan:v1"] = (
        "canonical-projection-recovery-plan:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    command_id: UUID
    action: ProjectionRecoveryAction
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    command: RebuildCommand


class CanonicalProjectionRecoveryResolver(Protocol):
    async def resolve(
        self, request: ProjectionRecoveryRequestEnvelope
    ) -> CanonicalProjectionRecoveryPlan: ...


class CanonicalProjectionRecoveryUnitOfWork(Protocol):
    @property
    def projections(self) -> ProjectionRepository: ...

    @property
    def sources(self) -> SourceObservationRepository: ...

    @property
    def canonical(self) -> CanonicalAcceptanceRepository: ...

    @property
    def boundaries(self) -> CanonicalBoundaryRepository: ...

    @property
    def ingestion(self) -> IngestionRepository: ...

    async def __aenter__(self) -> "CanonicalProjectionRecoveryUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class PersistenceCanonicalProjectionRecoveryResolver:
    """Create or replay one canonical rebuild successor for a #296 request."""

    def __init__(
        self,
        *,
        uow_factory: Callable[
            [TrustedPersistenceContext], CanonicalProjectionRecoveryUnitOfWork
        ],
        context_factory: Callable[
            [ProjectionRecoveryRequestEnvelope], TrustedPersistenceContext
        ],
        clock: Callable[[], datetime],
        namespace_router: ProjectionNamespaceRouter | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._clock = clock
        self._namespace_router = namespace_router or ProjectionNamespaceRouter()

    async def resolve(
        self,
        request: ProjectionRecoveryRequestEnvelope,
    ) -> CanonicalProjectionRecoveryPlan:
        payload = request.payload
        successor = isinstance(payload, ProjectionSuccessorRequestedOutboxPayload)
        action = (
            ProjectionRecoveryAction.SUCCESSOR if successor else payload.action
        )
        run_id = projection_recovery_entity_id(payload.command_id, "run")
        candidate_id = projection_recovery_entity_id(payload.command_id, "candidate")
        partition_id = projection_recovery_entity_id(payload.command_id, "partition")
        transition_id = projection_recovery_entity_id(payload.command_id, "transition")
        receipt_id = projection_recovery_entity_id(payload.command_id, "run-receipt")
        command_identity = (
            f"successor-{payload.command_id.hex}"
            if successor
            else f"recovery-{payload.command_id.hex}"
        )
        async with self._uow_factory(self._context_factory(request)) as uow:
            publication = await uow.projections.resolve_active_publication(
                payload.projection_kind
            )
            if not isinstance(publication, PublishedActivePublication):
                raise ProjectionMappingError("active projection is unavailable")
            if (
                publication.workspace_id != request.workspace_id
                or publication.environment_id != request.environment_id
                or publication.projection_kind != payload.projection_kind
            ):
                raise ProjectionMappingConflictError(
                    "active projection scope does not match recovery request"
                )

            existing_run = (
                await uow.projections.resolve_run(run_id) if successor else None
            )
            if existing_run is not None:
                if (
                    existing_run.workspace_id != request.workspace_id
                    or existing_run.environment_id != request.environment_id
                    or existing_run.projection_kind != payload.projection_kind
                    or existing_run.operation is not ProjectionOperation.CATCH_UP
                    or existing_run.command_identity != command_identity
                    or existing_run.command_digest != payload.command_digest
                    or existing_run.canonical_boundary_id
                    != payload.canonical_boundary_id
                ):
                    raise ProjectionMappingConflictError(
                        "successor run does not match request"
                    )
                config = await uow.projections.resolve_config_version(
                    existing_run.projection_config_version_id
                )
                if config is None or config.projection_kind != payload.projection_kind:
                    raise ProjectionMappingConflictError(
                        "successor run configuration is unavailable"
                    )
                partition_run_id = existing_run.projection_run_id
                partition_candidate_id = existing_run.projection_candidate_id
            else:
                config = publication.projection_config_version
                partition_run_id = publication.projection_snapshot.projection_run_id
                partition_candidate_id = (
                    publication.projection_snapshot.projection_candidate_id
                )
            active_partitions = await uow.projections.list_partitions(
                partition_run_id
            )
            if len(active_partitions) != 1:
                raise ProjectionMappingError(
                    "recovery requires exactly one active projection partition"
                )
            active_partition = active_partitions[0]
            if (
                active_partition.projection_run_id != partition_run_id
                or active_partition.projection_candidate_id != partition_candidate_id
                or active_partition.workspace_id != request.workspace_id
                or active_partition.environment_id != request.environment_id
            ):
                raise ProjectionMappingConflictError(
                    "active projection partition lineage is invalid"
                )
            if successor:
                boundary_record = await uow.boundaries.resolve(
                    payload.canonical_boundary_id
                )
                if (
                    boundary_record is None
                    or boundary_record.workspace_id != request.workspace_id
                    or boundary_record.environment_id != request.environment_id
                    or boundary_record.projection_kind != payload.projection_kind
                    or boundary_record.manifest_digest
                    != payload.canonical_boundary_digest
                    or boundary_record.accepted_through_generation
                    != payload.accepted_through_generation
                ):
                    raise ProjectionMappingConflictError(
                        "successor boundary does not match request"
                    )
                boundary_members = await uow.boundaries.resolve_members(
                    boundary_record.boundary_id
                )
                source_revisions = await self._successor_source_revisions(
                    uow, boundary_record, boundary_members
                )
                canonical_boundary_id = boundary_record.boundary_id
                canonical_boundary_digest = boundary_record.manifest_digest
                accepted_through_generation = (
                    boundary_record.accepted_through_generation
                )
            else:
                source_revisions = await self._source_revisions(uow, publication)
                canonical_boundary_id = publication.canonical_boundary_id
                canonical_boundary_digest = (
                    publication.canonical_boundary_manifest_digest
                )
                accepted_through_generation = publication.accepted_through_generation
            run = existing_run or ProjectionRun(
                projection_run_id=run_id,
                projection_candidate_id=candidate_id,
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                projection_kind=payload.projection_kind,
                operation=(
                    ProjectionOperation.CATCH_UP
                    if successor
                    else ProjectionOperation.REBUILD
                ),
                projection_config_version_id=config.projection_config_version_id,
                canonical_boundary_id=canonical_boundary_id,
                predecessor_snapshot_id=(
                    publication.projection_snapshot.projection_snapshot_id
                ),
                command_identity=command_identity,
                command_digest=payload.command_digest,
                idempotency_receipt_id=receipt_id,
                started_at=self._clock(),
            )
            created = await uow.projections.create_run(
                ProjectionRunCommand.create(
                    run=run,
                    idempotency_key=IdempotencyKey(
                        f"projection-recovery-run-{payload.command_id.hex}"
                    ),
                )
            )
            canonical_run = created.value
            partitions = await uow.projections.list_partitions(run_id)
            if not created.replay:
                await uow.projections.append_transition(
                    ProjectionRunTransition(
                        projection_run_transition_id=transition_id,
                        projection_run_id=run_id,
                        projection_candidate_id=candidate_id,
                        workspace_id=request.workspace_id,
                        environment_id=request.environment_id,
                        state=ProjectionRunState.REQUESTED,
                        attempt=1,
                        occurred_at=self._clock(),
                    )
                )
                partition = await uow.projections.add_partition(
                    ProjectionPartition(
                        projection_partition_id=partition_id,
                        projection_run_id=run_id,
                        projection_candidate_id=candidate_id,
                        workspace_id=request.workspace_id,
                        environment_id=request.environment_id,
                        security_domain_reference=(
                            active_partition.security_domain_reference
                        ),
                        security_domain_version=active_partition.security_domain_version,
                        partition_identity=active_partition.partition_identity,
                        expected_command_identity=command_identity,
                        expected_command_digest=payload.command_digest,
                        created_at=self._clock(),
                    )
                )
            elif len(partitions) == 1:
                partition = partitions[0]
            else:
                raise ProjectionMappingConflictError(
                    "recovery partition replay is inconsistent"
                )
            await uow.commit()

        candidate = ProjectionCandidate(
            candidate_id=canonical_run.projection_candidate_id,
            run_id=canonical_run.projection_run_id,
            partition_id=partition.projection_partition_id,
        )
        projection_config = ProjectionConfigVersion(
            config_id=config.projection_config_version_id,
            version=str(config.version),
            config_digest=config.config_digest,
            ontology_version=str(config.ontology_version_id),
            adapter_contract_version=config.adapter_contract_version,
        )
        boundary = CanonicalBoundary(
            boundary_id=canonical_boundary_id,
            boundary_digest=canonical_boundary_digest,
            accepted_through_generation=accepted_through_generation,
        )
        namespace = self._namespace_router.route(
            ProjectionNamespaceContext(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                projection_kind=payload.projection_kind,
                projection_candidate=candidate,
                projection_config=projection_config,
                canonical_boundary=boundary,
                state=ProjectionNamespaceState.SHADOW,
            )
        )
        return CanonicalProjectionRecoveryPlan(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            command_id=payload.command_id,
            action=action,
            request_digest=payload.command_digest,
            command=RebuildCommand(
                workspace_id=request.workspace_id,
                environment_id=request.environment_id,
                projection_run_id=canonical_run.projection_run_id,
                projection_candidate=candidate,
                projection_config=projection_config,
                canonical_boundary=boundary,
                source_revisions=source_revisions,
                physical_namespace=namespace,
                command_identity=partition.expected_command_identity,
                command_digest=partition.expected_command_digest,
            ),
        )

    async def _source_revisions(
        self,
        uow: CanonicalProjectionRecoveryUnitOfWork,
        publication: PublishedActivePublication,
    ) -> tuple[ProjectionSourceRevision, ...]:
        members = await uow.projections.list_snapshot_members(
            publication.projection_snapshot.projection_snapshot_id
        )
        references = await uow.projections.list_source_references(
            publication.projection_snapshot.projection_run_id
        )
        metadata = {
            (reference.source_object_id, reference.source_revision_id): (
                reference.processing_receipt_id,
                reference.locator_schema_version,
            )
            for reference in references
        }
        result: list[ProjectionSourceRevision] = []
        for member in members:
            revision = await uow.sources.resolve_revision(member.source_revision_id)
            current = await uow.canonical.resolve_current(member.source_object_id)
            reference_metadata = metadata.get(
                (member.source_object_id, member.source_revision_id)
            )
            if (
                revision is None
                or revision.kind is not RevisionKind.CONTENT
                or current is None
                or current.current_revision_id != member.source_revision_id
                or reference_metadata is None
            ):
                raise ProjectionMappingError(
                    "recovery source revision is missing, stale, or tombstoned"
                )
            result.append(
                ProjectionSourceRevision(
                    source_object_id=member.source_object_id,
                    source_revision_id=member.source_revision_id,
                    revision_digest=revision.revision_digest,
                    revision_number=current.accepted_generation,
                    status="current",
                    processing_receipt_id=reference_metadata[0],
                    locator_contract_version=reference_metadata[1],
                )
            )
        if not result:
            raise ProjectionMappingError("active projection has no rebuildable sources")
        return tuple(result)

    async def _successor_source_revisions(
        self,
        uow: CanonicalProjectionRecoveryUnitOfWork,
        boundary: DomainCanonicalBoundary,
        members: tuple[CanonicalBoundaryMember, ...],
    ) -> tuple[ProjectionSourceRevision, ...]:
        if len({member.source_object_id for member in members}) != len(members):
            raise ProjectionMappingConflictError(
                "successor boundary membership is ambiguous"
            )
        result: list[ProjectionSourceRevision] = []
        for member in members:
            if (
                member.boundary_id != boundary.boundary_id
                or member.workspace_id != boundary.workspace_id
                or member.environment_id != boundary.environment_id
            ):
                raise ProjectionMappingConflictError(
                    "successor boundary membership is foreign"
                )
            revision = await uow.sources.resolve_revision(member.source_revision_id)
            current = await uow.canonical.resolve_current(member.source_object_id)
            if (
                revision is None
                or current is None
                or revision.source_object_id != member.source_object_id
                or revision.workspace_id != boundary.workspace_id
                or revision.environment_id != boundary.environment_id
                or revision.kind is not member.revision_kind
                or current.current_revision_id != member.source_revision_id
                or current.current_revision_kind is not member.revision_kind
                or current.accepted_generation != member.accepted_generation
            ):
                raise ProjectionMappingError(
                    "successor source revision is missing, stale, or foreign"
                )
            if member.revision_kind is RevisionKind.TOMBSTONE:
                continue
            bindings = await uow.ingestion.list_bindings_for_source_revision(
                member.source_object_id,
                member.source_revision_id,
            )
            receipts = []
            for binding in bindings:
                if (
                    binding.source_object_id != member.source_object_id
                    or binding.source_revision_id != member.source_revision_id
                    or binding.workspace_id != boundary.workspace_id
                    or binding.environment_id != boundary.environment_id
                ):
                    raise ProjectionMappingConflictError(
                        "successor processing binding is foreign"
                    )
                receipt = await uow.ingestion.resolve_processing_receipt(
                    binding.ingestion_run_source_id
                )
                if (
                    receipt is not None
                    and receipt.binding_id == binding.binding_id
                    and receipt.source_object_id == member.source_object_id
                    and receipt.source_revision_id == member.source_revision_id
                    and receipt.workspace_id == boundary.workspace_id
                    and receipt.environment_id == boundary.environment_id
                    and receipt.coverage is not ProcessingCoverage.FAILED
                    and receipt.output_reference is not None
                    and receipt.output_digest is not None
                ):
                    receipts.append(receipt)
            if len(receipts) != 1:
                raise ProjectionMappingError(
                    "successor processing receipt is missing or ambiguous"
                )
            receipt = receipts[0]
            if (
                revision.kind is not RevisionKind.CONTENT
                or revision.original_sha256 != receipt.original_sha256
            ):
                raise ProjectionMappingConflictError(
                    "successor processing lineage is invalid"
                )
            result.append(
                ProjectionSourceRevision(
                    source_object_id=member.source_object_id,
                    source_revision_id=member.source_revision_id,
                    revision_digest=revision.revision_digest,
                    revision_number=member.accepted_generation,
                    status="current",
                    processing_receipt_id=receipt.processing_receipt_id,
                    locator_contract_version=receipt.locator_contract_version,
                )
            )
        return tuple(sorted(result, key=lambda value: value.source_object_id.hex))

class ProjectionRecoveryResultRecorder(Protocol):
    """Canonical result bridge; implementations own validation and publication."""

    async def prepare(self, plan: CanonicalProjectionRecoveryPlan) -> bool:
        """Return true when this exact recovery result is already canonical."""

    async def record(
        self,
        plan: CanonicalProjectionRecoveryPlan,
        result: ProjectionResult,
    ) -> ProjectionMappingResult | None: ...


class ProjectionRecoveryLifecycle(Protocol):
    async def advance(
        self,
        plan: CanonicalProjectionRecoveryPlan,
        state: ProjectionRunState,
        *,
        reason_code: str | None = None,
    ) -> None: ...


class PersistenceProjectionRecoveryLifecycle:
    """Idempotently advance the canonical recovery run state machine."""

    def __init__(
        self,
        *,
        uow_factory: Callable[
            [TrustedPersistenceContext], CanonicalProjectionRecoveryUnitOfWork
        ],
        context_factory: Callable[[RebuildCommand], TrustedPersistenceContext],
        clock: Callable[[], datetime],
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._clock = clock

    async def advance(
        self,
        plan: CanonicalProjectionRecoveryPlan,
        state: ProjectionRunState,
        *,
        reason_code: str | None = None,
    ) -> None:
        async with self._uow_factory(self._context_factory(plan.command)) as uow:
            transitions = await uow.projections.list_transitions(
                plan.command.projection_run_id
            )
            by_state = {transition.state: transition for transition in transitions}
            if state in by_state:
                return
            path = self._path(by_state, state)
            predecessor = next(
                (
                    by_state[item]
                    for item in reversed(
                        (
                            ProjectionRunState.REQUESTED,
                            ProjectionRunState.BUILDING,
                            ProjectionRunState.VALIDATING,
                        )
                    )
                    if item in by_state
                ),
                None,
            )
            for next_state in path:
                transition = ProjectionRunTransition(
                    projection_run_transition_id=self._id(
                        plan.command.projection_run_id, next_state
                    ),
                    projection_run_id=plan.command.projection_run_id,
                    projection_candidate_id=(
                        plan.command.projection_candidate.candidate_id
                    ),
                    workspace_id=plan.workspace_id,
                    environment_id=plan.environment_id,
                    predecessor_transition_id=(
                        predecessor.projection_run_transition_id
                        if predecessor is not None
                        else None
                    ),
                    state=next_state,
                    attempt=1,
                    reason_code=reason_code if next_state is state else None,
                    occurred_at=self._clock(),
                )
                predecessor = await uow.projections.append_transition(transition)
            await uow.commit()

    @staticmethod
    def _path(
        by_state: dict[ProjectionRunState, ProjectionRunTransition],
        target: ProjectionRunState,
    ) -> tuple[ProjectionRunState, ...]:
        if ProjectionRunState.COMPLETE in by_state or ProjectionRunState.FAILED in by_state:
            return ()
        current = (
            ProjectionRunState.VALIDATING
            if ProjectionRunState.VALIDATING in by_state
            else ProjectionRunState.BUILDING
            if ProjectionRunState.BUILDING in by_state
            else ProjectionRunState.REQUESTED
        )
        if target is ProjectionRunState.BUILDING:
            return () if current is not ProjectionRunState.REQUESTED else (target,)
        if target is ProjectionRunState.VALIDATING:
            return (
                (ProjectionRunState.BUILDING, target)
                if current is ProjectionRunState.REQUESTED
                else ()
                if current is ProjectionRunState.VALIDATING
                else (target,)
            )
        if target is ProjectionRunState.COMPLETE:
            if current is ProjectionRunState.REQUESTED:
                return (
                    ProjectionRunState.BUILDING,
                    ProjectionRunState.VALIDATING,
                    target,
                )
            if current is ProjectionRunState.BUILDING:
                return (ProjectionRunState.VALIDATING, target)
            return (target,)
        if target is ProjectionRunState.FAILED:
            return (
                (ProjectionRunState.BUILDING, target)
                if current is ProjectionRunState.REQUESTED
                else (target,)
            )
        raise ValueError("unsupported recovery lifecycle target")

    @staticmethod
    def _id(run_id: UUID, state: ProjectionRunState) -> UUID:
        return uuid5(NAMESPACE_URL, f"spine:projection-recovery:{run_id}:{state.value}")


class RebuildAdapterReceiptBuilder(Protocol):
    async def build(
        self,
        command: RebuildCommand,
        result: ProjectionResult,
    ) -> AdapterProjectionReceipt: ...


class RebuildProjectionResultBridge(Protocol):
    async def prepare(
        self, command: RebuildCommand
    ) -> ProjectionPreparationStatus: ...

    async def map(
        self,
        command: RebuildCommand,
        adapter_receipt: AdapterProjectionReceipt,
    ) -> ProjectionMappingResult: ...


class MappingProjectionRecoveryResultRecorder:
    """Reuse the canonical #208 receipt/reference mapping handoff for rebuilds."""

    def __init__(
        self,
        *,
        receipt_builder: RebuildAdapterReceiptBuilder,
        result_bridge: RebuildProjectionResultBridge,
    ) -> None:
        self._receipt_builder = receipt_builder
        self._result_bridge = result_bridge

    async def prepare(self, plan: CanonicalProjectionRecoveryPlan) -> bool:
        return (
            await self._result_bridge.prepare(plan.command)
            is ProjectionPreparationStatus.RECORDED
        )

    async def record(
        self,
        plan: CanonicalProjectionRecoveryPlan,
        result: ProjectionResult,
    ) -> ProjectionMappingResult:
        receipt = await self._receipt_builder.build(plan.command, result)
        return await self._result_bridge.map(plan.command, receipt)


class ProjectionRecoveryRequestConsumer:
    """Consume one committed recovery request with at-least-once semantics."""

    def __init__(
        self,
        *,
        canonical_resolver: CanonicalProjectionRecoveryResolver,
        projection: ContextProjection,
        result_recorder: ProjectionRecoveryResultRecorder,
        completion: ProjectionRequestCompletion | None = None,
        lifecycle: ProjectionRecoveryLifecycle | None = None,
        operational_observer: OperationalObservationEmitter | None = None,
        qualification_guard: ProjectionPublicationQualificationGuard | None = None,
    ) -> None:
        self._canonical_resolver = canonical_resolver
        self._projection = projection
        self._result_recorder = result_recorder
        self._completion = completion
        self._lifecycle = lifecycle
        self._operational_observer = operational_observer
        self._qualification_guard = qualification_guard

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        request = self._request_from(intent, diagnostic_context)
        try:
            plan = await self._canonical_resolver.resolve(request)
        except ProjectionMappingConflictError:
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.integrity_conflict"
            ) from None
        except (ProjectionMappingError, ValueError):
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.canonical_rejected"
            ) from None
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.recovery.resolution_retryable"
            ) from None

        self._validate_plan(request, plan)
        if (
            plan.action is ProjectionRecoveryAction.SUCCESSOR
            and self._qualification_guard is not None
        ):
            try:
                self._qualification_guard.assert_command_allowed(plan.command)
            except ProjectionPublicationFaultRejected as error:
                raise TerminalOutboxConsumerFailure(error.reason_code) from None
        requires_completion = (
            plan.action is ProjectionRecoveryAction.SUCCESSOR
            and self._completion is not None
        )
        try:
            if (
                await self._result_recorder.prepare(plan)
                and not requires_completion
            ):
                await self._advance(plan, ProjectionRunState.COMPLETE)
                return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)
        except ProjectionMappingConflictError:
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.integrity_conflict"
            ) from None
        except ProjectionMappingError:
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.canonical_rejected"
            ) from None
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.recovery.resolution_retryable"
            ) from None

        if unit_of_work_is_active():
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.canonical_uow_open"
            )
        await self._advance(plan, ProjectionRunState.BUILDING)
        try:
            result = await self._projection.rebuild(plan.command)
        except Exception:
            await self._observe(
                request,
                diagnostic_context,
                plan,
                outcome=OperationalObservationOutcome.FAILED,
                failure_code=SafeOperationalFailureCode.PROJECTION_UNAVAILABLE,
            )
            raise RetryableOutboxConsumerFailure(
                "projection.recovery.physical_retryable"
            ) from None

        if result.failure is not None:
            await self._observe(
                request,
                diagnostic_context,
                plan,
                outcome=OperationalObservationOutcome.FAILED,
                failure_code=SafeOperationalFailureCode.PROJECTION_UNAVAILABLE,
            )
            if result.failure.code is ProjectionFailureCode.INTEGRITY_CONFLICT:
                await self._advance(
                    plan,
                    ProjectionRunState.FAILED,
                    reason_code="integrity_conflict",
                )
                raise TerminalOutboxConsumerFailure(
                    "projection.recovery.integrity_conflict"
                )
            if result.failure.retryable:
                raise RetryableOutboxConsumerFailure(
                    "projection.recovery.physical_retryable"
                )
            await self._advance(
                plan,
                ProjectionRunState.FAILED,
                reason_code="physical_rejected",
            )
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.physical_rejected"
            )

        await self._advance(plan, ProjectionRunState.VALIDATING)
        try:
            mapping = await self._result_recorder.record(plan, result)
            if requires_completion:
                assert self._completion is not None
                if mapping is None:
                    raise ProjectionMappingError(
                        "projection recovery mapping is unavailable"
                    )
                await self._completion.complete(mapping)
        except ProjectionMappingConflictError:
            await self._advance(
                plan,
                ProjectionRunState.FAILED,
                reason_code="integrity_conflict",
            )
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.integrity_conflict"
            ) from None
        except ProjectionMappingError:
            await self._advance(
                plan,
                ProjectionRunState.FAILED,
                reason_code="canonical_rejected",
            )
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.canonical_rejected"
            ) from None
        except ProjectionPublicationFaultInjected as error:
            raise RetryableOutboxConsumerFailure(error.reason_code) from None
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.recovery.handoff_retryable"
            ) from None
        await self._advance(plan, ProjectionRunState.COMPLETE)
        await self._observe(
            request,
            diagnostic_context,
            plan,
            outcome=OperationalObservationOutcome.SUCCEEDED,
        )
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)

    async def _advance(
        self,
        plan: CanonicalProjectionRecoveryPlan,
        state: ProjectionRunState,
        *,
        reason_code: str | None = None,
    ) -> None:
        if self._lifecycle is None:
            return
        try:
            await self._lifecycle.advance(plan, state, reason_code=reason_code)
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.recovery.lifecycle_retryable"
            ) from None

    async def _observe(
        self,
        request: ProjectionRecoveryRequestEnvelope,
        diagnostic_context: DiagnosticContext,
        plan: CanonicalProjectionRecoveryPlan,
        *,
        outcome: OperationalObservationOutcome,
        failure_code: SafeOperationalFailureCode | None = None,
    ) -> None:
        if self._operational_observer is None:
            return
        await self._operational_observer.emit(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            diagnostic_context=diagnostic_context,
            component=OperationalComponent.PROJECTION,
            stage=OperationalStage.PROJECTION,
            operation=OperationalOperation.PROJECTION_BUILD,
            outcome=outcome,
            safe_failure_code=failure_code,
            run_id=plan.command.projection_run_id,
        )

    @staticmethod
    def _request_from(
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> ProjectionRecoveryRequestEnvelope:
        if (
            intent.event_id is None
            or intent.environment_id is None
            or intent.event_type
            not in {
                PROJECTION_RECOVERY_REQUESTED_OUTBOX_EVENT_TYPE,
                PROJECTION_SUCCESSOR_REQUESTED_OUTBOX_EVENT_TYPE,
            }
            or intent.schema_version != PROJECTION_OUTBOX_SCHEMA_VERSION
            or type(intent.payload)
            not in {
                ProjectionRecoveryRequestedOutboxPayload,
                ProjectionSuccessorRequestedOutboxPayload,
            }
            or diagnostic_context.trace_id != intent.trace_id
            or diagnostic_context.correlation_id != intent.correlation_id
            or diagnostic_context.causation_id != intent.event_id
        ):
            raise TerminalOutboxConsumerFailure("projection.recovery.invalid")
        payload = intent.payload
        aggregate = intent.aggregate
        successor = isinstance(payload, ProjectionSuccessorRequestedOutboxPayload)
        if (
            aggregate is None
            or aggregate.object_type
            != (
                "projection_successor_request"
                if successor
                else "projection_recovery_request"
            )
            or aggregate.object_id != payload.command_id
            or aggregate.schema_version != 1
            or intent.producer_deduplication_id
            != (
                f"projection-successor:{payload.command_id}"
                if successor
                else f"projection-recovery:{payload.command_id}"
            )
        ):
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.lineage_invalid"
            )
        return ProjectionRecoveryRequestEnvelope(
            event_id=intent.event_id,
            workspace_id=intent.workspace_id,
            environment_id=intent.environment_id,
            payload=payload,
        )

    @staticmethod
    def _validate_plan(
        request: ProjectionRecoveryRequestEnvelope,
        plan: CanonicalProjectionRecoveryPlan,
    ) -> None:
        command = plan.command
        if (
            plan.workspace_id != request.workspace_id
            or plan.environment_id != request.environment_id
            or plan.command_id != request.payload.command_id
            or plan.action
            is not (
                ProjectionRecoveryAction.SUCCESSOR
                if isinstance(
                    request.payload, ProjectionSuccessorRequestedOutboxPayload
                )
                else request.payload.action
            )
            or plan.request_digest != request.payload.command_digest
            or command.workspace_id != request.workspace_id
            or command.environment_id != request.environment_id
            or command.physical_namespace.projection_kind
            != request.payload.projection_kind
        ):
            raise TerminalOutboxConsumerFailure(
                "projection.recovery.context_invalid"
            )


__all__ = [
    "CanonicalProjectionRecoveryPlan",
    "CanonicalProjectionRecoveryResolver",
    "MappingProjectionRecoveryResultRecorder",
    "PersistenceCanonicalProjectionRecoveryResolver",
    "PersistenceProjectionRecoveryLifecycle",
    "ProjectionRecoveryRequestConsumer",
    "ProjectionRecoveryRequestEnvelope",
    "ProjectionRecoveryResultRecorder",
    "ProjectionRecoveryLifecycle",
]
