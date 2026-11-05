"""Deterministic release gate for synthetic email citation locator vectors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.domain.disclosure import CitationInspectionResult, CitationInspectionStatus
from spine.domain.evaluations import EvaluationResult, EvaluationVerdict

EMAIL_CITATION_RESOLUTION_EVALUATOR_KEY: Final[str] = "qa.email_citation_resolution"
EMAIL_CITATION_RESOLUTION_EVALUATOR_VERSION: Final[str] = "v2"


@dataclass(frozen=True, slots=True)
class EmailCitationResolutionVector:
    """Pinned content-free expectation for one citation attack or happy path."""

    vector_id: str
    locator_contract_version: Literal[
        "email-message-locator:v1", "email-attachment-locator:v1"
    ]
    expected_status: CitationInspectionStatus


EMAIL_ATTACHMENT_CITATION_VECTORS: Final[tuple[EmailCitationResolutionVector, ...]] = (
    EmailCitationResolutionVector(
        "attachment_exact_lineage",
        "email-attachment-locator:v1",
        CitationInspectionStatus.AVAILABLE,
    ),
    *(
        EmailCitationResolutionVector(
            vector_id,
            "email-attachment-locator:v1",
            CitationInspectionStatus.UNAVAILABLE,
        )
        for vector_id in (
            "attachment_wrong_parent",
            "attachment_wrong_mime_part",
            "attachment_wrong_artifact_digest",
            "attachment_wrong_processing_digest",
            "attachment_stale_child_revision",
            "attachment_cross_workspace",
            "attachment_stale_parsed_artifact",
            "attachment_revoked_parent",
            "attachment_revoked_child",
        )
    ),
)


def evaluate_email_citation_resolution(
    result: CitationInspectionResult,
    *,
    expected_status: CitationInspectionStatus,
    vector_id: str,
    citation_id: UUID,
    workspace_id: UUID,
    environment_id: UUID,
    locator_contract_version: Literal[
        "email-message-locator:v1", "email-attachment-locator:v1"
    ] = "email-message-locator:v1",
) -> EvaluationResult:
    """Compare one pinned synthetic vector without retaining protected content."""

    passed = result.status is expected_status
    identity = (
        f"{EMAIL_CITATION_RESOLUTION_EVALUATOR_KEY}:"
        f"{EMAIL_CITATION_RESOLUTION_EVALUATOR_VERSION}:{vector_id}:"
        f"{locator_contract_version}:{citation_id}:"
        f"{expected_status.value}:{result.status.value}"
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
            "locator_contract_version": locator_contract_version,
            "expected_status": expected_status.value,
            "actual_status": result.status.value,
        },
    )


__all__ = [
    "EMAIL_CITATION_RESOLUTION_EVALUATOR_KEY",
    "EMAIL_CITATION_RESOLUTION_EVALUATOR_VERSION",
    "EMAIL_ATTACHMENT_CITATION_VECTORS",
    "EmailCitationResolutionVector",
    "evaluate_email_citation_resolution",
]
