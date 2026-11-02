from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from spine.domain.evaluations.r1_repository_qualification import (
    ArtifactAttestation,
    MigrationTargetReceipt,
    R1ComponentVersion,
    R1ExecutedCommand,
    R1RepositoryEvidence,
    R1SkipPolicy,
    R1SkipPolicyEntry,
    R1WorkflowExecution,
    RepositoryTestReceipt,
    SkipDisposition,
)
from spine.evaluations.release_gates.r1_repository_qualification import (
    ArtifactAttestationVerificationError,
    qualify_r1_repository_artifact,
)

HEAD_SHA = "a" * 40
POLICY_VERSION = "r1-skip-policy:v1"


class RecordingAttestationVerifier:
    def __init__(self, *, head_sha: str = HEAD_SHA) -> None:
        self.head_sha = head_sha
        self.requests: list[tuple[Path, str, str]] = []

    def verify(
        self,
        *,
        artifact_path: Path,
        artifact_digest: str,
        expected_head_sha: str,
    ) -> ArtifactAttestation:
        self.requests.append((artifact_path, artifact_digest, expected_head_sha))
        return ArtifactAttestation(
            artifact_digest=artifact_digest,
            head_sha=self.head_sha,
            repository="alkselsv/spine",
            workflow_ref="alkselsv/spine/.github/workflows/test.yml@refs/heads/main",
            run_invocation_uri=(
                "https://github.com/alkselsv/spine/actions/runs/41432821168/attempts/1"
            ),
        )


class RejectingAttestationVerifier:
    def verify(
        self,
        *,
        artifact_path: Path,
        artifact_digest: str,
        expected_head_sha: str,
    ) -> ArtifactAttestation:
        raise ArtifactAttestationVerificationError("no trusted attestation")


def _target(*, role: str, fingerprint: str) -> MigrationTargetReceipt:
    return MigrationTargetReceipt(
        role=role,
        target_fingerprint=fingerprint,
        postgresql_major=17,
        head_revision="20261101_76",
        retained_revision_count=76,
        tested_revision_count=76,
        status="passed",
    )


def _evidence(
    *,
    clean_fingerprint: str = "1" * 64,
    upgrade_fingerprint: str = "2" * 64,
    skips: tuple[SkipDisposition, ...] = (),
    skip_policy_digest: str = "3" * 64,
) -> R1RepositoryEvidence:
    return R1RepositoryEvidence(
        head_sha=HEAD_SHA,
        workflow_execution=R1WorkflowExecution(
            workflow_name="test",
            run_id="41432821168",
            job_id="r1-regression-gate",
            run_attempt=1,
            started_at=datetime(2026, 11, 2, 10, tzinfo=timezone.utc),
            completed_at=datetime(2026, 11, 2, 10, 10, tzinfo=timezone.utc),
        ),
        commands=tuple(
            R1ExecutedCommand(boundary=boundary, argv=("uv", "run", "pytest", "-q"))
            for boundary in ("clean_target", "upgrade_target", "repository_tests")
        ),
        component_versions=(
            R1ComponentVersion(component="python", version="3.12.12"),
        ),
        skip_policy_version=POLICY_VERSION,
        skip_policy_digest=skip_policy_digest,
        migration_targets=(
            _target(role="clean", fingerprint=clean_fingerprint),
            _target(role="upgrade", fingerprint=upgrade_fingerprint),
        ),
        repository_tests=RepositoryTestReceipt(
            passed=5799,
            failed=0,
            errors=0,
            skipped=len(skips),
            skip_dispositions=skips,
        ),
    )


