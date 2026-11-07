from __future__ import annotations

from decimal import Decimal

from spine.evals.unsupported_requirements_runner import (
    ISSUE_674_DATASET_SHA256,
    run_unsupported_requirements_qualification,
    unsupported_requirements_evaluation_identity,
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
    assert report.supported_precision.value == Decimal("1")
    assert report.supported_recall.value == Decimal("0.875")
    assert report.unsupported_safe_handling_recall.value == Decimal("1")
    assert report.clarification_needed_recall.value == Decimal("1")
    assert report.inconclusive_abstention_recall.value == Decimal("1")
    assert report.not_verified_reasons == (
        "insufficient_labelled_coverage",
        "missing_mandatory_stratum",
    )
    supported_to_clarification = next(
        item
        for item in report.confusion_matrix
        if item.actual_class is UnsupportedRequirementsClass.SUPPORTED
        and item.predicted_class is UnsupportedRequirementsClass.CLARIFICATION_NEEDED
    )
    assert supported_to_clarification.count == 1


def test_unverifiable_tested_commit_produces_not_verified_report() -> None:
    report = run_unsupported_requirements_qualification(
        tested_commit_sha="f" * 40,
    )

    assert report.result is UnsupportedRequirementsGateResult.NOT_VERIFIED
    assert report.observed_item_count == 0
    assert report.execution_failures == ("qualification_execution_failed",)
    assert "incomplete_execution" in report.not_verified_reasons
