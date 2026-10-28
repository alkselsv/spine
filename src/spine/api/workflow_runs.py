"""Protected API for admitting and reading canonical Workflow Runs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from spine.api.authorization import (
    add_protected_api_route,
    get_authorized_persistence_context,
    get_authorized_request_context,
)
from spine.api.diagnostics import get_diagnostic_context
from spine.api.schemas import (
    CursorCodec,
    InvalidCursor,
    canonical_filter_digest,
    parse_idempotency_key_header,
)
from spine.application.persisted_workflow_runs import PersistedWorkflowRunAdmissionService
from spine.application.persistence.errors import OptimisticConflictError
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedPersistenceContext,
)
from spine.application.workflow_runs import (
    AddContextCommandV1,
    StartWorkflowCommand,
    WorkflowArtifactRef,
    WorkflowCommand,
    WorkflowVersionResolver,
    WorkflowVersionUnavailableError,
)
from spine.auth import (
    AuthorizationRole,
    AuthorizationScopeRequirement,
    AuthorizedRequestContext,
    RouteAuthorizationPolicy,
)
from spine.domain.common import ActorKind, ActorRef
from spine.domain.workflows import (
    AcceptedSourceRevisionRef,
    ClientRequestV1,
    WorkflowRun,
    WorkflowRunSnapshot,
    WorkflowEvent,
)


_EVENTS_OPERATION = "workflow.run.events"


class StartWorkflowRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    workflow_key: str = Field(min_length=1, max_length=128)
    work_item_key: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1, max_length=32_768)
    accepted_sources: tuple[AcceptedSourceRevisionRef, ...] = ()


class WorkflowRunReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    work_item_id: UUID
    run_id: UUID
    workflow_version_id: UUID
    state: str
    run_version: int = Field(ge=1)
    admission: Literal["accepted", "replayed"]


class WorkflowRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    work_item_id: UUID
    workspace_id: UUID
    environment_id: UUID
    workflow_key: str
    workflow_version_id: UUID
    workflow_version: str
    run_version: int = Field(ge=1)
    state: str
    allowed_commands: tuple[Literal["add_context"], ...]
    created_at: datetime


class AddContextWorkflowCommandRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    command_id: UUID
    command_type: Literal["add_context"]
    command_schema_version: Literal[1]
    expected_run_version: int = Field(ge=1, strict=True)
    payload: AddContextCommandV1
    artifact_refs: tuple[WorkflowArtifactRef, ...] = ()
    correlation_id: UUID
    causation_id: UUID


class WorkflowEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    event_type: str
    event_schema_version: int
    run_version: int = Field(ge=1)
    summary: str
    correlation_id: UUID
    causation_id: UUID
    occurred_at: datetime


class WorkflowEventPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    items: tuple[WorkflowEventResponse, ...]
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None = None


def install_workflow_run_routes(
    application: FastAPI,
    *,
    service: PersistedWorkflowRunAdmissionService,
    version_resolver: WorkflowVersionResolver,
    cursor_secret: bytes,
    command_id_source=uuid4,
) -> None:
    """Register shared run admission and authorized read routes."""

    application.state.workflow_run_admission_service = service
    application.state.workflow_version_resolver = version_resolver
    cursor_codec = CursorCodec(secret=cursor_secret)
    policy = RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow.run.start"),
    )
    read_policy = RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow.run.read"),
    )
    command_policy = RouteAuthorizationPolicy(
        required_roles=frozenset({AuthorizationRole.ADMINISTRATOR}),
        required_scope=AuthorizationScopeRequirement.WORKSPACE_ENVIRONMENT,
        purpose=PersistencePurpose("workflow_execution"),
        operation=PersistenceOperation("workflow.run.command"),
    )

    async def start_endpoint(
        request: Request,
        body: StartWorkflowRequestV1 = Body(...),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> JSONResponse:
        scope = persistence_context.scope
        if not isinstance(scope, EnvironmentScope):
            return JSONResponse(status_code=403, content={"code": "workflow.scope_unavailable"})
        try:
            idempotency_key = parse_idempotency_key_header(
                request.headers.getlist("Idempotency-Key")
            )
        except ValueError:
            return JSONResponse(status_code=400, content={"code": "workflow.idempotency_key_invalid"})
        try:
            selected = await version_resolver.resolve(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                workflow_key=body.workflow_key,
            )
        except WorkflowVersionUnavailableError:
            return JSONResponse(status_code=503, content={"code": "workflow.version_unavailable"})
        if selected.workflow_key != body.workflow_key:
            return JSONResponse(status_code=503, content={"code": "workflow.version_unavailable"})
        command_id = command_id_source()
        diagnostic = await get_diagnostic_context(request)
        correlation_id = diagnostic.correlation_id or diagnostic.trace_id
        causation_id = diagnostic.causation_id or command_id
        receipt = await service.start(
            context=persistence_context,
            command=StartWorkflowCommand(
                command_id=command_id,
                idempotency_key=idempotency_key.value,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                actor=ActorRef(kind=ActorKind.HUMAN, id=request_context.acting_subject_id),
                workflow_key=body.workflow_key,
                workflow_version_id=selected.workflow_version_id,
                workflow_version=selected.version,
                work_item_key=body.work_item_key,
                client_request=ClientRequestV1(
                    message=body.message,
                    accepted_sources=body.accepted_sources,
                ),
                trace_id=diagnostic.trace_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
            ),
        )
        response = WorkflowRunReceiptResponse(
            command_id=receipt.command_id,
            work_item_id=receipt.work_item_id,
            run_id=receipt.run_id,
            workflow_version_id=receipt.workflow_version_id,
            state=receipt.state.value,
            run_version=receipt.run_version,
            admission=receipt.admission,
        )
        return JSONResponse(status_code=202, content=response.model_dump(mode="json"))

    async def detail_endpoint(
        run_id: UUID,
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> WorkflowRunResponse:
        run = await service.get_run(context=persistence_context, run_id=run_id)
        if run is None:
            return JSONResponse(status_code=404, content={"code": "workflow.run_not_found"})  # type: ignore[return-value]
        return _response(run)

    async def command_endpoint(
        run_id: UUID,
        request: Request,
        body: AddContextWorkflowCommandRequestV1 = Body(...),
        request_context: AuthorizedRequestContext[object] = Depends(get_authorized_request_context),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> JSONResponse:
        scope = persistence_context.scope
        if not isinstance(scope, EnvironmentScope):
            return JSONResponse(status_code=404, content={"code": "workflow.run_not_found"})
        try:
            idempotency_key = parse_idempotency_key_header(
                request.headers.getlist("Idempotency-Key")
            )
        except ValueError:
            return JSONResponse(status_code=400, content={"code": "workflow.idempotency_key_invalid"})
        diagnostic = await get_diagnostic_context(request)
        try:
            receipt = await service.dispatch(
                context=persistence_context,
                command=WorkflowCommand(
                command_id=body.command_id,
                idempotency_key=idempotency_key.value,
                run_id=run_id,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                actor=ActorRef(kind=ActorKind.HUMAN, id=request_context.acting_subject_id),
                command_type=body.command_type,
                command_schema_version=body.command_schema_version,
                expected_run_version=body.expected_run_version,
                payload=body.payload,
                artifact_refs=body.artifact_refs,
                trace_id=diagnostic.trace_id,
                correlation_id=body.correlation_id,
                causation_id=body.causation_id,
                ),
            )
        except OptimisticConflictError:
            current = await service.get_run(context=persistence_context, run_id=run_id)
            if current is None:
                return JSONResponse(status_code=404, content={"code": "workflow.run_not_found"})
            return JSONResponse(
                status_code=409,
                content={
                    "code": "workflow.run_stale",
                    "current": _response(current).model_dump(mode="json"),
                },
            )
        if receipt is None:
            return JSONResponse(status_code=404, content={"code": "workflow.run_not_found"})
        response = WorkflowRunReceiptResponse(
            command_id=receipt.command_id,
            work_item_id=receipt.work_item_id,
            run_id=receipt.run_id,
            workflow_version_id=receipt.workflow_version_id,
            state=receipt.state.value,
            run_version=receipt.run_version,
            admission=receipt.admission,
        )
        return JSONResponse(status_code=202, content=response.model_dump(mode="json"))

    async def events_endpoint(
        run_id: UUID,
        cursor: str | None = Query(default=None, max_length=768),
        limit: int = Query(default=50, ge=1, le=100),
        persistence_context: TrustedPersistenceContext = Depends(get_authorized_persistence_context),
    ) -> WorkflowEventPageResponse:
        scope = persistence_context.scope
        if not isinstance(scope, EnvironmentScope):
            return JSONResponse(status_code=404, content={"code": "workflow.run_not_found"})  # type: ignore[return-value]
        filters_digest = canonical_filter_digest({"run_id": str(run_id)})
        after_sequence = None
        if cursor is not None:
            claims = cursor_codec.decode(
                cursor,
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                operation_id=_EVENTS_OPERATION,
                limit=limit,
                filters_digest=filters_digest,
            )
            try:
                after_sequence = int(claims.position)
            except ValueError:
                raise InvalidCursor() from None
        page = await service.list_events(
            context=persistence_context,
            run_id=run_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        if page is None:
            return JSONResponse(status_code=404, content={"code": "workflow.run_not_found"})  # type: ignore[return-value]
        next_cursor = None
        if page.next_sequence is not None:
            next_cursor = cursor_codec.encode(
                workspace_id=scope.workspace_id,
                environment_id=scope.environment_id,
                operation_id=_EVENTS_OPERATION,
                limit=limit,
                filters_digest=filters_digest,
                position=str(page.next_sequence),
            )
        return WorkflowEventPageResponse(
            items=tuple(_event_response(item) for item in page.items),
            limit=limit,
            next_cursor=next_cursor,
        )

    add_protected_api_route(
        application,
        "/api/v1/runs",
        start_endpoint,
        route_id="workflow.runs.start",
        policy=policy,
        methods=["POST"],
        response_model=WorkflowRunReceiptResponse,
        status_code=202,
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 255},
                }
            ]
        },
    )
    add_protected_api_route(
        application,
        "/api/v1/runs/{run_id}",
        detail_endpoint,
        route_id="workflow.runs.detail",
        policy=read_policy,
        methods=["GET"],
        response_model=WorkflowRunResponse,
    )
    add_protected_api_route(
        application,
        "/api/v1/runs/{run_id}/commands",
        command_endpoint,
        route_id="workflow.runs.commands.create",
        policy=command_policy,
        methods=["POST"],
        response_model=WorkflowRunReceiptResponse,
        status_code=202,
        openapi_extra={
            "parameters": [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "maxLength": 255},
                }
            ]
        },
    )
    add_protected_api_route(
        application,
        "/api/v1/runs/{run_id}/events",
        events_endpoint,
        route_id="workflow.runs.events.list",
        policy=read_policy,
        methods=["GET"],
        response_model=WorkflowEventPageResponse,
    )


def _response(run: WorkflowRun | WorkflowRunSnapshot) -> WorkflowRunResponse:
    allowed_commands = run.allowed_commands if isinstance(run, WorkflowRunSnapshot) else ()
    return WorkflowRunResponse(
        run_id=run.run_id,
        work_item_id=run.work_item_id,
        workspace_id=run.workspace_id,
        environment_id=run.environment_id,
        workflow_key=run.workflow_key,
        workflow_version_id=run.workflow_version_id,
        workflow_version=run.workflow_version,
        run_version=run.run_version,
        state=run.state.value,
        allowed_commands=allowed_commands,
        created_at=run.created_at,
    )


def _event_response(event: WorkflowEvent) -> WorkflowEventResponse:
    return WorkflowEventResponse(**event.model_dump(mode="python"))


__all__ = [
    "StartWorkflowRequestV1",
    "AddContextWorkflowCommandRequestV1",
    "WorkflowEventPageResponse",
    "WorkflowRunReceiptResponse",
    "WorkflowRunResponse",
    "install_workflow_run_routes",
]
