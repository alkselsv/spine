"""Post-observation evaluator for the answer-only Golden Dataset."""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from spine.domain.common import DefinitionModel
from spine.evals.answer_quality_dataset import (
    AnswerQualityCase,
    LoadedAnswerQualityDataset,
)
from spine.evals.real_evaluation import FinalizedRealObservation


class AnswerQualityEvaluationContractError(ValueError):
    """A disclosure-safe answer-quality evaluation contract failure."""


class AnswerQualityGoldPort(Protocol):
    """Release protected answer labels only for a finalized observation."""

    dataset_version: str
    dataset_sha256: str

    def load_case(self, finalized: FinalizedRealObservation) -> AnswerQualityCase: ...


class ValidatedAnswerQualityGoldProvider:
    """Read gold from one structurally validated answer-quality dataset."""

    def __init__(self, loaded: LoadedAnswerQualityDataset) -> None:
        if (
            type(loaded) is not LoadedAnswerQualityDataset
            or loaded.validation_report.validation_status != "passed"
            or loaded.validation_report.dataset_version != loaded.dataset.version
            or loaded.validation_report.dataset_sha256 != loaded.dataset.sha256
            or loaded.validation_report.case_count != len(loaded.dataset.cases)
        ):
            raise AnswerQualityEvaluationContractError(
                "answer-quality gold dataset is not validated"
            )
        self._dataset = loaded.dataset
        self.dataset_version = loaded.dataset.version
        self.dataset_sha256 = loaded.dataset.sha256

    def load_case(self, finalized: FinalizedRealObservation) -> AnswerQualityCase:
        if type(finalized) is not FinalizedRealObservation or not finalized.is_finalized:
            raise AnswerQualityEvaluationContractError(
                "answer-quality gold requires a finalized observation"
            )
        matches = tuple(
            case
            for case in self._dataset.cases
            if case.case_id == finalized.observation.case_id
        )
        if len(matches) != 1:
            raise AnswerQualityEvaluationContractError(
                "answer-quality gold case is unavailable or ambiguous"
            )
        return matches[0]


class AnswerQualityEvaluationCase(DefinitionModel):
    """Protected answer labels joined to canonical source identities."""

    case_id: str = Field(min_length=1, max_length=256)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_answer: str = Field(min_length=1)
    expected_source_revision_ids: tuple[UUID, ...] = ()


class AnswerQualityJudgeAssessment(DefinitionModel):
    """Protected semantic comparison returned by a versioned judge."""

    evaluator_id: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    correctness_score: float = Field(ge=0.0, le=1.0)
    completeness_score: float = Field(ge=0.0, le=1.0)
    contradiction_detected: bool


class AnswerQualityJudgePort(Protocol):
    """Compare one finalized answer with protected gold after observation."""

    async def assess(
        self, *, reference_answer: str, observed_answer: str
    ) -> AnswerQualityJudgeAssessment: ...


class AnswerQualityCaseEvaluationResult(DefinitionModel):
    """Protected per-case result retained only in approved local storage."""

    case_id: str = Field(min_length=1, max_length=256)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_id: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    correctness_score: float = Field(ge=0.0, le=1.0)
    completeness_score: float = Field(ge=0.0, le=1.0)
    answered_outcome_score: float = Field(ge=0.0, le=1.0)
    contradiction_detected: bool
    document_citation_eligible: bool
    document_citation_covered: bool


