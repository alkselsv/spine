"""Protected, cursor-paginated ingestion history queries."""

from __future__ import annotations

from datetime import datetime
from typing import TypeVar, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.batch_ingestion import (
    BatchIngestionFailureResponse,
    BatchIngestionItemResponse,
    BatchIngestionWarningResponse,
    PublicBatchStatus,
)
from spine.api.ingestion import (
    IngestionReceiptResponse,
    PublicAcceptanceStatus,
    PublicIngestionStatus,
    PublicProcessingStatus,
)
from spine.api.schemas import (
    ApiCollectionEnvelope,
    CursorCodec,
    DEFAULT_PAGE_LIMIT,
    InvalidCursor,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    canonical_filter_digest,
)
from spine.application.ingestion_history import (
    DocumentProcessingHistorySnapshot,
    IngestionHistoryFailureSnapshot,
    IngestionHistoryPage,
    IngestionHistoryPosition,
    IngestionHistoryService,
    IngestionHistoryWarningSnapshot,
    IngestionRunHistorySnapshot,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.ingestion import IngestionItemState, IngestionRunState, ProcessingCoverage


_RUNS_OPERATION = "ingestion.run_history"
_PROCESSING_OPERATION = "documents.processing_history"
_EMPTY_FILTER_DIGEST = canonical_filter_digest({})
EnvelopeItemT = TypeVar("EnvelopeItemT")


class DocumentProcessingHistoryResponse(BatchIngestionItemResponse):
    run_id: UUID
    run_state: str
    status_url: str


class IngestionRunHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    requested_at: datetime
    status: PublicBatchStatus
    run_state: str
    acceptance: PublicAcceptanceStatus
    processing: PublicProcessingStatus
    terminal: bool
    status_url: str
    items: tuple[DocumentProcessingHistoryResponse, ...]


def install_ingestion_history_routes(
    application: FastAPI,
    *,
    service: IngestionHistoryService,
    cursor_secret: bytes,
) -> None:
    """Register administrator-only run and document processing collections."""

    codec = CursorCodec(secret=cursor_secret)
    application.state.ingestion_history_service = service

    async def runs_endpoint(
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiCollectionEnvelope[IngestionRunHistoryResponse]:
        after = _decode_cursor(
            cursor,
            codec=codec,
            context=request_context,
            operation=_RUNS_OPERATION,
            limit=limit,
        )
        page = await _service(request).list_runs(
            context=context,
            limit=limit,
            after=after,
        )
        snapshots = page.items
        items = tuple(
            _run_response(request, snapshot)
            for snapshot in snapshots
        )
        return _envelope(
            request_context=request_context,
            items=items,
            limit=limit,
            next_cursor=_next_cursor(
                snapshots[-1].requested_at,
                snapshots[-1].run_id,
                page=page,
                codec=codec,
                context=request_context,
                operation=_RUNS_OPERATION,
                limit=limit,
            ) if snapshots else None,
        )

    async def processing_endpoint(
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiCollectionEnvelope[DocumentProcessingHistoryResponse]:
        after = _decode_cursor(
            cursor,
            codec=codec,
            context=request_context,
            operation=_PROCESSING_OPERATION,
            limit=limit,
        )
        page = await _service(request).list_processing_history(
            context=context,
            limit=limit,
            after=after,
        )
        snapshots = page.items
        items = tuple(
            _processing_response(request, snapshot)
            for snapshot in snapshots
        )
        return _envelope(
            request_context=request_context,
            items=items,
            limit=limit,
            next_cursor=_next_cursor(
                snapshots[-1].occurred_at,
                snapshots[-1].item_id,
                page=page,
                codec=codec,
                context=request_context,
                operation=_PROCESSING_OPERATION,
                limit=limit,
            ) if snapshots else None,
        )

    policy = _policy()
    add_protected_api_route(
        application,
        "/api/v1/ingestion/runs",
        runs_endpoint,
        route_id=_RUNS_OPERATION,
        policy=policy,
        methods=["GET"],
        response_model=ApiCollectionEnvelope[IngestionRunHistoryResponse],
    )
    add_protected_api_route(
        application,
        "/api/v1/documents/processing-history",
        processing_endpoint,
        route_id=_PROCESSING_OPERATION,
        policy=policy,
        methods=["GET"],
        response_model=ApiCollectionEnvelope[DocumentProcessingHistoryResponse],
    )


def _service(request: Request) -> IngestionHistoryService:
    service = getattr(request.app.state, "ingestion_history_service", None)
    if not callable(getattr(service, "list_runs", None)) or not callable(
        getattr(service, "list_processing_history", None)
    ):
        raise RuntimeError("Ingestion history service is unavailable.")
    return cast(IngestionHistoryService, service)


def _run_response(
    request: Request,
    snapshot: IngestionRunHistorySnapshot,
) -> IngestionRunHistoryResponse:
    item_responses = tuple(
        DocumentProcessingHistoryResponse(
            **(_processing_item_response(item, run_state=snapshot.run_state).model_dump()),
            run_id=snapshot.run_id,
            run_state=snapshot.run_state.value,
            status_url=_absolute_url(
                request,
                f"/api/v1/ingestion/runs/{snapshot.run_id}/items/{item.item_id}",
            ),
        )
        for item in snapshot.items
    )
    status, acceptance, processing = _aggregate_run(snapshot.run_state, item_responses)
    return IngestionRunHistoryResponse(
        run_id=snapshot.run_id,
        requested_at=snapshot.requested_at,
        status=status,
        run_state=snapshot.run_state.value,
        acceptance=acceptance,
        processing=processing,
        terminal=status is not PublicBatchStatus.PROCESSING,
        status_url=_absolute_url(request, f"/api/v1/ingestion/runs/{snapshot.run_id}"),
        items=item_responses,
    )


def _processing_response(
    request: Request,
    snapshot: DocumentProcessingHistorySnapshot,
) -> DocumentProcessingHistoryResponse:
    item = _processing_item_response(snapshot, run_state=snapshot.run_state)
    return DocumentProcessingHistoryResponse(
        **item.model_dump(),
        run_id=snapshot.run_id,
        run_state=snapshot.run_state.value,
        status_url=_absolute_url(
            request,
            f"/api/v1/ingestion/runs/{snapshot.run_id}/items/{snapshot.item_id}",
        ),
    )


def _processing_item_response(
    item: DocumentProcessingHistorySnapshot,
    *,
    run_state: IngestionRunState,
) -> BatchIngestionItemResponse:
    status = _item_status(item, run_state=run_state)
    acceptance, processing = _item_phases(item, status=status)
    receipt = None
    if item.processing_receipt_id is not None and item.processing_coverage is not None:
        receipt = IngestionReceiptResponse(
            receipt_id=item.processing_receipt_id,
            parser_name=item.parser_name or "unknown",
            coverage=item.processing_coverage.value,
            artifact_id=item.artifact_id,
            artifact_digest=item.artifact_digest,
        )
    return BatchIngestionItemResponse(
        item_id=item.item_id,
        input_ordinal=item.input_ordinal,
        input_identity=item.input_identity,
        status=status,
        item_state=item.item_state.value,
        acceptance=acceptance,
        processing=processing,
        terminal=_item_terminal(item, run_state=run_state),
        retryable=item.retryable,
        attempt=item.attempt,
        coverage=item.processing_coverage.value if item.processing_coverage else None,
        source_object_id=item.source_object_id,
        source_revision_id=item.source_revision_id,
        binding_id=item.binding_id,
        processing_receipt=receipt,
        warning_ids=item.warning_ids,
        failure_ids=item.failure_ids,
        warnings=tuple(
            BatchIngestionWarningResponse(
                warning_id=warning.warning_id,
                attempt=warning.attempt,
                stage=warning.stage,
                code=warning.code,
            )
            if isinstance(warning, IngestionHistoryWarningSnapshot)
            else BatchIngestionWarningResponse(**warning)
            for warning in item.warnings
        ),
        failures=tuple(
            BatchIngestionFailureResponse(
                failure_id=failure.failure_id,
                attempt=failure.attempt,
                stage=failure.stage,
                code=failure.code,
                retryable=failure.retryable,
            )
            if isinstance(failure, IngestionHistoryFailureSnapshot)
            else BatchIngestionFailureResponse(**failure)
            for failure in item.failures
        ),
    )


def _item_status(
    item: DocumentProcessingHistorySnapshot,
    *,
    run_state: IngestionRunState,
) -> PublicIngestionStatus:
    if item.item_state is IngestionItemState.QUARANTINED:
        return PublicIngestionStatus.QUARANTINED
    if item.item_state is IngestionItemState.REJECTED:
        return PublicIngestionStatus.REJECTED
    if item.parser_name == "unsupported":
        return PublicIngestionStatus.UNSUPPORTED
    if item.processing_coverage is ProcessingCoverage.COMPLETE:
        return PublicIngestionStatus.SUCCEEDED
    if item.processing_coverage is ProcessingCoverage.PARTIAL:
        return PublicIngestionStatus.PARTIAL
    if item.processing_coverage is ProcessingCoverage.FAILED or item.item_state is IngestionItemState.PROCESSING_FAILED:
        return PublicIngestionStatus.FAILED
    if run_state is IngestionRunState.CANCELLED:
        return PublicIngestionStatus.CANCELLED
    return PublicIngestionStatus.NON_TERMINAL


def _item_phases(
    item: DocumentProcessingHistorySnapshot,
    *,
    status: PublicIngestionStatus,
) -> tuple[PublicAcceptanceStatus, PublicProcessingStatus]:
    if item.item_state is IngestionItemState.QUARANTINED:
        return PublicAcceptanceStatus.QUARANTINED, PublicProcessingStatus.NOT_STARTED
    if item.item_state is IngestionItemState.REJECTED:
        return PublicAcceptanceStatus.REJECTED, PublicProcessingStatus.NOT_STARTED
    accepted = item.item_state in {
        IngestionItemState.ACCEPTED,
        IngestionItemState.PROCESSING,
        IngestionItemState.PROCESSED,
        IngestionItemState.PROJECTION_REQUESTED,
        IngestionItemState.PROCESSING_FAILED,
    }
    acceptance = PublicAcceptanceStatus.ACCEPTED if accepted else PublicAcceptanceStatus.PENDING
    processing = {
        PublicIngestionStatus.SUCCEEDED: PublicProcessingStatus.SUCCEEDED,
        PublicIngestionStatus.PARTIAL: PublicProcessingStatus.PARTIAL,
        PublicIngestionStatus.FAILED: PublicProcessingStatus.FAILED,
        PublicIngestionStatus.UNSUPPORTED: PublicProcessingStatus.UNSUPPORTED,
        PublicIngestionStatus.CANCELLED: PublicProcessingStatus.CANCELLED,
    }.get(
        status,
        PublicProcessingStatus.PROCESSING
        if item.item_state is IngestionItemState.PROCESSING
        else PublicProcessingStatus.NOT_STARTED,
    )
    return acceptance, processing


def _item_terminal(
    item: DocumentProcessingHistorySnapshot,
    *,
    run_state: IngestionRunState,
) -> bool:
    return run_state is IngestionRunState.CANCELLED or item.item_state in {
        IngestionItemState.QUARANTINED,
        IngestionItemState.REJECTED,
        IngestionItemState.PROCESSED,
        IngestionItemState.PROJECTION_REQUESTED,
        IngestionItemState.PROCESSING_FAILED,
    }


def _aggregate_run(
    run_state: IngestionRunState,
    items: tuple[BatchIngestionItemResponse, ...],
) -> tuple[PublicBatchStatus, PublicAcceptanceStatus, PublicProcessingStatus]:
    if run_state is IngestionRunState.CANCELLED:
        return PublicBatchStatus.CANCELLED, PublicAcceptanceStatus.ACCEPTED, PublicProcessingStatus.CANCELLED
    if not items or any(item.status is PublicIngestionStatus.NON_TERMINAL for item in items):
        return PublicBatchStatus.PROCESSING, PublicAcceptanceStatus.PENDING, PublicProcessingStatus.PROCESSING
    successful = {PublicIngestionStatus.SUCCEEDED, PublicIngestionStatus.PARTIAL}
    success = [item for item in items if item.status in successful]
    unsuccessful = [item for item in items if item.status not in successful]
    status = (
        PublicBatchStatus.PARTIAL
        if unsuccessful and success or any(item.status is PublicIngestionStatus.PARTIAL for item in success)
        else PublicBatchStatus.SUCCEEDED
        if success
        else PublicBatchStatus.FAILED
    )
    acceptance = (
        PublicAcceptanceStatus.REJECTED
        if all(item.acceptance is PublicAcceptanceStatus.REJECTED for item in items)
        else PublicAcceptanceStatus.QUARANTINED
        if all(item.acceptance is PublicAcceptanceStatus.QUARANTINED for item in items)
        else PublicAcceptanceStatus.ACCEPTED
    )
    processing = (
        PublicProcessingStatus.PARTIAL
        if status is PublicBatchStatus.PARTIAL
        else PublicProcessingStatus.SUCCEEDED
        if status is PublicBatchStatus.SUCCEEDED
        else PublicProcessingStatus.FAILED
    )
    return status, acceptance, processing


def _decode_cursor(
    value: str | None,
    *,
    codec: CursorCodec,
    context: AuthorizedRequestContext[object],
    operation: str,
    limit: int,
) -> IngestionHistoryPosition | None:
    if value is None:
        return None
    claims = codec.decode(
        value,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        operation_id=operation,
        limit=limit,
        filters_digest=_EMPTY_FILTER_DIGEST,
    )
    try:
        timestamp, identity = claims.position.rsplit("|", 1)
        return IngestionHistoryPosition(
            occurred_at=datetime.fromisoformat(timestamp),
            identity=UUID(identity),
        )
    except (TypeError, ValueError):
        raise InvalidCursor() from None


def _next_cursor(
    occurred_at: datetime,
    identity: UUID,
    *,
    page: IngestionHistoryPage,
    codec: CursorCodec,
    context: AuthorizedRequestContext[object],
    operation: str,
    limit: int,
) -> str | None:
    if not page.has_more:
        return None
    return codec.encode(
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        operation_id=operation,
        limit=limit,
        filters_digest=_EMPTY_FILTER_DIGEST,
        position=f"{occurred_at.isoformat()}|{identity}",
    )


def _envelope(
    *,
    request_context: AuthorizedRequestContext[object],
    items: tuple[EnvelopeItemT, ...],
    limit: int,
    next_cursor: str | None,
) -> ApiCollectionEnvelope[EnvelopeItemT]:
    return ApiCollectionEnvelope(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        trace_id=request_context.diagnostic_context.trace_id,
        items=items,
        limit=limit,
        next_cursor=next_cursor,
    )


def _absolute_url(request: Request, path: str) -> str:
    return str(request.base_url).rstrip("/") + path


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_ingestion_history"),
    )


__all__ = [
    "DocumentProcessingHistoryResponse",
    "IngestionRunHistoryResponse",
    "install_ingestion_history_routes",
]
