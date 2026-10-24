"""Protected projection publication status and recovery command routes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    ApiCommandResult,
    ApiResponseEnvelope,
    parse_idempotency_key_header,
)
from spine.application.persistence import (
    PersistenceOperation,
    PersistencePurpose,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.projection_control import (
    InvalidProjectionControlRequest,
    ProjectionControlServiceUnavailable,
    ProjectionRecoveryAction,
    ProjectionRecoveryCommandService,
    ProjectionPublicationStatus,
    ProjectionSuccessorStatusSnapshot,
    ProjectionStatusService,
    ProjectionStatusSnapshot,
    UnavailableProjectionStatusService,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.projections import (
    PROJECTION_KIND_PATTERN,
    ProjectionFreshness,
    ProjectionHealth,
    ProjectionKind,
    ProjectionReconciliationOutcome,
)


_STATUS_ROUTE = "projection.status"
_RECOVERY_ROUTE = "projection.recovery"
_IDEMPOTENCY_HEADER = "Idempotency-Key"


class ProjectionAsOfResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_boundary_id: UUID
    accepted_through_generation: int = Field(ge=0)
    boundary_time: datetime


class ProjectionReconciliationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: UUID
    outcome: ProjectionReconciliationOutcome
    checked_at: datetime


class ProjectionOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal[
        "build",
        "catch_up",
        "rebuild",
        "rollback",
        "publish",
        "reconciliation",
    ]
    status: Literal["accepted", "running", "completed", "failed", "unavailable"]
    completed_at: datetime | None = None
    retryable: bool = False


class ProjectionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    projection_kind: ProjectionKind
    publication_status: ProjectionPublicationStatus
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$")
    snapshot_id: UUID | None = None
    activation_id: UUID | None = None
    canonical_boundary_id: UUID | None = None
    as_of: ProjectionAsOfResponse | None = None
    activation_generation: int | None = Field(default=None, ge=1)
    projection_config_version_id: UUID | None = None
    projection_config_version: int | None = Field(default=None, ge=1)
    freshness: ProjectionFreshness | None = None
    health: ProjectionHealth | None = None
    drift_count: int | None = Field(default=None, ge=0)
    reconciliation: ProjectionReconciliationResponse | None = None
    last_operation: ProjectionOperationResponse | None = None
    retryable: bool = False
    recovery_state: Literal[
        "not_requested", "pending", "succeeded", "failed", "unavailable"
    ] = "not_requested"


class ProjectionRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ProjectionRecoveryAction


class ProjectionRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    projection_kind: ProjectionKind
    action: ProjectionRecoveryAction
    state: Literal["accepted"] = "accepted"


class ProjectionSuccessorStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    intent_id: UUID
    projection_kind: ProjectionKind
    status: Literal["accepted", "running", "completed", "failed", "unavailable"]
    run_id: UUID | None = None
    candidate_id: UUID | None = None
    receipt_ids: tuple[UUID, ...] = ()
    snapshot_id: UUID | None = None
    activation_id: UUID | None = None


def install_projection_control_routes(
    application: FastAPI,
    *,
    status_service: ProjectionStatusService | None = None,
    recovery_service: ProjectionRecoveryCommandService | None = None,
) -> None:
    """Register administrator-only status and request-only recovery routes."""

    resolved_status = status_service or UnavailableProjectionStatusService()
    application.state.projection_status_service = resolved_status
    application.state.projection_recovery_service = recovery_service

    async def status_endpoint(
        request: Request,
        projection_kind: ProjectionKind = Path(pattern=PROJECTION_KIND_PATTERN),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[ProjectionStatusResponse]:
        del request
        snapshot = await _status_service(request_context, resolved_status).status(
            context=context,
            projection_kind=projection_kind,
        )
        snapshot = _scope_safe_snapshot(request_context, snapshot, projection_kind)
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=_status_response(snapshot),
        )

    async def recovery_endpoint(
        body: ProjectionRecoveryRequest,
        request: Request,
        projection_kind: ProjectionKind = Path(pattern=PROJECTION_KIND_PATTERN),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiCommandResult[ProjectionRecoveryResponse]:
        try:
            key = parse_idempotency_key_header(
                request.headers.getlist(_IDEMPOTENCY_HEADER)
            )
        except ValueError:
            raise InvalidProjectionControlRequest() from None
        service = _recovery_service(request, recovery_service)
        result = await service.request(
            context=context,
            projection_kind=projection_kind,
            action=body.action,
            idempotency_key=key,
        )
        return ApiCommandResult(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            command_id=result.command_id,
            status="accepted",
            replay=result.replay,
            result=ProjectionRecoveryResponse(
                command_id=result.command_id,
                projection_kind=result.projection_kind,
                action=result.action,
            ),
        )

    async def successor_status_endpoint(
        request: Request,
        command_id: UUID,
        projection_kind: ProjectionKind = Path(pattern=PROJECTION_KIND_PATTERN),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[ProjectionSuccessorStatusResponse]:
        snapshot = await _recovery_service(request, recovery_service).successor_status(
            context=context,
            projection_kind=projection_kind,
            command_id=command_id,
        )
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=_successor_status_response(snapshot),
        )
    policy = _policy("inspect_projection_status")
    add_protected_api_route(
        application,
        "/api/v1/projections/{projection_kind}/status",
        status_endpoint,
        route_id=_STATUS_ROUTE,
        policy=policy,
        methods=["GET"],
        response_model=ApiResponseEnvelope[ProjectionStatusResponse],
    )
    add_protected_api_route(
        application,
        "/api/v1/projections/{projection_kind}/recovery",
        recovery_endpoint,
        route_id=_RECOVERY_ROUTE,
        policy=_policy("request_projection_recovery"),
        methods=["POST"],
        status_code=202,
        response_model=ApiCommandResult[ProjectionRecoveryResponse],
        openapi_extra={
            "parameters": [
                {
                    "name": _IDEMPOTENCY_HEADER,
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 255},
                }
            ]
        },
    )
    add_protected_api_route(
        application,
        "/api/v1/projections/{projection_kind}/successors/{command_id}",
        successor_status_endpoint,
        route_id="projection.successor_status",
        policy=policy,
        methods=["GET"],
        response_model=ApiResponseEnvelope[ProjectionSuccessorStatusResponse],
    )


def _status_service(
    request_context: AuthorizedRequestContext[object],
    service: ProjectionStatusService,
) -> ProjectionStatusService:
    del request_context
    if not callable(getattr(service, "status", None)):
        return UnavailableProjectionStatusService()
    return service


def _recovery_service(
    request: Request,
    service: ProjectionRecoveryCommandService | None,
) -> ProjectionRecoveryCommandService:
    del request
    if service is None or not callable(getattr(service, "request", None)):
        raise ProjectionControlServiceUnavailable()
    return service


def _scope_safe_snapshot(
    request_context: AuthorizedRequestContext[object],
    snapshot: ProjectionStatusSnapshot,
    projection_kind: ProjectionKind,
) -> ProjectionStatusSnapshot:
    if (
        snapshot.workspace_id != request_context.scope.workspace_id
        or snapshot.environment_id != request_context.scope.environment_id
        or snapshot.projection_kind != projection_kind
    ):
        return ProjectionStatusSnapshot.unavailable(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            projection_kind=projection_kind,
        )
    return snapshot


def _status_response(snapshot: ProjectionStatusSnapshot) -> ProjectionStatusResponse:
    return ProjectionStatusResponse(
        projection_kind=snapshot.projection_kind,
        publication_status=snapshot.publication_status,
        reason_code=snapshot.reason_code,
        snapshot_id=snapshot.snapshot_id,
        activation_id=snapshot.activation_id,
        canonical_boundary_id=snapshot.canonical_boundary_id,
        as_of=(
            ProjectionAsOfResponse(
                canonical_boundary_id=snapshot.as_of.canonical_boundary_id,
                accepted_through_generation=snapshot.as_of.accepted_through_generation,
                boundary_time=snapshot.as_of.boundary_time,
            )
            if snapshot.as_of is not None
            else None
        ),
        activation_generation=snapshot.activation_generation,
        projection_config_version_id=snapshot.projection_config_version_id,
        projection_config_version=snapshot.projection_config_version,
        freshness=snapshot.freshness,
        health=snapshot.health,
        drift_count=snapshot.drift_count,
        reconciliation=(
            ProjectionReconciliationResponse(
                result_id=snapshot.reconciliation.result_id,
                outcome=snapshot.reconciliation.outcome,
                checked_at=snapshot.reconciliation.checked_at,
            )
            if snapshot.reconciliation is not None
            else None
        ),
        last_operation=(
            ProjectionOperationResponse(
                operation=snapshot.last_operation.operation,
                status=snapshot.last_operation.status,
                completed_at=snapshot.last_operation.completed_at,
                retryable=snapshot.last_operation.retryable,
            )
            if snapshot.last_operation is not None
            else None
        ),
        retryable=snapshot.retryable,
        recovery_state=snapshot.recovery_state,
    )


def _successor_status_response(
    snapshot: ProjectionSuccessorStatusSnapshot,
) -> ProjectionSuccessorStatusResponse:
    return ProjectionSuccessorStatusResponse(**snapshot.model_dump())


def _policy(operation: str) -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation(operation),
    )


__all__ = [
    "ProjectionRecoveryRequest",
    "ProjectionRecoveryResponse",
    "ProjectionSuccessorStatusResponse",
    "ProjectionStatusResponse",
    "install_projection_control_routes",
]
