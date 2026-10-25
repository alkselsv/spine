"""Application composition for the final, fail-closed Q&A disclosure gate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from spine.domain.capabilities.answer_disclosure import (
    AnswerDisclosureRecord,
    AnswerDisclosureRequest,
    AnswerDisclosureDecision,
    AcceptedGroundedAnswer,
    AnswerDisclosureRecordV2,
    AnswerDisclosureRecordV3,
    AnswerExecutionCheckpoint,
    execution_checkpoint_for_disclosure,
    CurrentDisclosureEligibility,
    DisclosureEligibilityReason,
    DisclosureEligibilityStatus,
    compose_answer_disclosure,
)
from spine.domain.capabilities.answer_outcomes import AnswerOutcomeResolution
from spine.domain.capabilities import AcceptedAnswerPresentation
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

    async def publish(self, answer: AcceptedGroundedAnswer, *, delivery_id: UUID) -> UUID: ...


class DisclosureFailureObserver(Protocol):
    """Opaque operational hook for recording fail-closed processing failures."""

    async def record_failure(self, request_id: UUID) -> None: ...


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
    safe_terminal_output: AnswerQuestionOutput | None = None

    def to_request(self) -> AnswerDisclosureRequest:
        return AnswerDisclosureRequest(
            request_id=self.request_id,
            decision=build_decision_input(self.outcome_facts),
            current_eligibility=self.current_eligibility,
            target_scope=self.outcome_facts.target_scope,
            observed_coverage=self.outcome_facts.observed_coverage,
            partial_fallback=self.outcome_facts.partial_fallback,
            claim_assessments=self.outcome_facts.claim_assessments,
            conflicts=self.outcome_facts.conflicts,
        )


class InMemoryAnswerDisclosureRepository:
    """Immutable, idempotent repository used by the offline contract suite."""

    def __init__(self) -> None:
        self._records: dict[tuple[UUID, UUID, UUID], AnswerDisclosureRecord] = {}
        self._deliveries: dict[tuple[UUID, UUID, UUID], UUID] = {}

    async def resolve(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> AnswerDisclosureRecord | None:
        return self._records.get((workspace_id, environment_id, request_id))

    async def save(self, record: AnswerDisclosureRecord) -> AnswerDisclosureRecord:
        key = (record.workspace_id, record.environment_id, record.request_id)
        current = self._records.get(key)
        if current is not None:
            if (
                current.request_digest != record.request_digest
                or execution_checkpoint_for_disclosure(current)
                != execution_checkpoint_for_disclosure(record)
            ):
                raise AnswerDisclosureIdempotencyConflict(
                    "Disclosure request conflicts with an immutable result."
                )
            return current
        self._records[key] = record
        return record

    async def resolve_for_attempt(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        run_id: UUID,
        attempt_id: UUID,
    ) -> AnswerDisclosureRecord | None:
        for record in self._records.values():
            if (
                record.workspace_id == workspace_id
                and record.environment_id == environment_id
                and record.run_id == run_id
                and record.attempt_id == attempt_id
            ):
                return record
        return None

    async def resolve_delivery(
        self, *, workspace_id: UUID, environment_id: UUID, request_id: UUID
    ) -> UUID | None:
        return self._deliveries.get((workspace_id, environment_id, request_id))

    async def record_delivery(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        request_id: UUID,
        output_reference: UUID,
    ) -> UUID:
        key = (workspace_id, environment_id, request_id)
        current = self._deliveries.get(key)
        if current is not None and current != output_reference:
            raise AnswerDisclosureIdempotencyConflict(
                "Disclosure publication was recorded with a different output reference."
            )
        self._deliveries[key] = output_reference
        return output_reference

    async def resolve_citation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        citation_id: UUID,
    ) -> AcceptedGroundedAnswer | None:
        for record in self._records.values():
            if (
                record.workspace_id == workspace_id
                and record.environment_id == environment_id
                and record.accepted_answer is not None
                and any(
                    citation.citation_id == citation_id
                    for citation in record.accepted_answer.answer.citations
                )
            ):
                return record.accepted_answer
        return None


class InMemoryAcceptedAnswerOutputPort:
    """Small sink fake proving only accepted content crosses the port."""

    def __init__(self, reference_factory: Callable[[], UUID] = uuid4) -> None:
        self._reference_factory = reference_factory
        self.published: list[AcceptedGroundedAnswer] = []
        self._references: dict[UUID, UUID] = {}

    async def publish(self, answer: AcceptedGroundedAnswer, *, delivery_id: UUID) -> UUID:
        current = self._references.get(delivery_id)
        if current is not None:
            return current
        reference = self._reference_factory()
        self.published.append(answer)
        self._references[delivery_id] = reference
        return reference


@dataclass(frozen=True, slots=True)
class AnswerDisclosureGate:
    repository: AnswerDisclosureRepository
    accepted_output: AcceptedAnswerOutputPort
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    failure_observer: DisclosureFailureObserver | None = None

    async def disclose(self, request: AnswerDisclosureRequest) -> AnswerDisclosureResult:
        """Preserve the legacy prepare-and-deliver operation.

        Durable Q&A must call :meth:`prepare_facts`, commit its canonical
        attempt/run/message transaction, and only then deliver the accepted
        result through the authorized message-read boundary.
        """

        prepared = await self._prepare(request)
        if prepared.accepted_answer is None:
            return prepared
        reference = await self.repository.resolve_delivery(
            workspace_id=prepared.record.workspace_id,
            environment_id=prepared.record.environment_id,
            request_id=prepared.record.request_id,
        )
        if reference is None:
            reference = await self._publish_and_record(prepared.record)
        return AnswerDisclosureResult(
            record=prepared.record,
            replay=prepared.replay,
            output_reference=reference,
        )

    async def prepare_facts(
        self,
        facts: AnswerDisclosureFacts,
        *,
        execution_checkpoint: AnswerExecutionCheckpoint | None = None,
    ) -> AnswerDisclosureResult:
        """Persist the immutable gate decision without external delivery."""

        return await self._prepare(
            facts.to_request(),
            execution_checkpoint=execution_checkpoint,
            safe_terminal_output=facts.safe_terminal_output,
        )

    async def prepare(
        self,
        request: AnswerDisclosureRequest,
        *,
        execution_checkpoint: AnswerExecutionCheckpoint | None = None,
    ) -> AnswerDisclosureResult:
        """Persist a precomposed gate request without external delivery."""

        return await self._prepare(request, execution_checkpoint=execution_checkpoint)

    async def deliver_committed(
        self,
        record: AnswerDisclosureRecord,
        presentation: AcceptedAnswerPresentation,
    ) -> UUID | None:
        """Publish accepted content only after its canonical message is committed.

        ``presentation`` is produced by a fresh authorized Q&A message read.
        This method binds it to the immutable disclosure before using the
        existing idempotent output port.
        """

        if record.accepted_answer is None:
            return None
        answer = record.accepted_answer.answer
        if (
            presentation.run_id != record.run_id
            or presentation.attempt_id != record.attempt_id
            or presentation.answer_id != answer.answer_id
            or presentation.answer_text_sha256
            != sha256(answer.text.encode("utf-8")).hexdigest()
        ):
            raise AnswerDisclosureIdempotencyConflict(
                "Committed Q&A presentation conflicts with its disclosure."
            )
        persisted = await self.repository.resolve(
            workspace_id=record.workspace_id,
            environment_id=record.environment_id,
            request_id=record.request_id,
        )
        if persisted != record:
            raise AnswerDisclosureIdempotencyConflict(
                "Committed Q&A disclosure is unavailable."
            )
        reference = await self.repository.resolve_delivery(
            workspace_id=record.workspace_id,
            environment_id=record.environment_id,
            request_id=record.request_id,
        )
        if reference is None:
            reference = await self._publish_and_record(record)
        return reference

    async def _prepare(
        self,
        request: AnswerDisclosureRequest,
        *,
        execution_checkpoint: AnswerExecutionCheckpoint | None = None,
        safe_terminal_output: AnswerQuestionOutput | None = None,
    ) -> AnswerDisclosureResult:
        if safe_terminal_output is not None and (
            safe_terminal_output.outcome
            not in {AnswerQuestionOutcome.FAILED, AnswerQuestionOutcome.CANCELLED}
            or safe_terminal_output.answer is not None
            or safe_terminal_output.scope != request.decision.question.scope
            or safe_terminal_output.correlation != request.decision.question.correlation
            or safe_terminal_output.reason is None
        ):
            raise ValueError("safe terminal disclosure output is invalid")
        existing = await self.repository.resolve(
            workspace_id=request.decision.question.scope.workspace_id,
            environment_id=request.decision.question.scope.environment_id,
            request_id=request.request_id,
        )
        if existing is not None:
            if execution_checkpoint is not None and (
                not isinstance(existing, AnswerDisclosureRecordV2)
                or existing.execution_checkpoint != execution_checkpoint
            ):
                raise AnswerDisclosureIdempotencyConflict(
                    "Persisted disclosure execution checkpoint conflicts with this retry."
                )
            if execution_checkpoint is None and existing.accepted_answer is not None and (
                request.current_eligibility is None
                or not request.current_eligibility.can_disclose
            ):
                decision = compose_answer_disclosure(
                    request, accepted_at=self.clock()
                )
                safe_record = _record(
                    request, decision, self.clock(), execution_checkpoint
                )
                return AnswerDisclosureResult(record=safe_record, replay=True)
            if existing.request_digest != request.command_digest:
                raise AnswerDisclosureIdempotencyConflict(
                    "Disclosure request conflicts with an immutable result."
                )
            if safe_terminal_output is not None and existing.output != safe_terminal_output:
                raise AnswerDisclosureIdempotencyConflict(
                    "Persisted safe terminal output conflicts with this retry."
                )
            if existing.accepted_answer is None:
                return AnswerDisclosureResult(record=existing, replay=True)
            return AnswerDisclosureResult(
                record=existing, replay=True
            )

        try:
            if safe_terminal_output is None:
                decision = compose_answer_disclosure(request, accepted_at=self.clock())
            else:
                decision = AnswerDisclosureDecision(
                    resolution=AnswerOutcomeResolution(
                        outcome=safe_terminal_output.outcome,
                        output=safe_terminal_output,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.failure_observer is not None:
                try:
                    await self.failure_observer.record_failure(request.request_id)
                except Exception:
                    pass
            decision = _safe_failure(request)
        record = _record(request, decision, self.clock(), execution_checkpoint)
        stored = await self.repository.save(record)
        if safe_terminal_output is not None and stored.output != safe_terminal_output:
            raise AnswerDisclosureIdempotencyConflict(
                "Persisted safe terminal output conflicts with this retry."
            )
        if stored.accepted_answer is None:
            return AnswerDisclosureResult(record=stored, replay=False)
        return AnswerDisclosureResult(record=stored, replay=False)

    async def _publish_and_record(self, record: AnswerDisclosureRecord) -> UUID:
        assert record.accepted_answer is not None
        reference = await self.accepted_output.publish(
            record.accepted_answer, delivery_id=record.request_id
        )
        return await self.repository.record_delivery(
            workspace_id=record.workspace_id,
            environment_id=record.environment_id,
            request_id=record.request_id,
            output_reference=reference,
        )

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
    run_ids = {fact.run_id for fact in result.facts if fact.run_id is not None}
    attempt_ids = {fact.attempt_id for fact in result.facts if fact.attempt_id is not None}
    return CurrentDisclosureEligibility(
        status=status,
        validation_reference=result.request_id,
        reason=reason,
        workspace_id=result.workspace_id,
        environment_id=result.environment_id,
        run_id=next(iter(run_ids)) if len(run_ids) == 1 else None,
        attempt_id=next(iter(attempt_ids)) if len(attempt_ids) == 1 else None,
        validation_fact_ids=tuple(fact.validation_id for fact in result.facts),
        dependency_ids=tuple(
            fact.dependency_id for fact in result.facts if fact.dependency_id is not None
        ),
    )


def _record(
    request: AnswerDisclosureRequest,
    decision: AnswerDisclosureDecision,
    created_at: datetime,
    execution_checkpoint: AnswerExecutionCheckpoint | None = None,
) -> AnswerDisclosureRecord:
    record_fields = dict(
        request_id=request.request_id,
        request_digest=request.command_digest,
        workspace_id=request.decision.question.scope.workspace_id,
        environment_id=request.decision.question.scope.environment_id,
        run_id=request.decision.question.correlation.run_id,
        attempt_id=request.decision.question.correlation.attempt_id,
        outcome=decision.resolution.outcome,
        validation=decision.validation,
        accepted_answer=decision.accepted_answer,
        safe_output=None if decision.accepted_answer is not None else decision.output,
        terminal_output=decision.output,
        created_at=created_at,
    )
    if execution_checkpoint is not None:
        if (
            request.target_scope is not None
            and request.observed_coverage is not None
            and request.decision.evaluation is not None
        ):
            return AnswerDisclosureRecordV3(
                **record_fields,
                execution_checkpoint=execution_checkpoint,
                target_scope=request.target_scope,
                observed_coverage=request.observed_coverage,
                partial_fallback=request.partial_fallback,
                claim_assessments=request.claim_assessments,
                conflicts=request.conflicts,
                evaluation=request.decision.evaluation,
            )
        return AnswerDisclosureRecordV2(
            **record_fields, execution_checkpoint=execution_checkpoint
        )
    return AnswerDisclosureRecord(**record_fields)


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
    "DisclosureFailureObserver",
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
