from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from spine.application.connections.email_read import (
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
from spine.connectors.gmail import GmailReadRequest, compose_gmail_mailbox_reader
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


def _scope(**changes: object) -> MailboxReadScope:
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
        "allowed_operations": (EmailProviderOperation.READ_MESSAGE_METADATA,),
    }
    values.update(changes)
    return MailboxReadScope(**values)


def _connection(**changes: object) -> ProviderConnection:
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
        "status": ProviderConnectionStatus.ACTIVE,
        "capabilities": capabilities,
    }
    values.update(changes)
    return ProviderConnection(**values)


def _context() -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(workspace_id=_id(1), environment_id=_id(2)),
        origin=ContextOrigin.WORKER,
        purpose=PersistencePurpose("connector.email.read.v1"),
        operation=PersistenceOperation("provider_connection_read"),
        trace_id=_id(20),
        acting_subject_id=None,
        service_principal_id=_id(21),
        provenance=TrustedContextProvenance(issuer_id=_id(22), signature=b"trusted"),
    )


class _Scopes:
    def __init__(self, approved: MailboxReadScope | None) -> None:
        self.approved = approved
        self.calls = 0

    async def resolve_exact(self, scope, context):
        del scope, context
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


class _RecordingGmailClient:
    def __init__(self) -> None:
        self.requests: list[GmailReadRequest] = []
        self.failure: Exception | None = None

    async def read(self, request: GmailReadRequest) -> str:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return "synthetic-gmail-result"


def _compose(
    *,
    scope: MailboxReadScope | None = None,
    approved: MailboxReadScope | None = None,
    connection: ProviderConnection | None = None,
):
    exact_scope = scope or _scope()
    scopes = _Scopes(approved if approved is not None else exact_scope)
    connections = _Connections(connection or _connection())
    client = _RecordingGmailClient()
    reader = compose_gmail_mailbox_reader(
        scopes=scopes,
        connections=connections,
        client=client,
    )
    command = MailboxReadCommand(
        scope=exact_scope,
        operation=EmailOperation.READ_MESSAGE_METADATA,
        mailbox_subject_reference=exact_scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
    )
    return reader, command, scopes, connections, client


@pytest.mark.asyncio
async def test_admitted_google_mailbox_reaches_fake_client_with_opaque_read_request() -> None:
    reader, command, _, _, client = _compose()

    result = await reader.read(command, _context())

    assert result == "synthetic-gmail-result"
    assert client.requests == [
        GmailReadRequest(
            operation=EmailOperation.READ_MESSAGE_METADATA,
            workspace_id=_id(1),
            environment_id=_id(2),
            connection_id=_id(3),
            connection_version_id=_id(4),
            credential_reference=OpaqueProviderReference(value=_id(24)),
            grant_reference=OpaqueProviderReference(value=_id(25)),
            resource_scope_reference=OpaqueProviderReference(value=_id(5)),
            mailbox_subject_reference=_id(6),
            mailbox_binding_id=_id(7),
            authorization_generation=8,
            configuration_generation=9,
            scope_generation=10,
            label_reference=_id(11),
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection_changes",
    (
        {"status": ProviderConnectionStatus.REVOKED},
        {"provider_kind": ProviderKind.YANDEX_MAIL},
        {"workspace_id": _id(99)},
        {"environment_id": _id(99)},
        {"connection_version_id": _id(99)},
        {"authorization_generation": 99},
        {"configuration_generation": 99},
        {"approved_resource_scope_references": (OpaqueProviderReference(value=_id(99)),)},
        {"capabilities": ()},
    ),
)
async def test_inactive_or_mismatched_connection_never_reaches_gmail(
    connection_changes: dict[str, object],
) -> None:
    reader, command, _, connections, client = _compose(
        connection=_connection(**connection_changes)
    )

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert connections.calls == 1
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approved",
    (
        None,
        _scope(mailbox_subject_reference=_id(99)),
        _scope(resource_scope_reference=_id(99)),
        _scope(scope_generation=99),
        _scope(mailbox_binding_id=_id(99)),
    ),
)
async def test_missing_or_wrong_exact_mail_scope_never_reaches_gmail(
    approved: MailboxReadScope | None,
) -> None:
    reader, command, scopes, connections, client = _compose(approved=approved)
    if approved is None:
        scopes.approved = None

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert connections.calls == 0
    assert client.requests == []


@pytest.mark.asyncio
async def test_drive_and_sheets_only_capabilities_never_reach_gmail() -> None:
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.BINARY_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(31)),
            ),
            ProviderCapability(
                kind=ProviderCapabilityKind.TABULAR_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    reader, command, _, connections, client = _compose(connection=connection)

    with pytest.raises(MailboxReadDeniedError):
        await reader.read(command, _context())

    assert connections.calls == 1
    assert client.requests == []


@pytest.mark.asyncio
async def test_google_error_details_are_sanitized_and_tokens_are_not_in_request() -> None:
    reader, command, _, _, client = _compose()
    client.failure = RuntimeError(
        "refresh-token-value private@example.test customer subject.pdf"
    )

    with pytest.raises(MailboxReadDeniedError) as error:
        await reader.read(command, _context())

    assert str(error.value) == "Mailbox read denied."
    assert "refresh-token-value" not in str(error.value)
    assert "private@example.test" not in str(error.value)
    assert "subject.pdf" not in str(error.value)
    assert client.requests[0].credential_reference == OpaqueProviderReference(value=_id(24))
    assert client.requests[0].grant_reference == OpaqueProviderReference(value=_id(25))