def _write_artifact(tmp_path: Path, evidence: R1RepositoryEvidence) -> tuple[Path, str]:
    artifact_path = tmp_path / "r1-repository-evidence.json"
    artifact_path.write_text(evidence.model_dump_json(indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    return artifact_path, digest


def _write_policy(
    tmp_path: Path,
    entries: tuple[R1SkipPolicyEntry, ...] = (),
) -> tuple[Path, str]:
    policy = R1SkipPolicy(policy_version=POLICY_VERSION, entries=entries)
    policy_path = tmp_path / "r1-skip-policy.json"
    policy_path.write_text(policy.model_dump_json(indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    return policy_path, digest


def test_reused_postgresql_target_identity_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    evidence = _evidence(
        clean_fingerprint="1" * 64,
        upgrade_fingerprint="1" * 64,
        skip_policy_digest=policy_digest,
    )
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("migration_target_identity_reused",)


def test_unclassified_skip_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    evidence = _evidence(
        skip_policy_digest=policy_digest,
        skips=(
            SkipDisposition(
                test_id="tests/provider/test_live.py::test_live_provider",
                reported_reason="provider not configured",
                reason_code="provider-not-configured",
                applicability="unclassified",
                policy_version=POLICY_VERSION,
            ),
        )
    )
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("repository_skip_unclassified",)


def test_distinct_targets_and_optional_skip_pass_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(
        tmp_path,
        entries=(
            R1SkipPolicyEntry(
                test_id="tests/provider/test_live.py::test_live_provider",
                expected_reason="live provider is opt-in",
                reason_code="external-provider-opt-in",
                applicability="optional",
            ),
        ),
    )
    evidence = _evidence(
        skip_policy_digest=policy_digest,
        skips=(
            SkipDisposition(
                test_id="tests/provider/test_live.py::test_live_provider",
                reported_reason="live provider is opt-in",
                reason_code="external-provider-opt-in",
                applicability="optional",
                policy_version=POLICY_VERSION,
            ),
        )
    )
    artifact_path, digest = _write_artifact(tmp_path, evidence)
    verifier = RecordingAttestationVerifier()

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=verifier,
    )

    assert result.status == "pass"
    assert result.reason_codes == ()
    assert verifier.requests == [(artifact_path, digest, HEAD_SHA)]


def test_required_skip_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(
        tmp_path,
        entries=(
            R1SkipPolicyEntry(
                test_id="tests/integration/postgresql/test_required.py::test_required",
                expected_reason="required prerequisite unavailable",
                reason_code="required-prerequisite-unavailable",
                applicability="required",
            ),
        ),
    )
    evidence = _evidence(
        skip_policy_digest=policy_digest,
        skips=(
            SkipDisposition(
                test_id="tests/integration/postgresql/test_required.py::test_required",
                reported_reason="required prerequisite unavailable",
                reason_code="required-prerequisite-unavailable",
                applicability="required",
                policy_version=POLICY_VERSION,
            ),
        )
    )
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("repository_required_test_skipped",)


def test_changed_skip_policy_digest_fails_qualification(tmp_path: Path) -> None:
    policy_path, _ = _write_policy(tmp_path)
    evidence = _evidence(skip_policy_digest="f" * 64)
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("skip_policy_digest_mismatch",)


def test_failed_migration_target_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    evidence = _evidence(skip_policy_digest=policy_digest)
    clean, upgrade = evidence.migration_targets
    evidence = evidence.model_copy(
        update={
            "migration_targets": (
                clean.model_copy(update={"status": "failed"}),
                upgrade,
            )
        }
    )
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("migration_target_failed",)


def test_missing_retained_revision_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    evidence = _evidence(skip_policy_digest=policy_digest)
    clean, upgrade = evidence.migration_targets
    evidence = evidence.model_copy(
        update={
            "migration_targets": (
                clean,
                upgrade.model_copy(update={"tested_revision_count": 75}),
            )
        }
    )
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("retained_revision_coverage_incomplete",)


def test_repository_test_failure_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    evidence = _evidence(skip_policy_digest=policy_digest)
    evidence = evidence.model_copy(
        update={
            "repository_tests": evidence.repository_tests.model_copy(
                update={"passed": 5798, "failed": 1}
            )
        }
    )
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("repository_tests_failed",)


def test_missing_trusted_attestation_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    evidence = _evidence(skip_policy_digest=policy_digest)
    artifact_path, digest = _write_artifact(tmp_path, evidence)

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RejectingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("artifact_attestation_untrusted",)


def test_stale_unobserved_skip_policy_entry_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(
        tmp_path,
        entries=(
            R1SkipPolicyEntry(
                test_id="tests/provider/test_removed.py::test_removed",
                expected_reason="removed provider test",
                reason_code="removed-provider-opt-in",
                applicability="optional",
            ),
        ),
    )
    artifact_path, digest = _write_artifact(
        tmp_path, _evidence(skip_policy_digest=policy_digest)
    )

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("skip_policy_stale",)


def test_tampered_artifact_digest_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    artifact_path, _ = _write_artifact(
        tmp_path, _evidence(skip_policy_digest=policy_digest)
    )

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest="f" * 64,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("artifact_digest_mismatch",)


def test_attestation_for_wrong_commit_fails_qualification(tmp_path: Path) -> None:
    policy_path, policy_digest = _write_policy(tmp_path)
    artifact_path, digest = _write_artifact(
        tmp_path, _evidence(skip_policy_digest=policy_digest)
    )

    result = qualify_r1_repository_artifact(
        artifact_path=artifact_path,
        expected_digest=digest,
        expected_head_sha=HEAD_SHA,
        skip_policy_path=policy_path,
        attestation_verifier=RecordingAttestationVerifier(head_sha="b" * 40),
    )

    assert result.status == "fail"
    assert result.reason_codes == ("head_sha_mismatch",)
