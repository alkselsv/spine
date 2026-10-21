"""Protected SSE and polling delivery for persisted Q&A events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, FastAPI, Header, Query, Request
from starlette.responses import StreamingResponse

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.application.authorized_ask import InvalidAuthorizedAskRequest
from spine.application.diagnostics import DiagnosticContext
from spine.application.qa_events import DisclosureAuthorizer, ResumeCursorError
from spine.application.qa_streaming import QAEventStream, QAResultEventPublisher
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationOutcome,
    OperationalOperation,
    OperationalStage,
    SafeOperationalFailureCode,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.qa_events import (
    AbstainedPayload,
    CancelledPayload,
    CompletedPayload,
    DeniedPayload,
    DisclosureReceipt,
    FailedPayload,
    QAEvent,
    QAEventType,
)
from spine.application.persistence import (
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.qa_sessions import QASessionRequester
from spine.domain.capabilities import AnswerQuestionOutcome
from spine.domain.qa_sessions import QAMessageState


QAStreamDisclosureAuthorizer = Callable[
    [DisclosureReceipt, AuthorizedRequestContext[object]],
    bool | Awaitable[bool],
]


class QAStreamReader(QAResultEventPublisher):
    """Application seam required by the HTTP stream adapter."""

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


def install_qa_stream_routes(
    application: FastAPI,
    *,
    service: QAStreamReader,
    disclosure_authorizer: QAStreamDisclosureAuthorizer | None = None,
    operational_observer: OperationalObservationEmitter | None = None,
) -> None:
    """Register the scope-bound Q&A SSE/polling event route."""

    application.state.qa_stream_service = service

    async def events_endpoint(
        session_id: UUID,
        message_id: UUID,
        request: Request,
        run_id: UUID | None = Query(None),
        last_sequence: int | None = Query(None, ge=0),
        limit: int = Query(100, ge=1, le=100),
        poll: bool = Query(False),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
        persistence_context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> StreamingResponse:
        async def authorize(receipt: DisclosureReceipt) -> bool:
            if disclosure_authorizer is None:
                return True
            decision = disclosure_authorizer(receipt, request_context)
            return bool(await decision) if isinstance(decision, Awaitable) else bool(decision)

        stream_authorizer = authorize if disclosure_authorizer is not None else None
        stream = _service(request).open_stream(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            trace_id=request_context.diagnostic_context.trace_id,
            disclosure_authorizer=stream_authorizer,
        )
        try:
            replay_method = stream.poll if poll else stream.replay
            replay = await replay_method(
                last_event_id=last_event_id,
                last_sequence=last_sequence,
                limit=limit,
            )
        except ResumeCursorError as exc:
            raise InvalidAuthorizedAskRequest() from exc

        terminal = replay.terminal
        if not replay.events and terminal is None:
            terminal = await _canonical_terminal_event(
                request=request,
                persistence_context=persistence_context,
                request_context=request_context,
                session_id=session_id,
                message_id=message_id,
                run_id=run_id,
            )
        events = replay.events
        observation_workspace_id = request_context.scope.workspace_id
        observation_environment_id = request_context.scope.environment_id
        if not events and terminal is not None:
            if last_event_id != str(terminal.event_id) and last_sequence != terminal.sequence:
                events = (terminal,)
        elif not events and terminal is None:
            events = (await stream.heartbeat(),)
        event_contexts = {
            (event.trace_id, event.correlation_id, event.causation_id)
            for event in events
        }
        if len(event_contexts) > 1:
            raise InvalidAuthorizedAskRequest()
        if event_contexts:
            trace_id, correlation_id, causation_id = next(iter(event_contexts))
            observation_context = DiagnosticContext(
                trace_id=trace_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
        else:
            observation_context = request_context.diagnostic_context

        async def body() -> AsyncIterator[str]:
            try:
                for event in events:
                    yield _encode_sse(event)
            except BaseException:
                await _observe_delivery(
                    operational_observer,
                    workspace_id=observation_workspace_id,
                    environment_id=observation_environment_id,
                    diagnostic_context=observation_context,
                    run_id=run_id,
                    message_id=message_id,
                    outcome=OperationalObservationOutcome.FAILED,
                    failure_code=SafeOperationalFailureCode.INTERNAL_FAILURE,
                )
                raise
            await _observe_delivery(
                operational_observer,
                workspace_id=observation_workspace_id,
                environment_id=observation_environment_id,
                diagnostic_context=observation_context,
                run_id=run_id,
                message_id=message_id,
                outcome=OperationalObservationOutcome.SUCCEEDED,
            )

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    add_protected_api_route(
        application,
        "/api/v1/qa/sessions/{session_id}/messages/{message_id}/events",
        events_endpoint,
        route_id="qa.messages.events",
        policy=_policy(),
        methods=["GET"],
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Resumable Q&A server-sent event stream.",
                "content": {
                    "text/event-stream": {
                        "schema": {"type": "string"},
                    }
                },
            }
        },
    )


async def _canonical_terminal_event(
    *,
    request: Request,
    persistence_context: TrustedPersistenceContext,
    request_context: AuthorizedRequestContext[object],
    session_id: UUID,
    message_id: UUID,
    run_id: UUID | None,
) -> QAEvent | None:
    """Rehydrate a worker-committed terminal without crossing disclosure data."""

    service = getattr(request.app.state, "qa_session_service", None)
    get_message = getattr(service, "get_message", None)
    if not callable(get_message):
        return None
    message = await get_message(
        context=persistence_context,
        requester=QASessionRequester(
            subject_id=request_context.acting_subject_id,
            authorization_generation=request_context.authorization_generation,
        ),
        session_id=session_id,
        message_id=message_id,
    )
    if (
        message is None
        or message.state is not QAMessageState.TERMINAL
        or message.terminal_outcome is None
        or (run_id is not None and message.run_id != run_id)
    ):
        return None
    outcome = message.terminal_outcome
    event_type, payload = _terminal_payload(outcome)
    return QAEvent(
        event_id=uuid5(
            NAMESPACE_URL,
            f"spine:qa-canonical-terminal:{message.workspace_id}:"
            f"{message.environment_id}:{message.session_id}:{message.message_id}:"
            f"{message.run_id}:{message.version}:{outcome.value}",
        ),
        sequence=2,
        type=event_type,
        session_id=message.session_id,
        workspace_id=message.workspace_id,
        environment_id=message.environment_id,
        message_id=message.message_id,
        run_id=message.run_id,
        trace_id=message.trace_id,
        payload=payload,
    )


def _terminal_payload(outcome: AnswerQuestionOutcome) -> tuple[QAEventType, object]:
    if outcome is AnswerQuestionOutcome.ANSWERED:
        return QAEventType.COMPLETED, CompletedPayload(outcome="answered")
    if outcome is AnswerQuestionOutcome.CLARIFICATION_REQUIRED:
        return QAEventType.COMPLETED, CompletedPayload(outcome="clarification_required")
    if outcome is AnswerQuestionOutcome.ABSTAINED:
        return QAEventType.ABSTAINED, AbstainedPayload(reason="no_usable_evidence")
    if outcome is AnswerQuestionOutcome.DENIED:
        return QAEventType.DENIED, DeniedPayload(reason="authorization_denied")
    if outcome is AnswerQuestionOutcome.CANCELLED:
        return QAEventType.CANCELLED, CancelledPayload(reason="request_cancelled")
    return QAEventType.FAILED, FailedPayload(reason="mandatory_processing_failed")


async def _observe_delivery(
    observer: OperationalObservationEmitter | None,
    *,
    workspace_id: UUID,
    environment_id: UUID,
    diagnostic_context: DiagnosticContext,
    run_id: UUID | None,
    message_id: UUID,
    outcome: OperationalObservationOutcome,
    failure_code: SafeOperationalFailureCode | None = None,
) -> None:
    if observer is None:
        return
    await observer.emit(
        workspace_id=workspace_id,
        environment_id=environment_id,
        diagnostic_context=diagnostic_context,
        component=OperationalComponent.SSE,
        stage=OperationalStage.DELIVERY,
        operation=OperationalOperation.SSE_DELIVERY,
        outcome=outcome,
        safe_failure_code=failure_code,
        run_id=run_id,
        message_id=message_id,
    )


def _service(request: Request) -> QAStreamReader:
    service = getattr(request.app.state, "qa_stream_service", None)
    if service is None:
        raise RuntimeError("Q&A stream service is unavailable.")
    return cast(QAStreamReader, service)


def _encode_sse(event: QAEvent) -> str:
    return (
        f"id: {event.event_id}\n"
        f"event: {event.type.value}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("qa_stream"),
    )


__all__ = ["QAStreamDisclosureAuthorizer", "QAStreamReader", "install_qa_stream_routes"]
