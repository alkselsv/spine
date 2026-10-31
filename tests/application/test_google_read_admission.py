from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionDeniedError,
    GoogleProviderAuthorizationError,
    GoogleProviderPolicyError,
    GoogleProviderQuotaError,
    GoogleProviderReadError,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.diagnostics import DiagnosticEventRegistry
from spine.application.lifecycle import LifecycleEligibilityQuery
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionApproval,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.domain.sources.lifecycle import (
    CurrentLifecycleEligibility,
    LifecycleEligibilityStatus,
)
from spine.infrastructure.connections.google import InMemoryGoogleConnectionRepository
from spine.infrastructure.diagnostics import RecordingDiagnosticSink
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


WORKSPACE_ID = _id(1)
ENVIRONMENT_ID = _id(2)
SERVICE_PRINCIPAL_ID = _id(3)
CONNECTION_ID = _id(4)
RESOURCE_SCOPE = OpaqueGoogleReference(value=_id(6))
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def _boundary() -> TrustedContextBoundary:
    return TrustedContextBoundary.for_testing(
        issuer_id=_id(90), secret=b"issue-599-trusted-context-secret-0001"
    )


def _context(
    *,
    workspace_id: UUID = WORKSPACE_ID,
    environment_id: UUID = ENVIRONMENT_ID,
    service_principal_id: UUID = SERVICE_PRINCIPAL_ID,
):
    return _boundary().worker(
        scope=EnvironmentScope(
            workspace_id=workspace_id, environment_id=environment_id
        ),
        service_principal_id=service_principal_id,
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_read"),
        trace_id=_id(91),
    )


def _connection(
    *,
    status: GoogleConnectionStatus = GoogleConnectionStatus.ACTIVE,
    version: int = 1,
    predecessor_version_id: UUID | None = None,
    authorization_generation: int = 7,
    configuration_generation: int = 11,
) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=_id(100 + version),
        connection_id=CONNECTION_ID,
        approval_id=_id(200 + version),
        version=version,
        predecessor_version_id=predecessor_version_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        credential_reference=OpaqueGoogleReference(value=_id(7 + version)),
        grant_reference=OpaqueGoogleReference(value=_id(17 + version)),
        approved_resource_scope_references=(RESOURCE_SCOPE,),
        authorization_generation=authorization_generation,
        configuration_generation=configuration_generation,
        status=status,
    )


def _approval(connection: GoogleConnection) -> GoogleConnectionApproval:
    return GoogleConnectionApproval(
        approval_id=connection.approval_id,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        service_principal_id=connection.service_principal_id,
        credential_reference=connection.credential_reference,
        grant_reference=connection.grant_reference,
        approved_resource_scope_references=connection.approved_resource_scope_references,
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        status=connection.status,
    )


def _admission(repository, provider, lifecycle_reader=None):
    registry = DiagnosticEventRegistry.with_default_families()
    sink = RecordingDiagnosticSink(registry)
    return (
        GoogleReadAdmission(
            repository=repository,
            provider=provider,
            diagnostic_sink=sink,
            diagnostic_registry=registry,
            clock=lambda: NOW,
            lifecycle_reader=lifecycle_reader,
        ),
        sink,
    )


class RecordingGoogleProvider:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[GoogleConnection, GoogleReadCommand]] = []
        self._error = error

    async def read(
        self, connection: GoogleConnection, command: GoogleReadCommand
    ) -> dict[str, str]:
        self.calls.append((connection, command))
        if self._error is not None:
            raise self._error
        return {"provider_event_reference": "event:synthetic-v1"}


class LifecycleReader:
    def __init__(self, *statuses: LifecycleEligibilityStatus) -> None:
        self.statuses = statuses
        self.calls = 0

    async def current_eligibility(self, query, context):
        del context
        self.calls += 1
        status = self.statuses[min(self.calls - 1, len(self.statuses) - 1)]
        return CurrentLifecycleEligibility(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            source_object_id=query.source_object_id,
            connection_id=query.connection_id,
            connection_version_id=query.connection_version_id,
            scope_reference=query.scope_reference,
            scope_version_id=query.scope_version_id,
            scope_digest=query.scope_digest,
            current_decision_id=_id(300),
            generation=1,
            status=status,
            legal_hold_active=False,
        )


