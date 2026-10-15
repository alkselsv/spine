"""FastAPI adapters for trusted request diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import cast
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from spine.api.schemas import InvalidCursor
from spine.application.diagnostics import (
    DiagnosticContext,
    DiagnosticEventRegistry,
    DiagnosticFailureCode,
    DiagnosticSeverity,
    DiagnosticSink,
    FailureDiagnosticPayload,
    HttpRequestDiagnosticPayload,
    HttpRequestDiagnosticState,
    Retryability,
    StructuredError,
    StructuredErrorRegistry,
    emit_diagnostic_safely,
)
from spine.infrastructure.diagnostics import InvalidTraceParentError, parse_traceparent


TRACE_ID_HEADER = "X-Trace-ID"
_CONTEXT_STATE_KEY = "diagnostic_context"


class DiagnosticContextMiddleware:
    """Establish one trusted trace before HTTP route execution."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        trace_id_source: Callable[[], UUID],
        error_registry: StructuredErrorRegistry,
        diagnostic_registry: DiagnosticEventRegistry,
        diagnostic_sink: DiagnosticSink,
        clock: Callable[[], datetime],
    ) -> None:
        self._app = app
        self._trace_id_source = trace_id_source
        self._error_registry = error_registry
        self._diagnostic_registry = diagnostic_registry
        self._diagnostic_sink = diagnostic_sink
        self._clock = clock

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        traceparent = Headers(scope=scope).get("traceparent")
        invalid_traceparent = False
        if traceparent is None:
            context = DiagnosticContext.create(id_source=self._trace_id_source)
        else:
            try:
                context = DiagnosticContext(
                    trace_id=parse_traceparent(traceparent).trace_id
                )
            except InvalidTraceParentError:
                invalid_traceparent = True
                context = DiagnosticContext.create(id_source=self._trace_id_source)
        scope.setdefault("state", {})[_CONTEXT_STATE_KEY] = context
        if invalid_traceparent:
            await self._emit_invalid_trace_diagnostic(context)

        response_started = False
        response_status_code: int | None = None

        async def send_with_trace(message: Message) -> None:
            nonlocal response_started, response_status_code
            if message["type"] == "http.response.start":
                response_started = True
                response_status_code = cast(int, message["status"])
                MutableHeaders(scope=message)[TRACE_ID_HEADER] = str(context.trace_id)
            await send(message)

        try:
            await self._app(scope, receive, send_with_trace)
        except Exception as failure:
            if response_started:
                raise
            if _is_versioned_api_path(scope.get("path", "")):
                error = self._error_registry.map(failure, context)
                response: Response = JSONResponse(
                    status_code=_status_for_structured_error(error),
                    content=error.model_dump(mode="json"),
                    headers=_headers_for_structured_error(error),
                )
            else:
                response = PlainTextResponse("Internal Server Error", status_code=500)
            await response(scope, receive, send_with_trace)
        finally:
            if response_status_code is not None:
                await self._emit_request_diagnostic(
                    context,
                    response_status_code=response_status_code,
                )

    async def _emit_request_diagnostic(
        self,
        context: DiagnosticContext,
        *,
        response_status_code: int,
    ) -> None:
        failed = response_status_code >= 400
        event = self._diagnostic_registry.build_event(
            event_type="http.request",
            schema_version=1,
            severity=(DiagnosticSeverity.WARNING if failed else DiagnosticSeverity.INFO),
            occurred_at=self._clock(),
            diagnostic_context=context,
            payload=HttpRequestDiagnosticPayload(
                request_state=(
                    HttpRequestDiagnosticState.FAILED
                    if failed
                    else HttpRequestDiagnosticState.SUCCEEDED
                ),
                response_status_code=response_status_code,
            ),
        )
        await emit_diagnostic_safely(self._diagnostic_sink, event)

    async def _emit_invalid_trace_diagnostic(
        self,
        context: DiagnosticContext,
    ) -> None:
        event = self._diagnostic_registry.build_event(
            event_type="failure.observed",
            schema_version=1,
            severity=DiagnosticSeverity.WARNING,
            occurred_at=self._clock(),
            diagnostic_context=context,
            payload=FailureDiagnosticPayload(
                failure_code=DiagnosticFailureCode.TRACE_CONTEXT_INVALID,
            ),
        )
        await emit_diagnostic_safely(self._diagnostic_sink, event)


def _read_diagnostic_context(request: Request) -> DiagnosticContext:
    """Return only the middleware-issued request diagnostic context."""

    context = getattr(request.state, _CONTEXT_STATE_KEY, None)
    if type(context) is not DiagnosticContext:
        raise RuntimeError("Trusted request diagnostic context is unavailable.")
    return cast(DiagnosticContext, context)


