"""Port for trusted artifact-attestation verification."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from spine.domain.evaluations.artifact_attestation import ArtifactAttestation


class ArtifactAttestationVerificationError(Exception):
    """Raised when an artifact has no acceptable trusted attestation."""


class ArtifactAttestationVerifier(Protocol):
    """Adapter seam for a trusted artifact-attestation implementation."""

    def verify(
        self,
        *,
        artifact_path: Path,
        artifact_digest: str,
        expected_head_sha: str,
    ) -> ArtifactAttestation: ...
