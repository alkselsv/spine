"""Verify attested provenance and recompute criterion R2."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pydantic import ValidationError

from spine.evaluations.release_gates.artifact_attestation import (
    ArtifactAttestationVerifier,
)
from spine.evaluations.release_gates.r2_deployment_qualification import (
    qualify_r2_deployment_artifact,
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
        description="Verify signed provenance and recompute the live R2 gate."
    )
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-digest", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--deployment-fingerprint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    verifier = attestation_verifier or GitHubCliArtifactAttestationVerifier(
        command_runner=SubprocessCommandRunner(),
        signer_workflow=(
            "alkselsv/spine/.github/workflows/r2-deployment-qualification.yml"
        ),
    )

    try:
        result = qualify_r2_deployment_artifact(
            artifact_path=arguments.artifact,
            expected_digest=arguments.artifact_digest,
            expected_head_sha=arguments.head_sha,
            expected_deployment_fingerprint=arguments.deployment_fingerprint,
            attestation_verifier=verifier,
        )
        arguments.output.write_text(
            result.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValidationError, ValueError):
        print("R2 deployment qualification refused invalid input.", file=sys.stderr)
        return 2
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
