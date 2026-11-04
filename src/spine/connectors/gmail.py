"""Read-only Gmail composition behind exact provider-neutral mail admission."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from enum import Enum
from hashlib import sha256
from typing import Generic, Literal, Protocol, TypeVar, cast
from uuid import UUID

from pydantic import Field, model_validator

from spine.application.connections.email_read import (
    MailboxReadAdmission,
    MailboxReadCommand,
    MailboxReadDeniedError,
    MailboxScopeRepository,
    required_mailbox_capability,
)
from spine.application.connections.provider import ProviderConnectionRepository
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.common import DefinitionModel
from spine.domain.connections.email_scope import EmailOperation, MailboxReadScope
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)
from spine.connectors.gmail_history import (
    GmailHistoryId,
    GmailHistoryPageRequest,
    GmailHistoryPageSource,
    GmailHistoryReader,
    GmailHistoryResult,
)


class GmailReadRequest(DefinitionModel):
    """A Gmail read call containing only opaque authority and approved scope."""

    schema_version: Literal["gmail-mailbox-read:v1"] = "gmail-mailbox-read:v1"
    method: Literal["GET"] = "GET"
    resource: Literal["messages", "message", "history", "profile"] = "messages"
    operation: EmailOperation
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    credential_reference: OpaqueProviderReference
    grant_reference: OpaqueProviderReference
    resource_scope_reference: OpaqueProviderReference
    mailbox_subject_reference: UUID
    mailbox_binding_id: UUID
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    scope_generation: int = Field(ge=1)
    label_references: tuple[UUID, ...] = ()
    page_token: str | None = None
    max_results: int = Field(default=100, ge=1, le=500)
    message_id: str | None = None
    format: Literal["RAW"] | None = None


class GmailHistoryPageReadRequest(GmailReadRequest):
    """Versioned history-page request while retaining Gmail read authority."""

    schema_version: Literal["gmail-history-page-read:v1"] = "gmail-history-page-read:v1"
    start_history_id: GmailHistoryId


class GmailHistoryReadCommand(MailboxReadCommand):
    """An admitted Gmail history drain with caller-owned cursor and bounds."""

    schema_version: Literal["gmail-history-read:v1"] = "gmail-history-read:v1"
    start_history_id: GmailHistoryId
    max_results: int = Field(ge=1, le=500)
    max_pages: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_history_operation(self) -> "GmailHistoryReadCommand":
        if self.operation is not EmailOperation.SYNCHRONIZE:
            raise ValueError("Gmail history reads require synchronize operation")
        return self


class GmailMessageIdPage(DefinitionModel):
    """Metadata-free response shape for Gmail messages.list."""

    message_ids: tuple[str, ...] = ()
    next_page_token: str | None = None


class GmailEnumerationResult(DefinitionModel):
    """Bounded ID inventory with explicit coverage evidence."""

    message_ids: tuple[str, ...]
    complete: bool
    pages_read: int = Field(ge=0)
    continuation_token: str | None = None


class GmailRawMessageResponse(DefinitionModel):
    """Allowlisted fields decoded from one synthetic/transport Gmail response."""

    http_status: int = Field(ge=100, le=599)
    message_id: str | None = None
    raw_data: str | None = None
    label_references: tuple[UUID, ...] = ()
    internal_date_ms: int | None = Field(default=None, ge=0)
    history_id: str | None = Field(default=None, pattern=r"^[0-9]+$")
    thread_id: str | None = None


class GmailRawMessageStatus(str, Enum):
    """Content-free outcomes for an exact Gmail RAW message read."""

    ACCEPTED = "accepted"
    MESSAGE_ID_MISMATCH = "message_id_mismatch"
    OUT_OF_SCOPE = "out_of_scope"
    NOT_FOUND = "not_found"
    MALFORMED_ENCODING = "malformed_encoding"
    TRUNCATED_DATA = "truncated_data"
    TOO_LARGE = "too_large"
    LIMIT_NOT_CONFIGURED = "limit_not_configured"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    THROTTLED = "throttled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"


class GmailRawMessageResult(DefinitionModel):
    """Immutable raw bytes and provider revision evidence, or a safe outcome."""

    schema_version: Literal["gmail-raw-message-result:v1"] = (
        "gmail-raw-message-result:v1"
    )
    status: GmailRawMessageStatus
    mailbox_scope: MailboxReadScope
    requested_message_id: str
    authorized_label_reference: UUID
    message_id: str | None = None
    raw_mime: bytes | None = None
    sha256: str | None = None
    byte_length: int | None = Field(default=None, ge=0)
    observed_label_references: tuple[UUID, ...] | None = None
    internal_date_ms: int | None = Field(default=None, ge=0)
    history_id: str | None = Field(default=None, pattern=r"^[0-9]+$")
    thread_id: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "GmailRawMessageResult":
        if self.status == GmailRawMessageStatus.ACCEPTED:
            if (
                self.raw_mime is None
                or self.sha256 is None
                or self.byte_length != len(self.raw_mime)
                or self.sha256 != sha256(self.raw_mime).hexdigest()
                or self.message_id != self.requested_message_id
                or not self.mailbox_scope.allows_location(
                    self.authorized_label_reference
                )
                or self.observed_label_references is None
                or self.authorized_label_reference not in self.observed_label_references
                or set(self.observed_label_references)
                & set(self.mailbox_scope.excluded_label_references)
                or self.internal_date_ms is None
                or self.history_id is None
            ):
                raise ValueError(
                    "accepted Gmail raw result requires verified revision evidence"
                )
        elif any(
            value is not None
            for value in (
                self.raw_mime,
                self.sha256,
                self.byte_length,
                self.observed_label_references,
                self.message_id,
                self.internal_date_ms,
                self.history_id,
                self.thread_id,
            )
        ):
            raise ValueError("failed Gmail raw result cannot contain provider evidence")
        return self


ResultT = TypeVar("ResultT")


class GmailReadClient(Protocol[ResultT]):
    """Provider client port; implementations must perform reads only."""

    async def read(self, request: GmailReadRequest) -> ResultT: ...


class GmailMailboxReader(Generic[ResultT]):
    """Translate an admitted mail read into a bounded Gmail client request."""

    def __init__(
        self,
        client: GmailReadClient[ResultT],
        *,
        max_pages: int = 100,
        max_items: int = 10_000,
        max_raw_message_bytes: int | None = None,
    ) -> None:
        if max_pages < 1 or max_items < 1:
            raise ValueError("Gmail enumeration bounds must be positive")
        self._client = client
        self._max_pages = max_pages
        self._max_items = max_items
        if max_raw_message_bytes is not None and max_raw_message_bytes < 1:
            raise ValueError("max_raw_message_bytes must be positive")
        self._max_raw_message_bytes = max_raw_message_bytes

    async def read(
        self,
        connection: ProviderConnection,
        command: MailboxReadCommand,
    ) -> ResultT:
        request = self.build_request(connection, command)
        is_enumeration = command.operation is EmailOperation.ENUMERATE_MAILBOX
        if is_enumeration:
            return await self._enumerate_messages(command.scope, request)
        if command.operation is not EmailOperation.READ_RAW_MESSAGE:
            return await self._client.read(request)
        label_reference = command.folder_or_label_reference
        if label_reference is None:
            raise MailboxReadDeniedError()
        return cast(
            ResultT,
            await self._read_raw_message(
                command.scope,
                command.message_id or "",
                label_reference,
                request,
            ),
        )

    async def _enumerate_messages(
        self,
        scope: MailboxReadScope,
        request: GmailReadRequest,
    ) -> GmailEnumerationResult:
        # Enumeration uses only messages.list metadata and an exact approved label.
        if not request.label_references or any(
            label not in scope.allowed_label_references
            for label in request.label_references
        ):
            raise MailboxReadDeniedError()
        ids: list[str] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        pages = 0
        while pages < self._max_pages and len(ids) < self._max_items:
            response = await self._client.read(
                request.model_copy(
                    update={
                        "page_token": token,
                        "max_results": min(500, self._max_items - len(ids)),
                    }
                )
            )
            if not isinstance(response, GmailMessageIdPage):
                raise MailboxReadDeniedError()
            pages += 1
            if not response.message_ids:
                return GmailEnumerationResult(
                    message_ids=tuple(ids),
                    complete=False,
                    pages_read=pages,
                    continuation_token=response.next_page_token,
                )
            truncated = False
            for message_id in response.message_ids:
                if not message_id or len(message_id) > 512:
                    raise MailboxReadDeniedError()
                if message_id not in seen_ids and len(ids) < self._max_items:
                    seen_ids.add(message_id)
                    ids.append(message_id)
                elif message_id not in seen_ids:
                    truncated = True
            next_token = response.next_page_token
            if not next_token and not truncated:
                return GmailEnumerationResult(
                    message_ids=tuple(ids), complete=True, pages_read=pages
                )
            if truncated:
                return GmailEnumerationResult(
                    message_ids=tuple(ids),
                    complete=False,
                    pages_read=pages,
                    continuation_token=next_token,
                )
            if next_token in seen_tokens:
                raise MailboxReadDeniedError()
            seen_tokens.add(next_token)
            token = next_token
        return GmailEnumerationResult(
            message_ids=tuple(ids),
            complete=False,
            pages_read=pages,
            continuation_token=token,
        )

    async def _read_raw_message(
        self,
        scope: MailboxReadScope,
        message_id: str,
        label_reference: UUID,
        request: GmailReadRequest,
    ) -> GmailRawMessageResult:
        if self._max_raw_message_bytes is None:
            return _raw_result(
                scope,
                message_id,
                label_reference,
                GmailRawMessageStatus.LIMIT_NOT_CONFIGURED,
            )
        try:
            response = await self._client.read(request)
        except Exception as error:
            return _raw_result(
                scope, message_id, label_reference, _status_for_exception(error)
            )
        if not isinstance(response, GmailRawMessageResponse):
            return _raw_result(
                scope, message_id, label_reference, GmailRawMessageStatus.PROVIDER_ERROR
            )
        status = _status_for_http(response.http_status)
        if status is not None:
            return _raw_result(scope, message_id, label_reference, status)
        if not 200 <= response.http_status <= 299:
            return _raw_result(
                scope, message_id, label_reference, GmailRawMessageStatus.PROVIDER_ERROR
            )
        if response.message_id != message_id:
            return _raw_result(
                scope,
                message_id,
                label_reference,
                GmailRawMessageStatus.MESSAGE_ID_MISMATCH,
            )
        if label_reference not in response.label_references or set(
            response.label_references
        ) & set(scope.excluded_label_references):
            return _raw_result(
                scope, message_id, label_reference, GmailRawMessageStatus.OUT_OF_SCOPE
            )
        if (
            response.raw_data is None
            or response.internal_date_ms is None
            or response.history_id is None
        ):
            return _raw_result(
                scope, message_id, label_reference, GmailRawMessageStatus.PROVIDER_ERROR
            )

        encoded_limit = ((self._max_raw_message_bytes + 2) // 3) * 4
        if len(response.raw_data) > encoded_limit:
            return _raw_result(
                scope, message_id, label_reference, GmailRawMessageStatus.TOO_LARGE
            )
        try:
            encoded = response.raw_data.encode("ascii")
        except UnicodeEncodeError:
            return _raw_result(
                scope,
                message_id,
                label_reference,
                GmailRawMessageStatus.MALFORMED_ENCODING,
            )
        if not encoded or any(
            byte
            not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="
            for byte in encoded
        ):
            return _raw_result(
                scope,
                message_id,
                label_reference,
                GmailRawMessageStatus.MALFORMED_ENCODING,
            )
        if len(encoded) % 4 == 1:
            return _raw_result(
                scope, message_id, label_reference, GmailRawMessageStatus.TRUNCATED_DATA
            )
        padding_length = len(encoded) - len(encoded.rstrip(b"="))
        if padding_length and (
            len(encoded) % 4 != 0
            or padding_length > 2
            or b"=" in encoded[:-padding_length]
        ):
            return _raw_result(
                scope,
                message_id,
                label_reference,
                GmailRawMessageStatus.MALFORMED_ENCODING,
            )
        try:
            raw_mime = base64.b64decode(
                encoded + b"=" * ((-len(encoded)) % 4), altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError):
            return _raw_result(
                scope,
                message_id,
                label_reference,
                GmailRawMessageStatus.MALFORMED_ENCODING,
            )
        if len(raw_mime) > self._max_raw_message_bytes:
            return _raw_result(
                scope, message_id, label_reference, GmailRawMessageStatus.TOO_LARGE
            )
        return GmailRawMessageResult(
            status=GmailRawMessageStatus.ACCEPTED,
            mailbox_scope=scope,
            requested_message_id=message_id,
            authorized_label_reference=label_reference,
            message_id=response.message_id,
            raw_mime=raw_mime,
            sha256=sha256(raw_mime).hexdigest(),
            byte_length=len(raw_mime),
            observed_label_references=response.label_references,
            internal_date_ms=response.internal_date_ms,
            history_id=response.history_id,
            thread_id=response.thread_id,
        )

    async def read_history_page(
        self,
        connection: ProviderConnection,
        command: GmailHistoryReadCommand,
        page_request: GmailHistoryPageRequest,
    ) -> ResultT:
        if (
            page_request.start_history_id != command.start_history_id
            or page_request.max_results != command.max_results
        ):
            raise MailboxReadDeniedError()
        base_request = self.build_request(connection, command)
        history_request = GmailHistoryPageReadRequest(
            **{
                **base_request.model_dump(),
                "schema_version": "gmail-history-page-read:v1",
                "resource": "history",
                "start_history_id": page_request.start_history_id,
                "page_token": page_request.page_token,
                "max_results": page_request.max_results,
            }
        )
        return await self._client.read(history_request)

    @staticmethod
    def build_request(
        connection: ProviderConnection,
        command: MailboxReadCommand,
    ) -> GmailReadRequest:
        scope = command.scope
        capability = required_mailbox_capability(command.operation)
        is_enumeration = command.operation is EmailOperation.ENUMERATE_MAILBOX
        if (
            capability is None
            or connection.status is not ProviderConnectionStatus.ACTIVE
            or connection.provider_kind is not ProviderKind.GOOGLE
            or scope.provider_kind != ProviderKind.GOOGLE.value
            or not scope.allows(command.operation)
            or command.mailbox_subject_reference != scope.mailbox_subject_reference
            or (is_enumeration and command.folder_or_label_reference is not None)
            or (
                not is_enumeration
                and not scope.allows_location(command.folder_or_label_reference)
            )
            or connection.workspace_id != scope.workspace_id
            or connection.environment_id != scope.environment_id
            or connection.connection_id != scope.connection_id
            or connection.connection_version_id != scope.connection_version_id
            or connection.authorization_generation != scope.authorization_generation
            or connection.configuration_generation != scope.configuration_generation
            or OpaqueProviderReference(value=scope.resource_scope_reference)
            not in connection.approved_resource_scope_references
            or not connection.supports(capability)
            or (
                command.operation is EmailOperation.READ_RAW_MESSAGE
                and not command.message_id
            )
        ):
            raise MailboxReadDeniedError()
        return GmailReadRequest(
            operation=command.operation,
            resource=(
                "message"
                if command.operation is EmailOperation.READ_RAW_MESSAGE
                else "messages"
            ),
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            credential_reference=connection.credential_reference,
            grant_reference=connection.grant_reference,
            resource_scope_reference=OpaqueProviderReference(
                value=scope.resource_scope_reference
            ),
            mailbox_subject_reference=scope.mailbox_subject_reference,
            mailbox_binding_id=scope.mailbox_binding_id,
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            scope_generation=scope.scope_generation,
            label_references=(
                scope.allowed_label_references
                if is_enumeration
                else (
                    (command.folder_or_label_reference,)
                    if command.folder_or_label_reference is not None
                    else ()
                )
            ),
            message_id=(
                command.message_id
                if command.operation is EmailOperation.READ_RAW_MESSAGE
                else None
            ),
            format=(
                "RAW" if command.operation is EmailOperation.READ_RAW_MESSAGE else None
            ),
        )


class _AdmittedGmailHistoryPageSource:
    def __init__(
        self,
        *,
        reader: GmailMailboxReader[Mapping[str, object]],
        connection: ProviderConnection,
        command: GmailHistoryReadCommand,
    ) -> None:
        self._reader = reader
        self._connection = connection
        self._command = command

    async def list_history(
        self, request: GmailHistoryPageRequest
    ) -> Mapping[str, object]:
        return await self._reader.read_history_page(
            self._connection,
            self._command,
            request,
        )


class _GmailHistoryProviderReader:
    def __init__(self, client: GmailReadClient[Mapping[str, object]]) -> None:
        self._mailbox_reader = GmailMailboxReader(client)

    async def read(
        self,
        connection: ProviderConnection,
        command: MailboxReadCommand,
    ) -> GmailHistoryResult:
        if not isinstance(command, GmailHistoryReadCommand):
            raise MailboxReadDeniedError()
        source = _AdmittedGmailHistoryPageSource(
            reader=self._mailbox_reader,
            connection=connection,
            command=command,
        )
        return await GmailHistoryReader(
            source,
            scope=command.scope,
            max_pages=command.max_pages,
        ).read(
            start_history_id=command.start_history_id,
            max_results=command.max_results,
        )


def compose_gmail_mailbox_reader(
    *,
    scopes: MailboxScopeRepository,
    connections: ProviderConnectionRepository,
    client: GmailReadClient[ResultT],
    max_pages: int = 100,
    max_items: int = 10_000,
    max_raw_message_bytes: int | None = None,
) -> MailboxReadAdmission[ResultT]:
    """Compose Gmail behind the provider-neutral, exact-scope admission gate."""

    return MailboxReadAdmission(
        scopes=scopes,
        connections=connections,
        provider=GmailMailboxReader(
            client,
            max_pages=max_pages,
            max_items=max_items,
            max_raw_message_bytes=max_raw_message_bytes,
        ),
    )


def compose_gmail_history_reader(
    *,
    scopes: MailboxScopeRepository,
    connections: ProviderConnectionRepository,
    client: GmailReadClient[Mapping[str, object]],
) -> MailboxReadAdmission[GmailHistoryResult]:
    """Compose bounded history paging behind exact mailbox read admission."""

    return MailboxReadAdmission(
        scopes=scopes,
        connections=connections,
        provider=_GmailHistoryProviderReader(client),
    )


def _raw_result(
    scope: MailboxReadScope,
    message_id: str,
    label_reference: UUID,
    status: GmailRawMessageStatus,
) -> GmailRawMessageResult:
    return GmailRawMessageResult(
        status=status,
        mailbox_scope=scope,
        requested_message_id=message_id,
        authorized_label_reference=label_reference,
    )


def _status_for_exception(error: Exception) -> GmailRawMessageStatus:
    status = getattr(error, "status_code", getattr(error, "status", None))
    return _status_for_http(status) or GmailRawMessageStatus.PROVIDER_ERROR


def _status_for_http(status_code: object) -> GmailRawMessageStatus | None:
    if status_code == 401:
        return GmailRawMessageStatus.UNAUTHORIZED
    if status_code == 403:
        return GmailRawMessageStatus.FORBIDDEN
    if status_code == 404:
        return GmailRawMessageStatus.NOT_FOUND
    if status_code == 429:
        return GmailRawMessageStatus.THROTTLED
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return GmailRawMessageStatus.PROVIDER_UNAVAILABLE
    return None


__all__ = [
    "GmailMailboxReader",
    "GmailHistoryReadCommand",
    "GmailRawMessageResponse",
    "GmailRawMessageResult",
    "GmailRawMessageStatus",
    "GmailReadClient",
    "GmailHistoryPageReadRequest",
    "GmailMessageIdPage",
    "GmailEnumerationResult",
    "GmailReadRequest",
    "compose_gmail_history_reader",
    "compose_gmail_mailbox_reader",
]
