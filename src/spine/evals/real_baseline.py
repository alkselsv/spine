"""Metadata-only coverage and strata for the opt-in real R1 benchmark."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Literal

from pydantic import Field, model_validator

from spine.domain.common import DefinitionModel
from spine.evals.real_qa_r1 import REAL_R1_DATASET_DIGEST, REAL_R1_DATASET_VERSION


class BaselineMetricInput(DefinitionModel):
    """One evaluator metric reduced to a case-local numeric observation."""

    metric: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    value: float | None = Field(default=None, ge=0, le=1)
    eligible_count: int = Field(ge=0)
    measured_denominator: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    failure_counts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_denominators(self) -> "BaselineMetricInput":
        if self.matched_count > self.measured_denominator:
            raise ValueError("matched count cannot exceed measured denominator")
        return self


class BaselineCaseInput(DefinitionModel):
    """Private case metadata consumed by aggregation and never serialized."""

    case_key: str = Field(min_length=1, max_length=256)
    source_formats: tuple[
        Literal["pdf", "docx", "xlsx", "txt", "markdown"], ...
    ] = Field(min_length=1)
    expected_outcome: Literal["answered", "abstained", "denied"]
    scenario_tags: tuple[str, ...] = ()
    observation_status: Literal["measured", "partial", "unavailable"]
    metric_eligibility: dict[str, int] = Field(default_factory=dict)
    metrics: tuple[BaselineMetricInput, ...] = ()

    @model_validator(mode="after")
    def validate_case_inputs(self) -> "BaselineCaseInput":
        if any(value < 0 for value in self.metric_eligibility.values()):
            raise ValueError("metric eligibility cannot be negative")
        if len(self.scenario_tags) != len(set(self.scenario_tags)):
            raise ValueError("scenario tags must be unique")
        if len({item.metric for item in self.metrics}) != len(self.metrics):
            raise ValueError("case metrics must be unique")
        return self


class BaselineMetricAggregate(DefinitionModel):
    metric: str
    version: str
    statistic: Literal["weighted_mean"] = "weighted_mean"
    value: float | None = Field(default=None, ge=0, le=1)
    eligible_count: int = Field(ge=0)
    measured_denominator: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    applicability: Literal["applicable", "not_applicable"]
    coverage_state: Literal["complete", "partial", "unavailable", "not_applicable"]
    matched_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_coverage(self) -> "BaselineMetricAggregate":
        if self.unavailable_count > self.eligible_count:
            raise ValueError("unavailable count cannot exceed metric eligibility")
        if self.matched_count > self.measured_denominator:
            raise ValueError("matched count cannot exceed measured denominator")
        if self.applicability == "not_applicable":
            if (
                self.eligible_count
                or self.value is not None
                or self.coverage_state != "not_applicable"
            ):
                raise ValueError("not-applicable metric cannot carry measurements")
        elif self.eligible_count == 0 or self.coverage_state == "not_applicable":
            raise ValueError("applicable metric requires an eligible population")
        return self


class BaselineStratum(DefinitionModel):
    dimension: Literal["source_format", "expected_outcome", "scenario_tag"]
    value: str
    eligible_count: int = Field(ge=0)
    measured_count: int = Field(ge=0)
    unavailable_count: int = Field(ge=0)
    coverage_state: Literal["complete", "partial", "unavailable"]


class BaselineLatency(DefinitionModel):
    measurement: Literal["observer_wall_clock", "worker_execution"]
    version: Literal["r1-case-execution-record:v2"] = "r1-case-execution-record:v2"
    unit: Literal["milliseconds", "nanoseconds"]
    statistic: Literal["median", "p95"] = "median"
    median: float | None = Field(default=None, ge=0)
    p95: float | None = Field(default=None, ge=0)
    measured_denominator: int = Field(ge=0)
    unavailable_count: int | None = Field(default=None, ge=0)
    applicability: Literal["applicable", "not_applicable", "unknown"]
    coverage_state: Literal["complete", "partial", "unavailable"]


class BaselineFailureCluster(DefinitionModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    count: int = Field(gt=0)


class BaselineMetricDelta(DefinitionModel):
    metric: str
    version: str
    previous_value: float = Field(ge=0, le=1)
    current_value: float = Field(ge=0, le=1)
    delta: float


class BaselineOperationalMeasurement(DefinitionModel):
    name: Literal["cost", "indexing_lag", "freshness", "usefulness"]
    status: Literal["measured", "unavailable"]
    value: float | None = Field(default=None, ge=0)
    applicability: Literal["applicable", "not_applicable", "unknown"]
    eligible_count: int | None = Field(default=None, ge=0)
    measured_denominator: int | None = Field(default=None, ge=0)
    unavailable_count: int | None = Field(default=None, ge=0)
    coverage_state: Literal["complete", "partial", "unavailable"]
    unavailable_reason: Literal[
        "no_benchmark_linked_population", "incomplete_population"
    ] | None = None

    @model_validator(mode="after")
    def validate_unavailable(self) -> "BaselineOperationalMeasurement":
        if self.status == "unavailable" and self.value is not None:
            raise ValueError("unavailable operational measurement cannot contain a value")
        if self.status == "unavailable" and self.unavailable_reason is None:
            raise ValueError("unavailable operational measurement requires a reason")
        if self.status == "measured" and self.unavailable_reason is not None:
            raise ValueError("measured operational measurement cannot have an unavailable reason")
        return self


class R1BaselineReport(DefinitionModel):
    schema_version: Literal["r1-baseline-aggregates:v1"] = "r1-baseline-aggregates:v1"
    metric_version: Literal["r1-baseline-aggregation:v1"] = "r1-baseline-aggregation:v1"
    dataset_version: Literal[REAL_R1_DATASET_VERSION] = REAL_R1_DATASET_VERSION
    dataset_sha256: Literal[REAL_R1_DATASET_DIGEST] = REAL_R1_DATASET_DIGEST
    baseline_status: Literal["completed", "partial", "unavailable"]
    eligible_case_count: int = Field(ge=0)
    measured_case_count: int = Field(ge=0)
    unavailable_case_count: int = Field(ge=0)
    metrics: tuple[BaselineMetricAggregate, ...]
    delta_status: Literal["no_comparable_prior_real_run", "comparable"] = (
        "no_comparable_prior_real_run"
    )
    metric_deltas: tuple[BaselineMetricDelta, ...] = ()
    strata: tuple[BaselineStratum, ...]
    limitations: tuple[str, ...] = ()
    failure_clusters: tuple[BaselineFailureCluster, ...]
    observer_latency: BaselineLatency
    worker_latency: BaselineLatency
    operational_measurements: tuple[BaselineOperationalMeasurement, ...]

    @model_validator(mode="after")
    def validate_case_coverage(self) -> "R1BaselineReport":
        if (
            self.measured_case_count + self.unavailable_case_count
            != self.eligible_case_count
        ):
            raise ValueError("case coverage counts must sum to eligibility")
        if self.delta_status == "no_comparable_prior_real_run" and self.metric_deltas:
            raise ValueError("metric deltas require a comparable prior real run")
        return self


def build_r1_baseline_report(
    *,
    cases: tuple[BaselineCaseInput, ...],
    observer_latencies_ms: tuple[int, ...],
    worker_latencies_ns: tuple[int, ...],
    observer_attempt_count: int,
    failure_counts: dict[str, int] | None = None,
) -> R1BaselineReport:
    """Aggregate existing evaluator results without retaining case identities."""
    if observer_attempt_count < 0:
        raise ValueError("observer attempt count cannot be negative")
    if observer_attempt_count < len(observer_latencies_ms):
        raise ValueError("observer timing samples exceed observer attempts")
    if len({case.case_key for case in cases}) != len(cases):
        raise ValueError("baseline case identities must be unique")

    grouped: dict[str, list[BaselineMetricInput]] = defaultdict(list)
    eligible_by_metric: Counter[str] = Counter()
    versions: dict[str, str] = {}
    failures: Counter[str] = Counter()
    failures.update(failure_counts or {})
    for case in cases:
        for key, count in case.metric_eligibility.items():
            eligible_by_metric[key] += count
        case_failures: Counter[str] = Counter()
        for metric in case.metrics:
            previous_version = versions.setdefault(metric.metric, metric.version)
            if previous_version != metric.version:
                raise ValueError("a baseline metric cannot mix evaluator versions")
            grouped[metric.metric].append(metric)
            for code, count in metric.failure_counts.items():
                case_failures[code] = max(case_failures[code], count)
        failures.update(case_failures)

    metric_names = set(eligible_by_metric) | set(grouped)
    aggregates: list[BaselineMetricAggregate] = []
    for name in sorted(metric_names):
        eligible = eligible_by_metric[name]
        observed = grouped.get(name, [])
        score_denominator = sum(item.measured_denominator for item in observed)
        matched = sum(item.matched_count for item in observed)
        # Some metrics use a different denominator than case eligibility (for
        # example citation precision). Coverage still counts eligible cases.
        measured_count = sum(item.eligible_count for item in observed)
        unavailable = max(0, eligible - measured_count)
        applicability: Literal["applicable", "not_applicable"] = (
            "applicable" if eligible else "not_applicable"
        )
        state: Literal["complete", "partial", "unavailable", "not_applicable"]
        if not eligible:
            state = "not_applicable"
        elif measured_count == 0:
            state = "unavailable"
        elif unavailable:
            state = "partial"
        else:
            state = "complete"
        value = (
            sum(
                item.value * item.measured_denominator
                for item in observed
                if item.value is not None
            )
            / score_denominator
            if score_denominator
            # Some existing evaluator metrics deliberately assign a score
            # with a zero item denominator (citation precision with no
            # generated citations is a measured zero). Preserve that score
            # without changing the evaluator's denominator semantics.
            else (
                sum(item.value for item in observed if item.value is not None)
                / sum(item.value is not None for item in observed)
                if any(item.value is not None for item in observed)
                else None
            )
        )
        aggregates.append(
            BaselineMetricAggregate(
                metric=name,
                version=versions.get(name, "v1"),
                value=value,
                eligible_count=eligible,
                measured_denominator=score_denominator,
                unavailable_count=unavailable,
                applicability=applicability,
                coverage_state=state,
                matched_count=matched,
            )
        )

    strata: list[BaselineStratum] = []
    dimension_values: dict[tuple[str, str], list[BaselineCaseInput]] = defaultdict(list)
    for case in cases:
        for source_format in set(case.source_formats):
            dimension_values[("source_format", source_format)].append(case)
        dimension_values[("expected_outcome", case.expected_outcome)].append(case)
        for tag in set(case.scenario_tags):
            dimension_values[("scenario_tag", tag)].append(case)
    for (dimension, value), members in sorted(dimension_values.items()):
        measured = sum(member.observation_status == "measured" for member in members)
        unavailable = len(members) - measured
        state = (
            "unavailable"
            if not measured
            else "partial"
            if unavailable
            else "complete"
        )
        strata.append(
            BaselineStratum(
                dimension=dimension,
                value=value,
                eligible_count=len(members),
                measured_count=measured,
                unavailable_count=unavailable,
                coverage_state=state,
            )
        )

    measured_cases = sum(case.observation_status == "measured" for case in cases)
    return R1BaselineReport(
        dataset_version=REAL_R1_DATASET_VERSION,
        dataset_sha256=REAL_R1_DATASET_DIGEST,
        baseline_status=(
            "unavailable"
            if not measured_cases
            else "partial"
            if measured_cases != len(cases)
            else "completed"
        ),
        eligible_case_count=len(cases),
        measured_case_count=measured_cases,
        unavailable_case_count=len(cases) - measured_cases,
        metrics=tuple(aggregates),
        strata=tuple(strata),
        failure_clusters=tuple(
            BaselineFailureCluster(code=code, count=count)
            for code, count in sorted(failures.items())
            if count
        ),
        observer_latency=_latency(
            observer_latencies_ms,
            observer_attempt_count,
            "observer_wall_clock",
            "milliseconds",
            applicability="applicable" if observer_attempt_count else "not_applicable",
        ),
        worker_latency=_latency(
            worker_latencies_ns,
            None,
            "worker_execution",
            "nanoseconds",
            applicability="unknown",
        ),
        operational_measurements=tuple(
            BaselineOperationalMeasurement(
                name=name, status="unavailable", value=None, eligible_count=None,
                applicability="unknown", measured_denominator=None,
                unavailable_count=None, coverage_state="unavailable",
                unavailable_reason="no_benchmark_linked_population",
            )
            for name in ("cost", "indexing_lag", "freshness", "usefulness")
        ),
        limitations=tuple(
            f"no_{source_format}_abstention_coverage"
            for source_format in ("pdf", "docx")
            if not any(
                case.expected_outcome == "abstained"
                and source_format in case.source_formats
                and case.observation_status == "measured"
                for case in cases
            )
        ),
    )


def _latency(
    values: tuple[int, ...],
    attempts: int | None,
    measurement: Literal["observer_wall_clock", "worker_execution"],
    unit: Literal["milliseconds", "nanoseconds"],
    *,
    applicability: Literal["applicable", "not_applicable", "unknown"],
) -> BaselineLatency:
    ordered = sorted(values)
    measured = len(ordered)
    unavailable = max(0, attempts - measured) if attempts is not None else None
    state = (
        "unavailable"
        if not measured
        else "partial"
        if attempts is None or unavailable
        else "complete"
    )
    if not ordered:
        return BaselineLatency(
            measurement=measurement,
            unit=unit,
            median=None,
            p95=None,
            measured_denominator=0,
            unavailable_count=unavailable, coverage_state=state,
            applicability=applicability,
        )
    middle = measured // 2
    median = (
        ordered[middle]
        if measured % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    p95 = ordered[max(0, (95 * measured + 99) // 100 - 1)]
    return BaselineLatency(
        measurement=measurement,
        unit=unit,
        median=median,
        p95=p95,
        measured_denominator=measured,
        unavailable_count=unavailable, coverage_state=state,
        applicability=applicability,
    )


__all__ = [
    "BaselineCaseInput",
    "BaselineMetricAggregate",
    "BaselineMetricInput",
    "BaselineOperationalMeasurement",
    "R1BaselineReport",
    "build_r1_baseline_report",
]
