"""Trusted Google connection registration and read-admission contracts."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    AuditObjectReference,
    AuditOutcome,
    CommandAuditPayload,
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticFailureCode,
    DiagnosticSeverity,
    DiagnosticSink,
    FailureDiagnosticPayload,
    emit_diagnostic_safely,
)
from spine.application.persistence.context import EnvironmentScope
from spine.application.persistence.context import TrustedPersistenceContext
from spine.domain.common import ContextOrigin
from spine.domain.connections.google import (
    GoogleConnection,
    GoogleConnectionApproval,
    GoogleConnectionStatus,
    OpaqueGoogleReference,
)


ProviderResultT = TypeVar("ProviderResultT")


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
    occurred_at: datetime

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.idempotency_key) is None:
            raise ValueError("idempotency_key must be a bounded identifier")
        if self.audit_event_id.int == 0:
            raise ValueError("audit_event_id must be a non-nil UUID")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

    def digest(self) -> str:
        payload = {
            "schema": "google-connection-command:v1",
            "connection": self.connection.model_dump(mode="json"),
            "idempotency_key": self.idempotency_key,
            "audit_event_id": str(self.audit_event_id),
            "occurred_at": self.occurred_at.isoformat(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class GoogleConnectionMutationResult:
    connection: GoogleConnection
    audit: AuditEvent
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

    def admit_read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> AbstractAsyncContextManager[GoogleConnection]: ...


class GoogleProvider(Protocol[ProviderResultT]):
    async def read(
        self,
        connection: GoogleConnection,
        command: GoogleReadCommand,
    ) -> ProviderResultT: ...


class GoogleReadAdmission(Generic[ProviderResultT]):
    """Authorize exact current connection state before any provider request."""

    def __init__(
        self,
        *,
        repository: GoogleConnectionRepository,
        provider: GoogleProvider[ProviderResultT],
        diagnostic_sink: DiagnosticSink,
        diagnostic_registry: DiagnosticEventRegistry,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._diagnostic_sink = diagnostic_sink
        self._diagnostic_registry = diagnostic_registry
        self._clock = clock

    async def read(
        self,
        command: GoogleReadCommand,
        context: TrustedPersistenceContext,
    ) -> ProviderResultT:
        try:
            async with self._repository.admit_read(command, context) as connection:
                try:
                    return await self._provider.read(connection, command)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._emit_failure(
                        context, DiagnosticFailureCode.PROVIDER_UNAVAILABLE
                    )
                    raise GoogleProviderReadError() from None
        except asyncio.CancelledError:
            raise
        except GoogleConnectionDeniedError:
            await self._emit_failure(
                context, DiagnosticFailureCode.AUTHORIZATION_DENIED
            )
            raise

    async def _emit_failure(
        self,
        context: TrustedPersistenceContext,
        failure_code: DiagnosticFailureCode,
    ) -> None:
        scope = context.scope
        environment_id = (
            scope.environment_id if isinstance(scope, EnvironmentScope) else None
        )
        event = self._diagnostic_registry.build_event(
            event_type="failure.observed",
            schema_version=1,
            severity=DiagnosticSeverity.WARNING,
            occurred_at=self._clock(),
            diagnostic_context=DiagnosticContext(trace_id=context.trace_id),
            workspace_id=scope.workspace_id,
            environment_id=environment_id,
            payload=FailureDiagnosticPayload(failure_code=failure_code),
        )
        await emit_diagnostic_safely(self._diagnostic_sink, event)


def build_google_connection_audit_event(
    command: GoogleConnectionCommand,
    context: TrustedPersistenceContext,
) -> AuditEvent:
    connection = command.connection
    return AuditEventRegistry.with_default_families().build_event(
        audit_event_id=command.audit_event_id,
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="google_connection_registration"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=connection.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=command.occurred_at,
        target=AuditObjectReference(
            object_type="google_connection_version",
            object_id=connection.connection_version_id,
            schema_version=1,
        ),
        outcome=AuditOutcome.ACCEPTED,
        reason="connection_version_registered",
        producer_deduplication_id=command.idempotency_key,
    )


def connection_matches_approval(
    connection: GoogleConnection,
    approval: GoogleConnectionApproval | None,
) -> bool:
    return approval is not None and (
        approval.approval_id == connection.approval_id
        and approval.workspace_id == connection.workspace_id
        and approval.environment_id == connection.environment_id
        and approval.service_principal_id == connection.service_principal_id
        and approval.credential_reference == connection.credential_reference
        and approval.grant_reference == connection.grant_reference
        and approval.approved_resource_scope_references
        == connection.approved_resource_scope_references
        and approval.authorization_generation == connection.authorization_generation
        and approval.configuration_generation == connection.configuration_generation
        and approval.status == connection.status
    )


def context_allows_google_connection_registration(
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


def is_valid_google_connection_successor(
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


__all__ = [
    "GoogleConnectionCommand",
    "GoogleConnectionConflictError",
    "GoogleConnectionDeniedError",
    "GoogleConnectionMutationResult",
    "GoogleConnectionRepository",
    "GoogleProvider",
    "GoogleProviderReadError",
    "GoogleReadAdmission",
    "GoogleReadCommand",
    "build_google_connection_audit_event",
    "connection_matches_approval",
    "context_allows_google_connection_registration",
    "is_valid_google_connection_successor",
]
