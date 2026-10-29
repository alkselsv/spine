"""Administrator control-plane routes for provider-neutral source connections."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    ApiCollectionEnvelope,
    ApiCommandResult,
    ApiResponseEnvelope,
    CursorCodec,
    DEFAULT_PAGE_LIMIT,
    InvalidCursor,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    canonical_filter_digest,
    parse_idempotency_key_header,
)
from spine.application.connections.management import (
    InvalidProviderConnectionRequest,
    ProviderConnectionConfigurationRequest,
    ProviderConnectionInspection,
    ProviderConnectionManager,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.connections.provider import (
    ProviderCapabilityKind,
    ProviderCapabilityStatus,
    ProviderConnectionHealth,
    ProviderConnectionStatus,
    ProviderDiagnosticCode,
    ProviderKind,
)


_IDEMPOTENCY_HEADER = "Idempotency-Key"
_LIST_OPERATION = "source_connections.list"
_LIST_FILTERS_DIGEST = canonical_filter_digest({})


class ProviderConnectionConfigurationBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    connection_id: UUID
    expected_version: int = Field(ge=0)
    expected_predecessor_version_id: UUID | None = None

    @model_validator(mode="after")
    def validate_predecessor(self) -> "ProviderConnectionConfigurationBody":
        if self.expected_version == 0 and self.expected_predecessor_version_id is not None:
            raise ValueError("initial connection cannot have a predecessor")
        if self.expected_version > 0 and self.expected_predecessor_version_id is None:
            raise ValueError("successor connection requires a predecessor")
        return self


class ProviderCapabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ProviderCapabilityKind
    status: ProviderCapabilityStatus
    qualification_reference: UUID | None = None


class ProviderConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    connection_id: UUID
    connection_version_id: UUID
    approval_id: UUID
    version: int = Field(ge=1)
    predecessor_version_id: UUID | None
    provider_kind: ProviderKind
    status: ProviderConnectionStatus
    authorization_generation: int = Field(ge=1)
    configuration_generation: int = Field(ge=1)
    approved_resource_scope_references: tuple[UUID, ...]
    capabilities: tuple[ProviderCapabilityResponse, ...]
    health: ProviderConnectionHealth
    diagnostic_codes: tuple[ProviderDiagnosticCode, ...]
    observed_at: datetime
    cursor_lag_seconds: int | None = Field(default=None, ge=0)
    last_successful_reconciliation_at: datetime | None = None


def install_provider_connection_routes(
    application: FastAPI,
    *,
    service: ProviderConnectionManager,
    cursor_secret: bytes,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Register scoped create/list/inspect routes for source connections."""

    resolved_clock = clock or (lambda: datetime.now(timezone.utc))
    cursor_codec = CursorCodec(secret=cursor_secret)

    async def configure_endpoint(
        body: ProviderConnectionConfigurationBody,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiCommandResult[ProviderConnectionResponse]:
        try:
            key = parse_idempotency_key_header(
                request.headers.getlist(_IDEMPOTENCY_HEADER)
            )
        except ValueError:
            raise InvalidProviderConnectionRequest() from None
        result = await service.configure(
            ProviderConnectionConfigurationRequest(
                approval_id=body.approval_id,
                connection_id=body.connection_id,
                expected_version=body.expected_version,
                expected_predecessor_version_id=(
                    body.expected_predecessor_version_id
                ),
                idempotency_key=key.value,
                occurred_at=resolved_clock(),
            ),
            context,
        )
        return ApiCommandResult(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            command_id=result.receipt_id,
            status="completed",
            replay=result.replay,
            result=_response(result.inspection),
        )

    async def list_endpoint(
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(default=None, max_length=MAX_CURSOR_LENGTH),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiCollectionEnvelope[ProviderConnectionResponse]:
        after_connection_id = None
        if cursor is not None:
            claims = cursor_codec.decode(
                cursor,
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                operation_id=_LIST_OPERATION,
                limit=limit,
                filters_digest=_LIST_FILTERS_DIGEST,
            )
            try:
                after_connection_id = UUID(claims.position)
            except ValueError:
                raise InvalidCursor() from None
        page = await service.list(
            context=context,
            after_connection_id=after_connection_id,
            limit=limit,
        )
        next_cursor = None
        if page.has_more and page.items:
            next_cursor = cursor_codec.encode(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                operation_id=_LIST_OPERATION,
                limit=limit,
                filters_digest=_LIST_FILTERS_DIGEST,
                position=str(page.items[-1].connection.connection_id),
            )
        return ApiCollectionEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            items=tuple(_response(item) for item in page.items),
            limit=limit,
            next_cursor=next_cursor,
        )

    async def inspect_endpoint(
        connection_id: UUID,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[ProviderConnectionResponse]:
        item = await service.inspect(connection_id=connection_id, context=context)
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=_response(item),
        )

    add_protected_api_route(
        application,
        "/api/v1/source-connections",
        configure_endpoint,
        route_id="source_connections.configure",
        policy=_policy("provider_connection_configure"),
        methods=["POST"],
        status_code=201,
        response_model=ApiCommandResult[ProviderConnectionResponse],
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
        "/api/v1/source-connections",
        list_endpoint,
        route_id="source_connections.list",
        policy=_policy("provider_connection_list"),
        methods=["GET"],
        response_model=ApiCollectionEnvelope[ProviderConnectionResponse],
    )
    add_protected_api_route(
        application,
        "/api/v1/source-connections/{connection_id}",
        inspect_endpoint,
        route_id="source_connections.inspect",
        policy=_policy("provider_connection_inspect"),
        methods=["GET"],
        response_model=ApiResponseEnvelope[ProviderConnectionResponse],
    )


def _response(item: ProviderConnectionInspection) -> ProviderConnectionResponse:
    connection = item.connection
    return ProviderConnectionResponse(
        connection_id=connection.connection_id,
        connection_version_id=connection.connection_version_id,
        approval_id=connection.approval_id,
        version=connection.version,
        predecessor_version_id=connection.predecessor_version_id,
        provider_kind=connection.provider_kind,
        status=connection.status,
        authorization_generation=connection.authorization_generation,
        configuration_generation=connection.configuration_generation,
        approved_resource_scope_references=tuple(
            reference.value
            for reference in connection.approved_resource_scope_references
        ),
        capabilities=tuple(
            ProviderCapabilityResponse(
                kind=capability.kind,
                status=capability.status,
                qualification_reference=(
                    capability.qualification_reference.value
                    if capability.qualification_reference is not None
                    else None
                ),
            )
            for capability in connection.capabilities
        ),
        health=item.health,
        diagnostic_codes=item.diagnostic_codes,
        observed_at=item.observed_at,
        cursor_lag_seconds=item.cursor_lag_seconds,
        last_successful_reconciliation_at=(
            item.last_successful_reconciliation_at
        ),
    )


def _policy(operation: str) -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("source_connection_control"),
        operation=PersistenceOperation(operation),
    )


__all__ = [
    "ProviderCapabilityResponse",
    "ProviderConnectionConfigurationBody",
    "ProviderConnectionResponse",
    "install_provider_connection_routes",
]
