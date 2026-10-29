"""Protected API for exact transcript time-range inspection."""

from __future__ import annotations

from typing import Literal, Protocol, cast
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import ApiResponseEnvelope
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.transcript_inspection import TranscriptInspectionResult
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.artifacts import TimeRangeLocator


class TranscriptEvidenceInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    locator: TimeRangeLocator


class TranscriptEvidenceInspectorService(Protocol):
    async def inspect(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        workspace_id: UUID,
        environment_id: UUID,
        locator: TimeRangeLocator,
    ) -> TranscriptInspectionResult: ...


def install_transcript_evidence_routes(
    application: FastAPI,
    *,
    service: TranscriptEvidenceInspectorService,
) -> None:
    application.state.transcript_evidence_inspector_service = service

    async def inspect_transcript_evidence(
        request: Request,
        body: TranscriptEvidenceInspectionRequest = Body(...),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[TranscriptInspectionResult]:
        result = await _service(request).inspect(
            context=context,
            requester=ParsedContentInspectionRequester(
                subject_id=request_context.acting_subject_id,
                authorization_generation=request_context.authorization_generation,
                effective_team_ids=request_context.effective_team_ids,
            ),
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            locator=body.locator,
        )
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=result,
        )

    add_protected_api_route(
        application,
        "/api/v1/transcript-evidence/inspect",
        inspect_transcript_evidence,
        route_id="transcript.evidence.inspect",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("inspect_parsed_content"),
        ),
        methods=["POST"],
        response_model=ApiResponseEnvelope[TranscriptInspectionResult],
    )


def _service(request: Request) -> TranscriptEvidenceInspectorService:
    service = getattr(request.app.state, "transcript_evidence_inspector_service", None)
    if not callable(getattr(service, "inspect", None)):
        raise RuntimeError("Transcript evidence inspection service is unavailable.")
    return cast(TranscriptEvidenceInspectorService, service)


__all__ = [
    "TranscriptEvidenceInspectionRequest",
    "TranscriptEvidenceInspectorService",
    "install_transcript_evidence_routes",
]
