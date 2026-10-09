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


class _SwitchingContext(TrustedPersistenceContext):
    def __getattribute__(self, name: str) -> object:
        switch_field = object.__getattribute__(self, "_switch_field")
        if name == switch_field:
            reads = object.__getattribute__(self, "_switch_reads") + 1
            object.__setattr__(self, "_switch_reads", reads)
            if reads >= object.__getattribute__(self, "_switch_after"):
                return object.__getattribute__(self, "_alternate_value")
        return object.__getattribute__(self, name)


class _CyclingWorkspaceContext(TrustedPersistenceContext):
    def __getattribute__(self, name: str) -> object:
        if name == "scope":
            reads = object.__getattribute__(self, "_scope_reads") + 1
            object.__setattr__(self, "_scope_reads", reads)
            if reads % 5 == 0:
                return object.__getattribute__(self, "_alternate_scope")
        return object.__getattribute__(self, name)


class _SwitchingWorkspaceScope(WorkspaceScope):
    def __getattribute__(self, name: str) -> object:
        if name == "workspace_id":
            try:
                reads = object.__getattribute__(self, "_workspace_reads") + 1
            except AttributeError:
                return object.__getattribute__(self, name)
            object.__setattr__(self, "_workspace_reads", reads)
            if reads >= 3:
                return object.__getattribute__(self, "_alternate_workspace_id")
        return object.__getattribute__(self, name)


class _SwitchingEnvironmentScope(EnvironmentScope):
    def __getattribute__(self, name: str) -> object:
        if name == "environment_id":
            try:
                reads = object.__getattribute__(self, "_environment_reads") + 1
            except AttributeError:
                return object.__getattribute__(self, name)
            object.__setattr__(self, "_environment_reads", reads)
            if reads >= 3:
                return object.__getattribute__(self, "_alternate_environment_id")
        return object.__getattribute__(self, name)


def _switch_context_field(
    source: TrustedPersistenceContext,
    *,
    field_name: str,
    switch_after: int,
    alternate_value: object,
) -> TrustedPersistenceContext:
    candidate = _SwitchingContext(
        scope=source.scope,
        origin=source.origin,
        purpose=source.purpose,
        operation=source.operation,
        trace_id=source.trace_id,
        acting_subject_id=source.acting_subject_id,
        service_principal_id=source.service_principal_id,
        provenance=source.provenance,
    )
    object.__setattr__(candidate, "_switch_field", field_name)
    object.__setattr__(candidate, "_switch_after", switch_after)
    object.__setattr__(candidate, "_alternate_value", alternate_value)
    object.__setattr__(candidate, "_switch_reads", 0)
    return candidate


