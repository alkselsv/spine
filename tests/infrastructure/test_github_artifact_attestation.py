from __future__ import annotations

import json
from pathlib import Path

from spine.infrastructure.evaluations.github_artifact_attestation import (
    CommandExecutionResult,
    GitHubCliArtifactAttestationVerifier,
)

HEAD_SHA = "a" * 40
ARTIFACT_DIGEST = "b" * 64


class RecordingCommandRunner:
    def __init__(self, result: CommandExecutionResult) -> None:
        self.result = result
        self.requests: list[tuple[str, ...]] = []

    def run(self, arguments: tuple[str, ...]) -> CommandExecutionResult:
        self.requests.append(arguments)
        return self.result


def test_verified_github_attestation_returns_signed_identity(tmp_path: Path) -> None:
    artifact_path = tmp_path / "r1-repository-evidence.json"
    artifact_path.write_bytes(b"canonical evidence")
    workflow_ref = "alkselsv/spine/.github/workflows/test.yml@refs/heads/main"
    run_uri = (
        "https://github.com/alkselsv/spine/actions/runs/41432821168/attempts/1"
    )
    output = json.dumps(
        [
            {
                "verificationResult": {
                    "signature": {
                        "certificate": {
                            "subjectAlternativeName": f"https://github.com/{workflow_ref}",
                            "sourceRepositoryDigest": HEAD_SHA,
                            "runInvocationURI": run_uri,
                        }
                    }
                }
            }
        ]
    )
    runner = RecordingCommandRunner(
        CommandExecutionResult(return_code=0, stdout=output, stderr="")
    )
    verifier = GitHubCliArtifactAttestationVerifier(command_runner=runner)

    attestation = verifier.verify(
        artifact_path=artifact_path,
        artifact_digest=ARTIFACT_DIGEST,
        expected_head_sha=HEAD_SHA,
    )

    assert attestation.artifact_digest == ARTIFACT_DIGEST
    assert attestation.head_sha == HEAD_SHA
    assert attestation.workflow_ref == workflow_ref
    assert attestation.run_invocation_uri == run_uri
    assert runner.requests == [
        (
            "gh",
            "attestation",
            "verify",
            str(artifact_path),
            "--repo",
            "alkselsv/spine",
            "--signer-workflow",
            "alkselsv/spine/.github/workflows/test.yml",
            "--source-digest",
            HEAD_SHA,
            "--source-ref",
            "refs/heads/main",
            "--deny-self-hosted-runners",
            "--format",
            "json",
        )
    ]
