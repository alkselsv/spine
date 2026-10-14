"""Bounded multipart transport for durable batch document ingestion."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from enum import Enum
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
)
from spine.api.ingestion import (
    IngestionReceiptResponse,
    PublicAcceptanceStatus,
    PublicIngestionStatus,
    PublicProcessingStatus,
)
from spine.application.batch_ingestion import (
    BatchDocumentInput,
    BatchDocumentUploadCommand,
    BatchDocumentUploadItem,
)
from spine.application.batch_ingestion_api import (
    BatchDocumentIngestionService,
    BatchIngestionItemSnapshot,
    BatchIngestionSnapshot,
)
from spine.application.ingestion_api_errors import (
    IngestionRunNotFound,
    IngestionServiceUnavailable,
    InvalidIngestionRequest,
    UploadPayloadTooLarge,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    RouteAuthorizationPolicy,
)
from spine.domain.ingestion import (
    IngestionItemState,
    IngestionRunState,
    ProcessingCoverage,
)


DEFAULT_BATCH_FILE_LIMIT = 20
DEFAULT_BATCH_FILE_LIMIT_BYTES = 10_000_000
DEFAULT_BATCH_UPLOAD_LIMIT_BYTES = 50_000_000
_IDEMPOTENCY_KEY = "Idempotency-Key"
_READ_CHUNK_BYTES = 64 * 1024


class PublicBatchStatus(str, Enum):
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchIngestionWarningResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warning_id: UUID
    attempt: int = Field(ge=1)
    stage: str
    code: str


class BatchIngestionFailureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_id: UUID
    attempt: int = Field(ge=1)
    stage: str
    code: str
    retryable: bool


class BatchIngestionItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: UUID
    input_ordinal: int = Field(ge=1)
    input_identity: str
    status: PublicIngestionStatus
    item_state: str
    acceptance: PublicAcceptanceStatus
    processing: PublicProcessingStatus
    terminal: bool
    retryable: bool
    attempt: int = Field(ge=0)
    coverage: str | None = None
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    binding_id: UUID | None = None
    processing_receipt: IngestionReceiptResponse | None = None
    warning_ids: tuple[UUID, ...] = ()
    failure_ids: tuple[UUID, ...] = ()
    warnings: tuple[BatchIngestionWarningResponse, ...] = ()
    failures: tuple[BatchIngestionFailureResponse, ...] = ()


class BatchIngestionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    status: PublicBatchStatus
    run_state: str
    terminal: bool
    items: tuple[BatchIngestionItemResponse, ...]


class BatchIngestionUploadResponse(BatchIngestionStatusResponse):
    status_url: str
    replay: bool


def install_batch_ingestion_routes(
    application: FastAPI,
    *,
    service: BatchDocumentIngestionService,
    max_files: int = DEFAULT_BATCH_FILE_LIMIT,
    max_file_bytes: int = DEFAULT_BATCH_FILE_LIMIT_BYTES,
    max_batch_bytes: int = DEFAULT_BATCH_UPLOAD_LIMIT_BYTES,
) -> None:
    """Register authorized batch upload and whole-run polling routes."""

    if max_files <= 0 or max_file_bytes <= 0 or max_batch_bytes <= 0:
        raise ValueError("batch upload limits must be positive")
    if max_batch_bytes < max_file_bytes:
        raise ValueError("max_batch_bytes must be at least max_file_bytes")
    application.state.batch_document_ingestion_service = service
    upload_policy = _policy("document_batch_upload")
    status_policy = _policy("document_batch_status")

    async def upload_endpoint(
        request: Request,
        context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
    ) -> BatchIngestionUploadResponse:
        content_length = _content_length(request)
        if content_length is not None and content_length > max_batch_bytes:
            raise UploadPayloadTooLarge()
        idempotency_key = _single_header(request, _IDEMPOTENCY_KEY)
        form = await _parse_files(
            request,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            max_batch_bytes=max_batch_bytes,
        )
        try:
            command, inputs = await _batch_command(
                request,
                form,
                idempotency_key=idempotency_key,
                max_file_bytes=max_file_bytes,
            )
            snapshot = await _service(request).submit(
                context=context,
                command=command,
                items=inputs,
            )
            return _upload_response(request, snapshot)
        finally:
            await form.close()

    async def status_endpoint(
        run_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
    ) -> BatchIngestionStatusResponse:
        snapshot = await _service(request).status(
            context=context,
            run_id=run_id,
        )
        if snapshot is None:
            raise IngestionRunNotFound()
        return _status_response(snapshot)

    add_protected_api_route(
        application,
        "/api/v1/documents/batches",
        upload_endpoint,
        route_id="ingestion.batch_upload",
        policy=upload_policy,
        methods=["POST"],
        response_model=BatchIngestionUploadResponse,
        status_code=202,
        openapi_extra=_batch_upload_openapi_extra(max_files=max_files),
    )
    add_protected_api_route(
        application,
        "/api/v1/ingestion/runs/{run_id}",
        status_endpoint,
        route_id="ingestion.batch_status",
        policy=status_policy,
        methods=["GET"],
        response_model=BatchIngestionStatusResponse,
    )


def _service(request: Request) -> BatchDocumentIngestionService:
    service = getattr(request.app.state, "batch_document_ingestion_service", None)
    if service is None:
        raise IngestionServiceUnavailable()
    if not callable(getattr(service, "submit", None)) or not callable(
        getattr(service, "status", None)
    ):
        raise IngestionServiceUnavailable()
    return service


async def _parse_files(
    request: Request,
    *,
    max_files: int,
    max_file_bytes: int,
    max_batch_bytes: int,
) -> FormData:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise InvalidIngestionRequest()
    parser = _BoundedMultiPartParser(
        headers=request.headers,
        stream=_bounded_request_stream(request, max_batch_bytes),
        max_files=max_files,
        max_fields=0,
        max_part_size=1024,
        max_file_bytes=max_file_bytes,
    )
    try:
        return await parser.parse()
    except UploadPayloadTooLarge:
        raise
    except (MultiPartException, MultipartParseError, KeyError, UnicodeError):
        raise InvalidIngestionRequest() from None


class _BoundedMultiPartParser(MultiPartParser):
    """Apply a per-file bound while Starlette spools multipart bytes to disk."""

    def __init__(self, *args: object, max_file_bytes: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._maximum_file_bytes = max_file_bytes
        self._current_file_bytes = 0

    def on_part_begin(self) -> None:
        self._current_file_bytes = 0
        super().on_part_begin()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_bytes += end - start
            if self._current_file_bytes > self._maximum_file_bytes:
                raise UploadPayloadTooLarge()
        super().on_part_data(data, start, end)


async def _bounded_request_stream(
    request: Request,
    maximum: int,
) -> AsyncIterator[bytes]:
    length = 0
    async for chunk in request.stream():
        if not isinstance(chunk, bytes):
            raise InvalidIngestionRequest()
        length += len(chunk)
        if length > maximum:
            raise UploadPayloadTooLarge()
        yield chunk


async def _batch_command(
    request: Request,
    form: FormData,
    *,
    idempotency_key: str,
    max_file_bytes: int,
) -> tuple[BatchDocumentUploadCommand, tuple[BatchDocumentInput, ...]]:
    parts = form.multi_items()
    if not parts or any(not isinstance(value, UploadFile) for _, value in parts):
        raise InvalidIngestionRequest()
    identities = [name for name, _ in parts]
    if len(set(identities)) != len(identities):
        raise InvalidIngestionRequest()

    commands: list[BatchDocumentUploadItem] = []
    uploads: list[UploadFile] = []
    for input_identity, value in parts:
        assert isinstance(value, UploadFile)
        digest, byte_length = await _inspect_upload(value, max_file_bytes)
        commands.append(
            BatchDocumentUploadItem(
                input_identity=input_identity,
                purpose="document.upload.v1",
                declared_media_type=_media_type(value),
                declared_filename=value.filename or None,
                expected_sha256=digest,
                expected_byte_length=byte_length,
            )
        )
        uploads.append(value)
    try:
        command = BatchDocumentUploadCommand(
            workspace_id=_scope_id(request, "X-Workspace-ID"),
            environment_id=_scope_id(request, "X-Environment-ID"),
            command_identity="api.document.batch-upload",
            idempotency_key=idempotency_key,
            items=tuple(commands),
        )
    except (ValidationError, ValueError, TypeError):
        raise InvalidIngestionRequest() from None
    return command, tuple(
        BatchDocumentInput(command=item, chunks=_upload_chunks(upload))
        for item, upload in zip(commands, uploads, strict=True)
    )


async def _inspect_upload(upload: UploadFile, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    while chunk := await upload.read(_READ_CHUNK_BYTES):
        length += len(chunk)
        if length > maximum:
            raise UploadPayloadTooLarge()
        digest.update(chunk)
    await upload.seek(0)
    return digest.hexdigest(), length


def _media_type(upload: UploadFile) -> str | None:
    if not upload.content_type:
        return None
    value = upload.content_type.split(";", 1)[0].strip().lower()
    return value or None


async def _upload_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await upload.read(_READ_CHUNK_BYTES):
        yield chunk


def _upload_response(
    request: Request,
    snapshot: BatchIngestionSnapshot,
) -> BatchIngestionUploadResponse:
    status_url = f"/api/v1/ingestion/runs/{snapshot.run_id}"
    response = _status_response(snapshot)
    return BatchIngestionUploadResponse(
        **response.model_dump(),
        status_url=str(request.base_url).rstrip("/") + status_url,
        replay=snapshot.replay,
    )


def _status_response(snapshot: BatchIngestionSnapshot) -> BatchIngestionStatusResponse:
    status = _batch_status(snapshot)
    return BatchIngestionStatusResponse(
        run_id=snapshot.run_id,
        status=status,
        run_state=snapshot.run_state.value,
        terminal=status is not PublicBatchStatus.PROCESSING,
        items=tuple(
            _item_response(item, run_state=snapshot.run_state)
            for item in snapshot.items
        ),
    )


def _item_response(
    item: BatchIngestionItemSnapshot,
    *,
    run_state: IngestionRunState,
) -> BatchIngestionItemResponse:
    status = _item_status(item, run_state=run_state)
    acceptance, processing = _item_phases(item, status=status)
    receipt = None
    if item.processing_receipt_id is not None and item.processing_coverage is not None:
        receipt = IngestionReceiptResponse(
            receipt_id=item.processing_receipt_id,
            parser_name=item.parser_name or "unknown",
            coverage=item.processing_coverage.value,
            artifact_id=item.artifact_id,
            artifact_digest=item.artifact_digest,
        )
    return BatchIngestionItemResponse(
        item_id=item.item_id,
        input_ordinal=item.input_ordinal,
        input_identity=item.input_identity,
        status=status,
        item_state=item.item_state.value,
        acceptance=acceptance,
        processing=processing,
        terminal=_item_terminal(item, run_state=run_state),
        retryable=item.retryable,
        attempt=item.attempt,
        coverage=(
            item.processing_coverage.value if item.processing_coverage else None
        ),
        source_object_id=item.source_object_id,
        source_revision_id=item.source_revision_id,
        binding_id=item.binding_id,
        processing_receipt=receipt,
        warning_ids=item.warning_ids,
        failure_ids=item.failure_ids,
        warnings=tuple(
            BatchIngestionWarningResponse(
                warning_id=warning.warning_id,
                attempt=warning.attempt,
                stage=warning.stage,
                code=warning.code,
            )
            for warning in item.warnings
        ),
        failures=tuple(
            BatchIngestionFailureResponse(
                failure_id=failure.failure_id,
                attempt=failure.attempt,
                stage=failure.stage,
                code=failure.code,
                retryable=failure.retryable,
            )
            for failure in item.failures
        ),
    )


def _batch_status(snapshot: BatchIngestionSnapshot) -> PublicBatchStatus:
    if snapshot.run_state is IngestionRunState.CANCELLED:
        return PublicBatchStatus.CANCELLED
    statuses = tuple(
        _item_status(item, run_state=snapshot.run_state) for item in snapshot.items
    )
    if not statuses or any(
        status is PublicIngestionStatus.NON_TERMINAL for status in statuses
    ):
        return PublicBatchStatus.PROCESSING
    successes = {
        PublicIngestionStatus.SUCCEEDED,
        PublicIngestionStatus.PARTIAL,
    }
    successful = [status for status in statuses if status in successes]
    unsuccessful = [status for status in statuses if status not in successes]
    if successful and unsuccessful:
        return PublicBatchStatus.PARTIAL
    if any(status is PublicIngestionStatus.PARTIAL for status in successful):
        return PublicBatchStatus.PARTIAL
    return PublicBatchStatus.SUCCEEDED if successful else PublicBatchStatus.FAILED


def _item_status(
    item: BatchIngestionItemSnapshot,
    *,
    run_state: IngestionRunState,
) -> PublicIngestionStatus:
    if item.item_state is IngestionItemState.QUARANTINED:
        return PublicIngestionStatus.QUARANTINED
    if item.item_state is IngestionItemState.REJECTED:
        return PublicIngestionStatus.REJECTED
    if item.parser_name == "unsupported":
        return PublicIngestionStatus.UNSUPPORTED
    if item.processing_coverage is ProcessingCoverage.COMPLETE:
        return PublicIngestionStatus.SUCCEEDED
    if item.processing_coverage is ProcessingCoverage.PARTIAL:
        return PublicIngestionStatus.PARTIAL
    if (
        item.processing_coverage is ProcessingCoverage.FAILED
        or item.item_state is IngestionItemState.PROCESSING_FAILED
    ):
        return PublicIngestionStatus.FAILED
    if run_state is IngestionRunState.CANCELLED:
        return PublicIngestionStatus.CANCELLED
    return PublicIngestionStatus.NON_TERMINAL


def _item_phases(
    item: BatchIngestionItemSnapshot,
    *,
    status: PublicIngestionStatus,
) -> tuple[PublicAcceptanceStatus, PublicProcessingStatus]:
    if item.item_state is IngestionItemState.QUARANTINED:
        return PublicAcceptanceStatus.QUARANTINED, PublicProcessingStatus.NOT_STARTED
    if item.item_state is IngestionItemState.REJECTED:
        return PublicAcceptanceStatus.REJECTED, PublicProcessingStatus.NOT_STARTED
    accepted_states = {
        IngestionItemState.ACCEPTED,
        IngestionItemState.PROCESSING,
        IngestionItemState.PROCESSED,
        IngestionItemState.PROJECTION_REQUESTED,
        IngestionItemState.PROCESSING_FAILED,
    }
    acceptance = (
        PublicAcceptanceStatus.ACCEPTED
        if item.item_state in accepted_states
        else PublicAcceptanceStatus.PENDING
    )
    processing = {
        PublicIngestionStatus.SUCCEEDED: PublicProcessingStatus.SUCCEEDED,
        PublicIngestionStatus.PARTIAL: PublicProcessingStatus.PARTIAL,
        PublicIngestionStatus.FAILED: PublicProcessingStatus.FAILED,
        PublicIngestionStatus.UNSUPPORTED: PublicProcessingStatus.UNSUPPORTED,
        PublicIngestionStatus.CANCELLED: PublicProcessingStatus.CANCELLED,
    }.get(
        status,
        (
            PublicProcessingStatus.PROCESSING
            if item.item_state is IngestionItemState.PROCESSING
            else PublicProcessingStatus.NOT_STARTED
        ),
    )
    return acceptance, processing


def _item_terminal(
    item: BatchIngestionItemSnapshot,
    *,
    run_state: IngestionRunState,
) -> bool:
    return run_state is IngestionRunState.CANCELLED or item.item_state in {
        IngestionItemState.QUARANTINED,
        IngestionItemState.REJECTED,
        IngestionItemState.PROCESSED,
        IngestionItemState.PROJECTION_REQUESTED,
        IngestionItemState.PROCESSING_FAILED,
    }


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise InvalidIngestionRequest()
    value = values[0].strip()
    if not value or "\x00" in value:
        raise InvalidIngestionRequest()
    return value


def _scope_id(request: Request, name: str) -> UUID:
    value = request.headers.get(name)
    try:
        parsed = UUID(value) if value is not None else None
    except ValueError:
        raise InvalidIngestionRequest() from None
    if parsed is None or parsed.int == 0:
        raise InvalidIngestionRequest()
    return parsed


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


def _policy(operation: str) -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation(operation),
    )


def _batch_upload_openapi_extra(*, max_files: int) -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "minProperties": 1,
                        "maxProperties": max_files,
                        "additionalProperties": {
                            "type": "string",
                            "format": "binary",
                        },
                        "description": (
                            "Each property name is the stable input identity. "
                            "TXT and Markdown are the completed batch formats."
                        ),
                    }
                }
            },
        },
        "parameters": [
            {
                "name": _IDEMPOTENCY_KEY,
                "in": "header",
                "required": True,
                "schema": {"type": "string", "maxLength": 255},
            }
        ],
    }


__all__ = [
    "BatchIngestionStatusResponse",
    "BatchIngestionUploadResponse",
    "DEFAULT_BATCH_FILE_LIMIT",
    "DEFAULT_BATCH_FILE_LIMIT_BYTES",
    "DEFAULT_BATCH_UPLOAD_LIMIT_BYTES",
    "install_batch_ingestion_routes",
]
