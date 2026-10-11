from __future__ import annotations

import asyncio
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentHandler,
    AgentRetryDiagnosticState,
    AgentRetryPolicy,
    AgentRetryPolicyError,
    AgentRetryReason,
    AgentRequest,
    AgentResponse,
    ArtifactDraft,
    ArtifactReadError,
    CapabilityArtifactSchema,
    CapabilitySchemaIdentity,
    CancellationController,
    CancellationToken,
    CancellationScope,
    CooperativeCancellationController,
    CooperativeCancellationToken,
    AgentRuntimeAuditError,
    AgentRuntimeDiagnosticEmitter,
    AgentRunDiagnosticState,
    AgentToolDiagnosticState,
    AgentUsageKind,
    InvocationOutputCheckpoint,
    InvocationReplayRecord,
    LocalAgentRuntime,
    TrustedExecutionIdentitySnapshot,
    build_agent_runtime_audit_registry,
    build_agent_runtime_diagnostic_registry,
)
from spine.application.diagnostics import (
    AuditEvent,
    AuditWriter,
    DiagnosticContext,
    DiagnosticEvent,
    Retryability,
    StructuredError,
)
from spine.application.persistence import (
    CommandDigest,
    IdempotencyConflictError,
    IdempotencyKey,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.domain.agents import (
    AgentInvocation,
    AgentResult,
    AgentRetrySafety,
    AgentRunStatus,
    AgentRuntimeKind,
    AgentVersion,
)
from spine.domain.artifacts import Artifact, ArtifactRef, ArtifactState
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind, SchemaRef
from tests.contracts.diagnostics.fixtures import (
    DIAGNOSTIC_LEAK_CORPUS,
    assert_no_diagnostic_leaks,
)
from tests.agents.runtime_fixtures import bound_adapter_runtime


def uid(value: int) -> UUID:
    return UUID(int=value)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


class CombinedInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_question: str
    second_question: str


def cancellation_scope(**overrides: UUID) -> CancellationScope:
    values = {
        "workspace_id": uid(1),
        "environment_id": uid(2),
        "step_run_id": uid(32),
        "agent_run_id": uid(33),
    }
    values.update(overrides)
    return CancellationScope(**values)


def cooperative_cancellation(**overrides: UUID) -> CooperativeCancellationToken:
    controller = CooperativeCancellationController(
        cancellation_scope(**overrides)
    )
    _CANCELLATION_CONTROLLERS[id(controller.token)] = controller
    return controller.token


_CANCELLATION_CONTROLLERS: dict[int, CooperativeCancellationController] = {}


class FakeEventSink:
    def __init__(self) -> None:
        self.events: list[DiagnosticEvent] = []

    async def emit(self, event: object) -> None:
        assert isinstance(event, DiagnosticEvent)
        self.events.append(event)


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> UUID:
        self.events.append(event)
        return uid(80 + len(self.events))


class FailingEventSink:
    async def emit(self, event: object) -> None:
        del event
        raise RuntimeError("diagnostic backend exposed production-token")


class FailingAuditWriter:
    async def append(self, event: AuditEvent) -> UUID:
        del event
        raise RuntimeError("audit backend exposed production-token")


def capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        id=uid(10),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(name="answer_question.input", version="1"),
        output_schema=SchemaRef(name="answer_question.output", version="2"),
    )


def version(*, runtime_config: dict[str, object] | None = None) -> AgentVersion:
    return AgentVersion(
        id=uid(20),
        agent_id=uid(21),
        version="2026.10.1",
        runtime=AgentRuntimeKind.CODE,
        capabilities=("answer_question",),
        runtime_config=runtime_config or {},
    )


def execution_context(**overrides: object) -> AgentExecutionContext:
    selected_capability = capability()
    values: dict[str, object] = {
        "workspace_id": uid(1),
        "environment_id": uid(2),
        "environment": EnvironmentKind.DEVELOPMENT,
        "trusted_identity": TrustedExecutionIdentitySnapshot(
            workspace_id=uid(1),
            environment_id=uid(2),
            acting_subject_id=uid(3),
            authorization_generation=1,
        ),
        "work_item_id": uid(30),
        "workflow_run_id": uid(31),
        "step_run_id": uid(32),
        "agent_run_id": uid(33),
        "capability": selected_capability,
        "schema_identity": CapabilitySchemaIdentity.from_capability(selected_capability),
        "agent_version": version(),
        "idempotency_key": "qa:30:1",
        "diagnostic_context": DiagnosticContext(trace_id=uid(40)),
        "input_artifact_ids": (uid(50),),
        "cancellation": cooperative_cancellation(),
        "event_sink": FakeEventSink(),
    }
    values.update(overrides)
    return AgentExecutionContext(**values)


def retry_execution_context(
    selected_version: AgentVersion,
    **overrides: object,
) -> AgentExecutionContext:
    return execution_context(
        agent_version=selected_version.model_copy(update={"runtime_config": {}}),
        retry_policy=AgentRetryPolicy.from_runtime_config(
            selected_version.runtime_config
        ),
        **overrides,
    )


def invocation(**overrides: object) -> AgentInvocation:
    values: dict[str, object] = {
        "work_item_id": uid(30),
        "step_run_id": uid(32),
        "capability": "answer_question",
        "acting_on_behalf_of": ActorRef(kind=ActorKind.HUMAN, id=uid(3)),
        "input_artifact_ids": (uid(50),),
        "constraints": {"max_sources": 3},
        "idempotency_key": "qa:30:1",
        "retry_safety": AgentRetrySafety.SAFE_TO_REPEAT,
    }
    values.update(overrides)
    return AgentInvocation(**values)


def input_artifact(**overrides: object) -> Artifact:
    values: dict[str, object] = {
        "id": uid(50),
        "workspace_id": uid(1),
        "environment_id": uid(2),
        "work_item_id": uid(30),
        "produced_by": ActorRef(kind=ActorKind.HUMAN, id=uid(3)),
        "artifact_schema": capability().input_schema,
        "content": {"question": "What is the answer?"},
    }
    values.update(overrides)
    return Artifact(**values)


