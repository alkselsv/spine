"""Typed, versioned transactional outbox intent contracts."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Protocol, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticSerializationError

from spine.application.persistence.context import EnvironmentScope, PersistenceScope
from spine.application.persistence.errors import (
    InvalidPersistenceContextError,
    PersistenceError,
)


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_IMMUTABLE_SCALARS = (
    NoneType,
    str,
    bytes,
    int,
    float,
    bool,
    Decimal,
    UUID,
    date,
    datetime,
    time,
    timedelta,
)


class UnsupportedOutboxEventError(PersistenceError):
    """An event type/version or payload schema is not allowlisted."""


def _require_identifier(value: str, *, field_name: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier.")
    return value


def _is_deeply_immutable_value(value: object) -> bool:
    if isinstance(value, Enum):
        return _is_deeply_immutable_value(value.value)
    if isinstance(value, _IMMUTABLE_SCALARS):
        return True
    if isinstance(value, (tuple, frozenset)):
        return all(_is_deeply_immutable_value(item) for item in value)
    return False


def _is_deeply_immutable_annotation(
    annotation: object,
    *,
    checked_models: set[type[BaseModel]],
) -> bool:
    if annotation is Any:
        return False
    if annotation in _IMMUTABLE_SCALARS:
        return True
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return all(
                _is_deeply_immutable_value(member.value)
                for member in annotation.__members__.values()
            )
        if issubclass(annotation, BaseModel):
            if annotation in checked_models:
                return True
            if annotation.model_config.get("frozen") is not True:
                return False
            checked_models.add(annotation)
            return all(
                _is_deeply_immutable_annotation(
                    field.annotation,
                    checked_models=checked_models,
                )
                for field in annotation.model_fields.values()
            )

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, UnionType):
        return all(
            _is_deeply_immutable_annotation(item, checked_models=checked_models)
            for item in arguments
        )
    if origin is Annotated:
        return bool(arguments) and _is_deeply_immutable_annotation(
            arguments[0],
            checked_models=checked_models,
        )
    if origin is Literal:
        return all(isinstance(item, _IMMUTABLE_SCALARS) for item in arguments)
    if origin in (tuple, frozenset):
        return all(
            item is Ellipsis
            or _is_deeply_immutable_annotation(item, checked_models=checked_models)
            for item in arguments
        )
    return False


def _require_deeply_immutable_payload_type(
    payload_type: type[BaseModel],
) -> None:
    if not _is_deeply_immutable_annotation(
        payload_type,
        checked_models=set(),
    ):
        raise ValueError(
            "Outbox payload schemas must contain only deeply immutable fields."
        )


class OpaqueObjectReference(BaseModel):
    """Non-content-bearing reference to one versioned canonical object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object_type: str
    object_id: UUID
    schema_version: int

    @field_validator("object_type")
    @classmethod
    def validate_object_type(cls, value: str) -> str:
        return _require_identifier(value, field_name="object_type")

    @field_validator("object_id")
    @classmethod
    def validate_object_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("object_id must be a non-zero UUID.")
        return value

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("schema_version must be a positive integer.")
        return value


