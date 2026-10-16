"""Protected Q&A session and message navigation routes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    ApiCollectionEnvelope,
    ApiCommandResult,
    ApiResponseEnvelope,
    CommandStatus,
    CursorCodec,
    DEFAULT_PAGE_LIMIT,
    InvalidCursor,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    canonical_filter_digest,
    parse_idempotency_key_header,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.qa_sessions import (
    InvalidQASessionRequest,
    QAMessageNotFound,
    QAMessagePosition,
    QAMessageRead,
    QASessionNotFound,
    QASessionPosition,
    QASessionRequester,
    QASessionService,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.qa_sessions import QAMessageState, QASession, QASessionStatus
from spine.domain.capabilities import AnswerQuestionOutcome


_SESSIONS_OPERATION = "qa.sessions.list"
_MESSAGES_OPERATION = "qa.messages.list"
_EMPTY_FILTER_DIGEST = canonical_filter_digest({})


class QASessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    session_id: UUID
    workspace_id: UUID
    environment_id: UUID
    subject_id: UUID
    version: int = Field(ge=1)
    status: QASessionStatus
    created_at: datetime
    updated_at: datetime


class QAMessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    message_id: UUID
    session_id: UUID
    workspace_id: UUID
    environment_id: UUID
    version: int = Field(ge=1)
    ordinal: int = Field(ge=1)
    trace_id: UUID
    run_id: UUID | None
    state: QAMessageState
    terminal_outcome: AnswerQuestionOutcome | None
    cancelled: bool
    disclosure_eligibility: Literal["eligible", "ineligible"]
    question: str | None
    answer: str | None
    created_at: datetime
    updated_at: datetime


class CreateQASessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RestoreQASessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int = Field(ge=1, strict=True)


class CreateQAMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=32_768)
    run_id: UUID | None = None


def install_qa_session_routes(
    application: FastAPI,
    *,
    service: QASessionService,
    cursor_secret: bytes,
) -> None:
    """Register administrator-only session and message routes."""

    codec = CursorCodec(secret=cursor_secret)
    application.state.qa_session_service = service

    async def create_session_endpoint(
        request: Request,
        body: CreateQASessionRequest = Body(...),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiCommandResult[QASessionResponse]:
        del body
        result = await _service(request).create_session(
            context=context,
            requester=_requester(request_context),
            idempotency_key=_idempotency(request.headers.getlist("Idempotency-Key")),
        )
        return _command(request_context, result.command_id, _session_response(result.value), result.replay)

    async def list_sessions_endpoint(
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiCollectionEnvelope[QASessionResponse]:
        after = _decode_session_cursor(cursor, codec=codec, request_context=request_context, limit=limit)
        page = await _service(request).list_sessions(context=context, limit=limit, after=after)
        items = tuple(_session_response(item) for item in page.items)
        next_cursor = None
        if page.has_more and items:
            last = page.items[-1]
            next_cursor = codec.encode(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                operation_id=_SESSIONS_OPERATION,
                limit=limit,
                filters_digest=_EMPTY_FILTER_DIGEST,
                position=f"{last.created_at.isoformat()}|{last.session_id}",
            )
        return _collection(request_context, items, limit, next_cursor)

    async def get_session_endpoint(
        session_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiResponseEnvelope[QASessionResponse]:
        session = await _service(request).get_session(context=context, session_id=session_id)
        if session is None:
            raise QASessionNotFound()
        return _response(request_context, _session_response(session))

    async def restore_session_endpoint(
        session_id: UUID,
        request: Request,
        body: RestoreQASessionRequest,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiCommandResult[QASessionResponse]:
        result = await _service(request).restore_session(
            context=context,
            session_id=session_id,
            expected_version=body.expected_version,
            idempotency_key=_idempotency(request.headers.getlist("Idempotency-Key")),
        )
        return _command(request_context, result.command_id, _session_response(result.value), result.replay)

    async def create_message_endpoint(
        session_id: UUID,
        request: Request,
        body: CreateQAMessageRequest,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiCommandResult[QAMessageResponse]:
        result = await _service(request).create_pending_message(
            context=context,
            requester=_requester(request_context),
            session_id=session_id,
            question=body.question,
            run_id=body.run_id,
            idempotency_key=_idempotency(request.headers.getlist("Idempotency-Key")),
        )
        read = await _service(request).get_message(
            context=context, requester=_requester(request_context),
            session_id=session_id, message_id=result.value.message_id,
        )
        if read is None:
            raise QAMessageNotFound()
        return _command(request_context, result.command_id, _message_response(read), result.replay)

    async def list_messages_endpoint(
        session_id: UUID,
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiCollectionEnvelope[QAMessageResponse]:
        after = _decode_message_cursor(cursor, codec=codec, request_context=request_context, limit=limit)
        page = await _service(request).list_messages(
            context=context, requester=_requester(request_context), session_id=session_id,
            limit=limit, after=after,
        )
        if page is None:
            raise QASessionNotFound()
        items = tuple(_message_response(item) for item in page.items)
        next_cursor = None
        if page.has_more and items:
            last = page.items[-1]
            next_cursor = codec.encode(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                operation_id=_MESSAGES_OPERATION,
                limit=limit,
                filters_digest=_EMPTY_FILTER_DIGEST,
                position=f"{last.ordinal}|{last.message_id}",
            )
        return _collection(request_context, items, limit, next_cursor)

    async def get_message_endpoint(
        session_id: UUID,
        message_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiResponseEnvelope[QAMessageResponse]:
        message = await _service(request).get_message(
            context=context, requester=_requester(request_context),
            session_id=session_id, message_id=message_id,
        )
        if message is None:
            raise QAMessageNotFound()
        return _response(request_context, _message_response(message))

    policy = _policy("qa_sessions")
    add_protected_api_route(application, "/api/v1/qa/sessions", create_session_endpoint, route_id="qa.sessions.create", policy=policy, methods=["POST"], response_model=ApiCommandResult[QASessionResponse], openapi_extra=_idempotency_openapi_extra())
    add_protected_api_route(application, "/api/v1/qa/sessions", list_sessions_endpoint, route_id=_SESSIONS_OPERATION, policy=policy, methods=["GET"], response_model=ApiCollectionEnvelope[QASessionResponse])
    add_protected_api_route(application, "/api/v1/qa/sessions/{session_id}", get_session_endpoint, route_id="qa.sessions.detail", policy=policy, methods=["GET"], response_model=ApiResponseEnvelope[QASessionResponse])
    add_protected_api_route(application, "/api/v1/qa/sessions/{session_id}/restore", restore_session_endpoint, route_id="qa.sessions.restore", policy=policy, methods=["POST"], response_model=ApiCommandResult[QASessionResponse], openapi_extra=_idempotency_openapi_extra())
    add_protected_api_route(application, "/api/v1/qa/sessions/{session_id}/messages", create_message_endpoint, route_id="qa.messages.create", policy=policy, methods=["POST"], response_model=ApiCommandResult[QAMessageResponse], openapi_extra=_idempotency_openapi_extra())
    add_protected_api_route(application, "/api/v1/qa/sessions/{session_id}/messages", list_messages_endpoint, route_id=_MESSAGES_OPERATION, policy=policy, methods=["GET"], response_model=ApiCollectionEnvelope[QAMessageResponse])
    add_protected_api_route(application, "/api/v1/qa/sessions/{session_id}/messages/{message_id}", get_message_endpoint, route_id="qa.messages.detail", policy=policy, methods=["GET"], response_model=ApiResponseEnvelope[QAMessageResponse])


def _service(request: Request) -> QASessionService:
    service = getattr(request.app.state, "qa_session_service", None)
    if service is None:
        raise RuntimeError("Q&A session service is unavailable.")
    return cast(QASessionService, service)


def _requester(request_context: AuthorizedRequestContext[object]) -> QASessionRequester:
    return QASessionRequester(
        subject_id=request_context.acting_subject_id,
        authorization_generation=request_context.authorization_generation,
    )


def _idempotency(values: list[str]):
    try:
        parsed = parse_idempotency_key_header(values)
        return IdempotencyKey(parsed.value)
    except (TypeError, ValueError):
        raise InvalidQASessionRequest() from None


def _decode_session_cursor(value, *, codec, request_context, limit):
    if value is None:
        return None
    claims = _decode(value, codec=codec, request_context=request_context, operation=_SESSIONS_OPERATION, limit=limit)
    try:
        timestamp, identity = claims.position.rsplit("|", 1)
        return QASessionPosition(datetime.fromisoformat(timestamp), UUID(identity))
    except (TypeError, ValueError):
        raise InvalidCursor() from None


def _decode_message_cursor(value, *, codec, request_context, limit):
    if value is None:
        return None
    claims = _decode(value, codec=codec, request_context=request_context, operation=_MESSAGES_OPERATION, limit=limit)
    try:
        ordinal, identity = claims.position.rsplit("|", 1)
        return QAMessagePosition(int(ordinal), UUID(identity))
    except (TypeError, ValueError):
        raise InvalidCursor() from None


def _decode(value, *, codec, request_context, operation, limit):
    try:
        return codec.decode(
            value,
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            operation_id=operation,
            limit=limit,
            filters_digest=_EMPTY_FILTER_DIGEST,
        )
    except InvalidCursor:
        raise
    except Exception:
        raise InvalidCursor() from None


def _session_response(session: QASession) -> QASessionResponse:
    return QASessionResponse.model_validate(session.model_dump(mode="python"))


def _message_response(message: QAMessageRead) -> QAMessageResponse:
    return QAMessageResponse(
        message_id=message.message_id,
        session_id=message.session_id,
        workspace_id=message.workspace_id,
        environment_id=message.environment_id,
        version=message.version,
        ordinal=message.ordinal,
        trace_id=message.trace_id,
        run_id=message.run_id,
        state=message.state,
        terminal_outcome=message.terminal_outcome.value if message.terminal_outcome else None,
        cancelled=message.cancelled,
        disclosure_eligibility=cast(
            Literal["eligible", "ineligible"], message.disclosure_eligibility.value
        ),
        question=message.question,
        answer=message.answer,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def _response(request_context, data):
    return ApiResponseEnvelope(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        trace_id=request_context.diagnostic_context.trace_id,
        data=data,
    )


def _collection(request_context, items, limit, next_cursor):
    return ApiCollectionEnvelope(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        trace_id=request_context.diagnostic_context.trace_id,
        items=items,
        limit=limit,
        next_cursor=next_cursor,
    )


def _command(request_context, command_id, result, replay):
    return ApiCommandResult(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        trace_id=request_context.diagnostic_context.trace_id,
        command_id=command_id,
        status=CommandStatus.COMPLETED,
        replay=replay,
        result=result,
    )


def _policy(operation: str) -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation(operation),
    )


def _idempotency_openapi_extra() -> dict[str, object]:
    return {
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 255},
            }
        ]
    }


__all__ = [
    "CreateQAMessageRequest",
    "CreateQASessionRequest",
    "QAMessageResponse",
    "QASessionResponse",
    "RestoreQASessionRequest",
    "install_qa_session_routes",
]