def _lifecycle_query(connection: GoogleConnection) -> LifecycleEligibilityQuery:
    return LifecycleEligibilityQuery(
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        source_object_id=_id(301),
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        scope_reference=RESOURCE_SCOPE.value,
        scope_version_id=connection.approval_id,
        scope_digest="a" * 64,
        provider_object_reference="file-1",
    )


async def _repository_with(connection: GoogleConnection):
    boundary = _boundary()
    repository = InMemoryGoogleConnectionRepository(
        context_verifier=boundary, approvals=(_approval(connection),)
    )
    context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_register"),
        trace_id=_id(92),
    )
    await repository.register(
        GoogleConnectionCommand(
            connection=connection,
            idempotency_key="google-connection-registration-v1",
            audit_event_id=_id(93),
            occurred_at=NOW,
        ),
        context,
    )
    return boundary, repository


@pytest.mark.asyncio
async def test_active_connection_allows_one_provider_read() -> None:
    boundary, repository = await _repository_with(_connection())
    provider = RecordingGoogleProvider()
    admission, sink = _admission(repository, provider)
    command = GoogleReadCommand(
        connection_id=CONNECTION_ID,
        resource_scope_reference=RESOURCE_SCOPE,
        authorization_generation=7,
        configuration_generation=11,
    )

    result = await admission.read(
        command,
        boundary.worker(
            scope=EnvironmentScope(
                workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
            ),
            service_principal_id=SERVICE_PRINCIPAL_ID,
            purpose=PersistencePurpose("google_connector"),
            operation=PersistenceOperation("google_connection_read"),
            trace_id=_id(94),
        ),
    )

    assert result == {"provider_event_reference": "event:synthetic-v1"}
    assert provider.calls == [(_connection(), command)]
    assert sink.events == ()


@pytest.mark.asyncio
async def test_withdrawn_google_source_is_denied_before_provider_call() -> None:
    connection = _connection()
    boundary, repository = await _repository_with(connection)
    provider = RecordingGoogleProvider()
    lifecycle = LifecycleReader(LifecycleEligibilityStatus.READ_WITHDRAWN)
    admission, _ = _admission(repository, provider, lifecycle_reader=lifecycle)
    command = replace(
        GoogleReadCommand(
            connection_id=CONNECTION_ID,
            resource_scope_reference=RESOURCE_SCOPE,
            authorization_generation=7,
            configuration_generation=11,
            resource_object_reference="file-1",
        ),
        lifecycle_query=_lifecycle_query(connection),
    )

    with pytest.raises(GoogleConnectionDeniedError):
        await admission.read(command, _context())

    assert lifecycle.calls == 1
    assert provider.calls == []


@pytest.mark.asyncio
async def test_configured_lifecycle_authority_rejects_unbound_google_read() -> None:
    connection = _connection()
    _, repository = await _repository_with(connection)
    provider = RecordingGoogleProvider()
    lifecycle = LifecycleReader(LifecycleEligibilityStatus.ELIGIBLE)
    admission, _ = _admission(repository, provider, lifecycle_reader=lifecycle)

    with pytest.raises(GoogleConnectionDeniedError):
        await admission.read(
            GoogleReadCommand(
                connection_id=CONNECTION_ID,
                resource_scope_reference=RESOURCE_SCOPE,
                authorization_generation=7,
                configuration_generation=11,
            ),
            _context(),
        )

    assert provider.calls == []
    assert lifecycle.calls == 0


