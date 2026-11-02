from __future__ import annotations

import hashlib
from pathlib import Path

from spine.domain.evaluations.r1_repository_qualification import (
    ArtifactAttestation,
    MigrationTargetReceipt,
    R1RepositoryEvidence,
    R1RepositoryQualificationResult,
    R1SkipPolicy,
)
from spine.scripts.r1_repository_evidence import main as collect_main
from spine.scripts.r1_repository_qualification import main as verify_main

HEAD_SHA = "a" * 40


class AcceptingAttestationVerifier:
    def verify(
        self,
        *,
        artifact_path: Path,
        artifact_digest: str,
        expected_head_sha: str,
    ) -> ArtifactAttestation:
        del artifact_path
        return ArtifactAttestation(
            artifact_digest=artifact_digest,
            head_sha=expected_head_sha,
            repository="alkselsv/spine",
            workflow_ref="alkselsv/spine/.github/workflows/test.yml@refs/heads/main",
            run_invocation_uri=(
                "https://github.com/alkselsv/spine/actions/runs/41432821168/attempts/1"
            ),
        )


def _collector_arguments(tmp_path: Path, *, reused_target: bool = False) -> list[str]:
    clean_path = tmp_path / "clean.json"
    upgrade_path = tmp_path / "upgrade.json"
    for role, path, fingerprint, tested in (
        ("clean", clean_path, "1" * 64, 1),
        (
            "upgrade",
            upgrade_path,
            "1" * 64 if reused_target else "2" * 64,
            96,
        ),
    ):
        receipt = MigrationTargetReceipt(
            role=role,
            target_fingerprint=fingerprint,
            postgresql_major=17,
            head_revision="20261227_95",
            retained_revision_count=96,
            tested_revision_count=tested,
            status="passed",
        )
        path.write_text(receipt.model_dump_json() + "\n", encoding="utf-8")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        R1SkipPolicy(
            policy_version="r1-skip-policy:2026-11-02", entries=()
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )
    junit_path = tmp_path / "pytest.xml"
    junit_path.write_text(
        '<testsuites tests="1" failures="0" errors="0" skipped="0">'
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="tests.unit.test_ok" name="test_ok" />'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return [
        "--head-sha",
        HEAD_SHA,
        "--clean-target",
        str(clean_path),
        "--upgrade-target",
        str(upgrade_path),
        "--junit",
        str(junit_path),
        "--skip-policy",
        str(policy_path),
        "--workflow-run-id",
        "41432821168",
        "--workflow-run-attempt",
        "1",
        "--started-at",
        "2026-11-02T10:00:00+00:00",
        "--completed-at",
        "2026-11-02T10:10:00+00:00",
        "--output",
        str(tmp_path / "evidence.json"),
    ]


def test_collector_cli_success_writes_canonical_evidence(tmp_path: Path) -> None:
    result = collect_main(_collector_arguments(tmp_path))

    evidence = R1RepositoryEvidence.model_validate_json(
        (tmp_path / "evidence.json").read_bytes()
    )
    assert result == 0
    assert evidence.head_sha == HEAD_SHA
    assert evidence.workflow_execution.run_id == "41432821168"


def test_collector_cli_failed_gate_writes_evidence_and_returns_one(
    tmp_path: Path,
) -> None:
    result = collect_main(_collector_arguments(tmp_path, reused_target=True))

    assert result == 1
    assert (tmp_path / "evidence.json").is_file()


def test_collector_cli_invalid_input_returns_two(tmp_path: Path) -> None:
    arguments = _collector_arguments(tmp_path)
    arguments[3] = str(tmp_path / "missing-clean.json")

    assert collect_main(arguments) == 2


def test_verifier_cli_success_writes_pass_result(tmp_path: Path) -> None:
    assert collect_main(_collector_arguments(tmp_path)) == 0
    artifact_path = tmp_path / "evidence.json"
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    output_path = tmp_path / "result.json"

    result = verify_main(
        _verifier_arguments(tmp_path, digest=digest, output_path=output_path),
        attestation_verifier=AcceptingAttestationVerifier(),
    )

    report = R1RepositoryQualificationResult.model_validate_json(
        output_path.read_bytes()
    )
    assert result == 0
    assert report.status == "pass"


def test_verifier_cli_failed_gate_writes_result_and_returns_one(tmp_path: Path) -> None:
    assert collect_main(_collector_arguments(tmp_path)) == 0
    output_path = tmp_path / "result.json"

    result = verify_main(
        _verifier_arguments(tmp_path, digest="f" * 64, output_path=output_path),
        attestation_verifier=AcceptingAttestationVerifier(),
    )

    assert result == 1
    assert R1RepositoryQualificationResult.model_validate_json(
        output_path.read_bytes()
    ).status == "fail"


def test_verifier_cli_invalid_input_returns_two(tmp_path: Path) -> None:
    result = verify_main(
        _verifier_arguments(
            tmp_path,
            digest="f" * 64,
            output_path=tmp_path / "result.json",
        ),
        attestation_verifier=AcceptingAttestationVerifier(),
    )

    assert result == 2


def _verifier_arguments(
    tmp_path: Path,
    *,
    digest: str,
    output_path: Path,
) -> list[str]:
    return [
        "--artifact",
        str(tmp_path / "evidence.json"),
        "--artifact-digest",
        digest,
        "--head-sha",
        HEAD_SHA,
        "--skip-policy",
        str(tmp_path / "policy.json"),
        "--output",
        str(output_path),
    ]