@dataclass
class FakeArtifactPersistence:
    records: dict[UUID, Artifact] = field(default_factory=dict)
    read_error: bool = False
    cancel_on_read: bool = False
    write_error: bool = False
    audit_error: bool = False
    checkpoint_completion_error_after_commit: bool = False
    completion_error_after_commit: bool = False
    returned_ref: ArtifactRef | None = None
    lineage_error_ids: set[UUID] = field(default_factory=set)
    load_calls: list[tuple[UUID, UUID, UUID, UUID]] = field(default_factory=list)
    lineage_calls: list[UUID] = field(default_factory=list)
    writes: list[tuple[tuple[ArtifactDraft, ...], str]] = field(default_factory=list)
    audit_calls: list[AuditEvent] = field(default_factory=list)
    validation_calls: int = 0
    receipts: dict[
        tuple[UUID, UUID, str, int, str],
        tuple[
            CommandDigest,
            OpaqueResultReference,
            InvocationReplayRecord,
        ],
    ] = field(default_factory=dict)

    async def load(
        self,
        artifact_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> Artifact | None:
        self.load_calls.append((artifact_id, workspace_id, environment_id, work_item_id))
        if self.cancel_on_read:
            raise CancelledError
        if self.read_error:
            raise ArtifactReadError("protected record exists")
        return self.records.get(artifact_id)

    async def verify_lineage(
        self,
        artifact: Artifact,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> None:
        del workspace_id, environment_id, work_item_id
        self.lineage_calls.append(artifact.id)
        if artifact.id in self.lineage_error_ids:
            raise ArtifactReadError("protected ancestor exists")

    async def claim_invocation(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        operation_schema_version: int,
        key: IdempotencyKey,
        digest: CommandDigest,
    ) -> OwnedIdempotencyClaim | IdempotencyReplay:
        receipt_key = (
            workspace_id,
            environment_id,
            key.value,
            operation_schema_version,
            digest.operation.value,
        )
        existing = self.receipts.get(receipt_key)
        if existing is not None:
            existing_digest, result_reference, _ = existing
            if existing_digest != digest:
                raise IdempotencyConflictError(
                    "Idempotency key conflicts with existing command."
                )
            return IdempotencyReplay(
                kind="replay",
                receipt_id=uid(200 + len(self.receipts)),
                operation=digest.operation,
                operation_schema_version=operation_schema_version,
                key=key,
                result=result_reference,
            )
        return OwnedIdempotencyClaim(
            kind="owned",
            receipt_id=uid(200 + len(self.receipts)),
            operation=digest.operation,
            operation_schema_version=operation_schema_version,
            key=key,
            digest=digest,
        )

    async def load_invocation_result(
        self,
        replay: IdempotencyReplay,
        *,
        workspace_id: UUID,
        environment_id: UUID,
    ) -> InvocationReplayRecord | None:
        existing = self.receipts.get(
            (
                workspace_id,
                environment_id,
                replay.key.value,
                replay.operation_schema_version,
                replay.operation.value,
            )
        )
        if existing is None or existing[1] != replay.result:
            return None
        return existing[2].model_copy(deep=True)

    async def checkpoint_outputs(
        self,
        checkpoint: InvocationOutputCheckpoint,
        *,
        claim: OwnedIdempotencyClaim,
        result_reference: OpaqueResultReference,
    ) -> InvocationOutputCheckpoint:
        first = checkpoint.drafts[0]
        receipt_key = (
            first.workspace_id,
            first.environment_id,
            claim.key.value,
            claim.operation_schema_version,
            claim.operation.value,
        )
        existing = self.receipts.get(receipt_key)
        expected = (
            claim.digest,
            result_reference,
            InvocationReplayRecord.from_checkpoint(checkpoint),
        )
        if existing is not None and existing != expected:
            raise IdempotencyConflictError(
                "Idempotency key conflicts with existing command."
            )
        self.receipts[receipt_key] = expected
        if self.checkpoint_completion_error_after_commit:
            self.checkpoint_completion_error_after_commit = False
            raise RuntimeError("checkpoint acknowledgement was unavailable")
        return checkpoint.model_copy(deep=True)

    async def persist_outputs(
        self,
        checkpoint: InvocationOutputCheckpoint,
        *,
        claim: OwnedIdempotencyClaim | IdempotencyReplay,
        result_reference: OpaqueResultReference,
        audit_event: AuditEvent,
        validate_references: Any,
    ) -> AgentResult:
        references = (
            self.returned_ref
            or ArtifactRef(
                id=uid(70),
                schema_name="answer_question.output",
                schema_version="2",
            ),
        )
        self.validation_calls += 1
        validate_references(references)
        self.audit_calls.append(audit_event)
        if self.audit_error:
            raise RuntimeError("audit commit exposed production-token")
        if self.write_error:
            raise RuntimeError("database included protected output")
        result = checkpoint.build_result(references)
        first = checkpoint.drafts[0]
        receipt_key = (
            first.workspace_id,
            first.environment_id,
            claim.key.value,
            claim.operation_schema_version,
            claim.operation.value,
        )
        existing = self.receipts.get(receipt_key)
        if existing is None or existing[1] != result_reference:
            raise IdempotencyConflictError(
                "Idempotency key conflicts with existing command."
            )
        existing_digest, _, existing_value = existing
        if isinstance(claim, OwnedIdempotencyClaim) and existing_digest != claim.digest:
            raise IdempotencyConflictError(
                "Idempotency key conflicts with existing command."
            )
        if existing_value.state == "completed":
            assert existing_value.result is not None
            return existing_value.result.model_copy(deep=True)
        if existing_value.checkpoint != checkpoint:
            raise IdempotencyConflictError(
                "Idempotency key conflicts with existing command."
            )
        self.writes.append((checkpoint.drafts, claim.key.value))
        self.receipts[receipt_key] = (
            existing_digest,
            result_reference,
            InvocationReplayRecord.from_result(result),
        )
        if self.completion_error_after_commit:
            self.completion_error_after_commit = False
            raise RuntimeError("commit acknowledgement was unavailable")
        return result.model_copy(deep=True)

    async def persist_result(
        self,
        result: AgentResult,
        *,
        claim: OwnedIdempotencyClaim,
        result_reference: OpaqueResultReference,
        audit_event: AuditEvent,
    ) -> AgentResult:
        self.audit_calls.append(audit_event)
        if self.audit_error:
            raise RuntimeError("audit commit exposed production-token")
        receipt_key = (
            audit_event.workspace_id,
            audit_event.environment_id,
            claim.key.value,
            claim.operation_schema_version,
            claim.operation.value,
        )
        existing = self.receipts.get(receipt_key)
        completed = InvocationReplayRecord.from_result(result)
        if existing is not None and existing != (
            claim.digest,
            result_reference,
            completed,
        ):
            raise IdempotencyConflictError(
                "Idempotency key conflicts with existing command."
            )
        self.receipts[receipt_key] = (claim.digest, result_reference, completed)
        if self.completion_error_after_commit:
            self.completion_error_after_commit = False
            raise RuntimeError("commit acknowledgement was unavailable")
        return result.model_copy(deep=True)


class RecordingHandler:
    def __init__(self, response: object | None = None) -> None:
        self.calls: list[tuple[AgentRequest[Any], AgentExecutionContext]] = []
        self.response = response

    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        self.calls.append((request, context))
        if self.response is not None:
            return self.response  # type: ignore[return-value]
        return AgentResponse(
            schema_identity=context.schema_identity,
            output=OutputModel(answer="42"),
            evidence_refs=("source:revision:page=1",),
            confidence=0.9,
            structured_metrics={"sources": 1.0},
        )


class SyntheticAnswerQuestionHandler:
    async def invoke(
        self,
        request: AgentRequest[InputModel],
        context: AgentExecutionContext,
    ) -> AgentResponse[OutputModel]:
        assert request.input == InputModel(question="What is the answer?")
        return AgentResponse(
            schema_identity=context.schema_identity,
            output=OutputModel(answer="Synthetic grounded answer"),
            evidence_refs=("source:revision:page=1",),
            structured_metrics={"sources": 1.0},
        )


class RaisingHandler(RecordingHandler):
    def __init__(self, protected_message: str = "provider payload is protected") -> None:
        super().__init__()
        self.protected_message = protected_message

    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        self.calls.append((request, context))
        raise RuntimeError(self.protected_message)


class CancellingHandler(RecordingHandler):
    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        self.calls.append((request, context))
        raise CancelledError


class SequenceHandler(RecordingHandler):
    def __init__(self, responses: list[AgentResponse[Any]]) -> None:
        super().__init__()
        self.responses = responses

    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        self.calls.append((request, context))
        return self.responses.pop(0)


class RecordingRetryScheduler:
    def __init__(self) -> None:
        self.delays: list[timedelta] = []

    async def wait(self, delay: timedelta) -> None:
        self.delays.append(delay)


class CancellingRetryScheduler(RecordingRetryScheduler):
    def __init__(
        self,
        controller: CooperativeCancellationController,
        scope: CancellationScope,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.scope = scope

    async def wait(self, delay: timedelta) -> None:
        await super().wait(delay)
        self.controller.request_cancel(self.scope)


class FakeTemporalActivityReexecutor:
    def __init__(self) -> None:
        self.attempt_numbers: list[int] = []

    async def run(
        self,
        activity: Callable[[int], Awaitable[AgentResult]],
    ) -> tuple[AgentResult, AgentResult]:
        self.attempt_numbers.append(1)
        first = await activity(1)
        assert first.status is AgentRunStatus.FAILED
        self.attempt_numbers.append(2)
        return first, await activity(2)


def local_runtime(
    persistence: FakeArtifactPersistence,
    handler: AgentHandler[Any, Any],
    *,
    context: AgentExecutionContext | None = None,
    audit_writer: AuditWriter | None = None,
    cancellation_controller: object | None = None,
    retry_scheduler: object | None = None,
    attempt_id_factory: object | None = None,
) -> LocalAgentRuntime:
    selected_context = context or execution_context()
    selected_controller = cancellation_controller or _CANCELLATION_CONTROLLERS.get(
        id(selected_context.cancellation)
    )
    return LocalAgentRuntime(
        context=selected_context,
        handler=handler,
        artifacts=persistence,
        schemas=CapabilityArtifactSchema(
            schema_identity=selected_context.schema_identity,
            input_model=InputModel,
            output_model=OutputModel,
            assemble_input=lambda artifacts: artifacts[0].content,
            validate_metadata=validate_metadata,
        ),
        diagnostic_registry=build_agent_runtime_diagnostic_registry(),
        audit_registry=build_agent_runtime_audit_registry(),
        audit_writer=audit_writer or RecordingAuditWriter(),
        clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
        cancellation_controller=selected_controller,
        retry_scheduler=retry_scheduler,
        attempt_id_factory=attempt_id_factory,
    )


def observability_dependencies() -> dict[str, object]:
    return {
        "diagnostic_registry": build_agent_runtime_diagnostic_registry(),
        "audit_registry": build_agent_runtime_audit_registry(),
        "audit_writer": RecordingAuditWriter(),
        "clock": lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
    }


def validate_metadata(response: AgentResponse[Any]) -> None:
    if any(not reference.strip() for reference in response.evidence_refs):
        raise ValueError("evidence reference is invalid")
    if len(set(response.evidence_refs)) != len(response.evidence_refs):
        raise ValueError("evidence references must be unique")
    if set(response.structured_metrics) - {"sources"}:
        raise ValueError("structured metric is not permitted")
    if response.proposed_action_ids:
        raise ValueError("proposed actions are not permitted")


def test_runtime_rejects_schema_mapping_for_another_capability_version() -> None:
    incompatible = capability().model_copy(
        update={
            "input_schema": SchemaRef(name="answer_question.input", version="future"),
        }
    )

    context = execution_context()
    with pytest.raises(ValueError, match="schema mapping"):
        LocalAgentRuntime(
            context=context,
            handler=RecordingHandler(),
            artifacts=FakeArtifactPersistence(),
            schemas=CapabilityArtifactSchema(
                schema_identity=CapabilitySchemaIdentity.from_capability(incompatible),
                input_model=InputModel,
                output_model=OutputModel,
                assemble_input=lambda artifacts: artifacts[0].content,
                validate_metadata=validate_metadata,
            ),
            cancellation_controller=_CANCELLATION_CONTROLLERS[
                id(context.cancellation)
            ],
            **observability_dependencies(),
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "workspace_id",
        "environment_id",
        "work_item_id",
        "produced_by_agent_version_id",
    ),
)
def test_artifact_draft_rejects_zero_scope_identity(field_name: str) -> None:
    values: dict[str, object] = {
        "workspace_id": uid(1),
        "environment_id": uid(2),
        "work_item_id": uid(30),
        "produced_by": ActorRef(kind=ActorKind.AGENT, id=uid(21)),
        "produced_by_agent_version_id": uid(20),
        "artifact_schema": capability().output_schema,
        "content": {"answer": "42"},
        "evidence_refs": (),
        "upstream_artifact_ids": (uid(50),),
    }
    values[field_name] = UUID(int=0)

    with pytest.raises(ValueError):
        ArtifactDraft(**values)


@pytest.mark.parametrize(
    "lineage",
    ((UUID(int=0),), (uid(50), uid(50))),
)
def test_artifact_draft_rejects_invalid_lineage(lineage: tuple[UUID, ...]) -> None:
    with pytest.raises(ValueError):
        ArtifactDraft(
            workspace_id=uid(1),
            environment_id=uid(2),
            work_item_id=uid(30),
            produced_by=ActorRef(kind=ActorKind.AGENT, id=uid(21)),
            produced_by_agent_version_id=uid(20),
            artifact_schema=capability().output_schema,
            content={"answer": "42"},
            evidence_refs=(),
            upstream_artifact_ids=lineage,
        )


@pytest.mark.asyncio
async def test_valid_artifact_handoff_persists_lineage_before_reporting_success() -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    handler = RecordingHandler()

    result = await local_runtime(persistence, handler).invoke(version(), invocation())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output_artifact_ids == (uid(70),)
    assert result.evidence_refs == ("source:revision:page=1",)
    assert result.confidence == 0.9
    assert result.structured_metrics == {"sources": 1.0}
    assert result.trace_ref == str(uid(40))
    request, received_context = handler.calls[0]
    assert request.input == InputModel(question="What is the answer?")
    assert request.constraints == {"max_sources": 3}
    assert received_context is not None
    ((draft,), idempotency_key) = persistence.writes[0]
    assert idempotency_key == "qa:30:1"
    assert draft.workspace_id == uid(1)
    assert draft.environment_id == uid(2)
    assert draft.work_item_id == uid(30)
    assert draft.produced_by == ActorRef(kind=ActorKind.AGENT, id=uid(21))
    assert draft.produced_by_agent_version_id == uid(20)
    assert draft.artifact_schema == capability().output_schema
    assert draft.content == {"answer": "42"}
    assert draft.evidence_refs == ("source:revision:page=1",)
    assert draft.upstream_artifact_ids == (uid(50),)


@pytest.mark.asyncio
async def test_legacy_invoke_runs_synthetic_answer_question_with_typed_lineage() -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    runtime = local_runtime(
        persistence,
        SyntheticAnswerQuestionHandler(),
    )

    result = await runtime.invoke(version(), invocation())

    assert result.status is AgentRunStatus.SUCCEEDED
    ((draft,), _) = persistence.writes[0]
    assert draft.content == {"answer": "Synthetic grounded answer"}
    assert draft.evidence_refs == ("source:revision:page=1",)
    assert draft.upstream_artifact_ids == (uid(50),)


@pytest.mark.asyncio
async def test_adapted_async_function_persists_typed_artifact_through_runtime() -> None:
    async def answer_question(
        request: AgentRequest[InputModel],
        context: AgentExecutionContext,
    ) -> OutputModel:
        assert context.capability == request.capability
        return OutputModel(answer=f"Adapted: {request.input.question}")

    async with bound_adapter_runtime(
        answer_question,
        input_artifact_ids=(uid(50),),
    ) as bound:
        acting_subject_id = bound.context.trusted_identity.acting_subject_id
        assert acting_subject_id is not None
        source = input_artifact(
            workspace_id=bound.context.workspace_id,
            environment_id=bound.context.environment_id,
            work_item_id=bound.context.work_item_id,
            produced_by=ActorRef(kind=ActorKind.HUMAN, id=acting_subject_id),
            artifact_schema=bound.context.capability.input_schema,
        )
        persistence = FakeArtifactPersistence(
            records={source.id: source},
            returned_ref=ArtifactRef(
                id=uid(70),
                schema_name=bound.context.capability.output_schema.name,
                schema_version=bound.context.capability.output_schema.version,
            ),
        )
        runtime = LocalAgentRuntime(
            context=bound.context,
            handler=bound.handler,
            artifacts=persistence,
            schemas=CapabilityArtifactSchema(
                schema_identity=bound.context.schema_identity,
                input_model=InputModel,
                output_model=OutputModel,
                assemble_input=lambda artifacts: artifacts[0].content,
                validate_metadata=validate_metadata,
            ),
            cancellation_controller=bound.cancellation,
            **observability_dependencies(),
        )
        selected_invocation = invocation(
            work_item_id=bound.context.work_item_id,
            step_run_id=bound.context.step_run_id,
            acting_on_behalf_of=ActorRef(
                kind=ActorKind.HUMAN,
                id=acting_subject_id,
            ),
            input_artifact_ids=(source.id,),
            idempotency_key=bound.context.idempotency_key,
            retry_safety=AgentRetrySafety.SAFE_TO_REPEAT,
        )

        result = await runtime.invoke(
            bound.context.agent_version,
            selected_invocation,
        )

    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output_artifact_ids == (uid(70),)
    ((draft,), _) = persistence.writes[0]
    assert draft.content == {"answer": "Adapted: What is the answer?"}
    assert draft.artifact_schema == bound.context.capability.output_schema
    assert draft.upstream_artifact_ids == (source.id,)


@pytest.mark.asyncio
async def test_success_emits_separate_registered_diagnostics_and_audit_events() -> None:
    diagnostics = FakeEventSink()
    audit = RecordingAuditWriter()
    context = execution_context(
        event_sink=diagnostics,
        attempt_id=uid(34),
        diagnostic_context=DiagnosticContext(
            trace_id=uid(40),
            correlation_id=uid(41),
            causation_id=uid(42),
        ),
    )
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(
        persistence,
        RecordingHandler(),
        context=context,
        audit_writer=audit,
    ).invoke(version(), invocation())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert [event.event_type for event in diagnostics.events] == [
        "agent.run",
        "agent.run",
    ]
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "started",
        "completed",
    ]
    completed = diagnostics.events[-1]
    assert completed.workspace_id == uid(1)
    assert completed.environment_id == uid(2)
    assert completed.trace_id == uid(40)
    assert completed.correlation_id == uid(41)
    assert completed.causation_id == uid(42)
    assert completed.schema_version == 1
    assert completed.payload_json() == {
        "agent_run_id": str(uid(33)),
        "agent_version_id": str(uid(20)),
        "attempt_id": str(uid(34)),
        "capability_id": str(uid(10)),
        "run_state": "completed",
        "step_run_id": str(uid(32)),
        "work_item_id": str(uid(30)),
        "workflow_run_id": str(uid(31)),
    }
    assert [event.payload_json()["run_state"] for event in audit.events] == [
        "started"
    ]
    assert persistence.audit_calls[0].payload_json()["run_state"] == "completed"
    assert persistence.audit_calls[0].target is not None
    assert persistence.audit_calls[0].target.object_id == uid(33)
    assert "42" not in json.dumps(completed.payload_json())


@pytest.mark.asyncio
async def test_capability_adapter_assembles_multiple_artifacts_by_named_fields() -> None:
    first = input_artifact()
    second = input_artifact(
        id=uid(51),
        content={"question": "What is the second answer?"},
    )
    context = execution_context(input_artifact_ids=(uid(50), uid(51)))
    handler = RecordingHandler()
    persistence = FakeArtifactPersistence(records={uid(50): first, uid(51): second})
    runtime = LocalAgentRuntime(
        context=context,
        handler=handler,
        artifacts=persistence,
        schemas=CapabilityArtifactSchema(
            schema_identity=context.schema_identity,
            input_model=CombinedInputModel,
            output_model=OutputModel,
            assemble_input=lambda artifacts: {
                "first_question": artifacts[0].content["question"],
                "second_question": artifacts[1].content["question"],
            },
            validate_metadata=validate_metadata,
        ),
        cancellation_controller=_CANCELLATION_CONTROLLERS[
            id(context.cancellation)
        ],
        **observability_dependencies(),
    )

    result = await runtime.invoke(
        version(),
        invocation(input_artifact_ids=(uid(50), uid(51))),
    )

    assert result.status is AgentRunStatus.SUCCEEDED
    assert handler.calls[0][0].input == CombinedInputModel(
        first_question="What is the answer?",
        second_question="What is the second answer?",
    )


@pytest.mark.asyncio
async def test_invalid_upstream_lineage_fails_without_disclosing_ancestor() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact(upstream_artifact_ids=(uid(60),))},
        lineage_error_ids={uid(50)},
    )
    handler = RecordingHandler()

    result = await local_runtime(persistence, handler).invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert uid(50) in persistence.lineage_calls
    assert "ancestor" not in result.model_dump_json()
    assert handler.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    (
        None,
        input_artifact(state=ArtifactState.STALE),
        input_artifact(state=ArtifactState.TOMBSTONED),
        input_artifact(workspace_id=uid(99)),
        input_artifact(environment_id=uid(99)),
        input_artifact(work_item_id=uid(99)),
        input_artifact(artifact_schema=SchemaRef(name="other", version="1")),
        input_artifact(content={}),
        input_artifact(content={"question": None}),
        input_artifact(content={"question": "secret", "unexpected": True}),
    ),
    ids=(
        "missing",
        "stale",
        "tombstoned",
        "cross-workspace",
        "cross-environment",
        "cross-work-item",
        "incompatible-schema",
        "missing-field",
        "malformed-field",
        "invalid-content",
    ),
)
async def test_unusable_input_artifact_fails_without_handler_or_data_leak(
    record: Artifact | None,
) -> None:
    persistence = FakeArtifactPersistence(records={} if record is None else {uid(50): record})
    handler = RecordingHandler()

    result = await local_runtime(persistence, handler).invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert "secret" not in result.model_dump_json()
    assert result.output_artifact_ids == ()
    assert handler.calls == []
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_inaccessible_artifact_uses_same_safe_failure_as_missing_artifact() -> None:
    handler = RecordingHandler()
    missing = FakeArtifactPersistence()
    inaccessible = FakeArtifactPersistence(
        records={uid(50): input_artifact(content={"question": "protected"})},
        read_error=True,
    )

    missing_result = await local_runtime(missing, handler).invoke(version(), invocation())
    inaccessible_result = await local_runtime(inaccessible, handler).invoke(
        version(), invocation()
    )

    assert missing_result == inaccessible_result
    assert "protected" not in inaccessible_result.model_dump_json()
    assert handler.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    (
        {},
        {"answer": None},
        {"answer": "42", "unexpected": "protected"},
        {"different": "42"},
    ),
    ids=("missing-field", "malformed-field", "extra-field", "incompatible-shape"),
)
async def test_incompatible_handler_output_is_not_persisted(
    output: dict[str, object],
) -> None:
    response = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        output=output,
    )
    handler = RecordingHandler(response)
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, handler).invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert len(handler.calls) == 1
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_capability_metadata_policy_rejects_unpermitted_metadata() -> None:
    response = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        output=OutputModel(answer="42"),
        structured_metrics={"unsupported": 1.0},
    )
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, RecordingHandler(response)).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_unpersisted_proposed_action_reference_is_rejected() -> None:
    response = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        output=OutputModel(answer="42"),
        proposed_action_ids=(uid(90),),
    )
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, RecordingHandler(response)).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert result.proposed_action_ids == ()
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_handler_exception_returns_safe_internal_failure() -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, RaisingHandler()).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "An unexpected internal error occurred."
    assert "provider payload" not in result.model_dump_json()
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_explicitly_eligible_transient_failure_retries_with_pinned_identity() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0.25],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    transient = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code="spine.provider.unavailable",
            safe_message="Agent provider is temporarily unavailable.",
            retryability=Retryability.AFTER_DELAY,
            trace_id=uid(40),
        ),
    )
    handler = SequenceHandler(
        [
            transient,
            AgentResponse(
                schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
                output=OutputModel(answer="42"),
            ),
        ]
    )
    diagnostics = FakeEventSink()
    scheduler = RecordingRetryScheduler()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    context = retry_execution_context(
        selected_version,
        event_sink=diagnostics,
        attempt_id=uid(34),
    )

    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=scheduler,
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(context.agent_version, invocation())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert scheduler.delays == [timedelta(milliseconds=250)]
    assert len(handler.calls) == 2
    assert [request.local_attempt_id for request, _ in handler.calls] == [
        uid(101),
        uid(102),
    ]
    assert all(
        call_context.agent_version == context.agent_version
        for _, call_context in handler.calls
    )
    assert all(call_context.workspace_id == uid(1) for _, call_context in handler.calls)
    assert all(
        call_context.diagnostic_context.trace_id == uid(40)
        for _, call_context in handler.calls
    )
    assert len(persistence.writes) == 1
    retry_event = next(
        event for event in diagnostics.events if event.event_type == "agent.retry"
    )
    assert retry_event.payload_json() == {
        "agent_run_id": str(uid(33)),
        "agent_version_id": str(uid(20)),
        "attempt_id": str(uid(102)),
        "attempt_number": 2,
        "capability_id": str(uid(10)),
        "retry_reason_code": "provider.unavailable",
        "retry_state": "scheduled",
        "step_run_id": str(uid(32)),
        "work_item_id": str(uid(30)),
        "workflow_run_id": str(uid(31)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "message", "reason"),
    [
        (
            "spine.provider.rate_limited",
            "Agent provider rate limit was reached.",
            "provider.rate_limited",
        ),
        (
            "spine.tool.unavailable",
            "Required tool is temporarily unavailable.",
            "tool.unavailable",
        ),
    ],
)
async def test_each_declared_transient_reason_requires_explicit_policy_eligibility(
    code: str,
    message: str,
    reason: str,
) -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0],
                "eligible_reasons": [reason],
            }
        }
    )
    transient = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code=code,
            safe_message=message,
            retryability=Retryability.AFTER_DELAY,
            trace_id=uid(40),
        ),
    )
    handler = SequenceHandler(
        [
            transient,
            AgentResponse(
                schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
                output=OutputModel(answer="42"),
            ),
        ]
    )
    diagnostics = FakeEventSink()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    context = retry_execution_context(
        selected_version,
        event_sink=diagnostics,
        attempt_id=uid(34),
    )
    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=RecordingRetryScheduler(),
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(context.agent_version, invocation())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert len(handler.calls) == 2
    retry_event = next(
        event for event in diagnostics.events if event.event_type == "agent.retry"
    )
    assert retry_event.payload_json()["retry_reason_code"] == reason


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_one_sanitized_terminal_result() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 3,
                "backoff_seconds": [0.25, 0.5],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    transient = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code="spine.provider.unavailable",
            safe_message="Agent provider is temporarily unavailable.",
            retryability=Retryability.AFTER_DELAY,
            trace_id=uid(40),
        ),
    )
    handler = SequenceHandler([transient, transient, transient])
    diagnostics = FakeEventSink()
    scheduler = RecordingRetryScheduler()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    context = retry_execution_context(
        selected_version,
        event_sink=diagnostics,
        attempt_id=uid(34),
    )

    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=scheduler,
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(context.agent_version, invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "Agent provider is temporarily unavailable."
    assert result.trace_ref == str(uid(40))
    assert len(handler.calls) == 3
    assert scheduler.delays == [
        timedelta(milliseconds=250),
        timedelta(milliseconds=500),
    ]
    assert persistence.writes == []
    retry_events = [
        event.payload_json()
        for event in diagnostics.events
        if event.event_type == "agent.retry"
    ]
    assert [event["retry_state"] for event in retry_events] == [
        "scheduled",
        "scheduled",
        "exhausted",
    ]
    assert [event["attempt_id"] for event in retry_events] == [
        str(uid(102)),
        str(uid(103)),
        str(uid(103)),
    ]
    assert [event["attempt_number"] for event in retry_events] == [2, 3, 3]


@pytest.mark.asyncio
async def test_ambiguous_external_effect_never_retries() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0.25],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    ambiguous = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code="spine.execution.ambiguous_effect",
            safe_message="External effect outcome is ambiguous and requires recovery.",
            retryability=Retryability.AFTER_DELAY,
            trace_id=uid(40),
        ),
    )
    handler = RecordingHandler(ambiguous)
    scheduler = RecordingRetryScheduler()
    diagnostics = FakeEventSink()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    context = retry_execution_context(
        selected_version,
        event_sink=diagnostics,
        attempt_id=uid(34),
    )
    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=scheduler,
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(
        context.agent_version,
        invocation(retry_safety=AgentRetrySafety.SAFE_TO_REPEAT),
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "External effect outcome is ambiguous and requires recovery."
    assert len(handler.calls) == 1
    assert scheduler.delays == []
    assert all(event.event_type != "agent.retry" for event in diagnostics.events)


@pytest.mark.asyncio
async def test_configured_retry_requires_scheduler_and_attempt_identity_before_execution() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0.25],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    handler = RecordingHandler()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    context = retry_execution_context(selected_version)

    result = await local_runtime(persistence, handler, context=context).invoke(
        context.agent_version,
        invocation(),
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert handler.calls == []
    assert persistence.load_calls == []
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_caller_cannot_replace_the_pinned_agent_version_retry_policy() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0.25],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    context = retry_execution_context(selected_version)
    substituted = context.agent_version.model_copy(
        update={
            "runtime_config": {
                "runtime_retry_policy": {
                    "maximum_attempts": 3,
                    "backoff_seconds": [0, 0],
                    "eligible_reasons": ["internal.transient"],
                }
            }
        }
    )
    handler = RecordingHandler()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=RecordingRetryScheduler(),
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(substituted, invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert handler.calls == []
    assert persistence.load_calls == []


@pytest.mark.asyncio
async def test_transient_failure_requires_invocation_safe_to_repeat_policy() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0.25],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    transient = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code="spine.provider.unavailable",
            safe_message="Agent provider is temporarily unavailable.",
            retryability=Retryability.AFTER_DELAY,
            trace_id=uid(40),
        ),
    )
    context = retry_execution_context(selected_version)
    handler = RecordingHandler(transient)
    scheduler = RecordingRetryScheduler()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=scheduler,
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(
        context.agent_version,
        invocation(retry_safety=AgentRetrySafety.NEVER),
    )

    assert result.status is AgentRunStatus.FAILED
    assert len(handler.calls) == 1
    assert scheduler.delays == []


