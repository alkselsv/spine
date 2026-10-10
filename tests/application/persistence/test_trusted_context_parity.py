from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.domain.common import EnvironmentKind
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000001")
FOREIGN_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000002")
ACTOR_ID = UUID("30000000-0000-0000-0000-000000000001")
SERVICE_ID = UUID("40000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("50000000-0000-0000-0000-000000000001")
ISSUER_ID = UUID("60000000-0000-0000-0000-000000000001")
SECRET = b"issue-40-parity-context-secret-00001"


async def configured_persistence() -> tuple[InMemoryPersistence, TrustedContextBoundary]:
    boundary = TrustedContextBoundary.for_testing(issuer_id=ISSUER_ID, secret=SECRET)
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(outbox_events=OutboxEventRegistry(),
        context_verifier=boundary,
        bootstrap_authority=bootstrap_authority,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(id=WORKSPACE_ID, slug="northwind", display_name="Northwind"),
    )
    setup_context = boundary.worker(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        service_principal_id=SERVICE_ID,
        purpose=PersistencePurpose("test_setup"),
        operation=PersistenceOperation("create_environment"),
        trace_id=TRACE_ID,
    )
    async with persistence.uow_factory(setup_context) as uow:
        await uow.environments.add(
            Environment(
                id=ENVIRONMENT_ID,
                workspace_id=WORKSPACE_ID,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()
    return persistence, boundary


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["interactive", "worker"])
async def test_interactive_and_worker_contexts_obey_same_environment_scope(origin: str) -> None:
    persistence, boundary = await configured_persistence()
    scope = EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID)
    if origin == "interactive":
        context = boundary.interactive(
            scope=scope,
            acting_subject_id=ACTOR_ID,
            service_principal_id=SERVICE_ID,
            purpose=PersistencePurpose("question_answering"),
            operation=PersistenceOperation("resolve_environment"),
            trace_id=TRACE_ID,
        )
    else:
        context = boundary.worker(
            scope=scope,
            service_principal_id=SERVICE_ID,
            purpose=PersistencePurpose("projection"),
            operation=PersistenceOperation("resolve_environment"),
            trace_id=TRACE_ID,
        )

    async with persistence.uow_factory(context) as uow:
        resolved = await uow.environments.resolve(ENVIRONMENT_ID)

    assert resolved is not None
    assert resolved.workspace_id == WORKSPACE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["interactive", "worker"])
async def test_interactive_and_worker_contexts_fail_closed_on_mismatch(origin: str) -> None:
    persistence, boundary = await configured_persistence()
    scope = EnvironmentScope(
        workspace_id=FOREIGN_WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
    )
    if origin == "interactive":
        context = boundary.interactive(
            scope=scope,
            acting_subject_id=ACTOR_ID,
            service_principal_id=SERVICE_ID,
            purpose=PersistencePurpose("question_answering"),
            operation=PersistenceOperation("resolve_environment"),
            trace_id=TRACE_ID,
        )
    else:
        context = boundary.worker(
            scope=scope,
            service_principal_id=SERVICE_ID,
            purpose=PersistencePurpose("projection"),
            operation=PersistenceOperation("resolve_environment"),
            trace_id=TRACE_ID,
        )

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        async with persistence.uow_factory(context):
            pytest.fail("invalid context became usable")
