from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentHandler,
    AgentRequest,
    AgentResponse,
    CapabilitySchemaIdentity,
    adapt_async_function,
)
from spine.application.diagnostics import DiagnosticContext
from spine.domain.agents import AgentResult, AgentRuntimeKind, AgentVersion
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import EnvironmentKind, SchemaRef


def uid(value: int) -> UUID:
    return UUID(int=value)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str


class OutputModel(BaseModel):
    answer: str


class OtherInput(BaseModel):
    value: int


@dataclass
class FakeTrustedIdentity:
    workspace_id: UUID = uid(1)
    environment_id: UUID = uid(2)
    acting_subject_id: UUID | None = uid(3)
    service_principal_id: UUID | None = None
    authorization_generation: int = 1


class FakeCancellation:
    is_cancelled = False

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise RuntimeError("cancelled")


class FakeEventSink:
    async def emit(self, event: object) -> None:
        del event


def capability(*, identity_seed: int = 10) -> CapabilityDefinition:
    return CapabilityDefinition(
        id=uid(identity_seed),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(name="answer_question.input", version="1"),
        output_schema=SchemaRef(name="answer_question.output", version="1"),
    )


def request() -> AgentRequest[InputModel]:
    selected = capability()
    return AgentRequest[InputModel](
        capability=selected,
        schema_identity=CapabilitySchemaIdentity.from_capability(selected),
        input=InputModel(question="What is the answer?"),
    )


def context() -> AgentExecutionContext:
    selected = capability()
    return AgentExecutionContext(
        workspace_id=uid(1),
        environment_id=uid(2),
        environment=EnvironmentKind.DEVELOPMENT,
        trusted_identity=FakeTrustedIdentity(),
        work_item_id=uid(30),
        workflow_run_id=uid(31),
        step_run_id=uid(32),
        agent_run_id=uid(33),
        capability=selected,
        schema_identity=CapabilitySchemaIdentity.from_capability(selected),
        agent_version=AgentVersion(
            id=uid(20),
            agent_id=uid(21),
            version="2026.10.1",
            runtime=AgentRuntimeKind.CODE,
            capabilities=(selected.key,),
        ),
        idempotency_key="qa:30:1",
        diagnostic_context=DiagnosticContext(trace_id=uid(40)),
        input_artifact_ids=(uid(50),),
        cancellation=FakeCancellation(),
        event_sink=FakeEventSink(),
    )


def formed_response(
    *,
    selected: CapabilityDefinition | None = None,
    answer: str = "42",
) -> AgentResponse[OutputModel]:
    selected = selected or capability()
    return AgentResponse[OutputModel](
        schema_identity=CapabilitySchemaIdentity.from_capability(selected),
        output=OutputModel(answer=answer),
        evidence_refs=("evidence-1",),
        confidence=0.9,
        structured_metrics={"tokens": 3.0},
        proposed_action_ids=(uid(60),),
    )


async def annotated_raw_handler(
    request: AgentRequest[InputModel],
    execution_context: AgentExecutionContext,
) -> OutputModel:
    del execution_context
    return OutputModel(answer=request.input.question)


async def annotated_response_handler(
    request: AgentRequest[InputModel],
    execution_context: AgentExecutionContext,
) -> AgentResponse[OutputModel]:
    del request, execution_context
    return formed_response()


@pytest.mark.asyncio
async def test_compatible_async_function_invokes_through_structural_handler() -> None:
    adapted = adapt_async_function(annotated_raw_handler)

    assert isinstance(adapted, AgentHandler)
    result = await adapted.invoke(request(), context())

    assert result.output == OutputModel(answer="What is the answer?")
    assert result.schema_identity == request().schema_identity


@pytest.mark.asyncio
async def test_adapter_passes_request_and_context_without_replacing_identity() -> None:
    seen: list[object] = []

    async def handler(
        received_request: AgentRequest[InputModel],
        received_context: AgentExecutionContext,
    ) -> OutputModel:
        seen.extend((received_request, received_context))
        return OutputModel(answer="ok")

    adapted = adapt_async_function(handler)
    original_request = request()
    original_context = context()

    await adapted.invoke(original_request, original_context)

    assert seen == [original_request, original_context]
    assert seen[0] is original_request
    assert seen[1] is original_context


@pytest.mark.asyncio
async def test_existing_response_is_preserved_when_schema_identity_matches() -> None:
    formed = formed_response(answer="preserve me")

    async def handler(
        received_request: AgentRequest[InputModel],
        received_context: AgentExecutionContext,
    ) -> AgentResponse[OutputModel]:
        del received_request, received_context
        return formed

    result = await adapt_async_function(handler).invoke(request(), context())

    assert result is formed
    assert result.output == OutputModel(answer="preserve me")
    assert result.evidence_refs == ("evidence-1",)
    assert result.confidence == 0.9
    assert result.structured_metrics == {"tokens": 3.0}
    assert result.proposed_action_ids == (uid(60),)


