from __future__ import annotations

import asyncio
from collections.abc import Callable
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
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.persistence.contexts import create_initial_workspace_bootstrap_authority
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
OTHER_ENVIRONMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
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


def test_verified_context_is_a_complete_detached_snapshot() -> None:
    source = interactive_context()

    snapshot = boundary().verify(source)

    assert snapshot == source
    assert snapshot is not source
    assert snapshot.scope is not source.scope
    assert snapshot.purpose is not source.purpose
    assert snapshot.operation is not source.operation
    assert snapshot.provenance is not source.provenance


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


class _EntrySignalingLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.acquire_started = asyncio.Event()
        self._armed = False

    def arm(self) -> None:
        self.acquire_started.clear()
        self._armed = True

    async def acquire(self) -> bool:
        if self._armed:
            self.acquire_started.set()
        return await super().acquire()


def _mutate_workspace_scope(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context.scope, "workspace_id", OTHER_WORKSPACE_ID)


def _mutate_environment_scope(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context.scope, "environment_id", OTHER_ENVIRONMENT_ID)


def _mutate_acting_subject(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context, "acting_subject_id", OTHER_ACTING_SUBJECT_ID)


def _mutate_service_principal(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context, "service_principal_id", OTHER_SERVICE_PRINCIPAL_ID)


def _mutate_purpose(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context.purpose, "value", "other_purpose")


def _mutate_operation(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context.operation, "value", "other_operation")


def _mutate_origin(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context, "origin", ContextOrigin.WORKER)


def _mutate_trace(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context, "trace_id", OTHER_TRACE_ID)


def _mutate_issuer(context: TrustedPersistenceContext) -> None:
    object.__setattr__(context.provenance, "issuer_id", OTHER_ISSUER_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_environment_scope", "mutate"),
    [
        (False, _mutate_workspace_scope),
        (True, _mutate_environment_scope),
        (False, _mutate_acting_subject),
        (False, _mutate_service_principal),
        (False, _mutate_purpose),
        (False, _mutate_operation),
        (False, _mutate_origin),
        (False, _mutate_trace),
        (False, _mutate_issuer),
    ],
    ids=(
        "workspace",
        "environment",
        "acting-subject",
        "service-principal",
        "purpose",
        "operation",
        "origin",
        "trace",
        "issuer",
    ),
)
async def test_context_mutated_while_entry_waits_never_gains_authority(
    use_environment_scope: bool,
    mutate: Callable[[TrustedPersistenceContext], None],
) -> None:
    transaction_lock = _EntrySignalingLock()
    authority = boundary()
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=authority,
        bootstrap_authority=bootstrap_authority,
        transaction_lock=transaction_lock,
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(id=WORKSPACE_ID, slug="northwind", display_name="Northwind"),
    )
    setup_context = authority.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTING_SUBJECT_ID,
        purpose=PersistencePurpose("test_setup"),
        operation=PersistenceOperation("create_environment"),
        trace_id=TRACE_ID,
    )
    async with persistence.uow_factory(setup_context) as setup_uow:
        for environment_id in (ENVIRONMENT_ID, OTHER_ENVIRONMENT_ID):
            await setup_uow.environments.add(
                Environment(
                    id=environment_id,
                    workspace_id=WORKSPACE_ID,
                    kind=EnvironmentKind.PRODUCTION,
                    display_name="Production",
                )
            )
        await setup_uow.commit()

    scope = (
        EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID)
        if use_environment_scope
        else WorkspaceScope(workspace_id=WORKSPACE_ID)
    )
    source_context = authority.interactive(
        scope=scope,
        acting_subject_id=ACTING_SUBJECT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )
    uow = persistence.uow_factory(source_context)

    await transaction_lock.acquire()
    transaction_lock.arm()

    async def enter_and_close() -> None:
        async with uow:
            pytest.fail("mutated context became repository-capable")

    entry_task = asyncio.create_task(enter_and_close())
    try:
        await transaction_lock.acquire_started.wait()
        mutate(source_context)
    finally:
        transaction_lock.release()

    with pytest.raises(InvalidPersistenceContextError, match="Persistence context is invalid"):
        await entry_task


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
