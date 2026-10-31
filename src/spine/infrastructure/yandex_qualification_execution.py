"""Cross-process execution lease for the isolated local Yandex qualification."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from collections.abc import AsyncIterator

from spine.application.yandex_qualification_execution import (
    YandexQualificationExecutionDeniedError,
    YandexQualificationExecutionScope,
)


@dataclass(frozen=True, slots=True)
class FileYandexQualificationExecutionLease:
    """Hold one non-blocking OS lease for an exact approved execution scope."""

    lock_file: Path
    approved_scope: YandexQualificationExecutionScope

    @asynccontextmanager
    async def hold(
        self, scope: YandexQualificationExecutionScope
    ) -> AsyncIterator[None]:
        if scope != self.approved_scope or not self.lock_file.is_absolute():
            raise YandexQualificationExecutionDeniedError()
        try:
            if self.lock_file.parent.resolve(strict=True) != self.lock_file.parent:
                raise YandexQualificationExecutionDeniedError()
        except OSError:
            raise YandexQualificationExecutionDeniedError() from None

        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor: int | None = None
        try:
            descriptor = os.open(self.lock_file, flags, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise YandexQualificationExecutionDeniedError()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                raise YandexQualificationExecutionDeniedError() from None
            expected = (_scope_digest(scope) + "\n").encode("ascii")
            os.lseek(descriptor, 0, os.SEEK_SET)
            current = os.read(descriptor, len(expected) + 1)
            if current not in (b"", expected):
                raise YandexQualificationExecutionDeniedError()
            if not current:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, expected)
                os.ftruncate(descriptor, len(expected))
                os.fsync(descriptor)
        except YandexQualificationExecutionDeniedError:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            raise
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            raise YandexQualificationExecutionDeniedError() from None

        try:
            yield
        finally:
            assert descriptor is not None
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _scope_digest(scope: YandexQualificationExecutionScope) -> str:
    payload = "\n".join(
        (
            str(scope.workspace_id),
            str(scope.environment_id),
            str(scope.connection_id),
            str(scope.connection_version_id),
            str(scope.resource_scope_reference),
            str(scope.authorization_generation),
            str(scope.configuration_generation),
        )
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["FileYandexQualificationExecutionLease"]
