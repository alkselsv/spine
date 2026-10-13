from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from spine.domain.evaluations import EvaluationResult, EvaluationVerdict
from spine.evals.qa_r1 import QACase, QADataset, load_r1_dataset
from spine.evals.offline_runner import (
    DatasetIdentity,
    CaseEvaluationContext,
    MeasurementObservation,
    OfflineEvaluationRunner,
    UnavailableIntegrationError,
    run_r1_evaluation,
)
from spine.memory.contracts import RetrievalQuery, RetrievalResult


class FixedMeasurements:
    def observe(
        self,
        *,
        scenario_id: str,
        case: QACase,
        retrieval: RetrievalResult,
    ) -> MeasurementObservation:
        del case, retrieval
        if scenario_id == "allowed-acl":
            return MeasurementObservation(
                source="synthetic-clock:v1",
                latency_ms=12.5,
                cost=Decimal("0.0042"),
                cost_currency="USD",
            )
        return MeasurementObservation.unavailable(
            source="synthetic-clock:v1",
            latency_reason="not-observed",
            cost_reason="not-observed",
        )


class FailingEvaluator:
    def evaluate(self, *, context: CaseEvaluationContext) -> tuple[EvaluationResult, ...]:
        case = context.case
        if case.case_id == "allowed-acl":
            raise RuntimeError("synthetic evaluator failure")
        if case.case_id == "denied-acl":
            raise UnavailableIntegrationError("synthetic integration unavailable")
        return ()


class MismatchedVersionEvaluator:
    def evaluate(self, *, context: CaseEvaluationContext) -> tuple[EvaluationResult, ...]:
        case = context.case
        return (
            EvaluationResult(
                evaluator_key="qa.answer_outcome",
                evaluator_version="v999",
                subject_type="qa_case",
                subject_id=case.principal_id,
                workspace_id=case.workspace_id,
                environment_id=case.environment_id,
                verdict=EvaluationVerdict.PASS,
                score=1.0,
            ),
        )


class UnavailableMeasurements:
    def observe(
        self,
        *,
        scenario_id: str,
        case: QACase,
        retrieval: RetrievalResult,
    ) -> MeasurementObservation:
        del scenario_id, case, retrieval
        raise UnavailableIntegrationError("synthetic measurement unavailable")


class FailingMeasurements(UnavailableMeasurements):
    def observe(
        self,
        *,
        scenario_id: str,
        case: QACase,
        retrieval: RetrievalResult,
    ) -> MeasurementObservation:
        del scenario_id, case, retrieval
        raise RuntimeError("synthetic measurement failure")


class BrokenReplay:
    index_version = "qa-r1-replay:test"

    async def retrieve_case(self, case: QACase, query: RetrievalQuery) -> RetrievalResult:
        del query
        return RetrievalResult(retrieval_strategy=case.retrieval_strategy, index_version=self.index_version)


@pytest.mark.asyncio
async def test_r1_runner_produces_reproducible_golden_report() -> None:
    first = await run_r1_evaluation()
    second = await run_r1_evaluation()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.dataset == DatasetIdentity(
        dataset_id="qa-r1-synthetic",
        version="1.0.0",
        digest=load_r1_dataset().dataset_digest,
    )
    assert first.schema_version == "offline-evaluation-report:v1"
    assert first.runner_version == "v1"
    assert first.report_id == UUID("ad54606f-a579-52d0-b0bb-9fa72e915a67")
    assert first.qualification_scope == "offline-contract-only"
    assert len(first.scenarios) == 12
    assert len(first.evaluations) == 96
    assert len(first.measurements) == 12
    assert [scenario.scenario_id for scenario in first.scenarios] == [
        case.case_id for case in load_r1_dataset().cases
    ]
    assert all(scenario.status == "passed" for scenario in first.scenarios)
    assert all(isinstance(result, EvaluationResult) for result in first.evaluations)
    assert first.evaluator_catalog
    assert first.failures == ()
    assert first.unavailable_integrations == ()
    aggregates = {
        aggregate.evaluator_key: aggregate for aggregate in first.aggregates
    }
    assert aggregates["retrieval.recall_at_k"].measured_count == 7
    assert aggregates["retrieval.recall_at_k"].not_applicable_count == 5
    assert aggregates["retrieval.recall_at_k"].mean_score == pytest.approx(6 / 7)
    assert aggregates["qa.citation_recall"].review_count == 5

    assert {
        aggregate.evaluator_key for aggregate in first.aggregates
    } == {
        "qa.answer_outcome",
        "qa.citation_precision",
        "qa.citation_recall",
        "qa.citations",
        "qa.groundedness",
        "qa.locator_validity",
        "retrieval.mrr",
        "retrieval.recall_at_k",
    }