@pytest.mark.asyncio
async def test_retry_policy_rejects_unbounded_attempts_before_execution() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 6,
                "backoff_seconds": [0, 0, 0, 0, 0],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    with pytest.raises(AgentRetryPolicyError, match="retry policy"):
        AgentRetryPolicy.from_runtime_config(selected_version.runtime_config)


@pytest.mark.asyncio
async def test_unlisted_retryable_failure_never_retries() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0.25],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    authorization_failure = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code="spine.authorization.unavailable",
            safe_message="Authorization is temporarily unavailable.",
            retryability=Retryability.AFTER_DELAY,
            trace_id=uid(40),
        ),
    )
    handler = RecordingHandler(authorization_failure)
    scheduler = RecordingRetryScheduler()
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    context = retry_execution_context(selected_version)

    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=scheduler,
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(context.agent_version, invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "Authorization is temporarily unavailable."
    assert len(handler.calls) == 1
    assert scheduler.delays == []


@pytest.mark.asyncio
async def test_cancellation_during_retry_wait_prevents_next_attempt() -> None:
    selected_version = version(
        runtime_config={
            "runtime_retry_policy": {
                "maximum_attempts": 2,
                "backoff_seconds": [0.25],
                "eligible_reasons": ["provider.unavailable"],
            }
        }
    )
    transient = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code="spine.provider.unavailable",
            safe_message="Agent provider is temporarily unavailable.",
            retryability=Retryability.AFTER_DELAY,
            trace_id=uid(40),
        ),
    )
    handler = SequenceHandler([transient])
    context = retry_execution_context(selected_version, attempt_id=uid(34))
    controller = _CANCELLATION_CONTROLLERS[id(context.cancellation)]
    scheduler = CancellingRetryScheduler(controller, cancellation_scope())
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(
        persistence,
        handler,
        context=context,
        retry_scheduler=scheduler,
        attempt_id_factory=lambda attempt_number: uid(100 + attempt_number),
    ).invoke(context.agent_version, invocation())

    assert result.status is AgentRunStatus.CANCELLED
    assert result.error == "Execution was cancelled."
    assert len(handler.calls) == 1
    assert scheduler.delays == [timedelta(milliseconds=250)]


