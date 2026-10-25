"""Disclosure-safe Q&A completeness and grounded-conflict presentation."""

from __future__ import annotations

from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import field_validator

from spine.domain.capabilities.answer_evaluation import (
    AnswerEvaluationSafeReason,
    AnswerPartialityStatus,
    CoverageEvaluationStatus,
    GroundedConflictStatus,
    PartialFallbackAuthority,
)
from spine.domain.common import ValidatedDefinitionModel


class AnswerServingDecisionCode(str, Enum):
    SERVE = "serve"
    WITHHOLD = "withhold"


class AnswerCompletenessPresentation(ValidatedDefinitionModel):
    """Stable codes and opaque version identities; never source membership/counts."""

    schema_version: Literal["answer-completeness-presentation:v1"] = (
        "answer-completeness-presentation:v1"
    )
    target_scope_id: UUID
    target_scope_version: Literal["target-completeness-scope:v1"]
    observed_coverage_id: UUID
    observed_coverage_version: Literal["observed-coverage:v1"]
    coverage_status: CoverageEvaluationStatus
    partiality_status: AnswerPartialityStatus
    conflict_status: GroundedConflictStatus
    partial_fallback_authority: PartialFallbackAuthority | None = None
    serving_decision: AnswerServingDecisionCode
    validation_decision: AnswerEvaluationSafeReason

    @field_validator("target_scope_id", "observed_coverage_id")
    @classmethod
    def validate_identifier(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("completeness presentation identity must be non-zero")
        return value


__all__ = ["AnswerCompletenessPresentation", "AnswerServingDecisionCode"]
