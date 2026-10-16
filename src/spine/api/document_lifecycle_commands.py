"""Protected transport for document replacement and logical tombstoning."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from spine.api.authorization import add_protected_api_route, get_authorized_persistence_context
from spine.api.schemas import parse_idempotency_key_header
from spine.application.document_lifecycle import (
    DocumentLifecycleCommandService,
    ExistingUploadIntent,
    LifecycleFailureCode,
    LifecycleOperationKind,
    LifecycleOperationStatus,
    ReplaceDocumentCommand,
    SourceLifecycleOperationReceipt,
    TombstoneDocumentCommand,
)
from spine.application.ingestion_api_errors import InvalidIngestionRequest, IngestionRunNotFound
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import EnvironmentScope, TrustedPersistenceContext
from spine.auth import AuthorizationRole, AuthorizationScopeRequirement, RouteAuthorizationPolicy
from spine.domain.sources.models import ALLOWED_TOMBSTONE_REASONS


class ReplaceDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_current_revision_id: UUID
    upload_intent: ExistingUploadIntent


class TombstoneDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_current_revision_id: UUID
    deletion_reason: Literal[
        "legal_erasure",
        "provider_deleted",
        "retention_expired",
        "source_deleted",
    ]


assert set(TombstoneDocumentRequest.model_fields["deletion_reason"].annotation.__args__) == set(  # type: ignore[union-attr]
    ALLOWED_TOMBSTONE_REASONS
)


class SourceLifecycleOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operation_id: UUID
    operation_kind: LifecycleOperationKind
    source_object_id: UUID
    expected_current_revision_id: UUID
    accepted_revision_id: UUID | None = None
    canonical_acceptance_id: UUID | None = None
    processing_run_id: UUID | None = None
    status: LifecycleOperationStatus
    status_url: str
    idempotency_receipt_id: UUID
    accepted_at: datetime | None = None
    completed_at: datetime | None = None
    safe_failure_code: LifecycleFailureCode | None = None
    retryable: bool
    replay: bool


def install_document_lifecycle_command_routes(
    application: FastAPI,
    *,
    service: DocumentLifecycleCommandService,
) -> None:
    application.state.document_lifecycle_command_service = service

    async def replace_endpoint(
        source_object_id: UUID,
        body: ReplaceDocumentRequest,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> SourceLifecycleOperationResponse:
        scope = _scope(context)
        result = await _service(request).replace(
            context=context,
            command=ReplaceDocumentCommand(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                source_object_id=source_object_id,
                expected_current_revision_id=body.expected_current_revision_id,
                upload_intent=body.upload_intent,
                idempotency_key=_idempotency_key(request),
            ),
        )
        return _response(request, result)

    async def tombstone_endpoint(
        source_object_id: UUID,
        body: TombstoneDocumentRequest,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> SourceLifecycleOperationResponse:
        scope = _scope(context)
        result = await _service(request).tombstone(
            context=context,
            command=TombstoneDocumentCommand(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                source_object_id=source_object_id,
                expected_current_revision_id=body.expected_current_revision_id,
                deletion_reason=body.deletion_reason,
                idempotency_key=_idempotency_key(request),
            ),
        )
        return _response(request, result)

    async def status_endpoint(
        operation_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> SourceLifecycleOperationResponse:
        result = await _service(request).status(context=context, operation_id=operation_id)
        if result is None:
            raise IngestionRunNotFound()
        return _response(request, result)

    policy = _policy()
    extra = _idempotency_openapi_extra()
    add_protected_api_route(
        application,
        "/api/v1/documents/{source_object_id}/replacement",
        replace_endpoint,
        route_id="documents.lifecycle.replace",
        policy=policy,
        methods=["POST"],
        response_model=SourceLifecycleOperationResponse,
        status_code=202,
        openapi_extra=extra,
    )
    add_protected_api_route(
        application,
        "/api/v1/documents/{source_object_id}/tombstone",
        tombstone_endpoint,
        route_id="documents.lifecycle.tombstone",
        policy=policy,
        methods=["POST"],
        response_model=SourceLifecycleOperationResponse,
        status_code=202,
        openapi_extra=extra,
    )
    add_protected_api_route(
        application,
        "/api/v1/documents/lifecycle/operations/{operation_id}",
        status_endpoint,
        route_id="documents.lifecycle.status",
        policy=policy,
        methods=["GET"],
        response_model=SourceLifecycleOperationResponse,
    )


def _service(request: Request) -> DocumentLifecycleCommandService:
    service = getattr(request.app.state, "document_lifecycle_command_service", None)
    if not all(callable(getattr(service, name, None)) for name in ("replace", "tombstone", "status")):
        raise RuntimeError("Document lifecycle command service is unavailable.")
    return cast(DocumentLifecycleCommandService, service)


def _scope(context: TrustedPersistenceContext) -> EnvironmentScope:
    if not isinstance(context.scope, EnvironmentScope):
        raise InvalidIngestionRequest()
    return context.scope


def _idempotency_key(request: Request) -> str:
    try:
        return parse_idempotency_key_header(request.headers.getlist("Idempotency-Key")).value
    except ValueError:
        raise InvalidIngestionRequest() from None


def _response(
    request: Request,
    receipt: SourceLifecycleOperationReceipt,
) -> SourceLifecycleOperationResponse:
    return SourceLifecycleOperationResponse(
        **receipt.model_dump(),
        status_url=(
            str(request.base_url).rstrip("/")
            + f"/api/v1/documents/lifecycle/operations/{receipt.operation_id}"
        ),
    )


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("document_lifecycle"),
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
    "ReplaceDocumentRequest",
    "SourceLifecycleOperationResponse",
    "TombstoneDocumentRequest",
    "install_document_lifecycle_command_routes",
]
