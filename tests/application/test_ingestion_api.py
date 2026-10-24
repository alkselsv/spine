from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.ingestion_api import (
    InvalidIngestionRequest,
    OrchestratedDocumentIngestionService,
)
from spine.application.persistence.context import EnvironmentScope
from spine.domain.ingestion import IngestionItemState, IngestionRunState, ProcessingCoverage


WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000073")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000073")
RUN_ID = UUID("90000000-0000-0000-0000-000000000001")
ITEM_ID = UUID("90000000-0000-0000-0000-000000000002")
SOURCE_OBJECT_ID = UUID("90000000-0000-0000-0000-000000000003")
REVISION_ID = UUID("90000000-0000-0000-0000-000000000004")
BINDING_ID = UUID("90000000-0000-0000-0000-000000000005")
RECEIPT_ID = UUID("90000000-0000-0000-0000-000000000006")
ORIGINAL_RECEIPT_ID = UUID("90000000-0000-0000-0000-000000000008")
PROVENANCE_ID = UUID("90000000-0000-0000-0000-000000000009")


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.ingestion = SimpleNamespace(
            derive_run_state=self.derive_run_state,
            resolve_item=self.resolve_item,
            resolve_item_head=self.resolve_item_head,
            resolve_binding=self.resolve_binding,
            resolve_processing_receipt=self.resolve_processing_receipt,
            resolve_failures=self.resolve_failures,
        )
        self.canonical = SimpleNamespace(list_acceptances=self.list_acceptances)
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def derive_run_state(self, run_id: UUID):
        if run_id != RUN_ID:
            return None
        return SimpleNamespace(state=IngestionRunState.PROCESSING)

    async def resolve_item(self, item_id: UUID):
        if item_id != ITEM_ID:
            return None
        return SimpleNamespace(
            ingestion_run_id=RUN_ID,
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )

    async def resolve_item_head(self, item_id: UUID):
        if item_id != ITEM_ID:
            return None
        return SimpleNamespace(state=IngestionItemState.PROCESSING)

    async def resolve_binding(self, item_id: UUID):
        if item_id != ITEM_ID:
            return None
        return SimpleNamespace(
            binding_id=BINDING_ID,
            source_object_id=SOURCE_OBJECT_ID,
            source_revision_id=REVISION_ID,
            original_receipt_id=ORIGINAL_RECEIPT_ID,
            original_receipt_digest="b" * 64,
        )

    async def resolve_processing_receipt(self, item_id: UUID):
        if item_id != ITEM_ID:
            return None
        return SimpleNamespace(
            processing_receipt_id=RECEIPT_ID,
            parser_name="text",
            coverage=ProcessingCoverage.COMPLETE,
            output_reference=None,
            output_digest=None,
            warning_ids=(),
            failure_ids=(),
        )

    async def list_acceptances(self, source_object_id: UUID):
        assert source_object_id == SOURCE_OBJECT_ID
        return (
            SimpleNamespace(
                source_revision_id=REVISION_ID,
                provenance_id=PROVENANCE_ID,
            ),
        )

    async def resolve_failures(self, item_id: UUID):
        assert item_id == ITEM_ID
        return (SimpleNamespace(ingestion_failure_id=UUID("90000000-0000-0000-0000-000000000007"), code="parser_retryable"),)


def context(workspace_id: UUID = WORKSPACE_ID, environment_id: UUID = ENVIRONMENT_ID):
    return SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
    )


async def test_status_reads_ledger_binding_and_receipt_in_one_authorized_scope() -> None:
    uow = FakeUnitOfWork()
    service = OrchestratedDocumentIngestionService(
        orchestrator=SimpleNamespace(),  # type: ignore[arg-type]
        uow_factory=lambda trusted_context: uow,
    )

    status = await service.status(
        context=context(),
        run_id=RUN_ID,
        item_id=ITEM_ID,
    )

    assert status is not None
    assert status.run_state is IngestionRunState.PROCESSING
    assert status.item_state is IngestionItemState.PROCESSING
    assert status.binding_id == BINDING_ID
    assert status.canonical_acceptance_committed is True
    assert status.original_receipt_id == ORIGINAL_RECEIPT_ID
    assert status.original_receipt_digest == "b" * 64
    assert status.provenance_id == PROVENANCE_ID
    assert status.processing_receipt_id == RECEIPT_ID
    assert status.failure_reason_codes == ("parser_retryable",)
    assert uow.committed is True
    assert uow.rolled_back is False


async def test_status_does_not_leak_a_foreign_run_or_workspace() -> None:
    uow = FakeUnitOfWork()
    service = OrchestratedDocumentIngestionService(
        orchestrator=SimpleNamespace(),  # type: ignore[arg-type]
        uow_factory=lambda trusted_context: uow,
    )

    assert await service.status(
        context=context(),
        run_id=UUID("90000000-0000-0000-0000-000000000099"),
        item_id=ITEM_ID,
    ) is None
    assert await service.status(
        context=context(environment_id=UUID("30000000-0000-0000-0000-000000000099")),
        run_id=RUN_ID,
        item_id=ITEM_ID,
    ) is None


async def test_status_rejects_a_workspace_only_context() -> None:
    uow = FakeUnitOfWork()
    service = OrchestratedDocumentIngestionService(
        orchestrator=SimpleNamespace(),  # type: ignore[arg-type]
        uow_factory=lambda trusted_context: uow,
    )

    workspace_context = SimpleNamespace(scope=SimpleNamespace(workspace_id=WORKSPACE_ID))
    with pytest.raises(InvalidIngestionRequest):
        await service.status(
            context=workspace_context,
            run_id=RUN_ID,
            item_id=ITEM_ID,
        )
