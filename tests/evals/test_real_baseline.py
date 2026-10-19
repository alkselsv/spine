from __future__ import annotations

from spine.evals.real_baseline import (
    BaselineCaseInput,
    BaselineMetricInput,
    build_r1_baseline_report,
)


def test_baseline_reports_denominators_coverage_strata_and_unavailable_metrics() -> None:
    cases = (
        BaselineCaseInput(
            case_key="opaque-a",
            source_formats=("pdf",),
            expected_outcome="answered",
            scenario_tags=("single_page_fact",),
            observation_status="measured",
            metric_eligibility={"qa.answer_outcome": 1},
            metrics=(
                BaselineMetricInput(
                    metric="qa.answer_outcome",
                    version="v1",
                    value=1.0,
                    eligible_count=1,
                    measured_denominator=1,
                    matched_count=1,
                    failure_counts={"outcome_mismatch": 1},
                ),
            ),
        ),
        BaselineCaseInput(
            case_key="opaque-b",
            source_formats=("docx",),
            expected_outcome="abstained",
            scenario_tags=("absent_evidence",),
            observation_status="unavailable",
            metric_eligibility={
                "qa.answer_outcome": 1,
                "qa.abstention_quality": 1,
                "qa.denial_quality": 0,
            },
            metrics=(),
        ),
    )

    report = build_r1_baseline_report(
        cases=cases,
        observer_latencies_ms=(10, 30),
        worker_latencies_ns=(100,),
        observer_attempt_count=2,
    )

    answer_outcome = next(
        metric for metric in report.metrics if metric.metric == "qa.answer_outcome"
    )
    assert answer_outcome.eligible_count == 2
    assert answer_outcome.measured_denominator == 1
    assert answer_outcome.unavailable_count == 1
    assert answer_outcome.coverage_state == "partial"
    assert answer_outcome.value == 1.0

    abstention = next(
        metric
        for metric in report.metrics
        if metric.metric == "qa.abstention_quality"
    )
    assert abstention.eligible_count == 1
    assert abstention.measured_denominator == 0
    assert abstention.coverage_state == "unavailable"

    denial = next(
        metric for metric in report.metrics if metric.metric == "qa.denial_quality"
    )
    assert denial.applicability == "not_applicable"
    assert denial.eligible_count == 0
    assert denial.value is None

    pdf_slice = next(
        item for item in report.strata if item.dimension == "source_format" and item.value == "pdf"
    )
    assert pdf_slice.eligible_count == 1
    assert "opaque-a" not in report.model_dump_json()
    assert report.observer_latency.median == 20
    assert report.observer_latency.measurement == "observer_wall_clock"
    assert report.observer_latency.version == "r1-case-execution-record:v2"
    assert report.worker_latency.median == 100
    assert report.worker_latency.applicability == "unknown"
    assert report.worker_latency.coverage_state == "partial"
    assert report.observer_latency.applicability == "applicable"
    assert all(item.status == "unavailable" for item in report.operational_measurements)
    assert {
        item.unavailable_reason for item in report.operational_measurements
    } == {"no_benchmark_linked_population"}
    assert {item.code for item in report.failure_clusters} == {"outcome_mismatch"}
    assert report.limitations == (
        "no_pdf_abstention_coverage",
        "no_docx_abstention_coverage",
    )


def test_baseline_keeps_supplementary_citations_out_of_groundedness_denominator() -> None:
    report = build_r1_baseline_report(
        cases=(
            BaselineCaseInput(
                case_key="synthetic-answered-case",
                source_formats=("xlsx",),
                expected_outcome="answered",
                observation_status="measured",
                metric_eligibility={
                    "qa.citation_recall": 177,
                    "qa.groundedness": 158,
                },
                metrics=(
                    BaselineMetricInput(
                        metric="qa.citation_recall",
                        version="v1",
                        value=1.0,
                        eligible_count=177,
                        measured_denominator=177,
                        matched_count=177,
                    ),
                    BaselineMetricInput(
                        metric="qa.groundedness",
                        version="v1",
                        value=1.0,
                        eligible_count=158,
                        measured_denominator=158,
                        matched_count=158,
                    ),
                ),
            ),
        ),
        observer_latencies_ms=(),
        worker_latencies_ns=(),
        observer_attempt_count=0,
    )

    assert next(m for m in report.metrics if m.metric == "qa.citation_recall").eligible_count == 177
    assert next(m for m in report.metrics if m.metric == "qa.groundedness").eligible_count == 158


def test_zero_citation_precision_remains_a_measured_zero() -> None:
    report = build_r1_baseline_report(
        cases=(
            BaselineCaseInput(
                case_key="no-citations",
                source_formats=("pdf",),
                expected_outcome="answered",
                observation_status="measured",
                metric_eligibility={"qa.citation_precision": 1},
                metrics=(
                    BaselineMetricInput(
                        metric="qa.citation_precision",
                        version="v1",
                        value=0.0,
                        eligible_count=1,
                        measured_denominator=0,
                        matched_count=0,
                    ),
                ),
            ),
        ),
        observer_latencies_ms=(5,),
        worker_latencies_ns=(),
        observer_attempt_count=1,
    )

    precision = next(m for m in report.metrics if m.metric == "qa.citation_precision")
    assert precision.value == 0.0
    assert precision.eligible_count == 1
    assert precision.measured_denominator == 0
    assert precision.coverage_state == "complete"
