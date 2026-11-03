from __future__ import annotations

import asyncio
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
    YandexMailUidImapReadRequest,
    compose_yandex_mail_reader,
)
from spine.connectors.yandex_mail_folders import (
    YandexMailFolderBinding,
    YandexMailFolderDiscoveryCode,
    YandexMailFolderDiscoveryDeniedError,
    YandexMailFolderDiscoveryProfile,
    YandexMailFolderListing,
    YandexMailListedFolder,
    discover_approved_yandex_mail_folders,
)
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderExamineTranscript,
    YandexMailFolderGenerationCode,
    YandexMailFolderGenerationObservation,
)
from spine.connectors.yandex_mail_uid_enumeration import (
    YandexMailUidEnumerationPageRequest,
    YandexMailUidEnumerationPageTranscript,
    YandexMailUidMessageMetadata,
    YandexMailUidPageCode,
)
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchCommand,
    YandexMailRawMessageFetchEnd,
    YandexMailRawMessageFetchEvent,
    YandexMailRawMessageFetchStart,
    YandexMailRawMessageLiteralChunk,
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
            EmailProviderOperation.ENUMERATE_FOLDER_UIDS,
            EmailProviderOperation.OBSERVE_FOLDER_GENERATION,
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
        "capabilities": (
            capability,
            ProviderCapability(
                kind=ProviderCapabilityKind.MAILBOX_ENUMERATION,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(31)),
            ),
        ),
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
        self.closed_sessions = 0
        self.authentication_transcript = []
        self.read_calls = []
        self.open_failure: Exception | None = None
        self.authentication_failure: Exception | None = None
        self.read_result: object = "synthetic-result"
        self.read_failure: Exception | None = None

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
        if self.transport.read_failure is not None:
            raise self.transport.read_failure
        result = self.transport.read_result
        return result() if callable(result) else result

    async def close(self):
        self.transport.closed_sessions += 1
        return None


class _RawFetchTranscript:
    def __init__(self, *events: YandexMailRawMessageFetchEvent) -> None:
        assert events and isinstance(events[0], YandexMailRawMessageFetchStart)
        self.start = events[0]
        self.events = events[1:]
        self.index = 0
        self.failure_after: int | None = None
        self.failure: Exception | None = None

    def __repr__(self) -> str:
        return "_RawFetchTranscript(private_events=True)"

    async def read_start(self):
        return self.start

    async def read_next(self, *, max_bytes: int):
        del max_bytes
        if self.failure_after == self.index and self.failure is not None:
            raise self.failure
        if self.index == len(self.events):
            return None
        event = self.events[self.index]
        self.index += 1
        return event


def _raw_fetch_transcript(
    scope: MailboxReadScope,
    raw_message: bytes,
) -> _RawFetchTranscript:
    return _RawFetchTranscript(
        YandexMailRawMessageFetchStart(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=_id(11),
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            command_names=("EXAMINE", "UID FETCH"),
            fetch_data_item="BODY.PEEK[]",
            selected_uid_validity=101,
            fetched_uid=42,
            declared_octets=len(raw_message),
            envelope=b"private synthetic envelope",
            internal_date="2026-11-03T09:15:00Z",
            flags_before=("\\Flagged",),
        ),
        YandexMailRawMessageLiteralChunk(value=raw_message[:8]),
        YandexMailRawMessageLiteralChunk(value=raw_message[8:]),
        YandexMailRawMessageFetchEnd(
            command_names=("EXAMINE", "UID FETCH"),
            flags_after=("\\Flagged",),
            complete=True,
        ),
    )


def _command(
    scope: MailboxReadScope,
    operation: EmailOperation = EmailOperation.READ_MESSAGE_METADATA,
    *,
    location: UUID | None = _id(11),
    expected_uid_validity: int | None = None,
) -> MailboxReadCommand:
    return MailboxReadCommand(
        scope=scope,
        operation=operation,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=location,
        expected_uid_validity=expected_uid_validity,
    )


