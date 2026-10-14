"""Execute committed projection requests through the provider-neutral seam.

The consumer owns no canonical lifecycle state.  It resolves a command from
canonical identities, invokes the physical projection outside persistence, and
hands the adapter result to the approved Issue #208 mapping bridge.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from spine.application.diagnostics import DiagnosticContext
from spine.application.document_ingestion import (
    PROJECTION_REQUEST_EVENT_TYPE,
    PROJECTION_REQUEST_SCHEMA_VERSION,
    ProjectionRequestPayload,
)
from spine.application.outbox import (
    OutboxConsumerSuccess,
    RetryableOutboxConsumerFailure,
    TerminalOutboxConsumerFailure,
)
from spine.application.persistence.outbox import OutboxIntent
from spine.application.persistence.boundary import CanonicalBoundaryRepository
from spine.application.persistence.canonical import CanonicalAcceptanceRepository
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.ingestion import IngestionRepository
from spine.application.persistence.projection import ProjectionRepository
from spine.application.persistence.repositories import SourceObservationRepository
from spine.application.persistence.unit_of_work_activity import (
    unit_of_work_is_active,
)
from spine.application.projection_result_mapping import (
    AdapterProjectionReceipt,
    ProjectionMappingConflictError,
    ProjectionMappingError,
    ProjectionMappingResult,
    ProjectionPreparationStatus,
)
from spine.memory.contracts import (
    CanonicalBoundary,
    ContextProjection,
    ProjectCommand,
    ProjectionCandidate,
    ProjectionConfigVersion,
    ProjectionFailureCode,
    ProjectionNamespace,
    ProjectionResult,
    ProjectionSourceRevision,
)
from spine.memory.namespace import ProjectionNamespaceContext, ProjectionNamespaceRouter
from spine.domain.ingestion import IngestionItemState, ProcessingCoverage
from spine.domain.projections import (
    ProjectionOperation as CanonicalProjectionOperation,
    ProjectionCandidateState,
)
from spine.domain.sources import RevisionKind


class ProjectionRequestEnvelope(BaseModel):
    """Content-free canonical lookup input reconstructed from one outbox intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["projection-request-envelope:v1"] = (
        "projection-request-envelope:v1"
    )
    event_id: UUID
    workspace_id: UUID
    environment_id: UUID
    payload: ProjectionRequestPayload


class CanonicalProjectionRequestContext(BaseModel):
    """Issue #7-owned inputs from which the consumer constructs one command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["canonical-projection-request-context:v1"] = (
        "canonical-projection-request-context:v1"
    )
    workspace_id: UUID
    environment_id: UUID
    projection_run_id: UUID
    projection_candidate: ProjectionCandidate
    projection_config: ProjectionConfigVersion
    canonical_boundary: CanonicalBoundary
    source_revision: ProjectionSourceRevision
    physical_namespace: ProjectionNamespace
    command_identity: str
    command_digest: str


class CanonicalProjectionRequestResolver(Protocol):
    """Resolve Issue #7 context and close its read UoW before returning."""

    async def resolve(
        self,
        request: ProjectionRequestEnvelope,
    ) -> CanonicalProjectionRequestContext: ...


