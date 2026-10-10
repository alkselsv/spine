from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from spine.domain.common import (
    ActorRef,
    DefinitionModel,
    EnvironmentKind,
    FrozenDict,
    ValidatedDefinitionModel,
)


class AgentRuntimeKind(str, Enum):
    LLM = "llm"
    CODE = "code"
    EXTERNAL = "external"


class DeploymentStage(str, Enum):
    DEVELOPMENT = "development"
    SHADOW = "shadow"
    CANARY = "canary"
    PRODUCTION = "production"
    DISABLED = "disabled"


class AgentDefinition(DefinitionModel):
    id: UUID = Field(default_factory=uuid4)
    key: str
    display_name: str
    owner: ActorRef
    description: str = ""


class AgentVersion(ValidatedDefinitionModel):
    id: UUID = Field(default_factory=uuid4)
    agent_id: UUID
    version: str
    runtime: AgentRuntimeKind
    capabilities: tuple[str, ...]
    model_profile: str | None = None
    prompt_version: str | None = None
    context_profile: str | None = None
    tool_grants: tuple[str, ...] = ()
    runtime_config: Mapping[str, Any] = Field(default_factory=dict, validate_default=True)

    @field_validator("runtime_config", mode="after")
    @classmethod
    def freeze_runtime_config(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return FrozenDict(value)


class AgentBinding(DefinitionModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    environment: EnvironmentKind
    capability: str
    agent_version_id: UUID
    priority: int = 100


class AgentDeployment(DefinitionModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    environment: EnvironmentKind
    agent_version_id: UUID
    stage: DeploymentStage
    traffic_percentage: int = Field(default=100, ge=0, le=100)