def _generation_observation(
    scope: MailboxReadScope, folder_reference: UUID, *, uid_validity: int, uid_next: int
) -> YandexMailFolderGenerationObservation:
    return YandexMailFolderGenerationObservation(
        complete=True,
        code=YandexMailFolderGenerationCode.COMPLETE,
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=folder_reference,
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=uid_validity,
        uid_next=uid_next,
        selection_command="EXAMINE",
        read_only=True,
    )


@pytest.mark.asyncio
async def test_uid_enumeration_uses_bounded_folder_read_and_returns_coverage() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)
    imap.read_result = YandexMailUidEnumerationPageTranscript(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=_id(11),
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        uid_validity=101,
        command_names=("UID SEARCH", "UID FETCH"),
        search_uids=(4, 7),
        fetched_messages=(
            YandexMailUidMessageMetadata(uid=4, flags=(), size_octets=120),
            YandexMailUidMessageMetadata(uid=7, flags=(), size_octets=240),
        ),
        response_bytes=512,
        search_complete=True,
        fetch_complete=True,
    )
    command = YandexMailUidEnumerationPageRequest(
        scope=scope,
        operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        generation=_generation_observation(
            scope, _id(11), uid_validity=101, uid_next=21
        ),
        start_uid=1,
        enumeration_max_items=10,
        enumeration_max_response_bytes=4096,
    )

    result = await composition.read(command, _context())

    assert result.code is YandexMailUidPageCode.COMPLETE
    request = imap.read_calls[0]
    assert isinstance(request, YandexMailUidImapReadRequest)
    assert request.operation is EmailOperation.ENUMERATE_FOLDER_UIDS
    assert request.selection_command == "EXAMINE"
    assert request.uid_validity == 101
    assert request.uid_start == 1
    assert request.uid_end == 10
    assert request.enumeration_max_response_bytes == 4096
    assert request.command_allowlist == ("EXAMINE", "UID SEARCH", "UID FETCH")
    assert request.fetch_data_items == "UID FLAGS RFC822.SIZE"
    assert result.continuation_uid == 11


@pytest.mark.asyncio
async def test_uid_pages_continue_contiguously_after_reconnect() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)

    async def read_page(start_uid: int, uids: tuple[int, ...]):
        imap.read_result = YandexMailUidEnumerationPageTranscript(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            folder_reference=_id(11),
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            uid_validity=101,
            command_names=("UID SEARCH", "UID FETCH"),
            search_uids=uids,
            fetched_messages=tuple(
                YandexMailUidMessageMetadata(uid=uid, size_octets=uid * 10)
                for uid in uids
            ),
            response_bytes=256,
            search_complete=True,
            fetch_complete=True,
        )
        command = YandexMailUidEnumerationPageRequest(
            scope=scope,
            operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            folder_or_label_reference=_id(11),
            generation=_generation_observation(
                scope, _id(11), uid_validity=101, uid_next=21
            ),
            start_uid=start_uid,
            enumeration_max_items=10,
            enumeration_max_response_bytes=4096,
        )
        return await composition.read(command, _context())

    first = await read_page(1, (4, 7))
    second = await read_page(first.continuation_uid, (12, 20))

    assert first.complete is True
    assert first.covered_uid_range == (1, 10)
    assert first.continuation_uid == 11
    assert second.complete is True
    assert second.covered_uid_range == (11, 20)
    assert second.continuation_uid is None
    assert len(imap.tls_calls) == 2
    assert imap.closed_sessions == 2


@pytest.mark.asyncio
async def test_uid_enumeration_cancellation_propagates_without_complete_page() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)
    imap.read_failure = asyncio.CancelledError()
    command = YandexMailUidEnumerationPageRequest(
        scope=scope,
        operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        generation=_generation_observation(
            scope, _id(11), uid_validity=101, uid_next=21
        ),
        start_uid=1,
        enumeration_max_items=10,
        enumeration_max_response_bytes=4096,
    )

    with pytest.raises(asyncio.CancelledError):
        await composition.read(command, _context())

    assert imap.closed_sessions == 1


