"""Trusted Google connection registration and read-admission contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Protocol
from uuid import UUID

from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.connections.google import GoogleConnection, OpaqueGoogleReference


class GoogleConnectionError(RuntimeError):
    """Base class for disclosure-safe Google connection failures."""


class GoogleConnectionDeniedError(GoogleConnectionError):
    def __init__(self) -> None:
        super().__init__("Google connection read denied.")


class GoogleConnectionConflictError(GoogleConnectionError):
    def __init__(self) -> None:
        super().__init__("Google connection command conflicts with canonical state.")


class GoogleProviderReadError(GoogleConnectionError):
    def __init__(self) -> None:
        super().__init__("Google provider read failed.")


@dataclass(frozen=True, slots=True)
class GoogleConnectionCommand:
    connection: GoogleConnection
    idempotency_key: str
    audit_event_id: UUID

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.idempotency_key) is None:
            raise ValueError("idempotency_key must be a bounded identifier")
        if self.audit_event_id.int == 0:
            raise ValueError("audit_event_id must be a non-nil UUID")

    def digest(self) -> str:
        payload = {
            "schema": "google-connection-command:v1",
            "connection": self.connection.model_dump(mode="json"),
            "idempotency_key": self.idempotency_key,
            "audit_event_id": str(self.audit_event_id),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class GoogleConnectionAuditSnapshot:
    """Content-free append-only evidence for a connection version."""

    audit_event_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    workspace_id: UUID
    environment_id: UUID
    service_principal_id: UUID
    authorization_generation: int
    configuration_generation: int
    status: str
    resource_scope_references: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoogleConnectionMutationResult:
    connection: GoogleConnection
    audit: GoogleConnectionAuditSnapshot
    replay: bool = False


@dataclass(frozen=True, slots=True)
class GoogleReadCommand:
    connection_id: UUID
    resource_scope_reference: OpaqueGoogleReference
    authorization_generation: int
    configuration_generation: int

    def __post_init__(self) -> None:
        if self.connection_id.int == 0:
            raise ValueError("connection_id must be a non-nil UUID")
        if self.authorization_generation < 1 or self.configuration_generation < 1:
            raise ValueError("connection generations must be positive")


class GoogleConnectionRepository(Protocol):
    async def register(
        self,
        command: GoogleConnectionCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnectionMutationResult: ...

    async def resolve_for_read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnection: ...


class GoogleProvider(Protocol):
    async def read(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
    ) -> Any: ...


class GoogleReadAdmission:
    """Authorize exact current connection state before any provider request."""

    def __init__(
        self,
        *,
        repository: GoogleConnectionRepository,
        provider: GoogleProvider,
    ) -> None:
        self._repository = repository
        self._provider = provider

    async def read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> Any:
        connection = await self._repository.resolve_for_read(command, context)
        try:
            return await self._provider.read(connection, command)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise GoogleProviderReadError() from None


def google_connection_audit_snapshot(
    command: GoogleConnectionCommand,
) -> GoogleConnectionAuditSnapshot:
    connection = command.connection
    return GoogleConnectionAuditSnapshot(
        audit_event_id=command.audit_event_id,
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        service_principal_id=connection.service_principal_id,
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        status=connection.status.value,
        resource_scope_references=tuple(
            item.value for item in connection.approved_resource_scope_references
        ),
    )


__all__ = [
    "GoogleConnectionAuditSnapshot",
    "GoogleConnectionCommand",
    "GoogleConnectionConflictError",
    "GoogleConnectionDeniedError",
    "GoogleConnectionMutationResult",
    "GoogleConnectionRepository",
    "GoogleProvider",
    "GoogleProviderReadError",
    "GoogleReadAdmission",
    "GoogleReadCommand",
    "google_connection_audit_snapshot",
]
