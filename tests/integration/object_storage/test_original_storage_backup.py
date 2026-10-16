from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from spine.application.storage.backup_profile import (
    BackupComponentFailure,
    BackupProtectionProfile,
)
from spine.domain.storage.backup_profile import (
    R1_BACKUP_INCLUSION_POLICY,
    canonical_included_set_digest,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.infrastructure.backup_artifacts import FilesystemBackupArtifactStore
from spine.infrastructure.object_storage import (
    AuthorizedReadGrantRegistry,
    FilesystemOriginalObjectStore,
    FilesystemStorageProfile,
    OriginalUploadCommandInput,
    PreWriteRequestDigest,
    UploadCommandState,
    UploadCommandStateName,
    WriteReceipt,
)
from spine.infrastructure.original_storage_backup import (
    FilesystemOriginalStorageBackupSource,
)


class Inventory:
    def __init__(self, references: tuple[ObjectReference, ...]) -> None:
        self.references = references

    async def finalized_references(self) -> tuple[ObjectReference, ...]:
        return self.references


def protection() -> BackupProtectionProfile:
    return BackupProtectionProfile(
        source_volume_id="volume:originals",
        destination_id="volume:backups",
        encryption_at_rest_profile="encryption:deployment-bound",
        protected_transfer_profile="transfer:deployment-bound",
        backup_identity="identity:backup",
        restore_identity="identity:restore",
    )


async def put_original(store: FilesystemOriginalObjectStore) -> WriteReceipt:
    content = b"verified object bytes"
    command = OriginalUploadCommandInput(
        operation_name="original.upload",
        operation_schema_version=1,
        idempotency_key="backup-integration",
        workspace_id=UUID("10000000-0000-4000-8000-000000000001"),
        expected_sha256=hashlib.sha256(content).hexdigest(),
        expected_byte_length=len(content),
        purpose="document.upload",
    )
    state = UploadCommandState(
        issue8_receipt_id=UUID("40000000-0000-4000-8000-000000000001"),
        request_digest=PreWriteRequestDigest(
            algorithm="sha256",
            algorithm_version="spine.command-digest.v1",
            digest_hex="b" * 64,
        ),
        upload_id=UUID("20000000-0000-4000-8000-000000000001"),
        object_id=UUID("30000000-0000-4000-8000-000000000001"),
        state=UploadCommandStateName.STAGING,
    )
    state = await store.begin_or_resume_upload(command, state)

    async def chunks() -> AsyncIterator[bytes]:
        yield content[:8]
        yield content[8:]

    observed = await store.write_chunks(state.upload_id, chunks())
    return await store.finalize_upload(state, observed)


@pytest.fixture
def object_store(tmp_path: Path) -> FilesystemOriginalObjectStore:
    return FilesystemOriginalObjectStore(
        FilesystemStorageProfile(root=tmp_path / "objects"),
        grant_registry=AuthorizedReadGrantRegistry(),
        generation_factory=lambda: UUID("50000000-0000-4000-8000-000000000001"),
    )


@pytest.mark.asyncio
async def test_original_storage_source_persists_and_verifies_finalized_references(
    object_store: FilesystemOriginalObjectStore, tmp_path: Path
) -> None:
    write_receipt = await put_original(object_store)
    references = (write_receipt.object_reference,)
    protection_profile = protection()
    destination = FilesystemBackupArtifactStore(
        tmp_path / "protected-backups", protection=protection_profile
    )
    source = FilesystemOriginalStorageBackupSource(
        object_store=object_store,
        inventory=Inventory(references),
        artifact_store=destination,
        volume_generation="volume:originals",
    )

    evidence = await source.create_and_verify(
        recovery_set_id="recovery-objects",
        deployment_scope_id="deployment-1",
        fence_epoch=2,
        cutoff_ticket_sequence=0,
        inclusion_policy=R1_BACKUP_INCLUSION_POLICY,
        included_set_digest=canonical_included_set_digest(references),
        protection=protection_profile,
    )

    assert evidence.component == "object_volume"
    assert evidence.verified and evidence.durable
    assert evidence.included_set_digest == canonical_included_set_digest(references)
    assert evidence.tool_profile == "linux-posix-finalized-originals:v1"
    assert evidence.artifact_digest


@pytest.mark.asyncio
async def test_original_storage_source_rejects_mixed_canonical_included_set(
    object_store: FilesystemOriginalObjectStore, tmp_path: Path
) -> None:
    write_receipt = await put_original(object_store)
    protection_profile = protection()
    source = FilesystemOriginalStorageBackupSource(
        object_store=object_store,
        inventory=Inventory((write_receipt.object_reference,)),
        artifact_store=FilesystemBackupArtifactStore(
            tmp_path / "protected-backups", protection=protection_profile
        ),
        volume_generation="volume:originals",
    )

    with pytest.raises(BackupComponentFailure):
        await source.create_and_verify(
            recovery_set_id="recovery-mixed",
            deployment_scope_id="deployment-1",
            fence_epoch=2,
            cutoff_ticket_sequence=0,
            inclusion_policy=R1_BACKUP_INCLUSION_POLICY,
            included_set_digest="0" * 64,
            protection=protection_profile,
        )
