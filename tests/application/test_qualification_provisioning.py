from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from spine.application.qualification_provisioning import (
    InMemoryQualificationProvisioningStore,
    QualificationAuditUnavailable,
    QualificationInvocationRejected,
    QualificationInvocationSource,
    QualificationOperationConflict,
    QualificationOperationRequest,
    QualificationOperationUnavailable,
    QualificationOperatorDenied,
    QualificationProfileRejected,
    QualificationProvisioningService,
)
from spine.auth.records import (
    AuthorizationRecordProvenance,
    AuthorizationRecordStatus,
    QualificationAuthorityBinding,
    QualificationPrincipalKind,
)
from spine.domain.qualification import (
    QualificationOpaqueReference,
    QualificationOperation,
    QualificationOperationState,
    QualificationProfile,
)


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
WORKSPACE_ID = UUID("20000000-0000-0000-0000-000000000001")
ENVIRONMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
QUALIFICATION_ID = UUID("40000000-0000-0000-0000-000000000001")
BINDING_ID = UUID("50000000-0000-0000-0000-000000000001")
PROFILE_ID = UUID("60000000-0000-0000-0000-000000000001")
PRINCIPAL_ID = UUID("80000000-0000-0000-0000-000000000001")


class AllowOperator:
    async def validate(self, **_: object) -> bool:
        return True


class AllowPolicies:
    async def validate(self, profile: QualificationProfile) -> bool:
        assert profile.access_policy_references
        return True


class SecretProvider:
    def __init__(self, *, fail_bind: bool = False, fail_revoke: bool = False) -> None:
        self.fail_bind = fail_bind
        self.fail_revoke = fail_revoke
        self.bind_calls: list[UUID] = []
        self.revoke_calls: list[UUID] = []
        self.cleanup_calls: list[UUID] = []

    async def bind(self, *, operation_id: UUID, profile: QualificationProfile) -> None:
        assert all(reference.value.startswith("secret:") for reference in profile.secret_references)
        self.bind_calls.append(operation_id)
        if self.fail_bind:
            self.fail_bind = False
            raise RuntimeError("provider failure must not cross the seam")

    async def revoke(self, *, operation_id: UUID, profile: QualificationProfile) -> None:
        del profile
        self.revoke_calls.append(operation_id)
        if self.fail_revoke:
            self.fail_revoke = False
            raise RuntimeError("provider failure must not cross the seam")

    async def cleanup(self, *, operation_id: UUID, profile: QualificationProfile) -> None:
        del profile
        self.cleanup_calls.append(operation_id)


def binding() -> QualificationAuthorityBinding:
    return QualificationAuthorityBinding(
        binding_id=BINDING_ID,
        qualification_id=QUALIFICATION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        principal_kind=QualificationPrincipalKind.HUMAN,
        principal_id=PRINCIPAL_ID,
        expires_at=NOW + timedelta(hours=1),
        version=1,
        status=AuthorizationRecordStatus.DISABLED,
        provenance=AuthorizationRecordProvenance(
            change_id=UUID("70000000-0000-0000-0000-000000000001"),
            recorded_by="qualification-profile-test",
        ),
    )


def profile(
    *, profile_version: int = 1, target_ref: str = "target:source"
) -> QualificationProfile:
    return QualificationProfile.create(
        profile_id=PROFILE_ID,
        profile_version=profile_version,
        qualification_id=QUALIFICATION_ID,
        workspace_id=WORKSPACE_ID,
        environment_id=ENVIRONMENT_ID,
        target_reference=QualificationOpaqueReference(value=target_ref),
        binding=binding(),
        allowed_operations=frozenset(
            {
                QualificationOperation.PROVISION,
                QualificationOperation.REVOKE,
                QualificationOperation.CLEANUP,
            }
        ),
        secret_references=(QualificationOpaqueReference(value="secret:source"),),
        database_role_references=(QualificationOpaqueReference(value="role:runtime"),),
        access_policy_references=(QualificationOpaqueReference(value="policy:approved"),),
        approved=True,
    )


