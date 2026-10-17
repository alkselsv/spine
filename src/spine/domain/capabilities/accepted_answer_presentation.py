"""Disclosure-safe placement data projected from one accepted answer."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from spine.domain.capabilities.answer_question import (
    AnswerClaimKind,
    ClaimEvidenceRelationshipKind,
)
from spine.domain.common import ValidatedDefinitionModel


ACCEPTED_ANSWER_PRESENTATION_SCHEMA = "accepted-answer-presentation:v1"
_DIGEST = r"^[0-9a-f]{64}$"


def _identifier(value: UUID, field_name: str) -> UUID:
    if value.int == 0:
        raise ValueError(f"{field_name} must be non-zero")
    return value


class AcceptedAnswerClaimPresentation(ValidatedDefinitionModel):
    """An authoritative claim identity, kind and code-point span."""

    schema_version: Literal["accepted-answer-claim-presentation:v1"] = (
        "accepted-answer-claim-presentation:v1"
    )
    claim_id: UUID
    kind: AnswerClaimKind
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)

    @field_validator("claim_id")
    @classmethod
    def validate_claim_id(cls, value: UUID) -> UUID:
        return _identifier(value, "claim_id")

    @model_validator(mode="after")
    def validate_span(self) -> "AcceptedAnswerClaimPresentation":
        if self.end_offset <= self.start_offset:
            raise ValueError("claim presentation end must follow its start")
        return self


class AcceptedAnswerRelationshipPresentation(ValidatedDefinitionModel):
    """A typed relationship linking an accepted claim to citation identities."""

    schema_version: Literal["accepted-answer-relationship-presentation:v1"] = (
        "accepted-answer-relationship-presentation:v1"
    )
    relationship_id: UUID
    claim_id: UUID
    kind: ClaimEvidenceRelationshipKind
    citation_ids: tuple[UUID, ...] = ()
    related_claim_id: UUID | None = None

    @field_validator("relationship_id", "claim_id", "related_claim_id")
    @classmethod
    def validate_identifier(cls, value: UUID | None, info: object) -> UUID | None:
        if value is None:
            return None
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(identifier.int == 0 for identifier in value):
            raise ValueError("presentation citation IDs must be non-zero")
        if len(set(value)) != len(value):
            raise ValueError("presentation citation IDs must be unique")
        return value


class AcceptedAnswerPresentation(ValidatedDefinitionModel):
    """Versioned, content-free projection of a persisted accepted answer."""

    schema_version: Literal[ACCEPTED_ANSWER_PRESENTATION_SCHEMA] = (
        ACCEPTED_ANSWER_PRESENTATION_SCHEMA
    )
    message_id: UUID
    run_id: UUID
    attempt_id: UUID
    answer_id: UUID
    answer_text_sha256: str = Field(pattern=_DIGEST)
    claims: tuple[AcceptedAnswerClaimPresentation, ...] = Field(min_length=1)
    relationships: tuple[AcceptedAnswerRelationshipPresentation, ...] = Field(min_length=1)
    citation_ids: tuple[UUID, ...] = Field(min_length=1)

    @field_validator("message_id", "run_id", "attempt_id", "answer_id")
    @classmethod
    def validate_identifier(cls, value: UUID, info: object) -> UUID:
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if any(identifier.int == 0 for identifier in value):
            raise ValueError("presentation citation IDs must be non-zero")
        if len(set(value)) != len(value):
            raise ValueError("presentation citation IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> "AcceptedAnswerPresentation":
        claim_ids = {claim.claim_id for claim in self.claims}
        if len(claim_ids) != len(self.claims):
            raise ValueError("presentation claim IDs must be unique")
        relationship_ids = {item.relationship_id for item in self.relationships}
        if len(relationship_ids) != len(self.relationships):
            raise ValueError("presentation relationship IDs must be unique")
        citations = set(self.citation_ids)
        for relationship in self.relationships:
            if relationship.claim_id not in claim_ids:
                raise ValueError("presentation relationship references unknown claim")
            if relationship.related_claim_id is not None and relationship.related_claim_id not in claim_ids:
                raise ValueError("presentation relationship references unknown related claim")
            if not set(relationship.citation_ids) <= citations:
                raise ValueError("presentation relationship references unknown citation")
        return self


__all__ = [
    "ACCEPTED_ANSWER_PRESENTATION_SCHEMA",
    "AcceptedAnswerClaimPresentation",
    "AcceptedAnswerPresentation",
    "AcceptedAnswerRelationshipPresentation",
]
