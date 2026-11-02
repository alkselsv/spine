from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from spine.application.connections.email_read import (
    MailboxReadAdmission,
    MailboxReadCommand,
    MailboxReadDeniedError,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.connections.email_scope import (
    EmailOperation,
    EmailProviderOperation,
    MailboxReadScope,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope(**updates: object) -> MailboxReadScope:
    values: dict[str, object] = {
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "connection_id": _id(3),
        "connection_version_id": _id(4),
        "provider_kind": "google",
        "resource_scope_reference": _id(5),
        "mailbox_subject_reference": _id(6),
        "mailbox_binding_id": _id(7),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "scope_generation": 10,
        "allowed_label_references": (_id(11),),
        "excluded_label_references": (_id(12),),
        "allowed_operations": (
            EmailProviderOperation.ENUMERATE_MAILBOX,
            EmailProviderOperation.READ_MESSAGE_METADATA,
            EmailProviderOperation.READ_RAW_MESSAGE,
        ),
    }
    values.update(updates)
    return MailboxReadScope(**values)


def _context(workspace: UUID = _id(1), environment: UUID = _id(2)) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=workspace, environment_id=environment),
        origin=ContextOrigin.WORKER,
        purpose=PersistencePurpose("connector.email.read.v1"),
        operation=PersistenceOperation("provider_connection_read"),
        trace_id=_id(20),
        acting_subject_id=None,
        service_principal_id=_id(21),
        provenance=TrustedContextProvenance(issuer_id=_id(22), signature=b"trusted"),
    )


def _connection(
    *,
    status: ProviderConnectionStatus = ProviderConnectionStatus.ACTIVE,
    capabilities: tuple[ProviderCapability, ...] | None = None,
    **updates: object,
) -> ProviderConnection:
    if capabilities is None:
        capabilities = tuple(
            ProviderCapability(
                kind=kind,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(30 + index)),
            )
            for index, kind in enumerate(
                (
                    ProviderCapabilityKind.MAILBOX_ENUMERATION,
                    ProviderCapabilityKind.MESSAGE_METADATA_READ,
                    ProviderCapabilityKind.RAW_MESSAGE_READ,
                )
            )
        )
    values: dict[str, object] = {
        "connection_version_id": _id(4),
        "connection_id": _id(3),
        "approval_id": _id(23),
        "version": 1,
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "service_principal_id": _id(21),
        "provider_kind": ProviderKind.GOOGLE,
        "credential_reference": OpaqueProviderReference(value=_id(24)),
        "grant_reference": OpaqueProviderReference(value=_id(25)),
        "approved_resource_scope_references": (OpaqueProviderReference(value=_id(5)),),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "status": status,
        "capabilities": capabilities,
    }
    values.update(updates)
    return ProviderConnection(**values)


class _Scopes:
    def __init__(self, approved: MailboxReadScope | None = None) -> None:
        self.approved = approved
        self.calls = 0

    async def resolve_exact(self, scope, context):
        del context
        self.calls += 1
        return self.approved


class _Connections:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection
        self.calls = 0

    @asynccontextmanager
    async def admit_read(self, command, context):
        del command, context
        self.calls += 1
        yield self.connection


class _Provider:
    def __init__(self) -> None:
        self.calls: list[MailboxReadCommand] = []
        self.failure: Exception | None = None

    async def read(self, connection, command):
        del connection
        self.calls.append(command)
        if self.failure is not None:
            raise self.failure
        return "synthetic-result"


def _command(
    operation: EmailOperation = EmailOperation.READ_MESSAGE_METADATA,
    *,
    scope: MailboxReadScope | None = None,
    location: UUID | None = _id(11),
) -> MailboxReadCommand:
    effective = scope or _scope()
    return MailboxReadCommand(
        scope=effective,
        operation=operation,
        mailbox_subject_reference=effective.mailbox_subject_reference,
        folder_or_label_reference=location,
    )


def _admission(
    *,
    connection: ProviderConnection | None = None,
    approved: MailboxReadScope | None = None,
):
    provider = _Provider()
    scopes = _Scopes(approved or _scope())
    connections = _Connections(connection or _connection())
    return (
        MailboxReadAdmission(scopes=scopes, connections=connections, provider=provider),
        scopes,
        connections,
        provider,
    )


