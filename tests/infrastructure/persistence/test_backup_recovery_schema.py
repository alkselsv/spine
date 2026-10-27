from __future__ import annotations

import importlib
from datetime import datetime, timezone

from spine.infrastructure.persistence.postgresql_mappings import (
    backup_fence_leases,
    fence_operation_tickets,
    recovery_evidence_invalidations,
    recovery_quiescence_evidence,
    storage_recovery_manifests,
)
from spine.domain.storage.recovery import (
    BackupFenceLease,
    ComponentState,
    QuiescentEvidence,
    RecoveryComponentEvidence,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
)
from spine.domain.storage.backup_profile import BackupProtectionProfile
from spine.infrastructure.persistence.backup_recovery import _lease_from_row, _manifest_values


def test_persisted_lease_enum_values_rehydrate_under_strict_domain_contract() -> None:
    lease = _lease_from_row(
        {
            "deployment_scope_id": "deployment:issue509-source-001",
            "active_fence_epoch": None,
            "fence_state": "inactive",
            "cutoff_ticket_sequence": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "lease_renewal": None,
            "takeover_reference": None,
            "ticket_admission": "open",
            "cleanup_release_progress": "none",
        }
    )

    assert isinstance(lease, BackupFenceLease)
    assert lease.fence_state.value == "inactive"
    assert lease.ticket_admission.value == "open"


def test_backup_recovery_migration_declares_all_durable_coordination_records() -> None:
    migration = importlib.import_module(
        "migrations.versions.20261017_20_recovery_evidence_integrity"
    )

    assert migration.revision == "20261017_20"
    assert migration.down_revision == "20261016_19"
    assert migration.COORDINATION_TABLES == (
        "backup_fence_leases",
        "fence_operation_tickets",
        "recovery_quiescence_evidence",
        "storage_recovery_manifests",
        "recovery_evidence_invalidations",
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
    assert "postgres_component_evidence" in storage_recovery_manifests.c
    assert "object_component_evidence" in storage_recovery_manifests.c
    assert "previous_event_digest" in recovery_evidence_invalidations.c
    assert "event_digest" in recovery_evidence_invalidations.c


def test_manifest_values_are_json_serializable_for_jsonb_evidence() -> None:
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    evidence = QuiescentEvidence.build(
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        watermark="pg:1",
        pre_fence_terminal_ticket_count=0,
        captured_at=now,
    )
    included_set_digest = "1" * 64
    postgres_component_evidence = RecoveryComponentEvidence(
        component="postgres",
        expected_identity="backup:pg-1",
        observed_identity="backup:pg-1",
        artifact_reference="artifact:pg-1",
        artifact_digest="2" * 64,
        tool_profile="pg_dump-custom-compatible-restore:v1",
        tool_version="postgres-17.0",
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        included_set_digest=included_set_digest,
        inclusion_policy="r1-finalized-originals-and-canonical-metadata:v1",
        durable=True,
        verified=True,
        verification_reference="verify:pg-1",
    )
    object_component_evidence = RecoveryComponentEvidence(
        component="object_volume",
        expected_identity="volume:1",
        observed_identity="volume:1",
        artifact_reference="artifact:objects-1",
        artifact_digest="3" * 64,
        tool_profile="linux-posix-finalized-originals:v1",
        tool_version="python-3.13.0",
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        included_set_digest=included_set_digest,
        inclusion_policy="r1-finalized-originals-and-canonical-metadata:v1",
        durable=True,
        verified=True,
        verification_reference="verify:volume-1",
    )
    manifest = StorageRecoveryManifest.build(
        recovery_set_id="recovery-1",
        deployment_scope_id="deployment-1",
        profile=RecoveryManifestProfile.COORDINATED,
        manifest_status=RecoveryManifestStatus.COHERENT,
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        quiescent_evidence=evidence,
        backup_protection_profile=BackupProtectionProfile(
            source_volume_id="volume:1",
            destination_id="destination:1",
            encryption_at_rest_profile="encryption:approved",
            protected_transfer_profile="transfer:approved",
            backup_identity="identity:backup",
            restore_identity="identity:restore",
        ),
        postgres_component_evidence=postgres_component_evidence,
        object_component_evidence=object_component_evidence,
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
    assert values["postgres_component_evidence"]["artifact_digest"] == "2" * 64
    assert values["backup_protection_profile"]["cadence"] == "daily"
    assert values["backup_protection_profile"]["retention_days"] == 30
