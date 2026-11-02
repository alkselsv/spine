"""Shared trusted identity for an attested qualification artifact."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from spine.domain.common import DefinitionModel


Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]


class ArtifactAttestation(DefinitionModel):
    """Trusted identity returned by an artifact-attestation adapter."""

    artifact_digest: Sha256Digest
    head_sha: CommitSha
    repository: Literal["alkselsv/spine"]
    workflow_ref: str = Field(min_length=1, max_length=512)
    run_invocation_uri: str = Field(
        pattern=r"^https://github\.com/alkselsv/spine/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*$"
    )
