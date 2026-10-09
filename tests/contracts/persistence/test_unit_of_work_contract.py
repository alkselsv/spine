from __future__ import annotations

import asyncio
from contextlib import nullcontext
from uuid import UUID

import pytest

from spine.application.persistence.errors import (
    ConstraintConflictError,
    UnitOfWorkLifecycleError,
)
from spine.domain.workspaces import Workspace

from .adapter import PersistenceAdapter
from .ids import synthetic_uuid


def workspace(workspace_id: UUID) -> Workspace:
    return Workspace(id=workspace_id, slug="northwind", display_name="Northwind")


@pytest.mark.asyncio
async def test_explicit_commit_makes_workspace_visible(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace(synthetic_uuid(1))
    context = persistence_adapter.workspace_context(expected.id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(expected)
        await uow.commit()

    async with persistence_adapter.uow_factory(context) as uow:
        actual = await uow.workspaces.resolve(expected.id)

    assert actual == expected


@pytest.mark.asyncio
async def test_missing_workspace_is_normal_result_and_uow_remains_usable(
    persistence_adapter: PersistenceAdapter,
) -> None:
    workspace_id = synthetic_uuid(15)
    context = persistence_adapter.workspace_context(workspace_id)

    async with persistence_adapter.uow_factory(context) as uow:
        assert await uow.workspaces.resolve(workspace_id) is None
        assert await uow.workspaces.resolve(workspace_id) is None
        await uow.commit()


@pytest.mark.asyncio
async def test_independent_uows_do_not_observe_uncommitted_mutations(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace(synthetic_uuid(2))
    context = persistence_adapter.workspace_context(expected.id)

    async with persistence_adapter.uow_factory(context) as writer:
        await writer.workspaces.add(expected)
        async with persistence_adapter.uow_factory(context) as existing_reader:
            assert await existing_reader.workspaces.resolve(expected.id) is None
            await writer.commit()

    async with persistence_adapter.uow_factory(context) as later_reader:
        assert await later_reader.workspaces.resolve(expected.id) == expected


@pytest.mark.asyncio
async def test_concurrent_duplicate_commit_has_one_logical_effect(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace(synthetic_uuid(3))
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
    expected = workspace(synthetic_uuid(4 if raise_error else 5))
    context = persistence_adapter.workspace_context(expected.id)

    with pytest.raises(RuntimeError) if raise_error else nullcontext():
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
    expected = workspace(synthetic_uuid(6))
    context = persistence_adapter.workspace_context(expected.id)

    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(expected)
        await uow.rollback()
        await uow.rollback()

    async with persistence_adapter.uow_factory(context) as uow:
        assert await uow.workspaces.resolve(expected.id) is None


@pytest.mark.asyncio
async def test_repository_failure_is_terminal_and_discards_pending_mutations(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace(synthetic_uuid(7))
    context = persistence_adapter.workspace_context(expected.id)
    uow = persistence_adapter.uow_factory(context)

    async with uow:
        workspace_repository = uow.workspaces
        await workspace_repository.add(expected)
        with pytest.raises(ConstraintConflictError, match="already exists"):
            await workspace_repository.add(expected.model_copy(deep=True))
        with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
            await workspace_repository.resolve(expected.id)
        with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
            await workspace_repository.add(expected)
        with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
            await uow.commit()

    async with persistence_adapter.uow_factory(context) as reader:
        assert await reader.workspaces.resolve(expected.id) is None


@pytest.mark.asyncio
async def test_duplicate_workspace_identity_raises_typed_conflict(
    persistence_adapter: PersistenceAdapter,
) -> None:
    original = workspace(synthetic_uuid(8))
    context = persistence_adapter.workspace_context(original.id)
    async with persistence_adapter.uow_factory(context) as uow:
        await uow.workspaces.add(original)
        await uow.commit()

    with pytest.raises(ConstraintConflictError, match="already exists"):
        async with persistence_adapter.uow_factory(context) as uow:
            await uow.workspaces.add(original.model_copy(deep=True))


@pytest.mark.asyncio
async def test_uow_reentry_fails_deterministically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(synthetic_uuid(9))
    uow = persistence_adapter.uow_factory(context)

    async with uow:
        with pytest.raises(UnitOfWorkLifecycleError, match="cannot be entered"):
            await uow.__aenter__()


@pytest.mark.asyncio
async def test_uow_concurrent_entry_fails_deterministically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(synthetic_uuid(10))
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
    context = persistence_adapter.workspace_context(synthetic_uuid(11))

    async with persistence_adapter.uow_factory(context) as uow:
        task = asyncio.create_task(uow.workspaces.resolve(context.scope.workspace_id))
        with pytest.raises(UnitOfWorkLifecycleError, match="owning task"):
            await task


@pytest.mark.asyncio
async def test_uow_post_close_access_fails_deterministically(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(synthetic_uuid(12))
    uow = persistence_adapter.uow_factory(context)
    async with uow:
        repository = uow.workspaces

    with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
        await repository.resolve(context.scope.workspace_id)
    with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
        await uow.commit()


@pytest.mark.asyncio
async def test_cancelled_entry_is_terminal(
    persistence_adapter: PersistenceAdapter,
) -> None:
    context = persistence_adapter.workspace_context(synthetic_uuid(13))
    uow = persistence_adapter.uow_factory(context)
    started = asyncio.Event()

    async def enter() -> None:
        started.set()
        await uow.__aenter__()

    async with persistence_adapter.hold_transactions():
        task = asyncio.create_task(enter())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with pytest.raises(UnitOfWorkLifecycleError):
        await uow.commit()


@pytest.mark.asyncio
async def test_cancelled_commit_is_terminal_and_discards_pending_mutation(
    persistence_adapter: PersistenceAdapter,
) -> None:
    expected = workspace(synthetic_uuid(14))
    context = persistence_adapter.workspace_context(expected.id)
    ready = asyncio.Event()
    begin_commit = asyncio.Event()
    commit_started = asyncio.Event()

    async def write() -> None:
        async with persistence_adapter.uow_factory(context) as uow:
            repository = uow.workspaces
            await repository.add(expected)
            ready.set()
            await begin_commit.wait()
            commit_started.set()
            try:
                await uow.commit()
            except asyncio.CancelledError:
                with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
                    await repository.resolve(expected.id)
                with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
                    await uow.commit()
                raise

    task = asyncio.create_task(write())
    await ready.wait()
    async with persistence_adapter.hold_transactions():
        begin_commit.set()
        await commit_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async with persistence_adapter.uow_factory(context) as reader:
        assert await reader.workspaces.resolve(expected.id) is None
