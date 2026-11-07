from __future__ import annotations

from spine.evals.answer_quality_dataset import (
    AnswerQualityCase,
    AnswerQualityDataset,
    AnswerQualityDatasetValidationReport,
    LoadedAnswerQualityDataset,
)
from spine.evaluations.answer_quality import (
    AnswerQualityEvaluationAdapter,
    AnswerQualityEvaluationContractError,
    AnswerQualityJudgeAssessment,
    ValidatedAnswerQualityGoldProvider,
    aggregate_answer_quality_evaluations,
    evaluate_answer_quality_case,
)
from spine.evals.real_evaluation import FinalizedRealObservation
from spine.evals.qa_r1 import load_r1_dataset
from tests.evals.test_production_contract import _safe_observation
from tests.evals.test_real_evaluation import _synthetic_resolution


def _loaded() -> LoadedAnswerQualityDataset:
    resolution = _synthetic_resolution()
    request = resolution.request
    bindings = resolution.bindings
    case = AnswerQualityCase(
        case_id=request.case_id,
        question=request.question,
        expected_answer="The approved reference answer.",
        document_refs=("policy.txt",),
        source_ids=(bindings[0].dataset_source_id,),
        source_revision_ids=(bindings[0].dataset_revision_id,),
        retrieval_source_ids=tuple(item.dataset_source_id for item in bindings),
        retrieval_source_revision_ids=tuple(
            item.dataset_revision_id for item in bindings
        ),
    )
    dataset = AnswerQualityDataset(
        version="answer-quality-test",
        sha256="d" * 64,
        cases=(case,),
    )
    return LoadedAnswerQualityDataset(
        dataset=dataset,
        validation_report=AnswerQualityDatasetValidationReport(
            dataset_version=dataset.version,
            dataset_sha256=dataset.sha256,
            case_count=1,
            source_bound_case_count=1,
            unscoped_case_count=0,
            unique_document_count=1,
            source_files_verified=1,
        ),
    )


def test_answer_quality_gold_joins_only_after_observation_is_finalized() -> None:
    resolution = _synthetic_resolution()
    observation = _safe_observation(
        next(case for case in load_r1_dataset().cases if case.case_id == resolution.request.case_id)
    )
    finalized = FinalizedRealObservation.from_observation(
        observation=observation,
        resolution=resolution,
    )
    provider = ValidatedAnswerQualityGoldProvider(_loaded())

    joined = AnswerQualityEvaluationAdapter().join_gold(finalized, gold=provider)

    assert joined.case_id == resolution.request.case_id
    assert joined.expected_answer == "The approved reference answer."
    assert joined.expected_source_revision_ids == (
        resolution.bindings[0].source_revision_id,
    )

    try:
        AnswerQualityEvaluationAdapter().join_gold(  # type: ignore[arg-type]
            observation,
            gold=provider,
        )
    except AnswerQualityEvaluationContractError as exc:
        assert "finalized" in str(exc)
    else:
        raise AssertionError("unfinalized observation released protected gold")


async def test_answer_quality_evaluation_aggregates_scores_and_marks_evidence_unavailable() -> None:
    class Judge:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def assess(
            self, *, reference_answer: str, observed_answer: str
        ) -> AnswerQualityJudgeAssessment:
            self.calls.append((reference_answer, observed_answer))
            return AnswerQualityJudgeAssessment(
                evaluator_id="qualified-answer-judge",
                evaluator_version="v1",
                correctness_score=0.8,
                completeness_score=0.6,
                contradiction_detected=False,
            )

    resolution = _synthetic_resolution()
    loaded = _loaded()
    evaluation_case = AnswerQualityEvaluationAdapter().join_gold(
        FinalizedRealObservation.from_observation(
            observation=_safe_observation(
                next(
                    case
                    for case in load_r1_dataset().cases
                    if case.case_id == resolution.request.case_id
                )
            ),
            resolution=resolution,
        ),
        gold=ValidatedAnswerQualityGoldProvider(loaded),
    )
    judge = Judge()

    result = await evaluate_answer_quality_case(
        evaluation_case,
        observed_answer="An observed answer.",
        observed_outcome="answered",
        cited_source_revision_ids=(resolution.bindings[0].source_revision_id,),
        judge=judge,
    )
    report = aggregate_answer_quality_evaluations(
        (result,),
        dataset_version=loaded.dataset.version,
        dataset_sha256=loaded.dataset.sha256,
        expected_case_count=1,
    )

    assert judge.calls == [
        ("The approved reference answer.", "An observed answer.")
    ]
    assert report.evaluation_status == "completed"
    assert report.metric("qa.answer_correctness").score == 0.8
    assert report.metric("qa.answer_completeness").score == 0.6
    assert report.metric("qa.answer_outcome").score == 1.0
    assert report.metric("qa.document_citation_coverage").status == "unavailable"
    assert report.metric("qa.document_citation_coverage").score is None
    assert report.metric("qa.citation_recall").status == "unavailable"
    assert report.metric("qa.claim_groundedness").status == "unavailable"
