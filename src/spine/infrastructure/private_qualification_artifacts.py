"""Fail-closed local artifact writes for explicitly approved qualifications."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def create_private_qualification_file(path: Path, content: bytes) -> None:
    """Atomically publish one owner-only artifact without replacing evidence."""

    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise OSError("private output path is not an approved local path")
    temporary = path.parent / f".{path.name}.pending-{secrets.token_hex(16)}"
    descriptor: int | None = None
    temporary_created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        temporary_created = True
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(content):
            count = os.write(descriptor, content[written:])
            if count < 1:
                raise OSError("private output write did not make progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, path, follow_symlinks=False)
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_created:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise
    else:
        try:
            os.unlink(temporary)
        except OSError:
            # The final link already contains the complete, fsynced evidence.
            # A private leftover link must not make publication ambiguous.
            pass


@dataclass(frozen=True, slots=True)
class FilePrivateQualificationArtifactWriter:
    """Create owner-private artifacts on the local qualification filesystem."""

    def create(self, path: Path, content: bytes) -> None:
        create_private_qualification_file(path, content)


__all__ = [
    "FilePrivateQualificationArtifactWriter",
    "create_private_qualification_file",
]
