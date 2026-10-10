from __future__ import annotations

from dataclasses import dataclass
import ast
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from spine.agents.runtime import (
    AgentExecutionContext,
    AgentHandler,
    AgentRequest,
    AgentResponse,
    CancellationToken,
    EventSink,
    TrustedExecutionIdentity,
    TrustedExecutionIdentitySnapshot,
    agent_request_from_invocation,
    agent_result_from_response,
)
from spine.application.diagnostics import DiagnosticContext, Retryability, StructuredError
from spine.domain.agents import (
    AgentInvocation,
    AgentResult,
    AgentRunStatus,
    AgentRuntimeKind,
    AgentVersion,
)
from spine.domain.capabilities import CapabilityDefinition
from spine.domain.common import ActorKind, ActorRef, EnvironmentKind, FrozenDict, SchemaRef


def uid(value: int) -> UUID:
    return UUID(int=value)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str


class OutputModel(BaseModel):
    answer: str


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
    async def emit(self, event: str) -> None:
        del event


def capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        id=uid(10),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(
            name="answer_question.input",
            version="1",
            json_schema={"nested": {"value": 1}},
        ),
        output_schema=SchemaRef(name="answer_question.output", version="1"),
    )


def version() -> AgentVersion:
    return AgentVersion(
        id=uid(20),
        agent_id=uid(21),
        version="2026.10.1",
        runtime=AgentRuntimeKind.CODE,
        capabilities=("answer_question",),
    )


def context(
    *,
    execution_version: AgentVersion | None = None,
    **overrides: object,
) -> AgentExecutionContext:
    values: dict[str, object] = {
        "workspace_id": uid(1),
        "environment_id": uid(2),
        "environment": EnvironmentKind.DEVELOPMENT,
        "trusted_identity": FakeTrustedIdentity(),
        "work_item_id": uid(30),
        "workflow_run_id": uid(31),
        "step_run_id": uid(32),
        "agent_run_id": uid(33),
        "capability": capability(),
        "agent_version": execution_version or version(),
        "idempotency_key": "qa:30:1",
        "diagnostic_context": DiagnosticContext(trace_id=uid(40)),
        "input_artifact_ids": (uid(50),),
        "context_profile": "qa",
        "permitted_tool_capabilities": ("documents.read",),
        "cancellation": FakeCancellation(),
        "event_sink": FakeEventSink(),
    }
    values["agent_version"] = execution_version or version()
    values.update(overrides)
    return AgentExecutionContext(**values)


def test_agent_request_and_response_preserve_generic_types() -> None:
    request = AgentRequest[InputModel](
        capability=capability(),
        input=InputModel(question="What is the answer?"),
        constraints={"max_sources": 3},
    )
    response = AgentResponse[OutputModel](output=OutputModel(answer="42"))

    assert isinstance(request.input, InputModel)
    assert isinstance(response.output, OutputModel)
    assert response.error is None
    assert isinstance(request.constraints, FrozenDict)
    assert isinstance(response.structured_metrics, FrozenDict)
    assert isinstance(request.capability.input_schema.json_schema, FrozenDict)
    assert isinstance(context().agent_version.runtime_config, FrozenDict)
    with pytest.raises(TypeError):
        request.constraints["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        request.capability.input_schema.json_schema["new"] = "value"  # type: ignore[index]
    assert isinstance(request.capability.input_schema.json_schema["nested"], FrozenDict)


def test_public_contracts_are_frozen_and_reject_extra_fields() -> None:
    request = AgentRequest[InputModel](capability=capability(), input=InputModel(question="q"))

    with pytest.raises(ValidationError):
        request.input = InputModel(question="changed")  # type: ignore[misc]
    with pytest.raises(ValidationError):
        AgentRequest[InputModel](
            capability=capability(),
            input=InputModel(question="q"),
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        AgentResponse[OutputModel](output=OutputModel(answer="42"), unexpected=True)
    with pytest.raises(ValidationError):
        request.model_copy(update={"input": {"question": "changed", "extra": True}})
    with pytest.raises(ValidationError):
        request.copy(update={"input": {"question": "changed", "extra": True}})
    with pytest.raises(TypeError):
        AgentRequest[InputModel].model_construct(input=InputModel(question="unvalidated"))


def test_response_rejects_mixed_output_and_structured_error() -> None:
    error = StructuredError(
        code="spine.internal.unexpected",
        safe_message="An unexpected internal error occurred.",
        retryability=Retryability.NEVER,
        trace_id=uid(60),
    )

    with pytest.raises(ValidationError):
        AgentResponse[OutputModel](output=OutputModel(answer="42"), error=error)
    assert AgentResponse[OutputModel](error=error).output is None


def test_runtime_contract_rejects_empty_schema_identity() -> None:
    invalid_capability = CapabilityDefinition(
        id=uid(10),
        key="answer_question",
        display_name="Answer question",
        input_schema=SchemaRef(name="", version="1"),
        output_schema=SchemaRef(name="answer_question.output", version="1"),
    )

    with pytest.raises(ValidationError, match="schema identity"):
        AgentRequest[InputModel](
            capability=invalid_capability,
            input=InputModel(question="q"),
        )


def test_execution_context_is_immutable_and_validates_scope_and_identity() -> None:
    execution_context = context()

    assert isinstance(execution_context.trusted_identity, TrustedExecutionIdentitySnapshot)
    original_workspace = execution_context.trusted_identity.workspace_id
    source_identity = FakeTrustedIdentity()
    source_identity.workspace_id = uid(99)  # type: ignore[misc]
    assert execution_context.trusted_identity.workspace_id == original_workspace

    with pytest.raises(ValidationError):
        execution_context.workspace_id = uid(99)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        context(workspace_id=uid(99))
    with pytest.raises(ValidationError):
        execution_context.model_copy(update={"workspace_id": uid(99)})
    with pytest.raises(TypeError):
        AgentExecutionContext.model_construct(workspace_id=uid(99))


def test_execution_context_rejects_inconsistent_capability_and_version() -> None:
    invalid_version = version().model_copy(update={"capabilities": ("other",)})

    with pytest.raises(ValidationError, match="capability"):
        context(execution_version=invalid_version)


def test_execution_context_rejects_invalid_agent_version_identity() -> None:
    invalid_version = version().model_copy(update={"agent_id": UUID(int=0)})

    with pytest.raises(ValidationError, match="agent version identity"):
        context(execution_version=invalid_version)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workspace_id", UUID(int=0)),
        ("environment_id", UUID(int=0)),
        ("work_item_id", UUID(int=0)),
        ("step_run_id", UUID(int=0)),
    ),
)
def test_execution_context_rejects_zero_run_identifiers(field: str, value: UUID) -> None:
    with pytest.raises(ValidationError):
        context(**{field: value})


