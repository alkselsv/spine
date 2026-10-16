from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from spine.domain.sources.original_reference import ObjectReference
from spine.domain.storage.backup_profile import BackupProtectionProfile
from spine.domain.storage.backup_profile import canonical_included_set_digest
from spine.domain.storage.recovery import (
    ComponentState,
    QuiescentEvidence,
    RecoveryComponentEvidence,
    RecoveryManifestProfile,
    RecoveryManifestStatus,
    StorageRecoveryManifest,
)
from spine.infrastructure.object_storage.contracts import (
    IntegrityResult,
    IntegrityStatus,
    ObservedContentIdentity,
    WriteReceipt,
)
from spine.infrastructure.isolated_restore import (
    FilesystemArchiveMaterializer,
    IsolatedRestoreFailure,
    IsolatedRestoreTarget,
    IsolatedRestoreVerifier,
)
from spine.infrastructure.postgres_backup import PostgreSQLBackupConnection, TenantInventoryScope


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
OBJECT = ObjectReference(
    schema_version=1,
    object_id=UUID("30000000-0000-4000-8000-000000000001"),
    storage_generation=UUID("50000000-0000-4000-8000-000000000001"),
    digest_algorithm="sha256",
    digest_hex=hashlib.sha256(b"synthetic source bytes").hexdigest(),
    byte_length=len(b"synthetic source bytes"),
)


def make_manifest(pg_digest: str, fs_digest: str) -> StorageRecoveryManifest:
    protection = BackupProtectionProfile(
        source_volume_id="volume:originals",
        destination_id="volume:backups",
        encryption_at_rest_profile="encryption:profile",
        protected_transfer_profile="transfer:profile",
        backup_identity="identity:backup",
        restore_identity="identity:restore",
    )
    postgres = RecoveryComponentEvidence(
        component="postgres",
        expected_identity=f"postgres-backup:{pg_digest}",
        observed_identity=f"postgres-backup:{pg_digest}",
        artifact_reference="artifact:postgres",
        artifact_digest=pg_digest,
        tool_profile="pg_dump-custom-compatible-restore:v1",
        tool_version="postgres-17.2.1",
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        included_set_digest=canonical_included_set_digest((OBJECT,)),
        inclusion_policy="r1-finalized-originals-and-canonical-metadata:v1",
        durable=True,
        verified=True,
        verification_reference="pg_restore-list:verified",
    )
    filesystem = RecoveryComponentEvidence(
        component="object_volume",
        expected_identity="volume:originals",
        observed_identity="volume:originals",
        artifact_reference="artifact:filesystem",
        artifact_digest=fs_digest,
        tool_profile="linux-posix-finalized-originals:v1",
        tool_version="python-3.12.0",
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        included_set_digest=canonical_included_set_digest((OBJECT,)),
        inclusion_policy="r1-finalized-originals-and-canonical-metadata:v1",
        durable=True,
        verified=True,
        verification_reference="filesystem-tar-content:verified",
    )
    quiescence = QuiescentEvidence.build(
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        watermark="pg:1",
        pre_fence_terminal_ticket_count=0,
        captured_at=NOW,
    )
    return StorageRecoveryManifest.build(
        recovery_set_id="restore-set-1",
        deployment_scope_id="deployment-1",
        profile=RecoveryManifestProfile.COORDINATED,
        manifest_status=RecoveryManifestStatus.COHERENT,
        backup_protection_profile=protection,
        fence_epoch=1,
        cutoff_ticket_sequence=0,
        quiescent_evidence=quiescence,
        postgres_component_evidence=postgres,
        object_component_evidence=filesystem,
        postgres_boundary="artifact:postgres",
        postgres_recovery_watermark="pg:1",
        object_volume_generation="volume:originals",
        postgres_component_state=ComponentState.VERIFIED,
        object_component_state=ComponentState.VERIFIED,
        postgres_backup_identity=postgres.observed_identity,
        postgres_verification_reference=postgres.verification_reference,
        object_verification_reference=filesystem.verification_reference,
        drained_operation_count=0,
        orphaned_operation_count=0,
        indeterminate_operation_count=0,
        scope="deployment-1",
        created_at=NOW,
        completed_at=NOW,
    )


def target(tmp_path: Path) -> IsolatedRestoreTarget:
    restore = PostgreSQLBackupConnection(
        database="spine_restore_synthetic",
        username="restore_service",
        backup_role="spine_restore_owner",
        sslmode="require",
    )
    verifier = PostgreSQLBackupConnection(
        database="spine_restore_synthetic",
        username="verify_service",
        backup_role="spine_restore_verifier",
        sslmode="require",
    )
    return IsolatedRestoreTarget(
        database="spine_restore_synthetic",
        restore_connection=restore,
        verifier_connection=verifier,
        filesystem_root=tmp_path / "restored-originals",
        serving_identities=("spine_runtime",),
        verifier_scopes=(TenantInventoryScope(
            workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
            environment_id=UUID("20000000-0000-4000-8000-000000000001"),
        ),),
    )


