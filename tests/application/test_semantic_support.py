from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.semantic_support import (
    SemanticSupportValidationService,
    SemanticSupportValidatorUnavailable,
)
from spine.application.testing.semantic_support import (
    DeterministicSemanticSupportValidator,
)
from spine.domain.capabilities import (
    AnswerClaim,
    AnswerClaimLocation,
    Citation,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
    SemanticSupportAssessment,
    SemanticSupportOutcome,
    SemanticSupportPolicy,
    SemanticSupportReasonCode,
    SemanticSupportRequest,
)
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
    ContextBundleRef,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


WORKSPACE = uid(1)
ENVIRONMENT = uid(2)
RUN = uid(3)
ATTEMPT = uid(4)
TRACE = uid(5)
ANSWER = uid(6)
BUNDLE = uid(7)
SNAPSHOT = uid(8)
CLAIM = uid(9)
CITATION_A = uid(10)
CITATION_B = uid(11)
RELATIONSHIP_A = uid(12)
RELATIONSHIP_B = uid(13)
CONTRADICTORY_CLAIM = uid(14)


def citation(*, citation_id: UUID = CITATION_A, evidence_id: str = "evidence-a") -> Citation:
    return Citation(
        citation_id=citation_id,
        answer_id=ANSWER,
        context_bundle=ContextBundleLineageRef(
            bundle=ContextBundleRef(
                bundle_id=BUNDLE,
                bundle_version=1,
                bundle_digest="a" * 64,
            ),
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            run_id=RUN,
            attempt_id=ATTEMPT,
        ),
        evidence_reference=ContextBundleEvidenceReference(
            schema_version="context-evidence-reference:v2",
            evidence_id=evidence_id,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_snapshot_id=SNAPSHOT,
            activation_generation=1,
            source_object_id=uid(20 + citation_id.int),
            source_revision_id=uid(40 + citation_id.int),
            original_sha256="b" * 64,
            revision_digest="c" * 64,
            revision_number=1,
            processing_receipt_id=uid(60 + citation_id.int),
            projected_kind="chunk",
            locator_contract_version="locator:v1",
            locator={"kind": "page", "value": str(citation_id.int)},
            locator_digest="d" * 64,
            evidence_digest="e" * 64,
        ),
    )


def claim() -> AnswerClaim:
    return AnswerClaim(
        claim_id=CLAIM,
        workspace_id=WORKSPACE,
        environment_id=ENVIRONMENT,
        run_id=RUN,
        attempt_id=ATTEMPT,
        text="Alpha is available",
        location=AnswerClaimLocation(
            answer_id=ANSWER,
            start_offset=0,
            end_offset=len("Alpha is available"),
        ),
    )


def request(*, multiple: bool = False) -> SemanticSupportRequest:
    citations = (citation(),)
    relationships = (
        ClaimEvidenceRelationship(
            relationship_id=RELATIONSHIP_A,
            answer_id=ANSWER,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            run_id=RUN,
            attempt_id=ATTEMPT,
            claim_id=CLAIM,
            kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
            citation_ids=(CITATION_A,),
        ),
    )
    evidence = ({"citation": citations[0], "content": "Alpha is available."},)
    if multiple:
        second = citation(citation_id=CITATION_B, evidence_id="evidence-b")
        citations = (citations[0], second)
        relationships = (
            relationships[0],
            ClaimEvidenceRelationship(
                relationship_id=RELATIONSHIP_B,
                answer_id=ANSWER,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                run_id=RUN,
                attempt_id=ATTEMPT,
                claim_id=CLAIM,
                kind=ClaimEvidenceRelationshipKind.CORROBORATING,
                citation_ids=(CITATION_B,),
            ),
            ClaimEvidenceRelationship(
                relationship_id=uid(15),
                answer_id=ANSWER,
                workspace_id=WORKSPACE,
                environment_id=ENVIRONMENT,
                run_id=RUN,
                attempt_id=ATTEMPT,
                claim_id=CLAIM,
                kind=ClaimEvidenceRelationshipKind.CONTRADICTION,
                related_claim_id=CONTRADICTORY_CLAIM,
            ),
        )
        evidence = (
            evidence[0],
            {"citation": second, "content": "Alpha is available to members."},
        )
    return SemanticSupportRequest(
        policy=SemanticSupportPolicy(
            policy_id=uid(70),
            policy_version="semantic-support-policy:1",
            policy_digest="f" * 64,
        ),
        scope={"workspace_id": WORKSPACE, "environment_id": ENVIRONMENT},
        correlation={
            "run_id": RUN,
            "attempt_id": ATTEMPT,
            "trace_id": TRACE,
        },
        context_bundle=citations[0].context_bundle,
        claim=claim(),
        relationships=relationships,
        evidence=evidence,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "eligible"),
    [
        (SemanticSupportOutcome.SUPPORTED, True),
        (SemanticSupportOutcome.UNSUPPORTED, False),
        (SemanticSupportOutcome.INCONCLUSIVE, False),
    ],
)
async def test_semantic_support_outcomes_control_disclosure_eligibility(
    outcome: SemanticSupportOutcome, eligible: bool
) -> None:
    result = await SemanticSupportValidationService(
        validator=DeterministicSemanticSupportValidator(outcome=outcome)
    ).validate(request())

    assert result.outcome is outcome
    assert result.disclosure_eligible is eligible
    assert result.qna_failure is False
    assert result.policy.policy_version == "semantic-support-policy:1"
    assert result.evidence_ids == ("evidence-a",)
    assert result.relationship_ids == (RELATIONSHIP_A,)