@pytest.mark.asyncio
async def test_equivalent_terminal_failure_replays_without_handler_repeat() -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    first_handler = RaisingHandler()
    second_handler = RaisingHandler()

    first = await local_runtime(persistence, first_handler).invoke(
        version(), invocation()
    )
    replayed = await local_runtime(persistence, second_handler).invoke(
        version(), invocation()
    )

    assert replayed == first
    assert first.status is AgentRunStatus.FAILED
    assert len(first_handler.calls) == 1
    assert second_handler.calls == []
    assert persistence.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_value", DIAGNOSTIC_LEAK_CORPUS)
async def test_failure_diagnostics_reject_negative_disclosure_corpus(
    protected_value: str,
) -> None:
    diagnostics = FakeEventSink()
    audit = RecordingAuditWriter()
    context = execution_context(event_sink=diagnostics, attempt_id=uid(34))
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(
        persistence,
        RaisingHandler(protected_value),
        context=context,
        audit_writer=audit,
    ).invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert diagnostics.events[-1].payload_json()["run_state"] == "failed"
    captures = [
        result.model_dump_json(),
        *(json.dumps(event.payload_json()) for event in diagnostics.events),
        *(event.model_dump_json() for event in audit.events),
    ]
    assert_no_diagnostic_leaks(captures, leak_corpus=(protected_value,))


