"""Protected, content-free Q&A run history and timeline routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.schemas import (
    ApiCollectionEnvelope,
    ApiResponseEnvelope,
    CursorCodec,
    DEFAULT_PAGE_LIMIT,
    InvalidCursor,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_LIMIT,
    canonical_filter_digest,
)
from spine.application.persistence.answer_question_runs import AnswerQuestionRunPosition
from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.qa_run_history import (
    InvalidQARunRequest,
    QARunDetail,
    QARunHistoryFilters,
    QARunHistoryRequester,
    QARunHistoryService,
    QARunNotFound,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.application.persistence import PersistenceOperation, PersistencePurpose
from spine.domain.capabilities import AnswerQuestionOutcome


_COLLECTION_OPERATION = "qa.runs.list"
_DETAIL_OPERATION = "qa.runs.detail"


class QARunSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    subject_kind: str
    subject_id: UUID
    state: str
    outcome: AnswerQuestionOutcome | None
    final_attempt_id: UUID | None
    created_at: datetime
    completed_at: datetime | None
    trace_id: UUID
    attempt_count: int = Field(ge=0)
    session_id: UUID | None = None
    message_id: UUID | None = None


class QARunVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identity_id: UUID
    version: str
    digest: str
    configuration_generation: int = Field(ge=1)


class QARunAgentVersionResponse(QARunVersionResponse):
    agent_id: UUID
    agent_version_id: UUID


class QARunProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projection_snapshot_id: UUID
    snapshot_manifest_digest: str
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    projection_config_version_id: UUID
    projection_config_digest: str
    canonical_boundary_id: UUID
    canonical_boundary_digest: str


class QARunExecutionPinResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    configuration_generation: int = Field(ge=1)
    capability_id: UUID
    input_schema: str
    output_schema: str
    agent_version: QARunAgentVersionResponse
    prompt_version: QARunVersionResponse
    model_profile: QARunVersionResponse
    context_profile_binding: str
    context_profile_id: UUID
    context_profile_version: int = Field(ge=1)
    context_profile_digest: str
    projection: QARunProjectionResponse
    semantic_support_policy: QARunVersionResponse
    pin_digest: str


class QARunUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_invocations: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    cost: Decimal = Field(ge=Decimal("0"))
    currency: str | None = None


class QARunTimelineEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    kind: str
    occurred_at: datetime
    reason: str | None = None


class QARunContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: UUID
    bundle_version: int = Field(ge=1)
    bundle_digest: str
    retrieval_strategy: str
    outcome: str
    reason_code: str
    state: str
    projection_snapshot_id: UUID
    projection_activation_id: UUID
    activation_generation: int = Field(ge=1)
    canonical_boundary_id: UUID
    canonical_boundary_digest: str
    canonical_as_of_generation: int = Field(ge=0)
    canonical_as_of_time: datetime
    freshness: str
    health: str
    evidence_count: int = Field(ge=0)


class QARunAttemptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    attempt_id: UUID
    run_id: UUID
    attempt_number: int = Field(ge=1)
    state: str
    outcome: AnswerQuestionOutcome | None
    reason: str | None
    started_at: datetime
    completed_at: datetime | None
    latency_ms: int | None = Field(default=None, ge=0)
    input_artifact_ids: tuple[UUID, ...]
    output_artifact_ids: tuple[UUID, ...]
    context_bundle_id: UUID | None
    context_bundle_version: int | None
    context_bundle_digest: str | None
    agent_result_id: UUID | None
    execution_pin: QARunExecutionPinResponse
    usage: QARunUsageResponse
    timeline: tuple[QARunTimelineEventResponse, ...]
    context: QARunContextResponse | None


class QARunEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_id: UUID
    evidence_id: str
    workspace_id: UUID
    environment_id: UUID
    projection_snapshot_id: UUID
    activation_generation: int = Field(ge=1)
    source_object_id: UUID
    source_revision_id: UUID
    revision_number: int | None = Field(default=None, ge=1)
    locator_schema: str | None
    locator_kind: str
    locator_value: str
    evidence_digest: str


class QARunDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    workspace_id: UUID
    environment_id: UUID
    subject_kind: str
    subject_id: UUID
    state: str
    outcome: AnswerQuestionOutcome | None
    final_attempt_id: UUID | None
    input_artifact_ids: tuple[UUID, ...]
    output_artifact_ids: tuple[UUID, ...]
    command_digest: str
    created_at: datetime
    completed_at: datetime | None
    trace_id: UUID
    correlation_id: UUID | None
    causation_id: UUID | None
    session_id: UUID | None
    message_id: UUID | None
    warnings: tuple[str, ...]
    diagnostic_events: tuple[QARunTimelineEventResponse, ...]
    attempts: tuple[QARunAttemptResponse, ...]
    accepted_evidence: tuple[QARunEvidenceResponse, ...]
    evidence_eligible: bool


def install_qa_run_routes(
    application: FastAPI,
    *,
    service: QARunHistoryService,
    cursor_secret: bytes,
) -> None:
    """Register administrator-only Q&A run read routes."""

    codec = CursorCodec(secret=cursor_secret)
    application.state.qa_run_history_service = service

    async def collection_endpoint(
        request: Request,
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        cursor: str | None = Query(None, min_length=1, max_length=MAX_CURSOR_LENGTH),
        subject_id: UUID | None = Query(None),
        session_id: UUID | None = Query(None),
        message_id: UUID | None = Query(None),
        outcome: AnswerQuestionOutcome | None = Query(None),
        created_after: datetime | None = Query(None),
        created_before: datetime | None = Query(None),
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiCollectionEnvelope[QARunSummaryResponse]:
        try:
            filters = QARunHistoryFilters(
                subject_id=subject_id,
                session_id=session_id,
                message_id=message_id,
                outcome=outcome,
                created_after=created_after,
                created_before=created_before,
            )
        except ValueError:
            raise InvalidQARunRequest() from None
        after = _decode_cursor(
            cursor, codec=codec, context=request_context, limit=limit, filters=filters
        )
        page = await _service(request).list_runs(
            context=context,
            requester=_requester(request_context),
            filters=filters,
            limit=limit,
            after=after,
        )
        items = tuple(_summary(item) for item in page.items)
        next_cursor = None
        if page.has_more and page.items:
            last = page.items[-1].run
            next_cursor = codec.encode(
                workspace_id=request_context.scope.workspace_id,
                environment_id=request_context.scope.environment_id,
                operation_id=_COLLECTION_OPERATION,
                limit=limit,
                filters_digest=_filter_digest(filters),
                position=f"{last.created_at.isoformat()}|{last.run_id}",
            )
        return ApiCollectionEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            items=items,
            limit=limit,
            next_cursor=next_cursor,
        )

    async def detail_endpoint(
        run_id: UUID,
        request: Request,
        context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
    ) -> ApiResponseEnvelope[QARunDetailResponse]:
        detail = await _service(request).get_run(
            context=context,
            requester=_requester(request_context),
            run_id=run_id,
        )
        if detail is None:
            raise QARunNotFound()
        return ApiResponseEnvelope(
            workspace_id=request_context.scope.workspace_id,
            environment_id=request_context.scope.environment_id,
            trace_id=request_context.diagnostic_context.trace_id,
            data=_detail(detail),
        )

    policy = RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("qa_run_history"),
    )
    add_protected_api_route(
        application,
        "/api/v1/qa/runs",
        collection_endpoint,
        route_id=_COLLECTION_OPERATION,
        policy=policy,
        methods=["GET"],
        response_model=ApiCollectionEnvelope[QARunSummaryResponse],
    )
    add_protected_api_route(
        application,
        "/api/v1/qa/runs/{run_id}",
        detail_endpoint,
        route_id=_DETAIL_OPERATION,
        policy=policy,
        methods=["GET"],
        response_model=ApiResponseEnvelope[QARunDetailResponse],
    )


def _service(request: Request) -> QARunHistoryService:
    service = getattr(request.app.state, "qa_run_history_service", None)
    if service is None:
        raise RuntimeError("Q&A run history service is unavailable.")
    return cast(QARunHistoryService, service)


def _requester(context: AuthorizedRequestContext[object]) -> QARunHistoryRequester:
    return QARunHistoryRequester(
        subject_id=context.acting_subject_id,
        authorization_generation=context.authorization_generation,
    )


def _filter_digest(filters: QARunHistoryFilters) -> str:
    return canonical_filter_digest(filters.model_dump(mode="json"))


def _decode_cursor(value, *, codec, context, limit, filters):
    if value is None:
        return None
    try:
        claims = codec.decode(
            value,
            workspace_id=context.scope.workspace_id,
            environment_id=context.scope.environment_id,
            operation_id=_COLLECTION_OPERATION,
            limit=limit,
            filters_digest=_filter_digest(filters),
        )
        timestamp, run_id = claims.position.rsplit("|", 1)
        return AnswerQuestionRunPosition(datetime.fromisoformat(timestamp), UUID(run_id))
    except (InvalidCursor, TypeError, ValueError):
        raise InvalidCursor() from None


def _summary(item) -> QARunSummaryResponse:
    run = item.run
    link = item.message_link
    return QARunSummaryResponse(
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        subject_kind=run.subject.kind.value,
        subject_id=run.subject.id,
        state=run.state.value,
        outcome=run.outcome,
        final_attempt_id=run.final_attempt_id,
        created_at=run.created_at,
        completed_at=run.completed_at,
        trace_id=run.diagnostic_context.trace_id,
        attempt_count=item.attempt_count,
        session_id=getattr(link, "session_id", None),
        message_id=getattr(link, "message_id", None),
    )


def _detail(detail: QARunDetail) -> QARunDetailResponse:
    run = detail.run
    attempts = tuple(
        _attempt(attempt, context)
        for attempt, context in zip(detail.attempts, detail.contexts)
    )
    diagnostic_events = tuple(
        event
        for item in attempts
        for event in item.timeline
    )
    diagnostic_events = tuple(
        sorted(diagnostic_events, key=lambda event: (event.occurred_at, event.event_id.hex))
    )
    return QARunDetailResponse(
        run_id=run.run_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        subject_kind=run.subject.kind.value,
        subject_id=run.subject.id,
        state=run.state.value,
        outcome=run.outcome,
        final_attempt_id=run.final_attempt_id,
        input_artifact_ids=run.input_artifact_ids,
        output_artifact_ids=run.output_artifact_ids,
        command_digest=run.command_digest,
        created_at=run.created_at,
        completed_at=run.completed_at,
        trace_id=run.diagnostic_context.trace_id,
        correlation_id=run.diagnostic_context.correlation_id,
        causation_id=run.diagnostic_context.causation_id,
        session_id=getattr(detail.message_link, "session_id", None),
        message_id=getattr(detail.message_link, "message_id", None),
        warnings=tuple(
            event.reason
            for event in diagnostic_events
            if event.reason is not None and event.kind in {"failed", "cancelled"}
        ),
        diagnostic_events=diagnostic_events,
        attempts=attempts,
        accepted_evidence=tuple(_evidence(item) for item in detail.accepted_evidence),
        evidence_eligible=detail.evidence_eligible,
    )


def _attempt(attempt, context) -> QARunAttemptResponse:
    pin = attempt.execution_pin
    return QARunAttemptResponse(
        schema_version=attempt.schema_version,
        attempt_id=attempt.attempt_id,
        run_id=attempt.run_id,
        attempt_number=attempt.attempt_number,
        state=attempt.state.value,
        outcome=attempt.outcome,
        reason=attempt.reason.value if attempt.reason else None,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        latency_ms=(
            int((attempt.completed_at - attempt.started_at).total_seconds() * 1000)
            if attempt.completed_at
            else None
        ),
        input_artifact_ids=attempt.input_artifact_ids,
        output_artifact_ids=attempt.output_artifact_ids,
        context_bundle_id=attempt.context_bundle_ref.bundle_id if attempt.context_bundle_ref else None,
        context_bundle_version=attempt.context_bundle_ref.bundle_version if attempt.context_bundle_ref else None,
        context_bundle_digest=attempt.context_bundle_ref.bundle_digest if attempt.context_bundle_ref else None,
        agent_result_id=attempt.agent_result_ref.result_id if attempt.agent_result_ref else None,
        execution_pin=_pin(pin),
        usage=QARunUsageResponse.model_validate(attempt.usage.model_dump(mode="python")),
        timeline=tuple(
            QARunTimelineEventResponse(
                event_id=event.event_id,
                kind=event.kind.value,
                occurred_at=event.occurred_at,
                reason=event.reason.value if event.reason else None,
            )
            for event in attempt.events
        ),
        context=_context(context) if context is not None else None,
    )


def _pin(pin) -> QARunExecutionPinResponse:
    return QARunExecutionPinResponse(
        schema_version=pin.schema_version,
        configuration_generation=pin.configuration_generation,
        capability_id=pin.capability_id,
        input_schema=pin.input_schema.name + ":" + pin.input_schema.version,
        output_schema=pin.output_schema.name + ":" + pin.output_schema.version,
        agent_version=QARunAgentVersionResponse(
            identity_id=pin.agent_version.agent_version_id,
            version=str(pin.agent_version.version),
            digest=pin.agent_version.digest,
            configuration_generation=pin.agent_version.configuration_generation,
            agent_id=pin.agent_version.agent_id,
            agent_version_id=pin.agent_version.agent_version_id,
        ),
        prompt_version=_version(pin.prompt_version),
        model_profile=_version(pin.model_profile),
        context_profile_binding=str(pin.context_profile.binding_key),
        context_profile_id=pin.context_profile.profile.profile_id,
        context_profile_version=pin.context_profile.profile.profile_version,
        context_profile_digest=pin.context_profile.profile.profile_digest,
        projection=QARunProjectionResponse(
            projection_snapshot_id=pin.projection.projection_snapshot_id,
            snapshot_manifest_digest=pin.projection.snapshot_manifest_digest,
            projection_activation_id=pin.projection.projection_activation_id,
            activation_generation=pin.projection.activation_generation,
            projection_config_version_id=pin.projection.projection_config_version_id,
            projection_config_digest=pin.projection.projection_config_digest,
            canonical_boundary_id=pin.projection.canonical_boundary_id,
            canonical_boundary_digest=pin.projection.canonical_boundary_digest,
        ),
        semantic_support_policy=_version(pin.semantic_support_policy),
        pin_digest=pin.pin_digest,
    )


def _version(value) -> QARunVersionResponse:
    return QARunVersionResponse(
        identity_id=value.identity_id,
        version=str(value.version),
        digest=value.digest,
        configuration_generation=value.configuration_generation,
    )


def _context(value) -> QARunContextResponse:
    return QARunContextResponse(
        bundle_id=value.bundle_id,
        bundle_version=value.bundle_version,
        bundle_digest=value.bundle_digest,
        retrieval_strategy=value.retrieval_strategy,
        outcome=value.outcome.value,
        reason_code=value.reason_code,
        state=value.state.value,
        projection_snapshot_id=value.projection_snapshot.projection_snapshot_id,
        projection_activation_id=value.projection_activation.projection_activation_id,
        activation_generation=value.activation_generation,
        canonical_boundary_id=value.canonical_boundary.boundary_id,
        canonical_boundary_digest=value.canonical_boundary.manifest_digest,
        canonical_as_of_generation=value.canonical_as_of.accepted_through_generation,
        canonical_as_of_time=value.canonical_as_of.boundary_time,
        freshness=value.freshness.value,
        health=value.health.value,
        evidence_count=len(value.evidence_references),
    )


def _evidence(value) -> QARunEvidenceResponse:
    evidence = value.evidence
    return QARunEvidenceResponse(
        citation_id=value.citation_id,
        evidence_id=evidence.evidence_id,
        workspace_id=evidence.workspace_id,
        environment_id=evidence.environment_id,
        projection_snapshot_id=evidence.projection_snapshot_id,
        activation_generation=evidence.activation_generation,
        source_object_id=evidence.source_object_id,
        source_revision_id=evidence.source_revision_id,
        revision_number=evidence.revision_number,
        locator_schema=evidence.locator_contract_version,
        locator_kind=evidence.locator.kind,
        locator_value=evidence.locator.value,
        evidence_digest=evidence.evidence_digest,
    )


def _policy() -> RouteAuthorizationPolicy:
    return RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("knowledge_control"),
        operation=PersistenceOperation("qa_run_history"),
    )


__all__ = [
    "QARunAttemptResponse",
    "QARunDetailResponse",
    "QARunEvidenceResponse",
    "QARunSummaryResponse",
    "install_qa_run_routes",
]
