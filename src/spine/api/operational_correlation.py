"""Administrator-only operational correlation reads."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict

from spine.api.authorization import (
    add_protected_api_route,
    get_authorization_audit_event_id,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.application.operational_correlation import (
    OperationalCorrelationReaderPort,
    OperationalCorrelationStoreUnavailable,
    UnavailableOperationalCorrelationReader,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.operational_correlation import (
    CorrelationReferenceKind,
    CorrelationSourceKind,
    OperationalCorrelationQuery,
    OperationalCorrelationView,
)


class OperationalCorrelationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    workspace_id: UUID
    environment_id: UUID
    trace_id: UUID
    authorization_audit_event_id: UUID
    data: OperationalCorrelationView


def install_operational_correlation_routes(
    application: FastAPI,
    *,
    reader: OperationalCorrelationReaderPort | None = None,
) -> None:
    """Install the bounded, read-only correlation endpoint."""

    resolved_reader = reader or UnavailableOperationalCorrelationReader()
    application.state.operational_correlation_reader = resolved_reader

    async def get_correlation(
        request: Request,
        reference_kind: CorrelationReferenceKind = Query(...),
        reference_id: UUID = Query(...),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
        authorization_audit_event_id: UUID = Depends(
            get_authorization_audit_event_id
        ),
        persistence_context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
    ) -> OperationalCorrelationResponse:
        query = OperationalCorrelationQuery(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            reference_kind=reference_kind,
            reference_id=reference_id,
        )
        try:
            data = await _reader(request).read(
                query=query,
                authority=request_context,
                context=persistence_context,
            )
        except OperationalCorrelationStoreUnavailable:
            data = OperationalCorrelationView(
                workspace_id=query.workspace_id,
                environment_id=query.environment_id,
                reference_kind=query.reference_kind,
                reference_id=query.reference_id,
                coverage="unavailable",
                missing_sources=tuple(CorrelationSourceKind),
            )
        return OperationalCorrelationResponse(
            workspace_id=query.workspace_id,
            environment_id=query.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            authorization_audit_event_id=authorization_audit_event_id,
            data=data,
        )

    add_protected_api_route(
        application,
        "/api/v1/diagnostics/operational-correlation",
        get_correlation,
        route_id="insights.operational-correlation.get",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("operational_correlation"),
            operation=PersistenceOperation("correlation.read"),
        ),
        methods=["GET"],
        response_model=OperationalCorrelationResponse,
    )


def _reader(request: Request) -> OperationalCorrelationReaderPort:
    return getattr(
        request.app.state,
        "operational_correlation_reader",
        UnavailableOperationalCorrelationReader(),
    )


__all__ = [
    "OperationalCorrelationResponse",
    "install_operational_correlation_routes",
]
