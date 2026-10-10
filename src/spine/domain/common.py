"""Shared domain primitives."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FrozenDict(dict[str, Any]):
    """A JSON-serializable mapping that rejects ordinary mutation."""

    def __init__(self, values: Mapping[str, Any] = ()) -> None:
        dict.__init__(self, ((key, _freeze_value(value)) for key, value in values.items()))

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("FrozenDict is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable

    def __ior__(self, other: Mapping[str, Any]) -> FrozenDict:
        del other
        raise TypeError("FrozenDict is immutable")


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


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
    json_schema: Mapping[str, Any] = Field(default_factory=dict, validate_default=True)

    @field_validator("json_schema", mode="after")
    @classmethod
    def freeze_schema(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return FrozenDict(value)


class EntityRef(DefinitionModel):
    type: str
    id: UUID = Field(default_factory=uuid4)