def write_archive(path: Path, *, payload: bytes = b"synthetic source bytes") -> None:
    receipt = WriteReceipt.model_construct(
        schema_version=1,
        object_reference=OBJECT,
        upload_id=UUID("40000000-0000-4000-8000-000000000001"),
        observed_content=ObservedContentIdentity(
            observed_sha256=OBJECT.digest_hex,
            observed_byte_length=OBJECT.byte_length,
        ),
        verification=IntegrityResult(
            status=IntegrityStatus.VERIFIED,
            algorithm="sha256",
            observed_digest=OBJECT.digest_hex,
            observed_length=OBJECT.byte_length,
        ),
        storage_status="finalized_verified",
        finalized_at=NOW,
    )
    private = json.dumps({"receipt": receipt.model_dump(mode="json")}, separators=(",", ":")).encode()
    scope = hashlib.sha256(
        f"{UUID('10000000-0000-4000-8000-000000000001')}:{UUID('20000000-0000-4000-8000-000000000001')}".encode()
    ).hexdigest()
    with tarfile.open(path, "w") as archive:
        for name, content in (
            (f"objects/{scope}/{OBJECT.object_id.hex}.private", private),
            (f"objects/{scope}/{OBJECT.object_id.hex}", payload),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, __import__("io").BytesIO(content))


def test_isolated_restore_target_requires_dedicated_database_and_absent_root(tmp_path: Path) -> None:
    configured = target(tmp_path)
    with pytest.raises(ValueError, match="spine_restore_"):
        configured.model_copy(update={"database": "production"}).validate_target()
    configured.filesystem_root.mkdir()
    with pytest.raises(ValueError, match="must be absent"):
        configured.validate_target()


def test_filesystem_archive_restores_only_verified_originals(tmp_path: Path) -> None:
    archive = tmp_path / "objects.tar"
    write_archive(archive)
    destination = tmp_path / "stage"
    destination.mkdir()

    restored = FilesystemArchiveMaterializer().materialize(archive, destination)

    assert len(restored) == 1
    assert restored[0].reference == OBJECT
    payloads = list(destination.glob("*/*"))
    assert len(payloads) == 2
    assert any(path.read_bytes() == b"synthetic source bytes" for path in payloads)


def test_filesystem_archive_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "objects.tar"
    write_archive(archive, payload=b"tampered source bytes")
    destination = tmp_path / "stage"
    destination.mkdir()

    with pytest.raises(ValueError):
        FilesystemArchiveMaterializer().materialize(archive, destination)


@pytest.mark.asyncio
async def test_verification_rejects_mixed_component_evidence_before_restore(tmp_path: Path) -> None:
    executable = tmp_path / "pg_restore"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    verifier = IsolatedRestoreVerifier(pg_restore_executable=executable)
    pg = tmp_path / "postgres.dump"
    fs = tmp_path / "objects.tar"
    pg.write_bytes(b"pg archive")
    write_archive(fs)
    manifest = make_manifest(hashlib.sha256(pg.read_bytes()).hexdigest(), hashlib.sha256(fs.read_bytes()).hexdigest())
    bad_manifest = manifest.model_copy(
        update={
            "object_component_evidence": manifest.object_component_evidence.model_copy(
                update={"fence_epoch": 2}
            )
        }
    )

    with pytest.raises(IsolatedRestoreFailure) as error:
        await verifier.verify_and_restore(
            manifest=bad_manifest,
            postgres_artifact=pg,
            filesystem_artifact=fs,
            target=target(tmp_path),
        )

    assert error.value.evidence.outcome == "rejected"
    assert error.value.evidence.safe_failure_code == "restore.component_mismatch"


@pytest.mark.asyncio
async def test_recovery_identifier_path_error_returns_safe_typed_failure(tmp_path: Path) -> None:
    executable = tmp_path / "pg_restore"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    verifier = IsolatedRestoreVerifier(pg_restore_executable=executable)
    pg = tmp_path / "postgres.dump"
    fs = tmp_path / "objects.tar"
    pg.write_bytes(b"pg archive")
    write_archive(fs)
    manifest = make_manifest(
        hashlib.sha256(pg.read_bytes()).hexdigest(),
        hashlib.sha256(fs.read_bytes()).hexdigest(),
    ).model_copy(update={"recovery_set_id": "restore-set/unsafe"})

    with pytest.raises(IsolatedRestoreFailure) as error:
        await verifier.verify_and_restore(
            manifest=manifest,
            postgres_artifact=pg,
            filesystem_artifact=fs,
            target=target(tmp_path),
        )

    assert error.value.evidence.outcome == "unavailable"
    assert error.value.evidence.safe_failure_code == "restore.target_unavailable"


@pytest.mark.asyncio
async def test_successful_restore_evidence_is_read_back_without_rewriting_targets(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "pg_restore"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    class PostgreSQLRestoreFake:
        async def restore_and_read(
            self,
            _archive: Path,
            _target: IsolatedRestoreTarget,
            *,
            expected_component_version: str,
        ) -> tuple[ObjectReference, ...]:
            assert expected_component_version == "17.2.1"
            return (OBJECT,)

    verifier = IsolatedRestoreVerifier(
        pg_restore_executable=executable,
        postgres_restore_port=PostgreSQLRestoreFake(),
    )
    pg = tmp_path / "postgres.dump"
    fs = tmp_path / "objects.tar"
    pg.write_bytes(b"pg archive")
    write_archive(fs)
    manifest = make_manifest(
        hashlib.sha256(pg.read_bytes()).hexdigest(),
        hashlib.sha256(fs.read_bytes()).hexdigest(),
    )
    configured = target(tmp_path)

    first = await verifier.verify_and_restore(
        manifest=manifest,
        postgres_artifact=pg,
        filesystem_artifact=fs,
        target=configured,
    )
    evidence_bytes = (configured.filesystem_root / "restore-evidence.json").read_bytes()
    second = await verifier.verify_and_restore(
        manifest=manifest,
        postgres_artifact=pg,
        filesystem_artifact=fs,
        target=configured,
    )

    assert first == second
    assert second.outcome == "verified"
    assert (configured.filesystem_root / "restore-evidence.json").read_bytes() == evidence_bytes