@pytest.mark.asyncio
async def test_uid_enumeration_provider_failure_is_safe_and_has_no_cursor() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)
    imap.read_failure = RuntimeError(
        "private subject sender@example.test raw IMAP payload"
    )
    command = YandexMailUidEnumerationPageRequest(
        scope=scope,
        operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        generation=_generation_observation(
            scope, _id(11), uid_validity=101, uid_next=21
        ),
        start_uid=1,
        enumeration_max_items=10,
        enumeration_max_response_bytes=4096,
    )

    result = await composition.read(command, _context())

    assert result.complete is False
    assert result.code is YandexMailUidPageCode.SERVER_REJECTED
    assert result.continuation_uid is None
    assert "private subject" not in repr(result)
    assert "sender@example.test" not in repr(result)
    assert "raw IMAP payload" not in repr(result)


@pytest.mark.asyncio
async def test_uid_enumeration_without_exact_operation_authority_never_reaches_imap() -> None:
    scope = _scope(
        allowed_operations=(EmailProviderOperation.ENUMERATE_MAILBOX,)
    )
    composition, _, _, resolver, imap = _composition(scope=scope)
    command = YandexMailUidEnumerationPageRequest(
        scope=scope,
        operation=EmailOperation.ENUMERATE_FOLDER_UIDS,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        generation=_generation_observation(
            scope, _id(11), uid_validity=101, uid_next=21
        ),
        start_uid=1,
        enumeration_max_items=10,
        enumeration_max_response_bytes=4096,
    )

    with pytest.raises(MailboxReadDeniedError):
        await composition.read(command, _context())

    assert resolver.calls == 0
    assert imap.tls_calls == []

def _raw_message_command(scope: MailboxReadScope) -> YandexMailRawMessageFetchCommand:
    return YandexMailRawMessageFetchCommand(
        scope=scope,
        operation=EmailOperation.READ_RAW_MESSAGE,
        mailbox_subject_reference=scope.mailbox_subject_reference,
        folder_or_label_reference=_id(11),
        message_id="42",
        uid_validity=101,
        uid=42,
        max_message_bytes=1024,
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
async def test_folder_generation_observation_issues_only_examine_and_retains_evidence() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)
    imap.read_result = YandexMailFolderExamineTranscript(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=_id(11),
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        command_names=("EXAMINE",),
        response_lines=(
            b"* OK [UIDVALIDITY 101] synthetic generation",
            b"* OK [UIDNEXT 42] synthetic next UID",
            b"A001 OK [READ-ONLY] EXAMINE complete",
        ),
    )

    result = await composition.read(
        _command(scope, EmailOperation.OBSERVE_FOLDER_GENERATION), _context()
    )

    assert result.code is YandexMailFolderGenerationCode.COMPLETE
    assert result.connection_id == scope.connection_id
    assert result.authorization_generation == scope.authorization_generation
    assert result.configuration_generation == scope.configuration_generation
    assert result.uid_validity == 101
    assert result.uid_next == 42
    request = imap.read_calls[0]
    assert request.operation is EmailOperation.OBSERVE_FOLDER_GENERATION
    assert request.selection_command == "EXAMINE"
    assert request.folder_reference == _id(11)
    assert request.security.read_only is True


@pytest.mark.asyncio
async def test_folder_generation_raw_server_failure_is_typed_and_disclosure_safe() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)
    imap.read_failure = RuntimeError(
        "private subject sender@example.test raw provider payload"
    )

    result = await composition.read(
        _command(scope, EmailOperation.OBSERVE_FOLDER_GENERATION), _context()
    )

    assert result.complete is False
    assert result.code is YandexMailFolderGenerationCode.SERVER_REJECTED
    assert "private subject" not in repr(result)
    assert "sender@example.test" not in repr(result)
    assert "raw provider payload" not in repr(result)


