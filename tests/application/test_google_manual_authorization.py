from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import SecretStr

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionMutationResult,
)
from spine.application.google_manual_authorization import (
    GoogleConsentResult,
    GoogleGrantLifecycle,
    GoogleManualConsent,
    GoogleManualReauthorization,
    GoogleManualRevocation,
    GoogleReauthorizationCommand,
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
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from spine.infrastructure.connections.google import InMemoryGoogleConnectionRepository
from spine.domain.connections.google import GoogleConnectionApproval

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _connection(*, status: GoogleConnectionStatus = GoogleConnectionStatus.ACTIVE) -> GoogleConnection:
    return GoogleConnection(
        connection_version_id=_id(1),
        connection_id=_id(2),
        approval_id=_id(3),
        version=1,
        workspace_id=_id(4),
        environment_id=_id(5),
        service_principal_id=_id(6),
        credential_reference=OpaqueGoogleReference(value=_id(7)),
        grant_reference=OpaqueGoogleReference(value=_id(8)),
        approved_resource_scope_references=(OpaqueGoogleReference(value=_id(9)),),
        authorization_generation=4,
        configuration_generation=2,
        status=status,
    )


def _command(**overrides: object) -> GoogleReauthorizationCommand:
    values: dict[str, object] = {
        "connection_id": _id(2),
        "expected_current_version_id": _id(1),
        "approval_id": _id(10),
        "credential_reference": OpaqueGoogleReference(value=_id(11)),
        "grant_reference": OpaqueGoogleReference(value=_id(12)),
        "approved_resource_scope_references": (OpaqueGoogleReference(value=_id(9)),),
        "authorization_generation": 5,
        "configuration_generation": 2,
        "idempotency_key": "reauth-1",
        "audit_event_id": _id(13),
        "occurred_at": NOW,
    }
    values.update(overrides)
    return GoogleReauthorizationCommand(**values)  # type: ignore[arg-type]


class RecordingRepository:
    def __init__(
        self,
        *,
        current: GoogleConnection | None = None,
        fail: bool = False,
    ) -> None:
        self.calls: list[tuple[GoogleConnectionCommand, object]] = []
        self.current = current
        self.fail = fail

    async def get_current(self, connection_id, context):
        del context
        if self.current is None or self.current.connection_id != connection_id:
            raise GoogleConnectionConflictError()
        return self.current

    async def register(self, command, context):
        self.calls.append((command, context))
        if self.fail:
            raise GoogleConnectionConflictError()
        return GoogleConnectionMutationResult(
            connection=command.connection, audit=None  # type: ignore[arg-type]
        )


def _context():
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(20), secret=b"google-manual-reauth-test-secret"
    )
    return boundary.interactive(
        scope=EnvironmentScope(workspace_id=_id(4), environment_id=_id(5)),
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_register"),
        acting_subject_id=_id(21),
        trace_id=_id(22),
    )


async def _registered_connection_repository():
    current = _connection()
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(60), secret=b"google-manual-revocation-test-secret"
    )
    context = boundary.interactive(
        scope=EnvironmentScope(workspace_id=_id(4), environment_id=_id(5)),
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_register"),
        acting_subject_id=_id(61),
        trace_id=_id(62),
    )
    repository = InMemoryGoogleConnectionRepository(
        context_verifier=boundary, approvals=(_approval(current),)
    )
    await repository.register(
        GoogleConnectionCommand(
            connection=current,
            idempotency_key="initial-google-connection",
            audit_event_id=_id(63),
            occurred_at=NOW,
        ),
        context,
    )
    return current, context, repository


@pytest.mark.asyncio
async def test_reauthorization_registers_exact_next_generation_successor() -> None:
    current = _connection()
    repository = RecordingRepository(current=current)
    result = await GoogleManualReauthorization(repository).execute(
        current, _command(), _context()
    )

    registered = repository.calls[0][0]
    assert registered.connection.connection_id == _id(2)
    assert registered.connection.version == 2
    assert registered.connection.predecessor_version_id == _id(1)
    assert registered.connection.authorization_generation == 5
    assert registered.connection.configuration_generation == 2
    assert registered.connection.status is GoogleConnectionStatus.ACTIVE
    assert result.connection == registered.connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "command"),
    (
        (_connection(), _command(expected_current_version_id=_id(99))),
        (_connection(), _command(authorization_generation=6)),
        (_connection(), _command(configuration_generation=3)),
        (_connection(), _command(credential_reference=OpaqueGoogleReference(value=_id(7)))),
        (
            _connection(status=GoogleConnectionStatus.REVOKED),
            _command(),
        ),
    ),
)
async def test_invalid_or_revoked_reauthorization_is_rejected_before_register(
    current: GoogleConnection, command: GoogleReauthorizationCommand
) -> None:
    repository = RecordingRepository(current=_connection())

    with pytest.raises(GoogleConnectionConflictError):
        await GoogleManualReauthorization(repository).execute(current, command, _context())

    assert repository.calls == []


