from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import SecretStr

from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticFailureCode,
    RequiredAuditCoordinator,
)
from spine.application.diagnostics.audit import RequiredAuditUnitOfWorkFactory
from spine.application.persistence import (
    EnvironmentScope,
    InvalidPersistenceContextError,
    OutboxEventRegistry,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.auth import (
    AccessTokenCredential,
    AuthenticatedAlias,
    AuthenticationAlias,
    AuthenticationUnavailableError,
    AuthorizationDeniedError,
    AuthorizationResolution,
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizationUnavailableError,
    InMemoryAuthorizationDirectory,
    InMemoryAuthorizationEntry,
    InvalidAuthenticationError,
    RequestedAuthorizationScope,
    RouteAuthorizationPolicy,
    RoutePolicyRegistry,
)
from spine.auth.in_memory import AuthorizationRecordState
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.auth import (
    RequestAuthorizationComposer,
    TrustedRequestContextBoundary,
)
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import (
    TrustedContextBoundary,
    create_initial_workspace_bootstrap_authority,
)
from spine.infrastructure.persistence.in_memory import InMemoryPersistence

from ...contracts.diagnostics.fixtures import DIAGNOSTIC_LEAK_CORPUS


IDENTITY_ID = UUID("10000000-0000-0000-0000-000000000001")
ANCHOR_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000000")
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
OTHER_WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000002")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
OTHER_ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000002")
SERVICE_PRINCIPAL_ID = UUID("40000000-0000-0000-0000-000000000001")
OTHER_SERVICE_PRINCIPAL_ID = UUID("40000000-0000-0000-0000-000000000002")
TRACE_ID = UUID("50000000-0000-0000-0000-000000000001")
OTHER_TRACE_ID = UUID("50000000-0000-0000-0000-000000000002")
CORRELATION_ID = UUID("51000000-0000-0000-0000-000000000001")
CAUSATION_ID = UUID("52000000-0000-0000-0000-000000000001")
PERSISTENCE_ISSUER_ID = UUID("60000000-0000-0000-0000-000000000001")
REQUEST_ISSUER_ID = UUID("61000000-0000-0000-0000-000000000001")
REQUEST_ID_1 = UUID("70000000-0000-0000-0000-000000000001")
REQUEST_ID_2 = UUID("70000000-0000-0000-0000-000000000002")
AUDIT_EVENT_ID_1 = UUID("80000000-0000-0000-0000-000000000001")
AUDIT_EVENT_ID_2 = UUID("80000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc)


class StubAuthentication:
    def __init__(self, result: AuthenticatedAlias | Exception) -> None:
        self.result = result
        self.contexts: list[DiagnosticContext] = []

    async def authenticate(
        self,
        credentials: AccessTokenCredential,
        diagnostic_context: DiagnosticContext,
    ) -> AuthenticatedAlias:
        assert credentials.access_token.get_secret_value()
        self.contexts.append(diagnostic_context)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FixedResolutionDirectory:
    def __init__(self, resolution: AuthorizationResolution) -> None:
        self.resolution = resolution

    async def resolve_request_authority(
        self,
        authenticated_alias: AuthenticatedAlias,
        requested_scope: RequestedAuthorizationScope,
        route_policy: RouteAuthorizationPolicy,
    ) -> AuthorizationResolution:
        del authenticated_alias, requested_scope, route_policy
        return self.resolution

    def replace_resolution(self, resolution: AuthorizationResolution) -> None:
        self.resolution = resolution


class RaisingDirectory:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def resolve_request_authority(
        self,
        authenticated_alias: AuthenticatedAlias,
        requested_scope: RequestedAuthorizationScope,
        route_policy: RouteAuthorizationPolicy,
    ) -> AuthorizationResolution:
        del authenticated_alias, requested_scope, route_policy
        raise self.failure


class RecordingAuditWriter:
    def __init__(self, order: list[str]) -> None:
        self.events: list[AuditEvent] = []
        self._order = order

    async def append(self, event: AuditEvent) -> UUID:
        self.events.append(event)
        self._order.append("audit.append")
        return AUDIT_EVENT_ID_1


class RecordingAuditUnitOfWork:
    def __init__(self, writer: RecordingAuditWriter, order: list[str]) -> None:
        self.audit = writer
        self._order = order

    async def __aenter__(self) -> RecordingAuditUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback

    async def commit(self) -> None:
        self._order.append("audit.commit")


class RecordingAuditUnitOfWorkFactory:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.writer = RecordingAuditWriter(self.order)
        self.contexts: list[TrustedPersistenceContext] = []

    def __call__(
        self,
        context: TrustedPersistenceContext,
    ) -> RecordingAuditUnitOfWork:
        self.contexts.append(context)
        return RecordingAuditUnitOfWork(self.writer, self.order)


def uuid_source(values: tuple[UUID, ...]) -> Iterator[UUID]:
    return iter(values)


def alias(configuration_version: str = "oidc-2026-10-10") -> AuthenticatedAlias:
    return AuthenticatedAlias(
        alias=AuthenticationAlias(
            issuer="https://identity.example.test",
            subject="synthetic-subject",
        ),
        authentication_configuration_version=configuration_version,
    )


def scope(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
) -> RequestedAuthorizationScope:
    return RequestedAuthorizationScope(
        workspace_id=workspace_id,
        environment_id=environment_id,
    )


def policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("list_sources"),
        service_principal_id=SERVICE_PRINCIPAL_ID,
    )


def directory_entry(**overrides: object) -> InMemoryAuthorizationEntry:
    values: dict[str, object] = {
        "alias": alias().alias,
        "canonical_human_identity_id": IDENTITY_ID,
        "identity_state": AuthorizationRecordState.ACTIVE,
        "alias_state": AuthorizationRecordState.ACTIVE,
        "workspace_id": WORKSPACE_ID,
        "workspace_membership_state": AuthorizationRecordState.ACTIVE,
        "environment_id": ENVIRONMENT_ID,
        "environment_workspace_id": WORKSPACE_ID,
        "environment_membership_state": AuthorizationRecordState.ACTIVE,
        "roles": frozenset({AuthorizationRole.ADMINISTRATOR}),
        "role_binding_state": AuthorizationRecordState.ACTIVE,
        "snapshot_generation": 7,
        "current_generation": 7,
    }
    values.update(overrides)
    return InMemoryAuthorizationEntry.model_validate(values)


def diagnostic_context() -> DiagnosticContext:
    return DiagnosticContext(
        trace_id=TRACE_ID,
        correlation_id=CORRELATION_ID,
        causation_id=CAUSATION_ID,
    )


def credentials(secret: str = "synthetic-access-token") -> AccessTokenCredential:
    return AccessTokenCredential(access_token=SecretStr(secret))


async def composition(
    *,
    authentication_result: AuthenticatedAlias | Exception | None = None,
    directory: object | None = None,
    request_ids: tuple[UUID, ...] = (REQUEST_ID_1, REQUEST_ID_2),
    audit_ids: tuple[UUID, ...] = (AUDIT_EVENT_ID_1, AUDIT_EVENT_ID_2),
    audit_uow_factory: RequiredAuditUnitOfWorkFactory | None = None,
) -> tuple[
    RequestAuthorizationComposer,
    InMemoryPersistence,
    TrustedRequestContextBoundary,
    RecordingDiagnosticSink,
]:
    persistence_boundary = TrustedContextBoundary.for_testing(
        issuer_id=PERSISTENCE_ISSUER_ID,
        secret=b"ticket-72-persistence-secret-000001",
    )
    request_boundary = TrustedRequestContextBoundary.for_testing(
        persistence_boundary=persistence_boundary,
        issuer_id=REQUEST_ISSUER_ID,
        secret=b"ticket-72-request-secret-000000001",
    )
    audit_registry = AuditEventRegistry.with_default_families()
    audit_id_iterator = uuid_source(audit_ids)
    bootstrap_authority = create_initial_workspace_bootstrap_authority()
    persistence = InMemoryPersistence(
        context_verifier=request_boundary,
        outbox_events=OutboxEventRegistry(),
        audit_events=audit_registry,
        bootstrap_authority=bootstrap_authority,
        audit_clock=lambda: NOW,
        audit_event_id_factory=lambda: next(audit_id_iterator),
    )
    await persistence.initial_workspace_bootstrap.create_initial_workspace(
        bootstrap_authority,
        Workspace(
            id=ANCHOR_WORKSPACE_ID,
            slug="anchor",
            display_name="Anchor",
        ),
    )
    for workspace, environment in (
        (
            Workspace(
                id=WORKSPACE_ID,
                slug="synthetic-one",
                display_name="Synthetic One",
            ),
            Environment(
                id=ENVIRONMENT_ID,
                workspace_id=WORKSPACE_ID,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production One",
            ),
        ),
        (
            Workspace(
                id=OTHER_WORKSPACE_ID,
                slug="synthetic-two",
                display_name="Synthetic Two",
            ),
            Environment(
                id=OTHER_ENVIRONMENT_ID,
                workspace_id=OTHER_WORKSPACE_ID,
                kind=EnvironmentKind.PRODUCTION,
                display_name="Production Two",
            )
        ),
    ):
        setup_context = persistence_boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace.id),
            service_principal_id=SERVICE_PRINCIPAL_ID,
            purpose=PersistencePurpose("test_setup"),
            operation=PersistenceOperation("create_workspace"),
            trace_id=TRACE_ID,
        )
        async with persistence.uow_factory(setup_context) as uow:
            await uow.workspaces.add(workspace)
            await uow.commit()
        environment_context = persistence_boundary.worker(
            scope=WorkspaceScope(workspace_id=workspace.id),
            service_principal_id=SERVICE_PRINCIPAL_ID,
            purpose=PersistencePurpose("test_setup"),
            operation=PersistenceOperation("create_environment"),
            trace_id=TRACE_ID,
        )
        async with persistence.uow_factory(environment_context) as uow:
            await uow.environments.add(environment)
            await uow.commit()

    route_policies = RoutePolicyRegistry()
    route_policies.register("knowledge.sources.list", policy())
    diagnostic_registry = DiagnosticEventRegistry.with_default_families()
    diagnostic_sink = RecordingDiagnosticSink(diagnostic_registry)
    request_id_iterator = uuid_source(request_ids)
    composer = RequestAuthorizationComposer(
        authentication=StubAuthentication(authentication_result or alias()),
        authorization_directory=(
            directory
            if directory is not None
            else InMemoryAuthorizationDirectory(entries=(directory_entry(),))
        ),
        route_policies=route_policies,
        request_boundary=request_boundary,
        audit_registry=audit_registry,
        audit_gate=RequiredAuditCoordinator(
            persistence.uow_factory
            if audit_uow_factory is None
            else audit_uow_factory
        ),
        diagnostic_registry=diagnostic_registry,
        diagnostic_sink=diagnostic_sink,
        clock=lambda: NOW,
        request_id_source=lambda: next(request_id_iterator),
    )
    return composer, persistence, request_boundary, diagnostic_sink