@pytest.mark.asyncio
async def test_observation_sink_failures_cannot_rewrite_canonical_result() -> None:
    context = execution_context(event_sink=FailingEventSink())
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(
        persistence,
        RecordingHandler(),
        context=context,
        audit_writer=RecordingAuditWriter(),
    ).invoke(version(), invocation())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output_artifact_ids == (uid(70),)


@pytest.mark.asyncio
async def test_required_audit_failure_blocks_execution_with_safe_error() -> None:
    context = execution_context(event_sink=FakeEventSink())
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    handler = RecordingHandler()

    with pytest.raises(AgentRuntimeAuditError) as captured:
        await local_runtime(
            persistence,
            handler,
            context=context,
            audit_writer=FailingAuditWriter(),
        ).invoke(version(), invocation())

    assert str(captured.value) == "Agent runtime audit is unavailable."
    assert "production-token" not in str(captured.value)
    assert handler.calls == []


@pytest.mark.asyncio
async def test_handler_cancellation_emits_cancelled_without_reporting_success() -> None:
    diagnostics = FakeEventSink()
    audit = RecordingAuditWriter()
    context = execution_context(event_sink=diagnostics, attempt_id=uid(34))
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(
        persistence,
        CancellingHandler(),
        context=context,
        audit_writer=audit,
    ).invoke(version(), invocation())

    assert result.status is AgentRunStatus.CANCELLED
    assert result.error == "Execution was cancelled."
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "started",
        "cancelled",
    ]
    assert [event.payload_json()["run_state"] for event in audit.events] == [
        "started",
    ]
    assert persistence.audit_calls[0].payload_json()["run_state"] == "cancelled"


