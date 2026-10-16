from __future__ import annotations

from uuid import UUID

from spine.evals.offline_runner import _evaluation_bundle_from_domain
from spine.evals.grounded_answer_safety import GroundedAnswerSafetyGate
from spine.evals.qa_r1 import load_r1_dataset
from spine.evaluations.citation_metrics import (
    AnswerClaim,
    AnswerSubmission,
    Citation,
    ContextBundle,
)
from spine.memory.contracts import ContextReference

def test_grounded_answer_safety_rejects_abstention_for_answerable_case() -> None:
    from tests.evals.test_unauthorized_retrieval_safety import _allowed_observation

    case = next(case for case in load_r1_dataset().cases if case.case_id == "allowed-acl")
    observation = _allowed_observation()
    case = case.model_copy(
        update={"workspace_id": UUID(int=1), "environment_id": UUID(int=2)}
    )
    result = GroundedAnswerSafetyGate().evaluate(
        case=case,
        dataset=load_r1_dataset(),
        answer=AnswerSubmission(outcome="abstained"),
        context_bundle=_evaluation_bundle_from_domain(observation.context_bundle, case),
    )

    assert result.status == "failed"
    assert result.violation_codes == ("answer_outcome_mismatch",)


def test_grounded_answer_safety_rejects_unsupported_answer_claim() -> None:
    from tests.evals.test_unauthorized_retrieval_safety import _allowed_observation

    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "allowed-acl")
    observation = _allowed_observation()
    case = case.model_copy(
        update={"workspace_id": UUID(int=1), "environment_id": UUID(int=2)}
    )
    result = GroundedAnswerSafetyGate().evaluate(
        case=case,
        dataset=dataset,
        answer=AnswerSubmission(
            outcome="answered",
            claims=(
                AnswerClaim(
                    claim_id="retention-period",
                    text="The policy expires tomorrow.",
                    citation_ids=("made-up-citation",),
                ),
            ),
        ),
        context_bundle=_evaluation_bundle_from_domain(observation.context_bundle, case),
    )

    assert result.status == "failed"
    assert set(result.violation_codes) == {
        "citation_validation_failed",
        "groundedness_validation_failed",
    }


def test_grounded_answer_safety_preserves_typed_non_answer_outcomes() -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "absent-evidence-abstention")
    result = GroundedAnswerSafetyGate().evaluate(
        case=case,
        dataset=dataset,
        answer=AnswerSubmission(outcome="abstained"),
        context_bundle=ContextBundle(
            bundle_id="none",
            workspace_id=case.workspace_id,
            environment_id=case.environment_id,
        ),
    )

    assert result.status == "passed"
    assert result.violation_codes == ()


def test_grounded_answer_safety_accepts_exact_current_citation_and_claim_support() -> None:
    dataset = load_r1_dataset()
    case = next(case for case in dataset.cases if case.case_id == "allowed-acl")
    expected_claim = case.expected_claims[0]
    revision, evidence = next(
        (revision, evidence)
        for revision in dataset.source_revisions
        for evidence in revision.evidence
        if evidence.evidence_id == expected_claim.evidence_ids[0]
    )
    bundle_id = "persisted-bundle-1"
    context_bundle = ContextBundle(
        bundle_id=bundle_id,
        workspace_id=case.workspace_id,
        environment_id=case.environment_id,
        references=(
            ContextReference(
                source_id=str(revision.source_object_id),
                source_revision_id=revision.revision_id,
                workspace_id=case.workspace_id,
                environment_id=case.environment_id,
                evidence_id=evidence.evidence_id,
                locator=evidence.locator.model_dump(mode="json"),
            ),
        ),
    )
    answer = AnswerSubmission(
        outcome="answered",
        claims=(
            AnswerClaim(
                claim_id=expected_claim.claim_id,
                text=expected_claim.text,
                citation_ids=("citation-current",),
            ),
        ),
        citations=(
            Citation(
                citation_id="citation-current",
                evidence_id=evidence.evidence_id,
                source_object_id=revision.source_object_id,
                source_revision_id=revision.revision_id,
                workspace_id=case.workspace_id,
                environment_id=case.environment_id,
                revision_digest=revision.revision_digest,
                locator=evidence.locator.model_dump(mode="json"),
                context_bundle_id=bundle_id,
            ),
        ),
    )

    result = GroundedAnswerSafetyGate().evaluate(
        case=case,
        dataset=dataset,
        answer=answer,
        context_bundle=context_bundle,
    )

    assert result.status == "passed"
    assert result.violation_codes == ()

    valid_citation = answer.citations[0]
    wrong_locator = valid_citation.model_copy(
        update={"locator": {"kind": "pdf_page", "schema_version": "locator:pdf-page:v1", "page": 2}}
    )
    wrong_locator_result = GroundedAnswerSafetyGate().evaluate(
        case=case,
        dataset=dataset,
        answer=answer.model_copy(update={"citations": (wrong_locator,)}),
        context_bundle=context_bundle,
    )
    assert wrong_locator_result.status == "failed"
    assert "citation_validation_failed" in wrong_locator_result.violation_codes

    inaccessible_case = case.model_copy(
        update={
            "acl": tuple(
                grant.model_copy(update={"read_content": False}) for grant in case.acl
            )
        }
    )
    inaccessible_result = GroundedAnswerSafetyGate().evaluate(
        case=inaccessible_case,
        dataset=dataset,
        answer=answer,
        context_bundle=context_bundle,
    )
    assert inaccessible_result.status == "failed"
    assert "citation_validation_failed" in inaccessible_result.violation_codes
