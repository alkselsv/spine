"""Read one bounded file from an explicitly configured read-only mount."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
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
from spine.application.mounted_filesystem_reconciliation import (
    MountedFilesystemIncompleteScanError,
    MountedFilesystemScanItem,
    MountedFilesystemScanSnapshot,
    MountedFilesystemUnavailableError,
)
from spine.connectors.observations import ConnectorCoverage
from spine.connectors.observations_v2 import (
    BinaryObservationRepresentation,
    ExternalIdentityAssurance,
    NormalizedExternalObservationV2,
    ProviderExternalIdentity,
    ProviderFileRevisionEvidence,
    ProviderFileKey,
    ProviderObservationDiscovery,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderKind,
)
from spine.domain.sources.lifecycle import (
    SourceExternalIdentity,
    mounted_filesystem_source_identity,
)

DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CONFIGURED_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_READ_CHUNK_BYTES = 64 * 1024
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
MAX_CONCURRENT_FILE_READS = 4
_READ_SLOTS = BoundedSemaphore(MAX_CONCURRENT_FILE_READS)
_EXTRACTION_PROFILE = "mounted-file-bytes:v1"
_LOCATOR_CONTRACT = "mounted-file-path:v1"


@dataclass(frozen=True, slots=True)
class MountedFilesystemSourceIdentityResolver:
    """Resolve an approved relative path before reading its mounted bytes."""

    def resolve_source_identity(
        self, connection: ProviderConnection, command: ProviderReadCommand
    ) -> SourceExternalIdentity | None:
        if (
            connection.provider_kind is not ProviderKind.MOUNTED_FILESYSTEM
            or command.resource_object_reference is None
        ):
            return None
        return mounted_filesystem_source_identity(
            connection.connection_version_id,
            command.resource_scope_reference.value,
            command.resource_object_reference,
            command.external_identity_occurrence,
        )


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
class _StableFileRead:
    content: bytes
    provider_revision: str
    modified_at: datetime


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
            stable_read = await _bounded_read(
                root.path,
                relative_path,
                self.max_file_bytes,
                self.read_chunk_bytes,
                timeout=self.read_timeout_seconds,
            )
        except (OSError, ValueError, asyncio.TimeoutError) as error:
            raise MountedFilesystemReadError() from error
        if (
            command.expected_provider_revision is not None
            and stable_read.provider_revision != command.expected_provider_revision
        ):
            raise MountedFilesystemReadError()
        content = stable_read.content

        received_at = self.clock()
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("mounted filesystem clock must return an aware datetime")
        observed_at = stable_read.modified_at
        path_digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
        identity = mounted_filesystem_source_identity(
            command.connection_version_id,
            command.resource_scope_reference.value,
            relative_path,
            command.external_identity_occurrence,
        )
        content_digest = hashlib.sha256(content).hexdigest()
        provider_payload: dict[str, str | None] = {
            "resource_scope_reference": str(command.resource_scope_reference.value),
            "connection_version_id": str(command.connection_version_id),
            "identity_occurrence": command.external_identity_occurrence,
        }
        provider_payload_digest = hashlib.sha256(
            json.dumps(
                provider_payload, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        event_digest = hashlib.sha256(
            f"{identity_digest}:{content_digest}".encode("ascii")
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
                external_namespace=identity.namespace,
                external_generation=identity.generation,
                external_object_id=identity.object_id,
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
                revision_evidence=ProviderFileRevisionEvidence(
                    provider_revision=stable_read.provider_revision,
                    resource_path=relative_path,
                    content_sha256=content_digest,
                    provider_payload=provider_payload,
                    provider_payload_digest=provider_payload_digest,
                    byte_length=len(content),
                    modified_at=stable_read.modified_at,
                ),
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


@dataclass(frozen=True, slots=True)
class MountedFilesystemTreeScanner:
    """Enumerate one configured root without treating reachability as absence."""

    roots: Mapping[UUID, MountedFilesystemRoot]
    scan_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", MappingProxyType(dict(self.roots)))
        if (
            not isinstance(self.scan_timeout_seconds, (int, float))
            or isinstance(self.scan_timeout_seconds, bool)
            or not 0 < self.scan_timeout_seconds < float("inf")
        ):
            raise ValueError("mounted filesystem scan timeout must be positive")

    async def scan(
        self,
        connection: ProviderConnection,
        resource_scope_reference: OpaqueProviderReference,
    ) -> MountedFilesystemScanSnapshot:
        root = self.roots.get(connection.connection_version_id)
        if (
            connection.provider_kind is not ProviderKind.MOUNTED_FILESYSTEM
            or root is None
            or root.resource_scope_reference != resource_scope_reference
            or resource_scope_reference
            not in connection.approved_resource_scope_references
        ):
            raise MountedFilesystemUnavailableError()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_scan_regular_files, root.path),
                timeout=self.scan_timeout_seconds,
            )
        except MountedFilesystemIncompleteScanError:
            raise
        except (OSError, asyncio.TimeoutError) as error:
            raise MountedFilesystemUnavailableError() from error


async def _bounded_read(
    root: Path,
    relative_path: str,
    max_file_bytes: int,
    chunk_size: int,
    *,
    timeout: float,
) -> _StableFileRead:
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
) -> _StableFileRead:
    try:
        return _read_regular_file(root, relative_path, max_file_bytes, chunk_size)
    finally:
        _READ_SLOTS.release()


def _validate_relative_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value or value.startswith("/"):
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
) -> _StableFileRead:
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
        provider_revision = hashlib.sha256(
            ":".join(str(part) for part in _stat_identity(after)).encode()
        ).hexdigest()
        return _StableFileRead(
            content=b"".join(chunks),
            provider_revision=provider_revision,
            modified_at=datetime.fromtimestamp(
                after.st_mtime_ns / 1_000_000_000, tz=timezone.utc
            ),
        )
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


def _scan_regular_files(root: Path) -> MountedFilesystemScanSnapshot:
    """Capture a metadata snapshot and fail closed on incomplete traversal."""

    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise MountedFilesystemUnavailableError()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    errors: list[OSError] = []
    try:
        root_identity = _stat_node_identity(os.fstat(root_fd))
        items: list[MountedFilesystemScanItem] = []
        for directory, directory_names, file_names, directory_fd in os.fwalk(
            root,
            topdown=True,
            onerror=errors.append,
            follow_symlinks=False,
        ):
            # Symlinked directories are visible but are never traversed.
            directory_names[:] = [
                name
                for name in directory_names
                if not stat.S_ISLNK(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
                )
            ]
            relative_directory = Path(directory).relative_to(root)
            for name in file_names:
                try:
                    value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    errors.append(error)
                    continue
                if not stat.S_ISREG(value.st_mode):
                    continue
                relative = (relative_directory / name).as_posix()
                try:
                    relative = _validate_relative_path(relative)
                except MountedFilesystemReadError as error:
                    raise MountedFilesystemIncompleteScanError() from error
                read_token = hashlib.sha256(
                    ":".join(str(part) for part in _stat_identity(value)).encode()
                ).hexdigest()
                items.append(
                    MountedFilesystemScanItem(
                        provider_object_reference=relative,
                        read_token=read_token,
                    )
                )
        if errors:
            raise MountedFilesystemIncompleteScanError()
        verification_fd = os.open(root, flags)
        try:
            if _stat_node_identity(os.fstat(verification_fd)) != root_identity:
                raise MountedFilesystemUnavailableError()
        finally:
            os.close(verification_fd)
    finally:
        os.close(root_fd)
    items.sort(key=lambda item: item.provider_object_reference)
    coverage_digest = hashlib.sha256(
        "\n".join(
            f"{item.provider_object_reference}\0{item.read_token}" for item in items
        ).encode()
    ).hexdigest()
    return MountedFilesystemScanSnapshot(
        items=tuple(items), coverage_digest=coverage_digest
    )


__all__ = [
    "MountedFilesystemObservationReader",
    "MountedFilesystemReadError",
    "MountedFilesystemRoot",
    "MountedFilesystemSourceIdentityResolver",
    "MountedFilesystemTreeScanner",
]
