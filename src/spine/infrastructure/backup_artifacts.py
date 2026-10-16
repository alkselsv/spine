"""Protected-destination storage for immutable component backup artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import BinaryIO, TypeVar

from spine.application.storage.backup_profile import BackupProtectionProfile


class BackupDestinationFailure(RuntimeError):
    """A component artifact could not be durably stored or independently checked."""


_Result = TypeVar("_Result")


@dataclass(frozen=True, slots=True)
class StoredBackupArtifact:
    artifact_reference: str
    sha256: str
    byte_length: int
    local_path: Path


class FilesystemBackupArtifactStore:
    """Immutable file destination bound to an externally approved protection profile."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        protection: BackupProtectionProfile,
    ) -> None:
        self._root = Path(os.path.abspath(os.fspath(root)))
        if (
            not self._root.is_absolute()
            or not os.fspath(self._root)
            or self._root == Path(os.sep)
        ):
            raise ValueError("backup destination root must be absolute")
        self._protection = protection

    async def persist_file(
        self,
        *,
        recovery_set_id: str,
        component: str,
        source_path: str | os.PathLike[str],
        expected_sha256: str,
        protection: BackupProtectionProfile,
    ) -> StoredBackupArtifact:
        if protection != self._protection:
            raise BackupDestinationFailure("backup destination profile mismatch")
        return await run_backup_io(
            self._persist_file,
            recovery_set_id,
            component,
            Path(source_path),
            expected_sha256,
        )

    async def verify_file(
        self, artifact_reference: str, *, expected_sha256: str
    ) -> StoredBackupArtifact:
        return await run_backup_io(
            self._verify_file, artifact_reference, expected_sha256
        )

    def _persist_file(
        self,
        recovery_set_id: str,
        component: str,
        source_path: Path,
        expected_sha256: str,
    ) -> StoredBackupArtifact:
        if not _is_safe_identifier(recovery_set_id) or component not in {
            "postgres",
            "object_volume",
        }:
            raise BackupDestinationFailure("backup artifact identity is invalid")
        if len(expected_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in expected_sha256
        ):
            raise BackupDestinationFailure("backup artifact digest is invalid")
        self._prepare_root()
        reference_digest = hashlib.sha256(
            f"{recovery_set_id}\0{component}".encode("ascii")
        ).hexdigest()
        artifact_reference = f"artifact:{reference_digest}"
        target = self._root / f"{reference_digest}.backup"
        try:
            source_digest, source_size = _digest_regular_file(source_path)
            if source_digest != expected_sha256:
                raise BackupDestinationFailure("backup artifact changed before storage")
            if target.exists() or target.is_symlink():
                existing = self._verify_file(artifact_reference, expected_sha256)
                if existing.sha256 != source_digest or existing.byte_length != source_size:
                    raise BackupDestinationFailure("immutable backup artifact conflicts")
                return existing

            temporary_fd, temporary_name = tempfile.mkstemp(
                prefix=f".{reference_digest}.", suffix=".pending", dir=self._root
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(temporary_fd, "wb", closefd=True) as output:
                    actual_digest, actual_size = _copy_and_hash(source_path, output)
                    output.flush()
                    os.fsync(output.fileno())
                if actual_digest != expected_sha256 or actual_size != source_size:
                    raise BackupDestinationFailure("backup artifact changed during storage")
                try:
                    os.link(temporary, target, follow_symlinks=False)
                except FileExistsError:
                    existing = self._verify_file(artifact_reference, expected_sha256)
                    if existing.byte_length != actual_size:
                        raise BackupDestinationFailure("immutable backup artifact conflicts")
                    return existing
                os.chmod(target, stat.S_IRUSR | stat.S_IWUSR, follow_symlinks=False)
                directory_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            return self._verify_file(artifact_reference, expected_sha256)
        except BackupDestinationFailure:
            raise
        except (OSError, TypeError, ValueError):
            raise BackupDestinationFailure("backup destination is unavailable") from None

    def _verify_file(
        self, artifact_reference: str, expected_sha256: str
    ) -> StoredBackupArtifact:
        if not artifact_reference.startswith("artifact:"):
            raise BackupDestinationFailure("backup artifact reference is invalid")
        reference_digest = artifact_reference.removeprefix("artifact:")
        if len(reference_digest) != 64 or any(
            char not in "0123456789abcdef" for char in reference_digest
        ):
            raise BackupDestinationFailure("backup artifact reference is invalid")
        self._prepare_root()
        path = self._root / f"{reference_digest}.backup"
        try:
            digest, length = _digest_regular_file(path)
        except (OSError, ValueError):
            raise BackupDestinationFailure("stored backup artifact cannot be verified") from None
        if digest != expected_sha256:
            raise BackupDestinationFailure("stored backup artifact digest mismatch")
        return StoredBackupArtifact(artifact_reference, digest, length, path)

    def _prepare_root(self) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            for part in self._root.parts[1:]:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            os.fchmod(descriptor, stat.S_IRWXU)
        except BackupDestinationFailure:
            raise
        except (OSError, ValueError):
            raise BackupDestinationFailure("backup destination is unavailable") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _is_safe_identifier(value: str) -> bool:
    return bool(value) and len(value) <= 256 and all(
        char.isascii() and (char.isalnum() or char in "_.:/-") for char in value
    )


def _digest_regular_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("backup artifact must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                length += len(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), length


def _copy_and_hash(source_path: Path, output: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    source_fd = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ValueError("backup artifact must be a regular file")
        with os.fdopen(source_fd, "rb", closefd=False) as source:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                length += len(chunk)
    finally:
        os.close(source_fd)
    return digest.hexdigest(), length


async def run_backup_io(
    function: Callable[..., _Result], *args: object
) -> _Result:
    """Run blocking backup file work without joining a shared asyncio executor."""

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spine-backup")
    result = executor.submit(function, *args)
    try:
        while not result.done():
            await asyncio.sleep(0.001)
        return result.result()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