def request(
    operation: QualificationOperation,
    *,
    operation_id: UUID,
    profile_value: QualificationProfile | None = None,
) -> QualificationOperationRequest:
    return QualificationOperationRequest(
        operation_id=operation_id,
        operation=operation,
        profile=profile_value or profile(),
        operator_reference=QualificationOpaqueReference(value="operator:one"),
        approval_reference=QualificationOpaqueReference(value="change:approved"),
    )


def service(
    store: InMemoryQualificationProvisioningStore,
    secrets: SecretProvider,
) -> QualificationProvisioningService:
    return QualificationProvisioningService(
        store=store,
        operator_authorization=AllowOperator(),
        policy_references=AllowPolicies(),
        secrets=secrets,
    )


@pytest.mark.asyncio
async def test_non_operator_invocation_is_rejected_without_mutation() -> None:
    store = InMemoryQualificationProvisioningStore()
    secrets = SecretProvider()

    with pytest.raises(QualificationInvocationRejected):
        await service(store, secrets).execute(
            request(QualificationOperation.PROVISION, operation_id=UUID("90000000-0000-0000-0000-000000000001")),
            invocation_source=QualificationInvocationSource.API_REQUEST,
        )

    assert not store.operations
    assert not secrets.bind_calls


@pytest.mark.asyncio
async def test_operator_and_policy_are_independent_gates() -> None:
    store = InMemoryQualificationProvisioningStore()
    secrets = SecretProvider()
    denied = QualificationProvisioningService(
        store=store,
        operator_authorization=type("Denied", (), {"validate": lambda *_args, **_kwargs: False})(),
        policy_references=AllowPolicies(),
        secrets=secrets,
    )

    with pytest.raises(QualificationOperatorDenied):
        await denied.execute(
            request(QualificationOperation.PROVISION, operation_id=UUID("90000000-0000-0000-0000-000000000002")),
            invocation_source=QualificationInvocationSource.OPERATOR_COMMAND,
        )

    assert not store.operations


@pytest.mark.asyncio
async def test_provision_is_inactive_first_atomic_and_idempotent() -> None:
    store = InMemoryQualificationProvisioningStore(
        initial_generations={(WORKSPACE_ID, ENVIRONMENT_ID): 7}
    )
    secrets = SecretProvider()
    command = request(
        QualificationOperation.PROVISION,
        operation_id=UUID("90000000-0000-0000-0000-000000000003"),
    )

    result = await service(store, secrets).execute(
        command, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )
    replay = await service(store, secrets).execute(
        command, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )

    assert result.state is QualificationOperationState.ACTIVE
    assert result.generation_before == 7
    assert result.generation_after == 8
    assert replay.replay is True
    assert replay.audit_reference == result.audit_reference
    assert len(secrets.bind_calls) == 1
    assert store.histories[BINDING_ID][-1].status is AuthorizationRecordStatus.ACTIVE
    assert len(store.audits) == 1


@pytest.mark.asyncio
async def test_same_operation_id_with_changed_authority_inputs_conflicts() -> None:
    store = InMemoryQualificationProvisioningStore()
    secrets = SecretProvider()
    operation_id = UUID("90000000-0000-0000-0000-000000000004")
    first = request(QualificationOperation.PROVISION, operation_id=operation_id)
    await service(store, secrets).execute(
        first, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )

    with pytest.raises(QualificationOperationConflict):
        await service(store, secrets).execute(
            request(
                QualificationOperation.PROVISION,
                operation_id=operation_id,
                profile_value=profile(profile_version=2),
            ),
            invocation_source=QualificationInvocationSource.OPERATOR_COMMAND,
        )


@pytest.mark.asyncio
async def test_binding_cannot_be_reused_for_another_target() -> None:
    store = InMemoryQualificationProvisioningStore()
    secrets = SecretProvider()
    first = request(
        QualificationOperation.PROVISION,
        operation_id=UUID("90000000-0000-0000-0000-000000000012"),
    )
    await service(store, secrets).execute(
        first, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )

    with pytest.raises(QualificationOperationConflict):
        await service(store, secrets).execute(
            request(
                QualificationOperation.PROVISION,
                operation_id=UUID("90000000-0000-0000-0000-000000000013"),
                profile_value=profile(target_ref="target:restore"),
            ),
            invocation_source=QualificationInvocationSource.OPERATOR_COMMAND,
        )


