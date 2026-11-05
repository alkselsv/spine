from uuid import UUID

from spine.domain.disclosure import (
    CitationInspectionReason,
    CitationInspectionResult,
    CitationInspectionStatus,
)
from spine.domain.evaluations import EvaluationVerdict
from spine.evaluations.email_citation_resolution import (
    EMAIL_ATTACHMENT_CITATION_VECTORS,
    evaluate_email_citation_resolution,
)


def test_email_citation_resolution_evaluator_records_vector_outcome_without_content() -> (
    None
):
    unavailable = CitationInspectionResult(
        status=CitationInspectionStatus.UNAVAILABLE,
        reason=CitationInspectionReason.CITATION_UNAVAILABLE,
    )

    passed = evaluate_email_citation_resolution(
        unavailable,
        expected_status=CitationInspectionStatus.UNAVAILABLE,
        vector_id="wrong-range-v1",
        citation_id=UUID(int=1),
        workspace_id=UUID(int=2),
        environment_id=UUID(int=3),
    )
    failed = evaluate_email_citation_resolution(
        unavailable,
        expected_status=CitationInspectionStatus.AVAILABLE,
        vector_id="plain-range-v1",
        citation_id=UUID(int=1),
        workspace_id=UUID(int=2),
        environment_id=UUID(int=3),
        locator_contract_version="email-attachment-locator:v1",
    )

    assert (passed.verdict, passed.score) == (EvaluationVerdict.PASS, 1.0)
    assert (failed.verdict, failed.score) == (EvaluationVerdict.FAIL, 0.0)
    assert failed.details["locator_contract_version"] == "email-attachment-locator:v1"
    assert "excerpt" not in passed.model_dump_json()


def test_attachment_resolution_vectors_have_unique_pinned_identities() -> None:
    assert EMAIL_ATTACHMENT_CITATION_VECTORS[0].vector_id == (
        "attachment_exact_lineage"
    )
    assert EMAIL_ATTACHMENT_CITATION_VECTORS[0].expected_status is (
        CitationInspectionStatus.AVAILABLE
    )
    attack_vectors = EMAIL_ATTACHMENT_CITATION_VECTORS[1:]
    assert len(attack_vectors) == 9
    assert len({vector.vector_id for vector in EMAIL_ATTACHMENT_CITATION_VECTORS}) == 10
    assert all(
        vector.expected_status is CitationInspectionStatus.UNAVAILABLE
        for vector in attack_vectors
    )
