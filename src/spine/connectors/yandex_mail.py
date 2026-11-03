"""Offline composition boundary for read-only Yandex Mail IMAP access."""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import Field, SecretStr, StrictInt

from spine.application.connections.email_read import MailboxReadCommand
from spine.application.connections.email_read import (
    MailboxReadAdmission,
    MailboxScopeRepository,
)
from spine.application.connections.provider import ProviderConnectionRepository
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope
from spine.domain.connections.provider import (
    ProviderCapabilityKind,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
    OpaqueProviderReference,
)
from spine.connectors.yandex_mail_generation import (
    YandexMailFolderExamineTranscript,
    YandexMailFolderGenerationCode,
    failed_yandex_mail_folder_generation_observation,
    observe_yandex_mail_folder_generation,
)
from spine.connectors.yandex_mail_uid_enumeration import (
    YandexMailUidEnumerationPageRequest,
    YandexMailUidEnumerationPageTranscript,
    YandexMailUidPageCode,
    failed_yandex_mail_uid_page,
    normalize_yandex_mail_uid_page,
)
from spine.connectors.yandex_mail_message import (
    YandexMailRawMessageFetchCode,
    YandexMailRawMessageFetchCommand,
    YandexMailRawMessageFetchResult,
    YandexMailRawMessageFetchTranscript,
    failed_yandex_mail_raw_message_fetch,
    fetch_yandex_mail_raw_message,
)


YANDEX_MAIL_IMAP_HOST = "imap.yandex.ru"
YANDEX_MAIL_IMAP_PORT = 993
YANDEX_MAIL_OAUTH_SCOPE = "mail:imap_ro"
ReadResultT = TypeVar("ReadResultT")


class YandexMailAuthorityDeniedError(PermissionError):
    """Safe denial that carries no provider, mailbox, or credential details."""

    def __init__(self) -> None:
        super().__init__("Yandex Mail read denied.")


@dataclass(frozen=True, slots=True, repr=False)
class YandexMailOAuthCredential:
    """Resolved secret material, pinned to one approved connection generation."""

    access_token: SecretStr = field(repr=False)
    mailbox_login: SecretStr = field(repr=False)
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    credential_reference: UUID
    grant_reference: UUID
    authorization_generation: int
    configuration_generation: int
    approved_oauth_scopes: tuple[str, ...]


class YandexMailImapSecurity(DefinitionModel):
    """Immutable transport requirements for an admitted Yandex Mail read."""

    schema_version: Literal["yandex-mail-imap-security:v1"] = (
        "yandex-mail-imap-security:v1"
    )
    host: Literal["imap.yandex.ru"] = YANDEX_MAIL_IMAP_HOST
    port: Literal[993] = YANDEX_MAIL_IMAP_PORT
    tls_required: Literal[True] = True
    verify_server_identity: Literal[True] = True
    auth_mechanism: Literal["XOAUTH2"] = "XOAUTH2"
    read_only: Literal[True] = True


class YandexMailImapReadRequest(DefinitionModel):
    """Metadata-only request passed to the fakeable IMAP boundary."""

    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    mailbox_binding_id: UUID
    mailbox_subject_reference: UUID
    resource_scope_reference: UUID
    authorization_generation: int
    configuration_generation: int
    scope_generation: int
    operation: EmailOperation
    folder_reference: UUID | None
    schema_version: Literal["yandex-mail-imap-read-request:v1"] = (
        "yandex-mail-imap-read-request:v1"
    )
    enumeration_max_items: int | None = None
    enumeration_max_response_bytes: int | None = None
    expected_uid_validity: int | None = None
    selection_command: Literal["EXAMINE"] | None = None
    fetch_command: Literal["UID FETCH"] | None = None
    fetch_data_item: Literal["BODY.PEEK[]"] | None = None
    message_uid: int | None = Field(default=None, ge=1, le=2**32 - 1)
    max_message_bytes: int | None = Field(default=None, ge=1)
    mutation_commands: tuple[()] = ()
    security: YandexMailImapSecurity = YandexMailImapSecurity()


class YandexMailUidImapReadRequest(YandexMailImapReadRequest):
    """Versioned, bounded IMAP request for one UID SEARCH/FETCH interval."""

    schema_version: Literal["yandex-mail-uid-enumeration-imap-read-request:v1"] = (
        "yandex-mail-uid-enumeration-imap-read-request:v1"
    )
    uid_validity: StrictInt = Field(ge=1, le=2**32 - 1)
    uid_next: StrictInt = Field(ge=1, le=2**32 - 1)
    uid_start: StrictInt = Field(ge=1, le=2**32 - 1)
    uid_end: StrictInt = Field(ge=0, le=2**32 - 1)
    selection_command: Literal["EXAMINE"] = "EXAMINE"
    command_allowlist: tuple[Literal["EXAMINE", "UID SEARCH", "UID FETCH"], ...] = (
        "EXAMINE",
        "UID SEARCH",
        "UID FETCH",
    )
    fetch_data_items: Literal["UID FLAGS RFC822.SIZE"] = "UID FLAGS RFC822.SIZE"