class OutboxIntent(BaseModel):
    """Immutable event intent accepted only through an event registry."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    event_id: UUID | None = None
    workspace_id: UUID
    environment_id: UUID | None = None
    event_type: str
    schema_version: int
    payload: BaseModel
    payload_snapshot: bytes | None = Field(default=None, exclude=True, repr=False)
    aggregate: OpaqueObjectReference | None = None
    producer_deduplication_id: str | None = None
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    @field_validator("workspace_id", "trace_id")
    @classmethod
    def validate_required_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("identity must be a non-zero UUID.")
        return value

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("event_id must be a non-zero UUID.")
        return value

    @field_validator("environment_id", "correlation_id", "causation_id")
    @classmethod
    def validate_optional_id(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("identity must be a non-zero UUID.")
        return value

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: str) -> str:
        return _require_identifier(value, field_name="event_type")

    @field_validator("schema_version")
    @classmethod
    def validate_event_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("schema_version must be a positive integer.")
        return value

    @field_validator("producer_deduplication_id")
    @classmethod
    def validate_deduplication_id(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 255):
            raise ValueError(
                "producer_deduplication_id must be a non-empty bounded string."
            )
        return value

    def payload_json(self) -> dict[str, Any]:
        """Return the registry-validated payload in JSON-compatible form."""

        if self.payload_snapshot is None:
            raise UnsupportedOutboxEventError(
                "Outbox intent payload has not been validated by the registry."
            )
        payload = json.loads(self.payload_snapshot)
        if not isinstance(payload, dict):
            raise UnsupportedOutboxEventError(
                "Outbox payload serialization must be a JSON object."
            )
        return payload


class OutboxEventRegistry:
    """Explicit allowlist of producer-owned event payload schemas."""

    def __init__(self) -> None:
        self._payload_types: dict[tuple[str, int], type[BaseModel]] = {}

    def register(
        self,
        *,
        event_type: str,
        schema_version: int,
        payload_type: type[BaseModel],
    ) -> None:
        event_type = _require_identifier(event_type, field_name="event_type")
        if isinstance(schema_version, bool) or schema_version <= 0:
            raise ValueError("schema_version must be a positive integer.")
        if not isinstance(payload_type, type) or not issubclass(payload_type, BaseModel):
            raise TypeError("payload_type must be a Pydantic model type.")
        if payload_type.model_config.get("extra") != "forbid":
            raise ValueError('Outbox payload schemas must configure extra="forbid".')
        if payload_type.model_config.get("frozen") is not True:
            raise ValueError("Outbox payload schemas must be frozen.")
        _require_deeply_immutable_payload_type(payload_type)
        key = (event_type, schema_version)
        existing = self._payload_types.get(key)
        if existing is not None and existing is not payload_type:
            raise ValueError("Outbox event type and schema version are already registered.")
        self._payload_types[key] = payload_type

    def resolve(self, event_type: str, schema_version: int) -> type[BaseModel]:
        payload_type = self._payload_types.get((event_type, schema_version))
        if payload_type is None:
            raise UnsupportedOutboxEventError(
                "Outbox event type and schema version are not registered."
            )
        return payload_type

    def validate(self, intent: OutboxIntent) -> OutboxIntent:
        expected = self.resolve(intent.event_type, intent.schema_version)
        if type(intent.payload) is not expected:
            raise UnsupportedOutboxEventError(
                "Outbox payload schema does not match the registered event."
            )
        try:
            canonical_payload = expected.model_validate(
                intent.payload.model_dump(
                    mode="python",
                    round_trip=True,
                    warnings=False,
                ),
                strict=True,
            )
            canonical_aggregate = (
                OpaqueObjectReference.model_validate(
                    intent.aggregate.model_dump(
                        mode="python",
                        round_trip=True,
                        warnings=False,
                    ),
                    strict=True,
                )
                if intent.aggregate is not None
                else None
            )
            serialized_payload = canonical_payload.model_dump(mode="json")
            if not isinstance(serialized_payload, dict):
                raise ValueError("Outbox payload serialization must be a JSON object.")
            payload_snapshot = json.dumps(
                serialized_payload,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            canonical = OutboxIntent(
                event_id=intent.event_id,
                workspace_id=intent.workspace_id,
                environment_id=intent.environment_id,
                event_type=intent.event_type,
                schema_version=intent.schema_version,
                payload=canonical_payload,
                payload_snapshot=payload_snapshot,
                aggregate=canonical_aggregate,
                producer_deduplication_id=intent.producer_deduplication_id,
                trace_id=intent.trace_id,
                correlation_id=intent.correlation_id,
                causation_id=intent.causation_id,
            )
        except (PydanticSerializationError, TypeError, ValidationError, ValueError):
            raise UnsupportedOutboxEventError(
                "Outbox intent values do not match the registered schema."
            ) from None
        return canonical

    def build_intent(
        self,
        *,
        workspace_id: UUID,
        event_type: str,
        schema_version: int,
        payload: BaseModel,
        trace_id: UUID,
        environment_id: UUID | None = None,
        aggregate: OpaqueObjectReference | None = None,
        producer_deduplication_id: str | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        event_id: UUID | None = None,
    ) -> OutboxIntent:
        intent = OutboxIntent(
            event_id=event_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            event_type=event_type,
            schema_version=schema_version,
            payload=payload,
            aggregate=aggregate,
            producer_deduplication_id=producer_deduplication_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return self.validate(intent)


def validate_outbox_intent_for_context(
    intent: OutboxIntent,
    *,
    registry: OutboxEventRegistry,
    scope: PersistenceScope,
    trace_id: UUID,
) -> OutboxIntent:
    """Validate the adapter-independent allowlist, scope, and lineage contract."""

    if type(intent) is not OutboxIntent:
        raise InvalidPersistenceContextError("Outbox intent is invalid.")
    canonical = registry.validate(intent)
    expected_environment = (
        scope.environment_id if isinstance(scope, EnvironmentScope) else None
    )
    if (
        canonical.workspace_id != scope.workspace_id
        or canonical.environment_id != expected_environment
    ):
        raise InvalidPersistenceContextError(
            "Outbox intent scope does not match Unit of Work scope."
        )
    if canonical.trace_id != trace_id:
        raise InvalidPersistenceContextError(
            "Outbox intent trace does not match Unit of Work trace."
        )
    return canonical


class OutboxWriter(Protocol):
    async def append(self, intent: OutboxIntent) -> UUID: ...


__all__ = [
    "OpaqueObjectReference",
    "OutboxEventRegistry",
    "OutboxIntent",
    "OutboxWriter",
    "UnsupportedOutboxEventError",
    "validate_outbox_intent_for_context",
]
