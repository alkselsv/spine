from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

import spine.infrastructure.private_qualification_artifacts as artifacts_module
from spine.infrastructure.private_qualification_artifacts import (
    FilePrivateQualificationArtifactWriter,
)


def _create(path: Path, content: bytes = b"synthetic evidence\n") -> None:
    FilePrivateQualificationArtifactWriter().create(path, content)


def test_private_artifact_is_owner_only_after_publication(tmp_path: Path) -> None:
    artifact_path = tmp_path / "evidence.json"

    _create(artifact_path)

    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600


def test_private_artifact_is_owner_only_from_initial_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_modes: list[int] = []
    real_open = os.open

    def inspect_created_mode(path: Path, flags: int, mode: int) -> int:
        descriptor = real_open(path, flags, mode)
        observed_modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return descriptor

    monkeypatch.setattr(artifacts_module.os, "open", inspect_created_mode)
    previous_umask = os.umask(0)
    try:
        _create(tmp_path / "evidence.json")
    finally:
        os.umask(previous_umask)

    assert observed_modes == [0o600]


def test_private_artifact_refuses_to_replace_existing_file(tmp_path: Path) -> None:
    artifact_path = tmp_path / "evidence.json"
    artifact_path.write_bytes(b"prior evidence\n")

    with pytest.raises(OSError):
        _create(artifact_path)

    assert artifact_path.read_bytes() == b"prior evidence\n"


def test_private_artifact_refuses_symlink_target(tmp_path: Path) -> None:
    protected_path = tmp_path / "protected.json"
    protected_path.write_bytes(b"protected\n")
    artifact_path = tmp_path / "evidence.json"
    artifact_path.symlink_to(protected_path)

    with pytest.raises(OSError):
        _create(artifact_path)

    assert protected_path.read_bytes() == b"protected\n"


def test_private_artifact_refuses_non_regular_target(tmp_path: Path) -> None:
    artifact_path = tmp_path / "evidence.json"
    artifact_path.mkdir()

    with pytest.raises(OSError):
        _create(artifact_path)

    assert artifact_path.is_dir()


def test_private_artifact_publishes_only_complete_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path = tmp_path / "evidence.json"
    expected = b"complete synthetic evidence\n"
    real_link = os.link

    def inspect_before_publication(source: Path, target: Path, **kwargs: object) -> None:
        assert not target.exists()
        assert source.read_bytes() == expected
        real_link(source, target, **kwargs)

    monkeypatch.setattr(artifacts_module.os, "link", inspect_before_publication)

    _create(artifact_path, expected)

    assert artifact_path.read_bytes() == expected


def test_private_artifact_removes_temporary_after_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_write(*args: object, **kwargs: object) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(artifacts_module.os, "write", refuse_write)

    with pytest.raises(OSError, match="simulated write failure"):
        _create(tmp_path / "evidence.json")

    assert list(tmp_path.iterdir()) == []


def test_private_artifact_cleans_temporary_when_close_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_close = os.close

    def refuse_write(*args: object, **kwargs: object) -> None:
        raise OSError("simulated write failure")

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("simulated close failure")

    monkeypatch.setattr(artifacts_module.os, "write", refuse_write)
    monkeypatch.setattr(artifacts_module.os, "close", close_then_fail)

    with pytest.raises(OSError, match="simulated write failure"):
        _create(tmp_path / "evidence.json")

    assert list(tmp_path.iterdir()) == []


def test_private_artifact_stays_published_when_temporary_unlink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_unlink(*args: object, **kwargs: object) -> None:
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(artifacts_module.os, "unlink", refuse_unlink)
    artifact_path = tmp_path / "evidence.json"

    _create(artifact_path)

    assert artifact_path.read_bytes() == b"synthetic evidence\n"