@pytest.mark.asyncio
async def test_allowed_request_maps_one_exact_request_and_persistence_snapshot() -> None:
    composer, _, _, _ = await composition()
    route = composer.bind_route("knowledge.sources.list")

    async with route.authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        request = authorized.request_context
        persistence_context = authorized.persistence_context

        assert request.acting_subject_id == IDENTITY_ID
        assert request.scope == scope()
        assert request.roles == frozenset({AuthorizationRole.ADMINISTRATOR})
        assert request.authorization_generation == 7
        assert request.authentication_configuration_version == "oidc-2026-10-10"
        assert request.route_policy == policy()
        assert request.diagnostic_context == diagnostic_context()
        assert persistence_context.scope == EnvironmentScope(
            workspace_id=WORKSPACE_ID,
            environment_id=ENVIRONMENT_ID,
        )
        assert persistence_context.acting_subject_id == IDENTITY_ID
        assert persistence_context.service_principal_id == SERVICE_PRINCIPAL_ID
        assert persistence_context.purpose == policy().purpose
        assert persistence_context.operation == policy().operation
        assert persistence_context.trace_id == TRACE_ID

@pytest.mark.asyncio
async def test_allowed_request_commits_matching_audit_before_release() -> None:
    composer, persistence, _, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        event = await persistence.audit_reader.resolve(
            authorized.persistence_context,
            authorized.authorization_audit_event_id,
        )
        assert event is not None
        assert event.schema_version == 2
        assert event.outcome.value == "allowed"
        assert event.workspace_id == WORKSPACE_ID
        assert event.environment_id == ENVIRONMENT_ID
        assert event.acting_subject_id == IDENTITY_ID
        assert event.service_principal_id == SERVICE_PRINCIPAL_ID
        assert event.trace_id == TRACE_ID
        assert event.correlation_id == CORRELATION_ID
        assert event.causation_id == CAUSATION_ID
        assert event.payload_json() == {
            "authentication_configuration_version": "oidc-2026-10-10",
            "authorization_generation": 7,
            "operation": "list_sources",
            "purpose": "knowledge_control",
        }


