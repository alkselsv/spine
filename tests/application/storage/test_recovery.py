from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from spine.application.storage.recovery import (
    BackupRecoveryCoordinator,
    FenceAdmissionBlockedError,
    FenceOwnershipError,
    QuiescenceBlockedError,
    RecoveryManifestError,
)
from spine.infrastructure.persistence.backup_recovery_in_memory import (
    InMemoryRecoveryRepository,
)
from spine.domain.storage.recovery import (
    ComponentState,
    FenceOperationKind,
    FenceOperationState,
    FenceState,
    RecoveryFailureCategory,
    RecoveryComponentEvidence,
    RecoveryManifestStatus,
    TicketAdmission,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class EvidenceProvider:
    def __init__(self, *, verified: bool = True) -> None:
        self.verified = verified

    async def capture_quiescent_watermark(
        self,
        deployment_scope_id: str,
        fence_epoch: int,
        cutoff_ticket_sequence: int,
    ) -> str:
        del deployment_scope_id, fence_epoch, cutoff_ticket_sequence
        return "pg:captured"

    async def verify_component(
        self,
        deployment_scope_id: str,
        fence_epoch: int,
        component: str,
        boundary: str,
    ) -> RecoveryComponentEvidence:
        del deployment_scope_id, fence_epoch, boundary
        identity = "backup:pg-1" if component == "postgres" else "volume:1"
        return RecoveryComponentEvidence(
            component=component,
            expected_identity=identity,
            observed_identity=identity,
            durable=self.verified,
            verified=self.verified,
            verification_reference=f"verify:{component}",
        )


def coordinator() -> tuple[BackupRecoveryCoordinator, Clock]:
    clock = Clock()
    ids = iter(("ticket-1", "manifest-1"))
    return (
        BackupRecoveryCoordinator(
            repository=InMemoryRecoveryRepository(),
            ticket_id_factory=lambda: next(ids),
            recovery_set_id_factory=lambda: next(ids),
            clock=clock,
            evidence_provider=EvidenceProvider(),
        ),
        clock,
    )


@pytest.mark.asyncio
async def test_admission_and_fence_activation_share_one_serialized_boundary() -> None:
    backup, _ = coordinator()
    admitted = await backup.admit_operation(
        "deployment-1",
        FenceOperationKind.FINALIZATION,
        "object:1",
    )

    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=5),
    )

    assert admitted.ticket_sequence == 1
    assert lease.cutoff_ticket_sequence == 1
    assert lease.ticket_admission is TicketAdmission.CLOSED
    with pytest.raises(FenceAdmissionBlockedError):
        await backup.admit_operation(
            "deployment-1",
            FenceOperationKind.ASSOCIATION,
            "receipt:2",
        )


@pytest.mark.asyncio
async def test_quiescence_requires_every_pre_fence_ticket_to_be_terminal() -> None:
    backup, _ = coordinator()
    ticket = await backup.admit_operation(
        "deployment-1",
        FenceOperationKind.FINALIZATION,
        "object:1",
    )
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=5),
    )

    with pytest.raises(QuiescenceBlockedError):
        await backup.mark_quiescent(
            "deployment-1",
            fence_epoch=lease.active_fence_epoch or 0,
            lease_owner="coordinator-a",
        )

    await backup.transition_ticket(
        "deployment-1",
        ticket.ticket_id,
        FenceOperationState.EXECUTING,
        fence_epoch=lease.active_fence_epoch,
        lease_owner="coordinator-a",
    )
    await backup.transition_ticket(
        "deployment-1",
        ticket.ticket_id,
        FenceOperationState.TERMINAL_SUCCEEDED,
        fence_epoch=lease.active_fence_epoch,
        lease_owner="coordinator-a",
    )
    evidence = await backup.mark_quiescent(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
    )

    assert evidence.pre_fence_terminal_ticket_count == 1
    assert (await backup.lease("deployment-1")).fence_state is FenceState.QUIESCENT