@pytest.mark.asyncio
async def test_partial_secret_failure_keeps_binding_inactive_and_retry_is_safe() -> None:
    store = InMemoryQualificationProvisioningStore()
    secrets = SecretProvider(fail_bind=True)
    command = request(
        QualificationOperation.PROVISION,
        operation_id=UUID("90000000-0000-0000-0000-000000000005"),
    )

    with pytest.raises(QualificationOperationUnavailable) as failure:
        await service(store, secrets).execute(
            command, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
        )

    assert failure.value.receipt.state is QualificationOperationState.INCOMPLETE
    assert store.histories[BINDING_ID][-1].status is AuthorizationRecordStatus.DISABLED
    result = await service(store, secrets).execute(
        command, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )
    assert result.state is QualificationOperationState.ACTIVE


@pytest.mark.asyncio
async def test_audit_failure_never_activates_binding() -> None:
    store = InMemoryQualificationProvisioningStore(audit_available=False)
    secrets = SecretProvider()
    command = request(
        QualificationOperation.PROVISION,
        operation_id=UUID("90000000-0000-0000-0000-000000000006"),
    )

    with pytest.raises(QualificationAuditUnavailable) as failure:
        await service(store, secrets).execute(
            command, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
        )

    assert failure.value.receipt.state is QualificationOperationState.INCOMPLETE
    assert store.histories[BINDING_ID][-1].status is AuthorizationRecordStatus.DISABLED
    assert not store.audits


@pytest.mark.asyncio
async def test_revoke_denies_before_secret_cleanup_and_exposes_pending_state() -> None:
    store = InMemoryQualificationProvisioningStore(
        initial_generations={(WORKSPACE_ID, ENVIRONMENT_ID): 7}
    )
    secrets = SecretProvider(fail_revoke=True)
    provision = request(
        QualificationOperation.PROVISION,
        operation_id=UUID("90000000-0000-0000-0000-000000000007"),
    )
    await service(store, secrets).execute(
        provision, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )

    revoke = request(
        QualificationOperation.REVOKE,
        operation_id=UUID("90000000-0000-0000-0000-000000000008"),
    )
    result = await service(store, secrets).execute(
        revoke, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )

    assert result.state is QualificationOperationState.CLEANUP_PENDING
    assert store.histories[BINDING_ID][-1].status is AuthorizationRecordStatus.REVOKED
    assert store.generations[(WORKSPACE_ID, ENVIRONMENT_ID)] == 9

    cleanup = request(
        QualificationOperation.CLEANUP,
        operation_id=UUID("90000000-0000-0000-0000-000000000009"),
    )
    cleaned = await service(store, secrets).execute(
        cleanup, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )
    assert cleaned.state is QualificationOperationState.CLEANED


@pytest.mark.asyncio
async def test_revoke_audit_failure_preserves_active_authority() -> None:
    provision_store = InMemoryQualificationProvisioningStore()
    secrets = SecretProvider()
    provision = request(
        QualificationOperation.PROVISION,
        operation_id=UUID("90000000-0000-0000-0000-000000000010"),
    )
    await service(provision_store, secrets).execute(
        provision, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
    )
    provision_store.audit_available = False
    revoke = request(
        QualificationOperation.REVOKE,
        operation_id=UUID("90000000-0000-0000-0000-000000000011"),
    )

    with pytest.raises(QualificationAuditUnavailable):
        await service(provision_store, secrets).execute(
            revoke, invocation_source=QualificationInvocationSource.OPERATOR_COMMAND
        )

    assert provision_store.histories[BINDING_ID][-1].status is AuthorizationRecordStatus.ACTIVE
    assert provision_store.operations[revoke.operation_id].state is QualificationOperationState.INCOMPLETE