@pytest.mark.asyncio
async def test_recording_audit_writer_commits_allow_before_handler_release() -> None:
    recording = RecordingAuditUnitOfWorkFactory()
    composer, _, _, _ = await composition(audit_uow_factory=recording)

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        recording.order.append("handler.release")
        assert recording.contexts == [authorized.persistence_context]
        assert authorized.authorization_audit_event_id == AUDIT_EVENT_ID_1

    assert recording.order == ["audit.append", "audit.commit", "handler.release"]
    assert len(recording.writer.events) == 1
    event = recording.writer.events[0]
    assert event.workspace_id == WORKSPACE_ID
    assert event.environment_id == ENVIRONMENT_ID
    assert event.acting_subject_id == IDENTITY_ID
    assert event.trace_id == TRACE_ID


@pytest.mark.asyncio
async def test_request_lifecycle_allows_repository_then_rejects_reuse() -> None:
    composer, persistence, _, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        persistence_context = authorized.persistence_context
        async with persistence.uow_factory(persistence_context) as uow:
            resolved = await uow.environments.resolve(ENVIRONMENT_ID)
        assert resolved is not None

    with pytest.raises(InvalidPersistenceContextError):
        persistence.uow_factory(persistence_context)
    with pytest.raises(InvalidPersistenceContextError):
        _ = authorized.request_context.acting_subject_id


