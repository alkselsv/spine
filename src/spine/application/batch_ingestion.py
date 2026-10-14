"""Application coordination for idempotent multi-file document ingestion.

The batch coordinator owns only the parent run and its item identities.  The
single-document orchestrator remains responsible for every source, admission,
parser and receipt boundary for one item.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.application.document_ingestion import (
    DocumentIngestionOrchestrator,
    DocumentIngestionResult,
    DocumentIngestionStatus,
    DocumentUploadCommand,
)
from spine.application.persistence.context import PersistenceOperation, TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence.ingestion import IngestionRunCommand
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRun,
    IngestionRunSource,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    IngestionFailure,
    IngestionWarning,
    ProcessingCoverage,
    ProcessingReceipt,
)


class BatchIngestionStatus(str, Enum):
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchDocumentUploadItem(BaseModel):
    """Content-free command metadata for one batch input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_identity: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    purpose: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    declared_media_type: str | None = Field(default=None, min_length=1, max_length=255)
    declared_filename: str | None = Field(default=None, min_length=1, max_length=255)
    expected_sha256: str | None = Field(default=None, pattern=r"[0-9a-f]{64}")
    expected_byte_length: int | None = Field(default=None, ge=0)


class BatchDocumentUploadCommand(BaseModel):
    """Trusted batch metadata; bytes are supplied separately at the seam."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    environment_id: UUID
    command_identity: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    idempotency_key: str = Field(min_length=1, max_length=255)
    items: tuple[BatchDocumentUploadItem, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_items(self) -> "BatchDocumentUploadCommand":
        if self.workspace_id.int == 0 or self.environment_id.int == 0:
            raise ValueError("batch scope identities must be non-zero")
        identities = [item.input_identity for item in self.items]
        if len(set(identities)) != len(identities):
            raise ValueError("batch input identities must be unique")
        return self


@dataclass(frozen=True, slots=True)
class BatchDocumentInput:
    command: BatchDocumentUploadItem
    chunks: AsyncIterable[bytes]


@dataclass(frozen=True, slots=True)
class BatchItemOutcome:
    """A content-free, independently queryable outcome for one input."""

    item: IngestionRunSource
    state: IngestionItemState
    status: DocumentIngestionStatus
    attempt: int
    terminal: bool
    retryable: bool
    coverage: ProcessingCoverage | None
    processing_receipt_id: UUID | None
    warning_ids: tuple[UUID, ...]
    failure_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class BatchIngestionResult:
    run: IngestionRun
    items: tuple[BatchItemOutcome, ...]
    status: BatchIngestionStatus
    replay: bool


class _BatchItemProcessor(Protocol):
    async def ingest_item(
        self,
        *,
        request: DocumentUploadCommand,
        chunks: AsyncIterable[bytes],
        run: IngestionRun,
        item: IngestionRunSource,
        replay: bool = False,
    ) -> DocumentIngestionResult: ...

    async def record_item_failure(
        self,
        *,
        request: DocumentUploadCommand,
        item: IngestionRunSource,
        retryable: bool = True,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class BatchDocumentIngestionOrchestrator:
    """Create one durable run and execute each file as an independent item."""

    uow_factory: UnitOfWorkFactory
    item_processor: _BatchItemProcessor | DocumentIngestionOrchestrator
    context_factory: Callable[[PersistenceOperation, UUID], TrustedPersistenceContext]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    service_name: str = "batch-document-ingestion"

    async def ingest(
        self,
        *,
        request: BatchDocumentUploadCommand,
        items: Sequence[BatchDocumentInput],
    ) -> BatchIngestionResult:
        self._validate_inputs(request, items)
        digest = _batch_digest(request)
        run, ledger_items, replay = await self._ensure_batch(request, digest)
        supplied = {item.command.input_identity: item for item in items}
        outcomes: list[BatchItemOutcome] = []

        for ordinal, ledger_item in enumerate(ledger_items, start=1):
            snapshot = await self._snapshot(request, ledger_item)
            if snapshot is not None and _is_recoverable_without_work(snapshot.state):
                outcomes.append(snapshot)
                continue
            supplied_item = supplied[ledger_item.input_identity]
            child_request = _child_request(request, supplied_item.command, ordinal)
            try:
                result = await self.item_processor.ingest_item(
                    request=child_request,
                    chunks=supplied_item.chunks,
                    run=run,
                    item=ledger_item,
                    replay=replay,
                )
            except Exception:
                # Exception text and input metadata are deliberately not copied
                # into the batch result. The item processor records a safe
                # failure where the item has reached a recordable boundary.
                record_failure = getattr(self.item_processor, "record_item_failure", None)
                if callable(record_failure):
                    await record_failure(request=child_request, item=ledger_item, retryable=True)
                result = None
            outcomes.append(await self._outcome(request, ledger_item, result))

        return BatchIngestionResult(
            run=run,
            items=tuple(outcomes),
            status=_batch_status(outcomes, await self._run_state(request, run.ingestion_run_id)),
            replay=replay,
        )

    async def cancel(
        self, *, request: BatchDocumentUploadCommand, run_id: UUID
    ) -> bool:
        """Cancel the parent at a durable boundary; item history is retained."""

        context = self._context(PersistenceOperation("ingestion_run"), _batch_id(request, "cancel-trace"))
        async with self.uow_factory(context) as uow:
            view = await uow.ingestion.derive_run_state(run_id)
            if view is None or view.run.workspace_id != request.workspace_id or view.run.environment_id != request.environment_id:
                await uow.rollback()
                return False
            if view.state in {
                IngestionRunState.SUCCEEDED,
                IngestionRunState.PARTIALLY_SUCCEEDED,
                IngestionRunState.FAILED,
                IngestionRunState.CANCELLED,
            }:
                await uow.rollback()
                return False
            await self._advance_run(uow, view.run, IngestionRunState.CANCELLED, self._now())
            await uow.commit()
            return True

    async def _ensure_batch(
        self, request: BatchDocumentUploadCommand, digest: str
    ) -> tuple[IngestionRun, tuple[IngestionRunSource, ...], bool]:
        now = self._now()
        run = IngestionRun(
            ingestion_run_id=_batch_id(request, "run"),
            workspace_id=request.workspace_id,
            environment_id=request.environment_id,
            command_identity=request.command_identity,
            command_digest="0" * 64,
            trigger_kind="upload",
            requested_scope_schema="ingestion-scope:v1",
            requested_scope_digest=digest,
            requested_at=now,
        )
        context = self._context(PersistenceOperation("ingestion_run"), _batch_id(request, "run-trace"))
        async with self.uow_factory(context) as uow:
            created = await uow.ingestion.create_run(
                IngestionRunCommand.create(run=run, idempotency_key=_idempotency_key(request.idempotency_key))
            )
            actual_run = created.value
            ledger_items: list[IngestionRunSource] = []
            for ordinal, item in enumerate(request.items, start=1):
                ledger_items.append(
                    await uow.ingestion.create_item(
                        IngestionRunSource(
                            ingestion_run_source_id=_item_id(actual_run.ingestion_run_id, ordinal, item.input_identity),
                            ingestion_run_id=actual_run.ingestion_run_id,
                            workspace_id=request.workspace_id,
                            environment_id=request.environment_id,
                            input_ordinal=ordinal,
                            input_identity=item.input_identity,
                            input_digest=_item_digest(digest, ordinal, item),
                            command_identity=_child_identifier(request.command_identity, f"item-{ordinal}"),
                            command_digest=_item_digest(digest, ordinal, item),
                            created_at=actual_run.requested_at,
                        )
                    )
                )
            head = await uow.ingestion.resolve_run_head(actual_run.ingestion_run_id)
            if head is not None and head.state is IngestionRunState.REQUESTED:
                await self._advance_run(uow, actual_run, IngestionRunState.RECEIVING, now)
            await uow.commit()
        return actual_run, tuple(ledger_items), created.replay

    async def _snapshot(
        self, request: BatchDocumentUploadCommand, item: IngestionRunSource
    ) -> BatchItemOutcome | None:
        context = self._context(PersistenceOperation("ingestion_run"), _item_id(item.ingestion_run_id, item.input_ordinal, "snapshot"))
        async with self.uow_factory(context) as uow:
            head = await uow.ingestion.resolve_item_head(item.ingestion_run_source_id)
            if head is None:
                await uow.rollback()
                return None
            receipt = await uow.ingestion.resolve_processing_receipt(item.ingestion_run_source_id)
            warnings = await uow.ingestion.resolve_warnings(item.ingestion_run_source_id)
            failures = await uow.ingestion.resolve_failures(item.ingestion_run_source_id)
            await uow.commit()
        return _outcome_from_ledger(item, head.state, head.attempt, receipt, warnings, failures)

    async def _outcome(
        self,
        request: BatchDocumentUploadCommand,
        item: IngestionRunSource,
        result: DocumentIngestionResult | None,
    ) -> BatchItemOutcome:
        snapshot = await self._snapshot(request, item)
        if snapshot is None:
            return BatchItemOutcome(
                item=item,
                state=IngestionItemState.PROCESSING_FAILED,
                status=DocumentIngestionStatus.FAILED,
                attempt=0,
                terminal=True,
                retryable=True,
                coverage=ProcessingCoverage.FAILED,
                processing_receipt_id=None,
                warning_ids=(),
                failure_ids=(),
            )
        return BatchItemOutcome(
            item=snapshot.item,
            state=snapshot.state,
            status=(result.status if result is not None else snapshot.status),
            attempt=snapshot.attempt,
            terminal=snapshot.terminal,
            retryable=snapshot.retryable,
            coverage=snapshot.coverage,
            processing_receipt_id=snapshot.processing_receipt_id,
            warning_ids=snapshot.warning_ids,
            failure_ids=snapshot.failure_ids,
        )

    async def _run_state(self, request: BatchDocumentUploadCommand, run_id: UUID) -> IngestionRunState:
        context = self._context(PersistenceOperation("ingestion_run"), _batch_id(request, "state-trace"))
        async with self.uow_factory(context) as uow:
            view = await uow.ingestion.derive_run_state(run_id)
            await uow.commit()
        return view.state if view is not None else IngestionRunState.FAILED

    async def _advance_run(self, uow: object, run: IngestionRun, state: IngestionRunState, now: datetime) -> None:
        head = await uow.ingestion.resolve_run_head(run.ingestion_run_id)
        if head is None or head.state is state:
            return
        trace_factory = getattr(uow, "_trace_id_snapshot", None)
        trace_id = trace_factory() if callable(trace_factory) else uuid5(
            NAMESPACE_URL, f"spine:ingestion-batch-trace:v1:{run.ingestion_run_id}"
        )
        await uow.ingestion.append_transition(
            IngestionTransition(
                ingestion_transition_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:ingestion-batch-run-transition:v1:{run.ingestion_run_id}:{state.value}:{head.attempt + 1}",
                ),
                workspace_id=run.workspace_id,
                environment_id=run.environment_id,
                target_kind=IngestionTargetKind.RUN,
                ingestion_run_id=run.ingestion_run_id,
                predecessor_transition_id=head.ingestion_transition_id,
                state=state,
                attempt=head.attempt + 1,
                actor_or_service=self.service_name,
                trace_id=trace_id if isinstance(trace_id, UUID) else uuid5(
                    NAMESPACE_URL, f"spine:ingestion-batch-trace:v1:{run.ingestion_run_id}"
                ),
                occurred_at=now,
            )
        )

    def _context(self, operation: PersistenceOperation, trace_id: UUID) -> TrustedPersistenceContext:
        return self.context_factory(operation, trace_id)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("batch ingestion clock must be timezone-aware")
        return value

    @staticmethod
    def _validate_inputs(request: BatchDocumentUploadCommand, items: Sequence[BatchDocumentInput]) -> None:
        if len(items) != len(request.items):
            raise ValueError("batch input count does not match command")
        identities = [item.command.input_identity for item in items]
        if len(set(identities)) != len(identities) or set(identities) != {item.input_identity for item in request.items}:
            raise ValueError("batch input identities do not match command")


def _outcome_from_ledger(
    item: IngestionRunSource,
    state: IngestionItemState,
    attempt: int,
    receipt: ProcessingReceipt | None,
    warnings: tuple[IngestionWarning, ...],
    failures: tuple[IngestionFailure, ...],
) -> BatchItemOutcome:
    if state is IngestionItemState.QUARANTINED:
        status = DocumentIngestionStatus.QUARANTINED
    elif state is IngestionItemState.REJECTED:
        status = DocumentIngestionStatus.REJECTED
    elif state is IngestionItemState.PROCESSING_FAILED:
        status = (
            DocumentIngestionStatus.UNSUPPORTED
            if receipt is not None and receipt.parser_name == "unsupported"
            else DocumentIngestionStatus.FAILED
        )
    elif receipt is not None and receipt.coverage is ProcessingCoverage.PARTIAL:
        status = DocumentIngestionStatus.PARTIAL
    elif state in {IngestionItemState.PROCESSED, IngestionItemState.PROJECTION_REQUESTED}:
        status = DocumentIngestionStatus.SUCCEEDED
    else:
        status = DocumentIngestionStatus.FAILED
    retryable = state is IngestionItemState.PROCESSING_FAILED and any(value.retryable for value in failures)
    terminal = state in {
        IngestionItemState.QUARANTINED,
        IngestionItemState.REJECTED,
        IngestionItemState.PROCESSED,
        IngestionItemState.PROJECTION_REQUESTED,
        IngestionItemState.PROCESSING_FAILED,
    }
    return BatchItemOutcome(
        item=item,
        state=state,
        status=status,
        attempt=attempt,
        terminal=terminal,
        retryable=retryable,
        coverage=receipt.coverage if receipt is not None else None,
        processing_receipt_id=receipt.processing_receipt_id if receipt is not None else None,
        warning_ids=tuple(value.ingestion_warning_id for value in warnings),
        failure_ids=tuple(value.ingestion_failure_id for value in failures),
    )


def _is_recoverable_without_work(state: IngestionItemState) -> bool:
    return state in {
        IngestionItemState.QUARANTINED,
        IngestionItemState.REJECTED,
        IngestionItemState.PROCESSED,
        IngestionItemState.PROJECTION_REQUESTED,
    }


def _batch_status(items: Sequence[BatchItemOutcome], run_state: IngestionRunState) -> BatchIngestionStatus:
    if run_state is IngestionRunState.CANCELLED:
        return BatchIngestionStatus.CANCELLED
    if not items or any(item.state in {IngestionItemState.RECEIVED, IngestionItemState.STORED, IngestionItemState.OBSERVED, IngestionItemState.ADMISSION_PENDING, IngestionItemState.ACCEPTED, IngestionItemState.PROCESSING} for item in items):
        return BatchIngestionStatus.PROCESSING
    successful = [item for item in items if item.status in {DocumentIngestionStatus.SUCCEEDED, DocumentIngestionStatus.PARTIAL}]
    unsuccessful = [item for item in items if item.status not in {DocumentIngestionStatus.SUCCEEDED, DocumentIngestionStatus.PARTIAL}]
    if successful and unsuccessful:
        return BatchIngestionStatus.PARTIAL
    if successful and any(item.status is DocumentIngestionStatus.PARTIAL for item in successful):
        return BatchIngestionStatus.PARTIAL
    return BatchIngestionStatus.SUCCEEDED if successful else BatchIngestionStatus.FAILED


def _child_request(parent: BatchDocumentUploadCommand, item: BatchDocumentUploadItem, ordinal: int) -> DocumentUploadCommand:
    key = f"{parent.idempotency_key}:{ordinal}:{item.input_identity}"
    return DocumentUploadCommand(
        workspace_id=parent.workspace_id,
        environment_id=parent.environment_id,
        command_identity=_child_identifier(parent.command_identity, f"item-{ordinal}"),
        idempotency_key=key if len(key) <= 255 else f"{key[:220]}:{_sha256(key)[:32]}",
        upload_identity=item.input_identity,
        purpose=item.purpose,
        declared_media_type=item.declared_media_type,
        declared_filename=item.declared_filename,
        expected_sha256=item.expected_sha256,
        expected_byte_length=item.expected_byte_length,
    )


def _batch_digest(request: BatchDocumentUploadCommand) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _item_digest(batch_digest: str, ordinal: int, item: BatchDocumentUploadItem) -> str:
    return _sha256(json.dumps({"batch": batch_digest, "ordinal": ordinal, "item": item.model_dump(mode="json")}, sort_keys=True, separators=(",", ":")))


def _batch_id(request: BatchDocumentUploadCommand, suffix: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:document-ingestion-batch:v1:{request.workspace_id}:{request.environment_id}:{request.command_identity}:{request.idempotency_key}:{suffix}")


def _item_id(run_id: UUID, ordinal: int, identity: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"spine:document-ingestion-batch-item:v1:{run_id}:{ordinal}:{identity}")


def _idempotency_key(value: str) -> IdempotencyKey:
    return IdempotencyKey(value)


def _child_identifier(parent: str, suffix: str) -> str:
    value = f"{parent}.{suffix}"
    return value if len(value) <= 64 else f"{value[:31]}-{_sha256(value)[:32]}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "BatchDocumentInput",
    "BatchDocumentIngestionOrchestrator",
    "BatchDocumentUploadItem",
    "BatchDocumentUploadCommand",
    "BatchIngestionResult",
    "BatchIngestionStatus",
    "BatchItemOutcome",
]
