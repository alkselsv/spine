"""Application seam for the administrator document-ingestion API."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import AsyncIterable
from typing import Protocol
from uuid import UUID

from spine.application.document_ingestion import (
    DocumentIngestionOrchestrator,
    DocumentIngestionResult,
    DocumentIngestionStatus,
    DocumentUploadCommand,
)
from spine.application.ingestion_api_errors import (
    IngestionRunNotFound,
    IngestionServiceUnavailable,
    InvalidIngestionRequest,
    UploadPayloadTooLarge,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import IdempotencyConflictError
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunState,
    ProcessingCoverage,
)


@dataclass(frozen=True, slots=True)
class IngestionSubmission:
    """Stable references returned after the application path has committed."""

    run_id: UUID
    item_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    binding_id: UUID
    status: DocumentIngestionStatus
    replay: bool
    processing_receipt_id: UUID | None
    parser_name: str | None
    warning_ids: tuple[UUID, ...]
    failure_ids: tuple[UUID, ...]

    @classmethod
    def from_result(cls, result: DocumentIngestionResult) -> "IngestionSubmission":
        receipt = result.processing_receipt
        return cls(
            run_id=result.run.ingestion_run_id,
            item_id=result.item.ingestion_run_source_id,
            source_object_id=result.source.source_object_id,
            source_revision_id=result.revision.revision_id,
            binding_id=result.binding.binding_id,
            status=result.status,
            replay=result.replay,
            processing_receipt_id=(receipt.processing_receipt_id if receipt else None),
            parser_name=receipt.parser_name if receipt else None,
            warning_ids=receipt.warning_ids if receipt else (),
            failure_ids=receipt.failure_ids if receipt else (),
        )


@dataclass(frozen=True, slots=True)
class IngestionStatusSnapshot:
    """Content-free read model derived from the ingestion ledger."""

    run_id: UUID
    item_id: UUID
    run_state: IngestionRunState
    item_state: IngestionItemState
    source_object_id: UUID | None
    source_revision_id: UUID | None
    binding_id: UUID | None
    processing_receipt_id: UUID | None
    processing_coverage: ProcessingCoverage | None
    parser_name: str | None
    artifact_id: UUID | None
    artifact_digest: str | None
    warning_ids: tuple[UUID, ...]
    failure_ids: tuple[UUID, ...]


class DocumentIngestionService(Protocol):
    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: DocumentUploadCommand,
        chunks: AsyncIterable[bytes],
    ) -> IngestionSubmission: ...

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        item_id: UUID,
    ) -> IngestionStatusSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class OrchestratedDocumentIngestionService:
    """Adapt the durable #152 orchestrator and ledger to the HTTP seam."""

    orchestrator: DocumentIngestionOrchestrator
    uow_factory: UnitOfWorkFactory

    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: DocumentUploadCommand,
        chunks: AsyncIterable[bytes],
    ) -> IngestionSubmission:
        self._assert_scope(context, command.workspace_id, command.environment_id)
        result = await self.orchestrator.ingest(
            request=command,
            chunks=chunks,
        )
        return IngestionSubmission.from_result(result)

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        item_id: UUID,
    ) -> IngestionStatusSnapshot | None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidIngestionRequest()
        self._assert_scope(
            context,
            context.scope.workspace_id,
            context.scope.environment_id,
        )
        async with self.uow_factory(context) as uow:
            state = await uow.ingestion.derive_run_state(run_id)
            item = await uow.ingestion.resolve_item(item_id)
            if (
                state is None
                or item is None
                or item.ingestion_run_id != run_id
                or item.workspace_id != context.scope.workspace_id
                or item.environment_id != context.scope.environment_id
            ):
                await uow.rollback()
                return None
            head = await uow.ingestion.resolve_item_head(item_id)
            binding = await uow.ingestion.resolve_binding(item_id)
            receipt = await uow.ingestion.resolve_processing_receipt(item_id)
            await uow.commit()

        if head is None or not isinstance(head.state, IngestionItemState):
            raise IngestionServiceUnavailable()
        return IngestionStatusSnapshot(
            run_id=run_id,
            item_id=item_id,
            run_state=state.state,
            item_state=head.state,
            source_object_id=(binding.source_object_id if binding else None),
            source_revision_id=(binding.source_revision_id if binding else None),
            binding_id=(binding.binding_id if binding else None),
            processing_receipt_id=(receipt.processing_receipt_id if receipt else None),
            processing_coverage=(receipt.coverage if receipt else None),
            parser_name=(receipt.parser_name if receipt else None),
            artifact_id=(receipt.output_reference if receipt else None),
            artifact_digest=(receipt.output_digest if receipt else None),
            warning_ids=receipt.warning_ids if receipt else (),
            failure_ids=receipt.failure_ids if receipt else (),
        )

    @staticmethod
    def _assert_scope(
        context: TrustedPersistenceContext,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidIngestionRequest()
        if (
            context.scope.workspace_id != workspace_id
            or context.scope.environment_id != environment_id
        ):
            raise InvalidIngestionRequest()


__all__ = [
    "DocumentIngestionService",
    "IngestionRunNotFound",
    "IngestionServiceUnavailable",
    "IngestionStatusSnapshot",
    "IngestionSubmission",
    "InvalidIngestionRequest",
    "OrchestratedDocumentIngestionService",
    "UploadPayloadTooLarge",
]
