from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest

import spine.application.persistence as persistence_contracts
from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import InvalidPersistenceContextError
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.in_memory import InMemoryPersistence


WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000001")
ACTING_SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000001")
OTHER_ACTING_SUBJECT_ID = UUID("30000000-0000-0000-0000-000000000002")
SERVICE_PRINCIPAL_ID = UUID("40000000-0000-0000-0000-000000000001")
OTHER_SERVICE_PRINCIPAL_ID = UUID("40000000-0000-0000-0000-000000000002")
TRACE_ID = UUID("50000000-0000-0000-0000-000000000001")
OTHER_TRACE_ID = UUID("50000000-0000-0000-0000-000000000002")
ISSUER_ID = UUID("60000000-0000-0000-0000-000000000001")
OTHER_ISSUER_ID = UUID("60000000-0000-0000-0000-000000000002")
BOUNDARY_SECRET = b"issue-40-test-boundary-secret-0001"
OTHER_BOUNDARY_SECRET = b"issue-40-test-boundary-secret-0002"
PURPOSE = PersistencePurpose("workspace_admin")
OPERATION = PersistenceOperation("resolve_workspace")


def boundary() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(issuer_id=ISSUER_ID, secret=BOUNDARY_SECRET)


def interactive_context() -> TrustedPersistenceContext:
    return boundary().interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTING_SUBJECT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )


def test_workspace_scope_rejects_malformed_identity() -> None:
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        WorkspaceScope(workspace_id="caller-selected")  # type: ignore[arg-type]


def test_environment_scope_rejects_nil_identity() -> None:
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=UUID(int=0))


def test_policy_identifier_rejects_unbounded_or_malformed_value() -> None:
    for value in ("", "Caller Selected", "a" * 65):
        with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
            PersistencePurpose(value)


def test_scope_and_context_are_immutable() -> None:
    scope = WorkspaceScope(workspace_id=WORKSPACE_ID)
    context = interactive_context()

    with pytest.raises(FrozenInstanceError):
        scope.workspace_id = OTHER_WORKSPACE_ID  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        context.purpose = PersistencePurpose("forged")  # type: ignore[misc]


def test_public_fields_cannot_construct_verified_context() -> None:
    forged = TrustedPersistenceContext(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
        acting_subject_id=ACTING_SUBJECT_ID,
        service_principal_id=None,
        provenance=TrustedContextProvenance(issuer_id=ISSUER_ID, signature=b"caller"),
    )

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        boundary().verify(forged)


def test_application_contract_exposes_no_unrestricted_authority_issuer() -> None:
    assert not hasattr(persistence_contracts, "issue_trusted_context_authority")
    assert not hasattr(persistence_contracts, "issue_initial_workspace_bootstrap_authority")


def test_constructed_context_cannot_open_uow() -> None:
    forged = object.__new__(TrustedPersistenceContext)
    object.__setattr__(forged, "scope", WorkspaceScope(workspace_id=WORKSPACE_ID))
    persistence = InMemoryPersistence(context_verifier=boundary())

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        persistence.uow_factory(forged)


def test_post_construction_scope_mutation_invalidates_context() -> None:
    context = interactive_context()
    object.__setattr__(context.scope, "workspace_id", OTHER_WORKSPACE_ID)

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        boundary().verify(context)


def test_context_from_another_authority_boundary_is_rejected() -> None:
    other = TrustedContextBoundary.for_testing(
        issuer_id=OTHER_ISSUER_ID,
        secret=OTHER_BOUNDARY_SECRET,
    )
    context = other.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTING_SUBJECT_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        boundary().verify(context)

    persistence = InMemoryPersistence(context_verifier=boundary())
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        persistence.uow_factory(context)


def test_replaced_context_does_not_retain_authority() -> None:
    context = replace(
        interactive_context(),
        scope=WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
    )

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        boundary().verify(context)


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)},
        {"acting_subject_id": OTHER_ACTING_SUBJECT_ID},
        {"service_principal_id": OTHER_SERVICE_PRINCIPAL_ID},
        {"purpose": PersistencePurpose("other_purpose")},
        {"operation": PersistenceOperation("other_operation")},
        {"trace_id": OTHER_TRACE_ID},
        {
            "provenance": TrustedContextProvenance(
                issuer_id=OTHER_ISSUER_ID,
                signature=b"recognized-shape-but-untrusted",
            )
        },
    ],
)
def test_provenance_binds_every_authority_field(changes: dict[str, object]) -> None:
    context = replace(interactive_context(), **changes)

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        boundary().verify(context)


@pytest.mark.asyncio
async def test_context_mutated_after_uow_creation_is_rejected_on_entry() -> None:
    context = interactive_context()
    persistence = InMemoryPersistence(context_verifier=boundary())
    uow = persistence.uow_factory(context)
    object.__setattr__(context.scope, "workspace_id", OTHER_WORKSPACE_ID)

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        await uow.__aenter__()


def test_interactive_context_requires_human_identity() -> None:
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        boundary().interactive(
            scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
            acting_subject_id=None,
            purpose=PURPOSE,
            operation=OPERATION,
            trace_id=TRACE_ID,
        )


def test_worker_context_requires_service_identity() -> None:
    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        boundary().worker(
            scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
            service_principal_id=None,
            purpose=PersistencePurpose("workspace_sync"),
            operation=OPERATION,
            trace_id=TRACE_ID,
        )
