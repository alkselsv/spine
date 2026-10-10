"""Typed, registry-validated non-canonical Diagnostic Events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    Protocol,
    Union,
    get_args,
    get_origin,
)
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticSerializationError

from spine.application.diagnostics.context import DiagnosticContext


DIAGNOSTIC_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.:-]{0,127}$"
_DIAGNOSTIC_IDENTIFIER = re.compile(DIAGNOSTIC_IDENTIFIER_PATTERN)
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
_FORBIDDEN_PAYLOAD_FIELD_TERMS = frozenset(
    {
        "answer",
        "api_key",
        "authorization",
        "body",
        "chain_of_thought",
        "connection_string",
        "content",
        "cookie",
        "credential",
        "document",
        "error",
        "exception",
        "excerpt",
        "filename",
        "message",
        "password",
        "prompt",
        "provider",
        "question",
        "raw",
        "secret",
        "sql",
        "stack",
        "text",
        "token",
        "traceback",
        "url",
    }
)
_ALWAYS_FORBIDDEN_STRING_FIELD_TERMS = frozenset(
    {
        "api_key",
        "authorization",
        "connection_string",
        "cookie",
        "credential",
        "password",
        "raw",
        "secret",
        "token",
    }
)
_SAFE_IDENTIFIER_FIELD_SUFFIXES = (
    "_code",
    "_id",
    "_kind",
    "_operation",
    "_purpose",
    "_state",
    "_status",
    "_type",
)


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
                isinstance(member.value, _IMMUTABLE_SCALARS)
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


def _is_safe_payload_annotation(
    annotation: object,
    *,
    metadata: list[Any] | tuple[Any, ...] = (),
    checked_models: set[type[BaseModel]],
) -> bool:
    if annotation is str:
        return any(
            item is DiagnosticFieldKind.IDENTIFIER for item in metadata
        ) and any(
            isinstance(item, StringConstraints)
            and item.min_length is not None
            and item.min_length >= 1
            and item.max_length is not None
            and item.max_length <= 128
            and item.pattern == DIAGNOSTIC_IDENTIFIER_PATTERN
            for item in metadata
        )
    if annotation in (UUID, int, bool, datetime, date, time, timedelta):
        return True
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return all(
                isinstance(member.value, str)
                and _DIAGNOSTIC_IDENTIFIER.fullmatch(member.value) is not None
                for member in annotation.__members__.values()
            )
        if issubclass(annotation, BaseModel):
            return _has_only_bounded_safe_fields(
                annotation,
                checked_models=checked_models,
            )
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, UnionType):
        return all(
            item is NoneType
            or _is_safe_payload_annotation(
                item,
                checked_models=checked_models,
            )
            for item in arguments
        )
    if origin is Annotated:
        return bool(arguments) and _is_safe_payload_annotation(
            arguments[0],
            metadata=arguments[1:],
            checked_models=checked_models,
        )
    if origin is Literal:
        return all(
            isinstance(item, str)
            and _DIAGNOSTIC_IDENTIFIER.fullmatch(item) is not None
            for item in arguments
        )
    return False


def _has_only_bounded_safe_fields(
    payload_type: type[BaseModel],
    *,
    checked_models: set[type[BaseModel]] | None = None,
) -> bool:
    checked = checked_models if checked_models is not None else set()
    if payload_type in checked:
        return True
    checked.add(payload_type)
    return all(
        _is_safe_payload_annotation(
            field.annotation,
            metadata=field.metadata,
            checked_models=checked,
        )
        for field in payload_type.model_fields.values()
    )


def _annotation_can_carry_text(annotation: object) -> bool:
    if annotation is str:
        return True
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return any(
            isinstance(member.value, str)
            for member in annotation.__members__.values()
        )
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return bool(arguments) and _annotation_can_carry_text(arguments[0])
    if origin in (Union, UnionType):
        return any(_annotation_can_carry_text(item) for item in arguments)
    if origin is Literal:
        return any(isinstance(item, str) for item in arguments)
    return False


def _nested_model_types(annotation: object) -> tuple[type[BaseModel], ...]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    nested: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        nested.extend(_nested_model_types(argument))
    return tuple(nested)


def _has_no_content_bearing_field_names(
    payload_type: type[BaseModel],
    *,
    checked_models: set[type[BaseModel]] | None = None,
) -> bool:
    checked = checked_models if checked_models is not None else set()
    if payload_type in checked:
        return True
    checked.add(payload_type)
    for name, field in payload_type.model_fields.items():
        normalized = name.lower()
        if _annotation_can_carry_text(field.annotation):
            if any(
                term in normalized
                for term in _ALWAYS_FORBIDDEN_STRING_FIELD_TERMS
            ):
                return False
            if (
                not normalized.endswith(_SAFE_IDENTIFIER_FIELD_SUFFIXES)
                and any(
                    term in normalized for term in _FORBIDDEN_PAYLOAD_FIELD_TERMS
                )
            ):
                return False
        for nested_model in _nested_model_types(field.annotation):
            if not _has_no_content_bearing_field_names(
                nested_model,
                checked_models=checked,
            ):
                return False
    return True


def _has_computed_fields_or_custom_serializers(
    payload_type: type[BaseModel],
    *,
    checked_models: set[type[BaseModel]] | None = None,
) -> bool:
    checked = checked_models if checked_models is not None else set()
    if payload_type in checked:
        return False
    checked.add(payload_type)
    decorators = payload_type.__pydantic_decorators__
    if (
        payload_type.model_computed_fields
        or decorators.field_serializers
        or decorators.model_serializers
        or payload_type.model_config.get("json_encoders")
        or payload_type.model_dump is not BaseModel.model_dump
        or payload_type.model_dump_json is not BaseModel.model_dump_json
        or _core_schema_has_custom_serialization(
            payload_type.__pydantic_core_schema__
        )
    ):
        return True
    return any(
        _has_computed_fields_or_custom_serializers(
            nested_model,
            checked_models=checked,
        )
        for field in payload_type.model_fields.values()
        for nested_model in _nested_model_types(field.annotation)
    )


def _core_schema_has_custom_serialization(
    value: object,
    *,
    checked_objects: set[int] | None = None,
) -> bool:
    """Detect every Pydantic serialization hook, including Annotated metadata."""

    checked = checked_objects if checked_objects is not None else set()
    identity = id(value)
    if identity in checked:
        return False
    checked.add(identity)
    if isinstance(value, dict):
        if "serialization" in value:
            return True
        return any(
            _core_schema_has_custom_serialization(item, checked_objects=checked)
            for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _core_schema_has_custom_serialization(item, checked_objects=checked)
            for item in value
        )
    return False


class DiagnosticFieldKind(str, Enum):
    """Semantic classifications allowed for Diagnostic Event strings."""

    IDENTIFIER = "diagnostic_identifier"


DiagnosticIdentifier = Annotated[
    str,
    DiagnosticFieldKind.IDENTIFIER,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=DIAGNOSTIC_IDENTIFIER_PATTERN,
    ),
]


class DiagnosticSeverity(str, Enum):
    """Closed severity vocabulary independent of a telemetry backend."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DiagnosticObjectReference(BaseModel):
    """Opaque non-content-bearing reference to one versioned canonical object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object_type: DiagnosticIdentifier
    object_id: UUID
    schema_version: int = Field(ge=1)

    @field_validator("object_id")
    @classmethod
    def reject_zero_identifier(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("object_id must be non-zero.")
        return value


class DiagnosticFailureCode(str, Enum):
    """Registered safe failure categories for non-canonical observations."""

    INTERNAL_UNEXPECTED = "internal.unexpected"
    AUTHENTICATION_INVALID = "authentication.invalid"
    AUTHENTICATION_UNAVAILABLE = "authentication.unavailable"
    AUTHORIZATION_DENIED = "authorization.denied"
    AUTHORIZATION_UNAVAILABLE = "authorization.unavailable"
    PERSISTENCE_FAILURE = "persistence.failure"
    PERSISTENCE_UNAVAILABLE = "persistence.unavailable"
    SPINE_INTERNAL_UNEXPECTED = "spine.internal.unexpected"
    SPINE_PERSISTENCE_FAILURE = "spine.persistence.failure"
    SPINE_PERSISTENCE_INVALID_CONTEXT = "spine.persistence.invalid_context"
    SPINE_PERSISTENCE_INVALID_BOOTSTRAP_AUTHORITY = (
        "spine.persistence.invalid_bootstrap_authority"
    )
    SPINE_PERSISTENCE_UNAVAILABLE = "spine.persistence.unavailable"
    SPINE_PERSISTENCE_RETRYABLE_FAILURE = "spine.persistence.retryable_failure"
    SPINE_PERSISTENCE_DEADLOCK = "spine.persistence.deadlock"
    SPINE_PERSISTENCE_SERIALIZATION = "spine.persistence.serialization"
    SPINE_PERSISTENCE_OPTIMISTIC_CONFLICT = (
        "spine.persistence.optimistic_conflict"
    )
    SPINE_PERSISTENCE_CONSTRAINT_CONFLICT = (
        "spine.persistence.constraint_conflict"
    )
    SPINE_PERSISTENCE_IDEMPOTENCY_CONFLICT = (
        "spine.persistence.idempotency_conflict"
    )
    SPINE_PERSISTENCE_OUTBOX_CONFLICT = "spine.persistence.outbox_conflict"
    SPINE_PERSISTENCE_AUDIT_CONFLICT = "spine.persistence.audit_conflict"
    SPINE_PERSISTENCE_INCOMPATIBLE_SCHEMA = (
        "spine.persistence.incompatible_schema"
    )
    SPINE_PERSISTENCE_UNEXPECTED = "spine.persistence.unexpected"
    SPINE_PERSISTENCE_UNIT_OF_WORK_LIFECYCLE = (
        "spine.persistence.unit_of_work_lifecycle"
    )
    SPINE_PERSISTENCE_UNSUPPORTED_OUTBOX_EVENT = (
        "spine.persistence.unsupported_outbox_event"
    )
    SPINE_PERSISTENCE_UNSUPPORTED_AUDIT_EVENT = (
        "spine.persistence.unsupported_audit_event"
    )


class FailureDiagnosticPayload(BaseModel):
    """Disclosure-safe category for a mapped application failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_code: DiagnosticFailureCode


