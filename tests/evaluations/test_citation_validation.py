from uuid import UUID

from spine.application.citation_validation import (
    CitationValidationFacts,
    CitationValidationReasonCode,
    RejectedCitation,
    ValidCitation,
)
from spine.domain.context_bundles import PolicyDecisionGeneration
from spine.domain.evaluations import EvaluationVerdict
from spine.evaluations.citation_validation import evaluate_citation_validation


WORKSPACE, ENVIRONMENT, CITATION = UUID(int=1), UUID(int=2), UUID(int=3)


def test_citation_validation_evaluator_records_deterministic_gate_outcome() -> None:
    valid = ValidCitation(
        validation_reference=UUID(int=4),
        facts=CitationValidationFacts(
            citation_id=CITATION,
            context_bundle_id=UUID(int=5),
            context_bundle_version=1,
            context_bundle_digest="a" * 64,
            policy_decision_generations=(
                PolicyDecisionGeneration(
                    source_object_id=UUID(int=8),
                    policy_version_id=UUID(int=9),
                    policy_generation=1,
                    membership_generation=1,
                    authority_generation=1,
                    decision_reference=UUID(int=10),
                ),
            ),
        ),
    )
    rejected = RejectedCitation(
        reason_code=CitationValidationReasonCode.EVIDENCE_INVALIDATED,
        validation_reference=UUID(int=7),
    )

    passed = evaluate_citation_validation(
        valid, subject_id=CITATION, workspace_id=WORKSPACE, environment_id=ENVIRONMENT
    )
    failed = evaluate_citation_validation(
        rejected, subject_id=CITATION, workspace_id=WORKSPACE, environment_id=ENVIRONMENT
    )

    assert (passed.verdict, passed.score) == (EvaluationVerdict.PASS, 1.0)
    assert (failed.verdict, failed.score) == (EvaluationVerdict.FAIL, 0.0)
    assert "source" not in failed.model_dump_json()