@pytest.mark.asyncio
async def test_artifact_io_cancellation_is_observed_and_propagated() -> None:
    diagnostics = FakeEventSink()
    audit = RecordingAuditWriter()
    context = execution_context(event_sink=diagnostics, attempt_id=uid(34))
    persistence = FakeArtifactPersistence(cancel_on_read=True)

    with pytest.raises(CancelledError):
        await local_runtime(
            persistence,
            RecordingHandler(),
            context=context,
            audit_writer=audit,
        ).invoke(version(), invocation())

    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "started",
        "cancelled",
    ]
    assert [event.payload_json()["run_state"] for event in audit.events] == [
        "started",
        "cancelled",
    ]


@pytest.mark.asyncio
async def test_cancel_request_emits_observation_without_fabricating_audit_transition() -> None:
    diagnostics = FakeEventSink()
    audit = RecordingAuditWriter()
    cancellation = cooperative_cancellation()
    context = execution_context(
        event_sink=diagnostics,
        attempt_id=uid(34),
        cancellation=cancellation,
    )
    runtime = local_runtime(
        FakeArtifactPersistence(),
        RecordingHandler(),
        context=context,
        audit_writer=audit,
    )

    await runtime.cancel(uid(32), scope=cancellation_scope())

    assert cancellation.is_cancelled is True
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "cancellation_requested"
    ]
    assert audit.events == []


@pytest.mark.asyncio
async def test_legacy_cancel_request_uses_the_runtime_pinned_scope() -> None:
    diagnostics = FakeEventSink()
    cancellation = cooperative_cancellation()
    context = execution_context(event_sink=diagnostics, cancellation=cancellation)
    runtime = local_runtime(
        FakeArtifactPersistence(),
        RecordingHandler(),
        context=context,
    )

    await runtime.cancel(uid(32))

    assert cancellation.is_cancelled is True
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "cancellation_requested"
    ]


@pytest.mark.asyncio
async def test_repeated_cancel_request_is_idempotent() -> None:
    diagnostics = FakeEventSink()
    cancellation = cooperative_cancellation()
    context = execution_context(event_sink=diagnostics, cancellation=cancellation)
    runtime = local_runtime(
        FakeArtifactPersistence(),
        RecordingHandler(),
        context=context,
    )

    await runtime.cancel(uid(32), scope=cancellation_scope())
    await runtime.cancel(uid(32), scope=cancellation_scope())

    assert cancellation.is_cancelled is True
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "cancellation_requested"
    ]


@pytest.mark.asyncio
async def test_cancel_after_commit_boundary_cannot_replace_success() -> None:
    commit_started = asyncio.Event()
    resume_commit = asyncio.Event()

    class BlockingPersistence(FakeArtifactPersistence):
        async def persist_outputs(
            self,
            checkpoint: InvocationOutputCheckpoint,
            *,
            claim: OwnedIdempotencyClaim | IdempotencyReplay,
            result_reference: OpaqueResultReference,
            audit_event: AuditEvent,
            validate_references: Any,
        ) -> AgentResult:
            commit_started.set()
            await resume_commit.wait()
            return await super().persist_outputs(
                checkpoint,
                claim=claim,
                result_reference=result_reference,
                audit_event=audit_event,
                validate_references=validate_references,
            )

    diagnostics = FakeEventSink()
    context = execution_context(event_sink=diagnostics)
    persistence = BlockingPersistence(records={uid(50): input_artifact()})
    runtime = local_runtime(persistence, RecordingHandler(), context=context)

    invocation_task = asyncio.create_task(runtime.invoke(version(), invocation()))
    await commit_started.wait()
    await runtime.cancel(uid(32), scope=cancellation_scope())
    resume_commit.set()
    result = await invocation_task
    await runtime.cancel(uid(32), scope=cancellation_scope())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert len(persistence.writes) == 1
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "started",
        "completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope_override", "value"),
    [
        ("workspace_id", uid(101)),
        ("environment_id", uid(102)),
        ("step_run_id", uid(103)),
        ("agent_run_id", uid(104)),
    ],
)
async def test_cancel_request_rejects_every_cross_run_scope(
    scope_override: str,
    value: UUID,
) -> None:
    cancellation = cooperative_cancellation()
    context = execution_context(cancellation=cancellation)
    runtime = local_runtime(
        FakeArtifactPersistence(),
        RecordingHandler(),
        context=context,
    )
    with pytest.raises(ValueError, match="cancellation scope is invalid"):
        mismatched_scope = cancellation_scope(**{scope_override: value})
        await runtime.cancel(mismatched_scope.step_run_id, scope=mismatched_scope)

    assert cancellation.is_cancelled is False


@pytest.mark.asyncio
async def test_cancellation_isolated_between_agent_runs() -> None:
    first_cancellation = cooperative_cancellation()
    second_cancellation = cooperative_cancellation(
        workspace_id=uid(101),
        environment_id=uid(102),
        agent_run_id=uid(34),
    )
    first_context = execution_context(
        cancellation=first_cancellation,
        agent_run_id=uid(33),
        event_sink=FakeEventSink(),
    )
    second_context = execution_context(
        cancellation=second_cancellation,
        workspace_id=uid(101),
        environment_id=uid(102),
        agent_run_id=uid(34),
        trusted_identity=TrustedExecutionIdentitySnapshot(
            workspace_id=uid(101),
            environment_id=uid(102),
            acting_subject_id=uid(3),
            authorization_generation=1,
        ),
        event_sink=FakeEventSink(),
    )
    first_runtime = local_runtime(
        FakeArtifactPersistence(records={uid(50): input_artifact()}),
        RecordingHandler(),
        context=first_context,
    )
    second_runtime = local_runtime(
        FakeArtifactPersistence(
            records={
                uid(50): input_artifact(
                    workspace_id=uid(101),
                    environment_id=uid(102),
                )
            }
        ),
        RecordingHandler(),
        context=second_context,
    )

    await first_runtime.cancel(uid(32), scope=cancellation_scope())
    first_result, second_result = await asyncio.gather(
        first_runtime.invoke(version(), invocation()),
        second_runtime.invoke(version(), invocation()),
    )

    assert first_result.status is AgentRunStatus.CANCELLED
    assert second_result.status is AgentRunStatus.SUCCEEDED
    assert second_cancellation.is_cancelled is False


@pytest.mark.asyncio
async def test_running_handler_shares_cancellation_with_nested_call_and_tool() -> None:
    started = asyncio.Event()
    resume = asyncio.Event()
    observations: list[tuple[str, object, bool]] = []
    diagnostics = FakeEventSink()

    async def nested_call(cancellation: CancellationToken) -> None:
        assert cancellation is cooperative
        observations.append(("nested", cancellation, cancellation.is_cancelled))

    class PermittedTool:
        async def invoke(self, cancellation: CancellationToken) -> None:
            assert cancellation is cooperative
            observations.append(("tool", cancellation, cancellation.is_cancelled))

    permitted_tool = PermittedTool()

    class CooperativeHandler:
        async def invoke(
            self,
            request: AgentRequest[InputModel],
            context: AgentExecutionContext,
        ) -> AgentResponse[OutputModel]:
            del request
            assert not isinstance(context.cancellation, CancellationController)
            assert not hasattr(context.cancellation, "request_cancel")
            assert not hasattr(context.cancellation, "seal")
            started.set()
            await resume.wait()
            await nested_call(context.cancellation)
            await permitted_tool.invoke(context.cancellation)
            context.cancellation.raise_if_cancelled()
            raise AssertionError("cancelled handler continued")

    cooperative = cooperative_cancellation()
    context = execution_context(
        cancellation=cooperative,
        event_sink=diagnostics,
    )
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    runtime = local_runtime(
        persistence,
        CooperativeHandler(),
        context=context,
    )

    invocation_task = asyncio.create_task(runtime.invoke(version(), invocation()))
    await started.wait()
    await runtime.cancel(uid(32), scope=cancellation_scope())
    resume.set()
    result = await invocation_task

    assert result.status is AgentRunStatus.CANCELLED
    assert observations == [
        ("nested", cooperative, True),
        ("tool", cooperative, True),
    ]
    assert persistence.writes == []
    assert [event.event_type for event in diagnostics.events] == [
        "agent.run",
        "agent.run",
        "agent.run",
    ]
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "started",
        "cancellation_requested",
        "cancelled",
    ]