def test_compatibility_mapping_rejects_capability_substitution() -> None:
    invocation = AgentInvocation(
        work_item_id=uid(30),
        step_run_id=uid(32),
        capability="different_capability",
        acting_on_behalf_of=ActorRef(kind=ActorKind.HUMAN, id=uid(3)),
        idempotency_key="qa:30:1",
    )

    with pytest.raises(ValueError, match="capability"):
        agent_request_from_invocation(
            invocation,
            capability=capability(),
            input=InputModel(question="q"),
        )


def test_ports_are_structural_protocols() -> None:
    assert isinstance(FakeCancellation(), CancellationToken)
    assert isinstance(FakeEventSink(), EventSink)

    class Handler:
        async def invoke(
            self,
            request: AgentRequest[InputModel],
            execution_context: AgentExecutionContext,
        ) -> AgentResponse[OutputModel]:
            del request, execution_context
            return AgentResponse(output=OutputModel(answer="42"))

    assert isinstance(Handler(), AgentHandler)


def test_runtime_contract_boundary_has_no_framework_imports() -> None:
    forbidden = {"agno", "cognee", "fastapi", "langchain", "langgraph", "sqlalchemy", "temporalio"}
    root = Path(__file__).parents[2] / "src" / "spine" / "agents" / "runtime"

    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not imported_roots & forbidden, path


def test_compatibility_mappings_preserve_legacy_invocation_and_result_envelopes() -> None:
    invocation = AgentInvocation(
        work_item_id=uid(30),
        step_run_id=uid(32),
        capability="answer_question",
        acting_on_behalf_of=ActorRef(kind=ActorKind.HUMAN, id=uid(3)),
        input_artifact_ids=(uid(50),),
        constraints={"max_sources": 3},
        idempotency_key="qa:30:1",
    )
    request = agent_request_from_invocation(
        invocation,
        capability=capability(),
        input=InputModel(question="q"),
    )
    response = AgentResponse(output=OutputModel(answer="42"))
    result = agent_result_from_response(response, output_artifact_ids=(uid(70),))

    assert request.capability.key == invocation.capability
    assert request.constraints == invocation.constraints
    assert isinstance(result, AgentResult)
    assert result.status is AgentRunStatus.SUCCEEDED
    assert result.output_artifact_ids == (uid(70),)


def test_compatibility_mapping_requires_persisted_outputs_for_success() -> None:
    with pytest.raises(ValueError, match="persisted output"):
        agent_result_from_response(AgentResponse(output=OutputModel(answer="42")))


def test_compatibility_mapping_preserves_safe_structured_error() -> None:
    error = StructuredError(
        code="spine.internal.unexpected",
        safe_message="An unexpected internal error occurred.",
        retryability=Retryability.NEVER,
        trace_id=uid(80),
    )

    result = agent_result_from_response(AgentResponse[OutputModel](error=error))

    assert result.status is AgentRunStatus.FAILED
    assert result.error == error.safe_message


def test_compatibility_mapping_preserves_existing_non_success_status() -> None:
    error = StructuredError(
        code="spine.internal.unexpected",
        safe_message="An unexpected internal error occurred.",
        retryability=Retryability.NEVER,
        trace_id=uid(81),
    )

    result = agent_result_from_response(
        AgentResponse[OutputModel](error=error),
        status=AgentRunStatus.CANCELLED,
    )

    assert result.status is AgentRunStatus.CANCELLED