class YandexMailCredentialResolver(Protocol):
    async def resolve(
        self,
        connection: ProviderConnection,
        scope: MailboxReadScope,
    ) -> YandexMailOAuthCredential: ...


class YandexMailImapSession(Protocol[ReadResultT]):
    async def authenticate_xoauth2(
        self,
        *,
        mailbox_login: SecretStr,
        access_token: SecretStr,
    ) -> None: ...

    async def read(
        self, request: YandexMailImapReadRequest | YandexMailUidImapReadRequest
    ) -> ReadResultT: ...

    async def close(self) -> None: ...


class YandexMailImapTransport(Protocol[ReadResultT]):
    async def open_tls(
        self,
        *,
        host: str,
        port: int,
        server_hostname: str,
        ssl_context: ssl.SSLContext,
    ) -> YandexMailImapSession[ReadResultT]: ...


class _YandexMailProviderAdapter:
    """Resolve a scoped OAuth grant, then invoke only the read-only IMAP port."""

    def __init__(
        self,
        *,
        credentials: YandexMailCredentialResolver,
        imap: YandexMailImapTransport[ReadResultT],
    ) -> None:
        self._credentials = credentials
        self._imap = imap

    async def read(
        self,
        connection: ProviderConnection,
        command: MailboxReadCommand,
    ) -> ReadResultT:
        scope = command.scope
        required_capability = _required_capability(command.operation)
        if not _connection_matches_scope(connection, command, required_capability):
            raise YandexMailAuthorityDeniedError()
        if command.operation is EmailOperation.ENUMERATE_FOLDER_UIDS and not isinstance(
            command, YandexMailUidEnumerationPageRequest
        ):
            raise YandexMailAuthorityDeniedError()

        try:
            credential = await self._credentials.resolve(connection, scope)
        except Exception as error:
            if isinstance(command, YandexMailRawMessageFetchCommand):
                return _raw_fetch_error(command, error)  # type: ignore[return-value]
            raise YandexMailAuthorityDeniedError() from None
        if not isinstance(credential, YandexMailOAuthCredential) or not (
            _credential_matches_scope(credential, connection, scope)
        ):
            if isinstance(command, YandexMailRawMessageFetchCommand):
                return _raw_fetch_failure(
                    command,
                    YandexMailRawMessageFetchCode.AUTHORIZATION_LOST,
                )  # type: ignore[return-value]
            raise YandexMailAuthorityDeniedError()

        request_values = dict(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            mailbox_binding_id=scope.mailbox_binding_id,
            mailbox_subject_reference=scope.mailbox_subject_reference,
            resource_scope_reference=scope.resource_scope_reference,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            operation=command.operation,
            folder_reference=command.folder_or_label_reference,
            selection_command=(
                "EXAMINE"
                if command.operation
                in (
                    EmailOperation.OBSERVE_FOLDER_GENERATION,
                    EmailOperation.ENUMERATE_FOLDER_UIDS,
                    EmailOperation.READ_RAW_MESSAGE,
                )
                else None
            ),
            enumeration_max_items=command.enumeration_max_items,
            enumeration_max_response_bytes=command.enumeration_max_response_bytes,
            expected_uid_validity=(
                command.uid_validity
                if isinstance(command, YandexMailRawMessageFetchCommand)
                else command.expected_uid_validity
            ),
            fetch_command=(
                "UID FETCH"
                if isinstance(command, YandexMailRawMessageFetchCommand)
                else None
            ),
            fetch_data_item=(
                "BODY.PEEK[]"
                if isinstance(command, YandexMailRawMessageFetchCommand)
                else None
            ),
            message_uid=(
                command.uid
                if isinstance(command, YandexMailRawMessageFetchCommand)
                else None
            ),
            max_message_bytes=(
                command.max_message_bytes
                if isinstance(command, YandexMailRawMessageFetchCommand)
                else None
            ),
        )
        if isinstance(command, YandexMailUidEnumerationPageRequest):
            request = YandexMailUidImapReadRequest(
                **request_values,
                uid_validity=command.uid_validity,
                uid_next=command.uid_next,
                uid_start=command.start_uid,
                uid_end=command.end_uid,
            )
        else:
            request = YandexMailImapReadRequest(**request_values)
        ssl_context = ssl.create_default_context()
        if (
            not ssl_context.check_hostname
            or ssl_context.verify_mode is not ssl.CERT_REQUIRED
        ):
            raise YandexMailAuthorityDeniedError()
        session: YandexMailImapSession[ReadResultT] | None = None
        try:
            session = await self._imap.open_tls(
                host=YANDEX_MAIL_IMAP_HOST,
                port=YANDEX_MAIL_IMAP_PORT,
                server_hostname=YANDEX_MAIL_IMAP_HOST,
                ssl_context=ssl_context,
            )
            await session.authenticate_xoauth2(
                mailbox_login=credential.mailbox_login,
                access_token=credential.access_token,
            )
            try:
                result = await session.read(request)
            except Exception as error:
                if command.operation is EmailOperation.OBSERVE_FOLDER_GENERATION:
                    return failed_yandex_mail_folder_generation_observation(
                        scope=scope,
                        code=YandexMailFolderGenerationCode.SERVER_REJECTED,
                    )  # type: ignore[return-value]
                if command.operation is EmailOperation.ENUMERATE_FOLDER_UIDS:
                    assert isinstance(command, YandexMailUidEnumerationPageRequest)
                    return failed_yandex_mail_uid_page(
                        request=command,
                        code=YandexMailUidPageCode.SERVER_REJECTED,
                    )  # type: ignore[return-value]
                if isinstance(command, YandexMailRawMessageFetchCommand):
                    return _raw_fetch_error(command, error)  # type: ignore[return-value]
                raise
            if command.operation is EmailOperation.OBSERVE_FOLDER_GENERATION:
                if not isinstance(result, YandexMailFolderExamineTranscript):
                    return failed_yandex_mail_folder_generation_observation(
                        scope=scope,
                        code=YandexMailFolderGenerationCode.MALFORMED_RESPONSE,
                    )  # type: ignore[return-value]
                assert command.folder_or_label_reference is not None
                return observe_yandex_mail_folder_generation(
                    scope=scope,
                    folder_reference=command.folder_or_label_reference,
                    transcript=result,
                    expected_uid_validity=command.expected_uid_validity,
                )  # type: ignore[return-value]
            if command.operation is EmailOperation.ENUMERATE_FOLDER_UIDS:
                assert isinstance(command, YandexMailUidEnumerationPageRequest)
                if not isinstance(result, YandexMailUidEnumerationPageTranscript):
                    return failed_yandex_mail_uid_page(
                        request=command,
                        code=YandexMailUidPageCode.MALFORMED_RESPONSE,
                    )  # type: ignore[return-value]
                return normalize_yandex_mail_uid_page(
                    request=command,
                    transcript=result,
                )  # type: ignore[return-value]
            if isinstance(command, YandexMailRawMessageFetchCommand):
                assert command.folder_or_label_reference is not None
                if not isinstance(result, YandexMailRawMessageFetchTranscript):
                    return _raw_fetch_failure(
                        command,
                        YandexMailRawMessageFetchCode.MALFORMED_RESPONSE,
                    )  # type: ignore[return-value]
                return await fetch_yandex_mail_raw_message(
                    scope=scope,
                    folder_reference=command.folder_or_label_reference,
                    uid_validity=command.uid_validity,
                    uid=command.uid,
                    max_message_bytes=command.max_message_bytes,
                    transcript=result,
                )  # type: ignore[return-value]
            return result
        except Exception as error:
            if isinstance(command, YandexMailRawMessageFetchCommand):
                return _raw_fetch_error(command, error)  # type: ignore[return-value]
            raise YandexMailAuthorityDeniedError() from None
        finally:
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass


