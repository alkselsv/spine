"""Administrator API adapter for the production ``answer_question`` result."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    AuthorizedAskServiceUnavailable,
    InvalidAuthorizedAskRequest,
)
from spine.application.ask_admission import (
    AskCancellationLifecycle,
    AskCancellationNotFound,
    AskCancellationStatus,
)
from spine.application.persistence.ask_admission import (
    AskAdmissionApplication,
    AskAdmissionCommand,
    AskAdmissionLifecycle,
    AskAdmissionResult,
    AskAdmissionStatus,
    QA_ASK_ADMISSION_OPERATION,
)
from spine.application.qa_run_history import QARunNotFound

if TYPE_CHECKING:
    from spine.application.qa_streaming import QAResultEventPublisher
from spine.application.persistence.context import TrustedPersistenceContext
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
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind


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


class QAAskAdmissionRequestV1(AuthorizedAskRequest):
    """Versioned request model for the durable admission operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, title="QAAskAdmissionRequestV1")


class QAAskAdmissionResultV1(BaseModel):
    """Content-free identity and lifecycle result for durable Ask admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    session_id: UUID
    message_id: UUID
    run_id: UUID
    status: Literal["admitted", "running", "terminal"]
    outcome: AnswerQuestionOutcome | None = None

    @field_validator("command_id", "session_id", "message_id", "run_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("Ask admission identities must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "QAAskAdmissionResultV1":
        if self.status == "terminal" and self.outcome is None:
            raise ValueError("terminal Ask admission result requires an outcome")
        if self.status != "terminal" and self.outcome is not None:
            raise ValueError("non-terminal Ask admission result cannot carry an outcome")
        return self


class QACancelResultV1(BaseModel):
    """Content-free result of a durable cancellation request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    run_id: UUID
    status: Literal["cancellation_requested", "terminal"]
    outcome: AnswerQuestionOutcome | None = None

    @field_validator("command_id", "run_id")
    @classmethod
    def reject_zero_identity(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("cancellation identities must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "QACancelResultV1":
        if (self.status == "terminal") != (self.outcome is not None):
            raise ValueError("terminal cancellation result must carry its safe outcome")
        return self

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
    service: AuthorizedAskService | None,
    stream_service: QAResultEventPublisher | None = None,
    operational_observer: OperationalObservationEmitter | None = None,
    admission_service: AskAdmissionApplication | None = None,
    admission_environment: EnvironmentKind = EnvironmentKind.PRODUCTION,
) -> None:
    """Register the protected ask and cooperative cancellation commands."""

    application.state.authorized_ask_service = service
    application.state.qa_stream_service = stream_service
    application.state.operational_observer = operational_observer
    application.state.ask_admission_service = admission_service
    application.state.ask_admission_environment = admission_environment

    async def admission_endpoint(
        request: Request,
        body: QAAskAdmissionRequestV1 = Body(...),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> JSONResponse:
        try:
            result = await _admission_service(request).admit(
                context=persistence_context,
                command=AskAdmissionCommand.create(
                    workspace_id=request_context.scope.workspace_id,
                    environment_id=request_context.scope.environment_id,
                    session_id=body.session_id,
                    subject=ActorRef(
                        kind=ActorKind.HUMAN,
                        id=request_context.acting_subject_id,
                    ),
                    environment=getattr(
                        request.app.state,
                        "ask_admission_environment",
                        EnvironmentKind.PRODUCTION,
                    ),
                    question=body.question,
                    idempotency_key=_idempotency(
                        request.headers.getlist("Idempotency-Key")
                    ),
                    clarification_of_message_id=body.clarification_of_message_id,
                ),
            )
            status = await _admission_status(
                request,
                persistence_context=persistence_context,
                result=result,
            )
        except InvalidAuthorizedAskRequest:
            await _observe_api_failure(
                request,
                request_context,
                SafeOperationalFailureCode.VALIDATION_FAILED,
            )
            raise
        except Exception:
            await _observe_api_failure(
                request, request_context, SafeOperationalFailureCode.INTERNAL_FAILURE
            )
            raise
        payload = ApiCommandResult(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            command_id=status.command_id,
            status=(
                CommandStatus.COMPLETED
                if status.status is AskAdmissionLifecycle.TERMINAL
                else CommandStatus.ACCEPTED
            ),
            replay=result.replay,
            result=_admission_response(status),
        )
        return JSONResponse(
            status_code=(
                200 if status.status is AskAdmissionLifecycle.TERMINAL else 202
            ),
            content=payload.model_dump(mode="json"),
        )

    async def ask_endpoint(
        request: Request,
        body: AuthorizedAskRequest = Body(...),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> ApiCommandResult[AskOutcomeResponse] | JSONResponse:
        # Durable configuration has one execution owner: the Temporal worker.
        # Reuse the admission-only operation so this route cannot admit and then
        # invoke the capability inline in the API process.
        if getattr(request.app.state, "ask_admission_service", None) is not None:
            return await admission_endpoint(
                request,
                QAAskAdmissionRequestV1.model_validate(body.model_dump(mode="python")),
                request_context,
                persistence_context,
            )
        try:
            result = await _service(request).ask(
                AskCommand(
                    persistence_context=persistence_context,
                    request_context=_ask_context(request_context),
                    session_id=body.session_id,
                    question=body.question,
                    clarification_of_message_id=body.clarification_of_message_id,
                    idempotency_key=_idempotency(
                        request.headers.getlist("Idempotency-Key")
                    ),
                )
            )
        except InvalidAuthorizedAskRequest:
            await _observe_api_failure(
                request,
                request_context,
                SafeOperationalFailureCode.VALIDATION_FAILED,
            )
            raise
        except Exception:
            await _observe_api_failure(
                request, request_context, SafeOperationalFailureCode.INTERNAL_FAILURE
            )
            raise
        try:
            _validate_result_context(request_context, result)
        except InvalidAuthorizedAskRequest:
            await _observe_api_failure(
                request,
                request_context,
                SafeOperationalFailureCode.CONTEXT_MISMATCH,
            )
            raise
        await _observe_api_success(request, request_context, result)
        await _publish_stream_result(request, result)
        return _command(request_context, result, expected_session_id=body.session_id)

    async def cancel_endpoint(
        run_id: UUID,
        request: Request,
        body: CancelAskRequest = Body(...),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> ApiCommandResult[AskOutcomeResponse | QACancelResultV1] | JSONResponse:
        del body
        cancellation_key = _idempotency(request.headers.getlist("Idempotency-Key"))
        admission_service = getattr(request.app.state, "ask_admission_service", None)
        cancellation_status: AskCancellationStatus | None = None
        if admission_service is not None:
            try:
                cancellation_status = await admission_service.request_cancellation(
                    context=persistence_context,
                    run_id=run_id,
                )
            except AskCancellationNotFound:
                raise QARunNotFound() from None
            except Exception:
                await _observe_api_failure(
                    request, request_context, SafeOperationalFailureCode.INTERNAL_FAILURE
                )
                raise
        if _requests_v1_cancel(request):
            if cancellation_status is None:
                raise AuthorizedAskServiceUnavailable()
            response = QACancelResultV1(
                command_id=cancellation_status.command_id,
                run_id=cancellation_status.run_id,
                status=cancellation_status.status.value,
                outcome=cancellation_status.outcome,
            )
            payload = ApiCommandResult(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                trace_id=request_context.diagnostic_context.trace_id,
                command_id=cancellation_status.command_id,
                status=(
                    CommandStatus.COMPLETED
                    if cancellation_status.status is AskCancellationLifecycle.TERMINAL
                    else CommandStatus.ACCEPTED
                ),
                replay=cancellation_status.replay,
                result=response,
            )
            return JSONResponse(
                status_code=(
                    200
                    if cancellation_status.status is AskCancellationLifecycle.TERMINAL
                    else 202
                ),
                content=payload.model_dump(mode="json"),
            )
        wait_for_terminal = getattr(admission_service, "wait_for_cancellation", None)
        if (
            cancellation_status is not None
            and cancellation_status.status is AskCancellationLifecycle.CANCELLATION_REQUESTED
            and callable(wait_for_terminal)
        ):
            try:
                cancellation_status = await wait_for_terminal(
                    context=persistence_context,
                    run_id=run_id,
                )
            except TimeoutError:
                # The durable request remains recorded and will be dispatched by
                # the outbox worker; let the legacy client retry instead of
                # holding its HTTP connection indefinitely.
                raise AuthorizedAskServiceUnavailable() from None
            except AskCancellationNotFound:
                raise QARunNotFound() from None
            except Exception:
                await _observe_api_failure(
                    request, request_context, SafeOperationalFailureCode.INTERNAL_FAILURE
                )
                raise
        if (
            cancellation_status is not None
            and cancellation_status.status is AskCancellationLifecycle.TERMINAL
        ):
            if (
                cancellation_status.session_id is None
                or cancellation_status.message_id is None
                or cancellation_status.attempt_id is None
                or cancellation_status.outcome is None
            ):
                raise QARunNotFound()
            legacy_result = AskOutcomeResponse(
                session_id=cancellation_status.session_id,
                message_id=cancellation_status.message_id,
                run_id=cancellation_status.run_id,
                attempt_id=cancellation_status.attempt_id,
                outcome=cancellation_status.outcome,
            )
            payload = ApiCommandResult(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                trace_id=request_context.diagnostic_context.trace_id,
                command_id=cancellation_status.command_id,
                status=CommandStatus.COMPLETED,
                replay=True,
                result=legacy_result,
            )
            return JSONResponse(status_code=200, content=payload.model_dump(mode="json"))
        try:
            result = await _service(request).cancel(
                AskCancellationCommand(
                    persistence_context=persistence_context,
                    request_context=_ask_context(request_context),
                    run_id=run_id,
                    idempotency_key=cancellation_key,
                )
            )
        except InvalidAuthorizedAskRequest:
            await _observe_api_failure(
                request,
                request_context,
                SafeOperationalFailureCode.VALIDATION_FAILED,
            )
            raise
        except Exception:
            await _observe_api_failure(
                request, request_context, SafeOperationalFailureCode.INTERNAL_FAILURE
            )
            raise
        try:
            _validate_result_context(request_context, result)
        except InvalidAuthorizedAskRequest:
            await _observe_api_failure(
                request,
                request_context,
                SafeOperationalFailureCode.CONTEXT_MISMATCH,
            )
            raise
        await _observe_api_success(request, request_context, result)
        await _publish_stream_result(request, result)
        return _cancel_command(request_context, result)

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
        "/api/v1/ask/admissions",
        admission_endpoint,
        route_id="qa.ask.admissions",
        policy=_admission_policy(),
        methods=["POST"],
        response_model=ApiCommandResult[QAAskAdmissionResultV1],
        responses={
            200: {"model": ApiCommandResult[QAAskAdmissionResultV1]},
            202: {"model": ApiCommandResult[QAAskAdmissionResultV1]},
        },
        openapi_extra=_idempotency_openapi_extra(),
    )
    add_protected_api_route(
        application,
        "/api/v1/ask/{run_id}/cancel",
        cancel_endpoint,
        route_id="qa.ask.cancel",
        policy=policy,
        methods=["POST"],
        response_model=ApiCommandResult[AskOutcomeResponse | QACancelResultV1],
        responses={
            202: {"model": ApiCommandResult[QACancelResultV1]},
        },
        openapi_extra=_cancel_openapi_extra(),
    )


def _service(request: Request) -> AuthorizedAskService:
    service = getattr(request.app.state, "authorized_ask_service", None)
    if service is None:
        raise RuntimeError("Authorized ask service is unavailable.")
    return cast(AuthorizedAskService, service)


def _admission_service(request: Request) -> AskAdmissionApplication:
    service = getattr(request.app.state, "ask_admission_service", None)
    if service is None:
        raise AuthorizedAskServiceUnavailable()
    return cast(AskAdmissionApplication, service)


def _requests_v1_cancel(request: Request) -> bool:
    accept = request.headers.get("Accept", "")
    for media_range in accept.split(","):
        media_type, *parameters = media_range.strip().split(";")
        if media_type.strip().lower() != "application/vnd.spine.qa-cancel+json":
            continue
        if any(
            parameter.strip().lower().replace('"', "") == "version=1"
            for parameter in parameters
        ):
            return True
    return False


async def _admission_status(
    request: Request,
    *,
    persistence_context: TrustedPersistenceContext,
    result: AskAdmissionResult,
) -> AskAdmissionStatus:
    service = _admission_service(request)
    status_reader = getattr(service, "status", None)
    if callable(status_reader):
        return await status_reader(context=persistence_context, result=result)
    return AskAdmissionStatus(
        command_id=result.command_id,
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=result.run_id,
        status=AskAdmissionLifecycle.ADMITTED,
    )


def _admission_response(status: AskAdmissionStatus) -> QAAskAdmissionResultV1:
    return QAAskAdmissionResultV1(
        command_id=status.command_id,
        session_id=status.session_id,
        message_id=status.message_id,
        run_id=status.run_id,
        status=status.status.value,
        outcome=status.outcome,
    )


async def _publish_stream_result(request: Request, result: AuthorizedAskResult) -> None:
    stream_service = getattr(request.app.state, "qa_stream_service", None)
    if stream_service is not None:
        publisher = cast("QAResultEventPublisher", stream_service)
        await publisher.publish_result(result)


async def _observe_api_success(
    request: Request,
    request_context: AuthorizedRequestContext[object],
    result: AuthorizedAskResult,
) -> None:
    observer = getattr(request.app.state, "operational_observer", None)
    if not isinstance(observer, OperationalObservationEmitter):
        return
    await observer.emit(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        diagnostic_context=request_context.diagnostic_context,
        component=OperationalComponent.API,
        stage=OperationalStage.ACCEPTANCE,
        operation=OperationalOperation.HTTP_REQUEST,
        outcome=OperationalObservationOutcome.SUCCEEDED,
        run_id=result.run_id,
        attempt_id=result.attempt_id,
        message_id=result.message_id,
    )


async def _observe_api_failure(
    request: Request,
    request_context: AuthorizedRequestContext[object],
    failure_code: SafeOperationalFailureCode,
) -> None:
    observer = getattr(request.app.state, "operational_observer", None)
    if not isinstance(observer, OperationalObservationEmitter):
        return
    await observer.emit(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        diagnostic_context=request_context.diagnostic_context,
        component=OperationalComponent.API,
        stage=OperationalStage.ACCEPTANCE,
        operation=OperationalOperation.HTTP_REQUEST,
        outcome=OperationalObservationOutcome.FAILED,
        safe_failure_code=failure_code,
    )


def _idempotency(values: list[str]) -> IdempotencyKey:
    try:
        return IdempotencyKey(parse_idempotency_key_header(values).value)
    except (TypeError, ValueError):
        raise InvalidAuthorizedAskRequest() from None


def _ask_context(
    request_context: AuthorizedRequestContext[object],
) -> AskRequestContext:
    return AskRequestContext(
        subject_id=request_context.acting_subject_id,
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        trace_id=request_context.diagnostic_context.trace_id,
        authorization_generation=request_context.authorization_generation,
        correlation_id=request_context.diagnostic_context.correlation_id,
        causation_id=request_context.diagnostic_context.causation_id,
    )


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


def _cancel_command(
    request_context: AuthorizedRequestContext[object],
    result: AuthorizedAskResult,
) -> ApiCommandResult[AskOutcomeResponse]:
    """Keep the legacy cancellation shape while suppressing protected content."""

    _validate_result_context(request_context, result)
    response = AskOutcomeResponse(
        session_id=result.session_id,
        message_id=result.message_id,
        run_id=result.run_id,
        attempt_id=result.attempt_id,
        outcome=result.output.outcome,
        reason=(result.output.reason.code if result.output.reason is not None else None),
    )
    return ApiCommandResult(
        workspace_id=request_context.scope.workspace_id,
        environment_id=request_context.scope.environment_id,
        trace_id=request_context.diagnostic_context.trace_id,
        command_id=result.command_id,
        status=CommandStatus.COMPLETED,
        replay=result.replay,
        result=response,
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
        or result.output.correlation.correlation_id
        != request_context.diagnostic_context.correlation_id
        or result.output.correlation.causation_id
        != request_context.diagnostic_context.causation_id
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


def _admission_policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=QA_ASK_ADMISSION_OPERATION,
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


def _cancel_openapi_extra() -> dict[str, object]:
    value = _idempotency_openapi_extra()
    parameters = value["parameters"]
    assert isinstance(parameters, list)
    parameters.append(
        {
            "name": "Accept",
            "in": "header",
            "required": False,
            "description": (
                "Send application/vnd.spine.qa-cancel+json;version=1 to receive "
                "the content-free cancellation result."
            ),
            "schema": {"type": "string"},
        }
    )
    return value


__all__ = [
    "AskAnswerResponse",
    "AskAsOfResponse",
    "AskCitationResponse",
    "AskOutcomeResponse",
    "QAAskAdmissionResultV1",
    "QAAskAdmissionRequestV1",
    "QACancelResultV1",
    "AuthorizedAskRequest",
    "AskRequest",
    "CancelAskRequest",
    "install_authorized_ask_routes",
]

# Compatibility alias for imports that used the adapter before the request
# schema was renamed to avoid colliding with the legacy /ask OpenAPI model.
AskRequest = AuthorizedAskRequest
