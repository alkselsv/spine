"""Deterministic release gate for synthetic email citation locator vectors."""

from __future__ import annotations

from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.domain.disclosure import CitationInspectionResult, CitationInspectionStatus
from spine.domain.evaluations import EvaluationResult, EvaluationVerdict


EMAIL_CITATION_RESOLUTION_EVALUATOR_KEY: Final[str] = (
    "qa.email_citation_resolution"
)
EMAIL_CITATION_RESOLUTION_EVALUATOR_VERSION: Final[str] = "v1"


def evaluate_email_citation_resolution(
    result: CitationInspectionResult,
    *,
    expected_status: CitationInspectionStatus,
    vector_id: str,
    citation_id: UUID,
    workspace_id: UUID,
    environment_id: UUID,
) -> EvaluationResult:
    """Compare one pinned synthetic vector without retaining protected content."""

    passed = result.status is expected_status
    identity = (
        f"{EMAIL_CITATION_RESOLUTION_EVALUATOR_KEY}:"
        f"{EMAIL_CITATION_RESOLUTION_EVALUATOR_VERSION}:{vector_id}:"
        f"{citation_id}:{expected_status.value}:{result.status.value}"
    )
    return EvaluationResult(
        id=uuid5(NAMESPACE_URL, identity),
        evaluator_key=EMAIL_CITATION_RESOLUTION_EVALUATOR_KEY,
        evaluator_version=EMAIL_CITATION_RESOLUTION_EVALUATOR_VERSION,
        subject_type="email_citation",
        subject_id=citation_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        verdict=EvaluationVerdict.PASS if passed else EvaluationVerdict.FAIL,
        score=1.0 if passed else 0.0,
        details={
            "vector_id": vector_id,
            "locator_contract_version": "email-message-locator:v1",
            "expected_status": expected_status.value,
            "actual_status": result.status.value,
        },
    )


__all__ = [
    "EMAIL_CITATION_RESOLUTION_EVALUATOR_KEY",
    "EMAIL_CITATION_RESOLUTION_EVALUATOR_VERSION",
    "evaluate_email_citation_resolution",
]
