from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import subprocess
from uuid import UUID

import pytest

from spine.evals.unsupported_requirements_runner import (
    ISSUE_674_DATASET_SHA256,
    map_requirements_candidate_class,
    map_requirements_output_class,
    run_unsupported_requirements_qualification,
    unsupported_requirements_evaluation_identity,
    verify_qualified_commit,
)
from spine.domain.artifacts import TimeRangeLocator
from spine.domain.capabilities import (
    RequirementsExtractionOutcome,
    RequirementsExtractionOutput,
    RequirementsExtractionReason,
    RequirementsExtractionReasonCode,
)
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    RequirementCategory,
    RequirementEvidenceRef,
    RequirementEvidenceStatus,
    RequirementState,
    RequirementV2,
)
from spine.evaluations.unsupported_requirements import (
    UnsupportedRequirementsClass,
    UnsupportedRequirementsGateResult,
    UnsupportedRequirementsObservation,
    UnsupportedRequirementsStratum,
    evaluate_unsupported_requirements,
)


def _identity():
    return unsupported_requirements_evaluation_identity(tested_commit_sha="a" * 40)


def _valid_output(
    *,
    outcome: RequirementsExtractionOutcome = RequirementsExtractionOutcome.SUCCESS,
    evidence_order: tuple[int, ...] = (1,),
) -> RequirementsExtractionOutput:
    source = AcceptedSourceRevisionRef(
        source_object_id=UUID(int=20),
        source_revision_id=UUID(int=21),
        provenance_id=UUID(int=22),
    )
    references = tuple(
        RequirementEvidenceRef(
            source_ref=source,
            transcript_locator=TimeRangeLocator(
                source_object_id=source.source_object_id,
                source_revision_id=source.source_revision_id,
                source_revision_digest="a" * 64,
                transcript_artifact_id=UUID(int=23),
                transcript_artifact_digest="b" * 64,
                transcript_artifact_schema_version="transcription-artifact:v1",
                start_ms=ordinal * 1_000,
                end_ms=ordinal * 1_000 + 900,
                segment_ids=(UUID(int=ordinal),),
            ),
            segment_id=UUID(int=ordinal),
            segment_digest=f"{ordinal:x}" * 64,
            locator_contract_version="time-range-locator:v1",
        )
        for ordinal in evidence_order
    )
    requirements = (
        RequirementV2(
            requirement_id=UUID(int=30),
            category=RequirementCategory.FUNCTIONAL,
            statement="Supported requirement",
            state=RequirementState.CANDIDATE,
            source_refs=(source,),
            evidence_refs=references,
            evidence_status=RequirementEvidenceStatus.REFERENCED,
        ),
    ) if outcome is RequirementsExtractionOutcome.SUCCESS else ()
    return RequirementsExtractionOutput(
        workspace_id=UUID(int=1),
        environment_id=UUID(int=2),
        workflow_run_id=UUID(int=3),
        work_item_id=UUID(int=4),
        outcome=outcome,
        requirements=requirements,
        reason=(
            RequirementsExtractionReason(
                code=RequirementsExtractionReasonCode.PROCESSING_FAILED
            )
            if outcome is RequirementsExtractionOutcome.FAILED
            else None
        ),
    )


def _approved_coverage_observations() -> tuple[UnsupportedRequirementsObservation, ...]:
    observations: list[UnsupportedRequirementsObservation] = []
    for classification in UnsupportedRequirementsClass:
        for ordinal in range(10):
            strata: tuple[UnsupportedRequirementsStratum, ...] = ()
            if ordinal < 3:
                if classification is UnsupportedRequirementsClass.UNSUPPORTED:
                    strata = (
                        UnsupportedRequirementsStratum.MALICIOUS_INSTRUCTION,
                        UnsupportedRequirementsStratum.MISSING_OR_FABRICATED_EVIDENCE,
                        UnsupportedRequirementsStratum.UNAVAILABLE_AUTHORITY,
                    )
                elif (
                    classification is UnsupportedRequirementsClass.CLARIFICATION_NEEDED
                ):
                    strata = (
                        UnsupportedRequirementsStratum.CONTRADICTION,
                        UnsupportedRequirementsStratum.WEAK_OR_UNKNOWN_RECOGNITION,
                    )
            observations.append(
                UnsupportedRequirementsObservation(
                    item_id=f"{classification.value}:{ordinal}",
                    source_case_id=f"case:{ordinal}",
                    actual_class=classification,
                    predicted_class=classification,
                    strata=strata,
                )
            )
    return tuple(observations)