@pytest.mark.asyncio
async def test_raw_output_uses_request_schema_identity() -> None:
    async def handler(
        received_request: AgentRequest[InputModel],
        received_context: AgentExecutionContext,
    ) -> OutputModel:
        del received_request, received_context
        return OutputModel(answer="typed")

    original_request = request()
    result = await adapt_async_function(handler).invoke(original_request, context())

    assert result.schema_identity == original_request.schema_identity


@pytest.mark.asyncio
async def test_conflicting_response_schema_identity_is_rejected() -> None:
    async def handler(
        received_request: AgentRequest[InputModel],
        received_context: AgentExecutionContext,
    ) -> AgentResponse[OutputModel]:
        del received_request, received_context
        return formed_response(selected=capability(identity_seed=11))

    with pytest.raises(ValueError, match="schema identity"):
        await adapt_async_function(handler).invoke(request(), context())


@pytest.mark.asyncio
async def test_incorrect_raw_output_is_rejected_without_leaking_contents() -> None:
    async def handler(
        received_request: AgentRequest[InputModel],
        received_context: AgentExecutionContext,
    ) -> OutputModel:
        del received_request, received_context
        return "credential=do-not-leak"  # type: ignore[return-value]

    with pytest.raises(TypeError) as error:
        await adapt_async_function(handler).invoke(request(), context())

    assert "credential=do-not-leak" not in str(error.value)


@pytest.mark.parametrize(
    "factory",
    (
        lambda: (lambda request, context: OutputModel(answer="sync")),
        lambda: (lambda request, context: None),
    ),
)
def test_non_async_or_unannotated_functions_are_rejected_before_execution(factory) -> None:
    with pytest.raises(TypeError):
        adapt_async_function(factory())


async def malformed_one_parameter(request: AgentRequest[InputModel]) -> OutputModel:
    del request
    return OutputModel(answer="invalid")


async def malformed_keyword_only(
    request: AgentRequest[InputModel],
    *,
    execution_context: AgentExecutionContext,
) -> OutputModel:
    del request, execution_context
    return OutputModel(answer="invalid")


async def incompatible_input(
    request: AgentRequest[OtherInput],
    execution_context: AgentExecutionContext,
) -> OutputModel:
    del request, execution_context
    return OutputModel(answer="invalid")


async def unsupported_return(
    request: AgentRequest[InputModel],
    execution_context: AgentExecutionContext,
) -> AgentResult:
    del request, execution_context
    return AgentResult(status="succeeded")


@pytest.mark.parametrize(
    "function",
    (malformed_one_parameter, malformed_keyword_only, unsupported_return),
)
def test_malformed_signatures_and_return_shapes_are_rejected(function) -> None:
    with pytest.raises(TypeError):
        adapt_async_function(function)


def test_incompatible_declared_input_and_output_contracts_are_rejected() -> None:
    with pytest.raises(TypeError, match="input"):
        adapt_async_function(incompatible_input, input_type=InputModel)

    async def handler(
        request: AgentRequest[InputModel],
        execution_context: AgentExecutionContext,
    ) -> OutputModel:
        del request, execution_context
        return OutputModel(answer="invalid")

    with pytest.raises(TypeError, match="output"):
        adapt_async_function(handler, output_type=OtherInput)


@pytest.mark.asyncio
async def test_separate_invocations_do_not_share_adapter_state() -> None:
    async def handler(
        received_request: AgentRequest[InputModel],
        received_context: AgentExecutionContext,
    ) -> OutputModel:
        del received_context
        return OutputModel(answer=received_request.input.question)

    adapted = adapt_async_function(handler)
    first = request()
    second = request().model_copy(
        update={"input": InputModel(question="second question")}
    )

    first_result, second_result = await adapted.invoke(first, context()), await adapted.invoke(
        second, context()
    )

    assert first_result.output == OutputModel(answer="What is the answer?")
    assert second_result.output == OutputModel(answer="second question")


@pytest.mark.asyncio
async def test_handler_exceptions_propagate_without_adapter_diagnostics() -> None:
    async def handler(
        received_request: AgentRequest[InputModel],
        received_context: AgentExecutionContext,
    ) -> OutputModel:
        del received_request, received_context
        raise RuntimeError("protected document text and provider payload")

    with pytest.raises(RuntimeError, match="protected document text"):
        await adapt_async_function(handler).invoke(request(), context())
