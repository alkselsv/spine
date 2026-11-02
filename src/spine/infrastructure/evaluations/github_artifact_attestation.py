"""GitHub CLI adapter for cryptographically verified artifact provenance."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from spine.domain.evaluations.r1_repository_qualification import ArtifactAttestation
from spine.evaluations.release_gates.r1_repository_qualification import (
    ArtifactAttestationVerificationError,
)

_REPOSITORY = "alkselsv/spine"
_SIGNER_WORKFLOW = "alkselsv/spine/.github/workflows/test.yml"
_SOURCE_REF = "refs/heads/main"


@dataclass(frozen=True, slots=True)
class CommandExecutionResult:
    """Content captured from one non-shell command execution."""

    return_code: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Narrow execution port used by the GitHub CLI adapter."""

    def run(self, arguments: tuple[str, ...]) -> CommandExecutionResult: ...


class SubprocessCommandRunner:
    """Execute an argument vector without invoking a shell."""

    def run(self, arguments: tuple[str, ...]) -> CommandExecutionResult:
        completed = subprocess.run(  # noqa: S603 - fixed executable and argument vector
            arguments,
            check=False,
            capture_output=True,
            text=True,
        )
        return CommandExecutionResult(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class GitHubCliArtifactAttestationVerifier:
    """Verify artifact bytes against GitHub's signed Actions provenance."""

    def __init__(self, *, command_runner: CommandRunner) -> None:
        self._command_runner = command_runner

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
            _SIGNER_WORKFLOW,
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
                == f"https://github.com/{_SIGNER_WORKFLOW}@{_SOURCE_REF}"
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
