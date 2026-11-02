"""Behavioral tests for the final R0/R1 qualification gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from spine.evaluations.release_gates.final_qualification import (
    CriterionEvidence,
    CriterionInput,
    EvidenceArtifact,
    EvidenceIndex,
    FinalQualificationPackage,
    evaluate_final_qualification,
    verify_evidence_index,
)

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
COMMIT = "a" * 40
FINGERPRINT = "b" * 64
POLICY = "r1-policy-generation:v1"
EVIDENCE_KINDS = {
    1: ("migration_graph", "repository_gate"),
    2: ("deployment_profile",),
    3: ("authorization",),
    4: ("ingestion_lifecycle",),
    5: ("physical_provider",),
    6: ("projection_live", "projection_failure_injection"),
    7: ("grounded_qa",),
    8: ("safe_outcomes",),
    9: ("durable_session",),
    10: ("browser_journey",),
    11: ("diagnostics",),
    12: ("privacy_scan",),
    13: ("backup_restore",),
    14: ("real_baseline",),
    15: ("threshold_approval", "threshold_gate"),
    16: ("legacy_disposition",),
}
BOUNDARIES = {
    1: "repository",
    2: "live",
    3: "live",
    4: "live",
    5: "physical",
    6: "physical",
    7: "live",
    8: "live",
    9: "live",
    10: "live",
    11: "live",
    12: "live",
    13: "physical",
    14: "live",
    15: "repository",
    16: "repository",
}
PREREQUISITES = {
    1: (),
    2: ("R1",),
    3: ("R2",),
    4: ("R2",),
    5: ("R2", "R4"),
    6: ("R4", "R5"),
    7: ("R2", "R3", "R5", "R6"),
    8: ("R7",),
    9: ("R2", "R3"),
    10: ("R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"),
    11: ("R10",),
    12: ("R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10", "R11"),
    13: ("R2", "R4", "R5", "R6"),
    14: ("R2", "R3", "R4", "R5", "R6", "R7"),
    15: ("R14",),
    16: ("R1",),
}


def _evidence(
    criterion_id: str,
    *,
    evidence_kind: str | None = None,
    status: str = "pass",
    mandatory: bool = True,
    target_commit: str | None = COMMIT,
    deployment_fingerprint: str | None = FINGERPRINT,
    policy_generation: str | None = POLICY,
    captured_at: datetime | None = NOW - timedelta(hours=1),
    boundary: str | None = None,
) -> CriterionEvidence:
    number = int(criterion_id.removeprefix("R"))
    kind = evidence_kind or EVIDENCE_KINDS[number][0]
    return CriterionEvidence(
        evidence_id=f"evidence:{criterion_id.lower()}:{kind}:v1",
        evidence_kind=kind,
        digest=hashlib.sha256(
            f"evidence:{criterion_id.lower()}:{kind}:v1".encode("ascii")
        ).hexdigest(),
        status=status,
        mandatory=mandatory,
        boundary=boundary
        or (
            "repository"
            if kind == "projection_failure_injection"
            else BOUNDARIES[number]
        ),
        target_commit=target_commit,
        deployment_fingerprint=deployment_fingerprint,
        policy_generation=policy_generation,
        captured_at=captured_at,
    )


def _criterion(
    number: int,
    *,
    status: str = "pass",
    evidence: tuple[CriterionEvidence, ...] | None = None,
) -> CriterionInput:
    criterion_id = f"R{number}"
    default_evidence = tuple(
        _evidence(criterion_id, evidence_kind=kind) for kind in EVIDENCE_KINDS[number]
    )
    return CriterionInput(
        criterion_id=criterion_id,
        owner_function=f"owner:{criterion_id.lower()}",
        responsible_principal_id=(
            f"principal:{criterion_id.lower()}" if status == "pass" else None
        ),
        ticket_ids=(16, 500 + number),
        prerequisite_ids=PREREQUISITES[number],
        status=status,
        reason_codes=() if status == "pass" else (f"{status}_by_test",),
        evidence=evidence if evidence is not None else default_evidence,
    )


def _package(
    *overrides: CriterionInput,
    criteria: tuple[CriterionInput, ...] | None = None,
) -> FinalQualificationPackage:
    by_id = {item.criterion_id: item for item in overrides}
    return FinalQualificationPackage(
        package_id="issue-511:qualification-input:v1",
        target_commit=COMMIT,
        deployment_fingerprint=FINGERPRINT,
        policy_generation=POLICY,
        started_at=NOW - timedelta(hours=2),
        evaluated_at=NOW,
        evidence_valid_after=NOW - timedelta(days=1),
        release_owner_function="release-owner",
        release_owner_principal_id="principal:release-owner",
        component_versions=(("spine", "test-v1"),),
        configuration_digests=(("deployment-profile", FINGERPRINT),),
        scope_references=(("workspace-a", "production"),),
        known_limitations=("none-for-test",),
        criteria=(
            criteria
            if criteria is not None
            else tuple(
                by_id.get(f"R{number}", _criterion(number)) for number in range(1, 17)
            )
        ),
    )


def _verified_index(package: FinalQualificationPackage, tmp_path: Path):
    artifacts = {}
    for criterion in package.criteria:
        for evidence in criterion.evidence:
            artifacts.setdefault(evidence.evidence_id, evidence.digest)
    entries = []
    for number, (evidence_id, digest) in enumerate(sorted(artifacts.items())):
        artifact_path = f"evidence-{number}.txt"
        (tmp_path / artifact_path).write_text(evidence_id, encoding="ascii")
        assert hashlib.sha256(evidence_id.encode("ascii")).hexdigest() == digest
        entries.append(
            EvidenceArtifact(
                evidence_id=evidence_id,
                artifact_path=artifact_path,
                digest=digest,
            )
        )
    return verify_evidence_index(EvidenceIndex(entries=tuple(entries)), root=tmp_path)


def test_all_current_mandatory_evidence_produces_go(tmp_path: Path) -> None:
    package = _package()
    verified = _verified_index(package, tmp_path)
    report = evaluate_final_qualification(package, verified)

    assert report.decision == "go"
    assert report.criteria[-1].criterion_id == "R17"
    assert report.criteria[-1].status == "pass"
    assert report.counts.model_dump() == {
        "passed": 17,
        "failed": 0,
        "blocked": 0,
        "not_verified": 0,
        "not_applicable": 0,
    }
    assert report.blockers == ()
    assert len(report.input_digest) == 64
    assert report == evaluate_final_qualification(package, verified)


@pytest.mark.parametrize("status", ["fail", "blocked", "not_verified"])
def test_any_non_pass_mandatory_criterion_produces_no_go(
    status: str, tmp_path: Path
) -> None:
    package = _package(_criterion(8, status=status))
    report = evaluate_final_qualification(package, _verified_index(package, tmp_path))

    assert report.decision == "no_go"
    assert report.criteria[7].status == status
    assert report.criteria[-1].status == "fail"
    assert report.blockers[0].criterion_id == "R8"
    assert report.blockers[0].owner_function == "owner:r8"


def test_missing_release_criterion_is_rejected() -> None:
    with pytest.raises(ValidationError, match="R1 through R16"):
        _package(criteria=tuple(_criterion(number) for number in range(1, 16)))


@pytest.mark.parametrize(
    ("evidence", "reason_code"),
    [
        (_evidence("R4", target_commit="c" * 40), "evidence_commit_mismatch"),
        (
            _evidence("R4", deployment_fingerprint="d" * 64),
            "evidence_deployment_mismatch",
        ),
        (_evidence("R4", policy_generation="other:v1"), "evidence_policy_mismatch"),
        (
            _evidence("R4", captured_at=NOW - timedelta(days=2)),
            "evidence_stale",
        ),
        (
            _evidence("R4", target_commit=None),
            "evidence_binding_missing",
        ),
        (
            _evidence("R4", boundary="synthetic"),
            "synthetic_evidence_cannot_pass",
        ),
    ],
)
def test_incompatible_or_stale_pass_evidence_is_not_verified(
    evidence: CriterionEvidence,
    reason_code: str,
    tmp_path: Path,
) -> None:
    package = _package(_criterion(4, evidence=(evidence,)))
    report = evaluate_final_qualification(package, _verified_index(package, tmp_path))

    criterion = report.criteria[3]
    assert criterion.status == "not_verified"
    assert reason_code in criterion.reason_codes
    assert report.decision == "no_go"


def test_conflicting_evidence_is_not_verified(tmp_path: Path) -> None:
    passed = _evidence("R5")
    failed = passed.model_copy(update={"status": "fail", "digest": "f" * 64})

    package = _package(_criterion(5, evidence=(passed, failed)))
    report = evaluate_final_qualification(package, _verified_index(package, tmp_path))

    assert report.criteria[4].status == "not_verified"
    assert "evidence_conflict" in report.criteria[4].reason_codes


def test_non_applicable_optional_evidence_remains_distinct_from_pass(
    tmp_path: Path,
) -> None:
    required = tuple(
        _evidence("R15", evidence_kind=kind) for kind in EVIDENCE_KINDS[15]
    )
    denied_population = CriterionEvidence(
        evidence_id="metric:denial-quality:v1",
        evidence_kind="metric",
        digest=hashlib.sha256(b"metric:denial-quality:v1").hexdigest(),
        status="not_applicable",
        mandatory=False,
        boundary="physical",
        target_commit=COMMIT,
        deployment_fingerprint=FINGERPRINT,
        policy_generation=POLICY,
        captured_at=NOW - timedelta(hours=1),
        eligible_count=0,
    )

    package = _package(_criterion(15, evidence=(*required, denied_population)))
    report = evaluate_final_qualification(package, _verified_index(package, tmp_path))

    assert report.decision == "go"
    assert report.counts.not_applicable == 1
    assert report.criteria[14].evidence[2].status == "not_applicable"


def test_wrong_criterion_evidence_kind_is_rejected() -> None:
    wrong = _evidence("R10", evidence_kind="authorization")

    with pytest.raises(ValidationError, match="required evidence kind"):
        _package(_criterion(10, evidence=(wrong,)))


def test_wrong_prerequisite_map_is_rejected() -> None:
    criterion = _criterion(9).model_copy(update={"prerequisite_ids": ("R1",)})

    with pytest.raises(ValidationError, match="prerequisite map"):
        _package(criterion)


def test_not_applicable_requires_zero_eligible_population() -> None:
    with pytest.raises(ValidationError, match="zero eligible count"):
        CriterionEvidence(
            evidence_id="metric:denial-quality:v1",
            evidence_kind="metric",
            digest="e" * 64,
            status="not_applicable",
            mandatory=False,
            boundary="repository",
            target_commit=COMMIT,
            deployment_fingerprint=FINGERPRINT,
            policy_generation=POLICY,
            captured_at=NOW,
            eligible_count=1,
        )


def test_not_applicable_is_rejected_outside_permitted_metric(tmp_path: Path) -> None:
    base = _criterion(10)
    invalid = CriterionEvidence(
        evidence_id="metric:empty-browser-population:v1",
        evidence_kind="metric",
        digest=hashlib.sha256(b"metric:empty-browser-population:v1").hexdigest(),
        status="not_applicable",
        mandatory=False,
        boundary="live",
        eligible_count=0,
    )

    with pytest.raises(ValidationError, match="not_applicable is not permitted"):
        _package(base.model_copy(update={"evidence": (*base.evidence, invalid)}))


def test_evidence_index_rejects_tampered_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text("changed", encoding="utf-8")
    index = EvidenceIndex(
        entries=(
            EvidenceArtifact(
                evidence_id="evidence:r1:migration:v1",
                artifact_path="artifact.json",
                digest="a" * 64,
            ),
        )
    )

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_evidence_index(index, root=tmp_path)


def test_correct_evidence_kind_at_wrong_boundary_cannot_pass(tmp_path: Path) -> None:
    evidence = _evidence("R10", boundary="physical")
    package = _package(_criterion(10, evidence=(evidence,)))

    report = evaluate_final_qualification(package, _verified_index(package, tmp_path))

    assert report.criteria[9].status == "not_verified"
    assert "evidence_boundary_mismatch" in report.criteria[9].reason_codes


def test_metadata_output_contains_only_bounded_references(tmp_path: Path) -> None:
    package = _package()
    payload = evaluate_final_qualification(
        package, _verified_index(package, tmp_path)
    ).model_dump(mode="json")
    serialized = str(payload).lower()

    assert set(payload) == {
        "schema_version",
        "package_id",
        "target_commit",
        "deployment_fingerprint",
        "policy_generation",
        "release_owner_principal_id",
        "started_at",
        "evaluated_at",
        "component_versions",
        "configuration_digests",
        "scope_references",
        "known_limitations",
        "decision",
        "input_digest",
        "evidence_index_digest",
        "criteria",
        "counts",
        "blockers",
    }
    for forbidden in (
        "question",
        "answer",
        "excerpt",
        "filename",
        "credential",
        "raw_payload",
        "chain_of_thought",
    ):
        assert forbidden not in serialized
