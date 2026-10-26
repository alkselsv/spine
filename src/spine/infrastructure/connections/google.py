"""Deterministic in-memory Google connection authority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

from spine.application.connections.google import (
    GoogleConnectionCommand,
    GoogleConnectionConflictError,
    GoogleConnectionDeniedError,
    GoogleConnectionMutationResult,
    GoogleReadCommand,
    build_google_connection_audit_event,
    connection_matches_approval,
    context_allows_google_connection_registration,
    is_valid_google_connection_successor,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionApproval,
    GoogleConnectionStatus,
)


class InMemoryGoogleConnectionRepository:
    """Append-only connection repository with exact current-version admission."""

    def __init__(
        self,
        *,
        context_verifier: TrustedContextVerifier,
        approvals: tuple[GoogleConnectionApproval, ...],
    ) -> None:
        self._context_verifier = context_verifier
        self._lock = asyncio.Lock()
        self._versions: dict[UUID, GoogleConnection] = {}
        self._current: dict[tuple[UUID, UUID, UUID], UUID] = {}
        self._commands: dict[str, tuple[str, GoogleConnectionMutationResult]] = {}
        self._approvals = {approval.approval_id: approval for approval in approvals}
        if len(self._approvals) != len(approvals):
            raise ValueError("Google connection approval identities must be unique")

    async def register(
        self,
        command: GoogleConnectionCommand,
        context: TrustedPersistenceContext,
    ) -> GoogleConnectionMutationResult:
        trusted = self._context_verifier.verify(context)
        connection = command.connection
        if not context_allows_google_connection_registration(trusted, connection):
            raise GoogleConnectionConflictError()
        if not connection_matches_approval(
            connection, self._approvals.get(connection.approval_id)
        ):
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
            if not is_valid_google_connection_successor(current, connection):
                raise GoogleConnectionConflictError()
            if connection.connection_version_id in self._versions:
                raise GoogleConnectionConflictError()

            audit = build_google_connection_audit_event(command, trusted)
            result = GoogleConnectionMutationResult(connection=connection, audit=audit)
            self._versions[connection.connection_version_id] = connection
            self._current[current_key] = connection.connection_version_id
            self._commands[command.idempotency_key] = (digest, result)
            return result

    @asynccontextmanager
    async def admit_read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> AsyncIterator[GoogleConnection]:
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
        except asyncio.CancelledError:
            raise
        except GoogleConnectionDeniedError:
            raise
        except Exception:
            raise GoogleConnectionDeniedError() from None
        async with self._lock:
            try:
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
                    or connection.authorization_generation
                    != command.authorization_generation
                    or connection.configuration_generation
                    != command.configuration_generation
                    or command.resource_scope_reference
                    not in connection.approved_resource_scope_references
                ):
                    raise GoogleConnectionDeniedError()
            except asyncio.CancelledError:
                raise
            except GoogleConnectionDeniedError:
                raise
            except Exception:
                raise GoogleConnectionDeniedError() from None
            yield connection
__all__ = ["InMemoryGoogleConnectionRepository"]
