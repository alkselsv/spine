from __future__ import annotations

import ssl
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from pydantic import SecretStr

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
from spine.connectors.yandex_mail import (
    YANDEX_MAIL_OAUTH_SCOPE,
    YandexMailOAuthCredential,
    compose_yandex_mail_reader,
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


_TOKEN = "synthetic-access-token"
_MAILBOX = "synthetic-mailbox@example.test"


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


def _scope(**changes: object) -> MailboxReadScope:
    values: dict[str, object] = {
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "connection_id": _id(3),
        "connection_version_id": _id(4),
        "provider_kind": "yandex_mail",
        "resource_scope_reference": _id(5),
        "mailbox_subject_reference": _id(6),
        "mailbox_binding_id": _id(7),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "scope_generation": 10,
        "allowed_folder_references": (_id(11),),
        "allowed_operations": (
            EmailProviderOperation.ENUMERATE_MAILBOX,
            EmailProviderOperation.READ_MESSAGE_METADATA,
            EmailProviderOperation.READ_RAW_MESSAGE,
            EmailProviderOperation.READ_ATTACHMENT,
        ),
    }
    values.update(changes)
    return MailboxReadScope(**values)


def _connection(
    *,
    kind: ProviderKind = ProviderKind.YANDEX_MAIL,
    status: ProviderConnectionStatus = ProviderConnectionStatus.ACTIVE,
    capability_status: ProviderCapabilityStatus = ProviderCapabilityStatus.SUPPORTED,
    **changes: object,
) -> ProviderConnection:
    capability = ProviderCapability(
        kind=ProviderCapabilityKind.MESSAGE_METADATA_READ,
        status=capability_status,
        qualification_reference=(
            OpaqueProviderReference(value=_id(30))
            if capability_status is ProviderCapabilityStatus.SUPPORTED
            else None
        ),
    )
    values: dict[str, object] = {
        "connection_version_id": _id(4),
        "connection_id": _id(3),
        "approval_id": _id(23),
        "version": 1,
        "workspace_id": _id(1),
        "environment_id": _id(2),
        "service_principal_id": _id(21),
        "provider_kind": kind,
        "credential_reference": OpaqueProviderReference(value=_id(24)),
        "grant_reference": OpaqueProviderReference(value=_id(25)),
        "approved_resource_scope_references": (OpaqueProviderReference(value=_id(5)),),
        "authorization_generation": 8,
        "configuration_generation": 9,
        "status": status,
        "capabilities": (capability,),
    }
    values.update(changes)
    return ProviderConnection(**values)


def _credential(
    connection: ProviderConnection,
    scope: MailboxReadScope,
    *,
    scopes: tuple[str, ...] = (YANDEX_MAIL_OAUTH_SCOPE,),
    **changes: object,
) -> YandexMailOAuthCredential:
    values: dict[str, object] = {
        "access_token": SecretStr(_TOKEN),
        "mailbox_login": SecretStr(_MAILBOX),
        "workspace_id": scope.workspace_id,
        "environment_id": scope.environment_id,
        "connection_id": scope.connection_id,
        "connection_version_id": scope.connection_version_id,
        "credential_reference": connection.credential_reference.value,
        "grant_reference": connection.grant_reference.value,
        "authorization_generation": scope.authorization_generation,
        "configuration_generation": scope.configuration_generation,
        "approved_oauth_scopes": scopes,
    }
    values.update(changes)
    return YandexMailOAuthCredential(**values)


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
    def __init__(self, scope: MailboxReadScope) -> None:
        self.scope = scope
        self.calls = 0

    async def resolve_exact(self, scope, context):
        del context
        self.calls += 1
        return self.scope if scope == self.scope else None


class _Connections:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection
        self.calls = 0

    @asynccontextmanager
    async def admit_read(self, command, context):
        del command, context
        self.calls += 1
        yield self.connection


class _Credentials:
    def __init__(self, credential: YandexMailOAuthCredential) -> None:
        self.credential = credential
        self.calls = 0
        self.failure: Exception | None = None

    async def resolve(self, connection, scope):
        del connection, scope
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.credential


class _Imap:
    def __init__(self) -> None:
        self.tls_calls = []
        self.authentication_transcript = []
        self.read_calls = []
        self.open_failure: Exception | None = None
        self.authentication_failure: Exception | None = None

    async def open_tls(self, *, host, port, server_hostname, ssl_context):
        self.tls_calls.append((host, port, server_hostname, ssl_context))
        if self.open_failure is not None:
            raise self.open_failure
        return _ImapSession(self)


class _ImapSession:
    def __init__(self, transport: _Imap) -> None:
        self.transport = transport

    async def authenticate_xoauth2(self, *, mailbox_login, access_token):
        assert isinstance(mailbox_login, SecretStr)
        assert isinstance(access_token, SecretStr)
        self.transport.authentication_transcript.append("AUTHENTICATE XOAUTH2")
        if self.transport.authentication_failure is not None:
            raise self.transport.authentication_failure

    async def read(self, request):
        self.transport.read_calls.append(request)
        return "synthetic-result"

    async def close(self):
        return None


def _command(
    scope: MailboxReadScope,
    operation: EmailOperation = EmailOperation.READ_MESSAGE_METADATA,
    *,
    location: UUID | None = _id(11),
) -> MailboxReadCommand:
    return MailboxReadCommand(
        scope=scope,
        operation=operation,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=location,
    )


def _composition(
    *,
    scope: MailboxReadScope | None = None,
    connection: ProviderConnection | None = None,
    credential: YandexMailOAuthCredential | None = None,
):
    active_scope = scope or _scope()
    active_connection = connection or _connection()
    credential_resolver = _Credentials(
        credential or _credential(active_connection, active_scope)
    )
    imap = _Imap()
    scopes = _Scopes(active_scope)
    connections = _Connections(active_connection)
    composition = compose_yandex_mail_reader(
        scopes=scopes,
        connections=connections,
        credentials=credential_resolver,
        imap=imap,
    )
    return composition, scopes, connections, credential_resolver, imap


@pytest.mark.asyncio
async def test_yandex_mail_composition_uses_tls_verified_xoauth2_and_read_only_scope() -> None:
    composition, _, _, resolver, imap = _composition()

    result = await composition.read(_command(_scope()), _context())

    assert result == "synthetic-result"
    assert resolver.calls == 1
    request = imap.read_calls[0]
    assert request.security.host == "imap.yandex.ru"
    assert request.security.port == 993
    assert request.security.tls_required is True
    assert request.security.verify_server_identity is True
    assert request.security.auth_mechanism == "XOAUTH2"
    assert request.security.read_only is True
    assert request.operation is EmailOperation.READ_MESSAGE_METADATA
    host, port, server_hostname, tls_context = imap.tls_calls[0]
    assert (host, port, server_hostname) == ("imap.yandex.ru", 993, "imap.yandex.ru")
    assert tls_context.check_hostname is True
    assert tls_context.verify_mode is ssl.CERT_REQUIRED
    assert imap.authentication_transcript == ["AUTHENTICATE XOAUTH2"]
    assert _TOKEN not in repr(imap.authentication_transcript)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection",
    (
        _connection(kind=ProviderKind.YANDEX_DISK),
        _connection(status=ProviderConnectionStatus.REVOKED),
        _connection(capability_status=ProviderCapabilityStatus.UNQUALIFIED),
        _connection(workspace_id=_id(99)),
        _connection(environment_id=_id(99)),
        _connection(connection_version_id=_id(99)),
        _connection(authorization_generation=99),
        _connection(configuration_generation=99),
    ),
)
async def test_wrong_connection_or_generation_never_reaches_imap(
    connection: ProviderConnection,
) -> None:
    composition, _, _, resolver, imap = _composition(connection=connection)

    with pytest.raises(MailboxReadDeniedError):
        await composition.read(_command(_scope()), _context())

    assert resolver.calls == 0
    assert imap.tls_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approved_scopes",
    ((), ("cloud_api:disk.read",), (YANDEX_MAIL_OAUTH_SCOPE, "cloud_api:disk.read")),
)
async def test_missing_or_disk_oauth_scope_never_reaches_imap(
    approved_scopes: tuple[str, ...],
) -> None:
    scope = _scope()
    connection = _connection()
    credential = _credential(connection, scope, scopes=approved_scopes)
    composition, _, _, resolver, imap = _composition(
        scope=scope, connection=connection, credential=credential
    )

    with pytest.raises(MailboxReadDeniedError):
        await composition.read(_command(scope), _context())

    assert resolver.calls == 1
    assert imap.tls_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_changes",
    (
        {"workspace_id": _id(99)},
        {"environment_id": _id(99)},
        {"connection_id": _id(99)},
        {"connection_version_id": _id(99)},
        {"authorization_generation": 99},
        {"configuration_generation": 99},
        {"credential_reference": _id(99)},
        {"grant_reference": _id(99)},
        {"access_token": SecretStr("")},
    ),
)
async def test_stale_or_wrong_credential_binding_never_reaches_imap(
    credential_changes: dict[str, object],
) -> None:
    scope = _scope()
    connection = _connection()
    credential = _credential(connection, scope, **credential_changes)
    composition, _, _, _, imap = _composition(
        scope=scope, connection=connection, credential=credential
    )

    with pytest.raises(MailboxReadDeniedError):
        await composition.read(_command(scope), _context())

    assert imap.tls_calls == []


