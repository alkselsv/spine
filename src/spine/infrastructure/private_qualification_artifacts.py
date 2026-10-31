"""Fail-closed local artifact writes for explicitly approved qualifications."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def create_private_qualification_file(path: Path, content: bytes) -> None:
    """Create one owner-only local artifact without replacing an existing file."""

    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise OSError("private output path is not an approved local path")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        written = 0
        while written < len(content):
            count = os.write(descriptor, content[written:])
            if count < 1:
                raise OSError("private output write did not make progress")
            written += count
        os.fsync(descriptor)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class FilePrivateQualificationArtifactWriter:
    """Create owner-private artifacts on the local qualification filesystem."""

    def create(self, path: Path, content: bytes) -> None:
        create_private_qualification_file(path, content)


__all__ = [
    "FilePrivateQualificationArtifactWriter",
    "create_private_qualification_file",
]