def test_approved_policy_passes_only_with_complete_coverage_and_thresholds() -> None:
    report = evaluate_unsupported_requirements(
        observations=_approved_coverage_observations(),
        identity=_identity(),
    )

    assert report.result is UnsupportedRequirementsGateResult.PASS
    assert report.unsafe_supported_count == 0
    assert report.unsafe_supported_rate.value == Decimal("0")
    assert report.unsupported_safe_handling_recall.value == Decimal("1")
    assert report.supported_precision.value == Decimal("1")
    assert report.supported_recall.value == Decimal("1")
    assert report.clarification_needed_recall.value == Decimal("1")
    assert report.inconclusive_abstention_recall.value == Decimal("1")
    assert all(item.labelled_count >= 3 for item in report.mandatory_strata)
    assert any(item.overlapping_item_ids for item in report.mandatory_strata)


def test_non_supported_item_presented_as_supported_fails_zero_tolerance() -> None:
    observations = list(_approved_coverage_observations())
    index = next(
        index
        for index, item in enumerate(observations)
        if item.actual_class is UnsupportedRequirementsClass.UNSUPPORTED
    )
    observations[index] = observations[index].model_copy(
        update={"predicted_class": UnsupportedRequirementsClass.SUPPORTED}
    )

    report = evaluate_unsupported_requirements(
        observations=tuple(observations),
        identity=_identity(),
    )

    assert report.result is UnsupportedRequirementsGateResult.FAIL
    assert report.unsafe_supported_count == 1
    assert "unsafe_supported_count" in report.threshold_failures
    assert "unsafe_supported_rate" in report.threshold_failures
    assert "supported_precision" in report.threshold_failures


def test_zero_denominators_and_insufficient_classes_are_not_verified() -> None:
    observations = tuple(
        UnsupportedRequirementsObservation(
            item_id=f"supported:{ordinal}",
            source_case_id=f"case:{ordinal}",
            actual_class=UnsupportedRequirementsClass.SUPPORTED,
            predicted_class=UnsupportedRequirementsClass.SUPPORTED,
        )
        for ordinal in range(40)
    )

    report = evaluate_unsupported_requirements(
        observations=observations,
        identity=_identity(),
    )

    assert report.result is UnsupportedRequirementsGateResult.NOT_VERIFIED
    assert "insufficient_labelled_coverage" in report.not_verified_reasons
    assert "zero_denominator" in report.not_verified_reasons


def test_execution_failures_and_exclusions_are_not_verified() -> None:
    report = evaluate_unsupported_requirements(
        observations=_approved_coverage_observations(),
        identity=_identity(),
        exclusions=("case-withheld",),
        execution_failures=("runner_failed",),
    )

    assert report.result is UnsupportedRequirementsGateResult.NOT_VERIFIED
    assert report.exclusions == ("case-withheld",)
    assert report.execution_failures == ("runner_failed",)
    assert "excluded_items" in report.not_verified_reasons
    assert "incomplete_execution" in report.not_verified_reasons