@pytest.mark.asyncio
async def test_allowed_request_context_is_immutable_and_returns_detached_values() -> None:
    composer, _, _, _ = await composition()
    route = composer.bind_route("knowledge.sources.list")

    async with route.authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        request = authorized.request_context
        with pytest.raises(AttributeError):
            request.authorization_generation = 9  # type: ignore[misc]
        with pytest.raises(AttributeError):
            request.authentication_configuration_version = "oidc-other"  # type: ignore[misc]
        returned_scope = request.scope
        object.__setattr__(returned_scope, "workspace_id", OTHER_WORKSPACE_ID)
        returned_policy = request.route_policy
        object.__setattr__(returned_policy.purpose, "value", "caller_selected")
        returned_diagnostics = request.diagnostic_context
        object.__setattr__(returned_diagnostics, "trace_id", OTHER_TRACE_ID)

        assert request.scope == scope()
        assert request.route_policy == policy()
        assert request.diagnostic_context == diagnostic_context()


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_value", DIAGNOSTIC_LEAK_CORPUS)
async def test_sensitive_request_inputs_do_not_cross_context_or_audit_captures(
    sensitive_value: str,
) -> None:
    composer, persistence, _, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(sensitive_value),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        event = await persistence.audit_reader.resolve(
            authorized.persistence_context,
            authorized.authorization_audit_event_id,
        )
        assert event is not None
        captures = "\n".join(
            (
                repr(authorized.request_context),
                repr(authorized.persistence_context),
                event.model_dump_json(),
            )
        )

    assert sensitive_value not in captures


@pytest.mark.asyncio
async def test_untrusted_denial_emits_only_unscoped_safe_diagnostic() -> None:
    denied_directory = InMemoryAuthorizationDirectory(
        entries=(
            directory_entry(
                roles=frozenset(),
                role_binding_state=AuthorizationRecordState.MISSING,
            ),
        )
    )
    composer, _, _, diagnostics = await composition(directory=denied_directory)
    route = composer.bind_route("knowledge.sources.list")

    with pytest.raises(AuthorizationDeniedError, match="Authorization is denied"):
        async with route.authorize(
            credentials=credentials("raw-token-must-not-leak"),
            requested_scope=scope(),
            diagnostic_context=diagnostic_context(),
        ):
            pytest.fail("denied request reached the handler")

    assert len(diagnostics.events) == 1
    event = diagnostics.events[0]
    assert event.workspace_id is None
    assert event.environment_id is None
    assert event.trace_id == TRACE_ID
    assert event.payload.failure_code is DiagnosticFailureCode.AUTHORIZATION_DENIED
    assert "raw-token-must-not-leak" not in event.model_dump_json()


@pytest.mark.asyncio
async def test_two_workspaces_remain_isolated_before_and_inside_persistence() -> None:
    composer, persistence, _, diagnostics = await composition()
    route = composer.bind_route("knowledge.sources.list")

    async with route.authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        async with persistence.uow_factory(authorized.persistence_context) as uow:
            assert await uow.environments.resolve(OTHER_ENVIRONMENT_ID) is None

    with pytest.raises(AuthorizationDeniedError):
        async with route.authorize(
            credentials=credentials(),
            requested_scope=scope(
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
            ),
            diagnostic_context=diagnostic_context(),
        ):
            pytest.fail("foreign Workspace request reached the handler")

    denial = diagnostics.events[0]
    assert denial.workspace_id is None
    assert denial.environment_id is None
    serialized = denial.model_dump_json()
    assert str(OTHER_WORKSPACE_ID) not in serialized
    assert str(OTHER_ENVIRONMENT_ID) not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_value", DIAGNOSTIC_LEAK_CORPUS)