class OutboxDeliveryDiagnosticState(str, Enum):
    """Registered safe outbox lifecycle observations."""

    PENDING = "pending"
    LEASED = "leased"
    RETRY_SCHEDULED = "retry_scheduled"
    DELIVERED = "delivered"
    QUARANTINED = "quarantined"


class OutboxDeliveryDiagnosticPayload(BaseModel):
    """Safe operational state for later outbox dispatcher composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_state: OutboxDeliveryDiagnosticState
    attempt_number: int = Field(ge=1)
    retry_delay: timedelta | None = Field(default=None, ge=timedelta(0))


class UnsupportedDiagnosticEventError(ValueError):
    """An event type, version, or payload is not registered and valid."""


class DiagnosticContextMismatchError(ValueError):
    """An event does not match its trusted diagnostic or tenant context."""

    def __init__(self) -> None:
        super().__init__("Diagnostic Event context is invalid.")


@dataclass(frozen=True, slots=True)
class _DiagnosticEventContextSnapshot:
    trace_id: UUID
    correlation_id: UUID | None
    causation_id: UUID | None
    workspace_id: UUID | None
    environment_id: UUID | None


class DiagnosticEvent(BaseModel):
    """Detached immutable operational observation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    event_type: DiagnosticIdentifier
    schema_version: int
    severity: DiagnosticSeverity
    occurred_at: datetime
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    workspace_id: UUID | None = None
    environment_id: UUID | None = None
    payload: BaseModel
    payload_snapshot: bytes | None = Field(default=None, exclude=True, repr=False)
    issued_context: _DiagnosticEventContextSnapshot = Field(exclude=True, repr=False)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("schema_version must be a positive integer.")
        return value

    @field_validator(
        "trace_id",
        "correlation_id",
        "causation_id",
        "workspace_id",
        "environment_id",
    )
    @classmethod
    def validate_identifiers(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("diagnostic identifiers must be non-zero.")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("diagnostic timestamps must be timezone-aware.")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> DiagnosticEvent:
        if self.environment_id is not None and self.workspace_id is None:
            raise ValueError("environment diagnostics require a workspace scope.")
        return self

    def payload_json(self) -> dict[str, Any]:
        """Return only the registry-validated JSON payload snapshot."""

        if self.payload_snapshot is None:
            raise UnsupportedDiagnosticEventError(
                "Diagnostic Event payload has not been validated by the registry."
            )
        value = json.loads(self.payload_snapshot)
        if not isinstance(value, dict):
            raise UnsupportedDiagnosticEventError(
                "Diagnostic Event payload must serialize to an object."
            )
        return value


class DiagnosticEventRegistry:
    """Explicit allowlist of producer-owned Diagnostic Event payload schemas."""

    def __init__(self) -> None:
        self._payload_types: dict[tuple[str, int], type[BaseModel]] = {}
        self._sealed = False

    @classmethod
    def with_default_families(cls) -> DiagnosticEventRegistry:
        registry = cls()
        registry.register(
            event_type="failure.observed",
            schema_version=1,
            payload_type=FailureDiagnosticPayload,
        )
        registry.register(
            event_type="outbox.delivery",
            schema_version=1,
            payload_type=OutboxDeliveryDiagnosticPayload,
        )
        registry.seal()
        return registry

    def register(
        self,
        *,
        event_type: str,
        schema_version: int,
        payload_type: type[BaseModel],
    ) -> None:
        if self._sealed:
            raise RuntimeError("Diagnostic Event registry is sealed.")
        if _DIAGNOSTIC_IDENTIFIER.fullmatch(event_type) is None:
            raise ValueError("event_type must be a bounded identifier.")
        if isinstance(schema_version, bool) or schema_version <= 0:
            raise ValueError("schema_version must be a positive integer.")
        if not isinstance(payload_type, type) or not issubclass(payload_type, BaseModel):
            raise TypeError("payload_type must be a Pydantic model type.")
        if payload_type.model_config.get("extra") != "forbid":
            raise ValueError('Diagnostic payload schemas must configure extra="forbid".')
        if payload_type.model_config.get("frozen") is not True:
            raise ValueError("Diagnostic payload schemas must be frozen.")
        if _has_computed_fields_or_custom_serializers(payload_type):
            raise ValueError(
                "Diagnostic payload schemas must not define computed fields "
                "or custom serializers."
            )
        if not _is_deeply_immutable_annotation(
            payload_type,
            checked_models=set(),
        ):
            raise ValueError(
                "Diagnostic payload schemas must contain only deeply immutable fields."
            )
        if not _has_only_bounded_safe_fields(payload_type):
            raise ValueError(
                "Diagnostic payload schemas must contain only bounded safe fields."
            )
        if not _has_no_content_bearing_field_names(payload_type):
            raise ValueError(
                "Diagnostic payload schemas must not contain content-bearing fields."
            )
        key = (event_type, schema_version)
        existing = self._payload_types.get(key)
        if existing is not None and existing is not payload_type:
            raise ValueError(
                "Diagnostic Event type and schema version are already registered."
            )
        self._payload_types[key] = payload_type

    def seal(self) -> None:
        if not self._payload_types:
            raise ValueError("Diagnostic Event registry must not be empty.")
        self._sealed = True

    def validate(self, event: DiagnosticEvent) -> DiagnosticEvent:
        if not self._sealed:
            raise RuntimeError("Diagnostic Event registry is not sealed.")
        if type(event) is not DiagnosticEvent:
            raise UnsupportedDiagnosticEventError(
                "Diagnostic Event must use the canonical envelope."
            )
        issued_context = event.issued_context
        if type(issued_context) is not _DiagnosticEventContextSnapshot or (
            event.trace_id,
            event.correlation_id,
            event.causation_id,
            event.workspace_id,
            event.environment_id,
        ) != (
            issued_context.trace_id,
            issued_context.correlation_id,
            issued_context.causation_id,
            issued_context.workspace_id,
            issued_context.environment_id,
        ):
            raise DiagnosticContextMismatchError()
        payload_type = self._payload_types.get(
            (event.event_type, event.schema_version)
        )
        if payload_type is None or type(event.payload) is not payload_type:
            raise UnsupportedDiagnosticEventError(
                "Diagnostic Event type, version, or payload is not registered."
            )
        try:
            payload = payload_type.model_validate(
                event.payload.model_dump(
                    mode="python",
                    round_trip=True,
                    warnings=False,
                ),
                strict=True,
            )
            payload_snapshot = json.dumps(
                payload.model_dump(mode="json"),
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return DiagnosticEvent(
                event_type=event.event_type,
                schema_version=event.schema_version,
                severity=event.severity,
                occurred_at=event.occurred_at,
                trace_id=event.trace_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                workspace_id=event.workspace_id,
                environment_id=event.environment_id,
                payload=payload,
                payload_snapshot=payload_snapshot,
                issued_context=_DiagnosticEventContextSnapshot(
                    trace_id=issued_context.trace_id,
                    correlation_id=issued_context.correlation_id,
                    causation_id=issued_context.causation_id,
                    workspace_id=issued_context.workspace_id,
                    environment_id=issued_context.environment_id,
                ),
            )
        except (PydanticSerializationError, TypeError, ValidationError, ValueError):
            raise UnsupportedDiagnosticEventError(
                "Diagnostic Event values do not match the registered schema."
            ) from None

    def build_event(
        self,
        *,
        event_type: str,
        schema_version: int,
        severity: DiagnosticSeverity,
        occurred_at: datetime,
        diagnostic_context: DiagnosticContext,
        payload: BaseModel,
        workspace_id: UUID | None = None,
        environment_id: UUID | None = None,
    ) -> DiagnosticEvent:
        if not isinstance(payload, BaseModel):
            raise UnsupportedDiagnosticEventError(
                "Diagnostic Event payload must use a registered schema."
            )
        issued_context = _DiagnosticEventContextSnapshot(
            trace_id=diagnostic_context.trace_id,
            correlation_id=diagnostic_context.correlation_id,
            causation_id=diagnostic_context.causation_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
        )
        event = DiagnosticEvent(
            event_type=event_type,
            schema_version=schema_version,
            severity=severity,
            occurred_at=occurred_at,
            trace_id=diagnostic_context.trace_id,
            correlation_id=diagnostic_context.correlation_id,
            causation_id=diagnostic_context.causation_id,
            workspace_id=workspace_id,
            environment_id=environment_id,
            payload=payload,
            issued_context=issued_context,
        )
        return self.validate(event)


class DiagnosticSink(Protocol):
    """Emit one registry-validated safe operational observation."""

    async def emit(self, event: DiagnosticEvent) -> None: ...


async def emit_diagnostic_safely(
    sink: DiagnosticSink,
    event: DiagnosticEvent,
) -> None:
    """Emit best-effort diagnostics without changing a canonical outcome."""

    try:
        await sink.emit(event)
    except Exception:
        return


def validate_diagnostic_event_for_context(
    event: DiagnosticEvent,
    *,
    registry: DiagnosticEventRegistry,
    diagnostic_context: DiagnosticContext,
    workspace_id: UUID | None,
    environment_id: UUID | None,
) -> DiagnosticEvent:
    """Validate one event against the exact trusted context before emission."""

    if type(event) is not DiagnosticEvent:
        raise DiagnosticContextMismatchError()
    canonical = registry.validate(event)
    if (
        canonical.trace_id != diagnostic_context.trace_id
        or canonical.correlation_id != diagnostic_context.correlation_id
        or canonical.causation_id != diagnostic_context.causation_id
        or canonical.workspace_id != workspace_id
        or canonical.environment_id != environment_id
    ):
        raise DiagnosticContextMismatchError()
    return canonical


__all__ = [
    "DIAGNOSTIC_IDENTIFIER_PATTERN",
    "DiagnosticEvent",
    "DiagnosticEventRegistry",
    "DiagnosticFailureCode",
    "DiagnosticContextMismatchError",
    "DiagnosticFieldKind",
    "DiagnosticIdentifier",
    "DiagnosticObjectReference",
    "DiagnosticSeverity",
    "DiagnosticSink",
    "FailureDiagnosticPayload",
    "OutboxDeliveryDiagnosticPayload",
    "OutboxDeliveryDiagnosticState",
    "UnsupportedDiagnosticEventError",
    "emit_diagnostic_safely",
    "validate_diagnostic_event_for_context",
]