@pytest.mark.asyncio
async def test_exact_mailbox_scope_admits_only_the_requested_provider_operation() -> None:
    admission, _, _, provider = _admission()

    result = await admission.read(_command(), _context())

    assert result == "synthetic-result"
    assert [call.operation for call in provider.calls] == [EmailOperation.READ_MESSAGE_METADATA]


@pytest.mark.asyncio
async def test_wrong_workspace_or_environment_is_denied_before_repository_or_provider() -> None:
    admission, scopes, connections, provider = _admission()

    with pytest.raises(MailboxReadDeniedError, match="Mailbox read denied"):
        await admission.read(_command(), _context(workspace=_id(99)))

    assert scopes.calls == 0
    assert connections.calls == 0
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection_changes",
    (
        {"workspace_id": _id(99)},
        {"environment_id": _id(99)},
        {"connection_version_id": _id(99)},
        {"authorization_generation": 99},
        {"configuration_generation": 99},
        {"provider_kind": ProviderKind.YANDEX_MAIL},
        {"status": ProviderConnectionStatus.REVOKED},
    ),
)
async def test_connection_scope_or_generation_mismatch_never_calls_provider(
    connection_changes: dict[str, object],
) -> None:
    admission, _, _, provider = _admission(connection=_connection(**connection_changes))

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(_command(), _context())

    assert provider.calls == []


@pytest.mark.asyncio
async def test_stale_or_revoked_scope_is_denied_before_provider_call() -> None:
    admission, _, connections, provider = _admission(
        approved=_scope(scope_generation=11)
    )

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(_command(), _context())

    assert connections.calls == 0
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    (EmailOperation.READ_RAW_MESSAGE, EmailOperation.READ_ATTACHMENT, EmailOperation.SYNCHRONIZE),
)
async def test_ungranted_operation_is_denied_without_provider_call(
    operation: EmailOperation,
) -> None:
    scope = _scope(allowed_operations=(EmailProviderOperation.READ_MESSAGE_METADATA,))
    admission, _, connections, provider = _admission(approved=scope)

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(_command(operation, scope=scope), _context())

    assert connections.calls == 0
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", (EmailOperation.PROCESS_CONTENT, EmailOperation.READ_CONTENT))
async def test_provider_read_authority_never_grants_processing_or_content_read(
    operation: EmailOperation,
) -> None:
    admission, _, connections, provider = _admission()

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(_command(operation), _context())

    assert connections.calls == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_excluded_or_unknown_label_is_denied_before_provider_call() -> None:
    admission, _, connections, provider = _admission()

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(_command(location=_id(12)), _context())

    assert connections.calls == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_wrong_mailbox_subject_is_denied_before_scope_lookup() -> None:
    admission, scopes, connections, provider = _admission()

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(
            _command().model_copy(
                update={"mailbox_subject_reference": _id(99)}
            ),
            _context(),
        )

    assert scopes.calls == 0
    assert connections.calls == 0
    assert provider.calls == []


@pytest.mark.asyncio
async def test_connection_capability_is_required_independently_of_scope_grant() -> None:
    metadata_capability = ProviderCapability(
        kind=ProviderCapabilityKind.MESSAGE_METADATA_READ,
        status=ProviderCapabilityStatus.SUPPORTED,
        qualification_reference=OpaqueProviderReference(value=_id(32)),
    )
    admission, _, connections, provider = _admission(
        connection=_connection(capabilities=(metadata_capability,))
    )

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(_command(EmailOperation.READ_RAW_MESSAGE), _context())

    assert connections.calls == 1
    assert provider.calls == []


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized_without_sensitive_details() -> None:
    admission, _, _, provider = _admission()
    provider.failure = RuntimeError(
        "private@example.test subject token attachment-name.pdf"
    )

    with pytest.raises(MailboxReadDeniedError) as error:
        await admission.read(_command(), _context())

    assert str(error.value) == "Mailbox read denied."
    assert "private@example.test" not in str(error.value)
    assert "subject" not in str(error.value)
    assert "token" not in str(error.value)
    assert "attachment-name.pdf" not in str(error.value)


@pytest.mark.asyncio
async def test_enumeration_does_not_accept_a_folder_or_label_expansion() -> None:
    admission, _, connections, provider = _admission()

    with pytest.raises(MailboxReadDeniedError):
        await admission.read(
            _command(EmailOperation.ENUMERATE_MAILBOX, location=_id(11)), _context()
        )

    assert connections.calls == 0
    assert provider.calls == []
