"""Application adapter connecting accepted Q&A results to resumable events."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from spine.application.authorized_ask import AuthorizedAskResult
from spine.application.diagnostics import DiagnosticContext
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationOutcome,
    OperationalOperation,
    OperationalStage,
)
from spine.application.qa_events import (
    DisclosureAuthorizer,
    QAEventLogPort,
    QAEventStream,
    ResumeCursorError,
)
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.qa_events import (
    AbstainedPayload,
    AnswerDeltaPayload,
    CancelledPayload,
    CitationAddedPayload,
    CompletedPayload,
    DeniedPayload,
    DisclosureReceipt,
    FailedPayload,
    QAEvent,
    QAEventType,
    QACitation,
    QACitationLocator,
    ClarificationRequiredPayload,
    RunStartedPayload,
    StageChangedPayload,
)


class QAEventLogStore(QAEventLogPort, Protocol):
    """Store operations needed by the production Q&A event adapter."""

    async def terminal(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
    ) -> QAEvent | None: ...

    def open_stream(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        trace_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        disclosure_authorizer: DisclosureAuthorizer | None = None,
    ) -> QAEventStream: ...


class QAResultEventPublisher(Protocol):
    """Seam used by the Ask API to persist its accepted result events."""

    async def publish_result(self, result: AuthorizedAskResult) -> QAEvent | None: ...


DisclosureAuthorization = Callable[
    [DisclosureReceipt], bool | Awaitable[bool]
]


@dataclass(frozen=True, slots=True)
class AuthorizedQAStreamService:
    """Publish only the terminal Ask API result into the Q&A event log.

    The Ask API remains the owner of generation and disclosure.  This adapter
    only translates its already accepted result into the transport-neutral
    event contract and supplies a fresh authorizer to each delivery stream.
    """

    event_log: QAEventLogStore
    disclosure_authorizer: DisclosureAuthorization | None = None
    operational_observer: OperationalObservationEmitter | None = None

    async def publish_result(self, result: AuthorizedAskResult) -> QAEvent | None:
        """Persist one deterministic event sequence, preserving cancellation races."""

        output = result.output
        scope = output.scope
        run_id = output.correlation.run_id
        existing_terminal = await self.event_log.terminal(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
            session_id=result.session_id,
            message_id=result.message_id,
            run_id=run_id,
        )
        if existing_terminal is not None:
            return existing_terminal

        events = _events_for_result(result)
        persisted: QAEvent | None = None
        try:
            for event in events:
                persisted = await self.event_log.append(event)
        except ResumeCursorError:
            # A concurrent completion/cancellation may have won after the
            # initial terminal check.  The winner is the sole terminal event.
            winner = await self.event_log.terminal(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                session_id=result.session_id,
                message_id=result.message_id,
                run_id=run_id,
            )
            if winner is not None:
                return winner
            raise
        if self.operational_observer is not None and persisted is not None:
            correlation = output.correlation
            await self.operational_observer.emit(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                diagnostic_context=DiagnosticContext(
                    trace_id=correlation.trace_id,
                    correlation_id=correlation.correlation_id,
                    causation_id=correlation.causation_id,
                ),
                component=OperationalComponent.SSE,
                stage=OperationalStage.DELIVERY,
                operation=OperationalOperation.SSE_DELIVERY,
                outcome=OperationalObservationOutcome.SUCCEEDED,
                run_id=run_id,
                attempt_id=correlation.attempt_id,
                message_id=result.message_id,
            )
        return persisted

    def open_stream(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        session_id: UUID,
        trace_id: UUID,
        run_id: UUID | None = None,
        message_id: UUID | None = None,
        disclosure_authorizer: DisclosureAuthorizer | None = None,
    ) -> QAEventStream:
        authorizer = disclosure_authorizer or self.disclosure_authorizer
        if authorizer is None:
            raise RuntimeError("Q&A stream disclosure authorizer is unavailable")
        return self.event_log.open_stream(
            workspace_id=workspace_id,
            environment_id=environment_id,
            session_id=session_id,
            trace_id=trace_id,
            run_id=run_id,
            message_id=message_id,
            disclosure_authorizer=authorizer,
        )


def _events_for_result(result: AuthorizedAskResult) -> tuple[QAEvent, ...]:
    output = result.output
    scope = output.scope
    correlation = output.correlation
    common = {
        "session_id": result.session_id,
        "workspace_id": scope.workspace_id,
        "environment_id": scope.environment_id,
        "message_id": result.message_id,
        "run_id": correlation.run_id,
        "trace_id": correlation.trace_id,
    }
    events: list[QAEvent] = [
        _event(
            result=result,
            sequence=1,
            event_type=QAEventType.RUN_STARTED,
            payload=RunStartedPayload(stage="accepted"),
            **common,
        ),
        _event(
            result=result,
            sequence=2,
            event_type=QAEventType.STAGE_CHANGED,
            payload=StageChangedPayload(stage="answering"),
            **common,
        ),
    ]

    if output.outcome is AnswerQuestionOutcome.ANSWERED:
        accepted = result.accepted_answer
        if accepted is None:
            raise ValueError("answered Ask API result has no accepted output")
        receipt = DisclosureReceipt(
            receipt_id=accepted.validation.disclosure_eligibility_reference,
            accepted_output_id=accepted.answer.answer_id,
            validation_reference=accepted.validation.disclosure_eligibility_reference,
        )
        events.append(
            _event(
                result=result,
                sequence=3,
                event_type=QAEventType.ANSWER_DELTA,
                payload=AnswerDeltaPayload(delta=accepted.answer.text, disclosure=receipt),
                **common,
            )
        )
        relationships = {
            citation_id: relationship.claim_id
            for relationship in accepted.answer.relationships
            for citation_id in relationship.citation_ids
        }
        for offset, citation in enumerate(accepted.answer.citations, start=4):
            claim_id = relationships.get(citation.citation_id)
            if claim_id is None:
                raise ValueError("accepted citation has no claim relationship")
            evidence = citation.evidence_reference
            citation_receipt = DisclosureReceipt(
                receipt_id=uuid5(
                    NAMESPACE_URL,
                    f"spine:qa-disclosure:{accepted.answer.answer_id}:{citation.citation_id}",
                ),
                accepted_output_id=accepted.answer.answer_id,
                validation_reference=accepted.validation.disclosure_eligibility_reference,
            )
            events.append(
                _event(
                    result=result,
                    sequence=offset,
                    event_type=QAEventType.CITATION_ADDED,
                    payload=CitationAddedPayload(
                        citation=QACitation(
                            citation_id=citation.citation_id,
                            claim_id=claim_id,
                            answer_id=accepted.answer.answer_id,
                            source_revision_id=evidence.source_revision_id,
                            locator=QACitationLocator(
                                kind=evidence.locator.kind,
                                value=evidence.locator.value,
                            ),
                        ),
                        disclosure=citation_receipt,
                    ),
                    **common,
                )
            )
        terminal_sequence = len(events) + 1
        events.append(
            _event(
                result=result,
                sequence=terminal_sequence,
                event_type=QAEventType.COMPLETED,
                payload=CompletedPayload(outcome="answered"),
                **common,
            )
        )
    elif output.outcome is AnswerQuestionOutcome.CLARIFICATION_REQUIRED:
        if output.clarification is None:
            raise ValueError("clarification result has no prompt")
        events.extend(
            (
                _event(
                    result=result,
                    sequence=3,
                    event_type=QAEventType.CLARIFICATION_REQUIRED,
                    payload=ClarificationRequiredPayload(
                        prompt=output.clarification.prompt
                    ),
                    **common,
                ),
                _event(
                    result=result,
                    sequence=4,
                    event_type=QAEventType.COMPLETED,
                    payload=CompletedPayload(outcome="clarification_required"),
                    **common,
                ),
            )
        )
    else:
        events.append(
            _event(
                result=result,
                sequence=3,
                event_type=_terminal_event_type(output.outcome),
                payload=_terminal_payload(output.outcome),
                **common,
            )
        )
    return tuple(events)


def _event(
    *,
    result: AuthorizedAskResult,
    sequence: int,
    event_type: QAEventType,
    payload: object,
    session_id: UUID,
    workspace_id: UUID,
    environment_id: UUID,
    message_id: UUID,
    run_id: UUID,
    trace_id: UUID,
) -> QAEvent:
    del result
    event_id = uuid5(
        NAMESPACE_URL,
        f"spine:qa-emission:{run_id}:{message_id}:{sequence}:{event_type.value}",
    )
    return QAEvent(
        event_id=event_id,
        sequence=sequence,
        type=event_type,
        session_id=session_id,
        workspace_id=workspace_id,
        environment_id=environment_id,
        message_id=message_id,
        run_id=run_id,
        trace_id=trace_id,
        payload=payload,
    )


def _terminal_event_type(outcome: AnswerQuestionOutcome) -> QAEventType:
    return {
        AnswerQuestionOutcome.ABSTAINED: QAEventType.ABSTAINED,
        AnswerQuestionOutcome.DENIED: QAEventType.DENIED,
        AnswerQuestionOutcome.FAILED: QAEventType.FAILED,
        AnswerQuestionOutcome.CANCELLED: QAEventType.CANCELLED,
    }[outcome]


def _terminal_payload(outcome: AnswerQuestionOutcome) -> object:
    if outcome is AnswerQuestionOutcome.ABSTAINED:
        return AbstainedPayload(reason="insufficient_evidence")
    if outcome is AnswerQuestionOutcome.DENIED:
        return DeniedPayload(reason="authorization_denied")
    if outcome is AnswerQuestionOutcome.FAILED:
        return FailedPayload(reason="mandatory_processing_failed")
    if outcome is AnswerQuestionOutcome.CANCELLED:
        return CancelledPayload(reason="request_cancelled")
    raise ValueError("answered and clarification outcomes are not direct terminal payloads")


__all__ = [
    "AuthorizedQAStreamService",
    "QAEventLogStore",
    "QAResultEventPublisher",
]
