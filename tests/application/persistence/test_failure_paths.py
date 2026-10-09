from __future__ import annotations

from typing import ClassVar
from uuid import UUID

import pytest

from spine.application.persistence.context import (
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    UnexpectedPersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
import spine.infrastructure.persistence.in_memory as in_memory


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000001")
ACTING_SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("50000000-0000-0000-0000-000000000001")
ISSUER_ID = UUID("60000000-0000-0000-0000-000000000001")
COPY_FAILURE_DETAIL = "provider detail: foreign workspace 999"


def authority() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(
        issuer_id=ISSUER_ID,
        secret=b"issue-40-failure-paths-secret-0001",
    )


def context(
    boundary: TrustedContextBoundary,
    workspace_id: UUID,
) -> TrustedPersistenceContext:
    return boundary.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=ACTING_SUBJECT_ID,
        purpose=PersistencePurpose("failure_probe"),
        operation=PersistenceOperation("persistence_probe"),
        trace_id=TRACE_ID,
    )


def workspace(workspace_id: UUID) -> Workspace:
    return Workspace(
        id=workspace_id,
        slug="northwind" if workspace_id == WORKSPACE_ID else "contoso",
        display_name="Northwind" if workspace_id == WORKSPACE_ID else "Contoso",
    )


def environment() -> Environment:
    return Environment(
        id=ENVIRONMENT_ID,
        workspace_id=OTHER_WORKSPACE_ID,
        kind=EnvironmentKind.PRODUCTION,
        display_name="Production",
    )


class FailingWorkspace(Workspace):
    def model_copy(self, *, update=None, deep=False):
        raise RuntimeError(COPY_FAILURE_DETAIL)


class FailingEnvironment(Environment):
    def model_copy(self, *, update=None, deep=False):
        raise RuntimeError(COPY_FAILURE_DETAIL)


class ReadFailingWorkspace(Workspace):
    copy_calls: ClassVar[int] = 0

    def model_copy(self, *, update=None, deep=False):
        type(self).copy_calls += 1
        if type(self).copy_calls >= 4:
            raise RuntimeError(COPY_FAILURE_DETAIL)
        return self


class EntryFailingWorkspace(Workspace):
    copy_calls: ClassVar[int] = 0

    def model_copy(self, *, update=None, deep=False):
        type(self).copy_calls += 1
        if type(self).copy_calls >= 3:
            raise RuntimeError(COPY_FAILURE_DETAIL)
        return self


class ReadFailingEnvironment(Environment):
    copy_calls: ClassVar[int] = 0

    def model_copy(self, *, update=None, deep=False):
        type(self).copy_calls += 1
        if type(self).copy_calls >= 4:
            raise RuntimeError(COPY_FAILURE_DETAIL)
        return self


class CommitFailingWorkspace(Workspace):
    copy_calls: ClassVar[int] = 0

    def model_copy(self, *, update=None, deep=False):
        type(self).copy_calls += 1
        if type(self).copy_calls >= 2:
            raise RuntimeError(COPY_FAILURE_DETAIL)
        return self


class CommitFailingEnvironment(Environment):
    copy_calls: ClassVar[int] = 0

    def model_copy(self, *, update=None, deep=False):
        type(self).copy_calls += 1
        if type(self).copy_calls >= 2:
            raise RuntimeError(COPY_FAILURE_DETAIL)
        return self


async def configured_persistence() -> tuple[InMemoryPersistence, TrustedContextBoundary]:
    context_authority = authority()
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=context_authority,
        bootstrap_authority=bootstrap_authority,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        workspace(WORKSPACE_ID),
    )
    return persistence, context_authority


@pytest.mark.asyncio
async def test_bootstrap_copy_failure_is_sanitized_and_leaves_store_uninitialized() -> None:
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=authority(),
        bootstrap_authority=bootstrap_authority,
    )

    with pytest.raises(UnexpectedPersistenceError) as raised:
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            bootstrap_authority,
            FailingWorkspace(
                id=WORKSPACE_ID,
                slug="northwind",
                display_name="Northwind",
            ),
        )
    assert str(raised.value) == "Persistence operation failed."
    assert COPY_FAILURE_DETAIL not in str(raised.value)

    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        workspace(WORKSPACE_ID),
    )


async def assert_terminal_after_failure(uow, operation) -> None:
    with pytest.raises(UnexpectedPersistenceError) as raised:
        await operation()
    assert COPY_FAILURE_DETAIL not in str(raised.value)
    assert str(raised.value) == "Persistence operation failed."

    with pytest.raises(UnitOfWorkLifecycleError):
        await uow.workspaces.resolve(OTHER_WORKSPACE_ID)
    with pytest.raises(UnitOfWorkLifecycleError):
        await uow.workspaces.add(workspace(OTHER_WORKSPACE_ID))
    with pytest.raises(UnitOfWorkLifecycleError):
        await uow.commit()


