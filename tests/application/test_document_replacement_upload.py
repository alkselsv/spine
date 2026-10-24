from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.document_replacement_upload import (
    DocumentReplacementUploadCommand,
    DocumentReplacementUploadService,
)
from spine.application.ingestion_api import IngestionSubmission
from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.errors import OptimisticConflictError
from spine.domain.sources import IdentityMode, RevisionKind, SourceObject

WORKSPACE_ID = UUID("20000000-0000-4000-8000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-4000-8000-000000000001")
SOURCE_ID = UUID("40000000-0000-4000-8000-000000000001")
CURRENT_REVISION_ID = UUID("50000000-0000-4000-8000-000000000001")
SOURCE_CREATED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


class FakeUnitOfWork:
    def __init__(
        self,
        current_revision_id: UUID = CURRENT_REVISION_ID,
        *,
        existing_run: bool = False,
    ) -> None:
        self.sources = SimpleNamespace(resolve_source=self.resolve_source)
        self.canonical = SimpleNamespace(resolve_current=self.resolve_current)
        self.ingestion = SimpleNamespace(derive_run_state=self.derive_run_state)
        self.current_revision_id = current_revision_id
        self.existing_run = existing_run
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def resolve_source(self, source_object_id: UUID):
        if source_object_id != SOURCE_ID:
            return None
        return SourceObject(
            source_object_id=SOURCE_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
            source_kind="document",
            identity_mode=IdentityMode.UPLOAD,
            upload_identity="existing.upload.identity",
            created_at=SOURCE_CREATED_AT,
        )

    async def resolve_current(self, source_object_id: UUID):
        if source_object_id != SOURCE_ID:
            return None
        return SimpleNamespace(
            current_revision_id=self.current_revision_id,
            current_revision_kind=RevisionKind.CONTENT,
        )

    async def derive_run_state(self, run_id: UUID):
        del run_id
        return object() if self.existing_run else None


class FakeIngestionService:
    def __init__(self) -> None:
        self.submitted: tuple[object, object, AsyncIterable[bytes]] | None = None
        self.fail_next = False

    async def submit(
        self, *, context: object, command: object, chunks: AsyncIterable[bytes]
    ):
        self.submitted = (context, command, chunks)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("temporary ingestion failure")
        return IngestionSubmission(
            run_id=UUID("60000000-0000-4000-8000-000000000001"),
            item_id=UUID("70000000-0000-4000-8000-000000000001"),
            source_object_id=SOURCE_ID,
            source_revision_id=UUID("80000000-0000-4000-8000-000000000001"),
            binding_id=UUID("90000000-0000-4000-8000-000000000001"),
            status=SimpleNamespace(value="succeeded"),
            replay=False,
            processing_receipt_id=None,
            parser_name=None,
            warning_ids=(),
            failure_ids=(),
        )


def context():
    return SimpleNamespace(
        scope=EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID)
    )


def command(**overrides: object) -> DocumentReplacementUploadCommand:
    values: dict[str, object] = {
        "source_object_id": SOURCE_ID,
        "expected_current_revision_id": CURRENT_REVISION_ID,
        "idempotency_key": "replacement-001",
        "declared_filename": "handbook.md",
        "declared_media_type": "text/markdown",
    }
    values.update(overrides)
    return DocumentReplacementUploadCommand(**values)


async def chunks():
    yield b"# revised handbook"


@pytest.mark.asyncio
async def test_replacement_uses_existing_upload_identity_and_revision_cas() -> None:
    uow = FakeUnitOfWork()
    ingestion = FakeIngestionService()
    service = DocumentReplacementUploadService(
        uow_factory=lambda _context: uow,
        ingestion_service=ingestion,
    )

    result = await service.replace(
        context=context(), command=command(), chunks=chunks()
    )

    assert result.source_object_id == SOURCE_ID
    assert uow.committed is True
    assert ingestion.submitted is not None
    _, ingestion_command, _ = ingestion.submitted
    assert ingestion_command.upload_identity == "existing.upload.identity"
    assert ingestion_command.workspace_id == WORKSPACE_ID
    assert ingestion_command.environment_id == ENVIRONMENT_ID
    assert ingestion_command.expected_current_revision_id == CURRENT_REVISION_ID
    assert ingestion_command.source_created_at == SOURCE_CREATED_AT
    assert ingestion_command.declared_filename == "handbook.md"


@pytest.mark.asyncio
async def test_replacement_rejects_stale_expected_revision_before_upload() -> None:
    uow = FakeUnitOfWork(UUID("50000000-0000-4000-8000-000000000002"))
    ingestion = FakeIngestionService()
    service = DocumentReplacementUploadService(
        uow_factory=lambda _context: uow,
        ingestion_service=ingestion,
    )

    with pytest.raises(OptimisticConflictError):
        await service.replace(context=context(), command=command(), chunks=chunks())

    assert ingestion.submitted is None
    assert uow.committed is False


@pytest.mark.asyncio
async def test_replacement_replay_returns_through_existing_ingestion_identity_after_revision_advanced() -> (
    None
):
    uow = FakeUnitOfWork(
        UUID("50000000-0000-4000-8000-000000000002"),
        existing_run=True,
    )
    ingestion = FakeIngestionService()
    service = DocumentReplacementUploadService(
        uow_factory=lambda _context: uow,
        ingestion_service=ingestion,
    )

    result = await service.replace(
        context=context(), command=command(), chunks=chunks()
    )

    assert result.source_object_id == SOURCE_ID
    assert ingestion.submitted is not None
    assert ingestion.submitted[1].expected_current_revision_id == CURRENT_REVISION_ID


@pytest.mark.asyncio
async def test_replacement_retry_after_partial_failure_keeps_the_same_idempotency_command() -> (
    None
):
    uow = FakeUnitOfWork(existing_run=True)
    ingestion = FakeIngestionService()
    ingestion.fail_next = True
    service = DocumentReplacementUploadService(
        uow_factory=lambda _context: uow,
        ingestion_service=ingestion,
    )

    with pytest.raises(RuntimeError, match="temporary ingestion failure"):
        await service.replace(context=context(), command=command(), chunks=chunks())
    first_command = ingestion.submitted[1]
    result = await service.replace(
        context=context(), command=command(), chunks=chunks()
    )

    assert result.source_object_id == SOURCE_ID
    assert ingestion.submitted[1].idempotency_key == first_command.idempotency_key
    assert ingestion.submitted[1].upload_identity == first_command.upload_identity


@pytest.mark.asyncio
async def test_replacement_rejects_source_from_another_workspace() -> None:
    uow = FakeUnitOfWork()
    ingestion = FakeIngestionService()
    service = DocumentReplacementUploadService(
        uow_factory=lambda _context: uow,
        ingestion_service=ingestion,
    )
    other_scope = SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=UUID("20000000-0000-4000-8000-000000000002"),
            environment_id=ENVIRONMENT_ID,
        )
    )

    with pytest.raises(OptimisticConflictError):
        await service.replace(context=other_scope, command=command(), chunks=chunks())

    assert ingestion.submitted is None
