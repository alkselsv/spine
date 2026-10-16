"""Administrator API adapter for the production ``answer_question`` result."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    ApiCommandResult,
    CommandStatus,
    parse_idempotency_key_header,
)
from spine.application.authorized_ask import (
    AskCancellationCommand,
    AskCommand,
    AskRequestContext,
    AuthorizedAskResult,
    AuthorizedAskService,
    InvalidAuthorizedAskRequest,
)

if TYPE_CHECKING:
    from spine.application.qa_streaming import QAResultEventPublisher
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.diagnostics import DiagnosticContext
from spine.application.telemetry import (
    OperationalComponent,
    OperationalObservationEmitter,
    OperationalObservationOutcome,
    OperationalOperation,
    OperationalStage,
    SafeOperationalFailureCode,
)
from spine.application.persistence.idempotency import IdempotencyKey
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.domain.capabilities import AnswerQuestionOutcome, AnswerQuestionSafeReasonCode


_MAX_TEXT_LENGTH = 32_768
_IDEMPOTENCY_HEADER = "Idempotency-Key"


class AuthorizedAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    session_id: UUID
    question: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    clarification_of_message_id: UUID | None = None

    @field_validator("question")
    @classmethod
    def reject_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class CancelAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1


class AskCitationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_id: UUID
    evidence_id: str = Field(min_length=1, max_length=128)
    source_object_id: UUID
    source_revision_id: UUID
    revision_digest: str | None
    locator_contract_version: str | None
    locator_kind: str
    locator_value: str
    locator_digest: str | None
    processing_receipt_id: UUID | None


class AskAsOfResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context_bundle_id: UUID
    context_bundle_version: int = Field(ge=1)
    context_bundle_digest: str
    projection_snapshot_id: UUID
    activation_generation: int = Field(ge=1)


class AskAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer_id: UUID
    text: str = Field(min_length=1, max_length=_MAX_TEXT_LENGTH)
    citations: tuple[AskCitationResponse, ...] = Field(min_length=1)


class AskOutcomeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    session_id: UUID
    message_id: UUID
    run_id: UUID
    attempt_id: UUID
    outcome: AnswerQuestionOutcome
    answer: AskAnswerResponse | None = None
    clarification: str | None = None
    reason: AnswerQuestionSafeReasonCode | None = None
    as_of: AskAsOfResponse | None = None


def install_authorized_ask_routes(
    application: FastAPI,
    *,
    service: AuthorizedAskService,
    stream_service: QAResultEventPublisher | None = None,
    operational_observer: OperationalObservationEmitter | None = None,
) -> None:
    """Register the protected ask and cooperative cancellation commands."""

    application.state.authorized_ask_service = service
    application.state.qa_stream_service = stream_service
    application.state.operational_observer = operational_observer

    async def ask_endpoint(
        request: Request,
        body: AuthorizedAskRequest = Body(...),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> ApiCommandResult[AskOutcomeResponse]:
        result = await _service(request).ask(
            AskCommand(
                persistence_context=persistence_context,
                request_context=AskRequestContext(
                    subject_id=request_context.acting_subject_id,
                    workspace_id=request_context.scope.workspace_id,
                    environment_id=request_context.scope.environment_id,
                    trace_id=request_context.diagnostic_context.trace_id,
                    authorization_generation=request_context.authorization_generation,
                ),
                session_id=body.session_id,
                question=body.question,
                clarification_of_message_id=body.clarification_of_message_id,
                idempotency_key=_idempotency(request.headers.getlist("Idempotency-Key")),
            )
        )
        _validate_result_context(request_context, result)
        await _observe_result(request, result)
        await _publish_stream_result(request, result)
        return _command(request_context, result, expected_session_id=body.session_id)

    async def cancel_endpoint(
        run_id: UUID,
        request: Request,
        body: CancelAskRequest = Body(...),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> ApiCommandResult[AskOutcomeResponse]:
        del body
        result = await _service(request).cancel(
            AskCancellationCommand(
                persistence_context=persistence_context,
                request_context=AskRequestContext(
                    subject_id=request_context.acting_subject_id,
                    workspace_id=request_context.scope.workspace_id,
                    environment_id=request_context.scope.environment_id,
                    trace_id=request_context.diagnostic_context.trace_id,
                    authorization_generation=request_context.authorization_generation,
                ),
                run_id=run_id,
                idempotency_key=_idempotency(request.headers.getlist("Idempotency-Key")),
            )
        )
        _validate_result_context(request_context, result)
        await _observe_result(request, result)
        await _publish_stream_result(request, result)
        return _command(request_context, result)

    policy = _policy()
    add_protected_api_route(
        application,
        "/api/v1/ask",
        ask_endpoint,
        route_id="qa.ask",
        policy=policy,
        methods=["POST"],
        response_model=ApiCommandResult[AskOutcomeResponse],
        openapi_extra=_idempotency_openapi_extra(),
    )
    add_protected_api_route(
        application,
        "/api/v1/ask/{run_id}/cancel",
        cancel_endpoint,
        route_id="qa.ask.cancel",
        policy=policy,
        methods=["POST"],
        response_model=ApiCommandResult[AskOutcomeResponse],
        openapi_extra=_idempotency_openapi_extra(),
    )


def _service(request: Request) -> AuthorizedAskService:
    service = getattr(request.app.state, "authorized_ask_service", None)
    if service is None:
        raise RuntimeError("Authorized ask service is unavailable.")
    return cast(AuthorizedAskService, service)


async def _publish_stream_result(request: Request, result: AuthorizedAskResult) -> None:
    stream_service = getattr(request.app.state, "qa_stream_service", None)
    if stream_service is not None:
        publisher = cast("QAResultEventPublisher", stream_service)
        await publisher.publish_result(result)


async def _observe_result(request: Request, result: AuthorizedAskResult) -> None:
    observer = getattr(request.app.state, "operational_observer", None)
    if not isinstance(observer, OperationalObservationEmitter):
        return
    output = result.output
    correlation = output.correlation
    diagnostic_context = DiagnosticContext(
        trace_id=correlation.trace_id,
        correlation_id=correlation.correlation_id,
        causation_id=correlation.causation_id,
    )
    failed = output.outcome in {
        AnswerQuestionOutcome.FAILED,
        AnswerQuestionOutcome.DENIED,
    }
    safe_code = None
    if output.outcome is AnswerQuestionOutcome.DENIED:
        safe_code = SafeOperationalFailureCode.ACCESS_DENIED
    elif output.outcome is AnswerQuestionOutcome.FAILED:
        safe_code = SafeOperationalFailureCode.GENERATION_UNAVAILABLE
    for component, stage, operation in (
        (
            OperationalComponent.API,
            OperationalStage.ACCEPTANCE,
            OperationalOperation.HTTP_REQUEST,
        ),
        (
            OperationalComponent.GENERATION,
            OperationalStage.GENERATION,
            OperationalOperation.ANSWER_GENERATION,
        ),
        (
            OperationalComponent.RUN,
            OperationalStage.PERSISTENCE,
            OperationalOperation.RUN_PERSISTENCE,
        ),
    ):
        await observer.emit(
            workspace_id=output.scope.workspace_id,
            environment_id=output.scope.environment_id,
            diagnostic_context=diagnostic_context,
            component=component,
            stage=stage,
            operation=operation,
            outcome=(
                OperationalObservationOutcome.FAILED
                if failed
                else OperationalObservationOutcome.SUCCEEDED
            ),
            safe_failure_code=safe_code,
            run_id=correlation.run_id,
            attempt_id=correlation.attempt_id,
            message_id=result.message_id,
        )


def _idempotency(values: list[str]) -> IdempotencyKey:
    try:
        return IdempotencyKey(parse_idempotency_key_header(values).value)
    except (TypeError, ValueError):
        raise InvalidAuthorizedAskRequest() from None


def _command(
    request_context: AuthorizedRequestContext[object],
    result: AuthorizedAskResult,
    *,
    expected_session_id: UUID | None = None,
) -> ApiCommandResult[AskOutcomeResponse]:
    _validate_result_context(request_context, result)
    if (
        result.output.scope.workspace_id != request_context.scope.workspace_id
        or result.output.scope.environment_id != request_context.scope.environment_id
        or (expected_session_id is not None and result.session_id != expected_session_id)
    ):
        raise InvalidAuthorizedAskRequest("answer result is outside the trusted request scope")
    return ApiCommandResult(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        trace_id=request_context.diagnostic_context.trace_id,
        command_id=result.command_id,
        status=CommandStatus.COMPLETED,
        replay=result.replay,
        result=_response(result),
    )


def _validate_result_context(
    request_context: AuthorizedRequestContext[object],
    result: AuthorizedAskResult,
) -> None:
    if (
        result.output.scope.workspace_id != request_context.scope.workspace_id
        or result.output.scope.environment_id != request_context.scope.environment_id
        or result.output.correlation.trace_id
        != request_context.diagnostic_context.trace_id
    ):
        raise InvalidAuthorizedAskRequest(
            "answer result is outside the trusted request context"
        )


def _response(result: AuthorizedAskResult) -> AskOutcomeResponse:
    output = result.output
    accepted = result.accepted_answer
    answer = None
    clarification = None
    as_of = None
    reason = output.reason.code if output.reason is not None else None
    if output.outcome is AnswerQuestionOutcome.ANSWERED:
        if accepted is None:
            raise ValueError("answered result crossed the API without accepted content")
        if not accepted.answer.citations:
            raise ValueError("answered result requires citations")
        first_citation = accepted.answer.citations[0]
        bundle = first_citation.context_bundle.bundle
        first_evidence = first_citation.evidence_reference
        answer = AskAnswerResponse(
            answer_id=accepted.answer.answer_id,
            text=accepted.answer.text,
            citations=tuple(
                AskCitationResponse(
                    citation_id=citation.citation_id,
                    evidence_id=citation.evidence_id,
                    source_object_id=citation.source_object_id,
                    source_revision_id=citation.source_revision_id,
                    revision_digest=citation.revision_digest,
                    locator_contract_version=citation.locator_contract_version,
                    locator_kind=citation.evidence_reference.locator.kind,
                    locator_value=citation.evidence_reference.locator.value,
                    locator_digest=citation.locator_digest,
                    processing_receipt_id=citation.processing_receipt_id,
                )
                for citation in accepted.answer.citations
            ),
        )
        as_of = AskAsOfResponse(
            context_bundle_id=bundle.bundle_id,
            context_bundle_version=bundle.bundle_version,
            context_bundle_digest=bundle.bundle_digest,
            projection_snapshot_id=first_evidence.projection_snapshot_id,
            activation_generation=first_evidence.activation_generation,
        )
    elif output.outcome is AnswerQuestionOutcome.CLARIFICATION_REQUIRED:
        clarification = output.clarification.prompt if output.clarification is not None else None
    return AskOutcomeResponse(
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=result.run_id,
        attempt_id=result.attempt_id,
        outcome=output.outcome,
        answer=answer,
        clarification=clarification,
        reason=reason,
        as_of=as_of,
    )


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("qa.ask"),
    )


def _idempotency_openapi_extra() -> dict[str, object]:
    return {
        "parameters": [
            {
                "name": _IDEMPOTENCY_HEADER,
                "in": "header",
                "required": True,
                "schema": {"type": "string", "maxLength": 255},
            }
        ]
    }


__all__ = [
    "AskAnswerResponse",
    "AskAsOfResponse",
    "AskCitationResponse",
    "AskOutcomeResponse",
    "AuthorizedAskRequest",
    "AskRequest",
    "CancelAskRequest",
    "install_authorized_ask_routes",
]

# Compatibility alias for imports that used the adapter before the request
# schema was renamed to avoid colliding with the legacy /ask OpenAPI model.
AskRequest = AuthorizedAskRequest