class CanonicalProjectionRequestUnitOfWork(Protocol):
    """Read-only canonical repositories used by the production resolver."""

    @property
    def ingestion(self) -> IngestionRepository: ...

    @property
    def sources(self) -> SourceObservationRepository: ...

    @property
    def canonical(self) -> CanonicalAcceptanceRepository: ...

    @property
    def boundaries(self) -> CanonicalBoundaryRepository: ...

    @property
    def projections(self) -> ProjectionRepository: ...

    async def __aenter__(self) -> "CanonicalProjectionRequestUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class PersistenceCanonicalProjectionRequestResolver:
    """Resolve one projection command entirely from canonical persistence ports."""

    def __init__(
        self,
        *,
        uow_factory: Callable[
            [TrustedPersistenceContext], CanonicalProjectionRequestUnitOfWork
        ],
        context_factory: Callable[
            [ProjectionRequestEnvelope], TrustedPersistenceContext
        ],
        namespace_router: ProjectionNamespaceRouter | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_factory = context_factory
        self._namespace_router = namespace_router or ProjectionNamespaceRouter()

    async def resolve(
        self,
        request: ProjectionRequestEnvelope,
    ) -> CanonicalProjectionRequestContext:
        payload = request.payload
        async with self._uow_factory(self._context_factory(request)) as uow:
            item = await uow.ingestion.resolve_item(payload.ingestion_run_source_id)
            if (
                item is None
                or item.ingestion_run_id != payload.ingestion_run_id
                or (item.workspace_id, item.environment_id)
                != (request.workspace_id, request.environment_id)
            ):
                self._reject()

            item_head = await uow.ingestion.resolve_item_head(
                payload.ingestion_run_source_id
            )
            if item_head is None or item_head.state is not IngestionItemState.PROJECTION_REQUESTED:
                self._reject()

            binding = await uow.ingestion.resolve_binding(
                payload.ingestion_run_source_id
            )
            if (
                binding is None
                or binding.ingestion_run_source_id != item.ingestion_run_source_id
                or binding.source_revision_id != payload.source_revision_id
                or binding.workspace_id != request.workspace_id
                or binding.environment_id != request.environment_id
            ):
                self._reject()

            receipt = await uow.ingestion.resolve_processing_receipt(
                payload.ingestion_run_source_id
            )
            if (
                receipt is None
                or receipt.processing_receipt_id != payload.processing_receipt_id
                or receipt.ingestion_run_source_id != item.ingestion_run_source_id
                or receipt.binding_id != binding.binding_id
                or receipt.source_revision_id != payload.source_revision_id
                or receipt.workspace_id != request.workspace_id
                or receipt.environment_id != request.environment_id
                or receipt.processor_kind != payload.processor_kind
                or receipt.parser_version != payload.parser_version
                or receipt.parser_config_reference != payload.parser_config_reference
                or receipt.locator_contract_version != payload.locator_contract_version
                or receipt.coverage is ProcessingCoverage.FAILED
                or receipt.output_reference is None
                or receipt.output_digest is None
            ):
                self._reject()

            revision = await uow.sources.resolve_revision(payload.source_revision_id)
            if (
                revision is None
                or revision.revision_id != payload.source_revision_id
                or revision.source_object_id != binding.source_object_id
                or revision.workspace_id != request.workspace_id
                or revision.environment_id != request.environment_id
                or revision.kind is not RevisionKind.CONTENT
                or revision.original_sha256 != receipt.original_sha256
            ):
                self._reject()

            current = await uow.canonical.resolve_current(revision.source_object_id)
            if (
                current is None
                or current.workspace_id != request.workspace_id
                or current.environment_id != request.environment_id
                or current.current_revision_id != revision.revision_id
                or current.current_revision_kind is not RevisionKind.CONTENT
            ):
                self._reject()

            runs = await uow.projections.list_runs_for_source_revision(
                revision.source_object_id, revision.revision_id
            )
            if len(runs) != 1:
                self._reject()
            run = runs[0]
            if (
                run.workspace_id != request.workspace_id
                or run.environment_id != request.environment_id
                or run.operation is not CanonicalProjectionOperation.BUILD
                or run.candidate_state is not ProjectionCandidateState.OPEN
            ):
                self._reject()

            config = await uow.projections.resolve_config_version(
                run.projection_config_version_id
            )
            if (
                config is None
                or config.workspace_id != run.workspace_id
                or config.environment_id != run.environment_id
                or config.projection_kind != run.projection_kind
                or config.locator_contract_version
                != receipt.locator_contract_version
            ):
                self._reject()

            boundary = await uow.boundaries.resolve(run.canonical_boundary_id)
            if (
                boundary is None
                or boundary.workspace_id != run.workspace_id
                or boundary.environment_id != run.environment_id
                or boundary.projection_kind != run.projection_kind
            ):
                self._reject()
            members = await uow.boundaries.resolve_members(boundary.boundary_id)
            if len(members) != 1:
                self._reject()
            member = members[0]
            if (
                member.boundary_id != boundary.boundary_id
                or member.workspace_id != run.workspace_id
                or member.environment_id != run.environment_id
                or member.source_object_id != revision.source_object_id
                or member.source_revision_id != revision.revision_id
                or member.revision_kind is not RevisionKind.CONTENT
                or current.accepted_generation != member.accepted_generation
            ):
                self._reject()

            partitions = await uow.projections.list_partitions(run.projection_run_id)
            if len(partitions) != 1:
                self._reject()
            partition = partitions[0]
            if (
                partition.projection_run_id != run.projection_run_id
                or partition.projection_candidate_id != run.projection_candidate_id
                or partition.workspace_id != run.workspace_id
                or partition.environment_id != run.environment_id
            ):
                self._reject()

            projection_config = ProjectionConfigVersion(
                config_id=config.projection_config_version_id,
                version=str(config.version),
                config_digest=config.config_digest,
                ontology_version=str(config.ontology_version_id),
                adapter_contract_version=config.adapter_contract_version,
            )
            canonical_boundary = CanonicalBoundary(
                boundary_id=boundary.boundary_id,
                boundary_digest=boundary.manifest_digest,
                accepted_through_generation=boundary.accepted_through_generation,
            )
            candidate = ProjectionCandidate(
                candidate_id=run.projection_candidate_id,
                run_id=run.projection_run_id,
                partition_id=partition.projection_partition_id,
            )
            physical_namespace = self._namespace_router.route(
                ProjectionNamespaceContext(
                    workspace_id=run.workspace_id,
                    environment_id=run.environment_id,
                    projection_kind=run.projection_kind,
                    projection_candidate=candidate,
                    projection_config=projection_config,
                    canonical_boundary=canonical_boundary,
                )
            )
            return CanonicalProjectionRequestContext(
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                projection_run_id=run.projection_run_id,
                projection_candidate=candidate,
                projection_config=projection_config,
                canonical_boundary=canonical_boundary,
                source_revision=ProjectionSourceRevision(
                    source_object_id=revision.source_object_id,
                    source_revision_id=revision.revision_id,
                    revision_digest=revision.revision_digest,
                    revision_number=member.accepted_generation,
                    status="current",
                    processing_receipt_id=receipt.processing_receipt_id,
                    locator_contract_version=receipt.locator_contract_version,
                ),
                physical_namespace=physical_namespace,
                command_identity=partition.expected_command_identity,
                command_digest=partition.expected_command_digest,
            )

    @staticmethod
    def _reject() -> None:
        raise ProjectionMappingError("canonical projection request was rejected")


class AdapterProjectionReceiptBuilder(Protocol):
    """Attach adapter-owned metadata and provenance to a physical result."""

    async def build(
        self,
        command: ProjectCommand,
        result: ProjectionResult,
    ) -> AdapterProjectionReceipt: ...


class ProjectionResultRecorder(Protocol):
    """The approved Issue #208 result-mapping and canonical persistence seam."""

    async def prepare(
        self,
        command: ProjectCommand,
    ) -> ProjectionPreparationStatus: ...

    async def map(
        self,
        command: ProjectCommand,
        adapter_receipt: AdapterProjectionReceipt,
    ) -> ProjectionMappingResult: ...


class ProjectionRequestConsumer:
    """Consume one committed per-source projection request at least once."""

    def __init__(
        self,
        *,
        canonical_resolver: CanonicalProjectionRequestResolver,
        projection: ContextProjection,
        receipt_builder: AdapterProjectionReceiptBuilder,
        result_bridge: ProjectionResultRecorder,
    ) -> None:
        self._canonical_resolver = canonical_resolver
        self._projection = projection
        self._receipt_builder = receipt_builder
        self._result_bridge = result_bridge

    async def consume(
        self,
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> OutboxConsumerSuccess:
        request = self._request_from(intent, diagnostic_context)
        try:
            canonical = await self._canonical_resolver.resolve(request)
        except ProjectionMappingConflictError:
            raise TerminalOutboxConsumerFailure(
                "projection.request.integrity_conflict"
            ) from None
        except ProjectionMappingError:
            raise TerminalOutboxConsumerFailure(
                "projection.request.canonical_rejected"
            ) from None
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.request.resolution_retryable"
            ) from None
        if (
            canonical.workspace_id != request.workspace_id
            or canonical.environment_id != request.environment_id
        ):
            raise TerminalOutboxConsumerFailure(
                "projection.request.context_invalid"
            )
        command = ProjectCommand(
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            projection_run_id=canonical.projection_run_id,
            projection_candidate=canonical.projection_candidate,
            projection_config=canonical.projection_config,
            canonical_boundary=canonical.canonical_boundary,
            source_revisions=(canonical.source_revision,),
            physical_namespace=canonical.physical_namespace,
            command_identity=canonical.command_identity,
            command_digest=canonical.command_digest,
        )
        self._validate_command(request, command)

        try:
            replay = await self._result_bridge.prepare(command)
        except ProjectionMappingConflictError:
            raise TerminalOutboxConsumerFailure(
                "projection.request.integrity_conflict"
            ) from None
        except ProjectionMappingError:
            raise TerminalOutboxConsumerFailure(
                "projection.request.canonical_rejected"
            ) from None
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.request.resolution_retryable"
            ) from None
        if replay is ProjectionPreparationStatus.RECORDED:
            return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)

        # Physical projection is deliberately outside every PostgreSQL UnitOfWork.
        if unit_of_work_is_active():
            raise TerminalOutboxConsumerFailure(
                "projection.request.canonical_uow_open"
            )
        try:
            result = await self._projection.project(command)
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.request.physical_retryable"
            ) from None
        if result.failure is not None:
            if result.failure.code is ProjectionFailureCode.INTEGRITY_CONFLICT:
                raise TerminalOutboxConsumerFailure(
                    "projection.request.integrity_conflict"
                )
            if result.failure.retryable:
                raise RetryableOutboxConsumerFailure(
                    "projection.request.physical_retryable"
                )
            raise TerminalOutboxConsumerFailure(
                "projection.request.physical_rejected"
            )
        try:
            receipt = await self._receipt_builder.build(command, result)
            await self._result_bridge.map(command, receipt)
        except ProjectionMappingConflictError:
            raise TerminalOutboxConsumerFailure(
                "projection.request.integrity_conflict"
            ) from None
        except ProjectionMappingError:
            raise TerminalOutboxConsumerFailure(
                "projection.request.canonical_rejected"
            ) from None
        except Exception:
            raise RetryableOutboxConsumerFailure(
                "projection.request.handoff_retryable"
            ) from None
        return OutboxConsumerSuccess(diagnostic_context=diagnostic_context)

    @staticmethod
    def _request_from(
        intent: OutboxIntent,
        diagnostic_context: DiagnosticContext,
    ) -> ProjectionRequestEnvelope:
        event_id = intent.event_id
        environment_id = intent.environment_id
        if (
            event_id is None
            or environment_id is None
            or intent.event_type != PROJECTION_REQUEST_EVENT_TYPE
            or intent.schema_version != PROJECTION_REQUEST_SCHEMA_VERSION
            or type(intent.payload) is not ProjectionRequestPayload
            or diagnostic_context.trace_id != intent.trace_id
            or diagnostic_context.correlation_id != intent.correlation_id
            or diagnostic_context.causation_id != event_id
        ):
            raise TerminalOutboxConsumerFailure("projection.request.invalid")
        payload = intent.payload
        aggregate = intent.aggregate
        if (
            intent.correlation_id != payload.ingestion_run_id
            or intent.causation_id is None
            or aggregate is None
            or aggregate.object_type != "ingestion_run_source"
            or aggregate.object_id != payload.ingestion_run_source_id
            or aggregate.schema_version != 1
            or intent.producer_deduplication_id
            != f"projection-request:{payload.ingestion_run_source_id}"
        ):
            raise TerminalOutboxConsumerFailure("projection.request.lineage_invalid")
        return ProjectionRequestEnvelope(
            event_id=event_id,
            workspace_id=intent.workspace_id,
            environment_id=environment_id,
            payload=payload,
        )

    @staticmethod
    def _validate_command(
        request: ProjectionRequestEnvelope,
        command: ProjectCommand,
    ) -> None:
        payload = request.payload
        if (
            command.workspace_id != request.workspace_id
            or command.environment_id != request.environment_id
            or len(command.source_revisions) != 1
            or command.source_revisions[0].source_revision_id
            != payload.source_revision_id
            or command.source_revisions[0].processing_receipt_id
            != payload.processing_receipt_id
            or command.source_revisions[0].locator_contract_version
            != payload.locator_contract_version
        ):
            raise TerminalOutboxConsumerFailure("projection.request.context_invalid")


__all__ = [
    "AdapterProjectionReceiptBuilder",
    "CanonicalProjectionRequestContext",
    "CanonicalProjectionRequestResolver",
    "CanonicalProjectionRequestUnitOfWork",
    "PersistenceCanonicalProjectionRequestResolver",
    "ProjectionRequestConsumer",
    "ProjectionRequestEnvelope",
    "ProjectionResultRecorder",
]
