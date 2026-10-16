from __future__ import annotations

import asyncio
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
from spine.api.health import (
    LivenessResponse,
    ReadinessResponse,
    install_health_summary_route,
)
from spine.api.outbox_inspection import install_outbox_inspection_route
from spine.api.batch_ingestion import install_batch_ingestion_routes
from spine.api.ingestion import install_ingestion_routes
from spine.api.ingestion_history import install_ingestion_history_routes
from spine.api.documents import install_document_query_routes
from spine.api.projection_control import install_projection_control_routes
from spine.api.qa_sessions import install_qa_session_routes
from spine.api.authorized_ask import install_authorized_ask_routes
from spine.api.citation_inspector import install_citation_inspector_routes
from spine.api.feedback import install_feedback_routes
from spine.api.qa_runs import install_qa_run_routes
from spine.api.qa_stream import (
    QAStreamDisclosureAuthorizer,
    QAStreamReader,
    install_qa_stream_routes,
)
from spine.api.parsed_content import install_parsed_content_routes
from spine.api.processing_commands import install_processing_command_routes
from spine.api.operational_observations import install_operational_observation_routes
from spine.api.document_lifecycle_commands import (
    install_document_lifecycle_command_routes,
)
from spine.api.authorization import (
    ApiAuthorizationReadiness,
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
from spine.application.ingestion_api import DocumentIngestionService
from spine.application.batch_ingestion_api import BatchDocumentIngestionService
from spine.application.ingestion_history import IngestionHistoryService
from spine.application.document_queries import DocumentQueryService
from spine.application.health_summary import HealthStatusServices
from spine.application.outbox_inspection import OutboxDeliveryInspectionService
from spine.application.projection_control import (
    ProjectionRecoveryCommandService,
    ProjectionStatusService,
)
from spine.application.qa_sessions import QASessionService
from spine.application.authorized_ask import (
    AnswerQuestionCapability,
    AuthorizedAskService,
    PersistedAuthorizedAskService,
)
from spine.application.citation_inspector import CitationInspectorService
from spine.application.feedback import FeedbackService
from spine.application.qa_run_history import QARunHistoryService
from spine.application.parsed_content_inspection import ParsedContentInspectionService
from spine.application.processing_commands import ProcessingCommandService
from spine.application.telemetry import (
    OperationalObservationEmitter,
    OperationalObservationPolicy,
    OperationalObservationReader,
    OperationalObservationRegistry,
)
from spine.application.document_lifecycle import DocumentLifecycleCommandService
from spine.config import settings
from spine.ingest.loaders import documents_to_remember_payloads, load_path
from spine.infrastructure.diagnostics import JsonLoggingDiagnosticSink
from spine.infrastructure.telemetry import DiagnosticOperationalObservationSink
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


async def liveness() -> LivenessResponse:
    return LivenessResponse(status="ok", service="spine")


async def readiness(request: Request) -> JSONResponse:
    runtime = getattr(request.app.state, "authorization_runtime", None)
    if type(runtime) is ApiAuthorizationRuntime:
        try:
            authorization_readiness = await asyncio.wait_for(
                runtime.check_readiness(),
                timeout=request.app.state.health_readiness_timeout_seconds,
            )
            if type(authorization_readiness) is not ApiAuthorizationReadiness:
                raise TypeError("authorization readiness is not canonical")
            ready = authorization_readiness.ready
            code = authorization_readiness.code.value
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            ready = False
            code = "authorization.unavailable"
    else:
        ready = True
        code = "authorization.ready"
    body = ReadinessResponse(
        status="ready" if ready else "not_ready",
        service="spine",
        code=code,  # type: ignore[arg-type]
    )
    return JSONResponse(
        status_code=200 if ready else 503,
        content=body.model_dump(mode="json"),
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
    document_ingestion_service: DocumentIngestionService | None = None,
    batch_document_ingestion_service: BatchDocumentIngestionService | None = None,
    ingestion_history_service: IngestionHistoryService | None = None,
    ingestion_history_cursor_secret: bytes | None = None,
    document_query_service: DocumentQueryService | None = None,
    document_query_cursor_secret: bytes | None = None,
    max_upload_bytes: int = 10_000_000,
    max_batch_files: int = 20,
    max_batch_file_bytes: int = 10_000_000,
    max_batch_upload_bytes: int = 50_000_000,
    health_status_services: HealthStatusServices | None = None,
    build_version: str | None = None,
    health_summary_timeout_seconds: float = 0.25,
    outbox_delivery_inspection_service: OutboxDeliveryInspectionService | None = None,
    outbox_inspection_cursor_secret: bytes | None = None,
    health_readiness_timeout_seconds: float = 0.25,
    projection_status_service: ProjectionStatusService | None = None,
    projection_recovery_service: ProjectionRecoveryCommandService | None = None,
    qa_session_service: QASessionService | None = None,
    qa_session_cursor_secret: bytes | None = None,
    authorized_ask_service: AuthorizedAskService | None = None,
    answer_question_capability: AnswerQuestionCapability | None = None,
    citation_inspector_service: CitationInspectorService | None = None,
    feedback_service: FeedbackService | None = None,
    qa_run_history_service: QARunHistoryService | None = None,
    qa_run_history_cursor_secret: bytes | None = None,
    qa_stream_service: QAStreamReader | None = None,
    qa_stream_disclosure_authorizer: QAStreamDisclosureAuthorizer | None = None,
    parsed_content_inspection_service: ParsedContentInspectionService | None = None,
    parsed_content_cursor_secret: bytes | None = None,
    processing_command_service: ProcessingCommandService | None = None,
    document_lifecycle_command_service: DocumentLifecycleCommandService | None = None,
    operational_observation_reader: OperationalObservationReader | None = None,
    operational_observer: OperationalObservationEmitter | None = None,
) -> FastAPI:
    """Compose the HTTP API with injectable deterministic diagnostics."""

    if health_readiness_timeout_seconds <= 0 or health_readiness_timeout_seconds > 5:
        raise ValueError("health readiness timeout must be between 0 and 5 seconds")

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
    if operational_observer is None:
        observation_registry = OperationalObservationRegistry()
        operational_observer = OperationalObservationEmitter(
            registry=observation_registry,
            sink=DiagnosticOperationalObservationSink(
                observation_registry=observation_registry,
                diagnostic_registry=resolved_diagnostic_registry,
                diagnostic_sink=resolved_diagnostic_sink,
            ),
            policy=OperationalObservationPolicy(),
            clock=resolved_clock,
            observation_id_source=uuid4,
        )
    application.state.diagnostic_registry = resolved_diagnostic_registry
    application.state.diagnostic_sink = resolved_diagnostic_sink
    application.state.diagnostic_clock = resolved_clock
    application.state.authorization_runtime = authorization_runtime
    application.state.document_ingestion_service = document_ingestion_service
    application.state.batch_document_ingestion_service = (
        batch_document_ingestion_service
    )
    application.state.ingestion_history_service = ingestion_history_service
    application.state.document_query_service = document_query_service
    application.state.health_readiness_timeout_seconds = health_readiness_timeout_seconds
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
        response_model=LivenessResponse,
    )
    if type(authorization_runtime) is ApiAuthorizationRuntime:
        install_health_summary_route(
            application,
            services=health_status_services,
            build_version=build_version,
            timeout_seconds=health_summary_timeout_seconds,
        )
        install_outbox_inspection_route(
            application,
            service=outbox_delivery_inspection_service,
            cursor_secret=outbox_inspection_cursor_secret,
        )
        if projection_status_service is not None or projection_recovery_service is not None:
            install_projection_control_routes(
                application,
                status_service=projection_status_service,
                recovery_service=projection_recovery_service,
            )
        if operational_observation_reader is not None:
            install_operational_observation_routes(
                application,
                reader=operational_observation_reader,
            )
    add_public_api_route(
        application,
        "/api/v1/health/ready",
        readiness,
        route_id="system.health.ready",
        methods=["GET"],
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse}},
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
    if document_ingestion_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Document ingestion routes require an authorization runtime.")
        install_ingestion_routes(
            application,
            service=document_ingestion_service,
            max_upload_bytes=max_upload_bytes,
        )
    if batch_document_ingestion_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Batch document ingestion routes require an authorization runtime.")
        install_batch_ingestion_routes(
            application,
            service=batch_document_ingestion_service,
            max_files=max_batch_files,
            max_file_bytes=max_batch_file_bytes,
            max_batch_bytes=max_batch_upload_bytes,
        )
    if ingestion_history_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Ingestion history routes require an authorization runtime.")
        if ingestion_history_cursor_secret is None:
            raise RuntimeError("Ingestion history routes require a cursor secret.")
        install_ingestion_history_routes(
            application,
            service=ingestion_history_service,
            cursor_secret=ingestion_history_cursor_secret,
        )
    if document_query_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Document query routes require an authorization runtime.")
        if document_query_cursor_secret is None:
            raise RuntimeError("Document query routes require a cursor secret.")
        install_document_query_routes(
            application,
            service=document_query_service,
            cursor_secret=document_query_cursor_secret,
        )
    if qa_session_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Q&A session routes require an authorization runtime.")
        if qa_session_cursor_secret is None:
            raise RuntimeError("Q&A session routes require a cursor secret.")
        install_qa_session_routes(
            application,
            service=qa_session_service,
            cursor_secret=qa_session_cursor_secret,
        )
    resolved_authorized_ask_service = authorized_ask_service
    if resolved_authorized_ask_service is None and (
        answer_question_capability is not None and qa_session_service is not None
    ):
        resolved_authorized_ask_service = PersistedAuthorizedAskService(
            capability=answer_question_capability,
            sessions=qa_session_service,
        )
    if resolved_authorized_ask_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Authorized ask routes require an authorization runtime.")
        install_authorized_ask_routes(
            application,
            service=resolved_authorized_ask_service,
            stream_service=qa_stream_service,
            operational_observer=operational_observer,
        )
    if qa_stream_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Q&A stream routes require an authorization runtime.")
        install_qa_stream_routes(
            application,
            service=qa_stream_service,
            disclosure_authorizer=qa_stream_disclosure_authorizer,
            operational_observer=operational_observer,
        )
    if citation_inspector_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError(
                "Citation inspector routes require an authorization runtime."
            )
        install_citation_inspector_routes(
            application,
            service=citation_inspector_service,
        )
    if feedback_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Feedback routes require an authorization runtime.")
        install_feedback_routes(application, service=feedback_service)
    if qa_run_history_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError("Q&A run history routes require an authorization runtime.")
        if qa_run_history_cursor_secret is None:
            raise RuntimeError("Q&A run history routes require a cursor secret.")
        install_qa_run_routes(
            application,
            service=qa_run_history_service,
            cursor_secret=qa_run_history_cursor_secret,
        )
    if parsed_content_inspection_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError(
                "Parsed content routes require an authorization runtime."
            )
        if parsed_content_cursor_secret is None:
            raise RuntimeError("Parsed content routes require a cursor secret.")
        install_parsed_content_routes(
            application,
            service=parsed_content_inspection_service,
            cursor_secret=parsed_content_cursor_secret,
        )
    if processing_command_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError(
                "Processing command routes require an authorization runtime."
            )
        install_processing_command_routes(
            application,
            service=processing_command_service,
        )
    if document_lifecycle_command_service is not None:
        if type(authorization_runtime) is not ApiAuthorizationRuntime:
            raise RuntimeError(
                "Document lifecycle routes require an authorization runtime."
            )
        install_document_lifecycle_command_routes(
            application,
            service=document_lifecycle_command_service,
        )
    return application


app = create_app()


__all__ = ["app", "create_app", "get_diagnostic_context"]
