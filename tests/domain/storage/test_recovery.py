from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from spine.domain.storage.recovery import (
    BackupFenceLease,
    ComponentState,
    FenceOperationKind,
    FenceOperationState,
    FenceState,
    QuiescentEvidence,
    RecoveryComponentEvidence,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
    TicketAdmission,
    validate_ticket_transition,
)
from spine.domain.storage.backup_profile import BackupProtectionProfile


NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
INCLUDED_SET_DIGEST = "1" * 64


def protection_profile(source_volume_id: str) -> BackupProtectionProfile:
    return BackupProtectionProfile(
        source_volume_id=source_volume_id,
        destination_id="destination:protected",
        encryption_at_rest_profile="encryption:approved",
        protected_transfer_profile="transfer:approved",
        backup_identity="identity:backup",
        restore_identity="identity:restore",
    )


def component_evidence(component: str, *, included_set_digest: str = INCLUDED_SET_DIGEST) -> RecoveryComponentEvidence:
    identity = "backup:pg-1" if component == "postgres" else "volume:17"
    return RecoveryComponentEvidence(
        component=component,
        expected_identity=identity,
        observed_identity=identity,
        artifact_reference=f"artifact:{component}-1",
        artifact_digest="2" * 64,
        tool_profile=(
            "pg_dump-custom-compatible-restore:v1"
            if component == "postgres"
            else "linux-posix-finalized-originals:v1"
        ),
        tool_version="tool-17.0",
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        included_set_digest=included_set_digest,
        inclusion_policy="r1-finalized-originals-and-canonical-metadata:v1",
        durable=True,
        verified=True,
        verification_reference=(
            "verify:pg-1" if component == "postgres" else "verify:volume-1"
        ),
    )


def test_quiescent_evidence_builds_with_self_verifying_digest() -> None:
    evidence = QuiescentEvidence.build(
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        watermark="pg:42",
        pre_fence_terminal_ticket_count=3,
        captured_at=NOW,
    )

    assert evidence.indeterminate_ticket_count == 0
    assert len(evidence.verification_digest) == 64


def test_coherent_manifest_requires_verified_components_and_is_self_verifying() -> None:
    evidence = QuiescentEvidence.build(
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        watermark="pg:42",
        pre_fence_terminal_ticket_count=3,
        captured_at=NOW,
    )
    manifest = StorageRecoveryManifest.build(
        recovery_set_id="recovery-1",
        deployment_scope_id="deployment-1",
        profile=RecoveryManifestProfile.COORDINATED,
        manifest_status=RecoveryManifestStatus.COHERENT,
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        quiescent_evidence=evidence,
        backup_protection_profile=protection_profile("volume:17"),
        postgres_component_evidence=component_evidence("postgres"),
        object_component_evidence=component_evidence("object_volume"),
        postgres_boundary="snapshot:pg-1",
        postgres_recovery_watermark="pg:42",
        object_volume_generation="volume:17",
        postgres_component_state=ComponentState.VERIFIED,
        object_component_state=ComponentState.VERIFIED,
        postgres_backup_identity="backup:pg-1",
        postgres_verification_reference="verify:pg-1",
        object_verification_reference="verify:volume-1",
        drained_operation_count=3,
        orphaned_operation_count=1,
        indeterminate_operation_count=0,
        scope="deployment-1",
        created_at=NOW,
        completed_at=NOW,
    )

    assert manifest.manifest_status is RecoveryManifestStatus.COHERENT
    assert len(manifest.manifest_digest) == 64


def test_coherent_manifest_rejects_component_artifacts_from_different_included_sets() -> None:
    evidence = QuiescentEvidence.build(
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        watermark="pg:42",
        pre_fence_terminal_ticket_count=0,
        captured_at=NOW,
    )

    with pytest.raises(ValidationError, match="included set"):
        StorageRecoveryManifest.build(
            recovery_set_id="recovery-mixed-components",
            deployment_scope_id="deployment-1",
            profile=RecoveryManifestProfile.COORDINATED,
            manifest_status=RecoveryManifestStatus.COHERENT,
            fence_epoch=4,
            cutoff_ticket_sequence=8,
            quiescent_evidence=evidence,
            backup_protection_profile=protection_profile("volume:17"),
            postgres_component_evidence=component_evidence("postgres"),
            object_component_evidence=component_evidence(
                "object_volume", included_set_digest="3" * 64
            ),
            postgres_boundary="snapshot:pg-1",
            postgres_recovery_watermark="pg:42",
            object_volume_generation="volume:17",
            postgres_component_state=ComponentState.VERIFIED,
            object_component_state=ComponentState.VERIFIED,
            postgres_backup_identity="backup:pg-1",
            postgres_verification_reference="verify:pg-1",
            object_verification_reference="verify:volume-1",
            drained_operation_count=0,
            orphaned_operation_count=0,
            indeterminate_operation_count=0,
            scope="deployment-1",
            created_at=NOW,
            completed_at=NOW,
        )


