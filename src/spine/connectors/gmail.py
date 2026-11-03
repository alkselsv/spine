"""Read-only Gmail composition behind exact provider-neutral mail admission."""

from __future__ import annotations

from typing import Generic, Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import Field

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
from spine.domain.connections.email_scope import EmailOperation
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)


class GmailReadRequest(DefinitionModel):
    """A Gmail read call containing only opaque authority and approved scope."""

    schema_version: Literal["gmail-mailbox-read:v1"] = "gmail-mailbox-read:v1"
    method: Literal["GET"] = "GET"
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
    ) -> None:
        if max_pages < 1 or max_items < 1:
            raise ValueError("Gmail enumeration bounds must be positive")
        self._client = client
        self._max_pages = max_pages
        self._max_items = max_items

    async def read(
        self,
        connection: ProviderConnection,
        command: MailboxReadCommand,
    ) -> ResultT:
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
        ):
            raise MailboxReadDeniedError()
        request = GmailReadRequest(
                operation=command.operation,
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
                    else (command.folder_or_label_reference,)
                    if command.folder_or_label_reference is not None
                    else ()
                ),
            )
        if not is_enumeration:
            return await self._client.read(request)

        # Enumeration uses only messages.list metadata and an exact approved label.
        if not request.label_references or any(
            label not in scope.allowed_label_references for label in request.label_references
        ):
            raise MailboxReadDeniedError()
        ids: list[str] = []
        seen_ids: set[str] = set()
        seen_tokens: set[str] = set()
        token: str | None = None
        pages = 0
        while pages < self._max_pages and len(ids) < self._max_items:
            response = await self._client.read(
                request.model_copy(update={"page_token": token, "max_results": min(500, self._max_items - len(ids))})
            )
            if not isinstance(response, GmailMessageIdPage):
                raise MailboxReadDeniedError()
            pages += 1
            if not response.message_ids:
                return GmailEnumerationResult(
                    message_ids=tuple(ids), complete=False, pages_read=pages,
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
                return GmailEnumerationResult(message_ids=tuple(ids), complete=True, pages_read=pages)
            if truncated:
                return GmailEnumerationResult(
                    message_ids=tuple(ids), complete=False, pages_read=pages,
                    continuation_token=next_token,
                )
            if next_token in seen_tokens:
                raise MailboxReadDeniedError()
            seen_tokens.add(next_token)
            token = next_token
        return GmailEnumerationResult(
            message_ids=tuple(ids), complete=False, pages_read=pages, continuation_token=token
        )


def compose_gmail_mailbox_reader(
    *,
    scopes: MailboxScopeRepository,
    connections: ProviderConnectionRepository,
    client: GmailReadClient[ResultT],
    max_pages: int = 100,
    max_items: int = 10_000,
) -> MailboxReadAdmission[ResultT]:
    """Compose Gmail behind the provider-neutral, exact-scope admission gate."""

    return MailboxReadAdmission(
        scopes=scopes,
        connections=connections,
        provider=GmailMailboxReader(client, max_pages=max_pages, max_items=max_items),
    )


__all__ = [
    "GmailMailboxReader",
    "GmailReadClient",
    "GmailMessageIdPage",
    "GmailEnumerationResult",
    "GmailReadRequest",
    "compose_gmail_mailbox_reader",
]
