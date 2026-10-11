from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentRequest,
    AgentResponse,
    ArtifactDraft,
    ArtifactReadError,
    CapabilityArtifactSchema,
    CapabilitySchemaIdentity,
    LocalAgentRuntime,
    TrustedExecutionIdentitySnapshot,
)
from spine.application.diagnostics import (
    DiagnosticContext,
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
    async def emit(self, event: object) -> None:
        del event


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
    write_error: bool = False
    returned_ref: ArtifactRef | None = None
    lineage_error_ids: set[UUID] = field(default_factory=set)
    load_calls: list[tuple[UUID, UUID, UUID, UUID]] = field(default_factory=list)
    lineage_calls: list[UUID] = field(default_factory=list)
    writes: list[tuple[tuple[ArtifactDraft, ...], str]] = field(default_factory=list)

    async def load(
        self,
        artifact_id: UUID,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        work_item_id: UUID,
    ) -> Artifact | None:
        self.load_calls.append((artifact_id, workspace_id, environment_id, work_item_id))
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
    ) -> tuple[ArtifactRef, ...]:
        self.writes.append((drafts, idempotency_key))
        if self.write_error:
            raise RuntimeError("database included protected output")
        return (
            self.returned_ref
            or ArtifactRef(
                id=uid(70),
                schema_name="answer_question.output",
                schema_version="2",
            ),
        )


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
    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        self.calls.append((request, context))
        raise RuntimeError("provider payload is protected")


def local_runtime(
    persistence: FakeArtifactPersistence,
    handler: RecordingHandler,
    *,
    context: AgentExecutionContext | None = None,
) -> LocalAgentRuntime:
    return LocalAgentRuntime(
        context=context or execution_context(),
        handler=handler,
        artifacts=persistence,
        schemas=CapabilityArtifactSchema(
            schema_identity=CapabilitySchemaIdentity.from_capability(capability()),
            input_model=InputModel,
            output_model=OutputModel,
            assemble_input=lambda artifacts: artifacts[0].content,
            validate_metadata=validate_metadata,
        ),
    )


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
async def test_invalid_persistence_receipt_cannot_appear_as_success() -> None:
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact()},
        returned_ref=ArtifactRef(id=uid(70), schema_name="other", schema_version="1"),
    )

    result = await local_runtime(persistence, RecordingHandler()).invoke(
        version(), invocation()
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.output_artifact_ids == ()
    assert result.error == "Persistence operation failed."


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