async def get_diagnostic_context(request: Request) -> DiagnosticContext:
    """Async FastAPI dependency for the middleware-issued request context."""

    return _read_diagnostic_context(request)


def install_diagnostic_exception_handlers(
    application: FastAPI,
    *,
    error_registry: StructuredErrorRegistry,
) -> None:
    """Install safe `/api/v1` handlers while retaining legacy bodies."""

    async def handle_validation(
        request: Request,
        failure: RequestValidationError,
    ) -> Response:
        if not _is_versioned_api_path(request.url.path):
            return await request_validation_exception_handler(request, failure)
        context = _read_diagnostic_context(request)
        error = StructuredError(
            code="spine.request.validation",
            safe_message="Request validation failed.",
            retryability=Retryability.NEVER,
            trace_id=context.trace_id,
        )
        return JSONResponse(
            status_code=422,
            content=error.model_dump(mode="json"),
        )

    async def handle_registered(request: Request, failure: Exception) -> Response:
        if not _is_versioned_api_path(request.url.path):
            raise failure
        error = error_registry.map(failure, _read_diagnostic_context(request))
        return JSONResponse(
            status_code=_status_for_structured_error(error),
            content=error.model_dump(mode="json"),
            headers=_headers_for_structured_error(error),
        )

    async def handle_invalid_cursor(request: Request, failure: InvalidCursor) -> Response:
        if not _is_versioned_api_path(request.url.path):
            raise failure
        error = StructuredError(
            code="spine.request.invalid_cursor",
            safe_message="The cursor is invalid.",
            retryability=Retryability.NEVER,
            trace_id=_read_diagnostic_context(request).trace_id,
        )
        return JSONResponse(
            status_code=400,
            content=error.model_dump(mode="json"),
        )

    async def handle_http(
        request: Request,
        failure: StarletteHTTPException,
    ) -> Response:
        if not _is_versioned_api_path(request.url.path):
            return await http_exception_handler(request, failure)
        error = StructuredError(
            code="spine.request.rejected",
            safe_message="The request was rejected.",
            retryability=Retryability.NEVER,
            trace_id=_read_diagnostic_context(request).trace_id,
        )
        return JSONResponse(
            status_code=failure.status_code,
            content=error.model_dump(mode="json"),
        )

    application.add_exception_handler(RequestValidationError, handle_validation)
    application.add_exception_handler(StarletteHTTPException, handle_http)
    application.add_exception_handler(InvalidCursor, handle_invalid_cursor)
    for exception_type in error_registry.exception_types:
        application.add_exception_handler(exception_type, handle_registered)


def _is_versioned_api_path(path: str) -> bool:
    return path == "/api/v1" or path.startswith("/api/v1/")


def _status_for_structured_error(error: StructuredError) -> int:
    if error.code == "spine.authentication.invalid":
        return 401
    if error.code == "spine.authorization.denied":
        return 403
    if error.code in {
        "spine.persistence.optimistic_conflict",
        "spine.persistence.constraint_conflict",
        "spine.persistence.idempotency_conflict",
        "spine.persistence.outbox_conflict",
        "spine.persistence.audit_conflict",
    }:
        return 409
    if error.code in {
        "spine.persistence.invalid_context",
        "spine.persistence.invalid_bootstrap_authority",
        "spine.persistence.unsupported_outbox_event",
        "spine.persistence.unsupported_audit_event",
    }:
        return 400
    if error.code == "spine.ingestion.payload_too_large":
        return 413
    if error.code == "spine.ingestion.not_found":
        return 404
    if error.code == "spine.documents.not_found":
        return 404
    if error.code in {"spine.qa_sessions.not_found", "spine.qa_sessions.message_not_found"}:
        return 404
    if error.code == "spine.qa_sessions.conflict":
        return 409
    if error.code == "spine.qa_sessions.invalid_request":
        return 400
    if error.code == "spine.ingestion.invalid_request":
        return 400
    if error.code == "spine.projection.invalid_request":
        return 400
    if error.retryability is Retryability.AFTER_DELAY:
        return 503
    return 500


def _headers_for_structured_error(error: StructuredError) -> dict[str, str]:
    if error.code == "spine.authentication.invalid":
        return {"WWW-Authenticate": "Bearer"}
    return {}


__all__ = [
    "DiagnosticContextMiddleware",
    "TRACE_ID_HEADER",
    "get_diagnostic_context",
    "install_diagnostic_exception_handlers",
]
