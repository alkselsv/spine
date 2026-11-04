"""Protected read interface for governed transcription processing status."""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Request

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import ApiResponseEnvelope
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.parsed_content_inspection import ParsedContentInspectionRequester
from spine.application.transcription_status import TranscriptionStatusSnapshot
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)


class TranscriptionStatusReader(Protocol):
    async def get_status(
        self,
        *,
        context: TrustedPersistenceContext,
        requester: ParsedContentInspectionRequester,
        workspace_id: UUID,
        environment_id: UUID,
        job_id: UUID,
    ) -> TranscriptionStatusSnapshot: ...


def install_transcription_routes(
    application: FastAPI,
    *,
    service: TranscriptionStatusReader,
) -> None:
    application.state.transcription_status_reader = service

    async def get_transcription_status(
        request: Request,
        job_id: UUID,
        context: TrustedPersistenceContext = Depends(
            get_authorized_persistence_context
        ),
        request_context: AuthorizedRequestContext[object] = Depends(
            get_authorized_request_context
        ),
    ) -> ApiResponseEnvelope[TranscriptionStatusSnapshot]:
        result = await _service(request).get_status(
            context=context,
            requester=ParsedContentInspectionRequester(
                subject_id=request_context.acting_subject_id,
                authorization_generation=request_context.authorization_generation,
                effective_team_ids=request_context.effective_team_ids,
            ),
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            job_id=job_id,
        )
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=result,
        )

    add_protected_api_route(
        application,
        "/api/v1/transcriptions/{job_id}",
        get_transcription_status,
        route_id="transcription.status.read",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("inspect_transcription_status"),
        ),
        methods=["GET"],
        response_model=ApiResponseEnvelope[TranscriptionStatusSnapshot],
    )


def _service(request: Request) -> TranscriptionStatusReader:
    service = getattr(request.app.state, "transcription_status_reader", None)
    if not callable(getattr(service, "get_status", None)):
        raise RuntimeError("Transcription status reader is unavailable.")
    return cast(TranscriptionStatusReader, service)


__all__ = ["TranscriptionStatusReader", "install_transcription_routes"]
