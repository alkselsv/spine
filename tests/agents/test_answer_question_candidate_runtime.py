from __future__ import annotations

from typing import Any

import pytest

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentRequest,
    AgentResponse,
    CapabilitySchemaIdentity,
    LocalAgentRuntime,
)
from spine.application.answer_question_candidate import (
    answer_question_candidate_artifact_schema,
)
from spine.domain.agents import AgentRunStatus
from spine.domain.artifacts import Artifact, ArtifactRef
from spine.domain.common import ActorKind, ActorRef, SchemaRef

from tests.agents.test_local_runtime import (
    FakeArtifactPersistence,
    _CANCELLATION_CONTROLLERS,
    execution_context,
    invocation,
    observability_dependencies,
    uid,
    version,
)
from tests.domain.capabilities.test_answer_question_candidate import (
    candidate_input,
    candidate_output,
)


class CandidateHandler:
    def __init__(
        self,
        output: object,
        evidence_refs: tuple[str, ...] = ("evidence-1",),
    ) -> None:
        self.output = output
        self.evidence_refs = evidence_refs
        self.calls = 0

    async def invoke(
        self,
        request: AgentRequest[Any],
        context: AgentExecutionContext,
    ) -> AgentResponse[Any]:
        del request
        self.calls += 1
        return AgentResponse(
            schema_identity=CapabilitySchemaIdentity.from_capability(context.capability),
            output=self.output,
            evidence_refs=self.evidence_refs,
        )


def input_artifact(context: AgentExecutionContext) -> Artifact:
    return Artifact(
        id=uid(50),
        workspace_id=context.workspace_id,
        environment_id=context.environment_id,
        work_item_id=context.work_item_id,
        produced_by=ActorRef(kind=ActorKind.HUMAN, id=uid(3)),
        artifact_schema=context.capability.input_schema,
        content=candidate_input().model_dump(mode="python"),
    )


def candidate_context() -> AgentExecutionContext:
    base = execution_context()
    capability = base.capability.model_copy(
        update={
            "input_schema": SchemaRef(
                name="answer-question.candidate-input", version="v1"
            ),
            "output_schema": SchemaRef(
                name="answer-question.candidate-output", version="v1"
            ),
        }
    )
    return execution_context(
        capability=capability,
        schema_identity=CapabilitySchemaIdentity.from_capability(capability),
    )


@pytest.mark.asyncio
async def test_local_runtime_rejects_candidate_before_output_checkpoint() -> None:
    context = candidate_context()
    schema = answer_question_candidate_artifact_schema(context.capability)
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact(context)},
        returned_ref=ArtifactRef(
            id=uid(70),
            schema_name=context.capability.output_schema.name,
            schema_version=context.capability.output_schema.version,
        ),
    )
    runtime = LocalAgentRuntime(
        context=context,
        handler=CandidateHandler(candidate_output(), evidence_refs=("other-evidence",)),
        artifacts=persistence,
        schemas=schema,
        cancellation_controller=_CANCELLATION_CONTROLLERS[id(context.cancellation)],
        **observability_dependencies(),
    )

    result = await runtime.invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_local_runtime_rejects_untyped_or_source_free_candidate_output() -> None:
    context = candidate_context()
    schema = answer_question_candidate_artifact_schema(context.capability)
    malformed = candidate_output().model_dump(mode="python")
    malformed["answer"]["citations"] = ()
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact(context)},
        returned_ref=ArtifactRef(
            id=uid(70),
            schema_name=context.capability.output_schema.name,
            schema_version=context.capability.output_schema.version,
        ),
    )
    runtime = LocalAgentRuntime(
        context=context,
        handler=CandidateHandler(malformed),
        artifacts=persistence,
        schemas=schema,
        cancellation_controller=_CANCELLATION_CONTROLLERS[id(context.cancellation)],
        **observability_dependencies(),
    )

    result = await runtime.invoke(version(), invocation())

    assert result.status is AgentRunStatus.FAILED
    assert result.output_artifact_ids == ()
    assert persistence.writes == []


@pytest.mark.asyncio
async def test_local_runtime_persists_a_valid_typed_candidate_once() -> None:
    context = candidate_context()
    schema = answer_question_candidate_artifact_schema(context.capability)
    persistence = FakeArtifactPersistence(
        records={uid(50): input_artifact(context)},
        returned_ref=ArtifactRef(
            id=uid(70),
            schema_name=context.capability.output_schema.name,
            schema_version=context.capability.output_schema.version,
        ),
    )
    handler = CandidateHandler(candidate_output())
    runtime = LocalAgentRuntime(
        context=context,
        handler=handler,
        artifacts=persistence,
        schemas=schema,
        cancellation_controller=_CANCELLATION_CONTROLLERS[id(context.cancellation)],
        **observability_dependencies(),
    )

    result = await runtime.invoke(version(), invocation())

    assert result.status is AgentRunStatus.SUCCEEDED
    assert len(persistence.writes) == 1
    replay = await runtime.invoke(version(), invocation())

    assert replay == result
    assert handler.calls == 1
    assert persistence.writes[0][0][0].content["schema_version"] == (
        "answer-question.candidate-output:v1"
    )
