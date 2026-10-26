from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionDeniedError,
    GoogleProviderReadError,
    GoogleReadAdmission,
    GoogleReadCommand,
)
from spine.application.persistence import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
)
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)
from spine.infrastructure.connections.google import InMemoryGoogleConnectionRepository
from spine.infrastructure.persistence.contexts import TrustedContextBoundary


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


WORKSPACE_ID = _id(1)
ENVIRONMENT_ID = _id(2)
SERVICE_PRINCIPAL_ID = _id(3)
CONNECTION_ID = _id(4)
RESOURCE_SCOPE = OpaqueGoogleReference(value="scope:estimate-register-v1")


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
        version=version,
        predecessor_version_id=predecessor_version_id,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        service_principal_id=SERVICE_PRINCIPAL_ID,
        credential_reference=OpaqueGoogleReference(value="vault:google-r2-v1"),
        grant_reference=OpaqueGoogleReference(value="grant:google-r2-v1"),
        approved_resource_scope_references=(RESOURCE_SCOPE,),
        authorization_generation=authorization_generation,
        configuration_generation=configuration_generation,
        status=status,
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


async def _repository_with(connection: GoogleConnection):
    boundary = _boundary()
    repository = InMemoryGoogleConnectionRepository(context_verifier=boundary)
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
        ),
        context,
    )
    return boundary, repository


@pytest.mark.asyncio
async def test_active_connection_allows_one_provider_read() -> None:
    boundary, repository = await _repository_with(_connection())
    provider = RecordingGoogleProvider()
    admission = GoogleReadAdmission(repository=repository, provider=provider)
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
            {"resource_scope_reference": OpaqueGoogleReference(value="scope:other-v1")},
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
    admission = GoogleReadAdmission(repository=repository, provider=provider)
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


@pytest.mark.asyncio
async def test_provider_failure_returns_content_free_error() -> None:
    boundary, repository = await _repository_with(_connection())
    provider = RecordingGoogleProvider(
        error=RuntimeError(
            "https://drive.google.test/private/customer-plan.xlsx refresh-token-secret"
        )
    )
    admission = GoogleReadAdmission(repository=repository, provider=provider)

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