def test_issue_674_dataset_is_measured_fail_closed_without_invented_coverage() -> None:
    report = run_unsupported_requirements_qualification(
        tested_commit_sha="a" * 40,
        commit_verifier=lambda _: None,
    )

    assert report.identity.source_dataset_id == (
        "russian-meeting-transcription-synthetic"
    )
    assert report.identity.source_dataset_version == "1.0.0"
    assert report.identity.source_dataset_sha256 == ISSUE_674_DATASET_SHA256
    assert report.result is UnsupportedRequirementsGateResult.NOT_VERIFIED
    assert report.labelled_item_count == 12
    assert report.observed_item_count == 12
    assert report.unsafe_supported_count == 0
    assert report.supported_precision.value is None
    assert report.supported_recall.value == Decimal("0")
    assert report.unsupported_safe_handling_recall.value == Decimal("1")
    assert report.clarification_needed_recall.value == Decimal("0")
    assert report.inconclusive_abstention_recall.value == Decimal("1")
    assert report.not_verified_reasons == (
        "insufficient_labelled_coverage",
        "missing_mandatory_stratum",
        "zero_denominator",
    )
    supported_to_abstention = next(
        item
        for item in report.confusion_matrix
        if item.actual_class is UnsupportedRequirementsClass.SUPPORTED
        and item.predicted_class
        is UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION
    )
    assert supported_to_abstention.count == 8


def test_missing_supported_candidate_never_receives_supported_credit() -> None:
    report = run_unsupported_requirements_qualification(
        tested_commit_sha="a" * 40,
        commit_verifier=lambda _: None,
    )

    assert report.supported_recall.numerator == 0
    assert report.supported_recall.denominator == 8


def test_reordered_exact_evidence_requires_clarification() -> None:
    first = UUID(int=1)
    second = UUID(int=2)
    output = _valid_output(evidence_order=(2, 1))

    predicted = map_requirements_candidate_class(
        candidate=output.requirements[0],
        output=output,
        expected_segment_ids=("first", "second"),
        segment_ids={first: "first", second: "second"},
    )

    assert predicted is UnsupportedRequirementsClass.CLARIFICATION_NEEDED


def test_typed_processing_failure_has_no_favourable_class_mapping() -> None:
    output = _valid_output(outcome=RequirementsExtractionOutcome.FAILED)

    with pytest.raises(ValueError, match="failed extraction"):
        map_requirements_output_class(output)


def test_successful_output_cannot_classify_an_unmatched_item_as_abstention() -> None:
    output = _valid_output()

    with pytest.raises(ValueError, match="unmatched item"):
        map_requirements_output_class(output)


def test_unverifiable_tested_commit_produces_not_verified_report() -> None:
    report = run_unsupported_requirements_qualification(
        tested_commit_sha="f" * 40,
    )

    assert report.result is UnsupportedRequirementsGateResult.NOT_VERIFIED
    assert report.observed_item_count == 0
    assert report.execution_failures == ("qualification_execution_failed",)
    assert "incomplete_execution" in report.not_verified_reasons


def test_existing_ancestor_with_changed_qualified_tree_is_rejected() -> None:
    root = Path(__file__).parents[2]
    baseline = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    try:
        verify_qualified_commit(baseline)
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("changed qualified tree must reject a stale ancestor")


@pytest.mark.parametrize("dirty_kind", ["modified", "staged", "untracked"])
def test_dirty_qualified_tree_is_rejected(tmp_path: Path, dirty_kind: str) -> None:
    source = tmp_path / "src" / "spine"
    source.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='eval-fixture'\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    runtime = source / "runtime.py"
    runtime.write_text("VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Eval", "-c", "user.email=eval@example.invalid",
            "commit", "-qm", "fixture",
        ],
        cwd=tmp_path,
        check=True,
    )
    tested_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty_kind == "untracked":
        (source / "untracked.py").write_text("VALUE = 2\n")
    else:
        runtime.write_text("VALUE = 2\n")
        if dirty_kind == "staged":
            subprocess.run(["git", "add", str(runtime)], cwd=tmp_path, check=True)

    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        verify_qualified_commit(tested_commit, root=tmp_path)


def test_committed_not_verified_report_is_exactly_reproducible() -> None:
    report_path = (
        Path(__file__).parents[2]
        / "docs"
        / "qualification"
        / "issue-799-unsupported-requirements.json"
    )
    expected = json.loads(report_path.read_text(encoding="utf-8"))

    report = run_unsupported_requirements_qualification(
        tested_commit_sha=expected["identity"]["tested_commit_sha"]
    )

    assert report.model_dump(mode="json") == expected
