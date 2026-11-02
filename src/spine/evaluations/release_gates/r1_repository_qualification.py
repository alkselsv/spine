"""Trusted artifact verification and fail-closed R1 repository qualification."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from spine.domain.evaluations.r1_repository_qualification import (
    ArtifactAttestation,
    R1RepositoryEvidence,
    R1RepositoryQualificationResult,
    R1SkipPolicy,
)


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


def qualify_r1_repository_artifact(
    *,
    artifact_path: Path,
    expected_digest: str,
    expected_head_sha: str,
    skip_policy_path: Path,
    attestation_verifier: ArtifactAttestationVerifier,
) -> R1RepositoryQualificationResult:
    """Verify one artifact and evaluate the complete repository R1 contract."""

    artifact_bytes = artifact_path.read_bytes()
    observed_digest = hashlib.sha256(artifact_bytes).hexdigest()
    reasons: list[str] = []
    if observed_digest != expected_digest:
        reasons.append("artifact_digest_mismatch")

    try:
        evidence = R1RepositoryEvidence.model_validate_json(artifact_bytes)
    except ValidationError:
        return R1RepositoryQualificationResult(
            status="fail",
            reason_codes=("artifact_schema_invalid",),
            artifact_digest=observed_digest,
            head_sha=expected_head_sha,
        )

    try:
        attestation = attestation_verifier.verify(
            artifact_path=artifact_path,
            artifact_digest=observed_digest,
            expected_head_sha=expected_head_sha,
        )
    except ArtifactAttestationVerificationError:
        return R1RepositoryQualificationResult(
            status="fail",
            reason_codes=tuple((*reasons, "artifact_attestation_untrusted")),
            artifact_digest=observed_digest,
            head_sha=expected_head_sha,
        )
    if attestation.artifact_digest != observed_digest:
        reasons.append("attestation_digest_mismatch")
    if attestation.head_sha != expected_head_sha or evidence.head_sha != expected_head_sha:
        reasons.append("head_sha_mismatch")

    policy_bytes = skip_policy_path.read_bytes()
    policy_digest = hashlib.sha256(policy_bytes).hexdigest()
    try:
        skip_policy = R1SkipPolicy.model_validate_json(policy_bytes)
    except ValidationError:
        skip_policy = None
        reasons.append("skip_policy_schema_invalid")
    if policy_digest != evidence.skip_policy_digest:
        reasons.append("skip_policy_digest_mismatch")
    if skip_policy is not None:
        if skip_policy.policy_version != evidence.skip_policy_version:
            reasons.append("skip_policy_version_mismatch")
        policy_by_test_id = {item.test_id: item for item in skip_policy.entries}
        for disposition in evidence.repository_tests.skip_dispositions:
            policy_entry = policy_by_test_id.get(disposition.test_id)
            if disposition.applicability == "unclassified":
                continue
            if (
                policy_entry is None
                or policy_entry.expected_reason != disposition.reported_reason
                or policy_entry.reason_code != disposition.reason_code
                or policy_entry.applicability != disposition.applicability
                or disposition.policy_version != skip_policy.policy_version
            ):
                reasons.append("skip_policy_disposition_mismatch")
                break

    clean = next(item for item in evidence.migration_targets if item.role == "clean")
    upgrade = next(item for item in evidence.migration_targets if item.role == "upgrade")
    if clean.target_fingerprint == upgrade.target_fingerprint:
        reasons.append("migration_target_identity_reused")
    if upgrade.tested_revision_count != upgrade.retained_revision_count:
        reasons.append("retained_revision_coverage_incomplete")
    if any(item.status == "failed" for item in evidence.migration_targets):
        reasons.append("migration_target_failed")
    if evidence.repository_tests.failed or evidence.repository_tests.errors:
        reasons.append("repository_tests_failed")
    if any(
        item.applicability == "unclassified"
        for item in evidence.repository_tests.skip_dispositions
    ):
        reasons.append("repository_skip_unclassified")
    if any(
        item.applicability == "required"
        for item in evidence.repository_tests.skip_dispositions
    ):
        reasons.append("repository_required_test_skipped")

    return R1RepositoryQualificationResult(
        status="fail" if reasons else "pass",
        reason_codes=tuple(reasons),
        artifact_digest=observed_digest,
        head_sha=expected_head_sha,
    )
