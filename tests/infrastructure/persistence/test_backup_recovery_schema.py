from __future__ import annotations

import importlib
from datetime import datetime, timezone

from spine.infrastructure.persistence.postgresql_mappings import (
    backup_fence_leases,
    fence_operation_tickets,
    recovery_quiescence_evidence,
    storage_recovery_manifests,
)
from spine.domain.storage.recovery import (
    ComponentState,
    QuiescentEvidence,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
)
from spine.infrastructure.persistence.backup_recovery import _manifest_values


def test_backup_recovery_migration_declares_all_durable_coordination_records() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261016_19_backup_recovery_coordination"
    )

    assert migration.revision == "20261016_19"
    assert migration.down_revision == "20261015_18"
    assert migration.COORDINATION_TABLES == (
        "backup_fence_leases",
        "fence_operation_tickets",
        "recovery_quiescence_evidence",
        "storage_recovery_manifests",
    )


def test_backup_recovery_mappings_keep_live_and_immutable_records_separate() -> None:
    assert backup_fence_leases.name == "backup_fence_leases"
    assert fence_operation_tickets.name == "fence_operation_tickets"
    assert recovery_quiescence_evidence.name == "recovery_quiescence_evidence"
    assert storage_recovery_manifests.name == "storage_recovery_manifests"
    assert "fence_state" in backup_fence_leases.c
    assert "ticket_sequence" in fence_operation_tickets.c
    assert "verification_digest" in recovery_quiescence_evidence.c
    assert "manifest_digest" in storage_recovery_manifests.c


def test_manifest_values_are_json_serializable_for_jsonb_evidence() -> None:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    evidence = QuiescentEvidence.build(
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        watermark="pg:1",
        pre_fence_terminal_ticket_count=0,
        captured_at=now,
    )
    manifest = StorageRecoveryManifest.build(
        recovery_set_id="recovery-1",
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
        created_at=now,
        completed_at=now,
    )

    values = _manifest_values(manifest)
    assert isinstance(values["quiescent_evidence"]["captured_at"], str)