@pytest.mark.asyncio
async def test_folder_generation_reset_is_typed_when_prior_generation_is_supplied() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)
    imap.read_result = YandexMailFolderExamineTranscript(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=_id(11),
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        command_names=("EXAMINE",),
        response_lines=(
            b"* OK [UIDVALIDITY 102] reset generation",
            b"* OK [UIDNEXT 1] next available UID",
            b"A001 OK [READ-ONLY] EXAMINE complete",
        ),
    )

    result = await composition.read(
        _command(
            scope,
            EmailOperation.OBSERVE_FOLDER_GENERATION,
            expected_uid_validity=101,
        ),
        _context(),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderGenerationCode.UIDVALIDITY_CHANGED
    assert result.uid_validity == 102
    assert result.uid_next == 1
    assert imap.read_calls[0].expected_uid_validity == 101


@pytest.mark.asyncio
async def test_folder_generation_is_reobserved_after_reconnect() -> None:
    scope = _scope()
    composition, _, _, _, imap = _composition(scope=scope)
    imap.read_result = YandexMailFolderExamineTranscript(
        workspace_id=scope.workspace_id,
        environment_id=scope.environment_id,
        connection_id=scope.connection_id,
        connection_version_id=scope.connection_version_id,
        mailbox_binding_id=scope.mailbox_binding_id,
        folder_reference=_id(11),
        authorization_generation=scope.authorization_generation,
        configuration_generation=scope.configuration_generation,
        scope_generation=scope.scope_generation,
        command_names=("EXAMINE",),
        response_lines=(
            b"* OK [UIDVALIDITY 101] synthetic generation",
            b"* OK [UIDNEXT 42] synthetic next UID",
            b"A001 OK [READ-ONLY] EXAMINE complete",
        ),
    )

    first = await composition.read(
        _command(scope, EmailOperation.OBSERVE_FOLDER_GENERATION), _context()
    )
    after_reconnect = await composition.read(
        _command(scope, EmailOperation.OBSERVE_FOLDER_GENERATION), _context()
    )

    assert first == after_reconnect
    assert first.code is YandexMailFolderGenerationCode.COMPLETE
    assert len(imap.tls_calls) == 2
    assert len(imap.read_calls) == 2
    assert imap.closed_sessions == 2


@pytest.mark.asyncio
async def test_raw_message_composition_issues_bounded_body_peek_and_preserves_seen() -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    composition, _, _, _, imap = _composition(
        scope=scope,
        connection=connection,
        credential=_credential(connection, scope),
    )
    raw_message = b"Subject: Synthetic\r\n\r\nbody\r\n"
    imap.read_result = lambda: _raw_fetch_transcript(scope, raw_message)

    result = await composition.read(_raw_message_command(scope), _context())

    assert result.code is YandexMailRawMessageFetchCode.COMPLETE
    assert result.raw_bytes == raw_message
    assert result.seen_preserved is True
    request = imap.read_calls[0]
    assert request.selection_command == "EXAMINE"
    assert request.fetch_command == "UID FETCH"
    assert request.fetch_data_item == "BODY.PEEK[]"
    assert request.expected_uid_validity == 101
    assert request.message_uid == 42
    assert request.max_message_bytes == 1024
    assert request.mutation_commands == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("resolver", "tls", "authentication", "read"))
async def test_raw_message_provider_outage_is_typed_and_disclosure_safe(
    failure_stage: str,
) -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    composition, _, _, resolver, imap = _composition(
        scope=scope,
        connection=connection,
        credential=_credential(connection, scope),
    )
    failure = RuntimeError("private provider outage")
    if failure_stage == "resolver":
        resolver.failure = failure
    elif failure_stage == "tls":
        imap.open_failure = failure
    elif failure_stage == "authentication":
        imap.authentication_failure = failure
    else:
        imap.read_failure = failure

    result = await composition.read(_raw_message_command(scope), _context())

    assert result.code is YandexMailRawMessageFetchCode.PROVIDER_UNAVAILABLE
    assert result.complete is False
    assert str(failure) not in repr(result)
    assert _TOKEN not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("resolver", "tls", "authentication", "read"))
