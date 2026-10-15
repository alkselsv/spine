"""Read-side application contracts for durable ingestion history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedPersistenceContext,
)
from spine.application.persistence.ingestion import IngestionRepository
from spine.application.persistence.repositories import SourceObservationRepository
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunSource,
    IngestionRunState,
    ProcessingCoverage,
)
from spine.domain.sources import RevisionKind


SnapshotT = TypeVar("SnapshotT")


@dataclass(frozen=True, slots=True)
class IngestionHistoryPosition:
    occurred_at: datetime
    identity: UUID


@dataclass(frozen=True, slots=True)
class IngestionHistoryWarningSnapshot:
    warning_id: UUID
    attempt: int
    stage: str
    code: str


@dataclass(frozen=True, slots=True)
class IngestionHistoryFailureSnapshot:
    failure_id: UUID
    attempt: int
    stage: str
    code: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class DocumentProcessingHistorySnapshot:
    run_id: UUID
    run_state: IngestionRunState
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
    warnings: tuple[IngestionHistoryWarningSnapshot, ...]
    failures: tuple[IngestionHistoryFailureSnapshot, ...]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionRunHistorySnapshot:
    run_id: UUID
    requested_at: datetime
    run_state: IngestionRunState
    items: tuple[DocumentProcessingHistorySnapshot, ...]


@dataclass(frozen=True, slots=True)
class IngestionHistoryPage(Generic[SnapshotT]):
    items: tuple[SnapshotT, ...]
    has_more: bool


class IngestionHistoryService(Protocol):
    async def list_runs(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: IngestionHistoryPosition | None,
    ) -> IngestionHistoryPage[IngestionRunHistorySnapshot]: ...

    async def list_processing_history(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: IngestionHistoryPosition | None,
    ) -> IngestionHistoryPage[DocumentProcessingHistorySnapshot]: ...


@dataclass(frozen=True, slots=True)
class OrchestratedIngestionHistoryService:
    """Project scoped ledger records into content-free history read models."""

    uow_factory: UnitOfWorkFactory

    async def list_runs(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: IngestionHistoryPosition | None,
    ) -> IngestionHistoryPage[IngestionRunHistorySnapshot]:
        self._validate_scope(context)
        async with self.uow_factory(context) as uow:
            views = await uow.ingestion.list_runs(
                limit=limit + 1,
                after=_repository_position(after),
            )
            has_more = len(views) > limit
            selected = views[:limit]
            snapshots_list: list[IngestionRunHistorySnapshot] = []
            for view in selected:
                item_snapshot_list: list[DocumentProcessingHistorySnapshot] = []
                for item in view.items:
                    snapshot = await self._item_snapshot(
                        uow.ingestion,
                        uow.sources,
                        item,
                        view.state,
                    )
                    if snapshot is not None:
                        item_snapshot_list.append(snapshot)
                snapshots_list.append(
                    IngestionRunHistorySnapshot(
                        run_id=view.run.ingestion_run_id,
                        requested_at=view.run.requested_at,
                        run_state=view.state,
                        items=tuple(item_snapshot_list),
                    )
                )
            await uow.commit()
        snapshots = tuple(snapshots_list)
        return IngestionHistoryPage(items=snapshots, has_more=has_more)

    async def list_processing_history(
        self,
        *,
        context: TrustedPersistenceContext,
        limit: int,
        after: IngestionHistoryPosition | None,
    ) -> IngestionHistoryPage[DocumentProcessingHistorySnapshot]:
        self._validate_scope(context)
        async with self.uow_factory(context) as uow:
            items = await uow.ingestion.list_items(
                limit=limit + 1,
                after=_repository_position(after),
            )
            has_more = len(items) > limit
            selected = items[:limit]
            snapshots: list[DocumentProcessingHistorySnapshot] = []
            for item in selected:
                run = await uow.ingestion.derive_run_state(item.ingestion_run_id)
                if run is None:
                    continue
                snapshot = await self._item_snapshot(
                    uow.ingestion,
                    uow.sources,
                    item,
                    run.state,
                )
                if snapshot is not None:
                    snapshots.append(snapshot)
            await uow.commit()
        return IngestionHistoryPage(items=tuple(snapshots), has_more=has_more)

    @staticmethod
    def _validate_scope(context: TrustedPersistenceContext) -> None:
        if not isinstance(context.scope, EnvironmentScope):
            raise ValueError("ingestion history requires an environment scope")

    async def _item_snapshot(
        self,
        repository: IngestionRepository,
        sources: SourceObservationRepository,
        item: IngestionRunSource,
        run_state: IngestionRunState,
    ) -> DocumentProcessingHistorySnapshot | None:
        head = await repository.resolve_item_head(item.ingestion_run_source_id)
        if head is None or not isinstance(head.state, IngestionItemState):
            raise RuntimeError("ingestion item transition head is unavailable")
        binding = await repository.resolve_binding(item.ingestion_run_source_id)
        if binding is not None:
            revision = await sources.resolve_revision(binding.source_revision_id)
            if revision is None or revision.kind is RevisionKind.TOMBSTONE:
                return None
        receipt = await repository.resolve_processing_receipt(item.ingestion_run_source_id)
        warnings = await repository.resolve_warnings(item.ingestion_run_source_id)
        failures = await repository.resolve_failures(item.ingestion_run_source_id)
        return DocumentProcessingHistorySnapshot(
            run_id=item.ingestion_run_id,
            run_state=run_state,
            item_id=item.ingestion_run_source_id,
            input_ordinal=item.input_ordinal,
            input_identity=item.input_identity,
            item_state=head.state,
            attempt=head.attempt,
            retryable=(
                head.state is IngestionItemState.PROCESSING_FAILED
                and any(failure.retryable for failure in failures)
            ),
            source_object_id=binding.source_object_id if binding else None,
            source_revision_id=binding.source_revision_id if binding else None,
            binding_id=binding.binding_id if binding else None,
            processing_receipt_id=receipt.processing_receipt_id if receipt else None,
            processing_coverage=receipt.coverage if receipt else None,
            parser_name=receipt.parser_name if receipt else None,
            artifact_id=receipt.output_reference if receipt else None,
            artifact_digest=receipt.output_digest if receipt else None,
            warning_ids=tuple(warning.ingestion_warning_id for warning in warnings),
            failure_ids=tuple(failure.ingestion_failure_id for failure in failures),
            warnings=tuple(
                IngestionHistoryWarningSnapshot(
                    warning_id=warning.ingestion_warning_id,
                    attempt=warning.attempt,
                    stage=warning.stage.value,
                    code=warning.code,
                )
                for warning in warnings
            ),
            failures=tuple(
                IngestionHistoryFailureSnapshot(
                    failure_id=failure.ingestion_failure_id,
                    attempt=failure.attempt,
                    stage=failure.stage.value,
                    code=failure.code,
                    retryable=failure.retryable,
                )
                for failure in failures
            ),
            occurred_at=item.created_at,
        )


__all__ = [
    "DocumentProcessingHistorySnapshot",
    "IngestionHistoryFailureSnapshot",
    "IngestionHistoryPage",
    "IngestionHistoryPosition",
    "IngestionHistoryService",
    "IngestionHistoryWarningSnapshot",
    "IngestionRunHistorySnapshot",
    "OrchestratedIngestionHistoryService",
]


def _repository_position(
    position: IngestionHistoryPosition | None,
) -> tuple[datetime, UUID] | None:
    return None if position is None else (position.occurred_at, position.identity)
