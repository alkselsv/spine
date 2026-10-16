"""Protected SSE and polling delivery for persisted Q&A events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from starlette.responses import StreamingResponse

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_request_context,
)
from spine.application.authorized_ask import InvalidAuthorizedAskRequest
from spine.application.qa_events import DisclosureAuthorizer, ResumeCursorError
from spine.application.qa_streaming import QAEventStream, QAResultEventPublisher
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.qa_events import DisclosureReceipt, QAEvent
from spine.application.persistence import PersistenceOperation, PersistencePurpose


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

        events = replay.events
        if not events and replay.terminal is not None:
            terminal = replay.terminal
            if last_event_id != str(terminal.event_id) and last_sequence != terminal.sequence:
                events = (terminal,)
        elif not events and replay.terminal is None and (
            last_event_id is not None or last_sequence not in (None, 0)
        ):
            events = (await stream.heartbeat(),)

        async def body() -> AsyncIterator[str]:
            for event in events:
                yield _encode_sse(event)

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
