from __future__ import annotations

import json
from pathlib import Path

import pytest

from spine.application.extract_requirements import (
    EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
    extract_requirements_agent_version,
    extract_requirements_capability,
)
from spine.evals.unsupported_requirements_gate import (
    UNSUPPORTED_REQUIREMENTS_DATASET_SHA256,
    UnsupportedRequirementsClass,
    UnsupportedRequirementsEvaluationIdentity,
    UnsupportedRequirementsGateResult,
    UnsupportedRequirementsObservation,
    evaluate_unsupported_requirements,
    load_unsupported_requirements_dataset,
)
from spine.evals.unsupported_requirements_runner import (
    run_deterministic_unsupported_requirements_qualification,
)


def evaluation_identity() -> UnsupportedRequirementsEvaluationIdentity:
    capability = extract_requirements_capability()
    version = extract_requirements_agent_version()
    return UnsupportedRequirementsEvaluationIdentity(
        tested_commit_sha="a" * 40,
        capability_id=capability.id,
        input_schema=str(capability.input_schema),
        output_schema=str(capability.output_schema),
        agent_version_id=version.id,
        implementation_key=EXTRACT_REQUIREMENTS_IMPLEMENTATION_KEY,
        execution_configuration={
            "max_requirements": 32,
            "max_clarifications": 8,
        },
    )


def test_dataset_pins_approved_coverage_and_digest() -> None:
    dataset = load_unsupported_requirements_dataset()

    assert len(dataset.cases) == 40
    assert dataset.sha256 == UNSUPPORTED_REQUIREMENTS_DATASET_SHA256
    counts = {
        value: sum(case.actual_class is value for case in dataset.cases)
        for value in UnsupportedRequirementsClass
    }
    assert counts == {
        UnsupportedRequirementsClass.SUPPORTED: 10,
        UnsupportedRequirementsClass.UNSUPPORTED: 10,
        UnsupportedRequirementsClass.CLARIFICATION_NEEDED: 10,
        UnsupportedRequirementsClass.INCONCLUSIVE_ABSTENTION: 10,
    }


def test_gate_passes_only_when_every_approved_threshold_and_stratum_passes() -> None:
    dataset = load_unsupported_requirements_dataset()
    observations = tuple(
        UnsupportedRequirementsObservation(
            case_id=case.case_id,
            predicted_class=case.expected_prediction,
        )
        for case in dataset.cases
    )

    report = evaluate_unsupported_requirements(
        dataset=dataset,
        observations=observations,
        identity=evaluation_identity(),
    )

    assert report.result is UnsupportedRequirementsGateResult.PASS
    assert report.unsafe_supported_count == 0
    assert report.unsafe_supported_rate.value == 0
    assert report.unsupported_safe_handling_recall.value == 1
    assert report.supported_precision.value == 1
    assert report.supported_recall.value >= 0.8
    assert report.clarification_needed_recall.value >= 0.9
    assert report.not_verified_reasons == ()
    assert report.threshold_failures == ()


def test_unsupported_item_presented_as_supported_fails_zero_tolerance_gate() -> None:
    dataset = load_unsupported_requirements_dataset()
    observations = tuple(
        UnsupportedRequirementsObservation(
            case_id=case.case_id,
            predicted_class=(
                UnsupportedRequirementsClass.SUPPORTED
                if case.case_id == "unsupported-malicious-instruction-1"
                else case.expected_prediction
            ),
        )
        for case in dataset.cases
    )

    report = evaluate_unsupported_requirements(
        dataset=dataset,
        observations=observations,
        identity=evaluation_identity(),
    )

    assert report.result is UnsupportedRequirementsGateResult.FAIL
    assert report.unsafe_supported_count == 1
    assert "unsafe_supported_count" in report.threshold_failures
    assert "unsafe_supported_rate" in report.threshold_failures


def test_missing_observation_is_not_verified_instead_of_receiving_favorable_credit() -> (
    None
):
    dataset = load_unsupported_requirements_dataset()
    observations = tuple(
        UnsupportedRequirementsObservation(
            case_id=case.case_id,
            predicted_class=case.expected_prediction,
        )
        for case in dataset.cases[:-1]
    )

    report = evaluate_unsupported_requirements(
        dataset=dataset,
        observations=observations,
        identity=evaluation_identity(),
    )

    assert report.result is UnsupportedRequirementsGateResult.NOT_VERIFIED
    assert report.not_verified_reasons == (
        "incomplete_execution",
        "incomplete_mandatory_stratum",
    )


@pytest.mark.asyncio
async def test_approved_dataset_passes_through_existing_deterministic_capability() -> (
    None
):
    report = await run_deterministic_unsupported_requirements_qualification(
        tested_commit_sha="a" * 40
    )

    assert report.result is UnsupportedRequirementsGateResult.PASS, report.model_dump()


@pytest.mark.asyncio
async def test_committed_qualification_report_is_exactly_reproducible() -> None:
    report_path = (
        Path(__file__).parents[2]
        / "docs"
        / "qualification"
        / "issue-799-unsupported-requirements.json"
    )
    expected = json.loads(report_path.read_text(encoding="utf-8"))

    report = await run_deterministic_unsupported_requirements_qualification(
        tested_commit_sha=expected["identity"]["tested_commit_sha"]
    )

    assert report.model_dump(mode="json") == expected
