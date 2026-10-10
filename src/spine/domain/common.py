"""Shared domain primitives."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FrozenDict(dict[str, Any]):
    """A JSON-serializable mapping that rejects ordinary mutation."""

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        source = {} if values is None else values
        dict.__init__(self, ((key, _freeze_value(value)) for key, value in source.items()))

    def __copy__(self) -> FrozenDict:
        return type(self)(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenDict:
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        copied = type(self)()
        memo[id(self)] = copied
        dict.__init__(
            copied,
            ((deepcopy(key, memo), deepcopy(value, memo)) for key, value in self.items()),
        )
        return copied

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


class ValidatedDefinitionModel(DefinitionModel):
    """Definition model whose public copy shortcuts preserve validation.

    Most definitions only need Pydantic's immutable configuration. Definitions
    containing recursively frozen values also need ``model_copy`` and the
    deprecated ``copy`` path to re-run field validators, because Pydantic's
    default copy methods intentionally bypass validation. The explicit
    ``model_construct`` shortcut is disabled for the same narrow set of
    models.
    """

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> ValidatedDefinitionModel:
        values = deepcopy(self.__dict__) if deep else dict(self.__dict__)
        if update:
            values.update(update)
        return type(self).model_validate(values)

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> ValidatedDefinitionModel:
        values = deepcopy(self.__dict__) if deep else dict(self.__dict__)
        if include is not None:
            values = {key: value for key, value in values.items() if key in include}
        if exclude is not None:
            values = {key: value for key, value in values.items() if key not in exclude}
        if update:
            values.update(update)
        return type(self).model_validate(values)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Any:
        """Prevent explicitly unvalidated construction for these definitions."""

        raise TypeError(f"{cls.__name__}.model_construct is not supported")


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


class SchemaRef(ValidatedDefinitionModel):
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
