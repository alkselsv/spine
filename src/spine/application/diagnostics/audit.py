"""Immutable, registry-validated Audit Event contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    Protocol,
    TypeVar,
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

from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    InvalidPersistenceContextError,
    PersistenceError,
)
from spine.application.persistence.outbox import OpaqueObjectReference


_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_.:-]{0,127}$"
_IDENTIFIER = re.compile(_IDENTIFIER_PATTERN)
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
_ResultT = TypeVar("_ResultT")
AuditIdentifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=_IDENTIFIER_PATTERN,
    ),
]


class UnsupportedAuditEventError(PersistenceError):
    """An event type/version or payload schema is not allowlisted."""


def _require_identifier(value: str, *, field_name: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier.")
    return value


def _require_non_zero_uuid(value: UUID | None, *, field_name: str) -> UUID | None:
    if value is not None and value.int == 0:
        raise ValueError(f"{field_name} must be a non-zero UUID.")
    return value


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


def _has_only_bounded_safe_fields(payload_type: type[BaseModel]) -> bool:
    return all(
        _is_safe_payload_annotation(field.annotation, metadata=field.metadata)
        for field in payload_type.model_fields.values()
    )


def _is_safe_payload_annotation(
    annotation: object,
    *,
    metadata: list[Any] | tuple[Any, ...] = (),
) -> bool:
    if annotation is str:
        return any(
            isinstance(item, StringConstraints)
            and item.min_length is not None
            and item.min_length >= 1
            and item.max_length is not None
            and item.max_length <= 128
            and item.pattern == _IDENTIFIER_PATTERN
            for item in metadata
        )
    if annotation in (UUID, int, bool, Decimal, datetime, date, time, timedelta):
        return True
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return all(
                not isinstance(member.value, str)
                or _IDENTIFIER.fullmatch(member.value) is not None
                for member in annotation.__members__.values()
            )
        if issubclass(annotation, OpaqueObjectReference):
            return True
        if issubclass(annotation, BaseModel):
            return _has_only_bounded_safe_fields(annotation)
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin in (Union, UnionType):
        return all(
            item is NoneType or _is_safe_payload_annotation(item)
            for item in arguments
        )
    if origin is Annotated:
        return bool(arguments) and _is_safe_payload_annotation(
            arguments[0],
            metadata=arguments[1:],
        )
    if origin is Literal:
        return all(
            not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is not None
            for item in arguments
        )
    return False


class AuditOutcome(str, Enum):
    """Safe outcome codes shared by the initial Audit Event families."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPLAYED = "replayed"
    COMPLETED = "completed"
    COMMITTED = "committed"
    ALLOWED = "allowed"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    QUARANTINED = "quarantined"
    RECORDED = "recorded"


class CommandAuditPayload(BaseModel):
    """Safe command-family payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_type: AuditIdentifier

    @field_validator("command_type")
    @classmethod
    def validate_command_type(cls, value: str) -> str:
        return _require_identifier(value, field_name="command_type")


class CanonicalTransitionAuditPayload(BaseModel):
    """Safe canonical-transition payload with an opaque object identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transition: AuditIdentifier
    object: OpaqueObjectReference

    @field_validator("transition")
    @classmethod
    def validate_transition(cls, value: str) -> str:
        return _require_identifier(value, field_name="transition")