@pytest.mark.parametrize(
    ("failure", "expected_code", "failure_stage"),
    [
        (
            InvalidAuthenticationError(),
            DiagnosticFailureCode.AUTHENTICATION_INVALID,
            "authentication",
        ),
        (
            AuthenticationUnavailableError(),
            DiagnosticFailureCode.AUTHENTICATION_UNAVAILABLE,
            "authentication",
        ),
        (
            AuthorizationDeniedError(),
            DiagnosticFailureCode.AUTHORIZATION_DENIED,
            "authorization",
        ),
        (
            AuthorizationUnavailableError(),
            DiagnosticFailureCode.AUTHORIZATION_UNAVAILABLE,
            "authorization",
        ),
    ],
)
async def test_auth_failures_emit_registered_safe_diagnostics_without_scope(
    failure: Exception,
    expected_code: DiagnosticFailureCode,
    failure_stage: str,
    sensitive_value: str,
) -> None:
    if failure_stage == "authentication":
        composer, _, _, diagnostics = await composition(
            authentication_result=failure
        )
    else:
        composer, _, _, diagnostics = await composition(
            directory=RaisingDirectory(failure),
        )

    with pytest.raises(type(failure)) as captured:
        async with composer.bind_route("knowledge.sources.list").authorize(
            credentials=credentials(sensitive_value),
            requested_scope=scope(),
            diagnostic_context=diagnostic_context(),
        ):
            pytest.fail("failed authorization reached the handler")

    assert diagnostics.events[0].payload.failure_code is expected_code
    captures = repr(captured.value) + diagnostics.events[0].model_dump_json()
    assert sensitive_value not in captures
    assert "synthetic-subject" not in captures
    assert "identity.example.test" not in captures


@pytest.mark.asyncio
@pytest.mark.parametrize("secret_failure", DIAGNOSTIC_LEAK_CORPUS)
async def test_unexpected_auth_failure_is_collapsed_without_raw_exception(
    secret_failure: str,
) -> None:
    composer, _, _, diagnostics = await composition(
        authentication_result=RuntimeError(secret_failure)
    )

    with pytest.raises(RuntimeError) as captured:
        async with composer.bind_route("knowledge.sources.list").authorize(
            credentials=credentials("another-raw-secret"),
            requested_scope=scope(),
            diagnostic_context=diagnostic_context(),
        ):
            pytest.fail("unexpected authentication failure reached the handler")

    assert str(captured.value) == "Request authorization failed."
    assert captured.value.__cause__ is None
    assert diagnostics.events[0].payload.failure_code is (
        DiagnosticFailureCode.INTERNAL_UNEXPECTED
    )
    serialized = diagnostics.events[0].model_dump_json()
    assert secret_failure not in serialized
    assert "another-raw-secret" not in serialized


@pytest.mark.asyncio
async def test_configuration_version_mismatch_is_audited_as_eligible_denial() -> None:
    mismatched = AuthorizationResolution(
        acting_subject_id=IDENTITY_ID,
        scope=scope(),
        roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        authorization_generation=7,
        authentication_configuration_version="oidc-other",
    )
    fixed_directory = FixedResolutionDirectory(mismatched)
    composer, persistence, _, diagnostics = await composition(directory=fixed_directory)

    with pytest.raises(AuthorizationUnavailableError):
        async with composer.bind_route("knowledge.sources.list").authorize(
            credentials=credentials(),
            requested_scope=scope(),
            diagnostic_context=diagnostic_context(),
        ):
            pytest.fail("mismatched snapshot reached the handler")

    assert diagnostics.events == ()

    # The denial is canonical and committed even though no request context escaped.
    # A raw store read is intentionally unavailable; the stable test ID is resolved
    # through the next valid request in the same tenant.
    fixed_directory.replace_resolution(
        AuthorizationResolution(
            acting_subject_id=IDENTITY_ID,
            scope=scope(),
            roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            authorization_generation=7,
            authentication_configuration_version="oidc-2026-10-10",
        )
    )
    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        denied_event = await persistence.audit_reader.resolve(
            authorized.persistence_context,
            AUDIT_EVENT_ID_1,
        )
        assert denied_event is not None
        assert denied_event.outcome.value == "denied"
        assert denied_event.reason == "request_authorization_snapshot_mismatch"
        assert denied_event.payload_json()["authentication_configuration_version"] == (
            "oidc-2026-10-10"
        )


