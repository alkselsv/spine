"""Administrator-only, read-only quarantine inspection endpoint."""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    CursorCodec,
    DEFAULT_PAGE_LIMIT,
    InvalidCursor,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    canonical_filter_digest,
)
from spine.application.outbox_inspection import (
    OutboxDeliveryInspectionItem,
    OutboxDeliveryInspectionPage,
    OutboxDeliveryInspectionService,
    UnavailableOutboxDeliveryInspectionService,
)
from spine.application.persistence.context import (
    TrustedPersistenceContext,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)


_ROUTE_ID = "outbox.deliveries.quarantined"
_COLLECTION_PATH = "/api/v1/admin/outbox/deliveries/quarantined"
_CURSOR_OPERATION = "outbox_quarantined_deliveries"
_FILTER_DIGEST = canonical_filter_digest({"status": "quarantined"})


class OutboxDeliveryInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    state: Literal["empty", "complete", "partial", "unavailable"]
    items: tuple[OutboxDeliveryInspectionItem, ...]
    limit: int = Field(ge=1, le=MAX_PAGE_LIMIT)
    has_more: bool
    next_cursor: str | None = Field(default=None, max_length=MAX_CURSOR_LENGTH)


def install_outbox_inspection_route(
    application: FastAPI,
    *,
    service: OutboxDeliveryInspectionService | None = None,
    cursor_secret: bytes | None = None,
) -> None:
    """Register the tenant-scoped inspection route after authorization."""

    resolved_service = service or UnavailableOutboxDeliveryInspectionService()
    if service is not None and cursor_secret is None:
        raise ValueError("a cursor secret is required for an active inspection service")
    codec = CursorCodec(secret=cursor_secret or secrets.token_bytes(32))
    application.state.outbox_delivery_inspection_service = resolved_service

    async def list_quarantined_endpoint(
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> OutboxDeliveryInspectionResponse:
        after = _decode_cursor(
            cursor,
            codec=codec,
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            limit=limit,
        )
        page = await _service(request).list_quarantined(
            context=context,
            limit=limit,
            after=after,
        )
        next_cursor = None
        if page.has_more and page.items:
            last = page.items[-1]
            next_cursor = codec.encode(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                operation_id=_CURSOR_OPERATION,
                limit=limit,
                filters_digest=_FILTER_DIGEST,
                position=f"{last.quarantined_at.isoformat()}|{last.event_id}",
            )
        return OutboxDeliveryInspectionResponse(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            state=page.state,
            items=page.items,
            limit=limit,
            has_more=page.has_more,
            next_cursor=next_cursor,
        )

    add_protected_api_route(
        application,
        _COLLECTION_PATH,
        list_quarantined_endpoint,
        route_id=_ROUTE_ID,
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("outbox_inspection"),
            operation=PersistenceOperation("inspect_quarantined_deliveries"),
        ),
        methods=["GET"],
        response_model=OutboxDeliveryInspectionResponse,
    )


def _decode_cursor(
    value: str | None,
    *,
    codec: CursorCodec,
    workspace_id: UUID,
    environment_id: UUID,
    limit: int,
) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    claims = codec.decode(
        value,
        workspace_id=workspace_id,
        environment_id=environment_id,
        operation_id=_CURSOR_OPERATION,
        limit=limit,
        filters_digest=_FILTER_DIGEST,
    )
    try:
        timestamp, event_id = claims.position.rsplit("|", 1)
        parsed_timestamp = datetime.fromisoformat(timestamp)
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise ValueError
        return parsed_timestamp, UUID(event_id)
    except (TypeError, ValueError):
        raise InvalidCursor() from None


def _service(request: Request) -> OutboxDeliveryInspectionService:
    service = getattr(request.app.state, "outbox_delivery_inspection_service", None)
    if not callable(getattr(service, "list_quarantined", None)):
        return UnavailableOutboxDeliveryInspectionService()
    return service


__all__ = [
    "OutboxDeliveryInspectionResponse",
    "install_outbox_inspection_route",
]
