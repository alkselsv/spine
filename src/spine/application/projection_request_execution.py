"""Execute committed projection requests through the provider-neutral seam.

The consumer owns no canonical lifecycle state.  It resolves a command from
canonical identities, invokes the physical projection outside persistence, and
hands the adapter result to the approved Issue #208 mapping bridge.
"""

from __future__ import annotations

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
    "ProjectionRequestConsumer",
    "ProjectionRequestEnvelope",
    "ProjectionResultRecorder",
]
