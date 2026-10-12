from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from spine.domain.storage.recovery import (
    ComponentState,
    QuiescentEvidence,
    RecoveryEvidenceEventKind,
    RecoveryEvidenceFailure,
    RecoveryEvidenceInvalidation,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    RecoveryTrustState,
    StorageRecoveryManifest,
    derive_recovery_readiness,
    verify_recovery_evidence_chain,
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


def invalidation(
    *,
    sequence: int,
    target: StorageRecoveryManifest,
    previous_event_digest: str | None = None,
    event_kind: RecoveryEvidenceEventKind = RecoveryEvidenceEventKind.INVALIDATED,
    replacement: StorageRecoveryManifest | None = None,
) -> RecoveryEvidenceInvalidation:
    return RecoveryEvidenceInvalidation.build(
        deployment_scope_id="deployment-1",
        sequence=sequence,
        recovery_set_id=target.recovery_set_id,
        manifest_digest=target.manifest_digest,
        event_kind=event_kind,
        reason_code="component_identity_mismatch",
        replacement_recovery_set_id=(
            replacement.recovery_set_id if replacement is not None else None
        ),
        replacement_manifest_digest=(
            replacement.manifest_digest if replacement is not None else None
        ),
        replacement_validation_reference=(
            "validation:replacement" if replacement is not None else None
        ),
        replacement_approval_reference=(
            "approval:replacement" if replacement is not None else None
        ),
        previous_event_digest=previous_event_digest,
        occurred_at=NOW,
    )


def test_invalidation_chain_is_contiguous_and_hash_linked() -> None:
    original = manifest()
    replacement = manifest("recovery-2", created_at=NOW.replace(minute=1))
    first = invalidation(sequence=1, target=original)
    second = invalidation(
        sequence=2,
        target=original,
        previous_event_digest=first.event_digest,
        event_kind=RecoveryEvidenceEventKind.REPLACED,
        replacement=replacement,
    )

    result = verify_recovery_evidence_chain(
        "deployment-1",
        manifests=(original, replacement),
        invalidations=(first, second),
    )

    assert result.chain_complete is True
    assert result.trust_state is RecoveryTrustState.TRUSTED
    assert result.current_recovery_set_id == replacement.recovery_set_id
    assert result.failure is None


def test_missing_or_wrongly_linked_event_fails_closed() -> None:
    original = manifest()
    first = invalidation(sequence=1, target=original)
    missing_second = invalidation(
        sequence=3,
        target=original,
        previous_event_digest=first.event_digest,
    )

    result = verify_recovery_evidence_chain(
        "deployment-1",
        manifests=(original,),
        invalidations=(first, missing_second),
    )

    assert result.trust_state is RecoveryTrustState.INDETERMINATE
    assert result.chain_complete is False
    assert result.failure is RecoveryEvidenceFailure.NON_CONTIGUOUS_SEQUENCE
    assert derive_recovery_readiness(result).ready is False


def test_restore_reconciliation_rejects_a_valid_prefix_of_a_longer_chain() -> None:
    original = manifest()
    first = invalidation(sequence=1, target=original)
    second = invalidation(
        sequence=2,
        target=original,
        previous_event_digest=first.event_digest,
    )

    result = verify_recovery_evidence_chain(
        "deployment-1",
        manifests=(original,),
        invalidations=(first,),
        expected_last_sequence=second.sequence,
        expected_last_event_digest=second.event_digest,
    )

    assert result.trust_state is RecoveryTrustState.INDETERMINATE
    assert result.failure is RecoveryEvidenceFailure.TRUNCATED_CHAIN


def test_invalidated_manifest_remains_historical_but_untrusted() -> None:
    original = manifest()
    event = invalidation(sequence=1, target=original)

    result = verify_recovery_evidence_chain(
        "deployment-1",
        manifests=(original,),
        invalidations=(event,),
    )

    assert result.trust_state is RecoveryTrustState.INVALIDATED
    assert result.current_recovery_set_id == original.recovery_set_id
    assert derive_recovery_readiness(result).code == "recovery.evidence_invalidated"


def test_replacement_requires_independent_validation_and_approval_references() -> None:
    original = manifest()

    with pytest.raises(ValidationError, match="replacement validation"):
        RecoveryEvidenceInvalidation.build(
            deployment_scope_id="deployment-1",
            sequence=1,
            recovery_set_id=original.recovery_set_id,
            manifest_digest=original.manifest_digest,
            event_kind=RecoveryEvidenceEventKind.REPLACED,
            reason_code="component_identity_mismatch",
            replacement_recovery_set_id="recovery-2",
            replacement_manifest_digest="a" * 64,
            replacement_approval_reference="approval:replacement",
            occurred_at=NOW,
        )


def test_unknown_event_kind_is_rejected() -> None:
    original = manifest()

    with pytest.raises(ValidationError, match="event_kind"):
        RecoveryEvidenceInvalidation.build(
            deployment_scope_id="deployment-1",
            sequence=1,
            recovery_set_id=original.recovery_set_id,
            manifest_digest=original.manifest_digest,
            event_kind="unknown",
            reason_code="component_identity_mismatch",
            occurred_at=NOW,
        )