@pytest.mark.asyncio
async def test_repository_approval_conflict_is_preserved_for_reauthorization() -> None:
    repository = RecordingRepository(current=_connection(), fail=True)

    with pytest.raises(GoogleConnectionConflictError):
        await GoogleManualReauthorization(repository).execute(
            _connection(), _command(), _context()
        )

    assert len(repository.calls) == 1


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


def _successor_approval(command: GoogleReauthorizationCommand) -> GoogleConnectionApproval:
    current = _connection()
    return GoogleConnectionApproval(
        approval_id=command.approval_id,
        workspace_id=current.workspace_id,
        environment_id=current.environment_id,
        service_principal_id=current.service_principal_id,
        credential_reference=command.credential_reference,
        grant_reference=command.grant_reference,
        approved_resource_scope_references=command.approved_resource_scope_references,
        authorization_generation=command.authorization_generation,
        configuration_generation=command.configuration_generation,
        status=GoogleConnectionStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_reauthorization_is_idempotent_and_repository_rejects_stale_successor() -> None:
    current = _connection()
    command = _command()
    stale_command = _command(
        idempotency_key="stale-reauth",
        audit_event_id=_id(44),
        approval_id=_id(47),
        credential_reference=OpaqueGoogleReference(value=_id(45)),
        grant_reference=OpaqueGoogleReference(value=_id(46)),
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(40), secret=b"google-reauth-canonical-test-secret"
    )
    context = boundary.interactive(
        scope=EnvironmentScope(workspace_id=_id(4), environment_id=_id(5)),
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_register"),
        acting_subject_id=_id(41),
        trace_id=_id(42),
    )
    repository = InMemoryGoogleConnectionRepository(
        context_verifier=boundary,
        approvals=(
            _approval(current),
            _successor_approval(command),
            _successor_approval(stale_command),
        ),
    )
    initial_command = GoogleConnectionCommand(
        connection=current,
        idempotency_key="initial-google-connection",
        audit_event_id=_id(43),
        occurred_at=NOW,
    )
    await repository.register(initial_command, context)
    use_case = GoogleManualReauthorization(repository)

    first = await use_case.execute(current, command, context)
    replay = await use_case.execute(current, command, context)

    assert first.connection.version == 2
    assert replay.replay is True
    with pytest.raises(GoogleConnectionConflictError):
        await use_case.execute(current, stale_command, context)


@pytest.mark.asyncio
async def test_reauthorization_exact_approval_mismatch_is_rejected_by_repository() -> None:
    current = _connection()
    command = _command()
    wrong_approval = _successor_approval(command).model_copy(
        update={"grant_reference": OpaqueGoogleReference(value=_id(48))}
    )
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(49), secret=b"google-reauth-approval-test-secret"
    )
    context = boundary.interactive(
        scope=EnvironmentScope(workspace_id=_id(4), environment_id=_id(5)),
        purpose=PersistencePurpose("google_connector"),
        operation=PersistenceOperation("google_connection_register"),
        acting_subject_id=_id(50),
        trace_id=_id(51),
    )
    repository = InMemoryGoogleConnectionRepository(
        context_verifier=boundary,
        approvals=(_approval(current), wrong_approval),
    )
    await repository.register(
        GoogleConnectionCommand(
            connection=current,
            idempotency_key="initial-google-connection",
            audit_event_id=_id(52),
            occurred_at=NOW,
        ),
        context,
    )

    with pytest.raises(GoogleConnectionConflictError):
        await GoogleManualReauthorization(repository).execute(current, command, context)


class ConsentFake:
    def __init__(self, result: GoogleConsentResult) -> None:
        self.result = result
        self.calls = 0

    async def authorize(self, *, client_configuration: SecretStr) -> GoogleConsentResult:
        self.calls += 1
        return self.result


