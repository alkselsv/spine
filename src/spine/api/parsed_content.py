"""Protected Knowledge parsed-content and exact-locator inspection routes."""

from __future__ import annotations

import json
from typing import Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    ApiResponseEnvelope,
    CursorCodec,
    DEFAULT_PAGE_LIMIT,
    InvalidCursor,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    canonical_filter_digest,
)
from spine.application.parsed_content_inspection import (
    InvalidParsedContentRequest,
    ParsedContentInspectionQuery,
    ParsedContentInspectionRequester,
    ParsedContentInspectionResult,
    ParsedContentInspectionService,
    ParsedContentInspectionStatus,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.parsed_artifacts import ParsedArtifactMetadata
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.artifacts import Locator


_OPERATION = "knowledge.parsed_content.inspect"
_LOCATOR_ADAPTER = TypeAdapter(Locator)


class ParsedContentSegmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: UUID
    segment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_object_id: UUID
    source_revision_id: UUID
    source_revision_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: Locator
    text: str
    text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    heading_path: tuple[str, ...]


class ParsedContentInspectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["knowledge-parsed-content-inspection:v1"] = (
        "knowledge-parsed-content-inspection:v1"
    )
    status: ParsedContentInspectionStatus
    source_object_id: UUID | None = None
    source_revision_id: UUID | None = None
    source_revision_digest: str | None = None
    original_sha256: str | None = None
    processing_receipt_id: UUID | None = None
    artifact_id: UUID | None = None
    artifact_digest: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    parser_config_reference: str | None = None
    locator_contract_version: str | None = None
    items: tuple[ParsedContentSegmentResponse, ...] = ()
    next_cursor: str | None = None

    @model_validator(mode="after")
    def validate_disclosure_shape(self) -> "ParsedContentInspectionResponse":
        identity = (
            self.source_object_id,
            self.source_revision_id,
            self.source_revision_digest,
            self.original_sha256,
            self.processing_receipt_id,
            self.artifact_id,
            self.artifact_digest,
            self.parser_name,
            self.parser_version,
            self.parser_config_reference,
            self.locator_contract_version,
        )
        if self.status is ParsedContentInspectionStatus.READY:
            if any(value is None for value in identity):
                raise ValueError("ready parsed content requires exact identity")
        elif any(value is not None for value in identity) or self.items or self.next_cursor:
            raise ValueError("unavailable parsed content cannot carry protected data")
        return self


def install_parsed_content_routes(
    application: FastAPI,
    *,
    service: ParsedContentInspectionService,
    cursor_secret: bytes,
) -> None:
    """Install the current-only, administrator-plus-content-authority route."""

    codec = CursorCodec(secret=cursor_secret)
    application.state.parsed_content_inspection_service = service

    async def inspect_endpoint(
        source_object_id: UUID,
        source_revision_id: UUID,
        request: Request,
        processing_receipt_id: UUID = Query(...),
        page_size: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        locator: str | None = Query(None, max_length=4096),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[ParsedContentInspectionResponse]:
        selector = _parse_locator(locator)
        (
            after_segment_id,
            after_artifact_id,
            after_artifact_digest,
            after_original_sha256,
        ) = _decode_cursor(
            cursor,
            codec=codec,
            context=request_context,
            source_object_id=source_object_id,
            source_revision_id=source_revision_id,
            processing_receipt_id=processing_receipt_id,
            page_size=page_size,
            selector=selector,
        )
        result = await _service(request).inspect(
            context=context,
            requester=ParsedContentInspectionRequester(
                subject_id=request_context.acting_subject_id,
                authorization_generation=request_context.authorization_generation,
                effective_team_ids=request_context.effective_team_ids,
            ),
            query=ParsedContentInspectionQuery(
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
                processing_receipt_id=processing_receipt_id,
                selector=selector,
                after_segment_id=after_segment_id,
                after_artifact_id=after_artifact_id,
                after_artifact_digest=after_artifact_digest,
                after_original_sha256=after_original_sha256,
                page_size=page_size,
            ),
        )
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=_response(
                result,
                processing_receipt_id=processing_receipt_id,
                cursor_codec=codec,
                request_context=request_context,
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
                page_size=page_size,
                selector=selector,
            ),
        )

    add_protected_api_route(
        application,
        "/api/v1/knowledge/documents/{source_object_id}/revisions/{source_revision_id}/parsed-content",
        inspect_endpoint,
        route_id=_OPERATION,
        policy=_policy(),
        methods=["GET"],
        response_model=ApiResponseEnvelope[ParsedContentInspectionResponse],
    )


def _service(request: Request) -> ParsedContentInspectionService:
    service = getattr(request.app.state, "parsed_content_inspection_service", None)
    if not callable(getattr(service, "inspect", None)):
        raise RuntimeError("Parsed content inspection service is unavailable.")
    return cast(ParsedContentInspectionService, service)


def _parse_locator(value: str | None) -> Locator | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
        return _LOCATOR_ADAPTER.validate_python(parsed)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise InvalidParsedContentRequest("parsed content locator is invalid") from None


def _filter_digest(
    *,
    source_object_id: UUID,
    source_revision_id: UUID,
    processing_receipt_id: UUID,
    page_size: int,
    selector: Locator | None,
) -> str:
    return canonical_filter_digest(
        {
            "source_object_id": str(source_object_id),
            "source_revision_id": str(source_revision_id),
            "processing_receipt_id": str(processing_receipt_id),
            "page_size": page_size,
            "max_segment_text_bytes": 65_536,
            "max_total_text_bytes": 262_144,
            "selector": selector.model_dump(mode="json") if selector is not None else None,
        }
    )


def _decode_cursor(
    value: str | None,
    *,
    codec: CursorCodec,
    context: AuthorizedRequestContext[object],
    source_object_id: UUID,
    source_revision_id: UUID,
    processing_receipt_id: UUID,
    page_size: int,
    selector: Locator | None,
) -> tuple[UUID | None, UUID | None, str | None, str | None]:
    if value is None:
        return None, None, None, None
    try:
        claims = codec.decode(
            value,
            workspace_id=context.scope.workspace_id,
            environment_id=context.scope.environment_id,
            operation_id=_OPERATION,
            limit=page_size,
            filters_digest=_filter_digest(
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
                processing_receipt_id=processing_receipt_id,
                page_size=page_size,
                selector=selector,
            ),
        )
        segment, artifact, digest, original_sha256 = claims.position.split("|", 3)
        return UUID(segment), UUID(artifact), digest, original_sha256
    except (InvalidCursor, TypeError, ValueError):
        raise InvalidCursor() from None


def _response(
    result: ParsedContentInspectionResult,
    *,
    processing_receipt_id: UUID,
    cursor_codec: CursorCodec,
    request_context: AuthorizedRequestContext[object],
    source_object_id: UUID,
    source_revision_id: UUID,
    page_size: int,
    selector: Locator | None,
) -> ParsedContentInspectionResponse:
    metadata = result.metadata
    next_cursor = None
    if result.next_after_segment_id is not None and metadata is not None:
        next_cursor = cursor_codec.encode(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            operation_id=_OPERATION,
            limit=page_size,
            filters_digest=_filter_digest(
                source_object_id=source_object_id,
                source_revision_id=source_revision_id,
                processing_receipt_id=processing_receipt_id,
                page_size=page_size,
                selector=selector,
            ),
            position=(
                f"{result.next_after_segment_id}|{metadata.artifact_id}|"
                f"{metadata.artifact_digest}|{metadata.original_sha256}"
            ),
        )
    return ParsedContentInspectionResponse(
        status=result.status,
        source_object_id=metadata.source_object_id if metadata else None,
        source_revision_id=metadata.source_revision_id if metadata else None,
        source_revision_digest=metadata.source_revision_digest if metadata else None,
        original_sha256=metadata.original_sha256 if metadata else None,
        processing_receipt_id=processing_receipt_id if metadata else None,
        artifact_id=metadata.artifact_id if metadata else None,
        artifact_digest=metadata.artifact_digest if metadata else None,
        parser_name=metadata.parser_name if metadata else None,
        parser_version=metadata.parser_version if metadata else None,
        parser_config_reference=metadata.parser_config_reference if metadata else None,
        locator_contract_version=(
            metadata.locator_contract_version if metadata else None
        ),
        items=tuple(
            ParsedContentSegmentResponse(
                segment_id=segment.segment_id,
                segment_digest=segment.segment_digest,
                source_object_id=metadata.source_object_id,
                source_revision_id=metadata.source_revision_id,
                source_revision_digest=metadata.source_revision_digest,
                locator=segment.locator,
                text=segment.text,
                text_digest=segment.text_digest,
                heading_path=segment.heading_path,
            )
            for segment in result.segments
        ),
        next_cursor=next_cursor,
    )


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("inspect_parsed_content"),
    )


__all__ = [
    "ParsedContentInspectionResponse",
    "ParsedContentSegmentResponse",
    "install_parsed_content_routes",
]
