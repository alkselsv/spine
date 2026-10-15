from __future__ import annotations

from uuid import UUID

import pytest

from spine.agents.runtime import AgentResponse
from spine.application.answer_question_candidate import (
    answer_question_candidate_artifact_schema,
    generate_answer_question_candidate,
)
from spine.domain.agents import AgentRunStatus
from spine.domain.artifacts import Artifact
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import ActorKind, ActorRef, SchemaRef
from spine.domain.agents import AgentRetrySafety

from tests.domain.capabilities.test_answer_question_candidate import (
    candidate_input,
    candidate_output,
)


WORK_ITEM_ID = UUID(int=800)


def capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        id=UUID(int=801),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(name="answer-question.candidate-input", version="v1"),
        output_schema=SchemaRef(name="answer-question.candidate-output", version="v1"),
    )


def input_artifact() -> Artifact:
    selected = capability()
    return Artifact(
        id=UUID(int=802),
        workspace_id=candidate_input().context_bundle.workspace_id,
        environment_id=candidate_input().context_bundle.environment_id,
        work_item_id=WORK_ITEM_ID,
        produced_by=ActorRef(kind=ActorKind.HUMAN, id=UUID(int=803)),
        artifact_schema=selected.input_schema,
        content=candidate_input().model_dump(mode="python"),
    )


def test_candidate_mapper_accepts_only_one_exact_context_bundle() -> None:
    selected = capability()
    schema = answer_question_candidate_artifact_schema(selected)
    typed_input = schema.load_input((input_artifact(),))
    output = candidate_output()
    response = AgentResponse(
        schema_identity=schema.schema_identity,
        output=output,
        evidence_refs=("evidence-1",),
    )

    schema.check_metadata(response)
    schema.check_handoff(typed_input, schema.load_output(output), response)


def test_candidate_mapper_rejects_multiple_input_artifacts() -> None:
    schema = answer_question_candidate_artifact_schema(capability())

    with pytest.raises(ValueError, match="exactly one"):
        schema.load_input((input_artifact(), input_artifact().model_copy(update={"id": UUID(int=804)})))


def test_candidate_mapper_rejects_evidence_outside_exact_bundle() -> None:
    schema = answer_question_candidate_artifact_schema(capability())
    typed_input = schema.load_input((input_artifact(),))
    response = AgentResponse(
        schema_identity=schema.schema_identity,
        output=candidate_output(),
        evidence_refs=("other-evidence",),
    )

    with pytest.raises(ValueError, match="metadata"):
        schema.check_handoff(typed_input, schema.load_output(response.output), response)


def test_candidate_mapper_rejects_non_answer_capability() -> None:
    invalid = capability().model_copy(update={"key": "other_capability"})

    with pytest.raises(ValueError, match="answer_question"):
        answer_question_candidate_artifact_schema(invalid)


@pytest.mark.asyncio
async def test_candidate_generation_delegates_to_pinned_runtime_as_repeatable_call() -> None:
    from tests.application.test_answer_question_execution import (
        boundary,
        invocation,
        request,
    )

    execution, runtime, _ = boundary()

    result = await generate_answer_question_candidate(
        execution,
        request(),
        invocation(),
    )

    assert result.result.status is AgentRunStatus.SUCCEEDED
    assert runtime.calls[0][1].retry_safety is AgentRetrySafety.SAFE_TO_REPEAT
