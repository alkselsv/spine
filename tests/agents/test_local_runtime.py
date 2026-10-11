from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentRetryDiagnosticState,
    AgentRetryReason,
    AgentRequest,
    AgentResponse,
    ArtifactDraft,
    ArtifactReadError,
    CapabilityArtifactSchema,
    CapabilitySchemaIdentity,
    AgentRuntimeAuditError,
    AgentRuntimeDiagnosticEmitter,
    AgentRunDiagnosticState,
    AgentToolDiagnosticState,
    AgentUsageKind,
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
from spine.domain.agents import (
    AgentInvocation,
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


class FakeCancellation:
    is_cancelled = False

    def raise_if_cancelled(self) -> None:
        return None


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


def version() -> AgentVersion:
    return AgentVersion(
        id=uid(20),
        agent_id=uid(21),
        version="2026.10.1",
        runtime=AgentRuntimeKind.CODE,
        capabilities=("answer_question",),
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
        "cancellation": FakeCancellation(),
        "event_sink": FakeEventSink(),
    }
    values.update(overrides)
    return AgentExecutionContext(**values)


def invocation(**overrides: object) -> AgentInvocation:
    values: dict[str, object] = {
        "work_item_id": uid(30),
        "step_run_id": uid(32),
        "capability": "answer_question",
        "acting_on_behalf_of": ActorRef(kind=ActorKind.HUMAN, id=uid(3)),
        "input_artifact_ids": (uid(50),),
        "constraints": {"max_sources": 3},
        "idempotency_key": "qa:30:1",
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
    returned_ref: ArtifactRef | None = None
    lineage_error_ids: set[UUID] = field(default_factory=set)
    load_calls: list[tuple[UUID, UUID, UUID, UUID]] = field(default_factory=list)
    lineage_calls: list[UUID] = field(default_factory=list)
    writes: list[tuple[tuple[ArtifactDraft, ...], str]] = field(default_factory=list)
    audit_calls: list[AuditEvent] = field(default_factory=list)
    validation_calls: int = 0

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

    async def persist_outputs(
        self,
        drafts: tuple[ArtifactDraft, ...],
        *,
        idempotency_key: str,
        audit_event: AuditEvent,
        validate_references: Any,
    ) -> tuple[ArtifactRef, ...]:
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
        self.writes.append((drafts, idempotency_key))
        return references


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


def local_runtime(
    persistence: FakeArtifactPersistence,
    handler: RecordingHandler,
    *,
    context: AgentExecutionContext | None = None,
    audit_writer: AuditWriter | None = None,
) -> LocalAgentRuntime:
    selected_context = context or execution_context()
    return LocalAgentRuntime(
        context=selected_context,
        handler=handler,
        artifacts=persistence,
        schemas=CapabilityArtifactSchema(
            schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
            input_model=InputModel,
            output_model=OutputModel,
            assemble_input=lambda artifacts: artifacts[0].content,
            validate_metadata=validate_metadata,
        ),
        diagnostic_registry=build_agent_runtime_diagnostic_registry(),
        audit_registry=build_agent_runtime_audit_registry(),
        audit_writer=audit_writer or RecordingAuditWriter(),
        clock=lambda: datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
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

    with pytest.raises(ValueError, match="schema mapping"):
        LocalAgentRuntime(
            context=execution_context(),
            handler=RecordingHandler(),
            artifacts=FakeArtifactPersistence(),
            schemas=CapabilityArtifactSchema(
                schema_identity=CapabilitySchemaIdentity.from_capability(incompatible),
                input_model=InputModel,
                output_model=OutputModel,
                assemble_input=lambda artifacts: artifacts[0].content,
                validate_metadata=validate_metadata,
            ),
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
async def test_incompatible_handler_output_is_not_persisted() -> None:
    response = AgentResponse(
        schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
        output={"answer": "42", "unexpected": "protected"},
    )
    handler = RecordingHandler(response)
    persistence = FakeArtifactPersistence(records={uid(50): input_artifact()})

    result = await local_runtime(persistence, handler).invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.error == "The request was rejected."
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
    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "started",
        "cancelled",
    ]
    assert [event.payload_json()["run_state"] for event in audit.events] == [
        "started",
        "cancelled",
    ]


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
    context = execution_context(event_sink=diagnostics, attempt_id=uid(34))
    runtime = local_runtime(
        FakeArtifactPersistence(),
        RecordingHandler(),
        context=context,
        audit_writer=audit,
    )

    await runtime.cancel(uid(32))

    assert [event.payload_json()["run_state"] for event in diagnostics.events] == [
        "cancellation_requested"
    ]
    assert audit.events == []


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
