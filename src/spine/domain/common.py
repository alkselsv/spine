"""Shared domain primitives."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class SpineModel(BaseModel):
    """Base model for runtime domain objects."""

    model_config = ConfigDict(extra="forbid")


class DefinitionModel(SpineModel):
    """Immutable, versionable configuration object."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvironmentKind(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class ActorKind(str, Enum):
    HUMAN = "human"
    TEAM = "team"
    AGENT = "agent"
    SERVICE = "service"


class ActorRef(DefinitionModel):
    """A first-class assignee or initiator of work."""

    kind: ActorKind
    id: UUID
    display_name: str | None = None


class SchemaRef(DefinitionModel):
    name: str
    version: str
    json_schema: dict[str, Any] = Field(default_factory=dict)


class EntityRef(DefinitionModel):
    type: str
    id: UUID = Field(default_factory=uuid4)
