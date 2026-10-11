from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from spine.application.diagnostics.audit import AuditEventRegistry
from spine.application.persistence.delivery import OutboxDeliveryRouteRegistry
from spine.domain.workspaces import Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence

from .adapter import (
    AuditPersistenceAdapter,
    DeliveryPersistenceAdapter,
    PersistenceAdapter,
)
from .ids import synthetic_uuid
from .outbox_events import create_outbox_event_registry


AdapterFactory = Callable[[], Awaitable[PersistenceAdapter]]


async def create_in_memory_adapter() -> DeliveryPersistenceAdapter:
    transaction_lock = asyncio.Lock()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=synthetic_uuid(900),
        secret=b"issue-40-contract-boundary-secret-01",
    )
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    outbox_events = create_outbox_event_registry()
    delivery_routes = OutboxDeliveryRouteRegistry(version="contract.v1")
    delivery_routes.register(
        event_type="workspace.created",
        schema_version=1,
        consumer_name="workspace_projection",
    )
    delivery_routes.seal()
    audit_events = AuditEventRegistry.with_default_families()
    persistence = InMemoryPersistence(
        context_verifier=boundary,
        outbox_events=outbox_events,
        audit_events=audit_events,
        bootstrap_authority=bootstrap_authority,
        transaction_lock=transaction_lock,
        audit_clock=lambda: datetime(2026, 2, 3, 4, 5, tzinfo=timezone.utc),
        audit_event_id_factory=lambda: synthetic_uuid(908),
        delivery_routes=delivery_routes,
        delivery_lease_token_factory=(
            synthetic_uuid(value) for value in range(6600, 6700)
        ).__next__,
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

    def source_context(workspace_id: UUID, environment_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id),
            service_principal_id=synthetic_uuid(908),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("source_observation"),
            trace_id=synthetic_uuid(909),
        )

    def ingestion_context(
        workspace_id: UUID, environment_id: UUID, operation: str
    ) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=EnvironmentScope(
                workspace_id=workspace_id, environment_id=environment_id
            ),
            service_principal_id=synthetic_uuid(910),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation(operation),
            trace_id=synthetic_uuid(911),
        )

    def worker_workspace_context(workspace_id: UUID) -> TrustedPersistenceContext:
        return boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace_id),
            service_principal_id=synthetic_uuid(906),
            purpose=PersistencePurpose("contract_test"),
            operation=PersistenceOperation("workspace_repository"),
            trace_id=synthetic_uuid(907),
        )

    @asynccontextmanager
    async def hold_transactions() -> AsyncIterator[None]:
        await transaction_lock.acquire()
        try:
            yield
        finally:
            transaction_lock.release()

    return DeliveryPersistenceAdapter(
        uow_factory=persistence.uow_factory,
        workspace_context=workspace_context,
        worker_workspace_context=worker_workspace_context,
        environment_context=environment_context,
        hold_transactions=hold_transactions,
        outbox_events=outbox_events,
        audit_events=audit_events,
        read_audit_event=persistence.audit_reader.resolve,
        fail_next_audit_append=persistence.fail_next_audit_append,
        source_context=source_context,
        ingestion_context=ingestion_context,
        delivery_repository=persistence.delivery_repository,
        delivery_routes=delivery_routes,
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


@pytest_asyncio.fixture
async def delivery_persistence_adapter() -> DeliveryPersistenceAdapter:
    return await create_in_memory_adapter()
