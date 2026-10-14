from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.batch_ingestion import (
    BatchDocumentInput,
    BatchDocumentIngestionOrchestrator,
    BatchDocumentUploadCommand,
    BatchDocumentUploadItem,
    BatchIngestionStatus,
)
from spine.application.batch_ingestion_api import (
    OrchestratedBatchDocumentIngestionService,
)
from spine.application.document_ingestion import DocumentIngestionStatus
from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.errors import IdempotencyConflictError
from spine.domain.ingestion import (
    FailureStage,
    IngestionItemState,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    ProcessingCoverage,
    ProcessingReceipt,
)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"batch-ingestion-test:{value}")


async def _stream(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


class _Ledger:
    def __init__(self) -> None:
        self.runs = {}
        self.items = {}
        self.heads = {}
        self.failures = {}
        self.receipts = {}

    async def create_run(self, command):
        existing = self.runs.get(command.run.ingestion_run_id)
        if existing is not None:
            if existing.requested_scope_digest != command.run.requested_scope_digest:
                raise IdempotencyConflictError("idempotency key conflicts with another command")
            return SimpleNamespace(value=existing, replay=True)
        self.runs[command.run.ingestion_run_id] = command.run
        self.heads[(command.run.ingestion_run_id, None)] = self._transition(
            command.run.ingestion_run_id, None, IngestionRunState.REQUESTED, 1
        )
        return SimpleNamespace(value=command.run, replay=False)

    async def create_item(self, item):
        existing = self.items.get(item.ingestion_run_source_id)
        if existing is not None:
            assert existing == item
            return existing
        self.items[item.ingestion_run_source_id] = item
        self.heads[(item.ingestion_run_id, item.ingestion_run_source_id)] = self._transition(
            item.ingestion_run_id, item.ingestion_run_source_id, IngestionItemState.RECEIVED, 1
        )
        return item

    async def resolve_run_head(self, run_id):
        return self.heads.get((run_id, None))

    async def resolve_item_head(self, item_id):
        item = self.items[item_id]
        return self.heads.get((item.ingestion_run_id, item_id))

    async def append_transition(self, transition):
        key = (transition.ingestion_run_id, transition.ingestion_run_source_id)
        self.heads[key] = transition
        return transition

    async def derive_run_state(self, run_id):
        run = self.runs[run_id]
        states = tuple(self.heads[(run_id, item.ingestion_run_source_id)].state for item in self.items.values() if item.ingestion_run_id == run_id)
        state = self.heads[(run_id, None)].state
        if state is IngestionRunState.PROCESSING and states and all(value is IngestionItemState.PROCESSED for value in states):
            state = IngestionRunState.SUCCEEDED
        elif state is IngestionRunState.PROCESSING and any(value is IngestionItemState.PROCESSING_FAILED for value in states) and any(value is IngestionItemState.PROCESSED for value in states):
            state = IngestionRunState.PARTIALLY_SUCCEEDED
        items = tuple(
            sorted(
                (
                    item
                    for item in self.items.values()
                    if item.ingestion_run_id == run_id
                ),
                key=lambda item: item.input_ordinal,
            )
        )
        return SimpleNamespace(run=run, state=state, items=items, item_states=states)

    async def resolve_processing_receipt(self, item_id):
        return self.receipts.get(item_id)

    async def resolve_binding(self, item_id):
        del item_id
        return None

    async def resolve_warnings(self, item_id):
        return ()

    async def resolve_failures(self, item_id):
        return tuple(self.failures.get(item_id, ()))

    def _transition(self, run_id, item_id, state, attempt):
        previous = self.heads.get((run_id, item_id))
        return IngestionTransition(
            ingestion_transition_id=_id(f"transition:{run_id}:{item_id}:{attempt}"),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            target_kind=IngestionTargetKind.RUN if item_id is None else IngestionTargetKind.ITEM,
            ingestion_run_id=run_id,
            ingestion_run_source_id=item_id,
            predecessor_transition_id=previous.ingestion_transition_id if previous else None,
            state=state,
            attempt=attempt,
            actor_or_service="fake",
            trace_id=_id("trace"),
            occurred_at=NOW,
        )


class _Uow:
    def __init__(self, ledger: _Ledger) -> None:
        self.ingestion = ledger

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Processor:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.calls: list[str] = []
        self.failures_left = {"file-b": 1}

    async def ingest_item(self, *, request, chunks: AsyncIterable[bytes], run, item, replay=False):
        del chunks, replay
        self.calls.append(item.input_identity)
        head = await self.ledger.resolve_item_head(item.ingestion_run_source_id)
        processing = self.ledger._transition(run.ingestion_run_id, item.ingestion_run_source_id, IngestionItemState.PROCESSING, head.attempt + 1)
        await self.ledger.append_transition(processing)
        if self.failures_left.get(item.input_identity, 0):
            self.failures_left[item.input_identity] -= 1
            failure = SimpleNamespace(
                ingestion_failure_id=_id(f"failure:{item.input_identity}"),
                attempt=processing.attempt,
                stage=FailureStage.PARSING,
                code="parser_failed",
                retryable=True,
            )
            self.ledger.failures[item.ingestion_run_source_id] = [failure]
            await self.ledger.append_transition(self.ledger._transition(run.ingestion_run_id, item.ingestion_run_source_id, IngestionItemState.PROCESSING_FAILED, processing.attempt + 1))
            return SimpleNamespace(status=DocumentIngestionStatus.FAILED)
        await self.ledger.append_transition(self.ledger._transition(run.ingestion_run_id, item.ingestion_run_source_id, IngestionItemState.PROCESSED, processing.attempt + 1))
        return SimpleNamespace(status=DocumentIngestionStatus.SUCCEEDED)

    async def record_item_failure(self, *, request, item, retryable=True):
        del request, item, retryable


def _orchestrator():
    ledger = _Ledger()
    processor = _Processor(ledger)
    uow = _Uow(ledger)
    return BatchDocumentIngestionOrchestrator(
        uow_factory=lambda context: uow,
        item_processor=processor,
        context_factory=lambda operation, trace_id: SimpleNamespace(operation=operation, trace_id=trace_id),
        clock=lambda: NOW,
    ), processor, ledger


def _request(*, suffix: str = "") -> BatchDocumentUploadCommand:
    return BatchDocumentUploadCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        command_identity="document-batch-upload",
        idempotency_key=f"batch-1{suffix}",
        items=(
            BatchDocumentUploadItem(input_identity="file-a", purpose="document.upload.v1", declared_media_type="text/plain"),
            BatchDocumentUploadItem(input_identity="file-b", purpose="document.upload.v1", declared_media_type="text/plain"),
        ),
    )