class AccessDecisionAuditPayload(BaseModel):
    """Safe access-decision payload without protected target content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purpose: AuditIdentifier
    operation: AuditIdentifier

    @field_validator("purpose", "operation")
    @classmethod
    def validate_policy_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, field_name=info.field_name)


class OutboxDeliveryAuditPayload(BaseModel):
    """Safe delivery-attempt payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_event_id: UUID
    attempt_id: UUID
    attempt_number: int

    @field_validator("outbox_event_id", "attempt_id")
    @classmethod
    def validate_delivery_identifier(cls, value: UUID, info: Any) -> UUID:
        checked = _require_non_zero_uuid(value, field_name=info.field_name)
        assert checked is not None
        return checked

    @field_validator("attempt_number")
    @classmethod
    def validate_attempt_number(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("attempt_number must be a positive integer.")
        return value


class FeedbackAuditPayload(BaseModel):
    """Safe feedback-family payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_type: AuditIdentifier

    @field_validator("feedback_type")
    @classmethod
    def validate_feedback_type(cls, value: str) -> str:
        return _require_identifier(value, field_name="feedback_type")


class AuditEvent(BaseModel):
    """Detached immutable snapshot accepted only through an event registry."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
        revalidate_instances="always",
    )

    audit_event_id: UUID | None = None
    event_type: str
    schema_version: int
    workspace_id: UUID
    environment_id: UUID | None = None
    origin: ContextOrigin
    acting_subject_id: UUID | None = None
    service_principal_id: UUID | None = None
    trace_id: UUID
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    occurred_at: datetime
    appended_at: datetime | None = None
    target: OpaqueObjectReference | None = None
    outcome: AuditOutcome
    reason: str
    producer_deduplication_id: str | None = None
    payload: BaseModel
    payload_snapshot: bytes | None = Field(default=None, exclude=True, repr=False)

    @field_validator("event_type", "reason")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        return _require_identifier(value, field_name=info.field_name)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("schema_version must be a positive integer.")
        return value

    @field_validator(
        "audit_event_id",
        "workspace_id",
        "environment_id",
        "acting_subject_id",
        "service_principal_id",
        "trace_id",
        "correlation_id",
        "causation_id",
    )
    @classmethod
    def validate_identifiers(cls, value: UUID | None, info: Any) -> UUID | None:
        return _require_non_zero_uuid(value, field_name=info.field_name)

    @field_validator("occurred_at", "appended_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("audit timestamps must be timezone-aware.")
        return value

    @field_validator("producer_deduplication_id")
    @classmethod
    def validate_producer_identity(cls, value: str | None) -> str | None:
        if value is not None:
            _require_identifier(
                value,
                field_name="producer_deduplication_id",
            )
        return value

    @model_validator(mode="after")
    def validate_actor_scope(self) -> AuditEvent:
        if self.origin is ContextOrigin.INTERACTIVE:
            if self.acting_subject_id is None:
                raise ValueError("interactive audit events require an acting subject.")
        elif self.origin is ContextOrigin.WORKER:
            if self.acting_subject_id is not None or self.service_principal_id is None:
                raise ValueError(
                    "worker audit events require only a service principal."
                )
        return self

    def payload_json(self) -> dict[str, Any]:
        """Return the registry-validated payload in JSON-compatible form."""

        if self.payload_snapshot is None:
            raise ValueError("Audit Event payload has not been registry validated.")
        payload = json.loads(self.payload_snapshot)
        if not isinstance(payload, dict):
            raise ValueError("Audit Event payload must serialize as an object.")
        return payload


class AuditEventRegistry:
    """Explicit allowlist of producer-owned Audit Event payload schemas."""

    def __init__(self) -> None:
        self._payload_types: dict[tuple[str, int], type[BaseModel]] = {}

    @classmethod
    def with_default_families(cls) -> AuditEventRegistry:
        registry = cls()
        for event_type, payload_type in (
            ("command.outcome", CommandAuditPayload),
            ("canonical.transition", CanonicalTransitionAuditPayload),
            ("access.decision", AccessDecisionAuditPayload),
            ("outbox.delivery", OutboxDeliveryAuditPayload),
            ("feedback.recorded", FeedbackAuditPayload),
        ):
            registry.register(
                event_type=event_type,
                schema_version=1,
                payload_type=payload_type,
            )
        return registry

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
            raise ValueError('Audit payload schemas must configure extra="forbid".')
        if payload_type.model_config.get("frozen") is not True:
            raise ValueError("Audit payload schemas must be frozen.")
        if not _is_deeply_immutable_annotation(
            payload_type,
            checked_models=set(),
        ):
            raise ValueError(
                "Audit payload schemas must contain only deeply immutable fields."
            )
        if not _has_only_bounded_safe_fields(payload_type):
            raise ValueError(
                "Audit payload schemas must contain only bounded safe fields."
            )
        key = (event_type, schema_version)
        existing = self._payload_types.get(key)
        if existing is not None and existing is not payload_type:
            raise ValueError("Audit Event type and schema version are already registered.")
        self._payload_types[key] = payload_type

    def validate(self, event: AuditEvent) -> AuditEvent:
        """Return a detached snapshot after revalidating its registered schema."""

        expected = self._payload_types.get((event.event_type, event.schema_version))
        if expected is None or type(event.payload) is not expected:
            raise UnsupportedAuditEventError(
                "Audit Event type, version, or payload is not registered."
            )
        try:
            canonical_payload = expected.model_validate(
                event.payload.model_dump(
                    mode="python",
                    round_trip=True,
                    warnings=False,
                ),
                strict=True,
            )
            serialized_payload = json.dumps(
                canonical_payload.model_dump(mode="json"),
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            canonical_target = (
                OpaqueObjectReference.model_validate(
                    event.target.model_dump(
                        mode="python",
                        round_trip=True,
                        warnings=False,
                    ),
                    strict=True,
                )
                if event.target is not None
                else None
            )
            return AuditEvent(
                audit_event_id=event.audit_event_id,
                event_type=event.event_type,
                schema_version=event.schema_version,
                workspace_id=event.workspace_id,
                environment_id=event.environment_id,
                origin=event.origin,
                acting_subject_id=event.acting_subject_id,
                service_principal_id=event.service_principal_id,
                trace_id=event.trace_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
                occurred_at=event.occurred_at,
                appended_at=event.appended_at,
                target=canonical_target,
                outcome=event.outcome,
                reason=event.reason,
                producer_deduplication_id=event.producer_deduplication_id,
                payload=canonical_payload,
                payload_snapshot=serialized_payload,
            )
        except (
            PydanticSerializationError,
            TypeError,
            ValidationError,
            ValueError,
        ):
            raise UnsupportedAuditEventError(
                "Audit Event values do not match the registered schema."
            ) from None

    def build_event(
        self,
        *,
        workspace_id: UUID,
        event_type: str,
        schema_version: int,
        payload: BaseModel,
        origin: ContextOrigin,
        acting_subject_id: UUID | None,
        service_principal_id: UUID | None,
        trace_id: UUID,
        occurred_at: datetime,
        outcome: AuditOutcome,
        reason: str,
        environment_id: UUID | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        target: OpaqueObjectReference | None = None,
        producer_deduplication_id: str | None = None,
        audit_event_id: UUID | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            audit_event_id=audit_event_id,
            event_type=event_type,
            schema_version=schema_version,
            workspace_id=workspace_id,
            environment_id=environment_id,
            origin=origin,
            acting_subject_id=acting_subject_id,
            service_principal_id=service_principal_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            occurred_at=occurred_at,
            target=target,
            outcome=outcome,
            reason=reason,
            producer_deduplication_id=producer_deduplication_id,
            payload=payload,
        )
        return self.validate(event)


class AuditWriter(Protocol):
    """Append-only audit interface exposed by a purpose-specific Unit of Work."""

    async def append(self, event: AuditEvent) -> UUID: ...


class AuditReader(Protocol):
    """Scoped read-by-opaque-ID seam used by conformance tests and adapters."""

    async def resolve(
        self,
        context: TrustedPersistenceContext,
        audit_event_id: UUID,
    ) -> AuditEvent | None: ...


class RequiredAuditUnitOfWork(Protocol):
    @property
    def audit(self) -> AuditWriter: ...

    async def __aenter__(self) -> RequiredAuditUnitOfWork: ...

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    async def commit(self) -> None: ...


class RequiredAuditUnitOfWorkFactory(Protocol):
    def __call__(
        self,
        context: TrustedPersistenceContext,
    ) -> RequiredAuditUnitOfWork: ...


class RequiredAuditCoordinator:
    """Commit required audit before a non-transactional effect is released."""

    def __init__(self, uow_factory: RequiredAuditUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def release_after_audit(
        self,
        *,
        context: TrustedPersistenceContext,
        event: AuditEvent,
        release: Callable[[UUID], Awaitable[_ResultT]],
    ) -> _ResultT:
        async with self._uow_factory(context) as uow:
            audit_event_id = await uow.audit.append(event)
            await uow.commit()
        return await release(audit_event_id)


def validate_audit_event_for_context(
    event: AuditEvent,
    *,
    registry: AuditEventRegistry,
    context: TrustedPersistenceContext,
) -> AuditEvent:
    """Validate registry, tenant, actor and trace scope before persistence."""

    canonical = registry.validate(event)
    scope = context.scope
    if isinstance(scope, WorkspaceScope):
        scope_matches = (
            canonical.workspace_id == scope.workspace_id
            and canonical.environment_id is None
        )
    elif isinstance(scope, EnvironmentScope):
        scope_matches = (
            canonical.workspace_id == scope.workspace_id
            and canonical.environment_id == scope.environment_id
        )
    else:
        scope_matches = False
    if not scope_matches:
        raise InvalidPersistenceContextError("Audit Event scope does not match context.")
    if canonical.origin is not context.origin:
        raise InvalidPersistenceContextError("Audit Event origin does not match context.")
    if canonical.acting_subject_id != context.acting_subject_id:
        raise InvalidPersistenceContextError("Audit Event actor does not match context.")
    if canonical.service_principal_id != context.service_principal_id:
        raise InvalidPersistenceContextError(
            "Audit Event service principal does not match context."
        )
    if canonical.trace_id != context.trace_id:
        raise InvalidPersistenceContextError("Audit Event trace does not match context.")
    if canonical.appended_at is not None:
        raise InvalidPersistenceContextError("Audit append time is adapter-owned.")
    return canonical
