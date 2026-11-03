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
    label_reference: UUID | None = None


ResultT = TypeVar("ResultT")


class GmailReadClient(Protocol[ResultT]):
    """Provider client port; implementations must perform reads only."""

    async def read(self, request: GmailReadRequest) -> ResultT: ...


class GmailMailboxReader(Generic[ResultT]):
    """Translate an admitted mail read into a bounded Gmail client request."""

    def __init__(self, client: GmailReadClient[ResultT]) -> None:
        self._client = client

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
        return await self._client.read(
            GmailReadRequest(
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
                label_reference=command.folder_or_label_reference,
            )
        )


def compose_gmail_mailbox_reader(
    *,
    scopes: MailboxScopeRepository,
    connections: ProviderConnectionRepository,
    client: GmailReadClient[ResultT],
) -> MailboxReadAdmission[ResultT]:
    """Compose Gmail behind the provider-neutral, exact-scope admission gate."""

    return MailboxReadAdmission(
        scopes=scopes,
        connections=connections,
        provider=GmailMailboxReader(client),
    )


__all__ = [
    "GmailMailboxReader",
    "GmailReadClient",
    "GmailReadRequest",
    "compose_gmail_mailbox_reader",
]
