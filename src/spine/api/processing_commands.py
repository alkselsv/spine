"""Versioned protected transport for document processing retry/reprocess."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import add_protected_api_route, get_authorized_persistence_context
from spine.api.schemas import parse_idempotency_key_header
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.application.processing_commands import (
    ProcessingCommandResult,
    ProcessingCommandService,
    ProcessingCommandStatus,
    ReprocessProcessingCommand,
    RetryProcessingCommand,
)
from spine.application.ingestion_api_errors import InvalidIngestionRequest, IngestionRunNotFound
from spine.auth import AuthorizationRole, AuthorizationScopeRequirement, RouteAuthorizationPolicy


class RetryProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    source_revision_id: UUID
    expected_current_revision_id: UUID
    parent_failure_id: UUID
    parent_processing_receipt_id: UUID | None = None


class ReprocessProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_object_id: UUID
    source_revision_id: UUID
    expected_current_revision_id: UUID
    parent_processing_receipt_id: UUID | None = None
    processing_profile_reference: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.:-]{0,127}$",
    )


class ProcessingCommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operation_id: UUID
    run_id: UUID
    item_id: UUID
    source_object_id: UUID
    source_revision_id: UUID
    trigger_kind: Literal["retry", "reprocess", "replacement"]
    status: ProcessingCommandStatus
    status_url: str
    replay: bool
    processing_receipt_id: UUID | None = None
    failure_ids: tuple[UUID, ...] = ()
    next_action: Literal["poll", "retry", "review", "none"]


def install_processing_command_routes(
    application: FastAPI,
    *,
    service: ProcessingCommandService,
) -> None:
    application.state.processing_command_service = service

    async def retry_endpoint(
        request: Request,
        body: RetryProcessingRequest,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> ProcessingCommandResponse:
        key = _idempotency_key(request)
        scope = _scope(context)
        result = await _service(request).retry(
            context=context,
            command=RetryProcessingCommand(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                idempotency_key=key,
                **body.model_dump(),
            ),
        )
        return _response(request, result)

    async def reprocess_endpoint(
        request: Request,
        body: ReprocessProcessingRequest,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> ProcessingCommandResponse:
        key = _idempotency_key(request)
        scope = _scope(context)
        result = await _service(request).reprocess(
            context=context,
            command=ReprocessProcessingCommand(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                idempotency_key=key,
                **body.model_dump(),
            ),
        )
        return _response(request, result)

    async def status_endpoint(
        request: Request,
        operation_id: UUID,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> ProcessingCommandResponse:
        result = await _service(request).status(context=context, operation_id=operation_id)
        if result is None:
            raise IngestionRunNotFound()
        return _response(request, result)

    policy = _policy("document_processing")
    add_protected_api_route(
        application,
        "/api/v1/documents/processing/retry",
        retry_endpoint,
        route_id="ingestion.processing.retry",
        policy=policy,
        methods=["POST"],
        response_model=ProcessingCommandResponse,
        status_code=202,
        openapi_extra=_idempotency_openapi_extra(),
    )
    add_protected_api_route(
        application,
        "/api/v1/documents/processing/reprocess",
        reprocess_endpoint,
        route_id="ingestion.processing.reprocess",
        policy=policy,
        methods=["POST"],
        response_model=ProcessingCommandResponse,
        status_code=202,
        openapi_extra=_idempotency_openapi_extra(),
    )
    add_protected_api_route(
        application,
        "/api/v1/documents/processing/operations/{operation_id}",
        status_endpoint,
        route_id="ingestion.processing.status",
        policy=policy,
        methods=["GET"],
        response_model=ProcessingCommandResponse,
    )


def _service(request: Request) -> ProcessingCommandService:
    service = getattr(request.app.state, "processing_command_service", None)
    if not callable(getattr(service, "retry", None)) or not callable(
        getattr(service, "reprocess", None)
    ) or not callable(getattr(service, "status", None)):
        raise RuntimeError("Processing command service is unavailable.")
    return service


def _idempotency_key(request: Request) -> str:
    try:
        return parse_idempotency_key_header(request.headers.getlist("Idempotency-Key")).value
    except ValueError:
        raise InvalidIngestionRequest() from None


def _scope(context: TrustedPersistenceContext) -> EnvironmentScope:
    if not isinstance(context.scope, EnvironmentScope):
        raise InvalidIngestionRequest()
    return context.scope


def _response(request: Request, result: ProcessingCommandResult) -> ProcessingCommandResponse:
    return ProcessingCommandResponse(
        operation_id=result.operation_id,
        run_id=result.run_id,
        item_id=result.item_id,
        source_object_id=result.source_object_id,
        source_revision_id=result.source_revision_id,
        trigger_kind=result.trigger_kind,  # type: ignore[arg-type]
        status=result.status,
        status_url=str(request.base_url).rstrip(
            "/"
        ) + f"/api/v1/documents/processing/operations/{result.operation_id}",
        replay=result.replay,
        processing_receipt_id=result.processing_receipt_id,
        failure_ids=result.failure_ids,
        next_action=result.next_action,  # type: ignore[arg-type]
    )


def _policy(operation: str) -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation(operation),
    )


def _idempotency_openapi_extra() -> dict[str, object]:
    return {
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 255},
            }
        ]
    }


__all__ = [
    "ProcessingCommandResponse",
    "ReprocessProcessingRequest",
    "RetryProcessingRequest",
    "install_processing_command_routes",
]
