"""Application composition for the final, fail-closed Q&A disclosure gate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID, uuid4

from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecord,
    AnswerDisclosureRequest,
    AnswerDisclosureDecision,
    AcceptedGroundedAnswer,
    CurrentDisclosureEligibility,
    DisclosureEligibilityReason,
    DisclosureEligibilityStatus,
    compose_answer_disclosure,
)
from spine.domain.capabilities.answer_question import (
    AnswerQuestionOutput,
    AnswerQuestionOutcome,
    AnswerQuestionSafeReason,
    AnswerQuestionSafeReasonCode,
)
from spine.domain.disclosure import DisclosureRevalidationResult
from spine.application.answer_outcomes import AnswerOutcomeFacts, build_decision_input
from spine.application.persistence.answer_disclosure import (
    AnswerDisclosureRepository,
)


class AnswerDisclosureIdempotencyConflict(RuntimeError):
    """The request identity was reused with different validation facts."""


class AcceptedAnswerOutputPort(Protocol):
    """Transport-neutral port that accepts only post-gate grounded answers."""

    async def publish(self, answer: AcceptedGroundedAnswer) -> UUID: ...


@dataclass(frozen=True, slots=True)
class AnswerDisclosureResult:
    record: AnswerDisclosureRecord
    replay: bool
    output_reference: UUID | None = None

    @property
    def accepted_answer(self) -> AcceptedGroundedAnswer | None:
        return self.record.accepted_answer

    @property
    def safe_output(self) -> AnswerQuestionOutput | None:
        return self.record.safe_output


@dataclass(frozen=True, slots=True)
class AnswerDisclosureFacts:
    """Raw typed stage results composed at the final disclosure seam."""

    request_id: UUID
    outcome_facts: AnswerOutcomeFacts
    current_eligibility: CurrentDisclosureEligibility | None = None

    def to_request(self) -> AnswerDisclosureRequest:
        return AnswerDisclosureRequest(
            request_id=self.request_id,
            decision=build_decision_input(self.outcome_facts),
            current_eligibility=self.current_eligibility,
        )


class InMemoryAnswerDisclosureRepository:
    """Immutable, idempotent repository used by the offline contract suite."""

    def __init__(self) -> None:
        self._records: dict[tuple[UUID, UUID, UUID], AnswerDisclosureRecord] = {}

    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> AnswerDisclosureRecord | None:
        return self._records.get((workspace_id, environment_id, request_id))

    async def save(self, record: AnswerDisclosureRecord) -> AnswerDisclosureRecord:
        key = (record.workspace_id, record.environment_id, record.request_id)
        current = self._records.get(key)
        if current is not None:
            if current.request_digest != record.request_digest:
                raise AnswerDisclosureIdempotencyConflict(
                    "Disclosure request conflicts with an immutable result."
                )
            return current
        self._records[key] = record
        return record


class InMemoryAcceptedAnswerOutputPort:
    """Small sink fake proving only accepted content crosses the port."""

    def __init__(self, reference_factory: Callable[[], UUID] = uuid4) -> None:
        self._reference_factory = reference_factory
        self.published: list[AcceptedGroundedAnswer] = []

    async def publish(self, answer: AcceptedGroundedAnswer) -> UUID:
        self.published.append(answer)
        return self._reference_factory()


@dataclass(frozen=True, slots=True)
class AnswerDisclosureGate:
    repository: AnswerDisclosureRepository
    accepted_output: AcceptedAnswerOutputPort
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    async def disclose(self, request: AnswerDisclosureRequest) -> AnswerDisclosureResult:
        existing = await self.repository.resolve(
            workspace_id=request.decision.question.scope.workspace_id,
            environment_id=request.decision.question.scope.environment_id,
            request_id=request.request_id,
        )
        if existing is not None:
            if existing.request_digest != request.command_digest:
                raise AnswerDisclosureIdempotencyConflict(
                    "Disclosure request conflicts with an immutable result."
                )
            return AnswerDisclosureResult(record=existing, replay=True)

        try:
            decision = compose_answer_disclosure(request, accepted_at=self.clock())
        except asyncio.CancelledError:
            raise
        except Exception:
            decision = _safe_failure(request)
        record = _record(request, decision, self.clock())
        stored = await self.repository.save(record)
        if stored.accepted_answer is None:
            return AnswerDisclosureResult(record=stored, replay=False)
        reference = await self.accepted_output.publish(stored.accepted_answer)
        return AnswerDisclosureResult(record=stored, replay=False, output_reference=reference)

    async def disclose_facts(self, facts: AnswerDisclosureFacts) -> AnswerDisclosureResult:
        """Compose raw validator/evaluator records without exposing candidate output."""

        return await self.disclose(facts.to_request())


def eligibility_from_revalidation(
    result: DisclosureRevalidationResult,
) -> CurrentDisclosureEligibility:
    """Map stored/current authority facts without duplicating authorization policy."""

    if result.can_disclose_content:
        status = DisclosureEligibilityStatus.ELIGIBLE
        reason = DisclosureEligibilityReason.CURRENT_AUTHORITY_CONFIRMED
    elif result.disposition.value in {"ineligible", "cancelled"}:
        status = DisclosureEligibilityStatus.INELIGIBLE
        reason = DisclosureEligibilityReason.CURRENT_AUTHORITY_REJECTED
    else:
        status = DisclosureEligibilityStatus.UNAVAILABLE
        reason = DisclosureEligibilityReason.CURRENT_AUTHORITY_UNAVAILABLE
    return CurrentDisclosureEligibility(
        status=status,
        validation_reference=result.request_id,
        reason=reason,
    )


def _record(
    request: AnswerDisclosureRequest,
    decision: AnswerDisclosureDecision,
    created_at: datetime,
) -> AnswerDisclosureRecord:
    return AnswerDisclosureRecord(
        request_id=request.request_id,
        request_digest=request.command_digest,
        workspace_id=request.decision.question.scope.workspace_id,
        environment_id=request.decision.question.scope.environment_id,
        run_id=request.decision.question.correlation.run_id,
        attempt_id=request.decision.question.correlation.attempt_id,
        outcome=decision.resolution.outcome,
        accepted_answer=decision.accepted_answer,
        safe_output=None if decision.accepted_answer is not None else decision.output,
        created_at=created_at,
    )


def _safe_failure(request: AnswerDisclosureRequest) -> AnswerDisclosureDecision:
    output = AnswerQuestionOutput(
        correlation=request.decision.question.correlation,
        scope=request.decision.question.scope,
        outcome=AnswerQuestionOutcome.FAILED,
        reason=AnswerQuestionSafeReason(
            code=AnswerQuestionSafeReasonCode.MANDATORY_PROCESSING_FAILED
        ),
    )
    from spine.domain.capabilities.answer_outcomes import AnswerOutcomeResolution

    return AnswerDisclosureDecision(
        resolution=AnswerOutcomeResolution(
            outcome=AnswerQuestionOutcome.FAILED,
            output=output,
        )
    )


__all__ = [
    "AcceptedAnswerOutputPort",
    "AnswerDisclosureGate",
    "AnswerDisclosureFacts",
    "AnswerDisclosureIdempotencyConflict",
    "AnswerDisclosureRepository",
    "AnswerDisclosureResult",
    "InMemoryAcceptedAnswerOutputPort",
    "InMemoryAnswerDisclosureRepository",
    "GroundedAnswerDisclosureService",
    "AnswerDisclosureService",
    "eligibility_from_revalidation",
]

GroundedAnswerDisclosureService = AnswerDisclosureGate
AnswerDisclosureService = AnswerDisclosureGate
