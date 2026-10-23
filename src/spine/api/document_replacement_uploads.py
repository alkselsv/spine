"""Protected administrator upload route for a new revision of an existing source."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Request

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
)
from spine.api.ingestion import (
    DEFAULT_UPLOAD_LIMIT_BYTES,
    IngestionUploadResponse,
    _bounded_stream,
    _content_length,
    _policy,
    _single_header,
    _upload_response,
)
from spine.api.schemas import parse_idempotency_key_header
from spine.application.document_replacement_upload import (
    DocumentReplacementUploadCommand,
)
from spine.application.ingestion_api_errors import (
    InvalidIngestionRequest,
    UploadPayloadTooLarge,
)
from spine.application.ingestion_api import IngestionSubmission
from spine.application.persistence.context import TrustedPersistenceContext

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DocumentReplacementUploadService(Protocol):
    async def replace(
        self,
        *,
        context: TrustedPersistenceContext,
        command: DocumentReplacementUploadCommand,
        chunks: AsyncIterator[bytes],
    ) -> IngestionSubmission: ...


def install_document_replacement_upload_route(
    application: FastAPI,
    *,
    service: DocumentReplacementUploadService,
    max_upload_bytes: int = DEFAULT_UPLOAD_LIMIT_BYTES,
) -> None:
    if max_upload_bytes <= 0:
        raise ValueError("max_upload_bytes must be positive")

    async def replace_endpoint(
        source_object_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
    ) -> IngestionUploadResponse:
        content_length = _content_length(request)
        if content_length is not None and content_length > max_upload_bytes:
            raise UploadPayloadTooLarge()
        try:
            idempotency_key = parse_idempotency_key_header(
                request.headers.getlist("Idempotency-Key")
            ).value
            filename = _single_header(request, "X-Filename", required=True)
            declared_media_type = (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            expected_sha256 = _single_header(
                request, "X-Content-SHA256", required=False
            )
            if not declared_media_type or (
                expected_sha256 is not None
                and _SHA256_PATTERN.fullmatch(expected_sha256) is None
            ):
                raise InvalidIngestionRequest()
            command = DocumentReplacementUploadCommand(
                source_object_id=source_object_id,
                expected_current_revision_id=UUID(
                    _single_header(request, "If-Match", required=True) or ""
                ),
                idempotency_key=idempotency_key,
                declared_filename=filename or "",
                declared_media_type=declared_media_type,
                expected_sha256=expected_sha256,
                expected_byte_length=content_length,
            )
        except (ValueError, TypeError):
            raise InvalidIngestionRequest() from None

        submission = await service.replace(
            context=context,
            command=command,
            chunks=_bounded_stream(request, max_upload_bytes),
        )
        return _upload_response(request, submission)

    add_protected_api_route(
        application,
        "/api/v1/documents/{source_object_id}/replacement-upload",
        replace_endpoint,
        route_id="documents.replacement_upload",
        policy=_policy("document_upload"),
        methods=["POST"],
        response_model=IngestionUploadResponse,
        status_code=202,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/pdf": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    },
                    "text/plain": {"schema": {"type": "string", "format": "binary"}},
                    "text/markdown": {"schema": {"type": "string", "format": "binary"}},
                },
            },
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 255},
                },
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "format": "uuid"},
                },
                {
                    "name": "X-Filename",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 255},
                },
                {
                    "name": "X-Content-SHA256",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                },
            ],
        },
    )


__all__ = [
    "DocumentReplacementUploadService",
    "install_document_replacement_upload_route",
]