def test_runtime_rejects_cancellation_handle_bound_to_another_run() -> None:
    context = execution_context(
        cancellation=cooperative_cancellation(agent_run_id=uid(34)),
    )

    with pytest.raises(
        ValueError,
        match="local runtime cancellation scope is incompatible",
    ):
        local_runtime(
            FakeArtifactPersistence(records={uid(50): input_artifact()}),
            RecordingHandler(),
            context=context,
        )


def test_runtime_rejects_observation_only_cancellation_token() -> None:
    class ObservationOnlyCancellation:
        is_cancelled = False

        def raise_if_cancelled(self) -> None:
            return None

    context = execution_context(cancellation=ObservationOnlyCancellation())

    with pytest.raises(
        TypeError,
        match="local runtime cancellation control is unavailable",
    ):
        local_runtime(
            FakeArtifactPersistence(records={uid(50): input_artifact()}),
            RecordingHandler(),
            context=context,
        )


@pytest.mark.asyncio
async def test_retry_tool_and_usage_observations_use_registered_safe_payloads() -> None:
    diagnostics = FakeEventSink()
    context = execution_context(event_sink=diagnostics, attempt_id=uid(34))
    emitter = AgentRuntimeDiagnosticEmitter(
        context=context,
        diagnostic_registry=build_agent_runtime_diagnostic_registry(),
        audit_registry=build_agent_runtime_audit_registry(),
        audit_writer=RecordingAuditWriter(),
        clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
    )

    await emitter.observe_retry(
        state=AgentRetryDiagnosticState.SCHEDULED,
        attempt_number=2,
        retry_reason_code=AgentRetryReason.PROVIDER_UNAVAILABLE,
    )
    await emitter.observe_tool(
        tool_call_id=uid(60),
        state=AgentToolDiagnosticState.SUCCEEDED,
    )
    await emitter.observe_usage(kind=AgentUsageKind.INPUT_TOKENS, quantity=128)

    assert [event.event_type for event in diagnostics.events] == [
        "agent.retry",
        "agent.tool",
        "agent.usage",
    ]
    assert diagnostics.events[0].payload_json()["attempt_number"] == 2
    assert diagnostics.events[1].payload_json()["tool_call_id"] == str(uid(60))
    assert diagnostics.events[2].payload_json() == {
        "agent_run_id": str(uid(33)),
        "agent_version_id": str(uid(20)),
        "attempt_id": str(uid(34)),
        "capability_id": str(uid(10)),
        "quantity": 128,
        "step_run_id": str(uid(32)),
        "usage_kind": "input_tokens",
        "work_item_id": str(uid(30)),
        "workflow_run_id": str(uid(31)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_value", DIAGNOSTIC_LEAK_CORPUS)
async def test_retry_observation_rejects_unregistered_failure_detail(
    protected_value: str,
) -> None:
    diagnostics = FakeEventSink()
    context = execution_context(event_sink=diagnostics, attempt_id=uid(34))
    emitter = AgentRuntimeDiagnosticEmitter(
        context=context,
        diagnostic_registry=build_agent_runtime_diagnostic_registry(),
        audit_registry=build_agent_runtime_audit_registry(),
        audit_writer=RecordingAuditWriter(),
        clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValidationError):
        await emitter.observe_retry(
            state=AgentRetryDiagnosticState.SCHEDULED,
            attempt_number=2,
            retry_reason_code=protected_value,  # type: ignore[arg-type]
        )

    assert diagnostics.events == []


@pytest.mark.asyncio
async def test_distinct_attempts_have_distinct_audit_deduplication_identity() -> None:
    first_audit = RecordingAuditWriter()
    second_audit = RecordingAuditWriter()
    for attempt_id, audit in ((uid(34), first_audit), (uid(35), second_audit)):
        context = execution_context(event_sink=FakeEventSink(), attempt_id=attempt_id)
        emitter = AgentRuntimeDiagnosticEmitter(
            context=context,
            diagnostic_registry=build_agent_runtime_diagnostic_registry(),
            audit_registry=build_agent_runtime_audit_registry(),
            audit_writer=audit,
            clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
        )
        await emitter.observe_run(AgentRunDiagnosticState.STARTED)

    assert (
        first_audit.events[0].producer_deduplication_id
        != second_audit.events[0].producer_deduplication_id
    )


@pytest.mark.asyncio
async def test_non_response_handler_result_is_rejected() -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, RecordingHandler(object())).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_handler_response_with_other_schema_is_rejected() -> None:
    other_capability = capability().model_copy(
        update={"output_schema": SchemaRef(name="answer_question.output", version="future")}
    )
    response = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(other_capability),
        output=OutputModel(answer="42"),
    )
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, RecordingHandler(response)).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_explicit_handler_error_is_preserved_without_persistence() -> None:
    response = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        error=StructuredError(
            code="spine.request.rejected",
            safe_message="The request was rejected.",
            retryability=Retryability.NEVER,
            trace_id=uid(40),
        ),
    )
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, RecordingHandler(response)).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert result.trace_ref == str(uid(40))
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_failed_persistence_cannot_appear_as_success() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact()},
        write_error=True,
    )

    result = await local_runtime(persistence, RecordingHandler()).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output_artifact_ids == ()
    assert result.error == "Persistence operation failed."


@pytest.mark.asyncio
async def test_completion_audit_failure_rolls_back_artifact_effect() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact()},
        audit_error=True,
    )
    audit = RecordingAuditWriter()

    result = await local_runtime(
        persistence,
        RecordingHandler(),
        audit_writer=audit,
    ).invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.output_artifact_ids == ()
    assert result.error == "Persistence operation failed."
    assert persistence.writes == []
    assert persistence.audit_calls[0].payload_json()["run_state"] == "completed"
    assert [event.payload_json()["run_state"] for event in audit.events] == [
        "started",
        "failed",
    ]