class CancellingConsent:
    async def authorize(self, *, client_configuration: SecretStr) -> GoogleConsentResult:
        del client_configuration
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_manual_consent_uses_fakeable_port_without_secret_disclosure() -> None:
    secret = "synthetic-refresh-token"
    fake = ConsentFake(
        GoogleConsentResult(
            client_configuration=SecretStr('{"installed":{}}'),
            refresh_token=SecretStr(secret),
        )
    )

    result = await GoogleManualConsent(fake).authorize(
        client_configuration=SecretStr("client-config")
    )

    assert fake.calls == 1
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_manual_consent_propagates_cancellation() -> None:
    with pytest.raises(asyncio.CancelledError):
        await GoogleManualConsent(CancellingConsent()).authorize(
            client_configuration=SecretStr("synthetic-client-config")
        )


class CredentialFake:
    def __init__(self, *, delete_result: bool = True) -> None:
        self.delete_result = delete_result
        self.deleted: list[tuple[OpaqueGoogleReference, OpaqueGoogleReference]] = []

    async def read_refresh_token(self, reference: OpaqueGoogleReference) -> SecretStr:
        return SecretStr("synthetic-refresh-token")

    async def delete(
        self,
        credential_reference: OpaqueGoogleReference,
        grant_reference: OpaqueGoogleReference,
    ) -> bool:
        self.deleted.append((credential_reference, grant_reference))
        return self.delete_result


class RevocationFake:
    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.tokens: list[str] = []

    async def revoke(self, *, refresh_token: SecretStr) -> bool:
        self.tokens.append(refresh_token.get_secret_value())
        return self.result


class CancellingCredentials(CredentialFake):
    async def read_refresh_token(self, reference: OpaqueGoogleReference) -> SecretStr:
        del reference
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_manual_revocation_persists_denied_state_before_local_cleanup() -> None:
    current, context, repository = await _registered_connection_repository()
    credentials = CredentialFake()
    revocation = RevocationFake()

    result = await GoogleManualRevocation(
        repository=repository,
        grant_lifecycle=GoogleGrantLifecycle(credentials=credentials, revocation=revocation),
    ).execute(current, context, occurred_at=NOW)

    assert result.connection.status is GoogleConnectionStatus.SUSPENDED
    assert result.connection.version == 2
    assert result.remote_revocation_confirmed is True
    assert result.local_cleanup_verified is True
    assert credentials.deleted == [(current.credential_reference, current.grant_reference)]
    assert revocation.tokens == ["synthetic-refresh-token"]


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (GoogleConnectionStatus.SUSPENDED, GoogleConnectionStatus.REVOKED),
)
async def test_manual_revocation_rejects_noncanonical_state_before_side_effects(
    status: GoogleConnectionStatus,
) -> None:
    current, context, repository = await _registered_connection_repository()
    fabricated = current.model_copy(
        update={
            "connection_version_id": _id(70),
            "version": 2,
            "predecessor_version_id": current.connection_version_id,
            "status": status,
        }
    )
    credentials = CredentialFake()
    revocation = RevocationFake()
    operation = GoogleManualRevocation(
        repository=repository,
        grant_lifecycle=GoogleGrantLifecycle(
            credentials=credentials, revocation=revocation
        ),
    )

    with pytest.raises(GoogleConnectionConflictError):
        await operation.execute(fabricated, context, occurred_at=NOW)

    assert revocation.tokens == []
    assert credentials.deleted == []


@pytest.mark.asyncio
async def test_unverified_remote_revocation_keeps_connection_suspended_and_secret_files() -> None:
    current, context, repository = await _registered_connection_repository()
    credentials = CredentialFake()
    lifecycle = GoogleManualRevocation(
        repository=repository,
        grant_lifecycle=GoogleGrantLifecycle(
            credentials=credentials, revocation=RevocationFake(result=False)
        ),
    )

    result = await lifecycle.execute(current, context, occurred_at=NOW)

    assert result.connection.status is GoogleConnectionStatus.SUSPENDED
    assert result.remote_revocation_confirmed is False
    assert result.local_cleanup_verified is False
    assert credentials.deleted == []


@pytest.mark.asyncio
async def test_grant_lifecycle_propagates_cancellation_before_remote_revoke() -> None:
    revocation = RevocationFake()
    lifecycle = GoogleGrantLifecycle(
        credentials=CancellingCredentials(), revocation=revocation
    )

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.revoke_remote(OpaqueGoogleReference(value=_id(34)))

    assert revocation.tokens == []
