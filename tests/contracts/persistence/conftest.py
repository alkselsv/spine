from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio

from spine.application.persistence.context import (
    TrustedPersistenceContext,
    WorkspaceScope,
    issue_trusted_context_authority,
)
from spine.application.persistence.unit_of_work import UnitOfWorkFactory
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


@dataclass(frozen=True)
class PersistenceAdapter:
    uow_factory: UnitOfWorkFactory
    workspace_context: Callable[[UUID], TrustedPersistenceContext]


@pytest.fixture
def persistence_adapter_factory() -> Callable[[], PersistenceAdapter]:
    def build() -> PersistenceAdapter:
        persistence = InMemoryPersistence()
        issuer = issue_trusted_context_authority()

        def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
            return issuer.interactive(
                scope=WorkspaceScope(workspace_id=workspace_id),
                acting_subject_id=UUID("10000000-0000-0000-0000-000000000001"),
                purpose="contract_test",
                operation="workspace_repository",
                trace_id=UUID("10000000-0000-0000-0000-000000000002"),
            )

        return PersistenceAdapter(persistence.uow_factory, workspace_context)

    return build


@pytest_asyncio.fixture
async def persistence_adapter(
    persistence_adapter_factory: Callable[[], PersistenceAdapter],
) -> AsyncIterator[PersistenceAdapter]:
    yield persistence_adapter_factory()
