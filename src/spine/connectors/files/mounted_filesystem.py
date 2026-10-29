"""Read one bounded file from an explicitly configured read-only mount."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import mimetypes
import os
from pathlib import Path, PurePosixPath
import stat
from threading import BoundedSemaphore
from types import MappingProxyType
from typing import Callable, Mapping
from uuid import UUID

from spine.application.connections.provider import (
    ProviderReadCommand,
    ProviderReadError,
)
from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderFileKey,
    ProviderObservationDiscovery,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderKind,
)


DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CONFIGURED_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_READ_CHUNK_BYTES = 64 * 1024
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
MAX_CONCURRENT_FILE_READS = 4
_READ_SLOTS = BoundedSemaphore(MAX_CONCURRENT_FILE_READS)
_EXTRACTION_PROFILE = "mounted-file-bytes:v1"
_LOCATOR_CONTRACT = "mounted-file-path:v1"


class MountedFilesystemReadError(ProviderReadError):
    """A mounted file could not be read safely; details never expose its path."""

    def __init__(self) -> None:
        super().__init__("Mounted filesystem read failed.")


@dataclass(frozen=True, slots=True)
class MountedFilesystemRoot:
    """One trusted mount root bound to a connection's approved scope."""

    resource_scope_reference: OpaqueProviderReference
    path: Path

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("mounted filesystem root must be absolute")
        try:
            canonical = self.path.resolve(strict=True)
        except OSError as error:
            raise ValueError("mounted filesystem root must exist") from error
        if canonical != self.path or not self.path.is_dir():
            raise ValueError("mounted filesystem root must be a canonical directory")


@dataclass(frozen=True, slots=True)
class MountedFilesystemObservationReader:
    """Provider reader for pre-mounted, read-only filesystem roots.

    ``roots`` is immutable connection-version configuration supplied by the
    composition boundary. The adapter only opens regular files for reading;
    it does not create, modify, remove, mount, or authenticate to filesystems.
    """

    roots: Mapping[UUID, MountedFilesystemRoot]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    read_chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", MappingProxyType(dict(self.roots)))
        if (
            isinstance(self.max_file_bytes, bool)
            or not isinstance(self.max_file_bytes, int)
            or self.max_file_bytes <= 0
            or self.max_file_bytes > MAX_CONFIGURED_FILE_BYTES
            or isinstance(self.read_chunk_bytes, bool)
            or not isinstance(self.read_chunk_bytes, int)
            or self.read_chunk_bytes <= 0
        ):
            raise ValueError("mounted filesystem read limits are invalid")
        if (
            not isinstance(self.read_timeout_seconds, (int, float))
            or isinstance(self.read_timeout_seconds, bool)
            or not 0 < self.read_timeout_seconds < float("inf")
        ):
            raise ValueError("mounted filesystem read timeout must be positive")

    async def read(
        self,
        connection: ProviderConnection,
        command: ProviderReadCommand,
    ) -> NormalizedExternalObservationV2:
        if connection.provider_kind is not ProviderKind.MOUNTED_FILESYSTEM:
            raise MountedFilesystemReadError()
        root = self.roots.get(connection.connection_version_id)
        if (
            root is None
            or root.resource_scope_reference != command.resource_scope_reference
            or command.resource_object_reference is None
        ):
            raise MountedFilesystemReadError()
        relative_path = _validate_relative_path(command.resource_object_reference)
        try:
            content = await _bounded_read(
                root.path,
                relative_path,
                self.max_file_bytes,
                self.read_chunk_bytes,
                timeout=self.read_timeout_seconds,
            )
        except (OSError, ValueError, asyncio.TimeoutError) as error:
            raise MountedFilesystemReadError() from error

        observed_at = self.clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("mounted filesystem clock must return an aware datetime")
        path_digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
        content_digest = hashlib.sha256(content).hexdigest()
        event_digest = hashlib.sha256(
            f"{path_digest}:{content_digest}".encode("ascii")
        ).hexdigest()
        media_type = mimetypes.guess_type(PurePosixPath(relative_path).name)[0]
        if media_type is None:
            media_type = "application/octet-stream"
        media_type = media_type.lower()
        return NormalizedExternalObservationV2(
            workspace_id=command.workspace_id,
            environment_id=command.environment_id,
            connection_id=command.connection_id,
            connection_version_id=command.connection_version_id,
            provider_kind=ProviderKind.MOUNTED_FILESYSTEM,
            resource_scope_reference=command.resource_scope_reference,
            authorization_generation=command.authorization_generation,
            configuration_generation=command.configuration_generation,
            source_kind="document",
            external_identity=ProviderExternalIdentity(
                external_namespace="mounted_filesystem",
                external_generation="mounted-path:v1",
                external_object_id=path_digest,
                provider_key=ProviderFileKey(
                    connection_id=command.connection_id,
                    provider_object_id=relative_path,
                ),
                assurance=ExternalIdentityAssurance.PATH_SCOPED,
                assurance_evidence_reference="mounted-filesystem-path:v1",
            ),
            representation=BinaryObservationRepresentation(
                original_bytes=content,
                media_type=media_type,
                extraction_profile=_EXTRACTION_PROFILE,
                locator_contract_version=_LOCATOR_CONTRACT,
            ),
            coverage=ConnectorCoverage.COMPLETE,
            discovery=ProviderObservationDiscovery.DIRECT,
            producer_reference="mounted-filesystem",
            event_identity=f"mfs-{event_digest[:60]}",
            event_digest=event_digest,
            origin_locator_kind="mounted_path_sha256",
            origin_locator_value=f"path-{path_digest[:59]}",
            origin_locator_schema="mounted-file-path:v1",
            origin_locator_digest=path_digest,
            observer_service="spine.mounted_filesystem",
            received_at=observed_at,
            observed_at=observed_at,
            provider_media_type=media_type,
        )


