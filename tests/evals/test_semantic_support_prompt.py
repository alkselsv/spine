from __future__ import annotations

from hashlib import sha256

import pytest

from spine.domain.capabilities import (
    SemanticSupportAssessment,
    SemanticSupportOutcome,
    SemanticSupportReasonCode,
)
from spine.evals.semantic_support_prompt import (
    SEMANTIC_SUPPORT_EVAL_DATASET_VERSION,
    load_semantic_support_prompt_dataset,
    run_semantic_support_prompt_evaluation,
)
from spine.infrastructure.qa_agent_runtime import (
    SEMANTIC_SUPPORT_POLICY_BINDING,
    SEMANTIC_SUPPORT_POLICY_DIGEST,
    _SEMANTIC_SUPPORT_SYSTEM_PROMPT,
)


class LabelledValidator:
    async def assess(self, request):  # type: ignore[no-untyped-def]
        claim = request.claim.text
        outcomes = {
            "ALPHA IS AVAILABLE": SemanticSupportOutcome.SUPPORTED,
            "Members can use Alpha.": SemanticSupportOutcome.SUPPORTED,
            "Alpha is available worldwide.": SemanticSupportOutcome.UNSUPPORTED,
            "The evidence does not mention Beta.": SemanticSupportOutcome.INCONCLUSIVE,
        }
        outcome = outcomes[claim]
        reasons = {
            SemanticSupportOutcome.SUPPORTED: SemanticSupportReasonCode.SUPPORTED_BY_EVIDENCE,
            SemanticSupportOutcome.UNSUPPORTED: SemanticSupportReasonCode.UNSUPPORTED_CLAIM,
            SemanticSupportOutcome.INCONCLUSIVE: SemanticSupportReasonCode.INSUFFICIENT_OR_CONTRADICTORY_EVIDENCE,
        }
        return SemanticSupportAssessment(
            claim_id=request.claim.claim_id,
            outcome=outcome,
            reason_code=reasons[outcome],
            evidence_ids=tuple(item.citation.evidence_id for item in request.evidence),
            relationship_ids=tuple(
                item.relationship_id for item in request.relationships
            ),
        )


def test_semantic_support_dataset_pins_current_policy_prompt() -> None:
    dataset = load_semantic_support_prompt_dataset()

    assert dataset.dataset_version == SEMANTIC_SUPPORT_EVAL_DATASET_VERSION
    assert dataset.policy_binding == "qa.semantic-support:r2"
    assert dataset.prompt_digest == sha256(
        _SEMANTIC_SUPPORT_SYSTEM_PROMPT.encode("utf-8")
    ).hexdigest()
    assert SEMANTIC_SUPPORT_POLICY_BINDING == dataset.policy_binding
    assert SEMANTIC_SUPPORT_POLICY_DIGEST == dataset.prompt_digest
    assert {case.scenario for case in dataset.cases} == {
        "normalized_direct_match",
        "faithful_paraphrase",
        "partial_support",
        "unsupported_absence_claim",
    }


@pytest.mark.asyncio
async def test_semantic_support_evaluation_scores_human_labelled_cases() -> None:
    report = await run_semantic_support_prompt_evaluation(
        validator=LabelledValidator()
    )

    assert report.dataset_version == SEMANTIC_SUPPORT_EVAL_DATASET_VERSION
    assert report.policy_binding == "qa.semantic-support:r2"
    assert report.total_cases == 4
    assert report.passed_cases == 4
    assert report.failed_case_ids == ()


@pytest.mark.asyncio
async def test_semantic_support_evaluation_reports_regression() -> None:
    class AlwaysSupported(LabelledValidator):
        async def assess(self, request):  # type: ignore[no-untyped-def]
            assessment = await super().assess(request)
            return assessment.model_copy(
                update={
                    "outcome": SemanticSupportOutcome.SUPPORTED,
                    "reason_code": SemanticSupportReasonCode.SUPPORTED_BY_EVIDENCE,
                }
            )

    report = await run_semantic_support_prompt_evaluation(
        validator=AlwaysSupported()
    )

    assert report.passed_cases == 2
    assert report.failed_case_ids == (
        "partial-support",
        "unsupported-absence-claim",
    )
