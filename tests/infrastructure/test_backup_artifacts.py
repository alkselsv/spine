from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from spine.application.storage.backup_profile import BackupProtectionProfile
from spine.infrastructure.backup_artifacts import (
    BackupDestinationFailure,
    FilesystemBackupArtifactStore,
)


def protection() -> BackupProtectionProfile:
    return BackupProtectionProfile(
        source_volume_id="volume:originals",
        destination_id="volume:protected-backups",
        encryption_at_rest_profile="encryption:deployment-bound",
        protected_transfer_profile="transfer:deployment-bound",
        backup_identity="identity:backup",
        restore_identity="identity:restore",
    )


@pytest.mark.asyncio
async def test_backup_artifact_store_persists_and_replays_immutable_bytes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "postgres.dump"
    source.write_bytes(b"synthetic custom-format artifact")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = FilesystemBackupArtifactStore(
        tmp_path / "protected-destination", protection=protection()
    )

    first = await store.persist_file(
        recovery_set_id="recovery-1",
        component="postgres",
        source_path=source,
        expected_sha256=digest,
        protection=protection(),
    )
    replay = await store.persist_file(
        recovery_set_id="recovery-1",
        component="postgres",
        source_path=source,
        expected_sha256=digest,
        protection=protection(),
    )

    assert replay == first
    assert first.local_path.read_bytes() == source.read_bytes()
    assert first.byte_length == len(source.read_bytes())


@pytest.mark.asyncio
async def test_backup_artifact_store_rejects_replacement_and_digest_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "objects.tar"
    source.write_bytes(b"first artifact")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = FilesystemBackupArtifactStore(
        tmp_path / "protected-destination", protection=protection()
    )
    stored = await store.persist_file(
        recovery_set_id="recovery-1",
        component="object_volume",
        source_path=source,
        expected_sha256=digest,
        protection=protection(),
    )
    source.write_bytes(b"replacement")

    with pytest.raises(BackupDestinationFailure):
        await store.persist_file(
            recovery_set_id="recovery-1",
            component="object_volume",
            source_path=source,
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            protection=protection(),
        )

    with pytest.raises(BackupDestinationFailure):
        await store.verify_file(stored.artifact_reference, expected_sha256="0" * 64)


@pytest.mark.asyncio
async def test_backup_artifact_store_rejects_unbound_protection_profile(
    tmp_path: Path,
) -> None:
    source = tmp_path / "backup.dump"
    source.write_bytes(b"backup")
    store = FilesystemBackupArtifactStore(
        tmp_path / "protected-destination", protection=protection()
    )
    changed = protection().model_copy(
        update={"protected_transfer_profile": "transfer:unknown"}
    )

    with pytest.raises(BackupDestinationFailure):
        await store.persist_file(
            recovery_set_id="recovery-1",
            component="postgres",
            source_path=source,
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            protection=changed,
        )


@pytest.mark.asyncio
async def test_backup_artifact_store_rejects_symlinked_destination_ancestor(
    tmp_path: Path,
) -> None:
    source = tmp_path / "backup.dump"
    source.write_bytes(b"backup")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    store = FilesystemBackupArtifactStore(
        linked_parent / "protected-destination", protection=protection()
    )

    with pytest.raises(BackupDestinationFailure):
        await store.persist_file(
            recovery_set_id="recovery-1",
            component="postgres",
            source_path=source,
            expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            protection=protection(),
        )

    assert list(real_parent.iterdir()) == []


def test_backup_artifact_store_rejects_filesystem_root_as_destination() -> None:
    with pytest.raises(ValueError):
        FilesystemBackupArtifactStore(Path("/"), protection=protection())
