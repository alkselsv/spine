"""Verified archive source for the Linux POSIX original-object store."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import tarfile
import tempfile
from pathlib import Path

from spine.application.storage.backup_profile import (
    BackupComponentFailure,
    BackupProtectionProfile,
    CanonicalOriginalReferenceInventory,
)
from spine.domain.sources.original_reference import ObjectReference
from spine.domain.storage.backup_profile import canonical_included_set_digest
from spine.domain.storage.recovery import (
    RecoveryComponentEvidence,
    RecoveryFailureCategory,
)
from spine.infrastructure.object_storage.contracts import WriteReceipt
from spine.infrastructure.object_storage.filesystem import FilesystemOriginalObjectStore
from spine.infrastructure.object_storage.errors import (
    ObjectCorrupt,
    ObjectNotFound,
    StorageIntegrityIndeterminate,
)

from .backup_artifacts import (
    BackupDestinationFailure,
    FilesystemBackupArtifactStore,
    run_backup_io,
)


class FilesystemOriginalStorageBackupSource:
    """Capture finalized bytes and private manifests, with an explicit stage policy."""

    def __init__(
        self,
        *,
        object_store: FilesystemOriginalObjectStore,
        inventory: CanonicalOriginalReferenceInventory,
        artifact_store: FilesystemBackupArtifactStore,
        volume_generation: str,
    ) -> None:
        if not _is_safe_identifier(volume_generation):
            raise ValueError("filesystem volume generation is invalid")
        self._object_store = object_store
        self._inventory = inventory
        self._artifact_store = artifact_store
        self._volume_generation = volume_generation

    async def create_and_verify(
        self,
        *,
        recovery_set_id: str,
        deployment_scope_id: str,
        fence_epoch: int,
        cutoff_ticket_sequence: int,
        inclusion_policy: str,
        included_set_digest: str | None,
        protection: BackupProtectionProfile,
    ) -> RecoveryComponentEvidence:
        del deployment_scope_id
        if protection.source_volume_id != self._volume_generation:
            raise BackupComponentFailure(
                RecoveryFailureCategory.COMPONENT_IDENTITY_MISMATCH
            )
        references = await self._inventory.finalized_references()
        actual_set_digest = canonical_included_set_digest(references)
        if included_set_digest is None or included_set_digest != actual_set_digest:
            raise BackupComponentFailure(
                RecoveryFailureCategory.COMPONENT_IDENTITY_MISMATCH
            )

        try:
            with tempfile.TemporaryDirectory(prefix="spine-object-backup-") as temp_dir:
                snapshot_path = Path(temp_dir) / "originals.tar"
                snapshot = await self._object_store.export_finalized_backup(
                    snapshot_path, references
                )
                if snapshot.included_set_digest != actual_set_digest:
                    raise BackupComponentFailure(
                        RecoveryFailureCategory.COMPONENT_IDENTITY_MISMATCH
                    )
                stored = await self._artifact_store.persist_file(
                    recovery_set_id=recovery_set_id,
                    component="object_volume",
                    source_path=snapshot_path,
                    expected_sha256=snapshot.sha256,
                    protection=protection,
                )
                persisted = await self._artifact_store.verify_file(
                    stored.artifact_reference,
                    expected_sha256=snapshot.sha256,
                )
                await run_backup_io(
                    _verify_object_archive, persisted.local_path, references
                )
        except BackupComponentFailure:
            raise
        except ObjectNotFound:
            raise BackupComponentFailure(
                RecoveryFailureCategory.REFERENCED_OBJECT_MISSING
            ) from None
        except ObjectCorrupt:
            raise BackupComponentFailure(
                RecoveryFailureCategory.REFERENCED_OBJECT_CORRUPT
            ) from None
        except StorageIntegrityIndeterminate:
            raise BackupComponentFailure(
                RecoveryFailureCategory.INTEGRITY_FAILURE
            ) from None
        except BackupDestinationFailure:
            raise BackupComponentFailure(
                RecoveryFailureCategory.COMPONENT_FAILURE
            ) from None
        except Exception:
            raise BackupComponentFailure(
                RecoveryFailureCategory.COMPONENT_FAILURE
            ) from None

        identity = self._volume_generation
        return RecoveryComponentEvidence(
            component="object_volume",
            expected_identity=identity,
            observed_identity=identity,
            artifact_reference=stored.artifact_reference,
            artifact_digest=stored.sha256,
            tool_profile="linux-posix-finalized-originals:v1",
            tool_version=f"python-{platform.python_version()}",
            fence_epoch=fence_epoch,
            cutoff_ticket_sequence=cutoff_ticket_sequence,
            included_set_digest=actual_set_digest,
            inclusion_policy=inclusion_policy,
            excluded_staged_upload_count=snapshot.excluded_staged_upload_count,
            durable=True,
            verified=True,
            verification_reference=f"filesystem-tar-content:{stored.sha256}",
        )


def _verify_object_archive(
    archive_path: Path, expected_references: tuple[ObjectReference, ...]
) -> None:
    expected_by_id = {reference.object_id: reference for reference in expected_references}
    manifests: dict[str, ObjectReference] = {}
    payloads: dict[str, tarfile.TarInfo] = {}
    try:
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            if any(not member.isfile() or not _safe_archive_name(member.name) for member in members):
                raise ValueError("archive contains an unsupported member")
            for member in members:
                if member.name.endswith(".private"):
                    content = archive.extractfile(member)
                    if content is None:
                        raise ValueError("archive manifest is unavailable")
                    raw_manifest = json.loads(content.read())
                    receipt = WriteReceipt.model_validate(
                        raw_manifest["receipt"], strict=False
                    )
                    reference = receipt.object_reference
                    if reference.object_id in {item.object_id for item in manifests.values()}:
                        raise ValueError("archive contains duplicate object identities")
                    manifests[member.name] = reference
                else:
                    payloads[member.name] = member
            if len(manifests) != len(expected_by_id) or len(payloads) != len(expected_by_id):
                raise ValueError("archive included set is incomplete")
            discovered_ids = {reference.object_id for reference in manifests.values()}
            if discovered_ids != set(expected_by_id):
                raise ValueError("archive references differ from canonical metadata")
            for manifest_name, reference in manifests.items():
                scope = manifest_name.split("/")[1]
                payload_name = f"objects/{scope}/{reference.object_id.hex}"
                member = payloads.get(payload_name)
                if member is None or member.size != reference.byte_length:
                    raise ValueError("archive payload metadata does not match receipt")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("archive payload is unavailable")
                digest = hashlib.sha256()
                length = 0
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    length += len(chunk)
                if digest.hexdigest() != reference.digest_hex or length != reference.byte_length:
                    raise ValueError("archive payload integrity does not match receipt")
    except Exception:
        raise BackupComponentFailure(
            RecoveryFailureCategory.INTEGRITY_FAILURE
        ) from None


def _safe_archive_name(name: str) -> bool:
    components = name.split("/")
    return (
        len(components) == 3
        and components[0] == "objects"
        and len(components[1]) == 64
        and all(char in "0123456789abcdef" for char in components[1])
        and ".." not in components
        and components[2] not in {"", ".", ".."}
    )


def _is_safe_identifier(value: str) -> bool:
    return bool(value) and len(value) <= 128 and all(
        char.isascii() and (char.isalnum() or char in "_.:/-") for char in value
    )


__all__ = ["FilesystemOriginalStorageBackupSource"]