def compose_yandex_mail_reader(
    *,
    scopes: MailboxScopeRepository,
    connections: ProviderConnectionRepository,
    credentials: YandexMailCredentialResolver,
    imap: YandexMailImapTransport[ReadResultT],
) -> MailboxReadAdmission[ReadResultT]:
    """Build the fakeable, tenant-scoped admission and IMAP provider path."""

    provider = _YandexMailProviderAdapter(credentials=credentials, imap=imap)
    return MailboxReadAdmission(
        scopes=scopes,
        connections=connections,
        provider=provider,
    )


def _required_capability(operation: EmailOperation) -> ProviderCapabilityKind | None:
    return {
        EmailOperation.ENUMERATE_MAILBOX: ProviderCapabilityKind.MAILBOX_ENUMERATION,
        EmailOperation.ENUMERATE_FOLDER_UIDS: ProviderCapabilityKind.MAILBOX_ENUMERATION,
        EmailOperation.READ_MESSAGE_METADATA: ProviderCapabilityKind.MESSAGE_METADATA_READ,
        EmailOperation.READ_RAW_MESSAGE: ProviderCapabilityKind.RAW_MESSAGE_READ,
        EmailOperation.READ_ATTACHMENT: ProviderCapabilityKind.ATTACHMENT_READ,
        EmailOperation.SYNCHRONIZE: ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
        EmailOperation.OBSERVE_FOLDER_GENERATION: ProviderCapabilityKind.MAILBOX_ENUMERATION,
    }.get(operation)


