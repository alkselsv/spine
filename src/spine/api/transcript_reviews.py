"""Protected commands for approving, rejecting or correcting exact transcripts."""

from __future__ import annotations

from typing import Literal, Protocol, cast
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import ApiResponseEnvelope
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.transcript_review import (
    ReviewTranscriptCommand,
    TranscriptReviewArtifactStatus,
    TranscriptReviewAuthorizationError,
    TranscriptReviewConflictError,
    TranscriptReviewError,
    TranscriptReviewInspection,
    TranscriptReviewReplayConflictError,
    TranscriptReviewResult,
    TranscriptReviewService,
    TranscriptReviewStaleError,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.artifacts import (
    CorrectedTranscriptArtifact,
    SpeechToTextSegment,
    TranscriptReviewDecision,
    TranscriptReviewRecord,
)


class TranscriptReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    transcript_artifact_id: UUID
    transcript_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: TranscriptReviewDecision
    rationale: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(min_length=1, max_length=200)
    corrected_segments: tuple[SpeechToTextSegment, ...] = ()


class TranscriptReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review: TranscriptReviewRecord
    corrected_artifact: CorrectedTranscriptArtifact | None = None
    replay: bool


class TranscriptReviewInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    transcript_artifact_id: UUID
    transcript_artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TranscriptReviewCommandService(Protocol):
    async def review(
        self,
        command: ReviewTranscriptCommand,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
    ) -> TranscriptReviewResult: ...

    async def inspect(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        artifact_id: UUID,
        expected_artifact_digest: str,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
    ) -> TranscriptReviewInspection: ...


def install_transcript_review_routes(
    application: FastAPI,
    *,
    service: TranscriptReviewCommandService,
) -> None:
    application.state.transcript_review_service = service

    async def review_transcript(
        request: Request,
        body: TranscriptReviewRequest = Body(...),
        context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[TranscriptReviewResponse]:
        actor_authority = ",".join(sorted(role.value for role in request_context.roles))
        try:
            result = await _service(request).review(
                ReviewTranscriptCommand(
                    workspace_id=request_context.scope.workspace_id,
                    environment_id=request_context.scope.environment_id,
                    transcript_artifact_id=body.transcript_artifact_id,
                    expected_artifact_digest=body.transcript_artifact_digest,
                    decision=body.decision,
                    rationale=body.rationale,
                    actor_id=request_context.acting_subject_id,
                    actor_authority=actor_authority,
                    idempotency_key=body.idempotency_key,
                    correlation_id=request_context.diagnostic_context.trace_id,
                    corrected_segments=body.corrected_segments,
                ),
                context=context,
                requester=_requester(request_context),
            )
        except TranscriptReviewAuthorizationError as error:
            raise HTTPException(
                status_code=403, detail={"code": "review_forbidden"}
            ) from error
        except TranscriptReviewStaleError as error:
            raise HTTPException(
                status_code=409, detail={"code": "transcript_stale"}
            ) from error
        except (
            TranscriptReviewConflictError,
            TranscriptReviewReplayConflictError,
        ) as error:
            raise HTTPException(
                status_code=409, detail={"code": "review_conflict"}
            ) from error
        except TranscriptReviewError as error:
            raise HTTPException(
                status_code=422, detail={"code": "invalid_review"}
            ) from error
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=TranscriptReviewResponse(
                review=result.review,
                corrected_artifact=result.corrected_artifact,
                replay=result.replay,
            ),
        )

    async def inspect_transcript_review(
        request: Request,
        body: TranscriptReviewInspectionRequest = Body(...),
        context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[TranscriptReviewInspection]:
        inspection = await _service(request).inspect(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            artifact_id=body.transcript_artifact_id,
            expected_artifact_digest=body.transcript_artifact_digest,
            context=context,
            requester=_requester(request_context),
        )
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=inspection,
        )

    add_protected_api_route(
        application,
        "/api/v1/transcript-reviews/decide",
        review_transcript,
        route_id="transcript.review.decide",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("inspect_parsed_content"),
        ),
        methods=["POST"],
        response_model=ApiResponseEnvelope[TranscriptReviewResponse],
    )
    add_protected_api_route(
        application,
        "/api/v1/transcript-reviews/inspect",
        inspect_transcript_review,
        route_id="transcript.review.inspect",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("inspect_parsed_content"),
        ),
        methods=["POST"],
        response_model=ApiResponseEnvelope[TranscriptReviewInspection],
    )


def _service(request: Request) -> TranscriptReviewCommandService:
    service = getattr(request.app.state, "transcript_review_service", None)
    if not callable(getattr(service, "review", None)) or not callable(
        getattr(service, "inspect", None)
    ):
        raise RuntimeError("Transcript review service is unavailable.")
    return cast(TranscriptReviewCommandService, service)


def _requester(
    context: AuthorizedRequestContext[object],
) -> ParsedContentInspectionRequester:
    return ParsedContentInspectionRequester(
        subject_id=context.acting_subject_id,
        authorization_generation=context.authorization_generation,
        effective_team_ids=context.effective_team_ids,
    )


__all__ = [
    "TranscriptReviewCommandService",
    "TranscriptReviewInspectionRequest",
    "TranscriptReviewRequest",
    "TranscriptReviewResponse",
    "install_transcript_review_routes",
]
