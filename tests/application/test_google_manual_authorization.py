from __future__ import annotations

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
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[GoogleConnectionCommand, object]] = []
        self.fail = fail

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


@pytest.mark.asyncio
async def test_reauthorization_registers_exact_next_generation_successor() -> None:
    repository = RecordingRepository()
    result = await GoogleManualReauthorization(repository).execute(
        _connection(), _command(), _context()
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
    repository = RecordingRepository()

    with pytest.raises(GoogleConnectionConflictError):
        await GoogleManualReauthorization(repository).execute(current, command, _context())

    assert repository.calls == []


@pytest.mark.asyncio
async def test_repository_approval_conflict_is_preserved_for_reauthorization() -> None:
    repository = RecordingRepository(fail=True)

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


class ConsentFake:
    def __init__(self, result: GoogleConsentResult) -> None:
        self.result = result
        self.calls = 0

    async def authorize(self, *, client_configuration: SecretStr) -> GoogleConsentResult:
        self.calls += 1
        return self.result


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


@pytest.mark.asyncio
async def test_grant_lifecycle_deletes_only_after_verified_remote_revocation() -> None:
    reference = OpaqueGoogleReference(value=_id(30))
    credentials = CredentialFake()
    revocation = RevocationFake()
    credential_reference = OpaqueGoogleReference(value=_id(29))

    result = await GoogleGrantLifecycle(
        credentials=credentials, revocation=revocation
    ).revoke_and_delete(credential_reference, reference)

    assert result is True
    assert credentials.deleted == [(credential_reference, reference)]
    assert revocation.tokens == ["synthetic-refresh-token"]


@pytest.mark.asyncio
async def test_unverified_remote_revocation_keeps_local_credential() -> None:
    credentials = CredentialFake()
    lifecycle = GoogleGrantLifecycle(
        credentials=credentials, revocation=RevocationFake(result=False)
    )

    result = await lifecycle.revoke_and_delete(
        OpaqueGoogleReference(value=_id(31)), OpaqueGoogleReference(value=_id(32))
    )

    assert result is False
    assert credentials.deleted == []
