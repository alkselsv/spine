"""Application seam for the durable batch document-ingestion HTTP API."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from spine.application.batch_ingestion import (
    BatchDocumentIngestionOrchestrator,
    BatchDocumentInput,
    BatchDocumentUploadCommand,
)
from spine.application.ingestion_api_errors import (
    IngestionServiceUnavailable,
    InvalidIngestionRequest,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunState,
    ProcessingCoverage,
)


@dataclass(frozen=True, slots=True)
class BatchIngestionWarningSnapshot:
    warning_id: UUID
    attempt: int
    stage: str
    code: str


@dataclass(frozen=True, slots=True)
class BatchIngestionFailureSnapshot:
    failure_id: UUID
    attempt: int
    stage: str
    code: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class BatchIngestionItemSnapshot:
    """Content-free public data for one independently progressing batch item."""

    item_id: UUID
    input_ordinal: int
    input_identity: str
    item_state: IngestionItemState
    attempt: int
    retryable: bool
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
    warnings: tuple[BatchIngestionWarningSnapshot, ...] = ()
    failures: tuple[BatchIngestionFailureSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class BatchIngestionSnapshot:
    """Stable batch reference and ordered, tenant-scoped item outcomes."""

    run_id: UUID
    run_state: IngestionRunState
    items: tuple[BatchIngestionItemSnapshot, ...]
    replay: bool


class BatchDocumentIngestionService(Protocol):
    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: BatchDocumentUploadCommand,
        items: Sequence[BatchDocumentInput],
    ) -> BatchIngestionSnapshot: ...

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
    ) -> BatchIngestionSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class OrchestratedBatchDocumentIngestionService:
    """Adapt the N1 batch coordinator and canonical ledger to the API seam."""

    orchestrator: BatchDocumentIngestionOrchestrator
    uow_factory: UnitOfWorkFactory

    async def submit(
        self,
        *,
        context: TrustedPersistenceContext,
        command: BatchDocumentUploadCommand,
        items: Sequence[BatchDocumentInput],
    ) -> BatchIngestionSnapshot:
        self._assert_scope(context, command.workspace_id, command.environment_id)
        result = await self.orchestrator.ingest(request=command, items=items)
        snapshot = await self._status(
            context=context,
            run_id=result.run.ingestion_run_id,
            replay=result.replay,
        )
        if snapshot is None:
            raise IngestionServiceUnavailable()
        return snapshot

    async def status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
    ) -> BatchIngestionSnapshot | None:
        return await self._status(context=context, run_id=run_id, replay=False)

    async def _status(
        self,
        *,
        context: TrustedPersistenceContext,
        run_id: UUID,
        replay: bool,
    ) -> BatchIngestionSnapshot | None:
        if not isinstance(context.scope, EnvironmentScope):
            raise InvalidIngestionRequest()
        async with self.uow_factory(context) as uow:
            state = await uow.ingestion.derive_run_state(run_id)
            if state is None:
                await uow.rollback()
                return None
            item_snapshots: list[BatchIngestionItemSnapshot] = []
            for item in state.items:
                head = await uow.ingestion.resolve_item_head(
                    item.ingestion_run_source_id
                )
                if head is None or not isinstance(head.state, IngestionItemState):
                    raise IngestionServiceUnavailable()
                binding = await uow.ingestion.resolve_binding(
                    item.ingestion_run_source_id
                )
                receipt = await uow.ingestion.resolve_processing_receipt(
                    item.ingestion_run_source_id
                )
                warnings = await uow.ingestion.resolve_warnings(
                    item.ingestion_run_source_id
                )
                failures = await uow.ingestion.resolve_failures(
                    item.ingestion_run_source_id
                )
                item_snapshots.append(
                    BatchIngestionItemSnapshot(
                        item_id=item.ingestion_run_source_id,
                        input_ordinal=item.input_ordinal,
                        input_identity=item.input_identity,
                        item_state=head.state,
                        attempt=head.attempt,
                        retryable=(
                            head.state is IngestionItemState.PROCESSING_FAILED
                            and any(failure.retryable for failure in failures)
                        ),
                        source_object_id=(binding.source_object_id if binding else None),
                        source_revision_id=(
                            binding.source_revision_id if binding else None
                        ),
                        binding_id=(binding.binding_id if binding else None),
                        processing_receipt_id=(
                            receipt.processing_receipt_id if receipt else None
                        ),
                        processing_coverage=(receipt.coverage if receipt else None),
                        parser_name=(receipt.parser_name if receipt else None),
                        artifact_id=(receipt.output_reference if receipt else None),
                        artifact_digest=(receipt.output_digest if receipt else None),
                        warning_ids=tuple(
                            warning.ingestion_warning_id for warning in warnings
                        ),
                        failure_ids=tuple(
                            failure.ingestion_failure_id for failure in failures
                        ),
                        warnings=tuple(
                            BatchIngestionWarningSnapshot(
                                warning_id=warning.ingestion_warning_id,
                                attempt=warning.attempt,
                                stage=warning.stage.value,
                                code=warning.code,
                            )
                            for warning in warnings
                        ),
                        failures=tuple(
                            BatchIngestionFailureSnapshot(
                                failure_id=failure.ingestion_failure_id,
                                attempt=failure.attempt,
                                stage=failure.stage.value,
                                code=failure.code,
                                retryable=failure.retryable,
                            )
                            for failure in failures
                        ),
                    )
                )
            await uow.commit()
        return BatchIngestionSnapshot(
            run_id=run_id,
            run_state=state.state,
            items=tuple(item_snapshots),
            replay=replay,
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
    "BatchDocumentIngestionService",
    "BatchIngestionFailureSnapshot",
    "BatchIngestionItemSnapshot",
    "BatchIngestionSnapshot",
    "BatchIngestionWarningSnapshot",
    "OrchestratedBatchDocumentIngestionService",
]
