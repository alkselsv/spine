"""Independently verify and qualify an attested R1 repository artifact."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pydantic import ValidationError

from spine.evaluations.release_gates.r1_repository_qualification import (
    ArtifactAttestationVerifier,
    qualify_r1_repository_artifact,
)
from spine.infrastructure.evaluations.github_artifact_attestation import (
    GitHubCliArtifactAttestationVerifier,
    SubprocessCommandRunner,
)


def main(
    argv: list[str] | None = None,
    *,
    attestation_verifier: ArtifactAttestationVerifier | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Verify signed provenance and recompute the R1 repository gate."
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-digest", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--skip-policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    verifier = attestation_verifier or GitHubCliArtifactAttestationVerifier(
        command_runner=SubprocessCommandRunner()
    )

    try:
        result = qualify_r1_repository_artifact(
            artifact_path=arguments.artifact,
            expected_digest=arguments.artifact_digest,
            expected_head_sha=arguments.head_sha,
            skip_policy_path=arguments.skip_policy,
            attestation_verifier=verifier,
        )
        arguments.output.write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValidationError, ValueError):
        print("R1 repository qualification refused invalid input.", file=sys.stderr)
        return 2
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