@pytest.mark.asyncio
async def test_required_allow_audit_failure_prevents_handler_execution() -> None:
    composer, persistence, _, _ = await composition()
    persistence.fail_next_audit_append()
    handler_called = False

    with pytest.raises(Exception) as captured:
        async with composer.bind_route("knowledge.sources.list").authorize(
            credentials=credentials(),
            requested_scope=scope(),
            diagnostic_context=diagnostic_context(),
        ):
            handler_called = True

    assert not isinstance(captured.value, AuthorizationDeniedError)
    assert handler_called is False


@pytest.mark.asyncio
async def test_required_deny_audit_failure_never_converts_denial_to_allowance() -> None:
    mismatched = AuthorizationResolution(
        acting_subject_id=IDENTITY_ID,
        scope=scope(),
        roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        authorization_generation=7,
        authentication_configuration_version="oidc-other",
    )
    composer, persistence, _, _ = await composition(
        directory=FixedResolutionDirectory(mismatched),
    )
    persistence.fail_next_audit_append()
    handler_called = False

    with pytest.raises(AuthorizationUnavailableError):
        async with composer.bind_route("knowledge.sources.list").authorize(
            credentials=credentials(),
            requested_scope=scope(),
            diagnostic_context=diagnostic_context(),
        ):
            handler_called = True

    assert handler_called is False


@pytest.mark.asyncio
async def test_request_context_fails_closed_in_another_async_task() -> None:
    composer, persistence, _, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        async def use_in_child_task() -> tuple[
            type[BaseException] | None,
            type[BaseException] | None,
        ]:
            request_failure: type[BaseException] | None = None
            persistence_failure: type[BaseException] | None = None
            try:
                _ = authorized.request_context.acting_subject_id
            except BaseException as failure:
                request_failure = type(failure)
            try:
                persistence.uow_factory(authorized.persistence_context)
            except BaseException as failure:
                persistence_failure = type(failure)
            return request_failure, persistence_failure

        assert await asyncio.create_task(use_in_child_task()) == (
            InvalidPersistenceContextError,
            InvalidPersistenceContextError,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        {"acting_subject_id": UUID("10000000-0000-0000-0000-000000000002")},
        {
            "scope": EnvironmentScope(
                workspace_id=OTHER_WORKSPACE_ID,
                environment_id=OTHER_ENVIRONMENT_ID,
            )
        },
        {"purpose": PersistencePurpose("caller_selected")},
        {"operation": PersistenceOperation("caller_selected")},
        {"service_principal_id": OTHER_SERVICE_PRINCIPAL_ID},
        {"trace_id": OTHER_TRACE_ID},
    ],
    ids=["actor", "scope", "purpose", "operation", "service", "trace"],
)
async def test_persistence_field_substitution_fails_before_repository_access(
    replacement: dict[str, object],
) -> None:
    composer, persistence, _, _ = await composition()

    async with composer.bind_route("knowledge.sources.list").authorize(
        credentials=credentials(),
        requested_scope=scope(),
        diagnostic_context=diagnostic_context(),
    ) as authorized:
        substituted = replace(authorized.persistence_context, **replacement)
        with pytest.raises(InvalidPersistenceContextError):
            persistence.uow_factory(substituted)


@pytest.mark.asyncio
async def test_foreign_issuer_context_fails_before_repository_access() -> None:
    _, persistence, _, _ = await composition()
    foreign_boundary = TrustedContextBoundary.for_testing(
        issuer_id=UUID("60000000-0000-0000-0000-000000000099"),
        secret=b"foreign-persistence-context-secret-001",
    )
    foreign = foreign_boundary.interactive(
        scope=EnvironmentScope(WORKSPACE_ID, ENVIRONMENT_ID),
        acting_subject_id=IDENTITY_ID,
        purpose=policy().purpose,
        operation=policy().operation,
        trace_id=TRACE_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
    )

    with pytest.raises(InvalidPersistenceContextError):
        persistence.uow_factory(foreign)


def test_existing_authentication_configuration_version_syntax_is_preserved() -> None:
    assert alias("OIDC-2026").authentication_configuration_version == "OIDC-2026"
