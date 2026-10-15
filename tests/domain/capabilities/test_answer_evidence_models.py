from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.domain.capabilities import (
    AnswerClaim,
    AnswerClaimKind,
    AnswerClaimLocation,
    AnswerContent,
    Citation,
    ClaimEvidenceRelationship,
    ClaimEvidenceRelationshipKind,
)
from spine.domain.context_bundles import (
    ContextBundleEvidenceReference,
    ContextBundleLineageRef,
    ContextBundleRef,
)


WORKSPACE = UUID(int=1)
ENVIRONMENT = UUID(int=2)
RUN = UUID(int=3)
ATTEMPT = UUID(int=4)
ANSWER = UUID(int=5)
BUNDLE = UUID(int=6)
SNAPSHOT = UUID(int=7)
CLAIM_A = UUID(int=10)
CLAIM_B = UUID(int=11)
CITATION_A = UUID(int=20)
CITATION_B = UUID(int=21)


def claim(
    *,
    claim_id: UUID = CLAIM_A,
    text: str = "Alpha",
    start: int = 0,
    end: int = 5,
    kind: AnswerClaimKind = AnswerClaimKind.FACTUAL,
    **overrides: object,
) -> AnswerClaim:
    values: dict[str, object] = {
        "claim_id": claim_id,
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "run_id": RUN,
        "attempt_id": ATTEMPT,
        "kind": kind,
        "text": text,
        "location": AnswerClaimLocation(
            answer_id=ANSWER,
            start_offset=start,
            end_offset=end,
        ),
    }
    values.update(overrides)
    return AnswerClaim(**values)