def _inputs():
    return (
        BatchDocumentInput(BatchDocumentUploadItem(input_identity="file-a", purpose="document.upload.v1", declared_media_type="text/plain"), _stream(b"a")),
        BatchDocumentInput(BatchDocumentUploadItem(input_identity="file-b", purpose="document.upload.v1", declared_media_type="text/plain"), _stream(b"b")),
    )


@pytest.mark.asyncio
async def test_batch_keeps_one_run_and_exposes_independent_partial_outcomes() -> None:
    orchestrator, processor, ledger = _orchestrator()

    result = await orchestrator.ingest(request=_request(), items=_inputs())

    assert result.status is BatchIngestionStatus.PARTIAL
    assert len(ledger.runs) == 1
    assert len(ledger.items) == 2
    assert [item.status for item in result.items] == [DocumentIngestionStatus.SUCCEEDED, DocumentIngestionStatus.FAILED]
    assert processor.calls == ["file-a", "file-b"]


@pytest.mark.asyncio
async def test_batch_retry_skips_successful_item_and_replays_same_identities() -> None:
    orchestrator, processor, ledger = _orchestrator()

    first = await orchestrator.ingest(request=_request(), items=_inputs())
    second = await orchestrator.ingest(request=_request(), items=_inputs())

    assert first.run.ingestion_run_id == second.run.ingestion_run_id
    assert [item.item.ingestion_run_source_id for item in first.items] == [item.item.ingestion_run_source_id for item in second.items]
    assert processor.calls == ["file-a", "file-b", "file-b"]
    assert second.items[1].status is DocumentIngestionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_batch_changed_digest_conflicts_before_creating_another_run() -> None:
    orchestrator, _, ledger = _orchestrator()

    await orchestrator.ingest(request=_request(), items=_inputs())
    changed = _request()
    changed = changed.model_copy(update={"items": (changed.items[0], changed.items[1].model_copy(update={"declared_filename": "changed.txt"}))})

    with pytest.raises(IdempotencyConflictError):
        await orchestrator.ingest(request=changed, items=_inputs())

    assert len(ledger.runs) == 1


@pytest.mark.asyncio
async def test_batch_api_service_reads_ordered_independent_item_progress() -> None:
    orchestrator, _, ledger = _orchestrator()
    result = await orchestrator.ingest(request=_request(), items=_inputs())
    service = OrchestratedBatchDocumentIngestionService(
        orchestrator=orchestrator,
        uow_factory=lambda context: _Uow(ledger),
    )
    context = SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
        )
    )

    snapshot = await service.status(
        context=context,  # type: ignore[arg-type]
        run_id=result.run.ingestion_run_id,
    )

    assert snapshot is not None
    assert snapshot.run_state is IngestionRunState.PARTIALLY_SUCCEEDED
    assert [item.input_identity for item in snapshot.items] == ["file-a", "file-b"]
    assert [item.item_state for item in snapshot.items] == [
        IngestionItemState.PROCESSED,
        IngestionItemState.PROCESSING_FAILED,
    ]
    assert snapshot.items[1].retryable is True
    assert snapshot.items[1].failure_ids == (_id("failure:file-b"),)
