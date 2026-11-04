from uuid import UUID

from spine.domain.disclosure import (
    CitationInspectionReason,
    CitationInspectionResult,
    CitationInspectionStatus,
)
from spine.domain.evaluations import EvaluationVerdict
from spine.evaluations.email_citation_resolution import (
    evaluate_email_citation_resolution,
)


def test_email_citation_resolution_evaluator_records_vector_outcome_without_content() -> None:
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
    )

    assert (passed.verdict, passed.score) == (EvaluationVerdict.PASS, 1.0)
    assert (failed.verdict, failed.score) == (EvaluationVerdict.FAIL, 0.0)
    assert "excerpt" not in passed.model_dump_json()
