"""Deterministic in-memory provider connection management authority."""

from __future__ import annotations

import asyncio
from uuid import UUID

from spine.application.connections.management import (
    ProviderConnectionConfigurationAdmission,
    ProviderConnectionConfigurationRequest,
    ProviderConnectionConfigurationResult,
    ProviderConnectionInspection,
    ProviderConnectionManager,
    ProviderConnectionNotFoundError,
    ProviderRuntimeObservation,
    provider_connection_command_digest,
    require_provider_connection_successor,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
)
from spine.application.persistence.errors import (
    IdempotencyConflictError,
    OptimisticConflictError,
)
from spine.domain.common import ContextOrigin
from spine.domain.connections.provider import ProviderConnectionApproval
from spine.domain.connections.provider import ProviderConnectionStatus


class InMemoryProviderConnectionManagementRepository:
    """Append-only versions with current selection and stable command receipts."""

    def __init__(
        self,
        *,
        context_verifier: TrustedContextVerifier,
        approvals: tuple[ProviderConnectionApproval, ...],
    ) -> None:
        self._context_verifier = context_verifier
        self._lock = asyncio.Lock()
        self._approvals = {item.approval_id: item for item in approvals}
        if len(self._approvals) != len(approvals):
            raise ValueError("provider connection approval identities must be unique")
        self._current: dict[tuple[UUID, UUID, UUID], ProviderConnectionInspection] = {}
        self._commands: dict[
            tuple[UUID, UUID, str],
            tuple[str, ProviderConnectionConfigurationResult],
        ] = {}

    async def authorize_configuration(
        self,
        request: ProviderConnectionConfigurationRequest,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationAdmission:
        trusted = self._configuration_context(context)
        approval = self._approval(request, trusted)
        digest = provider_connection_command_digest(
            request,
            workspace_id=trusted.scope.workspace_id,
            environment_id=trusted.scope.environment_id,
        )
        command_key = (
            trusted.scope.workspace_id,
            trusted.scope.environment_id,
            request.idempotency_key,
        )
        current_key = (
            trusted.scope.workspace_id,
            trusted.scope.environment_id,
            request.connection_id,
        )
        async with self._lock:
            existing = self._commands.get(command_key)
            if existing is not None:
                existing_digest, result = existing
                if existing_digest != digest:
                    raise IdempotencyConflictError()
                return ProviderConnectionConfigurationAdmission(
                    approval=approval,
                    replay=result,
                )
            require_provider_connection_successor(
                self._current.get(current_key), request, approval
            )
        return ProviderConnectionConfigurationAdmission(approval=approval)

    async def register(
        self,
        request: ProviderConnectionConfigurationRequest,
        approval: ProviderConnectionApproval,
        observation: ProviderRuntimeObservation,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionConfigurationResult:
        trusted = self._configuration_context(context)
        canonical_approval = self._approval(request, trusted)
        if canonical_approval != approval:
            raise OptimisticConflictError()
        digest = provider_connection_command_digest(
            request,
            workspace_id=trusted.scope.workspace_id,
            environment_id=trusted.scope.environment_id,
        )
        command_key = (
            trusted.scope.workspace_id,
            trusted.scope.environment_id,
            request.idempotency_key,
        )
        current_key = (
            trusted.scope.workspace_id,
            trusted.scope.environment_id,
            request.connection_id,
        )
        async with self._lock:
            existing = self._commands.get(command_key)
            if existing is not None:
                existing_digest, result = existing
                if existing_digest != digest:
                    raise IdempotencyConflictError()
                return ProviderConnectionConfigurationResult(
                    receipt_id=result.receipt_id,
                    inspection=result.inspection,
                    audit=result.audit,
                    replay=True,
                )
            require_provider_connection_successor(
                self._current.get(current_key), request, approval
            )
            result = ProviderConnectionManager.build_result(
                request=request,
                approval=approval,
                observation=observation,
                context=trusted,
                replay=False,
            )
            self._current[current_key] = result.inspection
            self._commands[command_key] = (digest, result)
            return result

    async def list_current(
        self,
        context: TrustedPersistenceContext,
        *,
        after_connection_id: UUID | None = None,
        limit: int = 101,
    ) -> tuple[ProviderConnectionInspection, ...]:
        trusted = self._read_context(context)
        async with self._lock:
            items = (
                item
                for (workspace_id, environment_id, _), item in self._current.items()
                if workspace_id == trusted.scope.workspace_id
                and environment_id == trusted.scope.environment_id
            )
            ordered = sorted(items, key=lambda item: item.connection.connection_id.int)
            if after_connection_id is not None:
                ordered = [
                    item
                    for item in ordered
                    if item.connection.connection_id.int > after_connection_id.int
                ]
            return tuple(ordered[:limit])

    async def get_current(
        self,
        connection_id: UUID,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionInspection | None:
        trusted = self._read_context(context)
        async with self._lock:
            return self._current.get(
                (
                    trusted.scope.workspace_id,
                    trusted.scope.environment_id,
                    connection_id,
                )
            )

    def _configuration_context(
        self, context: TrustedPersistenceContext
    ) -> TrustedPersistenceContext:
        trusted = self._context_verifier.verify(context)
        if (
            trusted.origin is not ContextOrigin.INTERACTIVE
            or trusted.acting_subject_id is None
            or not isinstance(trusted.scope, EnvironmentScope)
            or trusted.purpose.value != "source_connection_control"
            or trusted.operation.value != "provider_connection_configure"
        ):
            raise ProviderConnectionNotFoundError()
        return trusted

    def _read_context(
        self, context: TrustedPersistenceContext
    ) -> TrustedPersistenceContext:
        trusted = self._context_verifier.verify(context)
        if (
            trusted.origin is not ContextOrigin.INTERACTIVE
            or trusted.acting_subject_id is None
            or not isinstance(trusted.scope, EnvironmentScope)
            or trusted.purpose.value != "source_connection_control"
            or trusted.operation.value
            not in {
                "provider_connection_configure",
                "provider_connection_inspect",
                "provider_connection_list",
            }
        ):
            raise ProviderConnectionNotFoundError()
        return trusted

    def _approval(
        self,
        request: ProviderConnectionConfigurationRequest,
        context: TrustedPersistenceContext,
    ) -> ProviderConnectionApproval:
        approval = self._approvals.get(request.approval_id)
        if (
            approval is None
            or approval.workspace_id != context.scope.workspace_id
            or approval.environment_id != context.scope.environment_id
            or approval.status is not ProviderConnectionStatus.ACTIVE
        ):
            raise ProviderConnectionNotFoundError()
        return approval


__all__ = ["InMemoryProviderConnectionManagementRepository"]
