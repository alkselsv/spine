"""Immutable, registry-validated Audit Event contracts."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
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
    TypeVar,
    Union,
    get_args,
    get_origin,
)
from uuid import UUID

from pydantic import (
    BaseModel,
    StringConstraints,
    ValidationError,
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
from spine.auth.errors import AuthorizationDeniedError
from spine.domain.audit import (
    AccessDecisionAuditPayload,
    AuditEvent,
    AuditFieldKind,
    AuditIdentifier,
    AuditObjectReference,
    AuditOutcome,
    CanonicalTransitionAuditPayload,
    CommandAuditPayload,
    FeedbackAuditPayload,
    OutboxDeliveryAuditPayload,
)
from spine.domain.audit.models import AUDIT_IDENTIFIER_PATTERN


_IDENTIFIER_PATTERN = AUDIT_IDENTIFIER_PATTERN
_IDENTIFIER = re.compile(_IDENTIFIER_PATTERN)
_FORBIDDEN_PAYLOAD_FIELD_TERMS = frozenset(
    {
        "answer",
        "api_key",
        "authorization",
        "body",
        "chain_of_thought",
        "content",
        "credential",
        "cookie",
        "connection_string",
        "document",
        "error",
        "exception",
        "excerpt",
        "filename",
        "message",
        "payload",
        "password",
        "prompt",
        "provider",
        "question",
        "raw",
        "secret",
        "sql",
        "stack",
        "token",
        "traceback",
        "text",
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
    "_transition",
    "_type",
)
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
_RELEASABLE_AUDIT_OUTCOMES = frozenset(
    {
        AuditOutcome.ACCEPTED,
        AuditOutcome.ALLOWED,
    }
)


class UnsupportedAuditEventError(PersistenceError):
    """An event type/version or payload schema is not allowlisted."""


@dataclass(frozen=True, slots=True)
class _AuditEventDefinition:
    payload_type: type[BaseModel]
    allowed_outcomes: frozenset[AuditOutcome]
    requires_target: bool


def _require_identifier(value: str, *, field_name: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded identifier.")
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
        if _is_content_bearing_field_name(name, annotation=field.annotation):
            return False
        for nested_model in _nested_model_types(field.annotation):
            if issubclass(nested_model, AuditObjectReference):
                continue
            if not _has_no_content_bearing_field_names(
                nested_model,
                checked_models=checked,
            ):
                return False
    return True


def _is_content_bearing_field_name(name: str, *, annotation: object) -> bool:
    if not _annotation_can_carry_free_text(annotation):
        return False
    normalized = name.lower()
    if any(
        term in normalized for term in _ALWAYS_FORBIDDEN_STRING_FIELD_TERMS
    ):
        return True
    if normalized.endswith(_SAFE_IDENTIFIER_FIELD_SUFFIXES):
        return False
    return any(term in normalized for term in _FORBIDDEN_PAYLOAD_FIELD_TERMS)


def _annotation_can_carry_free_text(annotation: object) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return bool(arguments) and _annotation_can_carry_free_text(arguments[0])
    if origin in (Union, UnionType):
        return any(_annotation_can_carry_free_text(item) for item in arguments)
    return False


def _nested_model_types(annotation: object) -> tuple[type[BaseModel], ...]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return (annotation,)
    nested: list[type[BaseModel]] = []
    for argument in get_args(annotation):
        nested.extend(_nested_model_types(argument))
    return tuple(nested)


def _is_safe_payload_annotation(
    annotation: object,
    *,
    metadata: list[Any] | tuple[Any, ...] = (),
) -> bool:
    if annotation is str:
        return any(
            item is AuditFieldKind.IDENTIFIER for item in metadata
        ) and any(
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
        if issubclass(annotation, AuditObjectReference):
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


class AuditEventRegistry:
    """Explicit allowlist of producer-owned Audit Event payload schemas."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], _AuditEventDefinition] = {}
        self._sealed = False

    @classmethod
    def with_default_families(cls) -> AuditEventRegistry:
        registry = cls()
        for event_type, payload_type, allowed_outcomes, requires_target in (
            (
                "command.outcome",
                CommandAuditPayload,
                frozenset(
                    {
                        AuditOutcome.ACCEPTED,
                        AuditOutcome.REJECTED,
                        AuditOutcome.REPLAYED,
                        AuditOutcome.COMPLETED,
                    }
                ),
                False,
            ),
            (
                "canonical.transition",
                CanonicalTransitionAuditPayload,
                frozenset({AuditOutcome.COMMITTED}),
                True,
            ),
            (
                "access.decision",
                AccessDecisionAuditPayload,
                frozenset({AuditOutcome.ALLOWED, AuditOutcome.DENIED}),
                False,
            ),
            (
                "outbox.delivery",
                OutboxDeliveryAuditPayload,
                frozenset(
                    {
                        AuditOutcome.SUCCEEDED,
                        AuditOutcome.RETRY_SCHEDULED,
                        AuditOutcome.QUARANTINED,
                    }
                ),
                False,
            ),
            (
                "feedback.recorded",
                FeedbackAuditPayload,
                frozenset({AuditOutcome.RECORDED}),
                False,
            ),
        ):
            registry.register(
                event_type=event_type,
                schema_version=1,
                payload_type=payload_type,
                allowed_outcomes=allowed_outcomes,
                requires_target=requires_target,
            )
        registry.seal()
        return registry

    def seal(self) -> None:
        """Finish construction so runtime definitions cannot change."""

        if not self._definitions:
            raise ValueError("Audit Event registry must not be empty.")
        self._sealed = True

    def require_sealed(self) -> None:
        if not self._sealed:
            raise RuntimeError("Audit Event registry is not sealed.")

    def register(
        self,
        *,
        event_type: str,
        schema_version: int,
        payload_type: type[BaseModel],
        allowed_outcomes: frozenset[AuditOutcome],
        requires_target: bool = False,
    ) -> None:
        if self._sealed:
            raise RuntimeError("Audit Event registry is sealed.")
        event_type = _require_identifier(event_type, field_name="event_type")
        if isinstance(schema_version, bool) or schema_version <= 0:
            raise ValueError("schema_version must be a positive integer.")
        if not isinstance(payload_type, type) or not issubclass(payload_type, BaseModel):
            raise TypeError("payload_type must be a Pydantic model type.")
        if payload_type.model_config.get("extra") != "forbid":
            raise ValueError('Audit payload schemas must configure extra="forbid".')
        if payload_type.model_config.get("frozen") is not True:
            raise ValueError("Audit payload schemas must be frozen.")
        if (
            type(allowed_outcomes) is not frozenset
            or not allowed_outcomes
            or any(type(outcome) is not AuditOutcome for outcome in allowed_outcomes)
        ):
            raise ValueError("Audit Event outcomes must be a non-empty frozen set.")
        if type(requires_target) is not bool:
            raise TypeError("requires_target must be a boolean.")
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
        if not _has_no_content_bearing_field_names(payload_type):
            raise ValueError(
                "Audit payload schemas must not contain content-bearing fields."
            )
        key = (event_type, schema_version)
        definition = _AuditEventDefinition(
            payload_type=payload_type,
            allowed_outcomes=allowed_outcomes,
            requires_target=requires_target,
        )
        existing = self._definitions.get(key)
        if existing is not None and existing != definition:
            raise ValueError("Audit Event type and schema version are already registered.")
        self._definitions[key] = definition

    def validate(self, event: AuditEvent) -> AuditEvent:
        """Return a detached snapshot after revalidating its registered schema."""

        self.require_sealed()
        definition = self._definitions.get(
            (event.event_type, event.schema_version)
        )
        if definition is None or type(event.payload) is not definition.payload_type:
            raise UnsupportedAuditEventError(
                "Audit Event type, version, or payload is not registered."
            )
        if event.outcome not in definition.allowed_outcomes:
            raise UnsupportedAuditEventError(
                "Audit Event outcome is not registered for this event family."
            )
        if definition.requires_target and event.target is None:
            raise UnsupportedAuditEventError(
                "Audit Event target is required for this event family."
            )
        try:
            canonical_payload = definition.payload_type.model_validate(
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
                AuditObjectReference.model_validate(
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
            return event.detached_snapshot(
                update={
                    "target": canonical_target,
                    "payload": canonical_payload,
                    "payload_snapshot": serialized_payload,
                }
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
        target: AuditObjectReference | None = None,
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
        if event.outcome not in _RELEASABLE_AUDIT_OUTCOMES:
            raise AuthorizationDeniedError()
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
