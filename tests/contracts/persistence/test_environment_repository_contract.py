from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from spine.application.persistence.context import EnvironmentScope, issue_trusted_context_authority
from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidPersistenceContextError,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace

from .conftest import PersistenceAdapter


def workspace(workspace_id: UUID) -> Workspace:
    return Workspace(id=workspace_id, slug="northwind", display_name="Northwind")


def environment(workspace_id: UUID, environment_id: UUID | None = None) -> Environment:
    return Environment(
        id=environment_id or uuid4(),
        workspace_id=workspace_id,
        kind=EnvironmentKind.PRODUCTION,
        display_name="Production",
    )


async def persist_workspace(adapter: PersistenceAdapter, workspace_id: UUID) -> None:
    async with adapter.uow_factory(adapter.workspace_context(workspace_id)) as uow:
        await uow.workspaces.add(workspace(workspace_id))
        await uow.commit()


@pytest.mark.asyncio
async def test_environment_commit_makes_it_visible_within_owning_workspace(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = uuid4()
    expected = environment(workspace_id)
    await persist_workspace(persistence_adapter, workspace_id)

    async with persistence_adapter.uow_factory(
        persistence_adapter.workspace_context(workspace_id)
    ) as uow:
        await uow.environments.add(expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(
        persistence_adapter.workspace_context(workspace_id)
    ) as uow:
        actual = await uow.environments.resolve(expected.id)

    assert actual == expected


@pytest.mark.asyncio
async def test_environment_cannot_be_added_for_another_workspace(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = uuid4()
    foreign_workspace_id = uuid4()
    await persist_workspace(persistence_adapter, workspace_id)

    with pytest.raises(InvalidPersistenceContextError) as raised:
        async with persistence_adapter.uow_factory(
            persistence_adapter.workspace_context(workspace_id)
        ) as uow:
            await uow.environments.add(environment(foreign_workspace_id))

    assert str(foreign_workspace_id) not in str(raised.value)


@pytest.mark.asyncio
async def test_foreign_environment_is_not_disclosed(
    persistence_adapter: PersistenceAdapter,
) -> None:
    owner_id = uuid4()
    other_id = uuid4()
    expected = environment(owner_id)
    await persist_workspace(persistence_adapter, owner_id)
    await persist_workspace(persistence_adapter, other_id)
    async with persistence_adapter.uow_factory(persistence_adapter.workspace_context(owner_id)) as uow:
        await uow.environments.add(expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(persistence_adapter.workspace_context(other_id)) as uow:
        assert await uow.environments.resolve(expected.id) is None


@pytest.mark.asyncio
async def test_duplicate_environment_identity_raises_typed_conflict(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = uuid4()
    original = environment(workspace_id)
    await persist_workspace(persistence_adapter, workspace_id)
    context = persistence_adapter.workspace_context(workspace_id)
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.environments.add(original)
        await uow.commit()

    with pytest.raises(ConstraintConflictError, match="already exists"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.environments.add(environment(workspace_id, original.id))


@pytest.mark.asyncio
async def test_mismatched_environment_scope_fails_before_repository_access(
    persistence_adapter: PersistenceAdapter,
) -> None:
    owner_id = uuid4()
    foreign_id = uuid4()
    expected = environment(owner_id)
    await persist_workspace(persistence_adapter, owner_id)
    async with persistence_adapter.uow_factory(persistence_adapter.workspace_context(owner_id)) as uow:
        await uow.environments.add(expected)
        await uow.commit()
    issuer = issue_trusted_context_authority()
    mismatched = issuer.worker(
        scope=EnvironmentScope(workspace_id=foreign_id, environment_id=expected.id),
        service_principal_id=uuid4(),
        purpose="projection",
        operation="read_environment",
        trace_id=uuid4(),
    )

    with pytest.raises(InvalidPersistenceContextError) as raised:
        async with persistence_adapter.uow_factory(mismatched):
            pytest.fail("an inconsistent context became repository-capable")

    assert str(owner_id) not in str(raised.value)
    assert str(foreign_id) not in str(raised.value)
    assert str(expected.id) not in str(raised.value)