class AnswerQualityAggregateMetric(DefinitionModel):
    """Disclosure-safe metric with explicit measurement coverage."""

    evaluator_key: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    status: Literal["measured", "unavailable"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    eligible_count: int = Field(ge=0)
    measured_count: int = Field(ge=0)


class AnswerQualityEvaluationReport(DefinitionModel):
    """Metadata-only aggregate that excludes questions, answers and identities."""

    schema_version: Literal["answer-quality-evaluation-report:v1"] = (
        "answer-quality-evaluation-report:v1"
    )
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_status: Literal["completed", "partial"]
    expected_case_count: int = Field(ge=1)
    evaluated_case_count: int = Field(ge=0)
    unavailable_case_count: int = Field(ge=0)
    metrics: tuple[AnswerQualityAggregateMetric, ...]

    def metric(self, evaluator_key: str) -> AnswerQualityAggregateMetric:
        matches = tuple(
            metric for metric in self.metrics if metric.evaluator_key == evaluator_key
        )
        if len(matches) != 1:
            raise KeyError(evaluator_key)
        return matches[0]


class AnswerQualityEvaluationAdapter:
    """Join answer gold only after production observation finalization."""

    def join_gold(
        self,
        finalized: FinalizedRealObservation,
        *,
        gold: AnswerQualityGoldPort,
    ) -> AnswerQualityEvaluationCase:
        if type(finalized) is not FinalizedRealObservation or not finalized.is_finalized:
            raise AnswerQualityEvaluationContractError(
                "answer-quality evaluation requires a finalized observation"
            )
        case = gold.load_case(finalized)
        if type(case) is not AnswerQualityCase or case.case_id != finalized.observation.case_id:
            raise AnswerQualityEvaluationContractError(
                "answer-quality gold provider returned another case"
            )
        bindings = {
            (item.dataset_source_id, item.dataset_revision_id): item
            for item in finalized.resolution.bindings
        }
        if len(bindings) != len(finalized.resolution.bindings):
            raise AnswerQualityEvaluationContractError(
                "answer-quality runtime bindings are ambiguous"
            )
        try:
            expected_revisions = tuple(
                bindings[(source_id, revision_id)].source_revision_id
                for source_id, revision_id in zip(
                    case.source_ids,
                    case.source_revision_ids,
                    strict=True,
                )
            )
        except (KeyError, ValueError):
            raise AnswerQualityEvaluationContractError(
                "answer-quality gold source is outside verified runtime bindings"
            ) from None
        return AnswerQualityEvaluationCase(
            case_id=case.case_id,
            dataset_version=gold.dataset_version,
            dataset_sha256=gold.dataset_sha256,
            expected_answer=case.expected_answer,
            expected_source_revision_ids=expected_revisions,
        )


async def evaluate_answer_quality_case(
    case: AnswerQualityEvaluationCase,
    *,
    observed_answer: str | None,
    observed_outcome: str,
    cited_source_revision_ids: tuple[UUID, ...],
    judge: AnswerQualityJudgePort,
) -> AnswerQualityCaseEvaluationResult:
    """Score one protected case after gold join; never performs retrieval."""

    if observed_outcome == "answered" and observed_answer:
        assessment = await judge.assess(
            reference_answer=case.expected_answer,
            observed_answer=observed_answer,
        )
        correctness = assessment.correctness_score
        completeness = assessment.completeness_score
        contradiction = assessment.contradiction_detected
        evaluator_id = assessment.evaluator_id
        evaluator_version = assessment.evaluator_version
        answered_score = 1.0
    else:
        correctness = 0.0
        completeness = 0.0
        contradiction = False
        evaluator_id = "answer-outcome-fallback"
        evaluator_version = "v1"
        answered_score = 0.0
    expected_revisions = set(case.expected_source_revision_ids)
    return AnswerQualityCaseEvaluationResult(
        case_id=case.case_id,
        dataset_version=case.dataset_version,
        dataset_sha256=case.dataset_sha256,
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        correctness_score=correctness,
        completeness_score=completeness,
        answered_outcome_score=answered_score,
        contradiction_detected=contradiction,
        document_citation_eligible=bool(expected_revisions),
        document_citation_covered=(
            bool(expected_revisions)
            and expected_revisions <= set(cited_source_revision_ids)
        ),
    )


def aggregate_answer_quality_evaluations(
    results: tuple[AnswerQualityCaseEvaluationResult, ...],
    *,
    dataset_version: str,
    dataset_sha256: str,
    expected_case_count: int,
) -> AnswerQualityEvaluationReport:
    """Aggregate protected case results without retaining case-level content."""

    if expected_case_count < 1 or len(results) > expected_case_count:
        raise AnswerQualityEvaluationContractError(
            "answer-quality evaluation population is invalid"
        )
    if any(
        item.dataset_version != dataset_version
        or item.dataset_sha256 != dataset_sha256
        for item in results
    ):
        raise AnswerQualityEvaluationContractError(
            "answer-quality evaluation dataset identity mismatch"
        )
    measured = len(results)
    document_eligible = tuple(item for item in results if item.document_citation_eligible)

    def mean(field: str) -> float | None:
        if not results:
            return None
        return sum(float(getattr(item, field)) for item in results) / len(results)

    evaluator_versions = sorted(
        {f"{item.evaluator_id}:{item.evaluator_version}" for item in results}
    )
    answer_evaluator_version = "+".join(evaluator_versions) or "unavailable"
    metrics = (
        AnswerQualityAggregateMetric(
            evaluator_key="qa.answer_correctness",
            evaluator_version=answer_evaluator_version,
            status="measured" if results else "unavailable",
            score=mean("correctness_score"),
            eligible_count=expected_case_count,
            measured_count=measured,
        ),
        AnswerQualityAggregateMetric(
            evaluator_key="qa.answer_completeness",
            evaluator_version=answer_evaluator_version,
            status="measured" if results else "unavailable",
            score=mean("completeness_score"),
            eligible_count=expected_case_count,
            measured_count=measured,
        ),
        AnswerQualityAggregateMetric(
            evaluator_key="qa.answer_outcome",
            evaluator_version="v1",
            status="measured" if results else "unavailable",
            score=mean("answered_outcome_score"),
            eligible_count=expected_case_count,
            measured_count=measured,
        ),
        AnswerQualityAggregateMetric(
            evaluator_key="qa.document_citation_coverage",
            evaluator_version="unavailable:answer-only-gold",
            status="unavailable",
            eligible_count=len(document_eligible),
            measured_count=0,
        ),
        AnswerQualityAggregateMetric(
            evaluator_key="qa.citation_recall",
            evaluator_version="unavailable:no-locator-gold",
            status="unavailable",
            eligible_count=expected_case_count,
            measured_count=0,
        ),
        AnswerQualityAggregateMetric(
            evaluator_key="qa.claim_groundedness",
            evaluator_version="unavailable:no-claim-evidence-gold",
            status="unavailable",
            eligible_count=expected_case_count,
            measured_count=0,
        ),
    )
    return AnswerQualityEvaluationReport(
        dataset_version=dataset_version,
        dataset_sha256=dataset_sha256,
        evaluation_status=(
            "completed" if measured == expected_case_count else "partial"
        ),
        expected_case_count=expected_case_count,
        evaluated_case_count=measured,
        unavailable_case_count=expected_case_count - measured,
        metrics=metrics,
    )


__all__ = [
    "AnswerQualityEvaluationAdapter",
    "AnswerQualityAggregateMetric",
    "AnswerQualityCaseEvaluationResult",
    "AnswerQualityEvaluationCase",
    "AnswerQualityEvaluationContractError",
    "AnswerQualityEvaluationReport",
    "AnswerQualityGoldPort",
    "AnswerQualityJudgeAssessment",
    "AnswerQualityJudgePort",
    "ValidatedAnswerQualityGoldProvider",
    "aggregate_answer_quality_evaluations",
    "evaluate_answer_quality_case",
]
