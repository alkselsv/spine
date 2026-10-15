"""Application seam for fail-closed semantic-support validation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol
from uuid import UUID, uuid4

from spine.domain.capabilities.semantic_support import (
    SemanticSupportAssessment,
    SemanticSupportOutcome,
    SemanticSupportReasonCode,
    SemanticSupportRequest,
    SemanticSupportResult,
)


class SemanticSupportValidatorUnavailable(RuntimeError):
    """The configured semantic validator cannot currently be reached."""


class SemanticSupportValidator(Protocol):
    """Provider-neutral seam for a deterministic or model-backed validator."""

    async def assess(self, request: SemanticSupportRequest) -> SemanticSupportAssessment: ...


class SemanticSupportValidationService:
    """Validate claim support and map infrastructure failure to safe outcomes."""

    def __init__(
        self,
        *,
        validator: SemanticSupportValidator,
        validation_record_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._validator = validator
        self._validation_record_id_factory = validation_record_id_factory

    async def validate(self, request: SemanticSupportRequest) -> SemanticSupportResult:
        request = SemanticSupportRequest.model_validate(request, strict=True)
        try:
            raw_assessment = await self._validator.assess(request)
            assessment = SemanticSupportAssessment.model_validate(
                raw_assessment, strict=True
            )
            self._ensure_assessment_matches(request, assessment)
        except asyncio.CancelledError:
            raise
        except SemanticSupportValidatorUnavailable:
            assessment = SemanticSupportAssessment(
                claim_id=request.claim.claim_id,
                outcome=SemanticSupportOutcome.VALIDATOR_UNAVAILABLE,
                reason_code=SemanticSupportReasonCode.VALIDATOR_UNAVAILABLE,
            )
        except Exception:
            assessment = SemanticSupportAssessment(
                claim_id=request.claim.claim_id,
                outcome=SemanticSupportOutcome.VALIDATOR_FAILED,
                reason_code=SemanticSupportReasonCode.VALIDATOR_FAILED,
            )
        return SemanticSupportResult(
            validation_record_id=self._validation_record_id_factory(),
            policy=request.policy,
            scope=request.scope,
            correlation=request.correlation,
            context_bundle=request.context_bundle,
            answer_id=request.claim.location.answer_id,
            claim_id=request.claim.claim_id,
            evidence_ids=tuple(item.citation.evidence_id for item in request.evidence),
            citation_ids=tuple(item.citation.citation_id for item in request.evidence),
            relationship_ids=tuple(item.relationship_id for item in request.relationships),
            outcome=assessment.outcome,
            reason_code=assessment.reason_code,
            disclosure_eligible=assessment.outcome is SemanticSupportOutcome.SUPPORTED,
            qna_failure=request.policy.mandatory
            and assessment.outcome
            in {
                SemanticSupportOutcome.VALIDATOR_UNAVAILABLE,
                SemanticSupportOutcome.VALIDATOR_FAILED,
            },
        )

    @staticmethod
    def _ensure_assessment_matches(
        request: SemanticSupportRequest, assessment: SemanticSupportAssessment
    ) -> None:
        if assessment.claim_id != request.claim.claim_id:
            raise ValueError("semantic validator returned another claim")
        expected_evidence_ids = tuple(item.citation.evidence_id for item in request.evidence)
        expected_relationship_ids = tuple(item.relationship_id for item in request.relationships)
        if assessment.evidence_ids != expected_evidence_ids:
            raise ValueError("semantic validator returned incomplete evidence lineage")
        if assessment.relationship_ids != expected_relationship_ids:
            raise ValueError("semantic validator returned incomplete relationship lineage")
