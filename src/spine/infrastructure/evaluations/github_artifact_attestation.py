"""GitHub CLI adapter for cryptographically verified artifact provenance."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from spine.domain.evaluations.artifact_attestation import ArtifactAttestation
from spine.evaluations.release_gates.artifact_attestation import (
    ArtifactAttestationVerificationError,
)
from spine.infrastructure.evaluations.command_runner import (
    CommandExecutionResult,
    CommandRunner,
    SubprocessCommandRunner,
)

_REPOSITORY = "alkselsv/spine"
_DEFAULT_SIGNER_WORKFLOW = "alkselsv/spine/.github/workflows/test.yml"
_SOURCE_REF = "refs/heads/main"


class GitHubCliArtifactAttestationVerifier:
    """Verify artifact bytes against GitHub's signed Actions provenance."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner,
        signer_workflow: str = _DEFAULT_SIGNER_WORKFLOW,
    ) -> None:
        self._command_runner = command_runner
        self._signer_workflow = signer_workflow

    def verify(
        self,
        *,
        artifact_path: Path,
        artifact_digest: str,
        expected_head_sha: str,
    ) -> ArtifactAttestation:
        arguments = (
            "gh",
            "attestation",
            "verify",
            str(artifact_path),
            "--repo",
            _REPOSITORY,
            "--signer-workflow",
            self._signer_workflow,
            "--source-digest",
            expected_head_sha,
            "--source-ref",
            _SOURCE_REF,
            "--deny-self-hosted-runners",
            "--format",
            "json",
        )
        result = self._command_runner.run(arguments)
        if result.return_code != 0:
            raise ArtifactAttestationVerificationError(
                "GitHub rejected the artifact attestation"
            )

        try:
            records = json.loads(result.stdout)
            certificates = [
                record["verificationResult"]["signature"]["certificate"]
                for record in records
            ]
            certificate = next(
                item
                for item in certificates
                if item["sourceRepositoryDigest"] == expected_head_sha
                and item["subjectAlternativeName"]
                == f"https://github.com/{self._signer_workflow}@{_SOURCE_REF}"
            )
            return ArtifactAttestation(
                artifact_digest=artifact_digest,
                head_sha=certificate["sourceRepositoryDigest"],
                repository=_REPOSITORY,
                workflow_ref=(
                    certificate["subjectAlternativeName"]
                    .removeprefix("https://github.com/")
                ),
                run_invocation_uri=certificate["runInvocationURI"],
            )
        except (KeyError, StopIteration, TypeError, ValueError, ValidationError) as exc:
            raise ArtifactAttestationVerificationError(
                "GitHub returned no acceptable signed identity"
            ) from exc
