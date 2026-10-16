"""Protected HTTP adapter for exact citation inspection."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import ApiResponseEnvelope
from spine.application.citation_inspector import (
    CitationInspectorRequester,
    CitationInspectorService,
    CitationInspectionUnavailable,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.domain.disclosure import (
    CitationInspectionMode,
    CitationInspectionResult,
)


def install_citation_inspector_routes(
    application: FastAPI,
    *,
    service: CitationInspectorService,
) -> None:
    """Register the administrator-protected citation inspector route."""

    application.state.citation_inspector_service = service

    async def inspect_endpoint(
        citation_id: UUID,
        request: Request,
        historical: bool = Query(
            False,
            description="Use only the explicitly permitted historical inspection flow.",
        ),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[CitationInspectionResult]:
        result = await _service(request).inspect(
            context=context,
            requester=CitationInspectorRequester(
                subject_id=request_context.acting_subject_id,
                authorization_generation=request_context.authorization_generation,
            ),
            citation_id=citation_id,
            mode=(
                CitationInspectionMode.HISTORICAL
                if historical
                else CitationInspectionMode.CURRENT
            ),
        )
        if result.evidence is None:
            raise CitationInspectionUnavailable()
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=result,
        )

    add_protected_api_route(
        application,
        "/api/v1/qa/citations/{citation_id}",
        inspect_endpoint,
        route_id="qa.citations.inspect",
        policy=_policy(),
        methods=["GET"],
        response_model=ApiResponseEnvelope[CitationInspectionResult],
    )


def _service(request: Request) -> CitationInspectorService:
    service = getattr(request.app.state, "citation_inspector_service", None)
    if service is None:
        raise RuntimeError("Citation inspector service is unavailable.")
    return cast(CitationInspectorService, service)


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_citations"),
    )


__all__ = ["install_citation_inspector_routes"]
