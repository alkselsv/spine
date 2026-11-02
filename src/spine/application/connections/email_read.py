"""Operation-specific admission for exact approved mailbox scopes."""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar
from uuid import UUID

from pydantic import field_validator

from spine.application.connections.provider import (
    ProviderConnectionRepository,
    ProviderReadCommand,
)
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.domain.connections.email_scope import (
    EmailOperation,
    MailboxReadScope,
)
from spine.domain.common import DefinitionModel
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderConnection,
    ProviderConnectionStatus,
)


class MailboxReadDeniedError(PermissionError):
    """Content-free failure for any invalid mailbox scope or operation."""

    def __init__(self) -> None:
        super().__init__("Mailbox read denied.")


class MailboxReadCommand(DefinitionModel):
    scope: MailboxReadScope
    operation: EmailOperation
    mailbox_subject_reference: UUID
    folder_or_label_reference: UUID | None = None

    @field_validator("mailbox_subject_reference", "folder_or_label_reference")
    @classmethod
    def _validate_reference(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("mailbox read references must be non-zero")
        return value


class MailboxScopeRepository(Protocol):
    async def resolve_exact(
        self,
        scope: MailboxReadScope,
        context: TrustedPersistenceContext,
    ) -> MailboxReadScope | None: ...


ReadResultT = TypeVar("ReadResultT")


class MailboxProviderReader(Protocol[ReadResultT]):
    async def read(
        self,
        connection: ProviderConnection,
        command: MailboxReadCommand,
    ) -> ReadResultT: ...


_REQUIRED_CAPABILITY: dict[EmailOperation, ProviderCapabilityKind] = {
    EmailOperation.ENUMERATE_MAILBOX: ProviderCapabilityKind.MAILBOX_ENUMERATION,
    EmailOperation.READ_MESSAGE_METADATA: ProviderCapabilityKind.MESSAGE_METADATA_READ,
    EmailOperation.READ_RAW_MESSAGE: ProviderCapabilityKind.RAW_MESSAGE_READ,
    EmailOperation.READ_ATTACHMENT: ProviderCapabilityKind.ATTACHMENT_READ,
    EmailOperation.SYNCHRONIZE: ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
}


class MailboxReadAdmission(Generic[ReadResultT]):
    """Revalidate exact scope and connection authority before invoking a provider."""

    def __init__(
        self,
        *,
        scopes: MailboxScopeRepository,
        connections: ProviderConnectionRepository,
        provider: MailboxProviderReader[ReadResultT],
    ) -> None:
        self._scopes = scopes
        self._connections = connections
        self._provider = provider

    async def read(
        self,
        command: MailboxReadCommand,
        context: TrustedPersistenceContext,
    ) -> ReadResultT:
        scope = command.scope
        trusted_scope = context.scope
        if (
            not isinstance(trusted_scope, EnvironmentScope)
            or (trusted_scope.workspace_id, trusted_scope.environment_id)
            != (scope.workspace_id, scope.environment_id)
            or command.mailbox_subject_reference != scope.mailbox_subject_reference
            or not scope.allows(command.operation)
        ):
            raise MailboxReadDeniedError()

        is_enumeration = command.operation is EmailOperation.ENUMERATE_MAILBOX
        if is_enumeration:
            if command.folder_or_label_reference is not None:
                raise MailboxReadDeniedError()
        elif not scope.allows_location(command.folder_or_label_reference):
            raise MailboxReadDeniedError()

        try:
            approved_scope = await self._scopes.resolve_exact(scope, context)
        except Exception:
            raise MailboxReadDeniedError() from None
        if approved_scope != scope:
            raise MailboxReadDeniedError()

        read_command = ProviderReadCommand(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            connection_id=scope.connection_id,
            connection_version_id=scope.connection_version_id,
            resource_scope_reference=OpaqueProviderReference(
                value=scope.resource_scope_reference
            ),
            authorization_generation=scope.authorization_generation,
            configuration_generation=scope.configuration_generation,
            required_capabilities=(_REQUIRED_CAPABILITY[command.operation],),
        )
        try:
            async with self._connections.admit_read(read_command, context) as connection:
                if not self._connection_matches(scope, connection):
                    raise MailboxReadDeniedError()
                if not connection.supports(_REQUIRED_CAPABILITY[command.operation]):
                    raise MailboxReadDeniedError()
                try:
                    return await self._provider.read(connection, command)
                except Exception:
                    raise MailboxReadDeniedError() from None
        except Exception:
            raise MailboxReadDeniedError() from None

    @staticmethod
    def _connection_matches(
        scope: MailboxReadScope,
        connection: ProviderConnection,
    ) -> bool:
        return (
            connection.status is ProviderConnectionStatus.ACTIVE
            and connection.provider_kind.value == scope.provider_kind
            and connection.connection_id == scope.connection_id
            and connection.connection_version_id == scope.connection_version_id
            and connection.workspace_id == scope.workspace_id
            and connection.environment_id == scope.environment_id
            and connection.authorization_generation == scope.authorization_generation
            and connection.configuration_generation == scope.configuration_generation
            and OpaqueProviderReference(value=scope.resource_scope_reference)
            in connection.approved_resource_scope_references
        )


__all__ = [
    "MailboxProviderReader",
    "MailboxReadAdmission",
    "MailboxReadCommand",
    "MailboxReadDeniedError",
    "MailboxScopeRepository",
]
