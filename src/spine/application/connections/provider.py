"""Provider-neutral read admission with capability and assurance gating."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from spine.application.persistence.context import TrustedPersistenceContext
from spine.connectors.observations_v2 import NormalizedExternalObservationV2
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapabilityKind,
    ProviderConnection,
    ProviderConnectionStatus,
)


class ProviderReadError(RuntimeError):
    """Base class for content-free provider-neutral admission failures."""


class ProviderReadDeniedError(ProviderReadError):
    def __init__(self) -> None:
        super().__init__("Provider connection read denied.")


class ProviderCapabilityUnavailableError(ProviderReadError):
    def __init__(self, capability: ProviderCapabilityKind) -> None:
        self.capability = capability
        super().__init__("Required provider capability is unavailable.")


@dataclass(frozen=True, slots=True)
class ProviderReadCommand:
    workspace_id: UUID
    environment_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    resource_scope_reference: OpaqueProviderReference
    authorization_generation: int
    configuration_generation: int
    required_capabilities: tuple[ProviderCapabilityKind, ...] = ()
    resource_object_reference: str | None = None
    acting_subject_id: UUID | None = None
    external_identity_occurrence: str | None = None

    def __post_init__(self) -> None:
        if any(
            value.int == 0
            for value in (
                self.workspace_id,
                self.environment_id,
                self.connection_id,
                self.connection_version_id,
            )
        ):
            raise ValueError("provider read scope IDs must be non-nil UUIDs")
        if self.authorization_generation < 1 or self.configuration_generation < 1:
            raise ValueError("connection generations must be positive")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ValueError("required provider capabilities must be unique")
        if self.resource_object_reference is not None and not (
            1 <= len(self.resource_object_reference) <= 1024
        ):
            raise ValueError("resource_object_reference is invalid")
        if self.acting_subject_id is not None and self.acting_subject_id.int == 0:
            raise ValueError("acting_subject_id must be a non-nil UUID")
        if self.external_identity_occurrence is not None and not (
            1 <= len(self.external_identity_occurrence) <= 128
        ):
            raise ValueError("external_identity_occurrence is invalid")


class ProviderConnectionRepository(Protocol):
    def admit_read(
        self,
        command: ProviderReadCommand,
        context: TrustedPersistenceContext,
    ) -> AbstractAsyncContextManager[ProviderConnection]: ...


class ProviderObservationReader(Protocol):
    async def read(
        self,
        connection: ProviderConnection,
        command: ProviderReadCommand,
    ) -> NormalizedExternalObservationV2: ...


class ProviderReadAdmission:
    """Authorize one exact connection version and validate claimed guarantees."""

    def __init__(
        self,
        *,
        repository: ProviderConnectionRepository,
        provider: ProviderObservationReader,
    ) -> None:
        self._repository = repository
        self._provider = provider

    async def read(
        self,
        command: ProviderReadCommand,
        context: TrustedPersistenceContext,
    ) -> NormalizedExternalObservationV2:
        async with self._repository.admit_read(command, context) as connection:
            self._validate_connection(command, connection)
            self._require_capabilities(connection, command.required_capabilities)
            observation = await self._provider.read(connection, command)
            self._validate_observation(command, connection, observation)
            self._require_capabilities(connection, observation.required_capabilities())
            return observation

    @staticmethod
    def _validate_connection(
        command: ProviderReadCommand, connection: ProviderConnection
    ) -> None:
        if connection.status is not ProviderConnectionStatus.ACTIVE:
            raise ProviderReadDeniedError()
        if (
            connection.connection_id,
            connection.connection_version_id,
            connection.workspace_id,
            connection.environment_id,
            connection.authorization_generation,
            connection.configuration_generation,
        ) != (
            command.connection_id,
            command.connection_version_id,
            command.workspace_id,
            command.environment_id,
            command.authorization_generation,
            command.configuration_generation,
        ):
            raise ProviderReadDeniedError()
        if command.resource_scope_reference not in (
            connection.approved_resource_scope_references
        ):
            raise ProviderReadDeniedError()

    @staticmethod
    def _validate_observation(
        command: ProviderReadCommand,
        connection: ProviderConnection,
        observation: NormalizedExternalObservationV2,
    ) -> None:
        if (
            observation.workspace_id,
            observation.environment_id,
            observation.connection_id,
            observation.connection_version_id,
            observation.provider_kind,
            observation.resource_scope_reference,
            observation.authorization_generation,
            observation.configuration_generation,
        ) != (
            connection.workspace_id,
            connection.environment_id,
            command.connection_id,
            command.connection_version_id,
            connection.provider_kind,
            command.resource_scope_reference,
            command.authorization_generation,
            command.configuration_generation,
        ):
            raise ProviderReadDeniedError()

    @staticmethod
    def _require_capabilities(
        connection: ProviderConnection,
        capabilities: tuple[ProviderCapabilityKind, ...]
        | frozenset[ProviderCapabilityKind],
    ) -> None:
        for capability in capabilities:
            if not connection.supports(capability):
                raise ProviderCapabilityUnavailableError(capability)


__all__ = [
    "ProviderCapabilityUnavailableError",
    "ProviderConnectionRepository",
    "ProviderObservationReader",
    "ProviderReadAdmission",
    "ProviderReadCommand",
    "ProviderReadDeniedError",
    "ProviderReadError",
]