async def test_raw_message_authorization_loss_is_typed_and_disclosure_safe(
    failure_stage: str,
) -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    composition, _, _, resolver, imap = _composition(
        scope=scope,
        connection=connection,
        credential=_credential(connection, scope),
    )
    failure = PermissionError("revoked private token")
    if failure_stage == "resolver":
        resolver.failure = failure
    elif failure_stage == "tls":
        imap.open_failure = failure
    elif failure_stage == "authentication":
        imap.authentication_failure = failure
    else:
        imap.read_failure = failure

    result = await composition.read(_raw_message_command(scope), _context())

    assert result.code is YandexMailRawMessageFetchCode.AUTHORIZATION_LOST
    assert result.complete is False
    assert str(failure) not in repr(result)
    assert _TOKEN not in repr(result)


@pytest.mark.asyncio
async def test_raw_message_reconnect_revalidates_generation_and_uid() -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    composition, _, _, _, imap = _composition(
        scope=scope,
        connection=connection,
        credential=_credential(connection, scope),
    )
    raw_message = b"synthetic\x00message"
    imap.read_result = lambda: _raw_fetch_transcript(scope, raw_message)

    first = await composition.read(_raw_message_command(scope), _context())
    after_reconnect = await composition.read(_raw_message_command(scope), _context())

    assert first == after_reconnect
    assert first.code is YandexMailRawMessageFetchCode.COMPLETE
    assert len(imap.tls_calls) == 2
    assert len(imap.read_calls) == 2
    assert all(request.expected_uid_validity == 101 for request in imap.read_calls)
    assert all(request.message_uid == 42 for request in imap.read_calls)
    assert imap.closed_sessions == 2


@pytest.mark.asyncio
async def test_raw_message_retry_after_partial_failure_returns_stable_single_read() -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    composition, _, _, _, imap = _composition(
        scope=scope,
        connection=connection,
        credential=_credential(connection, scope),
    )
    raw_message = b"stable synthetic message"
    partial_transcript = _raw_fetch_transcript(scope, raw_message)
    partial_transcript.failure_after = 1
    partial_transcript.failure = RuntimeError("partial private provider failure")
    imap.read_result = partial_transcript

    partial = await composition.read(_raw_message_command(scope), _context())
    imap.read_result = lambda: _raw_fetch_transcript(scope, raw_message)
    retried = await composition.read(_raw_message_command(scope), _context())
    replayed = await composition.read(_raw_message_command(scope), _context())

    assert partial.code is YandexMailRawMessageFetchCode.PROVIDER_UNAVAILABLE
    assert partial.raw_bytes is None
    assert "stable synthetic message" not in repr(partial)
    assert retried == replayed
    assert retried.raw_bytes == raw_message
    assert retried.code is YandexMailRawMessageFetchCode.COMPLETE
    assert len(imap.read_calls) == 3
    assert imap.closed_sessions == 3


@pytest.mark.asyncio
async def test_raw_message_cross_workspace_request_is_denied_before_imap() -> None:
    scope = _scope(workspace_id=_id(99))
    connection = _connection(
        workspace_id=_id(99),
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        ),
    )
    composition, _, _, resolver, imap = _composition(
        scope=scope,
        connection=connection,
        credential=_credential(connection, scope),
    )

    with pytest.raises(MailboxReadDeniedError) as error:
        await composition.read(_raw_message_command(scope), _context())

    assert str(error.value) == "Mailbox read denied."
    assert resolver.calls == 0
    assert imap.tls_calls == []