@pytest.mark.asyncio
async def test_invalid_persistence_receipt_cannot_appear_as_success() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact()},
        returned_ref=ArtifactRef(id=uid(70), schema_name="other", schema_version="1"),
    )

    audit = RecordingAuditWriter()
    result = await local_runtime(
        persistence,
        RecordingHandler(),
        audit_writer=audit,
    ).invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.output_artifact_ids == ()
    assert result.error == "Persistence operation failed."
    assert persistence.writes == []
    assert persistence.audit_calls == []
    assert persistence.validation_calls == 1
    assert [event.payload_json()["run_state"] for event in audit.events] == [
        "started",
        "failed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected_version", "selected_invocation"),
    (
        (version().model_copy(update={"id": uid(99)}), invocation()),
        (version(), invocation(work_item_id=uid(99))),
        (version(), invocation(step_run_id=uid(99))),
        (version(), invocation(capability="other")),
        (version(), invocation(idempotency_key="other")),
        (version(), invocation(input_artifact_ids=(uid(51),))),
    ),
)
async def test_cross_scope_invocation_fails_before_artifact_loading(
    selected_version: AgentVersion,
    selected_invocation: AgentInvocation,
) -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    handler = RecordingHandler()

    result = await local_runtime(persistence, handler).invoke(
        selected_version,
        selected_invocation,
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
    assert persistence.load_calls == []
    assert handler.calls == []


@pytest.mark.asyncio
async def test_equivalent_repeat_replays_one_stable_result_without_side_effects() -> None:
    diagnostics = FakeEventSink()
    audit = RecordingAuditWriter()
    first_context = execution_context(event_sink=diagnostics, attempt_id=uid(34))
    replay_context = execution_context(event_sink=diagnostics, attempt_id=uid(35))
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    first_handler = RecordingHandler()
    second_handler = RecordingHandler()

    first = await local_runtime(
        persistence, first_handler, context=first_context, audit_writer=audit
    ).invoke(version(), invocation())
    second = await local_runtime(
        persistence, second_handler, context=replay_context, audit_writer=audit
    ).invoke(version(), invocation())

    assert second == first
    assert len(first_handler.calls) == 1
    assert second_handler.calls == []
    assert len(persistence.writes) == 1
    assert [event.event_type for event in diagnostics.events] == [
        "agent.run",
        "agent.run",
        "agent.run",
        "agent.replay",
    ]
    assert [event.payload_json().get("run_state") for event in diagnostics.events] == [
        "started",
        "completed",
        "started",
        None,
    ]
    assert [event.event_type for event in audit.events] == [
        "agent.run",
        "agent.run",
        "agent.replay",
    ]
    assert [event.payload_json().get("run_state") for event in audit.events] == [
        "started",
        "started",
        None,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_invocation",
    (
        invocation(constraints={"max_sources": 4}),
        invocation(context_request={"profile": "other"}),
    ),
    ids=("constraints", "context-request"),
)
async def test_same_key_with_different_semantics_returns_safe_conflict(
    second_invocation: AgentInvocation,
) -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    handler = RecordingHandler()
    runtime = local_runtime(persistence, handler)
    first = await runtime.invoke(version(), invocation())

    conflict = await runtime.invoke(version(), second_invocation)

    assert first.status is AgentRunStatus.SUCCEEDED
    assert conflict.status is AgentRunStatus.FAILED
    assert conflict.error == "The idempotency key conflicts with an existing command."
    assert conflict.output_artifact_ids == ()
    assert len(handler.calls) == 1
    assert len(persistence.writes) == 1


@pytest.mark.asyncio
async def test_same_key_with_different_validated_input_returns_safe_conflict() -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    handler = RecordingHandler()
    runtime = local_runtime(persistence, handler)
    await runtime.invoke(version(), invocation())
    persistence.records[uid(50)] = input_artifact(
        content={"question": "A different question"}
    )

    conflict = await runtime.invoke(version(), invocation())

    assert conflict.status is AgentRunStatus.FAILED
    assert conflict.error == "The idempotency key conflicts with an existing command."
    assert len(handler.calls) == 1
    assert len(persistence.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_kind",
    (
        "capability",
        "version",
        "run",
        "acting-on-behalf-of",
        "authorization-generation",
        "deadline",
    ),
)
async def test_same_key_with_different_runtime_identity_returns_safe_conflict(
    identity_kind: str,
) -> None:
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})
    first_handler = RecordingHandler()
    await local_runtime(persistence, first_handler).invoke(version(), invocation())

    selected_version = version()
    context_overrides: dict[str, object] = {}
    if identity_kind == "capability":
        selected_capability = capability().model_copy(
            update={"id": uid(11)}
        )
        context_overrides.update(
            {
                "capability": selected_capability,
                "schema_identity": CapabilitySchemaIdentity.from_capability(
                    selected_capability
                ),
            }
        )
    elif identity_kind == "version":
        selected_version = version().model_copy(
            update={"id": uid(22), "version": "2026.10.2"}
        )
        context_overrides["agent_version"] = selected_version
    elif identity_kind == "run":
        context_overrides["workflow_run_id"] = uid(35)
    elif identity_kind == "authorization-generation":
        context_overrides["trusted_identity"] = (
            TrustedExecutionIdentitySnapshot(
                workspace_id=uid(1),
                environment_id=uid(2),
                acting_subject_id=uid(3),
                authorization_generation=2,
            )
        )
    elif identity_kind == "deadline":
        context_overrides["deadline"] = datetime(2026, 10, 12, tzinfo=timezone.utc)
    second_context = execution_context(**context_overrides)
    second_handler = RecordingHandler()
    selected_invocation = invocation()
    if identity_kind == "acting-on-behalf-of":
        selected_invocation = invocation(
            acting_on_behalf_of=ActorRef(kind=ActorKind.HUMAN, id=uid(4))
        )

    conflict = await local_runtime(
        persistence,
        second_handler,
        context=second_context,
    ).invoke(selected_version, selected_invocation)

    assert conflict.status is AgentRunStatus.FAILED
    assert conflict.error == "The idempotency key conflicts with an existing command."
    assert second_handler.calls == []
    assert len(persistence.writes) == 1


@pytest.mark.asyncio
async def test_temporal_style_reexecution_recovers_without_duplicate_committed_effect() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact()},
        completion_error_after_commit=True,
    )
    handlers = (RecordingHandler(), RecordingHandler())
    temporal = FakeTemporalActivityReexecutor()

    async def activity(attempt_number: int) -> AgentResult:
        return await local_runtime(
            persistence,
            handlers[attempt_number - 1],
            context=execution_context(attempt_id=uid(33 + attempt_number)),
        ).invoke(version(), invocation())

    ambiguous, recovered = await temporal.run(activity)

    assert ambiguous.status is AgentRunStatus.FAILED
    assert ambiguous.error == "Persistence operation failed."
    assert recovered.status is AgentRunStatus.SUCCEEDED
    assert recovered.output_artifact_ids == (uid(70),)
    assert temporal.attempt_numbers == [1, 2]
    assert len(handlers[0].calls) == 1
    assert handlers[1].calls == []
    assert len(persistence.writes) == 1


@pytest.mark.asyncio
async def test_lost_checkpoint_ack_recovers_without_handler_repeat() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact()},
        checkpoint_completion_error_after_commit=True,
    )
    handler = RecordingHandler()
    runtime = local_runtime(persistence, handler)

    ambiguous = await runtime.invoke(version(), invocation())
    recovered = await runtime.invoke(version(), invocation())

    assert ambiguous.status is AgentRunStatus.FAILED
    assert ambiguous.error == "Persistence operation failed."
    assert recovered.status is AgentRunStatus.SUCCEEDED
    assert recovered.output_artifact_ids == (uid(70),)
    assert len(handler.calls) == 1
    assert len(persistence.writes) == 1


@pytest.mark.asyncio
async def test_artifact_failure_retry_creates_one_logical_output() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact()},
        write_error=True,
    )
    handler = RecordingHandler()
    runtime = local_runtime(persistence, handler)

    failed = await runtime.invoke(version(), invocation())
    persistence.write_error = False
    recovered = await runtime.invoke(version(), invocation())

    assert failed.status is AgentRunStatus.FAILED
    assert recovered.status is AgentRunStatus.SUCCEEDED
    assert len(handler.calls) == 1
    assert len(persistence.writes) == 1
    assert recovered.output_artifact_ids == (uid(70),)


@pytest.mark.asyncio
async def test_same_raw_key_is_isolated_between_two_tenants() -> None:
    persistence = FakeArtifactPersistence(
        records={
            uid(50): input_artifact(),
            uid(51): input_artifact(
                id=uid(51),
                workspace_id=uid(101),
                environment_id=uid(102),
                content={"question": "Tenant two question"},
            ),
        }
    )
    first_handler = RecordingHandler()
    first_runtime = local_runtime(persistence, first_handler)
    first = await first_runtime.invoke(version(), invocation())

    second_context = execution_context(
        workspace_id=uid(101),
        environment_id=uid(102),
        cancellation=cooperative_cancellation(
            workspace_id=uid(101),
            environment_id=uid(102),
        ),
        trusted_identity=TrustedExecutionIdentitySnapshot(
            workspace_id=uid(101),
            environment_id=uid(102),
            acting_subject_id=uid(103),
            authorization_generation=1,
        ),
        input_artifact_ids=(uid(51),),
    )
    second_handler = RecordingHandler()
    persistence.returned_ref = ArtifactRef(
        id=uid(71),
        schema_name="answer_question.output",
        schema_version="2",
    )
    second = await local_runtime(
        persistence,
        second_handler,
        context=second_context,
    ).invoke(version(), invocation(input_artifact_ids=(uid(51),)))

    assert first.output_artifact_ids == (uid(70),)
    assert second.output_artifact_ids == (uid(71),)
    assert len(first_handler.calls) == 1
    assert len(second_handler.calls) == 1
    assert len(persistence.receipts) == 2