async def _bounded_read(
    root: Path,
    relative_path: str,
    max_file_bytes: int,
    chunk_size: int,
    *,
    timeout: float,
) -> bytes:
    if not _READ_SLOTS.acquire(blocking=False):
        raise MountedFilesystemReadError()
    try:
        read = asyncio.create_task(
            asyncio.to_thread(
                _read_regular_file_and_release,
                root,
                relative_path,
                max_file_bytes,
                chunk_size,
            )
        )
    except BaseException:
        _READ_SLOTS.release()
        raise
    return await asyncio.wait_for(asyncio.shield(read), timeout=timeout)


def _read_regular_file_and_release(
    root: Path,
    relative_path: str,
    max_file_bytes: int,
    chunk_size: int,
) -> bytes:
    try:
        return _read_regular_file(root, relative_path, max_file_bytes, chunk_size)
    finally:
        _READ_SLOTS.release()


def _validate_relative_path(value: str) -> str:
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
    ):
        raise MountedFilesystemReadError()
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MountedFilesystemReadError()
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise MountedFilesystemReadError()
    return value


def _read_regular_file(
    root: Path,
    relative_path: str,
    max_file_bytes: int,
    chunk_size: int,
) -> bytes:
    """Open each component without following symlinks and verify a stable read."""

    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise MountedFilesystemReadError()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, directory_flags | nofollow)
    parent_fd = root_fd
    opened_directories: list[int] = []
    verification_fds: list[int] = []
    file_fd: int | None = None
    try:
        parts = relative_path.split("/")
        for component in parts[:-1]:
            parent_fd = os.open(
                component,
                directory_flags | nofollow,
                dir_fd=parent_fd,
            )
            opened_directories.append(parent_fd)
        file_fd = os.open(
            parts[-1],
            os.O_RDONLY
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_file_bytes:
            raise MountedFilesystemReadError()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(chunk_size, max_file_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_file_bytes:
                raise MountedFilesystemReadError()
        after = os.fstat(file_fd)
        current_path = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if (
            _stat_identity(before) != _stat_identity(after)
            or not stat.S_ISREG(current_path.st_mode)
            or (current_path.st_dev, current_path.st_ino)
            != (after.st_dev, after.st_ino)
            or total != after.st_size
        ):
            raise MountedFilesystemReadError()
        verification_root_fd = os.open(root, directory_flags | nofollow)
        verification_fds.append(verification_root_fd)
        if _stat_node_identity(os.fstat(root_fd)) != _stat_node_identity(
            os.fstat(verification_root_fd)
        ):
            raise MountedFilesystemReadError()
        verification_parent_fd = verification_root_fd
        for component in parts[:-1]:
            verification_parent_fd = os.open(
                component,
                directory_flags | nofollow,
                dir_fd=verification_parent_fd,
            )
            verification_fds.append(verification_parent_fd)
        verified_path = os.stat(
            parts[-1], dir_fd=verification_parent_fd, follow_symlinks=False
        )
        if _stat_node_identity(verified_path) != _stat_node_identity(after):
            raise MountedFilesystemReadError()
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(verification_fds):
            os.close(directory_fd)
        for directory_fd in reversed(opened_directories):
            os.close(directory_fd)
        os.close(root_fd)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stat_node_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


__all__ = [
    "MountedFilesystemObservationReader",
    "MountedFilesystemReadError",
    "MountedFilesystemRoot",
]
