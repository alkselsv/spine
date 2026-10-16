"""Protected administrator feedback command for accepted Q&A references."""

from __future__ import annotations

from typing import Literal, cast
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import ApiCommandResult, CommandStatus, parse_idempotency_key_header
from spine.application.feedback import (
    FeedbackRequester,
    FeedbackService,
    InvalidFeedbackRequest,
)
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.idempotency import IdempotencyKey
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.feedback import FeedbackOutcome, FeedbackTargetKind


class SubmitFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    message_id: UUID | None = None
    target_kind: FeedbackTargetKind
    target_id: UUID
    citation_id: UUID | None = None
    outcome: FeedbackOutcome
    comment: str | None = Field(default=None, max_length=1_000)
    reason: str | None = Field(default=None, max_length=256)


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    feedback_id: UUID
    feedback_version: Literal[1] = 1
    target_kind: FeedbackTargetKind
    target_id: UUID
    citation_id: UUID | None = None
    run_id: UUID
    message_id: UUID | None = None
    outcome: FeedbackOutcome
    status: Literal["recorded"] = "recorded"
    audit_event_id: UUID | None = None


def install_feedback_routes(application: FastAPI, *, service: FeedbackService) -> None:
    """Register the administrator-only feedback mutation."""

    application.state.feedback_service = service

    async def submit_feedback_endpoint(
        request: Request,
        body: SubmitFeedbackRequest = Body(...),
        idempotency_key_header: str = Header(
            ...,
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
        ),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiCommandResult[FeedbackResponse]:
        del idempotency_key_header
        result = await _service(request).submit(
            context=context,
            requester=FeedbackRequester(
                request_context.acting_subject_id,
                request_context.authorization_generation,
            ),
            run_id=body.run_id,
            message_id=body.message_id,
            target_kind=body.target_kind,
            target_id=body.target_id,
            citation_id=body.citation_id,
            outcome=body.outcome,
            comment=body.comment,
            reason=body.reason,
            idempotency_key=_idempotency(request.headers.getlist("Idempotency-Key")),
        )
        return ApiCommandResult(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            command_id=result.command_id,
            status=CommandStatus.COMPLETED,
            replay=result.replay,
            result=FeedbackResponse(
                feedback_id=result.feedback_id,
                target_kind=result.target_kind,
                target_id=result.target_id,
                citation_id=result.citation_id,
                run_id=result.run_id,
                message_id=result.message_id,
                outcome=result.outcome,
                audit_event_id=result.audit_event_id,
            ),
        )

    add_protected_api_route(
        application,
        "/api/v1/qa/feedback",
        submit_feedback_endpoint,
        route_id="qa.feedback.create",
        policy=RouteAuthorizationPolicy(
            required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
            required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
            purpose=PersistencePurpose("knowledge_control"),
            operation=PersistenceOperation("qa_feedback"),
        ),
        methods=["POST"],
        response_model=ApiCommandResult[FeedbackResponse],
    )


def _service(request: Request) -> FeedbackService:
    service = getattr(request.app.state, "feedback_service", None)
    if service is None:
        raise RuntimeError("Feedback service is unavailable.")
    return cast(FeedbackService, service)


def _idempotency(values: list[str]) -> IdempotencyKey:
    try:
        return IdempotencyKey(parse_idempotency_key_header(values).value)
    except (TypeError, ValueError):
        raise InvalidFeedbackRequest() from None


__all__ = [
    "FeedbackResponse",
    "SubmitFeedbackRequest",
    "install_feedback_routes",
]
