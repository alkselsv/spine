"""Deterministic in-memory Google connection authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionDeniedError,
    GoogleConnectionMutationResult,
    GoogleReadCommand,
    google_connection_audit_snapshot,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.connections.google import GoogleConnection, GoogleConnectionStatus


class InMemoryGoogleConnectionRepository:
    """Append-only connection repository with exact current-version admission."""

    def __init__(self, *, context_verifier: TrustedContextVerifier) -> None:
        self._context_verifier = context_verifier
        self._lock = asyncio.Lock()
        self._versions: dict[UUID, GoogleConnection] = {}
        self._current: dict[tuple[UUID, UUID, UUID], UUID] = {}
        self._commands: dict[str, tuple[str, GoogleConnectionMutationResult]] = {}

    async def register(
        self,
        command: GoogleConnectionCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnectionMutationResult:
        trusted = self._context_verifier.verify(context)
        connection = command.connection
        if not _context_allows_registration(trusted, connection):
            raise GoogleConnectionConflictError()
        digest = command.digest()
        async with self._lock:
            existing_command = self._commands.get(command.idempotency_key)
            if existing_command is not None:
                existing_digest, result = existing_command
                if existing_digest != digest:
                    raise GoogleConnectionConflictError()
                return replace(result, replay=True)

            current_key = (
                connection.workspace_id,
                connection.environment_id,
                connection.connection_id,
            )
            current_id = self._current.get(current_key)
            current = self._versions.get(current_id) if current_id is not None else None
            if not _valid_successor(current, connection):
                raise GoogleConnectionConflictError()
            if connection.connection_version_id in self._versions:
                raise GoogleConnectionConflictError()

            audit = google_connection_audit_snapshot(command)
            result = GoogleConnectionMutationResult(connection=connection, audit=audit)
            self._versions[connection.connection_version_id] = connection
            self._current[current_key] = connection.connection_version_id
            self._commands[command.idempotency_key] = (digest, result)
            return result

    async def resolve_for_read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnection:
        try:
            trusted = self._context_verifier.verify(context)
            if (
                trusted.origin is not ContextOrigin.WORKER
                or not isinstance(trusted.scope, EnvironmentScope)
                or trusted.service_principal_id is None
                or trusted.purpose.value != "google_connector"
                or trusted.operation.value != "google_connection_read"
            ):
                raise GoogleConnectionDeniedError()
            current_id = self._current.get(
                (
                    trusted.scope.workspace_id,
                    trusted.scope.environment_id,
                    command.connection_id,
                )
            )
            connection = self._versions.get(current_id) if current_id else None
            if (
                connection is None
                or connection.status is not GoogleConnectionStatus.ACTIVE
                or connection.service_principal_id != trusted.service_principal_id
                or connection.authorization_generation != command.authorization_generation
                or connection.configuration_generation != command.configuration_generation
                or command.resource_scope_reference
                not in connection.approved_resource_scope_references
            ):
                raise GoogleConnectionDeniedError()
            return connection
        except GoogleConnectionDeniedError:
            raise
        except Exception:
            raise GoogleConnectionDeniedError() from None


def _context_allows_registration(
    context: TrustedPersistenceContext,
    connection: GoogleConnection,
) -> bool:
    return (
        isinstance(context.scope, EnvironmentScope)
        and context.purpose.value == "google_connector"
        and context.operation.value == "google_connection_register"
        and context.scope.workspace_id == connection.workspace_id
        and context.scope.environment_id == connection.environment_id
        and (
            context.origin is ContextOrigin.INTERACTIVE
            or context.service_principal_id == connection.service_principal_id
        )
    )


def _valid_successor(
    current: GoogleConnection | None,
    candidate: GoogleConnection,
) -> bool:
    if current is None:
        return candidate.version == 1 and candidate.predecessor_version_id is None
    return (
        candidate.connection_id == current.connection_id
        and candidate.workspace_id == current.workspace_id
        and candidate.environment_id == current.environment_id
        and candidate.version == current.version + 1
        and candidate.predecessor_version_id == current.connection_version_id
        and candidate.authorization_generation >= current.authorization_generation
        and candidate.configuration_generation >= current.configuration_generation
        and (
            candidate.authorization_generation > current.authorization_generation
            or candidate.configuration_generation > current.configuration_generation
        )
    )


__all__ = ["InMemoryGoogleConnectionRepository"]
