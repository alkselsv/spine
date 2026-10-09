from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
import pytest_asyncio

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid


AdapterFactory = Callable[[], Awaitable[PersistenceAdapter]]


async def create_in_memory_adapter() -> PersistenceAdapter:
    transaction_lock = asyncio.Lock()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(900),
        secret=b"issue-40-contract-boundary-secret-01",
    )
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        bootstrap_authority=bootstrap_authority,
        transaction_lock=transaction_lock,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(
            id=synthetic_uuid(901),
            slug="contract-suite-anchor",
            display_name="Contract Suite Anchor",
        ),
    )

    def workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.interactive(
            scope=WorkspaceScope(workspace_id=workspace_id),
            acting_subject_id=synthetic_uuid(902),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(903),
        )

    def environment_context(
        workspace_id: UUID,
        environment_id: UUID,
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id,
                environment_id=environment_id,
            ),
            service_principal_id=synthetic_uuid(904),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("environment_repository"),
            trace_id=synthetic_uuid(905),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        await transaction_lock.acquire()
        try:
            yield
        finally:
            transaction_lock.release()

    return PersistenceAdapter(
        uow_factory=persistence.uow_factory,
        workspace_context=workspace_context,
        environment_context=environment_context,
        hold_transactions=hold_transactions,
    )


# Future adapters extend this parametrization; contract test modules remain unchanged.
PERSISTENCE_ADAPTER_FACTORIES: tuple[AdapterFactory, ...] = (create_in_memory_adapter,)


@pytest.fixture(params=PERSISTENCE_ADAPTER_FACTORIES, ids=("in-memory",))
def persistence_adapter_factory(request: pytest.FixtureRequest) -> AdapterFactory:
    return request.param


@pytest_asyncio.fixture
async def persistence_adapter(
    persistence_adapter_factory: AdapterFactory,
) -> PersistenceAdapter:
    return await persistence_adapter_factory()
