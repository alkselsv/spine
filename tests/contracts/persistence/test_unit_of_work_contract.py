from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from spine.application.persistence.errors import (
    ConstraintConflictError,
    UnitOfWorkLifecycleError,
)
from spine.domain.workspaces import Workspace

from .conftest import PersistenceAdapter


def workspace(workspace_id: UUID | None = None) -> Workspace:
    return Workspace(
        id=workspace_id or uuid4(),
        slug="northwind",
        display_name="Northwind",
    )


@pytest.mark.asyncio
async def test_explicit_commit_makes_workspace_visible(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace()
    context = persistence_adapter.workspace_context(expected.id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        actual = await uow.workspaces.resolve(expected.id)

    assert actual == expected


@pytest.mark.asyncio
async def test_independent_uows_do_not_observe_uncommitted_or_late_mutations(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace()
    context = persistence_adapter.workspace_context(expected.id)

    async with persistence_adapter.uow_factory(context) as writer:
        await writer.workspaces.add(expected)
        async with persistence_adapter.uow_factory(context) as existing_reader:
            assert await existing_reader.workspaces.resolve(expected.id) is None
            await writer.commit()
            assert await existing_reader.workspaces.resolve(expected.id) is None

    async with persistence_adapter.uow_factory(context) as later_reader:
        assert await later_reader.workspaces.resolve(expected.id) == expected


@pytest.mark.asyncio
async def test_concurrent_duplicate_commit_has_one_logical_effect(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace()
    context = persistence_adapter.workspace_context(expected.id)

    async with persistence_adapter.uow_factory(context) as first:
        async with persistence_adapter.uow_factory(context) as second:
            await first.workspaces.add(expected)
            await second.workspaces.add(expected)
            await first.commit()
            with pytest.raises(ConstraintConflictError, match="already exists"):
                await second.commit()

    async with persistence_adapter.uow_factory(context) as reader:
        assert await reader.workspaces.resolve(expected.id) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_error", [False, True])
async def test_exit_without_successful_commit_discards_workspace(
    persistence_adapter: PersistenceAdapter,
    raise_error: bool,
) -> None:
    expected = workspace()
    context = persistence_adapter.workspace_context(expected.id)

    with pytest.raises(RuntimeError) if raise_error else _does_not_raise():
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.workspaces.add(expected)
            if raise_error:
                raise RuntimeError("synthetic failure")

    async with persistence_adapter.uow_factory(context) as uow:
        assert await uow.workspaces.resolve(expected.id) is None


@pytest.mark.asyncio
async def test_explicit_rollback_is_idempotent_before_close(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace()
    context = persistence_adapter.workspace_context(expected.id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(expected)
        await uow.rollback()
        await uow.rollback()

    async with persistence_adapter.uow_factory(context) as uow:
        assert await uow.workspaces.resolve(expected.id) is None


@pytest.mark.asyncio
async def test_duplicate_workspace_identity_raises_typed_conflict(
    persistence_adapter: PersistenceAdapter,
) -> None:
    original = workspace()
    context = persistence_adapter.workspace_context(original.id)
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(original)
        await uow.commit()

    with pytest.raises(ConstraintConflictError, match="already exists"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.workspaces.add(workspace(original.id))


@pytest.mark.asyncio
async def test_uow_reentry_fails_deterministically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(uuid4())
    uow = persistence_adapter.uow_factory(context)

    async with uow:
        with pytest.raises(UnitOfWorkLifecycleError, match="cannot be entered"):
            await uow.__aenter__()


@pytest.mark.asyncio
async def test_uow_concurrent_entry_fails_deterministically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(uuid4())
    uow = persistence_adapter.uow_factory(context)

    entered = await uow.__aenter__()
    task = asyncio.create_task(uow.__aenter__())
    with pytest.raises(UnitOfWorkLifecycleError, match="cannot be entered"):
        await task
    await entered.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_uow_concurrent_use_fails_deterministically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(uuid4())

    async with persistence_adapter.uow_factory(context) as uow:
        task = asyncio.create_task(uow.workspaces.resolve(context.scope.workspace_id))
        with pytest.raises(UnitOfWorkLifecycleError, match="owning task"):
            await task


@pytest.mark.asyncio
async def test_uow_post_close_access_fails_deterministically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(uuid4())
    uow = persistence_adapter.uow_factory(context)
    async with uow:
        repository = uow.workspaces

    with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
        await repository.resolve(context.scope.workspace_id)
    with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
        await uow.commit()


class _does_not_raise:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None