@pytest.mark.asyncio
async def test_citation_presence_does_not_substitute_for_semantic_support() -> None:
    result = await SemanticSupportValidationService(
        validator=DeterministicSemanticSupportValidator(
            outcome=SemanticSupportOutcome.UNSUPPORTED,
            reason_code=SemanticSupportReasonCode.UNSUPPORTED_CLAIM,
        )
    ).validate(request())

    assert result.outcome is SemanticSupportOutcome.UNSUPPORTED
    assert result.disclosure_eligible is False


@pytest.mark.asyncio
async def test_validator_unavailable_is_a_mandatory_qna_failure() -> None:
    result = await SemanticSupportValidationService(
        validator=DeterministicSemanticSupportValidator(
            error=SemanticSupportValidatorUnavailable()
        )
    ).validate(request())

    assert result.outcome is SemanticSupportOutcome.VALIDATOR_UNAVAILABLE
    assert result.reason_code is SemanticSupportReasonCode.VALIDATOR_UNAVAILABLE
    assert result.qna_failure is True
    assert result.disclosure_eligible is False


@pytest.mark.asyncio
async def test_validator_failure_is_fail_closed() -> None:
    result = await SemanticSupportValidationService(
        validator=DeterministicSemanticSupportValidator(error=RuntimeError("provider"))
    ).validate(request())

    assert result.outcome is SemanticSupportOutcome.VALIDATOR_FAILED
    assert result.reason_code is SemanticSupportReasonCode.VALIDATOR_FAILED
    assert result.qna_failure is True
    assert result.disclosure_eligible is False


@pytest.mark.asyncio
async def test_malformed_validator_output_is_a_failure_without_provider_detail() -> None:
    class MalformedValidator:
        async def assess(self, value: SemanticSupportRequest) -> object:
            del value
            return {"outcome": "supported"}

    result = await SemanticSupportValidationService(validator=MalformedValidator()).validate(
        request()
    )

    assert result.outcome is SemanticSupportOutcome.VALIDATOR_FAILED
    assert result.reason_code is SemanticSupportReasonCode.VALIDATOR_FAILED
    assert result.disclosure_eligible is False


@pytest.mark.asyncio
async def test_multi_evidence_and_contradictory_relationship_lineage_is_retained() -> None:
    result = await SemanticSupportValidationService(
        validator=DeterministicSemanticSupportValidator()
    ).validate(request(multiple=True))

    assert result.outcome is SemanticSupportOutcome.SUPPORTED
    assert result.evidence_ids == ("evidence-a", "evidence-b")
    assert result.citation_ids == (CITATION_A, CITATION_B)
    assert result.relationship_ids == (RELATIONSHIP_A, RELATIONSHIP_B, uid(15))


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_to_validator_failure() -> None:
    class CancelledValidator:
        async def assess(self, value: SemanticSupportRequest) -> SemanticSupportAssessment:
            del value
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await SemanticSupportValidationService(validator=CancelledValidator()).validate(
            request()
        )


def test_request_rejects_mixed_claim_and_evidence_lineage() -> None:
    evidence = request().evidence[0]

    with pytest.raises(ValidationError):
        SemanticSupportRequest(
            **{
                **request().model_dump(mode="python"),
                "evidence": ({"citation": evidence.citation.model_copy(update={"answer_id": uid(99)}), "content": evidence.content},),
            }
        )