def test_legacy_coherent_manifest_keeps_its_original_digest_contract() -> None:
    evidence = QuiescentEvidence.build(
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        watermark="pg:42",
        pre_fence_terminal_ticket_count=0,
        captured_at=NOW,
    )
    manifest = StorageRecoveryManifest.build(
        schema_version=1,
        recovery_set_id="legacy-recovery-1",
        deployment_scope_id="deployment-1",
        profile=RecoveryManifestProfile.COORDINATED,
        manifest_status=RecoveryManifestStatus.COHERENT,
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        quiescent_evidence=evidence,
        postgres_boundary="snapshot:pg-1",
        postgres_recovery_watermark="pg:42",
        object_volume_generation="volume:17",
        postgres_component_state=ComponentState.VERIFIED,
        object_component_state=ComponentState.VERIFIED,
        postgres_backup_identity="backup:pg-1",
        postgres_verification_reference="verify:pg-1",
        object_verification_reference="verify:volume-1",
        drained_operation_count=0,
        orphaned_operation_count=0,
        indeterminate_operation_count=0,
        scope="deployment-1",
        created_at=NOW,
        completed_at=NOW,
    )

    assert manifest.schema_version == 1
    assert manifest.postgres_component_evidence is None


def test_coherent_manifest_rejects_mismatched_quiescent_evidence() -> None:
    evidence = QuiescentEvidence.build(
        fence_epoch=5,
        cutoff_ticket_sequence=8,
        watermark="pg:42",
        pre_fence_terminal_ticket_count=3,
        captured_at=NOW,
    )

    with pytest.raises(ValidationError, match="fence epoch"):
        StorageRecoveryManifest.build(
            recovery_set_id="recovery-mismatch",
            deployment_scope_id="deployment-1",
            profile=RecoveryManifestProfile.COORDINATED,
            manifest_status=RecoveryManifestStatus.COHERENT,
            fence_epoch=4,
            cutoff_ticket_sequence=8,
            quiescent_evidence=evidence,
            backup_protection_profile=protection_profile("volume:17"),
            postgres_boundary="snapshot:pg-1",
            postgres_recovery_watermark="pg:42",
            object_volume_generation="volume:17",
            postgres_component_state=ComponentState.VERIFIED,
            object_component_state=ComponentState.VERIFIED,
            postgres_backup_identity="backup:pg-1",
            postgres_verification_reference="verify:pg-1",
            object_verification_reference="verify:volume-1",
            drained_operation_count=3,
            orphaned_operation_count=1,
            indeterminate_operation_count=0,
            scope="deployment-1",
            created_at=NOW,
            completed_at=NOW,
        )


def test_best_effort_manifest_cannot_be_coherent() -> None:
    evidence = QuiescentEvidence.build(
        fence_epoch=4,
        cutoff_ticket_sequence=8,
        watermark="pg:42",
        pre_fence_terminal_ticket_count=0,
        captured_at=NOW,
    )

    with pytest.raises(ValidationError, match="best-effort"):
        StorageRecoveryManifest.build(
            recovery_set_id="recovery-2",
            deployment_scope_id="deployment-1",
            profile=RecoveryManifestProfile.BEST_EFFORT,
            manifest_status=RecoveryManifestStatus.COHERENT,
            fence_epoch=4,
            cutoff_ticket_sequence=8,
            quiescent_evidence=evidence,
            postgres_boundary="snapshot:pg-2",
            postgres_recovery_watermark="pg:42",
            object_volume_generation="volume:18",
            postgres_component_state=ComponentState.VERIFIED,
            object_component_state=ComponentState.VERIFIED,
            drained_operation_count=0,
            orphaned_operation_count=0,
            indeterminate_operation_count=0,
            scope="deployment-1",
            created_at=NOW,
            completed_at=NOW,
        )


def test_active_lease_requires_closed_admission_and_expiry() -> None:
    with pytest.raises(ValidationError, match="complete lease evidence"):
        BackupFenceLease(
            deployment_scope_id="deployment-1",
            active_fence_epoch=1,
            fence_state=FenceState.DRAINING,
            cutoff_ticket_sequence=0,
            ticket_admission=TicketAdmission.CLOSED,
        )


def test_ticket_transition_rejects_terminal_replay() -> None:
    validate_ticket_transition(
        FenceOperationState.ADMITTED,
        FenceOperationState.EXECUTING,
    )

    with pytest.raises(ValueError, match="invalid ticket transition"):
        validate_ticket_transition(
            FenceOperationState.TERMINAL_SUCCEEDED,
            FenceOperationState.EXECUTING,
        )


def test_operation_kind_is_explicitly_typed() -> None:
    assert FenceOperationKind.FINALIZATION.value == "finalization"


def test_verified_component_evidence_requires_matching_durable_identity() -> None:
    with pytest.raises(ValidationError, match="identity-matched"):
        RecoveryComponentEvidence(
            component="postgres",
            expected_identity="backup:expected",
            observed_identity="backup:actual",
            artifact_reference="artifact:1",
            artifact_digest="2" * 64,
            tool_profile="pg_dump-custom-compatible-restore:v1",
            tool_version="tool-17.0",
            fence_epoch=1,
            cutoff_ticket_sequence=0,
            included_set_digest=INCLUDED_SET_DIGEST,
            inclusion_policy="r1-finalized-originals-and-canonical-metadata:v1",
            durable=True,
            verified=True,
            verification_reference="verify:1",
        )