def citation(
    *,
    citation_id: UUID = CITATION_A,
    evidence_id: str = "evidence-a",
    source_object_id: UUID = UUID(int=30),
    source_revision_id: UUID = UUID(int=31),
    **overrides: object,
) -> Citation:
    values: dict[str, object] = {
        "citation_id": citation_id,
        "answer_id": ANSWER,
        "context_bundle": ContextBundleLineageRef(
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
        "evidence_reference": ContextBundleEvidenceReference(
            evidence_id=evidence_id,
            workspace_id=WORKSPACE,
            environment_id=ENVIRONMENT,
            projection_snapshot_id=SNAPSHOT,
            activation_generation=1,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            revision_digest="b" * 64,
            processing_receipt_id=UUID(int=32),
            locator_contract_version="locator:v1",
            locator={"kind": "page", "value": "1"},
            evidence_digest="c" * 64,
        ),
    }
    values.update(overrides)
    return Citation(**values)


def relationship(
    *,
    relationship_id: UUID = UUID(int=40),
    claim_id: UUID = CLAIM_A,
    kind: ClaimEvidenceRelationshipKind = ClaimEvidenceRelationshipKind.INDEPENDENT,
    citation_ids: tuple[UUID, ...] = (CITATION_A,),
    related_claim_id: UUID | None = None,
    **overrides: object,
) -> ClaimEvidenceRelationship:
    values: dict[str, object] = {
        "relationship_id": relationship_id,
        "answer_id": ANSWER,
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "run_id": RUN,
        "attempt_id": ATTEMPT,
        "claim_id": claim_id,
        "kind": kind,
        "citation_ids": citation_ids,
        "related_claim_id": related_claim_id,
    }
    values.update(overrides)
    return ClaimEvidenceRelationship(**values)


def content(**overrides: object) -> AnswerContent:
    values: dict[str, object] = {
        "answer_id": ANSWER,
        "workspace_id": WORKSPACE,
        "environment_id": ENVIRONMENT,
        "run_id": RUN,
        "attempt_id": ATTEMPT,
        "text": "Alpha",
        "claims": (claim(),),
        "citations": (citation(),),
        "relationships": (relationship(),),
    }
    values.update(overrides)
    return AnswerContent(**values)


def test_single_source_answer_models_are_explicitly_versioned() -> None:
    answer = content()

    assert answer.schema_version == "answer-content:v1"
    assert answer.claims[0].schema_version == "answer-claim:v1"
    assert answer.citations[0].schema_version == "citation:v1"
    assert answer.relationships[0].schema_version == "claim-evidence-relationship:v1"


def test_answer_claim_is_immutable() -> None:
    answer = content()

    with pytest.raises(ValidationError):
        answer.claims[0].text = "Changed"  # type: ignore[misc]


def test_citation_rejects_presentation_marker_as_canonical_data() -> None:
    answer = content()

    with pytest.raises(ValidationError):
        Citation.model_validate({**answer.citations[0].model_dump(), "marker": "[1]"})


def test_multi_source_independent_and_corroborating_evidence_is_explicit() -> None:
    second = citation(
        citation_id=CITATION_B,
        evidence_id="evidence-b",
        source_object_id=UUID(int=33),
        source_revision_id=UUID(int=34),
    )

    answer = content(
        citations=(citation(), second),
        relationships=(
            relationship(),
            relationship(
                relationship_id=UUID(int=41),
                kind=ClaimEvidenceRelationshipKind.CORROBORATING,
                citation_ids=(CITATION_B,),
            ),
        ),
    )

    assert [item.kind for item in answer.relationships] == [
        ClaimEvidenceRelationshipKind.INDEPENDENT,
        ClaimEvidenceRelationshipKind.CORROBORATING,
    ]


def test_corroborating_evidence_without_primary_support_is_rejected() -> None:
    with pytest.raises(ValidationError, match="independent or joint"):
        content(
            relationships=(
                relationship(kind=ClaimEvidenceRelationshipKind.CORROBORATING),
            )
        )


def test_joint_evidence_groups_all_required_contributors() -> None:
    answer = content(
        citations=(citation(), citation(citation_id=CITATION_B, evidence_id="evidence-b")),
        relationships=(
            relationship(
                kind=ClaimEvidenceRelationshipKind.JOINT,
                citation_ids=(CITATION_A, CITATION_B),
            ),
        ),
    )

    assert answer.relationships[0].citation_ids == (CITATION_A, CITATION_B)


def test_joint_evidence_rejects_one_contributor() -> None:
    with pytest.raises(ValidationError, match="joint"):
        relationship(kind=ClaimEvidenceRelationshipKind.JOINT)


def test_derived_claim_accepts_explicit_derivation_inputs() -> None:
    answer = content(
        claims=(claim(kind=AnswerClaimKind.DERIVED),),
        relationships=(
            relationship(kind=ClaimEvidenceRelationshipKind.DERIVATION_INPUT),
        ),
    )

    assert answer.claims[0].kind is AnswerClaimKind.DERIVED


def test_derived_claim_rejects_independent_evidence_relationship() -> None:
    with pytest.raises(ValidationError, match="derived claim"):
        content(claims=(claim(kind=AnswerClaimKind.DERIVED),))


def opposing_claim_and_citation() -> tuple[AnswerClaim, Citation]:
    return (
        claim(claim_id=CLAIM_B, text="Beta", start=6, end=10),
        citation(citation_id=CITATION_B, evidence_id="evidence-b"),
    )


def test_contradiction_links_distinct_grounded_claims_not_opposing_evidence() -> None:
    opposing_claim, opposing_citation = opposing_claim_and_citation()

    answer = content(
        text="Alpha Beta",
        claims=(claim(), opposing_claim),
        citations=(citation(), opposing_citation),
        relationships=(
            relationship(),
            relationship(
                relationship_id=UUID(int=41),
                claim_id=CLAIM_B,
                citation_ids=(CITATION_B,),
            ),
            relationship(
                relationship_id=UUID(int=42),
                kind=ClaimEvidenceRelationshipKind.CONTRADICTION,
                citation_ids=(),
                related_claim_id=CLAIM_B,
            ),
        ),
    )

    assert answer.relationships[-1].related_claim_id == CLAIM_B


def test_contradiction_requires_a_related_claim() -> None:
    with pytest.raises(ValidationError, match="contradiction"):
        relationship(kind=ClaimEvidenceRelationshipKind.CONTRADICTION)


def test_symmetric_duplicate_contradiction_is_rejected() -> None:
    opposing_claim, opposing_citation = opposing_claim_and_citation()

    with pytest.raises(ValidationError, match="duplicate claim evidence relationship"):
        content(
            text="Alpha Beta",
            claims=(claim(), opposing_claim),
            citations=(citation(), opposing_citation),
            relationships=(
                relationship(),
                relationship(
                    relationship_id=UUID(int=41),
                    claim_id=CLAIM_B,
                    citation_ids=(CITATION_B,),
                ),
                relationship(
                    relationship_id=UUID(int=42),
                    kind=ClaimEvidenceRelationshipKind.CONTRADICTION,
                    citation_ids=(),
                    related_claim_id=CLAIM_B,
                ),
                relationship(
                    relationship_id=UUID(int=43),
                    claim_id=CLAIM_B,
                    kind=ClaimEvidenceRelationshipKind.CONTRADICTION,
                    citation_ids=(),
                    related_claim_id=CLAIM_A,
                ),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("workspace_id", UUID(int=99), "workspace or environment"),
        ("environment_id", UUID(int=99), "workspace or environment"),
        ("attempt_id", UUID(int=99), "run or attempt"),
    ),
)
def test_answer_rejects_cross_scope_citations(
    field: str, value: UUID, message: str
) -> None:
    foreign_lineage = citation().context_bundle.model_copy(update={field: value})
    with pytest.raises(ValidationError, match=message):
        content(citations=(citation(context_bundle=foreign_lineage),))


def test_citation_rejects_cross_tenant_persisted_evidence_reference() -> None:
    foreign_evidence = citation().evidence_reference.model_copy(
        update={"workspace_id": UUID(int=99)}
    )

    with pytest.raises(ValidationError, match="mixed workspace or environment"):
        citation(evidence_reference=foreign_evidence)


def test_citation_requires_complete_revision_lineage() -> None:
    incomplete_evidence = citation().evidence_reference.model_copy(
        update={"revision_digest": None}
    )

    with pytest.raises(ValidationError, match="complete revision and locator lineage"):
        citation(evidence_reference=incomplete_evidence)


def test_answer_rejects_mixed_context_bundles() -> None:
    foreign_bundle = citation().context_bundle.model_copy(
        update={
            "bundle": ContextBundleRef(
                bundle_id=UUID(int=99),
                bundle_version=1,
                bundle_digest="d" * 64,
            )
        }
    )
    with pytest.raises(ValidationError, match="one exact context bundle"):
        content(
            citations=(
                citation(),
                citation(
                    citation_id=CITATION_B,
                    evidence_id="evidence-b",
                    context_bundle=foreign_bundle,
                ),
            ),
            relationships=(
                relationship(),
                relationship(relationship_id=UUID(int=41), citation_ids=(CITATION_B,)),
            ),
        )


def test_answer_rejects_duplicate_relationship() -> None:
    with pytest.raises(ValidationError, match="duplicate claim evidence relationship"):
        content(
            relationships=(
                relationship(),
                relationship(relationship_id=UUID(int=41)),
            )
        )


def test_answer_rejects_duplicate_citation_evidence_identity() -> None:
    with pytest.raises(ValidationError, match="duplicate citation evidence identity"):
        content(
            citations=(citation(), citation(citation_id=CITATION_B)),
            relationships=(
                relationship(),
                relationship(relationship_id=UUID(int=41), citation_ids=(CITATION_B,)),
            ),
        )


def test_citation_identity_cannot_be_reassigned_to_different_evidence() -> None:
    reassigned = citation(citation_id=CITATION_A, evidence_id="evidence-b")

    with pytest.raises(ValidationError, match="citation identifiers must be unique"):
        content(
            citations=(citation(), reassigned),
            relationships=(relationship(),),
        )


def test_answer_rejects_multiple_roles_for_one_claim_citation_pair() -> None:
    with pytest.raises(ValidationError, match="multiple evidence roles"):
        content(
            relationships=(
                relationship(),
                relationship(
                    relationship_id=UUID(int=41),
                    kind=ClaimEvidenceRelationshipKind.CORROBORATING,
                ),
            )
        )


def test_claim_location_is_bound_to_the_accepted_answer() -> None:
    with pytest.raises(ValidationError, match="accepted answer"):
        content(
            claims=(
                claim(
                    location=AnswerClaimLocation(
                        answer_id=UUID(int=99),
                        start_offset=0,
                        end_offset=5,
                    )
                ),
            )
        )


def test_relocating_claim_does_not_change_claim_identity() -> None:
    relocated = claim(
        claim_id=CLAIM_A,
        location=AnswerClaimLocation(
            answer_id=ANSWER,
            start_offset=6,
            end_offset=11,
        ),
    )
    assert relocated.claim_id == CLAIM_A


def test_citation_is_bound_to_the_accepted_answer() -> None:
    with pytest.raises(ValidationError, match="different accepted answer"):
        content(citations=(citation(answer_id=UUID(int=99)),))


def test_immutable_replay_round_trips() -> None:
    answer = content()

    replayed = AnswerContent.model_validate_json(answer.canonical_json())

    assert replayed == answer


def test_canonical_replay_contains_no_presentation_markers() -> None:
    replayed = AnswerContent.model_validate_json(content().canonical_json())

    assert "marker" not in replayed.canonical_json()
