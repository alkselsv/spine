from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from spine.application.document_ingestion import (
    DocumentIngestionOrchestrator,
    DocumentIngestionStatus,
    DocumentUploadCommand,
)
from spine.application.ingestion import BoundedContentValidator
from spine.application.persistence.context import PersistenceOperation
from spine.application.persistence.idempotency import OpaqueResultReference
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.application.persistence.parsed_artifacts import ParsedArtifactPersistenceResult
from spine.application.persistence.ingestion import IngestionMutationResult
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunState,
    IngestionTargetKind,
    IngestionTransition,
    ProcessingReceipt,
)
from spine.domain.sources import AdmissionDisposition
from spine.infrastructure.object_storage import (
    IntegrityResult,
    IntegrityStatus,
    ObjectReference,
    ObservedContentIdentity,
    OriginalUploadCommandResult,
    PreWriteRequestDigest,
    WriteReceipt,
)
from spine.infrastructure.persistence.parsed_artifacts import InMemoryParsedArtifactRepository
from spine.ingestion.parsers import parse_document


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
WORKSPACE = UUID("10000000-0000-4000-8000-000000000001")
ENVIRONMENT = UUID("20000000-0000-4000-8000-000000000001")


def _id(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"document-ingestion-test:{value}")


async def _stream(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


class _Uploader:
    def __init__(self, content: bytes) -> None:
        digest = hashlib.sha256(content).hexdigest()
        reference = ObjectReference(
            schema_version=1, object_id=_id("object"), storage_generation=_id("generation"),
            digest_algorithm="sha256", digest_hex=digest, byte_length=len(content),
        )
        receipt = WriteReceipt(
            schema_version=1, upload_id=_id("upload"), object_reference=reference,
            observed_content=ObservedContentIdentity(observed_sha256=digest, observed_byte_length=len(content)),
            verification=IntegrityResult(
                status=IntegrityStatus.VERIFIED, algorithm="sha256",
                observed_digest=digest, observed_length=len(content),
            ), storage_status="finalized_verified", finalized_at=NOW,
        )
        result = OriginalUploadCommandResult(
            request_digest=PreWriteRequestDigest(
                algorithm="sha256", algorithm_version="spine.command-digest.v1", digest_hex="a" * 64,
            ), upload_id=receipt.upload_id, object_id=reference.object_id,
            issue8_result_reference={"receipt_id": _id("issue8-receipt")},
        )
        self.outcome = SimpleNamespace(result=result, write_receipt=receipt, replay=False)
        self.calls = 0

    async def upload(self, context: object, command: object, chunks: AsyncIterable[bytes]):
        del context, command, chunks
        self.calls += 1
        return self.outcome


class _Reader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls = 0

    def read_finalized(self, **kwargs: object) -> AsyncIterable[bytes]:
        assert kwargs["workspace_id"] == WORKSPACE
        assert kwargs["environment_id"] == ENVIRONMENT
        self.calls += 1
        return _stream(self.content[:2], self.content[2:])


class _Ingestion:
    def __init__(self) -> None:
        self.run = None
        self.item = None
        self.transitions: list[IngestionTransition] = []
        self.binding = None
        self.receipt: ProcessingReceipt | None = None

    def _head(self, *, kind: IngestionTargetKind, run_id: UUID, item_id: UUID | None = None):
        candidates = [
            value for value in self.transitions
            if value.target_kind is kind and value.ingestion_run_id == run_id
            and value.ingestion_run_source_id == item_id
        ]
        return candidates[-1] if candidates else None

    async def create_run(self, command):
        replay = self.run is not None
        if self.run is None:
            self.run = command.run
            self.transitions.append(IngestionTransition(
                ingestion_transition_id=_id("run-root"), workspace_id=command.run.workspace_id,
                environment_id=command.run.environment_id, target_kind=IngestionTargetKind.RUN,
                ingestion_run_id=command.run.ingestion_run_id, state=IngestionRunState.REQUESTED,
                attempt=1, actor_or_service="fake", trace_id=_id("trace"), occurred_at=NOW,
            ))
        return SimpleNamespace(value=self.run, replay=replay)

    async def create_item(self, item):
        if self.item is None:
            self.item = item
            self.transitions.append(IngestionTransition(
                ingestion_transition_id=_id("item-root"), workspace_id=item.workspace_id,
                environment_id=item.environment_id, target_kind=IngestionTargetKind.ITEM,
                ingestion_run_id=item.ingestion_run_id, ingestion_run_source_id=item.ingestion_run_source_id,
                state=IngestionItemState.RECEIVED, attempt=1, actor_or_service="fake",
                trace_id=_id("trace"), occurred_at=NOW,
            ))
        return self.item

    async def resolve_run_head(self, run_id):
        return self._head(kind=IngestionTargetKind.RUN, run_id=run_id)

    async def resolve_item_head(self, item_id):
        return self._head(kind=IngestionTargetKind.ITEM, run_id=self.item.ingestion_run_id, item_id=item_id)

    async def bind_source(self, command):
        if self.binding is None:
            self.binding = command.binding
            head = await self.resolve_item_head(command.item.ingestion_run_source_id)
            self.transitions.append(IngestionTransition(
                ingestion_transition_id=_id("observed"), workspace_id=command.item.workspace_id,
                environment_id=command.item.environment_id, target_kind=IngestionTargetKind.ITEM,
                ingestion_run_id=command.item.ingestion_run_id,
                ingestion_run_source_id=command.item.ingestion_run_source_id,
                predecessor_transition_id=head.ingestion_transition_id, state=IngestionItemState.OBSERVED,
                attempt=head.attempt + 1, actor_or_service="fake", trace_id=_id("trace"), occurred_at=NOW,
            ))
        return SimpleNamespace(value=self.binding, replay=self.binding == command.binding)

    async def append_transition(self, transition):
        self.transitions.append(transition)
        return transition

    async def resolve_processing_receipt(self, item_id):
        return self.receipt

    async def append_warning(self, warning):
        return warning

    async def append_failure(self, failure):
        return failure

    async def record_processing_receipt(self, command):
        replay = self.receipt is not None
        self.receipt = self.receipt or command.receipt
        return SimpleNamespace(value=self.receipt, replay=replay)

    async def resolve_item(self, item_id):
        return self.item


class _Admissions:
    def __init__(self) -> None:
        self.decision = None

    async def resolve_effective(self, revision_id):
        return self.decision

    async def append_decision(self, decision):
        self.decision = decision
        return decision


class _Canonical:
    def __init__(self) -> None:
        self.calls = 0

    async def accept(self, command):
        self.calls += 1
        return command


class _Uow:
    def __init__(self, ingestion, admissions, canonical, outbox):
        self.ingestion = ingestion
        self.admissions = admissions
        self.canonical = canonical
        self.outbox = outbox
        self.trace_id = _id("trace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    def _trace_id_snapshot(self):
        return self.trace_id


class _Outbox:
    def __init__(self) -> None:
        self.events = []

    async def append(self, intent):
        self.events.append(intent)
        return intent.event_id


def _orchestrator(content: bytes, *, media_type: str, parser_calls: list[int]):
    ingestion = _Ingestion()
    admissions = _Admissions()
    canonical = _Canonical()
    outbox = _Outbox()
    uow = _Uow(ingestion, admissions, canonical, outbox)
    uploader = _Uploader(content)
    reader = _Reader(content)

    def parse(revision, value: bytes, *, media_type: str):
        parser_calls.append(1)
        return parse_document(revision, value, media_type=media_type)

    def context_factory(operation: PersistenceOperation, trace_id: UUID):
        return SimpleNamespace(operation=operation, trace_id=trace_id)

    def canonical_command_factory(**kwargs):
        return SimpleNamespace()

    return DocumentIngestionOrchestrator(
        uow_factory=lambda context: uow,
        uploader=uploader,
        validation_reader=reader,
        validator=BoundedContentValidator(max_bytes=64, max_chunk_bytes=64),
        parsed_artifacts=InMemoryParsedArtifactRepository(),
        context_factory=context_factory,
        canonical_command_factory=canonical_command_factory,
        outbox_events=OutboxEventRegistry(),
        parser=parse,
        clock=lambda: NOW,
        max_validation_bytes=64,
        max_parse_bytes=64,
    ), uploader, reader, canonical, outbox


def _request(media_type: str, *, key: str = "document-1") -> DocumentUploadCommand:
    return DocumentUploadCommand(
        workspace_id=WORKSPACE, environment_id=ENVIRONMENT,
        command_identity="document-upload", idempotency_key=key,
        upload_identity="uploaded-document", purpose="document.upload.v1",
        declared_media_type=media_type, declared_filename="document.txt",
    )


@pytest.mark.asyncio
async def test_document_ingestion_completes_text_path_and_replays_stably() -> None:
    parser_calls: list[int] = []
    orchestrator, uploader, reader, canonical, outbox = _orchestrator(
        b"hello\nworld\n", media_type="text/plain", parser_calls=parser_calls
    )
    first = await orchestrator.ingest(request=_request("text/plain"), chunks=_stream(b"hello\nworld\n"))
    second = await orchestrator.ingest(request=_request("text/plain"), chunks=_stream(b"hello\nworld\n"))

    assert first.status is DocumentIngestionStatus.SUCCEEDED
    assert first.artifact is not None
    assert first.processing_receipt is not None
    assert second.replay is True
    assert second.processing_receipt == first.processing_receipt
    assert len(parser_calls) == 1
    assert uploader.calls == 2
    assert reader.calls == 3
    assert canonical.calls == 1
    assert len(outbox.events) == 1


@pytest.mark.asyncio
async def test_document_ingestion_marks_pdf_unsupported_without_parser_access() -> None:
    parser_calls: list[int] = []
    orchestrator, _, _, _, outbox = _orchestrator(
        b"%PDF-1.4\n%%EOF", media_type="application/pdf", parser_calls=parser_calls
    )
    result = await orchestrator.ingest(
        request=_request("application/pdf"), chunks=_stream(b"%PDF-1.4\n%%EOF")
    )

    assert result.status is DocumentIngestionStatus.UNSUPPORTED
    assert result.processing_receipt is not None
    assert result.processing_receipt.coverage.value == "failed"
    assert parser_calls == []
    assert outbox.events == []