@pytest.mark.asyncio
async def test_coherent_manifest_is_persisted_before_release_and_stays_immutable() -> None:
    backup, _ = coordinator()
    ticket = await backup.admit_operation(
        "deployment-1",
        FenceOperationKind.ASSOCIATION,
        "receipt:1",
    )
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=5),
    )
    await backup.transition_ticket(
        "deployment-1",
        ticket.ticket_id,
        FenceOperationState.EXECUTING,
        fence_epoch=lease.active_fence_epoch,
        lease_owner="coordinator-a",
    )
    await backup.transition_ticket(
        "deployment-1",
        ticket.ticket_id,
        FenceOperationState.RECONCILED_ORPHAN,
        fence_epoch=lease.active_fence_epoch,
        lease_owner="coordinator-a",
    )
    evidence = await backup.mark_quiescent(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
    )
    await backup.begin_snapshot(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
    )
    await backup.begin_validation(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
    )
    backup._evidence_provider.verified = False
    with pytest.raises(RecoveryManifestError):
        await backup.persist_coherent_manifest(
            "deployment-1",
            fence_epoch=lease.active_fence_epoch or 0,
            lease_owner="coordinator-a",
            postgres_boundary="snapshot:pg-1",
            object_boundary="snapshot:volume-1",
            scope="deployment-1",
        )
    backup._evidence_provider.verified = True
    manifest = await backup.persist_coherent_manifest(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
        postgres_boundary="snapshot:pg-1",
        object_boundary="snapshot:volume-1",
        scope="deployment-1",
    )

    assert manifest.manifest_status is RecoveryManifestStatus.COHERENT
    releasing = await backup.lease("deployment-1")
    assert releasing.fence_state is FenceState.RELEASING
    released = await backup.release_fence(
        "deployment-1",
        fence_epoch=manifest.fence_epoch,
        lease_owner="coordinator-a",
    )
    assert released.fence_state is FenceState.RELEASED
    assert released.ticket_admission is TicketAdmission.OPEN


@pytest.mark.asyncio
async def test_expired_coordinator_cannot_release_after_takeover() -> None:
    backup, clock = coordinator()
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=1),
    )
    clock.value += timedelta(minutes=2)
    with pytest.raises(FenceOwnershipError):
        await backup.renew_fence(
            "deployment-1",
            fence_epoch=lease.active_fence_epoch or 0,
            lease_owner="coordinator-a",
            lease_duration=timedelta(minutes=5),
        )

    takeover = await backup.take_over_fence(
        "deployment-1",
        lease_owner="coordinator-b",
        lease_duration=timedelta(minutes=5),
        takeover_reference="takeover:1",
    )

    assert takeover.active_fence_epoch == (lease.active_fence_epoch or 0) + 1
    with pytest.raises(FenceOwnershipError):
        await backup.release_fence(
            "deployment-1",
            fence_epoch=lease.active_fence_epoch or 0,
            lease_owner="coordinator-a",
            degraded=True,
        )


@pytest.mark.asyncio
async def test_expired_coordinator_cannot_transition_ticket_before_takeover() -> None:
    backup, clock = coordinator()
    ticket = await backup.admit_operation(
        "deployment-1",
        FenceOperationKind.FINALIZATION,
        "object:1",
    )
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=1),
    )
    clock.value += timedelta(minutes=2)

    with pytest.raises(FenceOwnershipError):
        await backup.transition_ticket(
            "deployment-1",
            ticket.ticket_id,
            FenceOperationState.EXECUTING,
            fence_epoch=lease.active_fence_epoch,
            lease_owner="coordinator-a",
        )


@pytest.mark.asyncio
async def test_snapshot_failure_releases_only_after_recording_degraded_attempt() -> None:
    backup, _ = coordinator()
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=5),
    )
    await backup.mark_quiescent(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
    )
    await backup.begin_snapshot(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
    )

    failed = await backup.fail_attempt(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
        reason=RecoveryFailureCategory.COMPONENT_FAILURE,
    )
    assert failed.fence_state is FenceState.RELEASING
    assert failed.ticket_admission is TicketAdmission.BLOCKED

    released = await backup.release_fence(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
    )
    assert released.fence_state is FenceState.RELEASED


