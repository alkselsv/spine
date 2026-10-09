from __future__ import annotations

from uuid import UUID

import pytest

from spine.application.persistence.context import (
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidBootstrapAuthorityError,
    InvalidPersistenceContextError,
    UnitOfWorkLifecycleError,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


INITIAL_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000002")
ACTOR_ID = UUID("20000000-0000-0000-0000-000000000001")
TRACE_ID = UUID("30000000-0000-0000-0000-000000000001")
ISSUER_ID = UUID("40000000-0000-0000-0000-000000000001")
SECRET = b"issue-40-bootstrap-context-secret-01"
PURPOSE = PersistencePurpose("workspace_admin")
OPERATION = PersistenceOperation("create_workspace")


def workspace(workspace_id: UUID = INITIAL_WORKSPACE_ID) -> Workspace:
    return Workspace(id=workspace_id, slug="initial", display_name="Initial Workspace")


def boundary() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(issuer_id=ISSUER_ID, secret=SECRET)


def context(
    authority: TrustedContextBoundary,
    workspace_id: UUID,
) -> TrustedPersistenceContext:
    return authority.interactive(
        scope=WorkspaceScope(workspace_id=workspace_id),
        acting_subject_id=ACTOR_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )


@pytest.mark.asyncio
async def test_ordinary_uow_cannot_create_initial_workspace() -> None:
    authority = boundary()
    persistence = InMemoryPersistence(context_verifier=authority)
    uow = persistence.uow_factory(context(authority, INITIAL_WORKSPACE_ID))

    with pytest.raises(InvalidBootstrapAuthorityError, match="not authorized"):
        async with uow:
            await uow.workspaces.add(workspace())

    with pytest.raises(UnitOfWorkLifecycleError, match="not active"):
        await uow.commit()


@pytest.mark.asyncio
async def test_authorized_bootstrap_establishes_exactly_one_initial_workspace() -> None:
    context_authority = boundary()
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=context_authority,
        bootstrap_authority=bootstrap_authority,
    )
    expected = workspace()

    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority, expected
    )

    async with persistence.uow_factory(context(context_authority, expected.id)) as uow:
        assert await uow.workspaces.resolve(expected.id) == expected

    with pytest.raises(ConstraintConflictError, match="sealed"):
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            bootstrap_authority, workspace(OTHER_WORKSPACE_ID)
        )


@pytest.mark.asyncio
async def test_bootstrap_rejects_authority_from_another_composition_root() -> None:
    context_authority = boundary()
    expected_authority = create_initial_workspace_bootstrap_authority()
    caller_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=context_authority,
        bootstrap_authority=expected_authority,
    )

    with pytest.raises(InvalidBootstrapAuthorityError, match="not authorized"):
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            caller_authority, workspace()
        )


@pytest.mark.asyncio
async def test_unconfigured_bootstrap_rejects_missing_authority() -> None:
    persistence = InMemoryPersistence(context_verifier=boundary())

    with pytest.raises(InvalidBootstrapAuthorityError, match="not authorized"):
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            None, workspace()  # type: ignore[arg-type]
        )


def test_bootstrap_authority_cannot_be_used_as_tenant_context() -> None:
    persistence = InMemoryPersistence(context_verifier=boundary())
    bootstrap_authority = create_initial_workspace_bootstrap_authority()

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        persistence.uow_factory(bootstrap_authority)  # type: ignore[arg-type]


def test_ordinary_uow_exposes_no_bootstrap_or_cross_tenant_repository() -> None:
    authority = boundary()
    persistence = InMemoryPersistence(context_verifier=authority)
    uow = persistence.uow_factory(context(authority, INITIAL_WORKSPACE_ID))

    assert not hasattr(uow, "bootstrap")
    assert not hasattr(uow, "all_workspaces")
    assert not hasattr(uow.context, "bootstrap_authority")