@pytest.mark.asyncio
async def test_workspace_write_failure_is_sanitized_and_terminal() -> None:
    persistence, context_authority = await configured_persistence()
    uow = persistence.uow_factory(context(context_authority, OTHER_WORKSPACE_ID))

    async with uow:
        await assert_terminal_after_failure(
            uow,
            lambda: uow.workspaces.add(
                FailingWorkspace(
                    id=OTHER_WORKSPACE_ID,
                    slug="contoso",
                    display_name="Contoso",
                )
            ),
        )

    async with persistence.uow_factory(context(context_authority, OTHER_WORKSPACE_ID)) as reader:
        assert await reader.workspaces.resolve(OTHER_WORKSPACE_ID) is None


@pytest.mark.asyncio
async def test_environment_write_failure_is_sanitized_and_terminal() -> None:
    persistence, context_authority = await configured_persistence()
    async with persistence.uow_factory(context(context_authority, OTHER_WORKSPACE_ID)) as setup:
        await setup.workspaces.add(workspace(OTHER_WORKSPACE_ID))
        await setup.commit()
    uow = persistence.uow_factory(context(context_authority, OTHER_WORKSPACE_ID))

    async with uow:
        await assert_terminal_after_failure(
            uow,
            lambda: uow.environments.add(FailingEnvironment(**environment().model_dump())),
        )

    async with persistence.uow_factory(context(context_authority, OTHER_WORKSPACE_ID)) as reader:
        assert await reader.environments.resolve(ENVIRONMENT_ID) is None


@pytest.mark.asyncio
async def test_workspace_read_failure_is_sanitized_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence, context_authority = await configured_persistence()
    write_context = context(context_authority, OTHER_WORKSPACE_ID)
    async with persistence.uow_factory(write_context) as uow:
        await uow.workspaces.add(
            workspace(OTHER_WORKSPACE_ID)
        )
        await uow.commit()

    def fail_copy(value: Workspace) -> Workspace:
        raise RuntimeError(COPY_FAILURE_DETAIL)

    monkeypatch.setattr(in_memory, "_workspace_copy", fail_copy)
    uow = persistence.uow_factory(write_context)
    with pytest.raises(UnexpectedPersistenceError):
        await uow.__aenter__()
    with pytest.raises(UnitOfWorkLifecycleError):
        await uow.commit()


@pytest.mark.asyncio
async def test_entry_copy_failure_is_sanitized_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence, context_authority = await configured_persistence()
    write_context = context(context_authority, OTHER_WORKSPACE_ID)
    def fail_copy(value: Workspace) -> Workspace:
        raise RuntimeError(COPY_FAILURE_DETAIL)

    monkeypatch.setattr(in_memory, "_workspace_copy", fail_copy)

    uow = persistence.uow_factory(write_context)
    with pytest.raises(UnexpectedPersistenceError) as raised:
        await uow.__aenter__()
    assert str(raised.value) == "Persistence operation failed."
    with pytest.raises(UnitOfWorkLifecycleError):
        await uow.commit()


@pytest.mark.asyncio
async def test_environment_read_failure_is_sanitized_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence, context_authority = await configured_persistence()
    write_context = context(context_authority, OTHER_WORKSPACE_ID)
    async with persistence.uow_factory(write_context) as setup:
        await setup.workspaces.add(workspace(OTHER_WORKSPACE_ID))
        await setup.commit()
    async with persistence.uow_factory(write_context) as uow:
        await uow.environments.add(environment())
        await uow.commit()

    def fail_copy(value: Environment) -> Environment:
        raise RuntimeError(COPY_FAILURE_DETAIL)

    monkeypatch.setattr(in_memory, "_environment_copy", fail_copy)
    uow = persistence.uow_factory(write_context)
    with pytest.raises(UnexpectedPersistenceError):
        await uow.__aenter__()
    with pytest.raises(UnitOfWorkLifecycleError):
        await uow.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_environment",
    [False, True],
    ids=("workspace-copy", "environment-copy-after-workspace-preparation"),
)
async def test_commit_copy_failure_is_atomic_and_terminal(
    failing_environment: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence, context_authority = await configured_persistence()
    write_context = context(context_authority, OTHER_WORKSPACE_ID)
    uow = persistence.uow_factory(write_context)

    async with uow:
        if failing_environment:
            await uow.workspaces.add(workspace(OTHER_WORKSPACE_ID))
            await uow.environments.add(environment())
            def fail_copy(value: Environment) -> Environment:
                raise RuntimeError(COPY_FAILURE_DETAIL)
            monkeypatch.setattr(in_memory, "_environment_copy", fail_copy)
        else:
            await uow.workspaces.add(workspace(OTHER_WORKSPACE_ID))
            def fail_copy(value: Workspace) -> Workspace:
                raise RuntimeError(COPY_FAILURE_DETAIL)
            monkeypatch.setattr(in_memory, "_workspace_copy", fail_copy)
        with pytest.raises(UnexpectedPersistenceError) as raised:
            await uow.commit()
        assert COPY_FAILURE_DETAIL not in str(raised.value)
        assert str(raised.value) == "Persistence operation failed."
        with pytest.raises(UnitOfWorkLifecycleError):
            await uow.commit()

    monkeypatch.undo()
    async with persistence.uow_factory(write_context) as reader:
        assert await reader.workspaces.resolve(OTHER_WORKSPACE_ID) is None
        assert await reader.environments.resolve(ENVIRONMENT_ID) is None