@pytest.mark.asyncio
async def test_indeterminate_ticket_blocks_quiescence_and_failure_blocks_admission() -> None:
    backup, _ = coordinator()
    ticket = await backup.admit_operation(
        "deployment-1",
        FenceOperationKind.DELETION_CLEANUP,
        "object:1",
    )
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=5),
    )
    await backup.transition_ticket(
        "deployment-1",
        ticket.ticket_id,
        FenceOperationState.EXECUTING,
        fence_epoch=lease.active_fence_epoch,
        lease_owner="coordinator-a",
    )
    await backup.transition_ticket(
        "deployment-1",
        ticket.ticket_id,
        FenceOperationState.INDETERMINATE,
        fence_epoch=lease.active_fence_epoch,
        lease_owner="coordinator-a",
    )

    with pytest.raises(QuiescenceBlockedError):
        await backup.mark_quiescent(
            "deployment-1",
            fence_epoch=lease.active_fence_epoch or 0,
            lease_owner="coordinator-a",
        )

    failed = await backup.fail_attempt(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
        reason=RecoveryFailureCategory.INDETERMINATE_OPERATION,
        release_degraded=True,
    )
    assert failed.fence_state is FenceState.RELEASE_DEGRADED
    with pytest.raises(FenceAdmissionBlockedError):
        await backup.admit_operation(
            "deployment-1",
            FenceOperationKind.ASSOCIATION,
            "receipt:2",
        )


@pytest.mark.asyncio
async def test_profile_b_persists_explicit_incomplete_best_effort_evidence() -> None:
    backup, _ = coordinator()
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=5),
    )

    manifest = await backup.persist_best_effort_manifest(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
        postgres_boundary="snapshot:pg-partial",
        postgres_recovery_watermark="pg:end",
        object_volume_generation="volume:partial",
        scope="deployment-1",
        postgres_component_state=ComponentState.IDENTIFIED,
        object_component_state=ComponentState.IDENTIFIED,
        start_watermark="pg:start",
        end_watermark="pg:end",
    )

    assert manifest.profile.value == "best_effort"
    assert manifest.manifest_status is RecoveryManifestStatus.INCOMPLETE
    assert manifest.completed_at is None


@pytest.mark.asyncio
async def test_profile_b_classifies_unfinished_pre_fence_ticket_as_indeterminate() -> None:
    backup, _ = coordinator()
    await backup.admit_operation(
        "deployment-1",
        FenceOperationKind.FINALIZATION,
        "object:1",
    )
    lease = await backup.activate_fence(
        "deployment-1",
        lease_owner="coordinator-a",
        lease_duration=timedelta(minutes=5),
    )

    manifest = await backup.persist_best_effort_manifest(
        "deployment-1",
        fence_epoch=lease.active_fence_epoch or 0,
        lease_owner="coordinator-a",
        postgres_boundary="snapshot:pg-partial",
        postgres_recovery_watermark="pg:forged",
        object_volume_generation="volume:partial",
        scope="deployment-1",
        postgres_component_state=ComponentState.IDENTIFIED,
        object_component_state=ComponentState.IDENTIFIED,
        start_watermark="pg:start",
        end_watermark="pg:end",
    )

    assert manifest.indeterminate_operation_count == 1
    assert manifest.drained_operation_count == 0
    assert manifest.postgres_recovery_watermark is None


@pytest.mark.asyncio
async def test_concurrent_admission_has_one_ticket_before_activation() -> None:
    backup, _ = coordinator()
    results = await asyncio.gather(
        backup.admit_operation("deployment-1", FenceOperationKind.FINALIZATION, "object:1"),
        backup.admit_operation("deployment-1", FenceOperationKind.FINALIZATION, "object:2"),
    )

    assert {ticket.ticket_sequence for ticket in results} == {1, 2}
