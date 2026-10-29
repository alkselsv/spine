from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from spine.application.connections.management import (
    ProviderConnectionConfigurationAdmission,
    ProviderConnectionConfigurationRequest,
    ProviderConnectionManager,
    ProviderConnectionNotFoundError,
    ProviderRuntimeObservation,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedPersistenceContext,
)
from spine.domain.common import ContextOrigin
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderCapability,
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnectionApproval,
    ProviderConnectionHealth,
    ProviderConnectionStatus,
    ProviderDiagnosticCode,
    ProviderKind,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


NOW = datetime(2026, 10, 29, 15, tzinfo=timezone.utc)


def _context(
    operation: str,
    *,
    workspace_id: UUID = _id(1),
    environment_id: UUID = _id(2),
) -> TrustedPersistenceContext:
    return TrustedPersistenceContext(
        scope=EnvironmentScope(
            workspace_id=workspace_id,
            environment_id=environment_id,
        ),
        origin=ContextOrigin.INTERACTIVE,
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation(operation),
        trace_id=_id(90),
        acting_subject_id=_id(91),
        service_principal_id=None,
        provenance=TrustedContextProvenance(
            issuer_id=_id(92),
            signature=b"provider-management-test-context",
        ),
    )


def _approval() -> ProviderConnectionApproval:
    return ProviderConnectionApproval(
        approval_id=_id(3),
        workspace_id=_id(1),
        environment_id=_id(2),
        service_principal_id=_id(4),
        provider_kind=ProviderKind.YANDEX_DISK,
        credential_reference=OpaqueProviderReference(value=_id(5)),
        grant_reference=OpaqueProviderReference(value=_id(6)),
        approved_resource_scope_references=(
            OpaqueProviderReference(value=_id(7)),
        ),
        authorization_generation=2,
        configuration_generation=3,
        status=ProviderConnectionStatus.ACTIVE,
    )


def _observation(
    health: ProviderConnectionHealth = ProviderConnectionHealth.HEALTHY,
) -> ProviderRuntimeObservation:
    return ProviderRuntimeObservation(
        capabilities=(
            ProviderCapability(
                kind=ProviderCapabilityKind.BINARY_READ,
                status=ProviderCapabilityStatus.SUPPORTED,
                qualification_reference=OpaqueProviderReference(value=_id(8)),
            ),
            ProviderCapability(
                kind=ProviderCapabilityKind.INCREMENTAL_CHANGE_DISCOVERY,
                status=ProviderCapabilityStatus.UNSUPPORTED,
            ),
        ),
        health=health,
        diagnostic_codes=(
            ProviderDiagnosticCode.CONNECTION_READY
            if health is ProviderConnectionHealth.HEALTHY
            else ProviderDiagnosticCode.PROVIDER_DEGRADED,
        ),
        observed_at=NOW,
        cursor_lag_seconds=12,
        last_successful_reconciliation_at=NOW,
    )


class _Repository:
    def __init__(self, approval: ProviderConnectionApproval | None) -> None:
        self.approval = approval
        self.saved = None
        self.current = None

    async def authorize_configuration(self, request, context):
        if (
            self.approval is None
            or context.scope.workspace_id != self.approval.workspace_id
            or context.scope.environment_id != self.approval.environment_id
        ):
            raise ProviderConnectionNotFoundError()
        return ProviderConnectionConfigurationAdmission(approval=self.approval)

    async def register(self, request, approval, observation, context):
        self.saved = (request, approval, observation, context)
        self.current = ProviderConnectionManager.build_result(
            request=request,
            approval=approval,
            observation=observation,
            context=context,
            replay=False,
        )
        return self.current

    async def list_current(self, context, *, after_connection_id=None, limit=101):
        del context, after_connection_id, limit
        return (self.current.inspection,) if self.current is not None else ()

    async def get_current(self, connection_id, context):
        del context
        if self.current is None or self.current.inspection.connection.connection_id != connection_id:
            return None
        return self.current.inspection


class _Provider:
    def __init__(self, observation: ProviderRuntimeObservation) -> None:
        self.observation = observation
        self.calls = 0

    async def inspect_approved(self, approval):
        del approval
        self.calls += 1
        return self.observation

    async def inspect_connection(self, connection):
        del connection
        self.calls += 1
        return self.observation


def _request() -> ProviderConnectionConfigurationRequest:
    return ProviderConnectionConfigurationRequest(
        approval_id=_id(3),
        connection_id=_id(10),
        expected_version=0,
        expected_predecessor_version_id=None,
        idempotency_key="configure-yandex-1",
        occurred_at=NOW,
    )


@pytest.mark.asyncio
async def test_approved_configuration_creates_immutable_refs_only_version() -> None:
    repository = _Repository(_approval())
    provider = _Provider(_observation())
    manager = ProviderConnectionManager(repository=repository, provider=provider)

    result = await manager.configure(
        _request(),
        _context("provider_connection_configure"),
    )

    connection = result.inspection.connection
    assert connection.version == 1
    assert connection.predecessor_version_id is None
    assert connection.provider_kind is ProviderKind.YANDEX_DISK
    assert connection.credential_reference == OpaqueProviderReference(value=_id(5))
    assert connection.grant_reference == OpaqueProviderReference(value=_id(6))
    assert connection.capabilities == _observation().capabilities
    assert result.inspection.health is ProviderConnectionHealth.HEALTHY
    assert result.replay is False
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_denied_configuration_stops_before_provider_and_hides_approval() -> None:
    provider = _Provider(_observation())
    manager = ProviderConnectionManager(
        repository=_Repository(None),
        provider=provider,
    )

    with pytest.raises(ProviderConnectionNotFoundError) as failure:
        await manager.configure(
            _request(),
            _context(
                "provider_connection_configure",
                workspace_id=_id(20),
                environment_id=_id(21),
            ),
        )

    assert str(failure.value) == "Provider connection is unavailable."
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_missing_connection_inspection_stops_before_provider_call() -> None:
    provider = _Provider(_observation())
    manager = ProviderConnectionManager(
        repository=_Repository(_approval()),
        provider=provider,
    )

    with pytest.raises(ProviderConnectionNotFoundError):
        await manager.inspect(
            connection_id=_id(10),
            context=_context("provider_connection_inspect"),
        )

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_revoked_connection_inspection_uses_persisted_safe_state_without_provider_call() -> None:
    repository = _Repository(_approval())
    provider = _Provider(_observation(ProviderConnectionHealth.REVOKED))
    manager = ProviderConnectionManager(repository=repository, provider=provider)
    result = ProviderConnectionManager.build_result(
        request=_request(),
        approval=_approval().model_copy(update={"status": ProviderConnectionStatus.REVOKED}),
        observation=_observation(ProviderConnectionHealth.REVOKED),
        context=_context("provider_connection_configure"),
        replay=False,
    )
    repository.current = result

    inspected = await manager.inspect(
        connection_id=_id(10),
        context=_context("provider_connection_inspect"),
    )

    assert inspected.health is ProviderConnectionHealth.REVOKED
    assert provider.calls == 0