@pytest.mark.asyncio
async def test_scope_denial_and_revocation_make_zero_imap_calls() -> None:
    scope = _scope()
    composition, scopes, connections, resolver, imap = _composition(scope=scope)
    denied_scope = scope.model_copy(update={"scope_generation": 11})

    with pytest.raises(MailboxReadDeniedError):
        await composition.read(_command(denied_scope), _context())

    assert scopes.calls == 1
    assert connections.calls == 0
    assert resolver.calls == 0
    assert imap.tls_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_changes",
    (
        {"mailbox_subject_reference": _id(99)},
        {"folder_or_label_reference": _id(99)},
    ),
)
async def test_wrong_mailbox_or_folder_is_denied_before_imap(
    command_changes: dict[str, object],
) -> None:
    scope = _scope()
    composition, _, connections, resolver, imap = _composition(scope=scope)
    command = _command(scope).model_copy(update=command_changes)

    with pytest.raises(MailboxReadDeniedError):
        await composition.read(command, _context())

    assert connections.calls == 0
    assert resolver.calls == 0
    assert imap.tls_calls == []


@pytest.mark.asyncio
async def test_revoked_oauth_authority_is_denied_before_imap() -> None:
    composition, _, _, resolver, imap = _composition()
    resolver.failure = PermissionError("revoked synthetic grant")

    with pytest.raises(MailboxReadDeniedError):
        await composition.read(_command(_scope()), _context())

    assert resolver.calls == 1
    assert imap.tls_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("tls", "authentication"))
async def test_imap_failures_and_credential_repr_do_not_disclose_secrets(
    failure_stage: str,
) -> None:
    scope = _scope()
    connection = _connection()
    credential = _credential(connection, scope)
    composition, _, _, _, imap = _composition(
        scope=scope, connection=connection, credential=credential
    )

    failure = (
        ssl.SSLCertVerificationError(f"{_TOKEN} {_MAILBOX} private subject")
        if failure_stage == "tls"
        else PermissionError(f"{_TOKEN} {_MAILBOX} private subject")
    )
    if failure_stage == "tls":
        imap.open_failure = failure
    else:
        imap.authentication_failure = failure
    with pytest.raises(MailboxReadDeniedError) as error:
        await composition.read(_command(scope), _context())

    assert str(error.value) == "Mailbox read denied."
    assert _TOKEN not in repr(credential)
    assert _MAILBOX not in repr(credential)
    assert _TOKEN not in str(error.value)
    assert _MAILBOX not in str(error.value)
    assert imap.read_calls == []
