"""Administrator-only operational observation reads."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import Depends, FastAPI, Query

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_request_context,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.telemetry import (
    OperationalObservationPage,
    OperationalObservationQuery,
    OperationalObservationReader,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)


def install_operational_observation_routes(
    application: FastAPI,
    *,
    reader: OperationalObservationReader,
) -> None:
    application.state.operational_observation_reader = reader

    async def list_observations(
        trace_id: UUID | None = Query(None),
        observed_from: datetime | None = Query(None),
        observed_until: datetime | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
        context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> OperationalObservationPage:
        return await reader.read(
            query=OperationalObservationQuery(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                trace_id=trace_id,
                observed_from=observed_from,
                observed_until=observed_until,
                limit=limit,
            ),
            authority=context,
        )

    add_protected_api_route(
        application,
        "/api/v1/operational-observations",
        list_observations,
        route_id="operations.observations.list",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("operational_diagnostics"),
            operation=PersistenceOperation("observations.read"),
        ),
        methods=["GET"],
        response_model=OperationalObservationPage,
    )


__all__ = ["install_operational_observation_routes"]
