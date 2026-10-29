"""Provider-neutral connection configuration and safe operator inspection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.connection_errors import (
    InvalidProviderConnectionRequest,
    ProviderConnectionManagementError,
    ProviderConnectionNotFoundError,
)
from spine.application.diagnostics import (
    AuditEvent,
    AuditEventRegistry,
    AuditObjectReference,
    AuditOutcome,
    CommandAuditPayload,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.errors import OptimisticConflictError
from spine.domain.connections.provider import (
    ProviderCapability,
    ProviderConnection,
    ProviderConnectionApproval,
    ProviderConnectionHealth,
    ProviderConnectionStatus,
    ProviderDiagnosticCode,
)


@dataclass(frozen=True, slots=True)
class ProviderConnectionConfigurationRequest:
    approval_id: UUID
    connection_id: UUID
    expected_version: int
    expected_predecessor_version_id: UUID | None
    idempotency_key: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.approval_id.int == 0 or self.connection_id.int == 0:
            raise ValueError("provider configuration IDs must be non-nil UUIDs")
        if self.expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        if self.expected_version == 0 and self.expected_predecessor_version_id is not None:
            raise ValueError("initial provider configuration cannot have a predecessor")
        if self.expected_version > 0 and self.expected_predecessor_version_id is None:
            raise ValueError("successor provider configuration requires a predecessor")
        if (
            not 1 <= len(self.idempotency_key) <= 255
            or self.idempotency_key != self.idempotency_key.strip()
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in self.idempotency_key
            )
        ):
            raise ValueError("idempotency_key must be a bounded identifier")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProviderRuntimeObservation:
    capabilities: tuple[ProviderCapability, ...]
    health: ProviderConnectionHealth
    diagnostic_codes: tuple[ProviderDiagnosticCode, ...]
    observed_at: datetime
    cursor_lag_seconds: int | None = None
    last_successful_reconciliation_at: datetime | None = None

    def __post_init__(self) -> None:
        if len({item.kind for item in self.capabilities}) != len(self.capabilities):
            raise ValueError("provider capability kinds must be unique")
        if len(set(self.diagnostic_codes)) != len(self.diagnostic_codes):
            raise ValueError("provider diagnostic codes must be unique")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.cursor_lag_seconds is not None and self.cursor_lag_seconds < 0:
            raise ValueError("cursor_lag_seconds must be non-negative")
        if (
            self.last_successful_reconciliation_at is not None
            and (
                self.last_successful_reconciliation_at.tzinfo is None
                or self.last_successful_reconciliation_at.utcoffset() is None
            )
        ):
            raise ValueError("last_successful_reconciliation_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProviderConnectionInspection:
    connection: ProviderConnection
    health: ProviderConnectionHealth
    diagnostic_codes: tuple[ProviderDiagnosticCode, ...]
    observed_at: datetime
    cursor_lag_seconds: int | None = None
    last_successful_reconciliation_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderConnectionConfigurationResult:
    receipt_id: UUID
    inspection: ProviderConnectionInspection
    audit: AuditEvent
    replay: bool = False


@dataclass(frozen=True, slots=True)
class ProviderConnectionPage:
    items: tuple[ProviderConnectionInspection, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class ProviderConnectionConfigurationAdmission:
    approval: ProviderConnectionApproval
    replay: ProviderConnectionConfigurationResult | None = None


class ProviderConnectionManagementRepository(Protocol):
    async def authorize_configuration(
        self,
        request: ProviderConnectionConfigurationRequest,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationAdmission: ...

    async def register(
        self,
        request: ProviderConnectionConfigurationRequest,
        approval: ProviderConnectionApproval,
        observation: ProviderRuntimeObservation,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult: ...

    async def list_current(
        self,
        context: TrustedPersistenceContext,
        *,
        after_connection_id: UUID | None = None,
        limit: int = 101,
    ) -> tuple[ProviderConnectionInspection, ...]: ...

    async def get_current(
        self, connection_id: UUID, context: TrustedPersistenceContext
    ) -> ProviderConnectionInspection | None: ...


class ProviderConnectionObserver(Protocol):
    async def inspect_approved(
        self, approval: ProviderConnectionApproval
    ) -> ProviderRuntimeObservation: ...

    async def inspect_connection(
        self, connection: ProviderConnection
    ) -> ProviderRuntimeObservation: ...


class ProviderConnectionManager:
    """Keep authorization, versioning and safe provider observation behind one interface."""

    def __init__(
        self,
        *,
        repository: ProviderConnectionManagementRepository,
        provider: ProviderConnectionObserver,
    ) -> None:
        self._repository = repository
        self._provider = provider

    async def configure(
        self,
        request: ProviderConnectionConfigurationRequest,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult:
        admission = await self._repository.authorize_configuration(request, context)
        if admission.replay is not None:
            return replace(admission.replay, replay=True)
        observation = await self._provider.inspect_approved(admission.approval)
        return await self._repository.register(
            request,
            admission.approval,
            observation,
            context,
        )

    async def list(
        self,
        *,
        context: TrustedPersistenceContext,
        after_connection_id: UUID | None = None,
        limit: int = 100,
    ) -> ProviderConnectionPage:
        if not 1 <= limit <= 100:
            raise InvalidProviderConnectionRequest()
        items = await self._repository.list_current(
            context,
            after_connection_id=after_connection_id,
            limit=limit + 1,
        )
        return ProviderConnectionPage(items=items[:limit], has_more=len(items) > limit)

    async def inspect(
        self,
        *,
        connection_id: UUID,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionInspection:
        current = await self._repository.get_current(connection_id, context)
        if current is None:
            raise ProviderConnectionNotFoundError()
        if current.connection.status is not ProviderConnectionStatus.ACTIVE:
            return current
        observation = await self._provider.inspect_connection(current.connection)
        return self._inspection(current.connection, observation)

    @staticmethod
    def build_result(
        *,
        request: ProviderConnectionConfigurationRequest,
        approval: ProviderConnectionApproval,
        observation: ProviderRuntimeObservation,
        context: TrustedPersistenceContext,
        replay: bool,
    ) -> ProviderConnectionConfigurationResult:
        receipt_id = uuid5(
            NAMESPACE_URL,
            "spine:provider-connection-receipt:v1:"
            f"{approval.workspace_id}:{approval.environment_id}:{request.idempotency_key}",
        )
        connection = ProviderConnection(
            connection_version_id=uuid5(
                NAMESPACE_URL,
                f"spine:provider-connection-version:v1:{receipt_id}",
            ),
            connection_id=request.connection_id,
            approval_id=approval.approval_id,
            version=request.expected_version + 1,
            predecessor_version_id=request.expected_predecessor_version_id,
            workspace_id=approval.workspace_id,
            environment_id=approval.environment_id,
            service_principal_id=approval.service_principal_id,
            provider_kind=approval.provider_kind,
            credential_reference=approval.credential_reference,
            grant_reference=approval.grant_reference,
            approved_resource_scope_references=(
                approval.approved_resource_scope_references
            ),
            authorization_generation=approval.authorization_generation,
            configuration_generation=approval.configuration_generation,
            status=approval.status,
            capabilities=observation.capabilities,
        )
        audit = build_provider_connection_audit(
            request=request,
            connection=connection,
            context=context,
            receipt_id=receipt_id,
        )
        return ProviderConnectionConfigurationResult(
            receipt_id=receipt_id,
            inspection=ProviderConnectionManager._inspection(connection, observation),
            audit=audit,
            replay=replay,
        )

    @staticmethod
    def _inspection(
        connection: ProviderConnection,
        observation: ProviderRuntimeObservation,
    ) -> ProviderConnectionInspection:
        return ProviderConnectionInspection(
            connection=connection,
            health=observation.health,
            diagnostic_codes=observation.diagnostic_codes,
            observed_at=observation.observed_at,
            cursor_lag_seconds=observation.cursor_lag_seconds,
            last_successful_reconciliation_at=(
                observation.last_successful_reconciliation_at
            ),
        )


def build_provider_connection_audit(
    *,
    request: ProviderConnectionConfigurationRequest,
    connection: ProviderConnection,
    context: TrustedPersistenceContext,
    receipt_id: UUID,
) -> AuditEvent:
    return AuditEventRegistry.with_default_families().build_event(
        audit_event_id=uuid5(
            NAMESPACE_URL,
            f"spine:provider-connection-audit:v1:{receipt_id}",
        ),
        workspace_id=connection.workspace_id,
        environment_id=connection.environment_id,
        event_type="command.outcome",
        schema_version=1,
        payload=CommandAuditPayload(command_type="provider_connection_configuration"),
        origin=context.origin,
        acting_subject_id=context.acting_subject_id,
        service_principal_id=context.service_principal_id,
        trace_id=context.trace_id,
        occurred_at=request.occurred_at,
        target=AuditObjectReference(
            object_type="provider_connection_version",
            object_id=connection.connection_version_id,
            schema_version=1,
        ),
        outcome=AuditOutcome.ACCEPTED,
        reason="connection_version_registered",
        producer_deduplication_id=request.idempotency_key,
    )


def provider_connection_command_digest(
    request: ProviderConnectionConfigurationRequest,
    *,
    workspace_id: UUID,
    environment_id: UUID,
) -> str:
    payload = {
        "schema": "provider-connection-configuration-command:v1",
        "workspace_id": str(workspace_id),
        "environment_id": str(environment_id),
        "approval_id": str(request.approval_id),
        "connection_id": str(request.connection_id),
        "expected_version": request.expected_version,
        "expected_predecessor_version_id": (
            str(request.expected_predecessor_version_id)
            if request.expected_predecessor_version_id is not None
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_provider_connection_successor(
    current: ProviderConnectionInspection | None,
    request: ProviderConnectionConfigurationRequest,
    approval: ProviderConnectionApproval,
) -> None:
    if current is None:
        if request.expected_version != 0 or request.expected_predecessor_version_id is not None:
            raise OptimisticConflictError()
        return
    connection = current.connection
    if (
        connection.version != request.expected_version
        or connection.connection_version_id != request.expected_predecessor_version_id
        or connection.provider_kind is not approval.provider_kind
        or connection.service_principal_id != approval.service_principal_id
        or approval.authorization_generation
        not in {connection.authorization_generation, connection.authorization_generation + 1}
        or approval.configuration_generation < connection.configuration_generation
        or (
            approval.authorization_generation == connection.authorization_generation
            and approval.configuration_generation == connection.configuration_generation
        )
    ):
        raise OptimisticConflictError()


__all__ = [
    "ProviderConnectionConfigurationAdmission",
    "ProviderConnectionConfigurationRequest",
    "ProviderConnectionConfigurationResult",
    "ProviderConnectionInspection",
    "InvalidProviderConnectionRequest",
    "ProviderConnectionManagementError",
    "ProviderConnectionManagementRepository",
    "ProviderConnectionManager",
    "ProviderConnectionNotFoundError",
    "ProviderConnectionObserver",
    "ProviderConnectionPage",
    "ProviderRuntimeObservation",
    "build_provider_connection_audit",
    "provider_connection_command_digest",
    "require_provider_connection_successor",
]
