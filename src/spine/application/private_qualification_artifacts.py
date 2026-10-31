"""Application port for owner-private qualification artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PrivateQualificationArtifactWriter(Protocol):
    def create(self, path: Path, content: bytes) -> None: ...


__all__ = ["PrivateQualificationArtifactWriter"]
