"""Safe projection of immutable Q&A completeness decision records."""

from __future__ import annotations

from spine.domain.capabilities import (
    AnswerCompletenessPresentation,
    AnswerServingDecisionCode,
)
from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecord,
    AnswerDisclosureRecordV3,
)
from spine.domain.capabilities.answer_evaluation import AnswerDisclosureStatus
from spine.domain.capabilities.answer_question import AnswerQuestionOutcome


def completeness_presentation(
    record: AnswerDisclosureRecord | None,
) -> AnswerCompletenessPresentation | None:
    if not isinstance(record, AnswerDisclosureRecordV3):
        return None
    if record.outcome is AnswerQuestionOutcome.DENIED:
        return None
    evaluation = record.evaluation
    return AnswerCompletenessPresentation(
        target_scope_id=record.target_scope.target_scope_id,
        target_scope_version=record.target_scope.schema_version,
        observed_coverage_id=record.observed_coverage.observed_coverage_id,
        observed_coverage_version=record.observed_coverage.schema_version,
        coverage_status=evaluation.coverage_status,
        partiality_status=evaluation.partiality_status,
        conflict_status=evaluation.conflict_status,
        partial_fallback_authority=(
            None
            if record.partial_fallback is None
            else record.partial_fallback.authority
        ),
        serving_decision=(
            AnswerServingDecisionCode.SERVE
            if evaluation.disclosure_status is AnswerDisclosureStatus.ACCEPTED
            and record.outcome is AnswerQuestionOutcome.ANSWERED
            else AnswerServingDecisionCode.WITHHOLD
        ),
        validation_decision=evaluation.safe_reason,
    )


__all__ = ["completeness_presentation"]
