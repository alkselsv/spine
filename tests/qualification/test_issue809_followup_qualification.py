from __future__ import annotations

import hashlib
import json
from pathlib import Path

from spine.domain.evaluations.r1_repository_qualification import (
    R1RepositoryEvidence,
    R1RepositoryQualificationResult,
    R1SkipPolicy,
)
from spine.evaluations.release_gates.r1_repository_qualification import (
    evaluate_r1_repository_evidence,
)


PROJECT_ROOT = Path(__file__).parents[2]
INPUT_PATH = PROJECT_ROOT / "docs/qualification/issue-809-r1-repository-qualification-input.json"
EVIDENCE_PATH = PROJECT_ROOT / "docs/qualification/issue-809-r1-repository-evidence.json"
RESULT_PATH = PROJECT_ROOT / "docs/qualification/issue-809-r1-repository-qualification-result.json"
POLICY_PATH = PROJECT_ROOT / "docs/qualification/r1-repository-skip-policy-v1.json"
REPORT_PATH = PROJECT_ROOT / "docs/qualification/issue-809-attested-r1-followup.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_attested_followup_input_binds_exact_main_artifact_and_policy() -> None:
    package = json.loads(INPUT_PATH.read_text(encoding="utf-8"))

    assert package["target_commit"] == "bfd4887e62e543ccdabebd06c8fc7777e239bab3"
    assert package["workflow"]["run_id"] == "41455426404"
    assert package["workflow"]["job_id"] == "125310772293"
    assert package["workflow"]["source_ref"] == "refs/heads/main"
    assert package["workflow"]["runner_environment"] == "github-hosted"
    assert package["artifact"]["subject_sha256"] == _sha256(EVIDENCE_PATH)
    assert package["skip_policy"]["sha256"] == _sha256(POLICY_PATH)


def test_attested_followup_evidence_recomputes_r1_pass() -> None:
    evidence = R1RepositoryEvidence.model_validate_json(EVIDENCE_PATH.read_bytes())
    policy = R1SkipPolicy.model_validate_json(POLICY_PATH.read_bytes())
    result = R1RepositoryQualificationResult.model_validate_json(RESULT_PATH.read_bytes())

    reasons = evaluate_r1_repository_evidence(
        evidence=evidence,
        skip_policy=policy,
        skip_policy_digest=_sha256(POLICY_PATH),
    )

    assert reasons == ()
    assert result.status == "pass"
    assert result.reason_codes == ()
    assert result.artifact_digest == _sha256(EVIDENCE_PATH)
    assert result.head_sha == evidence.head_sha
    assert evidence.repository_tests.passed == 5822
    assert evidence.repository_tests.failed == 0
    assert evidence.repository_tests.errors == 0
    assert len(evidence.repository_tests.skip_dispositions) == 7
    assert all(
        item.applicability == "optional"
        for item in evidence.repository_tests.skip_dispositions
    )


def test_attested_followup_preserves_release_no_go_scope() -> None:
    report = REPORT_PATH.read_text(encoding="utf-8")

    assert "R1 repository and migration boundary PASS" in report
    assert "overall release decision from **NO-GO**" in report
    assert "R17 remains FAIL" in report
    assert "production deployment" in report
