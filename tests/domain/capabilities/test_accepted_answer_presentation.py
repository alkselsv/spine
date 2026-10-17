from __future__ import annotations

from hashlib import sha256
from uuid import UUID

from spine.domain.capabilities import (
    AcceptedAnswerPresentation,
    AcceptedAnswerRelationshipPresentation,
    AnswerClaimKind,
    ClaimEvidenceRelationshipKind,
)


def test_accepted_answer_presentation_contains_only_authoritative_identities_and_spans() -> None:
    presentation = AcceptedAnswerPresentation(
        message_id=UUID(int=1),
        run_id=UUID(int=2),
        attempt_id=UUID(int=3),
        answer_id=UUID(int=4),
        answer_text_sha256=sha256("Привет 🌍 é".encode("utf-8")).hexdigest(),
        claims=(
            {
                "claim_id": UUID(int=5),
                "kind": AnswerClaimKind.FACTUAL,
                "start_offset": 0,
                "end_offset": 6,
            },
        ),
        relationships=(
            AcceptedAnswerRelationshipPresentation(
                relationship_id=UUID(int=6),
                claim_id=UUID(int=5),
                kind=ClaimEvidenceRelationshipKind.INDEPENDENT,
                citation_ids=(UUID(int=7),),
            ),
        ),
        citation_ids=(UUID(int=7),),
    )

    assert presentation.schema_version == "accepted-answer-presentation:v1"
    assert presentation.claims[0].start_offset == 0
    assert presentation.relationships[0].citation_ids == (UUID(int=7),)
