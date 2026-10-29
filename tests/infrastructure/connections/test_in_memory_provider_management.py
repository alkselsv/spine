from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from uuid import UUID

import pytest

from spine.application.connections.management import (
    ProviderConnectionConfigurationRequest,
    ProviderConnectionManager,
    ProviderRuntimeObservation,
)
from spine.application.persistence import (
    EnvironmentScope,
    IdempotencyConflictError,
    OptimisticConflictError,
    PersistenceOperation,
    PersistencePurpose,
)
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
from spine.infrastructure.connections.provider_management import (
    InMemoryProviderConnectionManagementRepository,
)
from spine.infrastructure.persistence.contexts import TrustedContextBoundary
from tests.contracts.connections.provider_connection_management_contract import (
    ProviderConnectionManagementScenario,
    assert_provider_connection_management_contract,
)


def _id(value: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{value:012d}")


NOW = datetime(2026, 10, 29, 16, tzinfo=timezone.utc)


def _approval(value: int, *, generation: int) -> ProviderConnectionApproval:
    return ProviderConnectionApproval(
        approval_id=_id(value),
        workspace_id=_id(1),
        environment_id=_id(2),
        service_principal_id=_id(3),
        provider_kind=ProviderKind.BITRIX24,
        credential_reference=OpaqueProviderReference(value=_id(4 + generation)),
        grant_reference=OpaqueProviderReference(value=_id(10 + generation)),
        approved_resource_scope_references=(OpaqueProviderReference(value=_id(20)),),
        authorization_generation=generation,
        configuration_generation=generation,
        status=ProviderConnectionStatus.ACTIVE,
    )


class _Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_next = False

    def fail_once(self) -> None:
        self.fail_next = True

    async def inspect_approved(self, approval):
        del approval
        self.calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("synthetic provider interruption")
        return _observation()

    async def inspect_connection(self, connection):
        del connection
        self.calls += 1
        return _observation()


def _observation() -> ProviderRuntimeObservation:
    return ProviderRuntimeObservation(
        capabilities=(ProviderCapability(
            kind=ProviderCapabilityKind.HEALTH_DIAGNOSTICS,
            status=ProviderCapabilityStatus.SUPPORTED,
            qualification_reference=OpaqueProviderReference(value=_id(30)),
        ),),
        health=ProviderConnectionHealth.HEALTHY,
        diagnostic_codes=(ProviderDiagnosticCode.CONNECTION_READY,),
        observed_at=NOW,
    )


def _request(
    *,
    approval_id: UUID = _id(40),
    key: str = "configure-1",
    expected_version: int = 0,
    predecessor: UUID | None = None,
) -> ProviderConnectionConfigurationRequest:
    return ProviderConnectionConfigurationRequest(
        approval_id=approval_id,
        connection_id=_id(50),
        expected_version=expected_version,
        expected_predecessor_version_id=predecessor,
        idempotency_key=key,
        occurred_at=NOW,
    )


def _manager(
    approvals: tuple[ProviderConnectionApproval, ...] | None = None,
    provider: _Provider | None = None,
):
    boundary = TrustedContextBoundary.for_testing(
        issuer_id=_id(60), secret=b"provider-management-repository-secret"
    )
    repository = InMemoryProviderConnectionManagementRepository(
        context_verifier=boundary,
        approvals=approvals or (_approval(40, generation=1), _approval(41, generation=2)),
    )
    context = boundary.interactive(
        scope=EnvironmentScope(workspace_id=_id(1), environment_id=_id(2)),
        acting_subject_id=_id(61),
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation("provider_connection_configure"),
        trace_id=_id(62),
    )
    return ProviderConnectionManager(repository=repository, provider=provider or _Provider()), repository, context


@pytest.mark.asyncio
async def test_same_configuration_command_replays_stable_receipt() -> None:
    manager, repository, context = _manager()

    first = await manager.configure(_request(), context)
    replay = await manager.configure(
        replace(_request(), occurred_at=datetime(2026, 10, 29, 16, 5, tzinfo=timezone.utc)),
        context,
    )

    assert replay.replay is True
    assert replay.receipt_id == first.receipt_id
    assert replay.inspection == first.inspection
    assert await repository.list_current(context) == (first.inspection,)


@pytest.mark.asyncio
async def test_changed_configuration_with_same_key_has_explicit_idempotency_conflict() -> None:
    manager, _, context = _manager()
    await manager.configure(_request(), context)

    with pytest.raises(IdempotencyConflictError):
        await manager.configure(
            _request(approval_id=_id(41)),
            context,
        )


@pytest.mark.asyncio
async def test_concurrent_successor_from_stale_version_has_explicit_optimistic_conflict() -> None:
    manager, _, context = _manager()
    first = await manager.configure(_request(), context)
    predecessor = first.inspection.connection.connection_version_id
    successor = _request(
        approval_id=_id(41),
        key="configure-2",
        expected_version=1,
        predecessor=predecessor,
    )
    stale_competitor = _request(
        approval_id=_id(41),
        key="configure-3",
        expected_version=1,
        predecessor=predecessor,
    )

    accepted = await manager.configure(successor, context)
    with pytest.raises(OptimisticConflictError):
        await manager.configure(stale_competitor, context)

    assert accepted.inspection.connection.version == 2


@pytest.mark.asyncio
async def test_successor_cannot_change_provider_principal_or_regress_generations() -> None:
    initial = _approval(40, generation=2)
    invalid = _approval(41, generation=1).model_copy(
        update={
            "provider_kind": ProviderKind.YANDEX_DISK,
            "service_principal_id": _id(99),
        }
    )
    manager, _, context = _manager((initial, invalid))
    first = await manager.configure(_request(), context)

    with pytest.raises(OptimisticConflictError):
        await manager.configure(
            _request(
                approval_id=invalid.approval_id,
                key="configure-invalid-successor",
                expected_version=1,
                predecessor=first.inspection.connection.connection_version_id,
            ),
            context,
        )


@pytest.mark.asyncio
async def test_in_memory_repository_satisfies_provider_management_contract() -> None:
    provider = _Provider()
    manager, _, context = _manager(provider=provider)
    initial = _request(key="contract-initial")
    created_preview = ProviderConnectionManager.build_result(
        request=initial,
        approval=_approval(40, generation=1),
        observation=_observation(),
        context=context,
        replay=False,
    )
    await assert_provider_connection_management_contract(
        ProviderConnectionManagementScenario(
            manager=manager,
            context=context,
            initial=initial,
            conflicting_replay=_request(
                key=initial.idempotency_key,
                approval_id=_id(41),
            ),
            stalled_successor=_request(
                key="contract-stalled",
                expected_version=1,
                predecessor=created_preview.inspection.connection.connection_version_id,
            ),
            provider_call_count=lambda: provider.calls,
            fail_next_provider_call=provider.fail_once,
        )
    )
