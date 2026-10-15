from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from spine.application.ingestion_history import (
    OrchestratedIngestionHistoryService,
)
from spine.application.persistence.context import EnvironmentScope
from spine.domain.ingestion import IngestionItemState, IngestionRunState
from spine.domain.sources import RevisionKind


WORKSPACE_ID = UUID("93000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("93000000-0000-0000-0000-000000000002")
RUN_ID = UUID("93000000-0000-0000-0000-000000000003")
ITEM_ID = UUID("93000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)


class FakeIngestionRepository:
    def __init__(self) -> None:
        self.item = SimpleNamespace(
            ingestion_run_id=RUN_ID,
            ingestion_run_source_id=ITEM_ID,
            input_ordinal=1,
            input_identity="handbook",
            created_at=NOW,
        )
        self.view = SimpleNamespace(
            run=SimpleNamespace(ingestion_run_id=RUN_ID, requested_at=NOW),
            state=IngestionRunState.PROCESSING,
            items=(self.item,),
        )

    async def list_runs(self, *, limit: int, after: object | None):
        assert limit == 2
        assert after is None
        return (self.view,)

    async def list_items(self, *, limit: int, after: object | None):
        assert limit == 2
        assert isinstance(after, tuple) or after is None
        return (self.item,)

    async def derive_run_state(self, run_id: UUID):
        assert run_id == RUN_ID
        return self.view

    async def resolve_item_head(self, item_id: UUID):
        assert item_id == ITEM_ID
        return SimpleNamespace(
            state=IngestionItemState.PROCESSING,
            attempt=3,
            occurred_at=NOW,
        )

    async def resolve_binding(self, item_id: UUID):
        assert item_id == ITEM_ID
        return None

    async def resolve_processing_receipt(self, item_id: UUID):
        assert item_id == ITEM_ID
        return None

    async def resolve_warnings(self, item_id: UUID):
        assert item_id == ITEM_ID
        return ()

    async def resolve_failures(self, item_id: UUID):
        assert item_id == ITEM_ID
        return ()


class FakeUnitOfWork:
    def __init__(self, repository: FakeIngestionRepository) -> None:
        self.ingestion = repository
        self.sources = SimpleNamespace(
            resolve_revision=lambda revision_id: _content_revision(revision_id),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        return None


async def _content_revision(revision_id: UUID) -> object:
    return SimpleNamespace(revision_id=revision_id, kind=RevisionKind.CONTENT)


@pytest.mark.asyncio
async def test_history_service_projects_scoped_ledger_rows_into_stable_read_models() -> None:
    repository = FakeIngestionRepository()
    service = OrchestratedIngestionHistoryService(
        uow_factory=lambda context: FakeUnitOfWork(repository),  # type: ignore[arg-type]
    )
    context = SimpleNamespace(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
    )

    runs = await service.list_runs(context=context, limit=1, after=None)  # type: ignore[arg-type]
    processing = await service.list_processing_history(
        context=context,
        limit=1,
        after=None,
    )  # type: ignore[arg-type]

    run = runs.items[0]
    item = run.items[0]
    assert run.run_id == RUN_ID
    assert item.item_id == ITEM_ID
    assert item.attempt == 3
    assert processing.items[0].item_id == ITEM_ID
