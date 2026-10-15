"""Deterministic semantic-support fake for offline contract tests."""

from __future__ import annotations

from typing import NoReturn

from spine.application.semantic_support import (
    SemanticSupportValidatorUnavailable,
)
from spine.domain.capabilities.semantic_support import (
    _expected_reason_code,
    SemanticSupportAssessment,
    SemanticSupportOutcome,
    SemanticSupportReasonCode,
    SemanticSupportRequest,
)


class DeterministicSemanticSupportValidator:
    """Return a configured outcome while preserving request lineage exactly."""

    def __init__(
        self,
        *,
        outcome: SemanticSupportOutcome = SemanticSupportOutcome.SUPPORTED,
        reason_code: SemanticSupportReasonCode | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._outcome = outcome
        self._reason_code = reason_code
        self._error = error

    async def assess(self, request: SemanticSupportRequest) -> SemanticSupportAssessment:
        if self._error is not None:
            self._raise_configured_error()
        reason_code = self._reason_code or _expected_reason_code(self._outcome)
        return SemanticSupportAssessment(
            claim_id=request.claim.claim_id,
            outcome=self._outcome,
            reason_code=reason_code,
            evidence_ids=tuple(item.citation.evidence_id for item in request.evidence),
            relationship_ids=tuple(item.relationship_id for item in request.relationships),
        )

    def _raise_configured_error(self) -> NoReturn:
        assert self._error is not None
        raise self._error
