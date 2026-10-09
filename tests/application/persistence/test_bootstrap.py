from __future__ import annotations

from uuid import uuid4

import pytest

from spine.application.persistence.bootstrap import (
    issue_initial_workspace_bootstrap_authority,
)
from spine.application.persistence.context import WorkspaceScope, issue_trusted_context_authority
from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidBootstrapAuthorityError,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


def workspace() -> Workspace:
    return Workspace(id=uuid4(), slug="initial", display_name="Initial Workspace")


@pytest.mark.asyncio
async def test_authorized_bootstrap_establishes_exactly_one_initial_workspace() -> None:
    authority = issue_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(bootstrap_authority=authority)
    expected = workspace()

    await persistence.initial_workspace_bootstrap.create_initial_workspace(authority, expected)

    issuer = issue_trusted_context_authority()
    context = issuer.worker(
        scope=WorkspaceScope(workspace_id=expected.id),
        service_principal_id=uuid4(),
        purpose="bootstrap_verification",
        operation="resolve_workspace",
        trace_id=uuid4(),
    )
    async with persistence.uow_factory(context) as uow:
        assert await uow.workspaces.resolve(expected.id) == expected

    with pytest.raises(ConstraintConflictError, match="sealed"):
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            authority, workspace()
        )


@pytest.mark.asyncio
async def test_bootstrap_rejects_authority_from_another_composition_root() -> None:
    expected_authority = issue_initial_workspace_bootstrap_authority()
    caller_authority = issue_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(bootstrap_authority=expected_authority)

    with pytest.raises(InvalidBootstrapAuthorityError, match="not authorized"):
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            caller_authority, workspace()
        )


@pytest.mark.asyncio
async def test_unconfigured_bootstrap_rejects_missing_authority() -> None:
    persistence = InMemoryPersistence()

    with pytest.raises(InvalidBootstrapAuthorityError, match="not authorized"):
        await persistence.initial_workspace_bootstrap.create_initial_workspace(
            None, workspace()  # type: ignore[arg-type]
        )


def test_ordinary_uow_exposes_no_bootstrap_or_cross_tenant_repository() -> None:
    persistence = InMemoryPersistence()
    issuer = issue_trusted_context_authority()
    context = issuer.interactive(
        scope=WorkspaceScope(workspace_id=uuid4()),
        acting_subject_id=uuid4(),
        purpose="workspace_admin",
        operation="create_workspace",
        trace_id=uuid4(),
    )
    uow = persistence.uow_factory(context)

    assert not hasattr(uow, "bootstrap")
    assert not hasattr(uow, "all_workspaces")
    assert not hasattr(context, "bootstrap_authority")
