from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import time

import pytest

from spine.application.connections.provider import (
    ProviderReadAdmission,
    ProviderReadCommand,
    ProviderReadDeniedError,
)
from spine.connectors.files.mounted_filesystem import (
    MountedFilesystemObservationReader,
    MountedFilesystemReadError,
    MountedFilesystemRoot,
)
from spine.domain.connections.provider import (
    OpaqueProviderReference,
    ProviderConnection,
    ProviderConnectionStatus,
    ProviderKind,
)


def _id(number: int):
    from uuid import UUID

    return UUID(f"00000000-0000-0000-0000-{number:012d}")


NOW = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
WORKSPACE = _id(1)
ENVIRONMENT = _id(2)
CONNECTION = _id(3)
CONNECTION_VERSION = _id(4)
SCOPE = _id(5)


def _connection() -> ProviderConnection:
    return ProviderConnection(
        connection_version_id=CONNECTION_VERSION,
        connection_id=CONNECTION,
        approval_id=_id(6),
        version=1,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        service_principal_id=_id(7),
        provider_kind=ProviderKind.MOUNTED_FILESYSTEM,
        credential_reference=OpaqueProviderReference(value=_id(8)),
        grant_reference=OpaqueProviderReference(value=_id(9)),
        approved_resource_scope_references=(OpaqueProviderReference(value=SCOPE),),
        authorization_generation=1,
        configuration_generation=1,
        status=ProviderConnectionStatus.ACTIVE,
    )


def _command(path: str = "docs/guide.md", *, scope: int = 5) -> ProviderReadCommand:
    return ProviderReadCommand(
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        connection_id=CONNECTION,
        connection_version_id=CONNECTION_VERSION,
        resource_scope_reference=OpaqueProviderReference(value=_id(scope)),
        authorization_generation=1,
        configuration_generation=1,
        resource_object_reference=path,
    )


def _reader(root: Path, **limits) -> MountedFilesystemObservationReader:
    return MountedFilesystemObservationReader(
        roots={
            CONNECTION_VERSION: MountedFilesystemRoot(
                resource_scope_reference=OpaqueProviderReference(value=SCOPE),
                path=root,
            )
        },
        clock=lambda: NOW,
        **limits,
    )


@pytest.mark.asyncio
async def test_allowed_file_read_returns_path_scoped_v2_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_bytes(b"# mounted source\n")

    observation = await _reader(root).read(_connection(), _command())

    assert observation.original_bytes == b"# mounted source\n"
    assert observation.external_identity.assurance.value == "path_scoped"
    assert (
        observation.external_identity.provider_key.provider_object_id == "docs/guide.md"
    )
    assert observation.required_capabilities() == frozenset({"binary_read"})
    assert observation.origin_locator_digest is not None
    assert observation.representation.revision_evidence is not None
    assert observation.representation.revision_evidence.resource_path == "docs/guide.md"


@pytest.mark.asyncio
async def test_reappearance_occurrence_creates_new_path_scoped_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "guide.md").write_bytes(b"same bytes")
    reader = _reader(root)

    original = await reader.read(_connection(), _command("guide.md"))
    reappeared = await reader.read(
        _connection(),
        replace(_command("guide.md"), external_identity_occurrence="scan-2"),
    )

    assert (
        original.external_identity.provider_key
        == reappeared.external_identity.provider_key
    )
    assert (
        original.external_identity.external_object_id
        != reappeared.external_identity.external_object_id
    )
    assert original.origin_locator_digest == reappeared.origin_locator_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ("../outside.txt", "/etc/passwd", "docs//guide.md", "docs/./guide.md"),
)
async def test_path_traversal_and_noncanonical_paths_are_denied(
    tmp_path: Path, path: str
) -> None:
    root = tmp_path / "mount"
    root.mkdir()

    with pytest.raises(MountedFilesystemReadError):
        await _reader(root).read(_connection(), _command(path))


@pytest.mark.asyncio
async def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "escape.txt").symlink_to(outside)

    with pytest.raises(MountedFilesystemReadError):
        await _reader(root).read(_connection(), _command("escape.txt"))


@pytest.mark.asyncio
async def test_special_device_is_denied_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    os.mkfifo(root / "pipe")

    with pytest.raises(MountedFilesystemReadError):
        await _reader(root).read(_connection(), _command("pipe"))


