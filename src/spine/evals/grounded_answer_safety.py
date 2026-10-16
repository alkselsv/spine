"""Independent fail-closed checks for observed R1 answer contracts."""

from __future__ import annotations

from enum import Enum
from typing import Final, Literal

from spine.domain.common import DefinitionModel
from spine.domain.evaluations import EvaluationVerdict
from spine.evals.qa_r1 import QACase, QADataset
from spine.evaluations.citation_metrics import (
    AnswerSubmission,
    ContextBundle,
    evaluate_answer_outcome,
    evaluate_citations,
    evaluate_groundedness,
)


GROUNDED_ANSWER_SAFETY_SEAM: Final[str] = "g03-grounded-answer-safety:v1"


class GroundedAnswerSafetyViolation(str, Enum):
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    CONTEXT_SCOPE_MISMATCH = "context_scope_mismatch"
    ANSWER_OUTCOME_MISMATCH = "answer_outcome_mismatch"
    CITATION_VALIDATION_FAILED = "citation_validation_failed"
    GROUNDEDNESS_VALIDATION_FAILED = "groundedness_validation_failed"


class GroundedAnswerSafetyResult(DefinitionModel):
    """Disclosure-safe, deterministic safety disposition for one answer."""

    schema_version: Literal[GROUNDED_ANSWER_SAFETY_SEAM] = GROUNDED_ANSWER_SAFETY_SEAM
    seam: Literal[GROUNDED_ANSWER_SAFETY_SEAM] = GROUNDED_ANSWER_SAFETY_SEAM
    case_id: str
    status: Literal["passed", "failed"]
    violation_codes: tuple[GroundedAnswerSafetyViolation, ...] = ()


class GroundedAnswerSafetyGate:
    """Apply exact outcome, citation, and claim-support checks to observed data."""

    def evaluate(
        self,
        *,
        case: QACase,
        dataset: QADataset,
        answer: AnswerSubmission,
        context_bundle: ContextBundle,
    ) -> GroundedAnswerSafetyResult:
        violations: list[GroundedAnswerSafetyViolation] = []
        if (
            context_bundle.workspace_id != case.workspace_id
            or context_bundle.environment_id != case.environment_id
        ):
            violations.append(GroundedAnswerSafetyViolation.CONTEXT_SCOPE_MISMATCH)

        outcome = evaluate_answer_outcome(
            answer,
            case,
            dataset=dataset,
            context_bundle=context_bundle,
        )
        if outcome.verdict is EvaluationVerdict.FAIL:
            violations.append(GroundedAnswerSafetyViolation.ANSWER_OUTCOME_MISMATCH)

        if answer.outcome == "answered":
            citations = evaluate_citations(
                answer,
                case,
                dataset=dataset,
                context_bundle=context_bundle,
            )
            if citations.verdict is EvaluationVerdict.FAIL:
                violations.append(
                    GroundedAnswerSafetyViolation.CITATION_VALIDATION_FAILED
                )

            groundedness = evaluate_groundedness(
                answer,
                case,
                dataset=dataset,
                context_bundle=context_bundle,
            )
            if groundedness.verdict is EvaluationVerdict.FAIL:
                violations.append(
                    GroundedAnswerSafetyViolation.GROUNDEDNESS_VALIDATION_FAILED
                )
        elif answer.claims or answer.citations:
            violations.extend(
                (
                    GroundedAnswerSafetyViolation.CITATION_VALIDATION_FAILED,
                    GroundedAnswerSafetyViolation.GROUNDEDNESS_VALIDATION_FAILED,
                )
            )

        unique_violations = tuple(dict.fromkeys(violations))
        return GroundedAnswerSafetyResult(
            case_id=case.case_id,
            status="failed" if unique_violations else "passed",
            violation_codes=unique_violations,
        )

    @staticmethod
    def failed_without_observation(case_id: str) -> GroundedAnswerSafetyResult:
        return GroundedAnswerSafetyResult(
            case_id=case_id,
            status="failed",
            violation_codes=(GroundedAnswerSafetyViolation.OBSERVATION_UNAVAILABLE,),
        )


__all__ = [
    "GROUNDED_ANSWER_SAFETY_SEAM",
    "GroundedAnswerSafetyGate",
    "GroundedAnswerSafetyResult",
    "GroundedAnswerSafetyViolation",
]
