"""Administrator-only operational metrics reads."""

from __future__ import annotations

from datetime import datetime

from fastapi import Depends, FastAPI, Query

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_request_context,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.telemetry import (
    OperationalMetricsPage,
    OperationalMetricsQuery,
    OperationalMetricsReader,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.metrics import MetricWindow, OperationalMeasurement


def install_operational_metrics_routes(
    application: FastAPI,
    *,
    reader: OperationalMetricsReader,
) -> None:
    """Install the tenant-scoped, read-only metrics endpoint."""

    application.state.operational_metrics_reader = reader

    async def list_metrics(
        window_start: datetime = Query(...),
        window_end: datetime = Query(...),
        measurement: list[OperationalMeasurement] | None = Query(None),
        context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> OperationalMetricsPage:
        return await reader.read(
            query=OperationalMetricsQuery(
                workspace_id=context.scope.workspace_id,
                environment_id=context.scope.environment_id,
                window=MetricWindow(start=window_start, end=window_end),
                measurements=None if measurement is None else tuple(measurement),
            ),
            authority=context,
        )

    add_protected_api_route(
        application,
        "/api/v1/metrics/operational",
        list_metrics,
        route_id="insights.operational-metrics.list",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("operational_metrics"),
            operation=PersistenceOperation("metrics.read"),
        ),
        methods=["GET"],
        response_model=OperationalMetricsPage,
    )


__all__ = ["install_operational_metrics_routes"]
