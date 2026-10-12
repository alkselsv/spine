from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from spine.application.storage.recovery_integrity import (
    RecoveryEvidenceConflictError,
    RecoveryEvidenceCoordinator,
    RecoveryEvidenceValidationError,
)
from spine.domain.storage.recovery import (
    ComponentState,
    FenceOperationKind,
    QuiescentEvidence,
    RecoveryEvidenceEventKind,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
    RecoveryTrustState,
)
from spine.infrastructure.persistence.backup_recovery_in_memory import (
    InMemoryRecoveryRepository,
)


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def manifest(
    recovery_set_id: str = "recovery-1",
    *,
    created_at: datetime = NOW,
) -> StorageRecoveryManifest:
    evidence = QuiescentEvidence.build(
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        watermark="pg:1",
        pre_fence_terminal_ticket_count=0,
        captured_at=created_at,
    )
    return StorageRecoveryManifest.build(
        recovery_set_id=recovery_set_id,
        deployment_scope_id="deployment-1",
        profile=RecoveryManifestProfile.COORDINATED,
        manifest_status=RecoveryManifestStatus.COHERENT,
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        quiescent_evidence=evidence,
        postgres_boundary="snapshot:pg-1",
        postgres_recovery_watermark="pg:1",
        object_volume_generation="volume:1",
        postgres_component_state=ComponentState.VERIFIED,
        object_component_state=ComponentState.VERIFIED,
        postgres_backup_identity="backup:pg-1",
        postgres_verification_reference="verify:pg-1",
        object_verification_reference="verify:volume-1",
        drained_operation_count=0,
        orphaned_operation_count=0,
        indeterminate_operation_count=0,
        scope="deployment-1",
        created_at=created_at,
        completed_at=created_at,
    )


def coordinator() -> tuple[RecoveryEvidenceCoordinator, InMemoryRecoveryRepository]:
    repository = InMemoryRecoveryRepository()
    return RecoveryEvidenceCoordinator(repository=repository), repository


@pytest.mark.asyncio
async def test_append_rejects_stale_sequence_and_previous_digest() -> None:
    recovery, repository = coordinator()
    original = manifest()
    await repository.seed_manifest(original)

    first = await recovery.append_invalidation(
        "deployment-1",
        recovery_set_id=original.recovery_set_id,
        manifest_digest=original.manifest_digest,
        reason_code="component_identity_mismatch",
    )

    with pytest.raises(RecoveryEvidenceConflictError):
        await recovery.append_invalidation(
            "deployment-1",
            recovery_set_id=original.recovery_set_id,
            manifest_digest=original.manifest_digest,
            reason_code="component_identity_mismatch",
            expected_sequence=1,
            expected_previous_event_digest=None,
        )

    assert first.sequence == 1
    assert (await recovery.verify("deployment-1")).trust_state is RecoveryTrustState.INVALIDATED


@pytest.mark.asyncio
async def test_concurrent_appenders_receive_distinct_contiguous_sequences() -> None:
    recovery, repository = coordinator()
    original = manifest()
    await repository.seed_manifest(original)

    events = await asyncio.gather(
        recovery.append_invalidation(
            "deployment-1",
            recovery_set_id=original.recovery_set_id,
            manifest_digest=original.manifest_digest,
            reason_code="component_identity_mismatch",
        ),
        recovery.append_invalidation(
            "deployment-1",
            recovery_set_id=original.recovery_set_id,
            manifest_digest=original.manifest_digest,
            reason_code="component_identity_mismatch",
        ),
    )

    assert {event.sequence for event in events} == {1, 2}
    assert (await recovery.verify("deployment-1")).chain_complete is True


@pytest.mark.asyncio
async def test_replacement_is_accepted_only_after_validation_and_approval_references() -> None:
    recovery, repository = coordinator()
    original = manifest()
    replacement = manifest("recovery-2", created_at=NOW.replace(minute=1))
    await repository.seed_manifest(original)
    await repository.seed_manifest(replacement)
    first = await recovery.append_invalidation(
        "deployment-1",
        recovery_set_id=original.recovery_set_id,
        manifest_digest=original.manifest_digest,
        reason_code="component_identity_mismatch",
    )

    with pytest.raises(RecoveryEvidenceValidationError):
        await recovery.append_invalidation(
            "deployment-1",
            recovery_set_id=original.recovery_set_id,
            manifest_digest=original.manifest_digest,
            reason_code="replacement",
            event_kind=RecoveryEvidenceEventKind.REPLACED,
            replacement_recovery_set_id=replacement.recovery_set_id,
            replacement_manifest_digest=replacement.manifest_digest,
            expected_sequence=2,
            expected_previous_event_digest=first.event_digest,
        )

    second = await recovery.append_invalidation(
        "deployment-1",
        recovery_set_id=original.recovery_set_id,
        manifest_digest=original.manifest_digest,
        reason_code="replacement",
        event_kind=RecoveryEvidenceEventKind.REPLACED,
        replacement_recovery_set_id=replacement.recovery_set_id,
        replacement_manifest_digest=replacement.manifest_digest,
        replacement_validation_reference="validation:replacement",
        replacement_approval_reference="approval:replacement",
        expected_sequence=2,
        expected_previous_event_digest=first.event_digest,
    )

    assert second.sequence == 2
    assert (await recovery.verify("deployment-1")).trust_state is RecoveryTrustState.TRUSTED


@pytest.mark.asyncio
async def test_isolated_restore_reconciliation_does_not_mutate_repository() -> None:
    recovery, repository = coordinator()
    original = manifest()
    await repository.seed_manifest(original)
    event = await recovery.append_invalidation(
        "deployment-1",
        recovery_set_id=original.recovery_set_id,
        manifest_digest=original.manifest_digest,
        reason_code="component_identity_mismatch",
    )
    before = await recovery.verify("deployment-1")

    restored = recovery.reconcile_isolated_restore(
        "deployment-1",
        manifests=(original,),
        invalidations=(event,),
        expected_last_sequence=event.sequence,
        expected_last_event_digest=event.event_digest,
    )

    assert restored == before
    assert (await recovery.verify("deployment-1")) == before


@pytest.mark.asyncio
async def test_failed_append_is_rolled_back_and_history_remains_unchanged() -> None:
    recovery, repository = coordinator()
    original = manifest()
    await repository.seed_manifest(original)

    with pytest.raises(RecoveryEvidenceValidationError):
        await recovery.append_invalidation(
            "deployment-1",
            recovery_set_id=original.recovery_set_id,
            manifest_digest="f" * 64,
            reason_code="component_identity_mismatch",
        )

    result = await recovery.verify("deployment-1")
    assert result.trust_state is RecoveryTrustState.TRUSTED