@pytest.mark.asyncio
async def test_r1_runner_records_injected_and_unavailable_measurements() -> None:
    report = await OfflineEvaluationRunner(
        measurement_source=FixedMeasurements(),
    ).run()

    observed = {item.scenario_id: item for item in report.measurements}
    assert observed["allowed-acl"].latency_ms == 12.5
    assert observed["allowed-acl"].cost == Decimal("0.0042")
    assert observed["allowed-acl"].cost_currency == "USD"
    assert observed["absent-evidence-abstention"].latency_ms is None
    assert observed["absent-evidence-abstention"].cost is None
    assert observed["absent-evidence-abstention"].latency_unavailable_reason == "not-observed"
    assert observed["absent-evidence-abstention"].cost_unavailable_reason == "not-observed"


@pytest.mark.asyncio
async def test_r1_runner_distinguishes_evaluator_failure_and_unavailable_integration() -> None:
    report = await OfflineEvaluationRunner(case_evaluator=FailingEvaluator()).run()

    failures = {failure.scenario_id: failure for failure in report.failures}
    assert failures["allowed-acl"].category == "evaluator_failure"
    assert failures["denied-acl"].category == "unavailable_integration"
    statuses = {scenario.scenario_id: scenario.status for scenario in report.scenarios}
    assert statuses["allowed-acl"] == "failed"
    assert statuses["denied-acl"] == "unavailable"
    assert report.unavailable_integrations == ("denied-acl",)


@pytest.mark.asyncio
async def test_r1_runner_rejects_dataset_identity_mismatch() -> None:
    expected = DatasetIdentity(
        dataset_id="qa-r1-synthetic",
        version="9.9.9",
        digest=load_r1_dataset().dataset_digest,
    )

    with pytest.raises(ValueError, match="dataset identity mismatch"):
        await OfflineEvaluationRunner(expected_dataset=expected).run()


@pytest.mark.asyncio
async def test_r1_runner_classifies_evaluator_version_mismatch() -> None:
    first = await OfflineEvaluationRunner(
        case_evaluator=MismatchedVersionEvaluator(),
    ).run()
    second = await OfflineEvaluationRunner(
        case_evaluator=MismatchedVersionEvaluator(),
    ).run()

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.failures[0].category == "evaluator_failure"
    assert first.failures[0].component == "evaluator-version"
    assert first.scenarios[0].status == "failed"
    assert first.evaluations[0].evaluator_version == "v999"


@pytest.mark.asyncio
async def test_r1_runner_records_unavailable_measurement_integration() -> None:
    report = await OfflineEvaluationRunner(
        measurement_source=UnavailableMeasurements(),
    ).run()

    assert report.scenarios[0].status == "passed"
    assert report.measurements[0].latency_unavailable_reason == "integration-unavailable"
    assert report.failures[0].category == "unavailable_integration"
    assert report.failures[0].component == "measurement"
    assert "allowed-acl" in report.unavailable_integrations


@pytest.mark.asyncio
async def test_r1_runner_classifies_unexpected_measurement_failure() -> None:
    report = await OfflineEvaluationRunner(
        measurement_source=FailingMeasurements(),
    ).run()

    assert report.scenarios[0].status == "passed"
    assert report.measurements[0].latency_unavailable_reason == "measurement-failure"
    assert report.failures[0].category == "unavailable_integration"
    assert report.failures[0].error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_r1_runner_classifies_retrieval_scenario_failure() -> None:
    report = await OfflineEvaluationRunner(replay=BrokenReplay()).run()

    assert report.scenarios[0].status == "failed"
    assert report.failures[0].category == "scenario_failure"
    assert report.failures[0].component == "answer-construction"


def test_evaluation_results_keep_not_applicable_metrics_explicit() -> None:
    report = OfflineEvaluationRunner().run_sync()
    not_applicable = [
        result
        for result in report.evaluations
        if result.score is None and result.evaluator_key.startswith("retrieval.")
    ]

    assert not_applicable
    assert all(result.verdict is EvaluationVerdict.REVIEW for result in not_applicable)
    assert all(result.details.get("status") == "not_applicable" for result in not_applicable)