def _connection_matches_scope(
    connection: ProviderConnection,
    command: MailboxReadCommand,
    required_capability: ProviderCapabilityKind | None,
) -> bool:
    scope = command.scope
    is_enumeration = command.operation is EmailOperation.ENUMERATE_MAILBOX
    is_uid_enumeration = command.operation is EmailOperation.ENUMERATE_FOLDER_UIDS
    return (
        required_capability is not None
        and connection.status is ProviderConnectionStatus.ACTIVE
        and connection.provider_kind is ProviderKind.YANDEX_MAIL
        and scope.provider_kind == ProviderKind.YANDEX_MAIL.value
        and connection.workspace_id == scope.workspace_id
        and connection.environment_id == scope.environment_id
        and connection.connection_id == scope.connection_id
        and connection.connection_version_id == scope.connection_version_id
        and connection.authorization_generation == scope.authorization_generation
        and connection.configuration_generation == scope.configuration_generation
        and OpaqueProviderReference(value=scope.resource_scope_reference)
        in connection.approved_resource_scope_references
        and command.mailbox_subject_reference == scope.mailbox_subject_reference
        and scope.allows(command.operation)
        and (
            not is_enumeration
            or (
                command.enumeration_max_items is not None
                and command.enumeration_max_response_bytes is not None
            )
        )
        and (
            not is_uid_enumeration
            or (
                isinstance(command, YandexMailUidEnumerationPageRequest)
                and command.enumeration_max_items is not None
                and command.enumeration_max_response_bytes is not None
            )
        )
        and (
            command.folder_or_label_reference is None
            if is_enumeration
            else scope.allows_location(command.folder_or_label_reference)
        )
        and connection.supports(required_capability)
    )


def _credential_matches_scope(
    credential: YandexMailOAuthCredential,
    connection: ProviderConnection,
    scope: MailboxReadScope,
) -> bool:
    return (
        isinstance(credential.access_token, SecretStr)
        and isinstance(credential.mailbox_login, SecretStr)
        and bool(credential.access_token.get_secret_value())
        and bool(credential.mailbox_login.get_secret_value())
        and credential.workspace_id == scope.workspace_id
        and credential.environment_id == scope.environment_id
        and credential.connection_id == scope.connection_id
        and credential.connection_version_id == scope.connection_version_id
        and credential.credential_reference == connection.credential_reference.value
        and credential.grant_reference == connection.grant_reference.value
        and credential.authorization_generation == scope.authorization_generation
        and credential.configuration_generation == scope.configuration_generation
        and credential.approved_oauth_scopes == (YANDEX_MAIL_OAUTH_SCOPE,)
    )


def _raw_fetch_error(
    command: YandexMailRawMessageFetchCommand,
    error: Exception,
) -> YandexMailRawMessageFetchResult:
    return _raw_fetch_failure(
        command,
        (
            YandexMailRawMessageFetchCode.AUTHORIZATION_LOST
            if isinstance(error, PermissionError)
            else YandexMailRawMessageFetchCode.PROVIDER_UNAVAILABLE
        ),
    )


def _raw_fetch_failure(
    command: YandexMailRawMessageFetchCommand,
    code: YandexMailRawMessageFetchCode,
) -> YandexMailRawMessageFetchResult:
    folder_reference = command.folder_or_label_reference
    assert folder_reference is not None
    return failed_yandex_mail_raw_message_fetch(
        scope=command.scope,
        folder_reference=folder_reference,
        uid_validity=command.uid_validity,
        uid=command.uid,
        code=code,
    )


__all__ = [
    "YANDEX_MAIL_IMAP_HOST",
    "YANDEX_MAIL_IMAP_PORT",
    "YANDEX_MAIL_OAUTH_SCOPE",
    "YandexMailAuthorityDeniedError",
    "YandexMailCredentialResolver",
    "YandexMailImapReadRequest",
    "YandexMailUidImapReadRequest",
    "YandexMailImapSecurity",
    "YandexMailImapSession",
    "YandexMailImapTransport",
    "YandexMailOAuthCredential",
    "compose_yandex_mail_reader",
]
