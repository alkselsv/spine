"""Deterministic evaluation record for the citation disclosure gate."""

from __future__ import annotations

from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.citation_validation import (
    CitationValidationOutcome,
    ValidCitation,
)
from spine.domain.evaluations import EvaluationResult, EvaluationVerdict


CITATION_VALIDATION_EVALUATOR_KEY: Final[str] = "qa.citation_validation"
CITATION_VALIDATION_EVALUATOR_VERSION: Final[str] = "v1"


def evaluate_citation_validation(
    outcome: CitationValidationOutcome,
    *,
    subject_id: UUID,
    workspace_id: UUID,
    environment_id: UUID,
) -> EvaluationResult:
    """Record whether the deterministic citation identity gate passed."""

    passed = isinstance(outcome, ValidCitation)
    identity = (
        f"{CITATION_VALIDATION_EVALUATOR_KEY}:"
        f"{CITATION_VALIDATION_EVALUATOR_VERSION}:{subject_id}:"
        f"{outcome.validation_reference}:{outcome.reason_code.value}"
    )
    return EvaluationResult(
        id=uuid5(NAMESPACE_URL, identity),
        evaluator_key=CITATION_VALIDATION_EVALUATOR_KEY,
        evaluator_version=CITATION_VALIDATION_EVALUATOR_VERSION,
        subject_type="citation",
        subject_id=subject_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        verdict=EvaluationVerdict.PASS if passed else EvaluationVerdict.FAIL,
        score=1.0 if passed else 0.0,
        details={
            "status": outcome.status,
            "reason_code": outcome.reason_code.value,
            "deterministic_gate": "passed" if passed else "failed",
        },
    )


__all__ = [
    "CITATION_VALIDATION_EVALUATOR_KEY",
    "CITATION_VALIDATION_EVALUATOR_VERSION",
    "evaluate_citation_validation",
]
