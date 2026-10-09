from __future__ import annotations

from uuid import uuid4

import pytest

from spine.application.persistence.context import (
    EnvironmentScope,
    WorkspaceScope,
    issue_trusted_context_authority,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["interactive", "worker"])
async def test_interactive_and_worker_contexts_obey_same_environment_scope(origin: str) -> None:
    persistence = InMemoryPersistence()
    issuer = issue_trusted_context_authority()
    workspace_id = uuid4()
    environment_id = uuid4()
    setup_context = issuer.worker(
        scope=WorkspaceScope(workspace_id=workspace_id),
        service_principal_id=uuid4(),
        purpose="test_setup",
        operation="create_environment",
        trace_id=uuid4(),
    )
    async with persistence.uow_factory(setup_context) as uow:
        await uow.workspaces.add(
            Workspace(id=workspace_id, slug="northwind", display_name="Northwind")
        )
        await uow.environments.add(
            Environment(
                id=environment_id,
                workspace_id=workspace_id,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production",
            )
        )
        await uow.commit()

    scope = EnvironmentScope(workspace_id=workspace_id, environment_id=environment_id)
    if origin == "interactive":
        context = issuer.interactive(
            scope=scope,
            acting_subject_id=uuid4(),
            purpose="question_answering",
            operation="resolve_environment",
            trace_id=uuid4(),
        )
    else:
        context = issuer.worker(
            scope=scope,
            service_principal_id=uuid4(),
            purpose="projection",
            operation="resolve_environment",
            trace_id=uuid4(),
        )

    async with persistence.uow_factory(context) as uow:
        resolved = await uow.environments.resolve(environment_id)

    assert resolved is not None
    assert resolved.workspace_id == workspace_id


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["interactive", "worker"])
async def test_interactive_and_worker_contexts_fail_closed_on_mismatch(origin: str) -> None:
    persistence = InMemoryPersistence()
    issuer = issue_trusted_context_authority()
    scope = EnvironmentScope(workspace_id=uuid4(), environment_id=uuid4())
    if origin == "interactive":
        context = issuer.interactive(
            scope=scope,
            acting_subject_id=uuid4(),
            purpose="question_answering",
            operation="resolve_environment",
            trace_id=uuid4(),
        )
    else:
        context = issuer.worker(
            scope=scope,
            service_principal_id=uuid4(),
            purpose="projection",
            operation="resolve_environment",
            trace_id=uuid4(),
        )

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        async with persistence.uow_factory(context):
            pytest.fail("invalid context became usable")
