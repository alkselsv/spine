from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.persistence.context import (
    PersistenceOperation,
    PersistencePurpose,
    WorkspaceScope,
)
from spine.application.persistence.errors import UnexpectedPersistenceError
from spine.domain.common import EnvironmentKind
from spine.application.persistence.outbox import OutboxEventRegistry
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence
import spine.infrastructure.persistence.in_memory as in_memory


ANCHOR_ID = UUID("71000000-0000-0000-0000-000000000001")
WORKSPACE_ID = UUID("71000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("72000000-0000-0000-0000-000000000001")
ACTOR_ID = UUID("73000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("74000000-0000-0000-0000-000000000001")
ISSUER_ID = UUID("75000000-0000-0000-0000-000000000001")


def boundary() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(
        issuer_id=ISSUER_ID,
        secret=b"issue-40-alias-isolation-secret-01",
    )


def workspace(workspace_id: UUID = WORKSPACE_ID) -> Workspace:
    return Workspace(id=workspace_id, slug="tenant", display_name="Tenant")


def environment() -> Environment:
    return Environment(
        id=ENVIRONMENT_ID,
        workspace_id=WORKSPACE_ID,
        kind=EnvironmentKind.PRODUCTION,
        display_name="Production",
    )


def workspace_context(authority: TrustedContextBoundary):
    return authority.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTOR_ID,
        purpose=PersistencePurpose("alias_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=TRACE_ID,
    )


async def configured() -> tuple[InMemoryPersistence, TrustedContextBoundary]:
    authority = boundary()
    bootstrap = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(outbox_events=OutboxEventRegistry(),
        context_verifier=authority,
        bootstrap_authority=bootstrap,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap,
        Workspace(
            id=ANCHOR_ID,
            slug="anchor",
            display_name="Anchor",
        ),
    )
    return persistence, authority


class SelfCopyWorkspace(Workspace):
    def model_copy(self, *, update=None, deep=False):
        return self


class SelfCopyEnvironment(Environment):
    def model_copy(self, *, update=None, deep=False):
        return self


@pytest.mark.asyncio
async def test_polymorphic_model_copy_cannot_enter_persistent_state() -> None:
    persistence, authority = await configured()
    async with persistence.uow_factory(workspace_context(authority)) as uow:
        with pytest.raises(UnexpectedPersistenceError):
            await uow.workspaces.add(
                SelfCopyWorkspace(
                    id=WORKSPACE_ID,
                    slug="tenant",
                    display_name="Tenant",
                )
            )

    async with persistence.uow_factory(workspace_context(authority)) as reader:
        assert await reader.workspaces.resolve(WORKSPACE_ID) is None


@pytest.mark.asyncio
async def test_staged_workspace_is_detached_before_caller_mutation() -> None:
    persistence, authority = await configured()
    candidate = workspace()
    async with persistence.uow_factory(workspace_context(authority)) as uow:
        await uow.workspaces.add(candidate)
        candidate.display_name = "Caller mutation"
        await uow.commit()

    async with persistence.uow_factory(workspace_context(authority)) as reader:
        stored = await reader.workspaces.resolve(WORKSPACE_ID)
        assert stored is not None
        assert stored.display_name == "Tenant"


@pytest.mark.asyncio
async def test_repository_results_are_detached_from_committed_workspace() -> None:
    persistence, authority = await configured()
    candidate = workspace()
    async with persistence.uow_factory(workspace_context(authority)) as uow:
        await uow.workspaces.add(candidate)
        await uow.commit()

    async with persistence.uow_factory(workspace_context(authority)) as reader:
        result = await reader.workspaces.resolve(WORKSPACE_ID)
        assert result is not None
        result.display_name = "Returned mutation"
        reread = await reader.workspaces.resolve(WORKSPACE_ID)
        assert reread is not None
        assert reread.display_name == "Tenant"


@pytest.mark.asyncio
async def test_independent_units_of_work_do_not_share_staged_aliases() -> None:
    persistence, authority = await configured()
    candidate = workspace()
    writer = persistence.uow_factory(workspace_context(authority))
    reader = persistence.uow_factory(workspace_context(authority))
    async with writer, reader:
        await writer.workspaces.add(candidate)
        candidate.display_name = "Caller mutation"
        assert await reader.workspaces.resolve(WORKSPACE_ID) is None
        await writer.commit()

    async with persistence.uow_factory(workspace_context(authority)) as committed:
        stored = await committed.workspaces.resolve(WORKSPACE_ID)
        assert stored is not None
        assert stored.display_name == "Tenant"


@pytest.mark.asyncio
async def test_failed_commit_discards_detached_staged_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence, authority = await configured()
    candidate = workspace()

    def fail_copy(value: Workspace) -> Workspace:
        raise RuntimeError("provider workspace detail")

    async with persistence.uow_factory(workspace_context(authority)) as uow:
        await uow.workspaces.add(candidate)
        candidate.display_name = "Mutation after staging"
        monkeypatch.setattr(in_memory, "_workspace_copy", fail_copy)
        with pytest.raises(UnexpectedPersistenceError):
            await uow.commit()
        monkeypatch.undo()

    async with persistence.uow_factory(workspace_context(authority)) as reader:
        assert await reader.workspaces.resolve(WORKSPACE_ID) is None


@pytest.mark.asyncio
async def test_environment_inputs_and_results_are_detached() -> None:
    persistence, authority = await configured()
    async with persistence.uow_factory(workspace_context(authority)) as setup:
        await setup.workspaces.add(workspace())
        await setup.commit()

    candidate = environment()
    async with persistence.uow_factory(workspace_context(authority)) as uow:
        await uow.environments.add(candidate)
        candidate.display_name = "Caller mutation"
        await uow.commit()

    async with persistence.uow_factory(workspace_context(authority)) as reader:
        result = await reader.environments.resolve(ENVIRONMENT_ID)
        assert result is not None
        assert result.display_name == "Production"
        result.display_name = "Returned mutation"
        reread = await reader.environments.resolve(ENVIRONMENT_ID)
        assert reread is not None
        assert reread.display_name == "Production"


@pytest.mark.asyncio
async def test_polymorphic_environment_copy_cannot_enter_persistent_state() -> None:
    persistence, authority = await configured()
    async with persistence.uow_factory(workspace_context(authority)) as uow:
        await uow.workspaces.add(workspace())
        with pytest.raises(UnexpectedPersistenceError):
            await uow.environments.add(
                SelfCopyEnvironment(**environment().model_dump())
            )


@pytest.mark.asyncio
async def test_bootstrap_detaches_input_workspace() -> None:
    authority = boundary()
    bootstrap = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(outbox_events=OutboxEventRegistry(),
        context_verifier=authority,
        bootstrap_authority=bootstrap,
    )
    candidate = Workspace(id=ANCHOR_ID, slug="anchor", display_name="Anchor")
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap,
        candidate,
    )
    candidate.display_name = "Caller mutation"

    context = authority.interactive(
        scope=WorkspaceScope(workspace_id=ANCHOR_ID),
        acting_subject_id=ACTOR_ID,
        purpose=PersistencePurpose("alias_test"),
        operation=PersistenceOperation("workspace_repository"),
        trace_id=TRACE_ID,
    )
    async with persistence.uow_factory(context) as reader:
        stored = await reader.workspaces.resolve(ANCHOR_ID)
        assert stored is not None
        assert stored.display_name == "Anchor"
