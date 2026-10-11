from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from spine.api.diagnostics import (
    DiagnosticContextMiddleware,
    get_diagnostic_context,
    install_diagnostic_exception_handlers,
)
from spine.api.authorization import (
    ApiAuthorizationRuntime,
    ApiRouteClassificationRegistry,
    add_public_api_route,
    install_authorization_openapi,
    validate_api_route_authorization,
)
from spine.application.diagnostics import (
    DiagnosticEventRegistry,
    DiagnosticSink,
    StructuredErrorRegistry,
    build_default_error_registry,
)
from spine.config import settings
from spine.ingest.loaders import documents_to_remember_payloads, load_path
from spine.infrastructure.diagnostics import JsonLoggingDiagnosticSink
from spine.memory.cognee_memory import ask, format_answers, remember_texts


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    dataset: str | None = None
    session_id: str | None = None


class AskResponse(BaseModel):
    question: str
    answer: str
    raw_count: int


class IngestRequest(BaseModel):
    path: str = Field(..., description="Absolute or project-relative path to a file or directory")
    dataset: str | None = None
    node_set: list[str] | None = None


class IngestResponse(BaseModel):
    path: str
    documents: int
    status: str


async def health() -> dict[str, str]:
    return {"status": "ok", "service": "spine", "phase": "qa"}


async def liveness() -> dict[str, str]:
    return {"status": "ok", "service": "spine"}


async def readiness(request: Request) -> JSONResponse:
    runtime = getattr(request.app.state, "authorization_runtime", None)
    if type(runtime) is ApiAuthorizationRuntime:
        authorization_readiness = await runtime.check_readiness()
        ready = authorization_readiness.ready
        code = authorization_readiness.code.value
    else:
        ready = True
        code = "authorization.ready"
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "service": "spine",
            "code": code,
        },
    )


async def ask_endpoint(body: AskRequest) -> AskResponse:
    try:
        results = await ask(
            body.question,
            dataset=body.dataset,
            session_id=body.session_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"recall failed: {exc}") from exc
    items = list(results) if results is not None else []
    return AskResponse(
        question=body.question,
        answer=format_answers(items),
        raw_count=len(items),
    )


async def ingest_endpoint(body: IngestRequest) -> IngestResponse:
    path = Path(body.path)
    if not path.is_absolute():
        path = settings.project_root / path
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"path not found: {path}")

    docs = load_path(path)
    if not docs:
        raise HTTPException(status_code=400, detail="no supported documents found")

    payloads = documents_to_remember_payloads(docs)
    try:
        await remember_texts(
            payloads,
            dataset=body.dataset,
            node_set=body.node_set,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"remember failed: {exc}") from exc

    return IngestResponse(path=str(path), documents=len(docs), status="ok")


def create_app(
    *,
    trace_id_source: Callable[[], UUID] = uuid4,
    diagnostic_registry: DiagnosticEventRegistry | None = None,
    diagnostic_sink: DiagnosticSink | None = None,
    error_registry: StructuredErrorRegistry | None = None,
    clock: Callable[[], datetime] | None = None,
    authorization_runtime: ApiAuthorizationRuntime | None = None,
) -> FastAPI:
    """Compose the HTTP API with injectable deterministic diagnostics."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        validate_api_route_authorization(application)
        runtime = application.state.authorization_runtime
        try:
            if type(runtime) is ApiAuthorizationRuntime:
                await runtime.startup()
            yield
        finally:
            if type(runtime) is ApiAuthorizationRuntime:
                await runtime.aclose()

    application = FastAPI(
        title="Spine",
        version="0.1.0",
        description="Phase 1: company Q&A on Cognee. Trace-like orchestration comes later.",
        lifespan=lifespan,
    )
    resolved_diagnostic_registry = (
        diagnostic_registry or DiagnosticEventRegistry.with_default_families()
    )
    resolved_diagnostic_sink = diagnostic_sink or JsonLoggingDiagnosticSink(
        resolved_diagnostic_registry,
        logger=logging.getLogger("spine.diagnostics"),
    )
    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
    application.state.diagnostic_registry = resolved_diagnostic_registry
    application.state.diagnostic_sink = resolved_diagnostic_sink
    application.state.diagnostic_clock = resolved_clock
    application.state.authorization_runtime = authorization_runtime
    application.state.route_classifications = ApiRouteClassificationRegistry()
    install_authorization_openapi(application)
    resolved_error_registry = error_registry or build_default_error_registry()
    application.add_middleware(
        DiagnosticContextMiddleware,
        trace_id_source=trace_id_source,
        error_registry=resolved_error_registry,
        diagnostic_registry=resolved_diagnostic_registry,
        diagnostic_sink=resolved_diagnostic_sink,
        clock=resolved_clock,
    )
    install_diagnostic_exception_handlers(
        application,
        error_registry=resolved_error_registry,
    )
    application.add_api_route("/health", health, methods=["GET"])
    add_public_api_route(
        application,
        "/api/v1/health/live",
        liveness,
        route_id="system.health.live",
        methods=["GET"],
    )
    add_public_api_route(
        application,
        "/api/v1/health/ready",
        readiness,
        route_id="system.health.ready",
        methods=["GET"],
    )
    application.add_api_route(
        "/ask",
        ask_endpoint,
        methods=["POST"],
        response_model=AskResponse,
    )
    application.add_api_route(
        "/ingest",
        ingest_endpoint,
        methods=["POST"],
        response_model=IngestResponse,
    )
    return application


app = create_app()


__all__ = ["app", "create_app", "get_diagnostic_context"]
