from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from spine.domain.common import ActorRef, DefinitionModel, SchemaRef


class StepKind(str, Enum):
    AGENT = "agent"
    CODE = "code"
    HUMAN = "human"
    GATE = "gate"
    SUBWORKFLOW = "subworkflow"


class StepDefinition(DefinitionModel):
    key: str
    title: str
    kind: StepKind
    depends_on: tuple[str, ...] = ()
    assignee: ActorRef | None = None
    capability: str | None = None
    input_schema: SchemaRef | None = None
    output_schema: SchemaRef | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_agent_capability(self) -> "StepDefinition":
        if self.kind is StepKind.AGENT and not self.capability:
            raise ValueError("agent steps require a capability")
        return self


class WorkflowDefinition(DefinitionModel):
    id: UUID = Field(default_factory=uuid4)
    key: str
    display_name: str
    owner: ActorRef
    description: str = ""


class WorkflowVersion(DefinitionModel):
    id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID
    version: str
    steps: tuple[StepDefinition, ...]
    trigger_schema: SchemaRef | None = None
    goal_id: UUID | None = None

    @model_validator(mode="after")
    def validate_step_graph(self) -> "WorkflowVersion":
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("workflow step keys must be unique")
        known = set(keys)
        for step in self.steps:
            missing = set(step.depends_on) - known
            if missing:
                raise ValueError(f"step {step.key!r} has unknown dependencies: {sorted(missing)}")
            if step.key in step.depends_on:
                raise ValueError(f"step {step.key!r} cannot depend on itself")

        dependencies = {step.key: set(step.depends_on) for step in self.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("workflow step graph must be acyclic")
            if key in visited:
                return
            visiting.add(key)
            for dependency in dependencies[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        return self