@pytest.mark.asyncio
async def test_raw_message_stale_resolved_credential_is_typed_before_imap() -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    stale = _credential(connection, scope, authorization_generation=99)
    composition, _, _, _, imap = _composition(
        scope=scope,
        connection=connection,
        credential=stale,
    )

    result = await composition.read(_raw_message_command(scope), _context())

    assert result.code is YandexMailRawMessageFetchCode.AUTHORIZATION_LOST
    assert result.complete is False
    assert imap.tls_calls == []


@pytest.mark.asyncio
async def test_raw_message_malformed_provider_response_is_typed_and_safe() -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.RAW_MESSAGE_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(32)),
            ),
        )
    )
    composition, _, _, _, imap = _composition(
        scope=scope,
        connection=connection,
        credential=_credential(connection, scope),
    )
    imap.read_result = b"private malformed provider payload"

    result = await composition.read(_raw_message_command(scope), _context())

    assert result.code is YandexMailRawMessageFetchCode.MALFORMED_RESPONSE
    assert result.complete is False
    assert "private malformed" not in repr(result)


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


class _FolderReader:
    def __init__(self, listing: object) -> None:
        self.listing = listing
        self.calls = 0
        self.last_command: MailboxReadCommand | None = None

    async def read(self, command, context):
        del context
        self.calls += 1
        self.last_command = command
        if isinstance(self.listing, Exception):
            raise self.listing
        return self.listing


class _FolderBindings:
    def __init__(
        self,
        scope: MailboxReadScope,
        *,
        wrong_generation: bool = False,
        records: tuple[YandexMailFolderBinding, ...] | None = None,
    ) -> None:
        self.scope = scope
        self.wrong_generation = wrong_generation
        self.records = records

    async def resolve_exact(self, scope, context):
        del scope, context
        if self.records is not None:
            return self.records
        return (
            _folder_binding(self.scope, _id(11), "INBOX"),
            _folder_binding(
                self.scope,
                _id(12),
                "Excluded/Secret",
                scope_generation=99 if self.wrong_generation else self.scope.scope_generation,
            ),
        )


def _folder_binding(
    scope: MailboxReadScope,
    reference: UUID,
    name: str,
    **changes: object,
) -> YandexMailFolderBinding:
    values: dict[str, object] = {
        "workspace_id": scope.workspace_id,
        "environment_id": scope.environment_id,
        "connection_id": scope.connection_id,
        "connection_version_id": scope.connection_version_id,
        "mailbox_binding_id": scope.mailbox_binding_id,
        "mailbox_subject_reference": scope.mailbox_subject_reference,
        "folder_reference": reference,
        "authorization_generation": scope.authorization_generation,
        "configuration_generation": scope.configuration_generation,
        "scope_generation": scope.scope_generation,
        "folder_name": name,
    }
    values.update(changes)
    return YandexMailFolderBinding(**values)


def _modified_utf7(value: str) -> bytes:
    import base64

    encoded = base64.b64encode(value.encode("utf-16-be")).decode("ascii")
    return ("&" + encoded.rstrip("=").replace("/", ",") + "-").encode("ascii")