@pytest.mark.asyncio
async def test_file_over_size_limit_is_denied_before_observation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "large.bin").write_bytes(b"x" * 33)

    with pytest.raises(MountedFilesystemReadError):
        await _reader(root, max_file_bytes=32).read(
            _connection(), _command("large.bin")
        )


@pytest.mark.asyncio
async def test_large_file_is_read_in_bounded_chunks(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    expected = b"x" * 200
    (root / "large.bin").write_bytes(expected)
    original_read = os.read
    read_sizes: list[int] = []

    def record_read_size(fd: int, size: int) -> bytes:
        read_sizes.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", record_read_size)

    observation = await _reader(root, max_file_bytes=256, read_chunk_bytes=13).read(
        _connection(), _command("large.bin")
    )

    assert observation.original_bytes == expected
    assert len(read_sizes) > 10
    assert max(read_sizes) <= 13


@pytest.mark.asyncio
async def test_read_timeout_does_not_return_an_observation(
    tmp_path: Path, monkeypatch
) -> None:
    from spine.connectors.files import mounted_filesystem

    root = tmp_path / "mount"
    root.mkdir()

    def slow_read(*args):
        time.sleep(0.05)
        return b"too late"

    monkeypatch.setattr(mounted_filesystem, "_read_regular_file", slow_read)

    with pytest.raises(MountedFilesystemReadError):
        await _reader(root, read_timeout_seconds=0.001).read(
            _connection(), _command("slow.txt")
        )


@pytest.mark.asyncio
async def test_mutating_file_during_read_is_denied(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    path = root / "guide.txt"
    path.write_bytes(b"before")
    original_read = os.read
    mutated = False

    def read_and_mutate(fd: int, size: int) -> bytes:
        nonlocal mutated
        result = original_read(fd, size)
        if not mutated:
            mutated = True
            path.write_bytes(b"changed")
        return result

    monkeypatch.setattr(os, "read", read_and_mutate)

    with pytest.raises(MountedFilesystemReadError):
        await _reader(root).read(_connection(), _command("guide.txt"))


@pytest.mark.asyncio
async def test_moving_parent_out_of_root_during_read_is_denied(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "mount"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "guide.txt").write_bytes(b"stable bytes")
    moved = tmp_path / "moved-docs"
    original_read = os.read
    relocated = False

    def read_and_relocate(fd: int, size: int) -> bytes:
        nonlocal relocated
        result = original_read(fd, size)
        if not relocated:
            relocated = True
            docs.rename(moved)
        return result

    monkeypatch.setattr(os, "read", read_and_relocate)

    with pytest.raises(MountedFilesystemReadError):
        await _reader(root).read(_connection(), _command("docs/guide.txt"))


@pytest.mark.asyncio
async def test_unapproved_scope_is_denied_without_returning_file_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "guide.md").write_bytes(b"private")
    reader = _reader(root)
    command = _command(scope=99)

    with pytest.raises(MountedFilesystemReadError) as failure:
        await reader.read(_connection(), command)

    assert "private" not in str(failure.value)
    assert "guide.md" not in str(failure.value)


@pytest.mark.asyncio
async def test_cross_workspace_request_is_denied_before_filesystem_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    (root / "guide.md").write_bytes(b"workspace private")

    class Repository:
        @asynccontextmanager
        async def admit_read(self, command, context):
            del command, context
            yield _connection()

    admission = ProviderReadAdmission(
        repository=Repository(),
        provider=_reader(root),
    )

    with pytest.raises(ProviderReadDeniedError) as failure:
        await admission.read(
            replace(_command(), workspace_id=_id(98)),
            object(),
        )

    assert "workspace private" not in str(failure.value)


def test_root_must_be_canonical_absolute_directory(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()

    with pytest.raises(ValueError, match="absolute"):
        MountedFilesystemRoot(
            resource_scope_reference=OpaqueProviderReference(value=SCOPE),
            path=Path("mount"),
        )


def test_root_symlink_alias_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "mount"
    root.mkdir()
    alias = tmp_path / "mount-alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(ValueError, match="canonical"):
        MountedFilesystemRoot(
            resource_scope_reference=OpaqueProviderReference(value=SCOPE),
            path=alias,
        )
