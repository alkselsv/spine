"""Administrator-only canonical document queries."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    ApiCollectionEnvelope,
    ApiResponseEnvelope,
    CursorCodec,
    DEFAULT_PAGE_LIMIT,
    InvalidCursor,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    canonical_filter_digest,
)
from spine.application.document_queries import (
    DocumentPage,
    DocumentNotFound,
    DocumentPublicationStatus,
    DocumentPosition,
    DocumentQueryFilters,
    DocumentQueryRequester,
    DocumentQueryService,
    DocumentSnapshot,
    RevisionPosition,
    RevisionSnapshot,
    RevisionLifecycle,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.ingestion import IngestionItemState
from spine.domain.projections import ProjectionHealth
from spine.domain.sources import SourceKind


_COLLECTION_OPERATION = "documents.list"
_DETAIL_OPERATION = "documents.detail"
_REVISIONS_OPERATION = "documents.revisions"


def install_document_query_routes(
    application: FastAPI,
    *,
    service: DocumentQueryService,
    cursor_secret: bytes,
) -> None:
    """Install scope-bound document collection queries."""

    codec = CursorCodec(secret=cursor_secret)
    application.state.document_query_service = service

    async def collection_endpoint(
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        source_kind: SourceKind | None = Query(
            None,
            description="Restrict results to a supported source kind.",
        ),
        lifecycle: RevisionLifecycle | None = Query(
            None,
            description="Restrict results to the current revision lifecycle.",
        ),
        processing_state: IngestionItemState | None = Query(
            None,
            description="Restrict results to documents with this processing state.",
        ),
        publication_status: DocumentPublicationStatus | None = Query(
            None,
            description="Restrict results to this publication status.",
        ),
        publication_health: ProjectionHealth | None = Query(
            None,
            description="Restrict published results to this publication health.",
        ),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiCollectionEnvelope[DocumentSnapshot]:
        _reject_unknown_collection_filters(request)
        try:
            filters = DocumentQueryFilters(
                source_kind=source_kind,
                lifecycle=lifecycle,
                processing_state=processing_state,
                publication_status=publication_status,
                publication_health=publication_health,
            )
        except ValueError:
            raise HTTPException(status_code=422) from None
        after = _decode_cursor(
            cursor,
            codec=codec,
            context=request_context,
            limit=limit,
            filters=filters,
        )
        page = await _service(request).list_documents(
            context=context,
            requester=DocumentQueryRequester(
                canonical_human_id=request_context.acting_subject_id,
                authorization_generation=request_context.authorization_generation,
                effective_team_ids=request_context.effective_team_ids,
            ),
            limit=limit,
            after=after,
            filters=filters,
        )
        return ApiCollectionEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            items=page.items,
            limit=limit,
            next_cursor=_next_cursor(
                page,
                codec=codec,
                context=request_context,
                limit=limit,
                filters=filters,
            ),
        )

    async def detail_endpoint(
        source_object_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[DocumentSnapshot]:
        snapshot = await _service(request).get_document(
            context=context,
            requester=DocumentQueryRequester(
                canonical_human_id=request_context.acting_subject_id,
                authorization_generation=request_context.authorization_generation,
                effective_team_ids=request_context.effective_team_ids,
            ),
            source_object_id=source_object_id,
        )
        if snapshot is None:
            raise DocumentNotFound()
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=snapshot,
        )

    async def revisions_endpoint(
        source_object_id: UUID,
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiCollectionEnvelope[RevisionSnapshot]:
        after = _decode_revision_cursor(
            cursor,
            codec=codec,
            context=request_context,
            source_object_id=source_object_id,
            limit=limit,
        )
        page = await _service(request).list_revisions(
            context=context,
            requester=DocumentQueryRequester(
                canonical_human_id=request_context.acting_subject_id,
                authorization_generation=request_context.authorization_generation,
                effective_team_ids=request_context.effective_team_ids,
            ),
            source_object_id=source_object_id,
            limit=limit,
            after=after,
        )
        if page is None:
            raise DocumentNotFound()
        return ApiCollectionEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            items=page.items,
            limit=limit,
            next_cursor=_next_revision_cursor(
                page,
                codec=codec,
                context=request_context,
                source_object_id=source_object_id,
                limit=limit,
            ),
        )

    add_protected_api_route(
        application,
        "/api/v1/documents",
        collection_endpoint,
        route_id=_COLLECTION_OPERATION,
        policy=_policy(),
        methods=["GET"],
        response_model=ApiCollectionEnvelope[DocumentSnapshot],
    )
    add_protected_api_route(
        application,
        "/api/v1/documents/{source_object_id}/revisions",
        revisions_endpoint,
        route_id=_REVISIONS_OPERATION,
        policy=_policy(),
        methods=["GET"],
        response_model=ApiCollectionEnvelope[RevisionSnapshot],
    )
    add_protected_api_route(
        application,
        "/api/v1/documents/{source_object_id}",
        detail_endpoint,
        route_id=_DETAIL_OPERATION,
        policy=_policy(),
        methods=["GET"],
        response_model=ApiResponseEnvelope[DocumentSnapshot],
    )


def _service(request: Request) -> DocumentQueryService:
    service = getattr(request.app.state, "document_query_service", None)
    if not callable(getattr(service, "list_documents", None)) or not callable(
        getattr(service, "get_document", None)
    ) or not callable(
        getattr(service, "list_revisions", None)
    ):
        raise RuntimeError("Document query service is unavailable.")
    return cast(DocumentQueryService, service)


def _decode_cursor(
    value: str | None,
    *,
    codec: CursorCodec,
    context: AuthorizedRequestContext[object],
    limit: int,
    filters: DocumentQueryFilters,
) -> DocumentPosition | None:
    if value is None:
        return None
    claims = codec.decode(
        value,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        operation_id=_COLLECTION_OPERATION,
        limit=limit,
        filters_digest=_document_filters_digest(filters),
    )
    try:
        timestamp, source_object_id = claims.position.rsplit("|", 1)
        return DocumentPosition(
            created_at=datetime.fromisoformat(timestamp),
            source_object_id=UUID(source_object_id),
        )
    except (TypeError, ValueError):
        raise InvalidCursor() from None


def _next_cursor(
    page: DocumentPage[DocumentSnapshot],
    *,
    codec: CursorCodec,
    context: AuthorizedRequestContext[object],
    limit: int,
    filters: DocumentQueryFilters,
) -> str | None:
    if not page.has_more or not page.items:
        return None
    last = page.items[-1]
    return codec.encode(
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        operation_id=_COLLECTION_OPERATION,
        limit=limit,
        filters_digest=_document_filters_digest(filters),
        position=f"{last.created_at.isoformat()}|{last.source_object_id}",
    )


def _decode_revision_cursor(
    value: str | None,
    *,
    codec: CursorCodec,
    context: AuthorizedRequestContext[object],
    source_object_id: UUID,
    limit: int,
) -> RevisionPosition | None:
    if value is None:
        return None
    claims = codec.decode(
        value,
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        operation_id=_REVISIONS_OPERATION,
        limit=limit,
        filters_digest=canonical_filter_digest(
            {"source_object_id": str(source_object_id)}
        ),
    )
    try:
        timestamp, source_revision_id = claims.position.rsplit("|", 1)
        return RevisionPosition(
            observed_at=datetime.fromisoformat(timestamp),
            source_revision_id=UUID(source_revision_id),
        )
    except (TypeError, ValueError):
        raise InvalidCursor() from None


def _next_revision_cursor(
    page: DocumentPage[RevisionSnapshot],
    *,
    codec: CursorCodec,
    context: AuthorizedRequestContext[object],
    source_object_id: UUID,
    limit: int,
) -> str | None:
    if not page.has_more or not page.items:
        return None
    last = page.items[-1]
    return codec.encode(
        workspace_id=context.scope.workspace_id,
        environment_id=context.scope.environment_id,
        operation_id=_REVISIONS_OPERATION,
        limit=limit,
        filters_digest=canonical_filter_digest(
            {"source_object_id": str(source_object_id)}
        ),
        position=f"{last.observed_at.isoformat()}|{last.source_revision_id}",
    )


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_documents"),
    )


def _reject_unknown_collection_filters(request: Request) -> None:
    allowed = {
        "limit",
        "cursor",
        "source_kind",
        "lifecycle",
        "processing_state",
        "publication_status",
        "publication_health",
    }
    if any(key not in allowed for key in request.query_params):
        raise HTTPException(status_code=422)


def _document_filters_digest(filters: DocumentQueryFilters) -> str:
    return canonical_filter_digest(filters.digest_payload())


__all__ = ["install_document_query_routes"]
