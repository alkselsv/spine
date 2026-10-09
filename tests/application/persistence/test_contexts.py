from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest

from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    TrustedPersistenceContext,
    WorkspaceScope,
    issue_trusted_context_authority,
)
from spine.application.persistence.errors import InvalidPersistenceContextError


def test_workspace_scope_rejects_malformed_identity() -> None:
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        WorkspaceScope(workspace_id="caller-selected")  # type: ignore[arg-type]


def test_environment_scope_rejects_nil_identity() -> None:
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        EnvironmentScope(workspace_id=uuid4(), environment_id=UUID(int=0))


def test_scope_is_immutable() -> None:
    scope = WorkspaceScope(workspace_id=uuid4())

    with pytest.raises(FrozenInstanceError):
        scope.workspace_id = uuid4()  # type: ignore[misc]


def test_client_fields_cannot_construct_trusted_context() -> None:
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        TrustedPersistenceContext(
            scope=WorkspaceScope(workspace_id=uuid4()),
            origin=ContextOrigin.INTERACTIVE,
            acting_subject_id=uuid4(),
            purpose="caller_selected",
            operation="caller_selected",
            trace_id=uuid4(),
        )


def test_trusted_context_is_immutable() -> None:
    issuer = issue_trusted_context_authority()
    context = issuer.interactive(
        scope=WorkspaceScope(workspace_id=uuid4()),
        acting_subject_id=uuid4(),
        purpose="workspace_admin",
        operation="resolve_workspace",
        trace_id=uuid4(),
    )

    with pytest.raises(FrozenInstanceError):
        context.purpose = "forged"  # type: ignore[misc]

    with pytest.raises(TypeError, match="dataclass instance"):
        replace(context, scope=WorkspaceScope(workspace_id=uuid4()))


def test_interactive_context_requires_human_identity() -> None:
    issuer = issue_trusted_context_authority()

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        issuer.interactive(
            scope=WorkspaceScope(workspace_id=uuid4()),
            acting_subject_id=None,
            purpose="workspace_admin",
            operation="resolve_workspace",
            trace_id=uuid4(),
        )

def test_worker_context_requires_service_identity() -> None:
    issuer = issue_trusted_context_authority()

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        issuer.worker(
            scope=WorkspaceScope(workspace_id=uuid4()),
            service_principal_id=None,
            purpose="workspace_sync",
            operation="resolve_workspace",
            trace_id=uuid4(),
        )