@pytest.mark.asyncio
async def test_google_download_is_discarded_when_scope_withdraws_during_read() -> None:
    connection = _connection()
    boundary, repository = await _repository_with(connection)
    provider = RecordingGoogleProvider()
    lifecycle = LifecycleReader(
        LifecycleEligibilityStatus.ELIGIBLE,
        LifecycleEligibilityStatus.READ_WITHDRAWN,
    )
    admission, _ = _admission(repository, provider, lifecycle_reader=lifecycle)
    command = replace(
        GoogleReadCommand(
            connection_id=CONNECTION_ID,
            resource_scope_reference=RESOURCE_SCOPE,
            authorization_generation=7,
            configuration_generation=11,
            resource_object_reference="file-1",
        ),
        lifecycle_query=_lifecycle_query(connection),
    )

    with pytest.raises(GoogleConnectionDeniedError):
        await admission.read(command, _context())

    assert lifecycle.calls == 2
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "connection", "command_updates", "scope_updates"),
    (
        ("revoked", _connection(status=GoogleConnectionStatus.REVOKED), {}, {}),
        ("wrong_workspace", _connection(), {}, {"workspace_id": _id(20)}),
        ("wrong_environment", _connection(), {}, {"environment_id": _id(21)}),
        (
            "wrong_principal",
            _connection(),
            {},
            {"service_principal_id": _id(22)},
        ),
        ("stale_authorization", _connection(), {"authorization_generation": 6}, {}),
        ("stale_configuration", _connection(), {"configuration_generation": 10}, {}),
        (
            "out_of_scope",
            _connection(),
            {"resource_scope_reference": OpaqueGoogleReference(value=_id(70))},
            {},
        ),
        ("wrong_operation", _connection(), {}, {"operation": "source_observation"}),
    ),
)
async def test_denied_request_fails_before_provider_call(
    case: str,
    connection: GoogleConnection,
    command_updates: dict[str, Any],
    scope_updates: dict[str, Any],
) -> None:
    del case
    boundary, repository = await _repository_with(connection)
    provider = RecordingGoogleProvider()
    admission, sink = _admission(repository, provider)
    command = GoogleReadCommand(
        connection_id=CONNECTION_ID,
        resource_scope_reference=RESOURCE_SCOPE,
        authorization_generation=7,
        configuration_generation=11,
    )
    if command_updates:
        command = replace(command, **command_updates)
    context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=scope_updates.get("workspace_id", WORKSPACE_ID),
            environment_id=scope_updates.get("environment_id", ENVIRONMENT_ID),
        ),
        service_principal_id=scope_updates.get(
            "service_principal_id", SERVICE_PRINCIPAL_ID
        ),
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation(
            scope_updates.get("operation", "google_connection_read")
        ),
        trace_id=_id(95),
    )

    with pytest.raises(GoogleConnectionDeniedError) as failure:
        await admission.read(command, context)

    assert str(failure.value) == "Google connection read denied."
    assert provider.calls == []
    assert sink.events[0].payload_json() == {"failure_code": "authorization.denied"}


@pytest.mark.asyncio
async def test_provider_failure_returns_content_free_error() -> None:
    boundary, repository = await _repository_with(_connection())
    provider = RecordingGoogleProvider(
        error=RuntimeError(
            "https://drive.google.test/private/customer-plan.xlsx refresh-token-secret"
        )
    )
    admission, sink = _admission(repository, provider)

    with pytest.raises(GoogleProviderReadError) as failure:
        await admission.read(
            GoogleReadCommand(
                connection_id=CONNECTION_ID,
                resource_scope_reference=RESOURCE_SCOPE,
                authorization_generation=7,
                configuration_generation=11,
            ),
            boundary.worker(
                scope=EnvironmentScope(
                    workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
                ),
                service_principal_id=SERVICE_PRINCIPAL_ID,
                purpose=PersistencePurpose("google_connector"),
                operation=PersistenceOperation("google_connection_read"),
                trace_id=_id(96),
            ),
        )

    assert str(failure.value) == "Google provider read failed."
    assert "customer-plan" not in repr(failure.value)
    assert "refresh-token-secret" not in repr(failure.value)
    assert sink.events[0].payload_json() == {"failure_code": "provider.unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "reason", "error_type", "failure_code"),
    (
        (401, "unauthorized", GoogleProviderAuthorizationError, "authorization_revoked"),
        (403, "invalid_grant", GoogleProviderAuthorizationError, "authorization_revoked"),
        (403, "insufficientPermissions", GoogleProviderPolicyError, "provider_policy_denied"),
        (429, "rateLimitExceeded", GoogleProviderQuotaError, "provider_quota_exhausted"),
    ),
)
async def test_provider_failures_are_classified_without_exposing_provider_text(
    status_code: int, reason: str, error_type: type[Exception], failure_code: str
) -> None:
    class ProviderError(RuntimeError):
        pass

    error = ProviderError("private URL https://drive.google.test/a.xlsx token-secret")
    error.status_code = status_code
    error.reason = reason
    boundary, repository = await _repository_with(_connection())
    admission, sink = _admission(repository, RecordingGoogleProvider(error=error))
    context = boundary.worker(
        scope=EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_read"),
        trace_id=_id(96),
    )

    with pytest.raises(error_type) as failure:
        await admission.read(
            GoogleReadCommand(
                connection_id=CONNECTION_ID,
                resource_scope_reference=RESOURCE_SCOPE,
                authorization_generation=7,
                configuration_generation=11,
            ),
            context,
        )

    assert failure.value.failure_code == failure_code
    assert "drive.google" not in repr(failure.value)
    assert "token-secret" not in repr(failure.value)
    assert sink.events[0].payload_json() == {
        "failure_code": (
            "authorization.denied"
            if error_type in {GoogleProviderAuthorizationError, GoogleProviderPolicyError}
            else "provider.unavailable"
        )
    }


