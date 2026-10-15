"""Versioned, administrator-only HTTP transport for document ingestion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import Enum
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
)
from spine.api.schemas import parse_idempotency_key_header
from spine.application.document_ingestion import (
    DocumentIngestionStatus,
    DocumentUploadCommand,
)
from spine.application.ingestion_api import (
    DocumentIngestionService,
    IngestionStatusSnapshot,
    IngestionSubmission,
)
from spine.application.ingestion_api_errors import (
    IngestionRunNotFound,
    IngestionServiceUnavailable,
    InvalidIngestionRequest,
    UploadPayloadTooLarge,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    RouteAuthorizationPolicy,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.domain.ingestion import ProcessingCoverage


DEFAULT_UPLOAD_LIMIT_BYTES = 10_000_000
_IDEMPOTENCY_KEY = "Idempotency-Key"
_UPLOAD_IDENTITY = "X-Upload-Identity"
_FILENAME = "X-Filename"
_CONTENT_SHA256 = "X-Content-SHA256"


class PublicIngestionStatus(str, Enum):
    NON_TERMINAL = "non_terminal"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class PublicAcceptanceStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


class PublicProcessingStatus(str, Enum):
    NOT_STARTED = "not_started"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"


class IngestionReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: UUID
    parser_name: str
    coverage: Literal["complete", "partial", "failed"]
    artifact_id: UUID | None = None
    artifact_digest: str | None = Field(default=None, pattern=r"[0-9a-f]{64}")


class IngestionUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    item_id: UUID
    status_url: str
    status: PublicIngestionStatus
    acceptance: PublicAcceptanceStatus
    processing: PublicProcessingStatus
    terminal: bool
    replay: bool
    source_object_id: UUID
    source_revision_id: UUID
    binding_id: UUID
    processing_receipt_id: UUID | None = None
    warning_ids: tuple[UUID, ...] = ()
    failure_ids: tuple[UUID, ...] = ()


class IngestionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    item_id: UUID
    status: PublicIngestionStatus
    run_state: str
    item_state: str
    acceptance: PublicAcceptanceStatus
    processing: PublicProcessingStatus
    terminal: bool
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    binding_id: UUID | None = None
    processing_receipt: IngestionReceiptResponse | None = None
    warning_ids: tuple[UUID, ...] = ()
    failure_ids: tuple[UUID, ...] = ()


def install_ingestion_routes(
    application: FastAPI,
    *,
    service: DocumentIngestionService,
    max_upload_bytes: int = DEFAULT_UPLOAD_LIMIT_BYTES,
) -> None:
    """Register the v1 upload/status surface on an authorized application."""

    if max_upload_bytes <= 0:
        raise ValueError("max_upload_bytes must be positive")
    application.state.document_ingestion_service = service
    application.state.document_ingestion_max_upload_bytes = max_upload_bytes
    upload_policy = _policy("document_upload")
    status_policy = _policy("document_status")

    async def upload_endpoint(
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> IngestionUploadResponse:
        resolved_service = _service(request)
        command = _command_from_request(request)
        content_length = _content_length(request)
        if content_length is not None and content_length > max_upload_bytes:
            raise UploadPayloadTooLarge()
        submission = await resolved_service.submit(
            context=context,
            command=command,
            chunks=_bounded_stream(request, max_upload_bytes),
        )
        return _upload_response(request, submission)

    async def status_endpoint(
        run_id: UUID,
        item_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> IngestionStatusResponse:
        snapshot = await _service(request).status(
            context=context,
            run_id=run_id,
            item_id=item_id,
        )
        if snapshot is None:
            raise IngestionRunNotFound()
        return _status_response(snapshot)

    add_protected_api_route(
        application,
        "/api/v1/documents/uploads",
        upload_endpoint,
        route_id="ingestion.upload",
        policy=upload_policy,
        methods=["POST"],
        response_model=IngestionUploadResponse,
        status_code=202,
        openapi_extra=_upload_openapi_extra(),
    )
    add_protected_api_route(
        application,
        "/api/v1/ingestion/runs/{run_id}/items/{item_id}",
        status_endpoint,
        route_id="ingestion.status",
        policy=status_policy,
        methods=["GET"],
        response_model=IngestionStatusResponse,
    )


def _service(request: Request) -> DocumentIngestionService:
    service = getattr(request.app.state, "document_ingestion_service", None)
    if service is None:
        raise IngestionServiceUnavailable()
    if not callable(getattr(service, "submit", None)) or not callable(
        getattr(service, "status", None)
    ):
        raise IngestionServiceUnavailable()
    return service


def _command_from_request(request: Request) -> DocumentUploadCommand:
    try:
        idempotency_key = parse_idempotency_key_header(
            request.headers.getlist(_IDEMPOTENCY_KEY)
        ).value
    except ValueError:
        raise InvalidIngestionRequest() from None
    upload_identity = _single_header(request, _UPLOAD_IDENTITY, required=False)
    if upload_identity is None:
        upload_identity = f"upload-{_stable_key_suffix(idempotency_key)}"
    filename = _single_header(request, _FILENAME, required=False)
    content_sha256 = _single_header(request, _CONTENT_SHA256, required=False)
    media_type = request.headers.get("content-type", "application/octet-stream")
    media_type = media_type.split(";", 1)[0].strip().lower()
    if not media_type:
        raise InvalidIngestionRequest()
    try:
        return DocumentUploadCommand(
            workspace_id=_workspace_id(request),
            environment_id=_environment_id(request),
            command_identity="api.document.upload",
            idempotency_key=idempotency_key,
            upload_identity=upload_identity,
            purpose="document.upload.v1",
            declared_media_type=media_type,
            declared_filename=filename,
            expected_sha256=content_sha256,
            expected_byte_length=_content_length(request),
        )
    except (ValidationError, ValueError, TypeError):
        raise InvalidIngestionRequest() from None


def _single_header(request: Request, name: str, *, required: bool) -> str | None:
    values = request.headers.getlist(name)
    if len(values) != 1:
        if required or values:
            raise InvalidIngestionRequest()
        return None
    value = values[0].strip()
    if not value or "\x00" in value:
        raise InvalidIngestionRequest()
    return value


def _content_length(request: Request) -> int | None:
    values = request.headers.getlist("content-length")
    if not values:
        return None
    if len(values) != 1:
        raise InvalidIngestionRequest()
    try:
        value = int(values[0])
    except ValueError:
        raise InvalidIngestionRequest() from None
    if value < 0:
        raise InvalidIngestionRequest()
    return value


async def _bounded_stream(request: Request, maximum: int) -> AsyncIterator[bytes]:
    length = 0
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            raise InvalidIngestionRequest()
        length += len(chunk)
        if length > maximum:
            raise UploadPayloadTooLarge()
        yield chunk


def _workspace_id(request: Request) -> UUID:
    return _scope_id(request, "X-Workspace-ID")


def _environment_id(request: Request) -> UUID:
    return _scope_id(request, "X-Environment-ID")


def _scope_id(request: Request, name: str) -> UUID:
    value = request.headers.get(name)
    if value is None:
        raise InvalidIngestionRequest()
    try:
        parsed = UUID(value)
    except ValueError:
        raise InvalidIngestionRequest() from None
    if parsed.int == 0:
        raise InvalidIngestionRequest()
    return parsed


def _stable_key_suffix(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _upload_response(request: Request, submission: IngestionSubmission) -> IngestionUploadResponse:
    status = _public_status(submission.status)
    acceptance, processing = _phases(submission.status)
    status_url = f"/api/v1/ingestion/runs/{submission.run_id}/items/{submission.item_id}"
    return IngestionUploadResponse(
        run_id=submission.run_id,
        item_id=submission.item_id,
        status_url=str(request.base_url).rstrip("/") + status_url,
        status=status,
        acceptance=acceptance,
        processing=processing,
        terminal=status is not PublicIngestionStatus.NON_TERMINAL,
        replay=submission.replay,
        source_object_id=submission.source_object_id,
        source_revision_id=submission.source_revision_id,
        binding_id=submission.binding_id,
        processing_receipt_id=submission.processing_receipt_id,
        warning_ids=submission.warning_ids,
        failure_ids=submission.failure_ids,
    )


def _status_response(snapshot: IngestionStatusSnapshot) -> IngestionStatusResponse:
    status = _snapshot_status(snapshot)
    acceptance, processing = _snapshot_phases(snapshot)
    receipt = None
    if snapshot.processing_receipt_id is not None and snapshot.processing_coverage is not None:
        receipt = IngestionReceiptResponse(
            receipt_id=snapshot.processing_receipt_id,
            parser_name=snapshot.parser_name or "unknown",
            coverage=snapshot.processing_coverage.value,
            artifact_id=snapshot.artifact_id,
            artifact_digest=snapshot.artifact_digest,
        )
    return IngestionStatusResponse(
        run_id=snapshot.run_id,
        item_id=snapshot.item_id,
        status=status,
        run_state=snapshot.run_state.value,
        item_state=snapshot.item_state.value,
        acceptance=acceptance,
        processing=processing,
        terminal=status is not PublicIngestionStatus.NON_TERMINAL,
        source_object_id=snapshot.source_object_id,
        source_revision_id=snapshot.source_revision_id,
        binding_id=snapshot.binding_id,
        processing_receipt=receipt,
        warning_ids=snapshot.warning_ids,
        failure_ids=snapshot.failure_ids,
    )


def _public_status(status: DocumentIngestionStatus) -> PublicIngestionStatus:
    if status is DocumentIngestionStatus.SUCCEEDED:
        return PublicIngestionStatus.SUCCEEDED
    if status is DocumentIngestionStatus.PARTIAL:
        return PublicIngestionStatus.PARTIAL
    if status is DocumentIngestionStatus.REJECTED:
        return PublicIngestionStatus.REJECTED
    if status is DocumentIngestionStatus.QUARANTINED:
        return PublicIngestionStatus.QUARANTINED
    if status is DocumentIngestionStatus.UNSUPPORTED:
        return PublicIngestionStatus.UNSUPPORTED
    return PublicIngestionStatus.FAILED


def _phases(status: DocumentIngestionStatus) -> tuple[PublicAcceptanceStatus, PublicProcessingStatus]:
    if status is DocumentIngestionStatus.REJECTED:
        return PublicAcceptanceStatus.REJECTED, PublicProcessingStatus.NOT_STARTED
    if status is DocumentIngestionStatus.QUARANTINED:
        return PublicAcceptanceStatus.QUARANTINED, PublicProcessingStatus.NOT_STARTED
    if status is DocumentIngestionStatus.SUCCEEDED:
        return PublicAcceptanceStatus.ACCEPTED, PublicProcessingStatus.SUCCEEDED
    if status is DocumentIngestionStatus.PARTIAL:
        return PublicAcceptanceStatus.ACCEPTED, PublicProcessingStatus.PARTIAL
    if status is DocumentIngestionStatus.UNSUPPORTED:
        return PublicAcceptanceStatus.ACCEPTED, PublicProcessingStatus.UNSUPPORTED
    return PublicAcceptanceStatus.ACCEPTED, PublicProcessingStatus.FAILED


def _snapshot_status(snapshot: IngestionStatusSnapshot) -> PublicIngestionStatus:
    if snapshot.run_state.value == "cancelled" or snapshot.item_state.value == "cancelled":
        return PublicIngestionStatus.CANCELLED
    if snapshot.item_state.value == "quarantined":
        return PublicIngestionStatus.QUARANTINED
    if snapshot.item_state.value == "rejected":
        return PublicIngestionStatus.REJECTED
    if snapshot.parser_name == "unsupported":
        return PublicIngestionStatus.UNSUPPORTED
    if snapshot.processing_coverage is ProcessingCoverage.COMPLETE:
        return PublicIngestionStatus.SUCCEEDED
    if snapshot.processing_coverage is ProcessingCoverage.PARTIAL:
        return PublicIngestionStatus.PARTIAL
    if snapshot.processing_coverage is ProcessingCoverage.FAILED or snapshot.item_state.value == "processing_failed":
        return PublicIngestionStatus.FAILED
    if snapshot.run_state.value == "partially_succeeded":
        return PublicIngestionStatus.PARTIAL
    return PublicIngestionStatus.NON_TERMINAL


def _snapshot_phases(snapshot: IngestionStatusSnapshot) -> tuple[PublicAcceptanceStatus, PublicProcessingStatus]:
    state = snapshot.item_state.value
    if state == "quarantined":
        return PublicAcceptanceStatus.QUARANTINED, PublicProcessingStatus.NOT_STARTED
    if state == "rejected":
        return PublicAcceptanceStatus.REJECTED, PublicProcessingStatus.NOT_STARTED
    if state in {"accepted", "processing", "processed", "projection_requested", "processing_failed"}:
        acceptance = PublicAcceptanceStatus.ACCEPTED
    else:
        acceptance = PublicAcceptanceStatus.PENDING
    status = _snapshot_status(snapshot)
    processing = {
        PublicIngestionStatus.SUCCEEDED: PublicProcessingStatus.SUCCEEDED,
        PublicIngestionStatus.PARTIAL: PublicProcessingStatus.PARTIAL,
        PublicIngestionStatus.FAILED: PublicProcessingStatus.FAILED,
        PublicIngestionStatus.UNSUPPORTED: PublicProcessingStatus.UNSUPPORTED,
        PublicIngestionStatus.CANCELLED: PublicProcessingStatus.CANCELLED,
    }.get(status, PublicProcessingStatus.PROCESSING if state == "processing" else PublicProcessingStatus.NOT_STARTED)
    return acceptance, processing


def _policy(operation: str) -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation(operation),
    )


def _upload_openapi_extra() -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"},
                },
                "text/plain": {"schema": {"type": "string", "format": "binary"}},
                "text/markdown": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        "parameters": [
            {"name": _IDEMPOTENCY_KEY, "in": "header", "required": True, "schema": {"type": "string", "maxLength": 255}},
            {"name": _UPLOAD_IDENTITY, "in": "header", "required": False, "schema": {"type": "string", "maxLength": 64}},
            {"name": _FILENAME, "in": "header", "required": False, "schema": {"type": "string", "maxLength": 255}},
            {"name": _CONTENT_SHA256, "in": "header", "required": False, "schema": {"type": "string", "pattern": "[0-9a-f]{64}"}},
        ],
    }


__all__ = [
    "DEFAULT_UPLOAD_LIMIT_BYTES",
    "IngestionStatusResponse",
    "IngestionUploadResponse",
    "install_ingestion_routes",
]