@pytest.mark.asyncio
async def test_yandex_folder_discovery_decodes_nested_unicode_and_returns_only_approved_refs() -> None:
    scope = _scope(
        allowed_folder_references=(_id(11), _id(13)),
        excluded_folder_references=(_id(12),),
    )
    listing = YandexMailFolderListing(
        folders=(
            YandexMailListedFolder(flags=b"(\\HasNoChildren)", delimiter=b"/", name=b"INBOX"),
            YandexMailListedFolder(
                flags=b"()", delimiter=b"/", name=_modified_utf7("Customers/日本語")
            ),
            YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"INBOX"),
            YandexMailListedFolder(
                flags=b"()", delimiter=b"/", name=b"Excluded/Secret"
            ),
        ),
        complete=True,
    )
    reader = _FolderReader(listing)
    bindings = _FolderBindings(
        scope,
        records=(
            _folder_binding(scope, _id(11), "INBOX"),
            _folder_binding(scope, _id(13), "Customers/日本語"),
            _folder_binding(scope, _id(12), "Excluded/Secret"),
        ),
    )
    result = await discover_approved_yandex_mail_folders(
        reader=reader,
        bindings=bindings,
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
    )

    assert result.complete is True
    assert result.code is YandexMailFolderDiscoveryCode.COMPLETE
    assert result.discovered_count == 3
    assert result.approved_count == 2
    assert result.folders == (_id(11), _id(13))
    assert "Customers" not in repr(result)
    assert "Excluded" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("listing", "code"),
    (
        (
            YandexMailFolderListing(
                folders=(YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"INBOX"),),
                complete=False,
            ),
            YandexMailFolderDiscoveryCode.PARTIAL_RESPONSE,
        ),
        (
            YandexMailFolderListing(
                folders=(YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"INBOX&"),),
                complete=True,
            ),
            YandexMailFolderDiscoveryCode.MALFORMED_RESPONSE,
        ),
        (
            YandexMailFolderListing(
                folders=(YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"Other"),),
                complete=True,
            ),
            YandexMailFolderDiscoveryCode.APPROVED_FOLDER_MISSING,
        ),
    ),
)
async def test_incomplete_or_malformed_folder_discovery_never_claims_complete(
    listing: YandexMailFolderListing,
    code: YandexMailFolderDiscoveryCode,
) -> None:
    scope = _scope()
    result = await discover_approved_yandex_mail_folders(
        reader=_FolderReader(listing),
        bindings=_FolderBindings(scope),
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
    )

    assert result.complete is False
    assert result.code is code
    assert result.folders == ()


@pytest.mark.asyncio
async def test_folder_discovery_provider_failure_returns_safe_incomplete_diagnostic() -> None:
    scope = _scope()
    secret_server_line = "private folder raw server response"
    result = await discover_approved_yandex_mail_folders(
        reader=_FolderReader(RuntimeError(secret_server_line)),
        bindings=_FolderBindings(scope),
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderDiscoveryCode.DISCOVERY_UNAVAILABLE
    assert secret_server_line not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (RuntimeError("outage"), PermissionError("auth lost")))
async def test_folder_discovery_outage_or_auth_loss_cannot_claim_complete(
    failure: Exception,
) -> None:
    scope = _scope()
    result = await discover_approved_yandex_mail_folders(
        reader=_FolderReader(failure),
        bindings=_FolderBindings(scope),
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderDiscoveryCode.DISCOVERY_UNAVAILABLE
    assert str(failure) not in repr(result)


@pytest.mark.asyncio
async def test_folder_discovery_response_limit_never_claims_complete() -> None:
    scope = _scope()
    reader = _FolderReader(
        YandexMailFolderListing(
            folders=(
                YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"INBOX"),
                YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"INBOX"),
            ),
            complete=True,
        )
    )
    result = await discover_approved_yandex_mail_folders(
        reader=reader,
        bindings=_FolderBindings(scope),
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
        profile=YandexMailFolderDiscoveryProfile(max_folders=1),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderDiscoveryCode.RESPONSE_LIMIT_EXCEEDED
    assert result.folders == ()
    assert reader.last_command is not None
    assert reader.last_command.enumeration_max_items == 1
    assert reader.last_command.enumeration_max_response_bytes == 1_048_576


@pytest.mark.asyncio
async def test_folder_discovery_byte_limit_is_sent_before_listing_is_returned() -> None:
    scope = _scope()
    reader = _FolderReader(
        YandexMailFolderListing(
            folders=(YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"A" * 1100),),
            complete=True,
        )
    )
    result = await discover_approved_yandex_mail_folders(
        reader=reader,
        bindings=_FolderBindings(scope),
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
        profile=YandexMailFolderDiscoveryProfile(max_response_bytes=1024),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderDiscoveryCode.RESPONSE_LIMIT_EXCEEDED
    assert reader.last_command is not None
    assert reader.last_command.enumeration_max_response_bytes == 1024


@pytest.mark.asyncio
async def test_yandex_imap_request_receives_limits_before_folder_listing() -> None:
    scope = _scope()
    connection = _connection(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.MAILBOX_ENUMERATION,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(31)),
            ),
        )
    )
    credential = _credential(connection, scope)
    composition, _, _, _, imap = _composition(
        scope=scope, connection=connection, credential=credential
    )
    imap.read_result = YandexMailFolderListing(
        folders=(YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"INBOX"),),
        complete=True,
    )
    result = await discover_approved_yandex_mail_folders(
        reader=composition,
        bindings=_FolderBindings(scope),
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
    )

    assert result.complete is True
    assert imap.read_calls[0].enumeration_max_items == 500
    assert imap.read_calls[0].enumeration_max_response_bytes == 1_048_576