@pytest.mark.asyncio
async def test_revoked_grant_suspends_connection_and_denies_the_next_read() -> None:
    boundary, repository = await _repository_with(_connection())
    provider_error = RuntimeError("private provider response")
    provider_error.status_code = 401
    provider_error.reason = "unauthorized"
    admission, _ = _admission(
        repository,
        RecordingGoogleProvider(error=provider_error),
    )
    read_context = boundary.worker(
        scope=EnvironmentScope(workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_read"),
        trace_id=_id(96),
    )
    command = GoogleReadCommand(
        connection_id=CONNECTION_ID,
        resource_scope_reference=RESOURCE_SCOPE,
        authorization_generation=7,
        configuration_generation=11,
    )

    with pytest.raises(GoogleProviderAuthorizationError):
        await admission.read(command, read_context)

    provider = RecordingGoogleProvider()
    next_admission, _ = _admission(repository, provider)
    with pytest.raises(GoogleConnectionDeniedError):
        await next_admission.read(command, read_context)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_read_lease_blocks_revocation_until_provider_call_finishes() -> None:
    initial = _connection()
    revoked = _connection(
        status=GoogleConnectionStatus.REVOKED,
        version=2,
        predecessor_version_id=initial.connection_version_id,
        authorization_generation=8,
        configuration_generation=12,
    )
    boundary = _boundary()
    repository = InMemoryGoogleConnectionRepository(
        context_verifier=boundary,
        approvals=(_approval(initial), _approval(revoked)),
    )
    registration_context = boundary.worker(
        scope=EnvironmentScope(
            workspace_id=WORKSPACE_ID, environment_id=ENVIRONMENT_ID
        ),
        service_principal_id=SERVICE_PRINCIPAL_ID,
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_register"),
        trace_id=_id(97),
    )
    await repository.register(
        GoogleConnectionCommand(
            connection=initial,
            idempotency_key="read-lease-initial-v1",
            audit_event_id=_id(98),
            occurred_at=NOW,
        ),
        registration_context,
    )
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    class BlockingProvider:
        async def read(
            self, connection: GoogleConnection, command: GoogleReadCommand
        ) -> str:
            del connection, command
            provider_started.set()
            await release_provider.wait()
            return "complete"

    admission, _ = _admission(repository, BlockingProvider())
    read_task = asyncio.create_task(
        admission.read(
            GoogleReadCommand(
                connection_id=CONNECTION_ID,
                resource_scope_reference=RESOURCE_SCOPE,
                authorization_generation=7,
                configuration_generation=11,
            ),
            _context(),
        )
    )
    await provider_started.wait()
    revoke_task = asyncio.create_task(
        repository.register(
            GoogleConnectionCommand(
                connection=revoked,
                idempotency_key="read-lease-revoked-v2",
                audit_event_id=_id(99),
                occurred_at=NOW,
            ),
            registration_context,
        )
    )
    await asyncio.sleep(0)

    assert revoke_task.done() is False

    release_provider.set()
    assert await read_task == "complete"
    assert (await revoke_task).connection == revoked
