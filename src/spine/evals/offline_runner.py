"""Reproducible offline-contract execution, not production qualification."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from spine.domain.common import ActorKind, ActorRef, DefinitionModel
from spine.domain.evaluations import EvaluationResult, EvaluationVerdict
from spine.evals.production_contract import (
    PRODUCTION_CONTRACT_INDEX_VERSION,
    PRODUCTION_CONTRACT_OBSERVATION_SEAM,
    ProductionContractCaseRequest,
    ProductionContractObservation,
    ProductionContractObservationError,
)
from spine.evals.qa_r1 import QACase, QADataset, load_r1_dataset
from spine.evals.retrieval_replay import DeterministicRetrievalReplay
from spine.evaluations.grounded_answer_safety import (
    GroundedAnswerSafetyGate,
    GroundedAnswerSafetyResult,
)
from spine.evals.unauthorized_retrieval import (
    ProductionContractSafetyGate,
    ProductionContractSafetyResult,
)
from spine.evaluations.citation_metrics import (
    AnswerClaim,
    AnswerSubmission,
    Citation,
    ContextBundle,
    CITATION_METRICS_EVALUATOR_VERSION,
    evaluate_answer_outcome,
    evaluate_citation_precision,
    evaluate_citation_recall,
    evaluate_citations,
    evaluate_groundedness,
    evaluate_locator_validity,
)
from spine.evaluations.retrieval_metrics import (
    RETRIEVAL_METRICS_EVALUATOR_VERSION,
    evaluate_mrr,
    evaluate_recall_at_k,
)
from spine.memory.contracts import RetrievalQuery, RetrievalResult


OFFLINE_REPORT_SCHEMA_VERSION: Final[str] = "offline-evaluation-report:v1"
OFFLINE_RUNNER_VERSION: Final[str] = "v1"
ANSWER_CONFIGURATION_VERSION: Final[str] = "qa-r1-expected-answer:v1"
SCENARIO_CONFIGURATION_VERSION: Final[str] = "qa-r1-regression:v1"
REPLAY_CONTEXT_PROFILE: Final[str] = "qa-r1-replay:v1"
QUALIFICATION_SCOPE: Final[str] = "offline-contract-only"
_REPORT_NAMESPACE: Final[UUID] = UUID("a5d7ce20-5ed1-4a7a-9c1e-7f1a7ab2bd78")
ExecutionMode = Literal["synthetic-replay", "production-contract"]

FailureCategory = Literal[
    "evaluator_failure",
    "scenario_failure",
    "unavailable_integration",
]
ScenarioStatus = Literal["passed", "failed", "unavailable"]


class DatasetIdentity(DefinitionModel):
    """Immutable identity of the dataset used by one evaluation run."""

    dataset_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    digest: str = Field(min_length=64, max_length=64)


class EvaluatorIdentity(DefinitionModel):
    evaluator_key: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)


class EvaluationConfiguration(DefinitionModel):
    """Version pins that make a report interpretable and replayable."""

    retrieval_index_version: str = Field(min_length=1, max_length=128)
    retrieval_strategy_versions: tuple[str, ...] = ()
    retrieval_evaluator_version: str = RETRIEVAL_METRICS_EVALUATOR_VERSION
    answer_configuration_version: str = ANSWER_CONFIGURATION_VERSION
    citation_evaluator_version: str = CITATION_METRICS_EVALUATOR_VERSION
    scenario_configuration_version: str = SCENARIO_CONFIGURATION_VERSION
    recall_k_values: tuple[int, ...] = (1,)


class MeasurementObservation(DefinitionModel):
    """Observed or explicitly unavailable latency and cost for one scenario."""

    scenario_id: str | None = Field(default=None, min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    latency_ms: float | None = Field(default=None, ge=0.0)
    cost: Decimal | None = Field(default=None, ge=Decimal("0"))
    cost_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    latency_unavailable_reason: str | None = Field(default=None, max_length=128)
    cost_unavailable_reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_availability(self) -> "MeasurementObservation":
        if self.latency_ms is None and not self.latency_unavailable_reason:
            raise ValueError("missing latency must have an explicit reason")
        if self.latency_ms is not None and self.latency_unavailable_reason is not None:
            raise ValueError("measured latency cannot have an unavailable reason")
        if self.cost is None and not self.cost_unavailable_reason:
            raise ValueError("missing cost must have an explicit reason")
        if self.cost is not None and self.cost_unavailable_reason is not None:
            raise ValueError("measured cost cannot have an unavailable reason")
        if self.cost is None and self.cost_currency is not None:
            raise ValueError("unavailable cost cannot have a currency")
        if self.cost is not None and self.cost_currency is None:
            raise ValueError("measured cost requires an explicit currency")
        return self

    @classmethod
    def unavailable(
        cls,
        *,
        scenario_id: str | None = None,
        source: str,
        latency_reason: str,
        cost_reason: str,
    ) -> "MeasurementObservation":
        return cls(
            scenario_id=scenario_id,
            source=source,
            latency_unavailable_reason=latency_reason,
            cost_unavailable_reason=cost_reason,
        )


class ScenarioResult(DefinitionModel):
    scenario_id: str = Field(min_length=1, max_length=128)
    case_id: str = Field(min_length=1, max_length=128)
    scenario_tags: tuple[str, ...] = ()
    expected_outcome: str
    observed_outcome: str | None = None
    status: ScenarioStatus
    evaluation_ids: tuple[UUID, ...] = ()
    expected_evidence_ids: tuple[str, ...] = ()
    observed_evidence_ids: tuple[str, ...] = ()


class FailureRecord(DefinitionModel):
    scenario_id: str = Field(min_length=1, max_length=128)
    category: FailureCategory
    component: str = Field(min_length=1, max_length=128)
    error_type: str = Field(min_length=1, max_length=128)


class MetricAggregate(DefinitionModel):
    evaluator_key: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    measured_count: int = Field(ge=0)
    not_applicable_count: int = Field(ge=0)
    pass_count: int = Field(ge=0)
    fail_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    mean_score: float | None = Field(default=None, ge=0.0, le=1.0)


class OfflineEvaluationReport(DefinitionModel):
    """Complete deterministic output of one synthetic offline run."""

    report_id: UUID
    schema_version: Literal[OFFLINE_REPORT_SCHEMA_VERSION] = OFFLINE_REPORT_SCHEMA_VERSION
    runner_version: Literal[OFFLINE_RUNNER_VERSION] = OFFLINE_RUNNER_VERSION
    qualification_scope: Literal[QUALIFICATION_SCOPE] = QUALIFICATION_SCOPE
    execution_mode: ExecutionMode = Field(
        default="synthetic-replay", exclude_if=lambda value: value == "synthetic-replay"
    )
    observation_seam: str | None = Field(default=None, exclude_if=lambda value: value is None)
    dataset: DatasetIdentity
    configuration: EvaluationConfiguration
    evaluator_catalog: tuple[EvaluatorIdentity, ...] = ()
    scenarios: tuple[ScenarioResult, ...] = ()
    measurements: tuple[MeasurementObservation, ...] = ()
    evaluations: tuple[EvaluationResult, ...] = ()
    aggregates: tuple[MetricAggregate, ...] = ()
    failures: tuple[FailureRecord, ...] = ()
    safety_results: tuple[ProductionContractSafetyResult, ...] = ()
    grounded_answer_safety_results: tuple[GroundedAnswerSafetyResult, ...] = Field(
        default=(), exclude_if=lambda value: value == ()
    )
    unavailable_integrations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseEvaluationContext:
    """Typed handoff shared by the case evaluator and its runner."""

    case: QACase
    dataset: QADataset
    retrieval: RetrievalResult
    answer: AnswerSubmission
    context_bundle: ContextBundle


class MeasurementSource(Protocol):
    def observe(
        self,
        *,
        scenario_id: str,
        case: QACase,
        retrieval: RetrievalResult,
    ) -> MeasurementObservation:
        """Return real/injected observations or explicit unavailable values."""


class CaseEvaluator(Protocol):
    def evaluate(
        self,
        *,
        context: CaseEvaluationContext,
    ) -> tuple[EvaluationResult, ...]:
        """Evaluate one case without accessing a network or model provider."""


class ProductionContractObservationPort(Protocol):
    """Execute one case through typed application contracts."""

    index_version: str

    async def observe(
        self, *, request: ProductionContractCaseRequest
    ) -> ProductionContractObservation: ...


class UnavailableIntegrationError(RuntimeError):
    """Raised by an injected adapter when a non-offline integration is absent."""


class _ScenarioFailure(RuntimeError):
    """Internal marker for a failed deterministic scenario assertion."""


class _EvaluatorVersionMismatch(RuntimeError):
    """Internal marker for an evaluator result outside the pinned contract."""


class _UnavailableMeasurementSource:
    def observe(
        self,
        *,
        scenario_id: str,
        case: QACase,
        retrieval: RetrievalResult,
    ) -> MeasurementObservation:
        del scenario_id, case, retrieval
        return MeasurementObservation.unavailable(
            source="not-provided",
            latency_reason="not-provided",
            cost_reason="not-provided",
        )


class _DefaultCaseEvaluator:
    def __init__(self, *, recall_k_values: tuple[int, ...]) -> None:
        self._recall_k_values = recall_k_values

    def evaluate(
        self,
        *,
        context: CaseEvaluationContext,
    ) -> tuple[EvaluationResult, ...]:
        case = context.case
        dataset = context.dataset
        retrieval = context.retrieval
        answer = context.answer
        context_bundle = context.context_bundle
        results: list[EvaluationResult] = []
        for k in self._recall_k_values:
            results.append(
                evaluate_recall_at_k(
                    retrieval,
                    case,
                    k=k,
                    dataset=dataset,
                )
            )
        results.append(evaluate_mrr(retrieval, case, dataset=dataset))
        results.extend(
            (
                evaluate_answer_outcome(
                    answer,
                    case,
                    dataset=dataset,
                    context_bundle=context_bundle,
                ),
                evaluate_citations(
                    answer,
                    case,
                    dataset=dataset,
                    context_bundle=context_bundle,
                ),
                evaluate_citation_precision(
                    answer,
                    case,
                    dataset=dataset,
                    context_bundle=context_bundle,
                ),
                evaluate_citation_recall(
                    answer,
                    case,
                    dataset=dataset,
                    context_bundle=context_bundle,
                ),
                evaluate_locator_validity(
                    answer,
                    case,
                    dataset=dataset,
                    context_bundle=context_bundle,
                ),
                evaluate_groundedness(
                    answer,
                    case,
                    dataset=dataset,
                    context_bundle=context_bundle,
                ),
            )
        )
        return tuple(results)


class OfflineEvaluationRunner:
    """Execute the fixed R1 replay and evaluators into one audit report."""

    def __init__(
        self,
        *,
        dataset: QADataset | None = None,
        replay: DeterministicRetrievalReplay | None = None,
        measurement_source: MeasurementSource | None = None,
        case_evaluator: CaseEvaluator | None = None,
        expected_dataset: DatasetIdentity | None = None,
        recall_k_values: tuple[int, ...] = (1,),
        execution_mode: ExecutionMode = "synthetic-replay",
        production_observer: ProductionContractObservationPort | None = None,
    ) -> None:
        self.dataset = load_r1_dataset() if dataset is None else dataset
        self.expected_dataset = expected_dataset
        self.replay = replay or DeterministicRetrievalReplay(self.dataset)
        self.measurement_source = measurement_source or _UnavailableMeasurementSource()
        self.case_evaluator = case_evaluator or _DefaultCaseEvaluator(
            recall_k_values=recall_k_values
        )
        if execution_mode not in {"synthetic-replay", "production-contract"}:
            raise ValueError("execution_mode is not supported")
        if execution_mode == "production-contract" and production_observer is None:
            raise ValueError("production-contract mode requires a production observer")
        self.execution_mode = execution_mode
        self.production_observer = production_observer
        self.safety_gate = ProductionContractSafetyGate()
        self.grounded_answer_safety_gate = GroundedAnswerSafetyGate()
        if not recall_k_values or any(k < 1 for k in recall_k_values):
            raise ValueError("recall_k_values must contain positive integers")
        self.recall_k_values = tuple(dict.fromkeys(recall_k_values))

    async def run(self) -> OfflineEvaluationReport:
        dataset_identity = DatasetIdentity(
            dataset_id=self.dataset.dataset_id,
            version=self.dataset.version,
            digest=self.dataset.dataset_digest,
        )
        if self.expected_dataset is not None and self.expected_dataset != dataset_identity:
            raise ValueError("dataset identity mismatch")

        evaluations: list[EvaluationResult] = []
        scenarios: list[ScenarioResult] = []
        measurements: list[MeasurementObservation] = []
        failures: list[FailureRecord] = []
        safety_results: list[ProductionContractSafetyResult] = []
        grounded_answer_safety_results: list[GroundedAnswerSafetyResult] = []

        for case in self.dataset.cases:
            (
                scenario,
                case_evaluations,
                measurement,
                case_failures,
                case_safety_result,
                case_grounded_answer_safety_result,
            ) = await self._run_case(case)
            scenarios.append(scenario)
            evaluations.extend(case_evaluations)
            measurements.append(measurement)
            failures.extend(case_failures)
            if case_safety_result is not None:
                safety_results.append(case_safety_result)
            if case_grounded_answer_safety_result is not None:
                grounded_answer_safety_results.append(case_grounded_answer_safety_result)

        evaluations_tuple = tuple(evaluations)
        configuration = EvaluationConfiguration(
            retrieval_index_version=self._index_version(),
            retrieval_strategy_versions=tuple(
                sorted({case.retrieval_strategy for case in self.dataset.cases})
            ),
            recall_k_values=self.recall_k_values,
        )
        report_without_id = {
            "schema_version": OFFLINE_REPORT_SCHEMA_VERSION,
            "runner_version": OFFLINE_RUNNER_VERSION,
            "qualification_scope": QUALIFICATION_SCOPE,
            "dataset": dataset_identity.model_dump(mode="json"),
            "configuration": configuration.model_dump(mode="json"),
            "evaluator_catalog": _evaluator_catalog(evaluations_tuple),
            "scenarios": [scenario.model_dump(mode="json") for scenario in scenarios],
            "measurements": [measurement.model_dump(mode="json") for measurement in measurements],
            "evaluations": [result.model_dump(mode="json") for result in evaluations_tuple],
            "aggregates": _aggregates(evaluations_tuple),
            "failures": [failure.model_dump(mode="json") for failure in failures],
            "unavailable_integrations": tuple(
                sorted(
                    failure.scenario_id
                    for failure in failures
                    if failure.category == "unavailable_integration"
                )
            ),
        }
        if self.execution_mode == "production-contract":
            report_without_id["safety_results"] = [
                result.model_dump(mode="json") for result in safety_results
            ]
            report_without_id["grounded_answer_safety_results"] = [
                result.model_dump(mode="json")
                for result in grounded_answer_safety_results
            ]
            report_without_id["execution_mode"] = self.execution_mode
            report_without_id["observation_seam"] = PRODUCTION_CONTRACT_OBSERVATION_SEAM
        report_id = uuid5(
            _REPORT_NAMESPACE,
            json.dumps(report_without_id, ensure_ascii=False, sort_keys=True, default=str),
        )
        return OfflineEvaluationReport(
            report_id=report_id,
            execution_mode=self.execution_mode,
            observation_seam=(
                PRODUCTION_CONTRACT_OBSERVATION_SEAM
                if self.execution_mode == "production-contract"
                else None
            ),
            dataset=dataset_identity,
            configuration=configuration,
            evaluator_catalog=tuple(
                EvaluatorIdentity.model_validate(item)
                for item in report_without_id["evaluator_catalog"]
            ),
            scenarios=tuple(scenarios),
            measurements=tuple(measurements),
            evaluations=evaluations_tuple,
            aggregates=tuple(
                MetricAggregate.model_validate(item)
                for item in report_without_id["aggregates"]
            ),
            failures=tuple(failures),
            safety_results=tuple(safety_results),
            grounded_answer_safety_results=tuple(grounded_answer_safety_results),
            unavailable_integrations=tuple(report_without_id["unavailable_integrations"]),
        )

    def run_sync(self) -> OfflineEvaluationReport:
        return asyncio.run(self.run())

    async def _run_case(
        self,
        case: QACase,
    ) -> tuple[
        ScenarioResult,
        tuple[EvaluationResult, ...],
        MeasurementObservation,
        tuple[FailureRecord, ...],
        ProductionContractSafetyResult | None,
        GroundedAnswerSafetyResult | None,
    ]:
        observed_answer: AnswerSubmission | None = None
        safety_result: ProductionContractSafetyResult | None = None
        grounded_answer_safety_result: GroundedAnswerSafetyResult | None = None
        if self.execution_mode == "production-contract":
            assert self.production_observer is not None
            request = ProductionContractCaseRequest.from_case(case)
            try:
                observation = await self.production_observer.observe(
                    request=request
                )
                if not isinstance(observation, ProductionContractObservation):
                    raise ProductionContractObservationError(
                        "production observer returned an invalid observation"
                    )
                observation.validate()
                expected_index_version = getattr(
                    self.production_observer, "index_version", None
                )
                if (
                    not expected_index_version
                    or observation.retrieval.index_version != expected_index_version
                ):
                    raise ProductionContractObservationError(
                        "observed retrieval index version is not pinned"
                    )
                safety_result = self.safety_gate.evaluate(
                    request=request, observation=observation
                )
                if safety_result.status == "failed":
                    return self._failed_case(
                        case,
                        "scenario_failure",
                        "unauthorized-retrieval-disclosure",
                        ProductionContractObservationError(
                            "safety gate rejected observation"
                        ),
                        safety_result=safety_result,
                        grounded_answer_safety_result=(
                            self.grounded_answer_safety_gate.failed_without_observation(
                                case.case_id
                            )
                        ),
                    )
                retrieval = observation.retrieval
                observed_answer = _answer_submission_from_output(
                    observation.answer,
                    case=case,
                )
                context_bundle = _evaluation_bundle_from_domain(
                    observation.context_bundle, case
                )
                grounded_answer_safety_result = self.grounded_answer_safety_gate.evaluate(
                    case=case,
                    dataset=self.dataset,
                    answer=observed_answer,
                    context_bundle=context_bundle,
                )
            except UnavailableIntegrationError as error:
                safety_result = self.safety_gate.failed_without_observation(case.case_id)
                grounded_answer_safety_result = (
                    self.grounded_answer_safety_gate.failed_without_observation(case.case_id)
                )
                return self._failed_case(
                    case,
                    "unavailable_integration",
                    "production-contract-observation",
                    error,
                    safety_result=safety_result,
                    grounded_answer_safety_result=grounded_answer_safety_result,
                )
            except Exception as error:
                safety_result = self.safety_gate.failed_without_observation(case.case_id)
                grounded_answer_safety_result = (
                    self.grounded_answer_safety_gate.failed_without_observation(case.case_id)
                )
                return self._failed_case(
                    case,
                    "scenario_failure",
                    "production-contract-observation",
                    error,
                    safety_result=safety_result,
                    grounded_answer_safety_result=grounded_answer_safety_result,
                )
        else:
            try:
                retrieval = await self.replay.retrieve_case(case, _query(case))
            except UnavailableIntegrationError as error:
                return self._failed_case(case, "unavailable_integration", "retrieval", error)
            except Exception as error:  # pragma: no cover - defensive adapter boundary
                return self._failed_case(case, "scenario_failure", "retrieval", error)

        measurement_failure: FailureRecord | None = None
        try:
            measurement = self.measurement_source.observe(
                scenario_id=case.case_id,
                case=case,
                retrieval=retrieval,
            )
        except UnavailableIntegrationError as error:
            measurement = MeasurementObservation.unavailable(
                source="integration-unavailable",
                latency_reason="integration-unavailable",
                cost_reason="integration-unavailable",
            )
            measurement_failure = FailureRecord(
                scenario_id=case.case_id,
                category="unavailable_integration",
                component="measurement",
                error_type=type(error).__name__,
            )
        except Exception as error:
            measurement = MeasurementObservation.unavailable(
                source="measurement-failure",
                latency_reason="measurement-failure",
                cost_reason="measurement-failure",
            )
            measurement_failure = FailureRecord(
                scenario_id=case.case_id,
                category="unavailable_integration",
                component="measurement",
                error_type=type(error).__name__,
            )
        measurement_data = measurement.model_dump()
        measurement_data["scenario_id"] = case.case_id
        measurement = MeasurementObservation.model_validate(measurement_data)

        if self.execution_mode == "synthetic-replay":
            try:
                answer, context_bundle = _expected_answer(case, retrieval, self.dataset)
            except _ScenarioFailure as error:
                return self._failed_case(
                    case,
                    "scenario_failure",
                    "answer-construction",
                    error,
                    measurement=measurement,
                    retrieval=retrieval,
                    additional_failures=(measurement_failure,) if measurement_failure else (),
                    safety_result=safety_result,
                    grounded_answer_safety_result=grounded_answer_safety_result,
                )
        else:
            assert observed_answer is not None
            answer = observed_answer

        try:
            results: tuple[EvaluationResult, ...] = self.case_evaluator.evaluate(
                context=CaseEvaluationContext(
                    case=case,
                    dataset=self.dataset,
                    retrieval=retrieval,
                    answer=answer,
                    context_bundle=context_bundle,
                )
            )
            if not all(isinstance(result, EvaluationResult) for result in results):
                raise TypeError("case evaluator returned a non-EvaluationResult")
            results = _stabilize_evaluation_ids(case, results)
            _validate_evaluator_versions(results)
        except _EvaluatorVersionMismatch as error:
            return self._failed_case(
                case,
                "evaluator_failure",
                "evaluator-version",
                error,
                measurement=measurement,
                retrieval=retrieval,
                evaluations=results,
                additional_failures=(measurement_failure,) if measurement_failure else (),
                safety_result=safety_result,
                grounded_answer_safety_result=grounded_answer_safety_result,
            )
        except UnavailableIntegrationError as error:
            return self._failed_case(
                case,
                "unavailable_integration",
                "evaluator",
                error,
                measurement=measurement,
                retrieval=retrieval,
                additional_failures=(measurement_failure,) if measurement_failure else (),
                safety_result=safety_result,
                grounded_answer_safety_result=grounded_answer_safety_result,
            )
        except Exception as error:
            return self._failed_case(
                case,
                "evaluator_failure",
                "evaluator",
                error,
                measurement=measurement,
                retrieval=retrieval,
                additional_failures=(measurement_failure,) if measurement_failure else (),
                safety_result=safety_result,
                grounded_answer_safety_result=grounded_answer_safety_result,
            )

        scenario_failure = _scenario_failure(case, retrieval, results)
        if scenario_failure is not None:
            return self._failed_case(
                case,
                "scenario_failure",
                "scenario-assertions",
                scenario_failure,
                measurement=measurement,
                retrieval=retrieval,
                evaluations=results,
                additional_failures=(measurement_failure,) if measurement_failure else (),
                safety_result=safety_result,
                grounded_answer_safety_result=grounded_answer_safety_result,
            )
        if (
            grounded_answer_safety_result is not None
            and grounded_answer_safety_result.status == "failed"
        ):
            return self._failed_case(
                case,
                "scenario_failure",
                "grounded-answer-safety",
                ProductionContractObservationError(
                    "grounded answer safety gate rejected observation"
                ),
                measurement=measurement,
                retrieval=retrieval,
                evaluations=results,
                additional_failures=(measurement_failure,) if measurement_failure else (),
                safety_result=safety_result,
                grounded_answer_safety_result=grounded_answer_safety_result,
            )
        return (
            ScenarioResult(
                scenario_id=case.case_id,
                case_id=case.case_id,
                scenario_tags=case.scenario_tags,
                expected_outcome=case.expected_outcome,
                observed_outcome=answer.outcome,
                status="passed",
                evaluation_ids=tuple(result.id for result in results),
                expected_evidence_ids=case.expected_evidence_ids,
                observed_evidence_ids=tuple(
                    reference.evidence_id for reference in retrieval.references
                ),
            ),
            tuple(results),
            measurement,
            (measurement_failure,) if measurement_failure else (),
            safety_result,
            grounded_answer_safety_result,
        )

    @staticmethod
    def _failed_case(
        case: QACase,
        category: FailureCategory,
        component: str,
        error: Exception,
        *,
        measurement: MeasurementObservation | None = None,
        retrieval: RetrievalResult | None = None,
        evaluations: Sequence[EvaluationResult] = (),
        additional_failures: Sequence[FailureRecord] = (),
        safety_result: ProductionContractSafetyResult | None = None,
        grounded_answer_safety_result: GroundedAnswerSafetyResult | None = None,
    ) -> tuple[
        ScenarioResult,
        tuple[EvaluationResult, ...],
        MeasurementObservation,
        tuple[FailureRecord, ...],
        ProductionContractSafetyResult | None,
        GroundedAnswerSafetyResult | None,
    ]:
        status: ScenarioStatus = "unavailable" if category == "unavailable_integration" else "failed"
        return (
            ScenarioResult(
                scenario_id=case.case_id,
                case_id=case.case_id,
                scenario_tags=case.scenario_tags,
                expected_outcome=case.expected_outcome,
                observed_outcome=None,
                status=status,
                evaluation_ids=tuple(result.id for result in evaluations),
                observed_evidence_ids=(
                    tuple(reference.evidence_id for reference in retrieval.references)
                    if retrieval is not None
                    else ()
                ),
            ),
            tuple(evaluations),
            measurement
            or MeasurementObservation.unavailable(
                scenario_id=case.case_id,
                source="not-observed",
                latency_reason="scenario-did-not-complete",
                cost_reason="scenario-did-not-complete",
            ),
            (
                *additional_failures,
                FailureRecord(
                    scenario_id=case.case_id,
                    category=category,
                    component=component,
                    error_type=type(error).__name__,
                ),
            ),
            safety_result,
            grounded_answer_safety_result,
        )

    def _index_version(self) -> str:
        if self.execution_mode == "production-contract":
            assert self.production_observer is not None
            return getattr(
                self.production_observer,
                "index_version",
                PRODUCTION_CONTRACT_INDEX_VERSION,
            )
        return self.replay.index_version


async def run_r1_evaluation(
    *,
    dataset: QADataset | None = None,
    replay: DeterministicRetrievalReplay | None = None,
    measurement_source: MeasurementSource | None = None,
    case_evaluator: CaseEvaluator | None = None,
    expected_dataset: DatasetIdentity | None = None,
    recall_k_values: tuple[int, ...] = (1,),
    execution_mode: ExecutionMode = "synthetic-replay",
    production_observer: ProductionContractObservationPort | None = None,
) -> OfflineEvaluationReport:
    """Run the fixed R1 evaluation set asynchronously for tests and workers."""

    return await OfflineEvaluationRunner(
        dataset=dataset,
        replay=replay,
        measurement_source=measurement_source,
        case_evaluator=case_evaluator,
        expected_dataset=expected_dataset,
        recall_k_values=recall_k_values,
        execution_mode=execution_mode,
        production_observer=production_observer,
    ).run()


def run_r1_evaluation_sync(
    *,
    dataset: QADataset | None = None,
    replay: DeterministicRetrievalReplay | None = None,
    measurement_source: MeasurementSource | None = None,
    case_evaluator: CaseEvaluator | None = None,
    expected_dataset: DatasetIdentity | None = None,
    recall_k_values: tuple[int, ...] = (1,),
    execution_mode: ExecutionMode = "synthetic-replay",
    production_observer: ProductionContractObservationPort | None = None,
) -> OfflineEvaluationReport:
    """Run the fixed R1 evaluation set synchronously for scripts and CI."""

    return OfflineEvaluationRunner(
        dataset=dataset,
        replay=replay,
        measurement_source=measurement_source,
        case_evaluator=case_evaluator,
        expected_dataset=expected_dataset,
        recall_k_values=recall_k_values,
        execution_mode=execution_mode,
        production_observer=production_observer,
    ).run_sync()


async def run_r1_production_contract_evaluation(
    *,
    production_observer: ProductionContractObservationPort,
    dataset: QADataset | None = None,
    measurement_source: MeasurementSource | None = None,
    case_evaluator: CaseEvaluator | None = None,
    expected_dataset: DatasetIdentity | None = None,
    recall_k_values: tuple[int, ...] = (1,),
) -> OfflineEvaluationReport:
    """Run R1 against observations emitted by an application composition root."""

    return await run_r1_evaluation(
        dataset=dataset,
        measurement_source=measurement_source,
        case_evaluator=case_evaluator,
        expected_dataset=expected_dataset,
        recall_k_values=recall_k_values,
        execution_mode="production-contract",
        production_observer=production_observer,
    )


def run_r1_production_contract_evaluation_sync(
    *,
    production_observer: ProductionContractObservationPort,
    dataset: QADataset | None = None,
    measurement_source: MeasurementSource | None = None,
    case_evaluator: CaseEvaluator | None = None,
    expected_dataset: DatasetIdentity | None = None,
    recall_k_values: tuple[int, ...] = (1,),
) -> OfflineEvaluationReport:
    """Synchronous entry point for the production-contract evaluation mode."""

    return run_r1_evaluation_sync(
        dataset=dataset,
        measurement_source=measurement_source,
        case_evaluator=case_evaluator,
        expected_dataset=expected_dataset,
        recall_k_values=recall_k_values,
        execution_mode="production-contract",
        production_observer=production_observer,
    )


def _answer_submission_from_output(
    output: object,
    *,
    case: QACase | None = None,
) -> AnswerSubmission:
    from spine.domain.capabilities import AnswerQuestionOutput, AnswerQuestionOutcome

    if type(output) is not AnswerQuestionOutput:
        raise ProductionContractObservationError("answer output is not a typed contract")
    answer_content = output.answer
    if answer_content is None:
        return AnswerSubmission(outcome=output.outcome.value)

    citation_ids_by_answer_id = {
        claim.claim_id: tuple(
            citation_id
            for relationship in answer_content.relationships
            if relationship.claim_id == claim.claim_id
            for citation_id in relationship.citation_ids
        )
        for claim in answer_content.claims
    }
    # Capability claim IDs are opaque UUIDs. Align labels by exact observed
    # claim text only for evaluation; the answer text and citations remain the
    # values emitted by the application flow.
    evaluation_claim_ids = (
        {claim.text: claim.claim_id for claim in case.expected_claims}
        if case is not None
        else {}
    )
    return AnswerSubmission(
        outcome="answered",
        claims=tuple(
            AnswerClaim(
                claim_id=evaluation_claim_ids.get(claim.text, str(claim.claim_id)),
                text=claim.text,
                citation_ids=tuple(str(item) for item in citation_ids_by_answer_id[claim.claim_id]),
            )
            for claim in answer_content.claims
        ),
        citations=tuple(
            Citation(
                citation_id=str(citation.citation_id),
                evidence_id=citation.evidence_reference.evidence_id,
                source_object_id=citation.evidence_reference.source_object_id,
                source_revision_id=citation.evidence_reference.source_revision_id,
                workspace_id=citation.evidence_reference.workspace_id,
                environment_id=citation.evidence_reference.environment_id,
                revision_digest=citation.evidence_reference.revision_digest,
                locator=citation.evidence_reference.locator.model_dump(mode="json"),
                context_bundle_id=str(citation.context_bundle_id),
            )
            for citation in answer_content.citations
        ),
        semantic_score=None,
    )


def _evaluation_bundle_from_domain(
    bundle: object | None, case: QACase
) -> ContextBundle:
    from spine.domain.context_bundles import ContextBundle as DomainContextBundle
    from spine.memory.contracts import ContextReference

    if bundle is None:
        return ContextBundle(
            bundle_id=f"production:{case.case_id}:none",
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            references=(),
        )
    if type(bundle) is not DomainContextBundle:
        raise ProductionContractObservationError("context bundle is not a typed domain contract")
    return ContextBundle(
        bundle_id=str(bundle.bundle_id),
        workspace_id=bundle.workspace_id,
        environment_id=bundle.environment_id,
        references=tuple(
            ContextReference(
                source_id=str(reference.source_object_id),
                source_revision_id=reference.source_revision_id,
                workspace_id=reference.workspace_id,
                environment_id=reference.environment_id,
                evidence_id=reference.evidence_id,
                locator=reference.locator.model_dump(mode="json"),
            )
            for reference in bundle.evidence_references
        ),
    )


def _query(case: QACase) -> RetrievalQuery:
    return RetrievalQuery(
        workspace_id=case.workspace_id,
        environment_id=case.environment_id,
        query=case.question,
        requested_by=ActorRef(kind=ActorKind.HUMAN, id=case.principal_id),
        context_profile=REPLAY_CONTEXT_PROFILE,
        limit=10,
    )


def _expected_answer(
    case: QACase,
    retrieval: RetrievalResult,
    dataset: QADataset,
) -> tuple[AnswerSubmission, ContextBundle]:
    if case.expected_outcome == "answered" and not case.expected_answer:
        raise _ScenarioFailure("answered case has no expected answer")
    revisions = {
        revision.revision_id: revision for revision in dataset.source_revisions
    }
    context_bundle_id = f"offline:{dataset.version}:{case.case_id}"
    citations = tuple(
        Citation(
            citation_id=f"citation-{reference.evidence_id}",
            evidence_id=reference.evidence_id,
            source_object_id=UUID(reference.source_id),
            source_revision_id=reference.source_revision_id,
            workspace_id=reference.workspace_id,
            environment_id=reference.environment_id,
            source_revision_digest=revisions[reference.source_revision_id].revision_digest,
            locator=reference.locator,
            context_bundle_id=context_bundle_id,
        )
        for reference in retrieval.references
    )
    citations_by_evidence = {citation.evidence_id: citation for citation in citations}
    if case.expected_outcome == "answered":
        try:
            claims = tuple(
                AnswerClaim(
                    claim_id=claim.claim_id,
                    text=claim.text,
                    citation_ids=tuple(
                        citations_by_evidence[evidence_id].citation_id
                        for evidence_id in claim.evidence_ids
                    ),
                )
                for claim in case.expected_claims
            )
        except KeyError as error:
            raise _ScenarioFailure("expected evidence was not retrievable") from error
    else:
        claims = ()
    answer = AnswerSubmission(
        outcome=case.expected_outcome,
        claims=claims,
        citations=citations,
        semantic_score=1.0,
    )
    return (
        answer,
        ContextBundle(
            bundle_id=context_bundle_id,
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
            references=retrieval.references,
        ),
    )


def _scenario_failure(
    case: QACase,
    retrieval: RetrievalResult,
    evaluations: Sequence[EvaluationResult],
) -> _ScenarioFailure | None:
    observed_ids = tuple(reference.evidence_id for reference in retrieval.references)
    if case.expected_outcome == "answered" and observed_ids != case.expected_evidence_ids:
        return _ScenarioFailure("retrieval did not match expected evidence")
    if case.expected_outcome != "answered" and (retrieval.references or retrieval.context):
        return _ScenarioFailure("non-answer scenario returned protected context")
    if any(result.verdict is EvaluationVerdict.FAIL for result in evaluations):
        return _ScenarioFailure("a deterministic evaluator rejected the scenario")
    return None


def _stabilize_evaluation_ids(
    case: QACase,
    results: Sequence[EvaluationResult],
) -> tuple[EvaluationResult, ...]:
    """Make injected evaluator output deterministic without trusting its UUIDs."""

    stabilized: list[EvaluationResult] = []
    for index, result in enumerate(results):
        payload = result.model_dump(mode="json", exclude={"id"})
        identity = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        stable_id = uuid5(
            _REPORT_NAMESPACE,
            f"evaluation:{case.case_id}:{index}:{identity}",
        )
        stabilized.append(result.model_copy(update={"id": stable_id}))
    return tuple(stabilized)


def _evaluator_catalog(results: Sequence[EvaluationResult]) -> list[dict[str, str]]:
    identities = {
        (result.evaluator_key, result.evaluator_version)
        for result in results
    }
    return [
        {"evaluator_key": key, "evaluator_version": version}
        for key, version in sorted(identities)
    ]


def _aggregates(results: Sequence[EvaluationResult]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[EvaluationResult]] = defaultdict(list)
    for result in results:
        grouped[(result.evaluator_key, result.evaluator_version)].append(result)

    aggregates: list[dict[str, Any]] = []
    for (key, version), group in sorted(grouped.items()):
        scores = [result.score for result in group if result.score is not None]
        aggregates.append(
            {
                "evaluator_key": key,
                "evaluator_version": version,
                "measured_count": len(scores),
                "not_applicable_count": sum(result.score is None for result in group),
                "pass_count": sum(result.verdict is EvaluationVerdict.PASS for result in group),
                "fail_count": sum(result.verdict is EvaluationVerdict.FAIL for result in group),
                "review_count": sum(result.verdict is EvaluationVerdict.REVIEW for result in group),
                "mean_score": sum(scores) / len(scores) if scores else None,
            }
        )
    return aggregates


__all__ = [
    "ANSWER_CONFIGURATION_VERSION",
    "CaseEvaluationContext",
    "CaseEvaluator",
    "DatasetIdentity",
    "ExecutionMode",
    "EvaluationConfiguration",
    "EvaluatorIdentity",
    "FailureRecord",
    "MeasurementObservation",
    "MeasurementSource",
    "MetricAggregate",
    "OFFLINE_REPORT_SCHEMA_VERSION",
    "OFFLINE_RUNNER_VERSION",
    "OfflineEvaluationReport",
    "OfflineEvaluationRunner",
    "ProductionContractObservationPort",
    "QUALIFICATION_SCOPE",
    "SCENARIO_CONFIGURATION_VERSION",
    "ScenarioResult",
    "UnavailableIntegrationError",
    "run_r1_evaluation",
    "run_r1_evaluation_sync",
    "run_r1_production_contract_evaluation",
    "run_r1_production_contract_evaluation_sync",
]


_EXPECTED_EVALUATOR_VERSIONS = {
    "retrieval.recall_at_k": RETRIEVAL_METRICS_EVALUATOR_VERSION,
    "retrieval.mrr": RETRIEVAL_METRICS_EVALUATOR_VERSION,
    "qa.answer_outcome": CITATION_METRICS_EVALUATOR_VERSION,
    "qa.citations": CITATION_METRICS_EVALUATOR_VERSION,
    "qa.citation_precision": CITATION_METRICS_EVALUATOR_VERSION,
    "qa.citation_recall": CITATION_METRICS_EVALUATOR_VERSION,
    "qa.locator_validity": CITATION_METRICS_EVALUATOR_VERSION,
    "qa.groundedness": CITATION_METRICS_EVALUATOR_VERSION,
}


def _validate_evaluator_versions(results: Sequence[EvaluationResult]) -> None:
    for result in results:
        expected = _EXPECTED_EVALUATOR_VERSIONS.get(result.evaluator_key)
        if expected is not None and result.evaluator_version != expected:
            raise _EvaluatorVersionMismatch