@pytest.mark.asyncio
async def test_duplicate_approved_folder_name_is_ambiguous_and_not_admitted() -> None:
    scope = _scope(allowed_folder_references=(_id(11), _id(13)))
    bindings = _FolderBindings(
        scope,
        records=(
            _folder_binding(scope, _id(11), "INBOX"),
            _folder_binding(scope, _id(13), "INBOX"),
        ),
    )

    result = await discover_approved_yandex_mail_folders(
        reader=_FolderReader(
            YandexMailFolderListing(
                folders=(YandexMailListedFolder(flags=b"()", delimiter=b"/", name=b"INBOX"),),
                complete=True,
            )
        ),
        bindings=bindings,
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderDiscoveryCode.APPROVED_FOLDER_AMBIGUOUS
    assert result.folders == ()


@pytest.mark.asyncio
async def test_missing_binding_for_allowed_folder_never_starts_provider_discovery() -> None:
    scope = _scope(allowed_folder_references=(_id(11), _id(13)))
    bindings = _FolderBindings(
        scope,
        records=(_folder_binding(scope, _id(11), "INBOX"),),
    )
    reader = _FolderReader(YandexMailFolderListing(folders=(), complete=True))

    result = await discover_approved_yandex_mail_folders(
        reader=reader,
        bindings=bindings,
        command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
        context=_context(),
    )

    assert result.complete is False
    assert result.code is YandexMailFolderDiscoveryCode.APPROVED_FOLDER_MISSING
    assert result.folders == ()
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_wrong_folder_binding_generation_is_rejected_before_provider_enumeration() -> None:
    scope = _scope()
    reader = _FolderReader(YandexMailFolderListing(folders=(), complete=True))
    with pytest.raises(YandexMailFolderDiscoveryDeniedError):
        await discover_approved_yandex_mail_folders(
            reader=reader,
            bindings=_FolderBindings(scope, wrong_generation=True),
            command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
            context=_context(),
        )

    assert reader.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding_changes",
    (
        {"workspace_id": _id(99)},
        {"mailbox_binding_id": _id(99)},
        {"mailbox_subject_reference": _id(99)},
        {"connection_version_id": _id(99)},
    ),
)
async def test_wrong_workspace_or_mailbox_binding_is_denied_before_enumeration(
    binding_changes: dict[str, object],
) -> None:
    scope = _scope()
    bindings = _FolderBindings(
        scope,
        records=(_folder_binding(scope, _id(11), "INBOX", **binding_changes),),
    )
    reader = _FolderReader(YandexMailFolderListing(folders=(), complete=True))

    with pytest.raises(YandexMailFolderDiscoveryDeniedError):
        await discover_approved_yandex_mail_folders(
            reader=reader,
            bindings=bindings,
            command=_command(scope, EmailOperation.ENUMERATE_MAILBOX, location=None),
            context=_context(),
        )

    assert reader.calls == 0