@pytest.mark.parametrize(
    ("environment_scoped", "field_name", "switch_after", "alternate_value", "observed"),
    [
        (
            False,
            "scope",
            5,
            WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
            lambda value: value.scope.workspace_id,
        ),
        (
            True,
            "scope",
            6,
            EnvironmentScope(
                workspace_id=WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
            ),
            lambda value: value.scope.environment_id,
        ),
        (
            False,
            "acting_subject_id",
            4,
            OTHER_ACTING_SUBJECT_ID,
            lambda value: value.acting_subject_id,
        ),
        (
            False,
            "service_principal_id",
            5,
            OTHER_SERVICE_PRINCIPAL_ID,
            lambda value: value.service_principal_id,
        ),
        (
            False,
            "purpose",
            4,
            PersistencePurpose("other_purpose"),
            lambda value: value.purpose,
        ),
        (
            False,
            "operation",
            4,
            PersistenceOperation("other_operation"),
            lambda value: value.operation,
        ),
        (
            False,
            "origin",
            3,
            ContextOrigin.WORKER,
            lambda value: value.origin,
        ),
        (
            False,
            "trace_id",
            3,
            OTHER_TRACE_ID,
            lambda value: value.trace_id,
        ),
        (
            False,
            "provenance",
            8,
            TrustedContextProvenance(
                issuer_id=OTHER_ISSUER_ID,
                signature=b"alternate-untrusted-signature",
            ),
            lambda value: value.provenance.issuer_id,
        ),
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
def test_stateful_context_cannot_substitute_unsigned_authority(
    environment_scoped: bool,
    field_name: str,
    switch_after: int,
    alternate_value: object,
    observed: Callable[[TrustedPersistenceContext], object],
) -> None:
    authority = boundary()
    source = authority.interactive(
        scope=(
            EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID)
            if environment_scoped
            else WorkspaceScope(workspace_id=WORKSPACE_ID)
        ),
        acting_subject_id=ACTING_SUBJECT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )
    expected = observed(source)
    candidate = _switch_context_field(
        source,
        field_name=field_name,
        switch_after=switch_after,
        alternate_value=alternate_value,
    )

    try:
        snapshot = authority.verify(candidate)
    except InvalidPersistenceContextError:
        return

    assert observed(snapshot) == expected


@pytest.mark.parametrize("scope_kind", ["workspace", "environment"])
def test_stateful_nested_scope_cannot_substitute_unsigned_identity(scope_kind: str) -> None:
    authority = boundary()
    issued_scope: WorkspaceScope | EnvironmentScope
    if scope_kind == "workspace":
        issued_scope = WorkspaceScope(workspace_id=WORKSPACE_ID)
        scope = _SwitchingWorkspaceScope(workspace_id=WORKSPACE_ID)
        object.__setattr__(scope, "_workspace_reads", 0)
        object.__setattr__(scope, "_alternate_workspace_id", OTHER_WORKSPACE_ID)
        expected = WORKSPACE_ID
        observed = lambda value: value.scope.workspace_id
    else:
        issued_scope = EnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
        scope = _SwitchingEnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
        object.__setattr__(scope, "_environment_reads", 0)
        object.__setattr__(scope, "_alternate_environment_id", OTHER_ENVIRONMENT_ID)
        expected = ENVIRONMENT_ID
        observed = lambda value: value.scope.environment_id
    issued = authority.interactive(
        scope=issued_scope,
        acting_subject_id=ACTING_SUBJECT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )
    source = TrustedPersistenceContext(
        scope=scope,
        origin=issued.origin,
        purpose=issued.purpose,
        operation=issued.operation,
        trace_id=issued.trace_id,
        acting_subject_id=issued.acting_subject_id,
        service_principal_id=issued.service_principal_id,
        provenance=issued.provenance,
    )

    try:
        snapshot = authority.verify(source)
    except InvalidPersistenceContextError:
        return

    assert observed(snapshot) == expected


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


@pytest.mark.asyncio
async def test_uow_entry_rejects_different_valid_authority_from_same_boundary() -> None:
    authority = boundary()
    source = authority.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTING_SUBJECT_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )
    replacement = authority.interactive(
        scope=WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
        acting_subject_id=ACTING_SUBJECT_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )
    persistence = InMemoryPersistence(context_verifier=authority)
    uow = persistence.uow_factory(source)
    object.__setattr__(source, "scope", replacement.scope)
    object.__setattr__(source, "provenance", replacement.provenance)

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


@pytest.mark.asyncio
async def test_stateful_context_cannot_escalate_uow_authority() -> None:
    authority = boundary()
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=authority,
        bootstrap_authority=bootstrap_authority,
    )
    expected = Workspace(id=WORKSPACE_ID, slug="northwind", display_name="Northwind")
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        expected,
    )
    source = authority.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTING_SUBJECT_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )
    candidate = _CyclingWorkspaceContext(
        scope=source.scope,
        origin=source.origin,
        purpose=source.purpose,
        operation=source.operation,
        trace_id=source.trace_id,
        acting_subject_id=source.acting_subject_id,
        service_principal_id=source.service_principal_id,
        provenance=source.provenance,
    )
    object.__setattr__(candidate, "_scope_reads", 0)
    object.__setattr__(
        candidate,
        "_alternate_scope",
        WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID),
    )

    try:
        async with persistence.uow_factory(candidate) as uow:
            assert uow.scope.workspace_id == WORKSPACE_ID
            assert await uow.workspaces.resolve(WORKSPACE_ID) == expected
    except InvalidPersistenceContextError:
        return


@pytest.mark.asyncio
async def test_source_mutation_after_uow_acceptance_does_not_change_authority() -> None:
    authority = boundary()
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=authority,
        bootstrap_authority=bootstrap_authority,
    )
    expected = Workspace(id=WORKSPACE_ID, slug="northwind", display_name="Northwind")
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        expected,
    )
    source = authority.interactive(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        acting_subject_id=ACTING_SUBJECT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PURPOSE,
        operation=OPERATION,
        trace_id=TRACE_ID,
    )

    async with persistence.uow_factory(source) as uow:
        _mutate_workspace_scope(source)
        _mutate_acting_subject(source)
        _mutate_service_principal(source)
        _mutate_purpose(source)
        _mutate_operation(source)
        _mutate_origin(source)
        _mutate_trace(source)
        _mutate_issuer(source)

        assert uow.scope.workspace_id == WORKSPACE_ID
        assert await uow.workspaces.resolve(WORKSPACE_ID) == expected


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
