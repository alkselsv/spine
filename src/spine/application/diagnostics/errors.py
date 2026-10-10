"""Versioned safe errors and their explicit application failure mappings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from spine.application.diagnostics.audit import UnsupportedAuditEventError
from spine.application.diagnostics.context import DiagnosticContext
from spine.application.persistence.errors import (
    AuditConflictError,
    ConstraintConflictError,
    IdempotencyConflictError,
    IncompatibleSchemaError,
    InvalidBootstrapAuthorityError,
    InvalidPersistenceContextError,
    OptimisticConflictError,
    OutboxConflictError,
    PersistenceError,
    PersistenceUnavailableError,
    RetryablePersistenceError,
    TransactionDeadlockError,
    TransactionSerializationError,
    UnexpectedPersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.application.persistence.outbox import UnsupportedOutboxEventError


_CODE_PATTERN = re.compile(r"^spine\.[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_MAX_CODE_LENGTH = 96
_SAFE_MESSAGE_BY_CODE = MappingProxyType({
    "spine.internal.unexpected": "An unexpected internal error occurred.",
    "spine.persistence.failure": "Persistence operation failed.",
    "spine.persistence.invalid_context": "Persistence context is invalid.",
    "spine.persistence.invalid_bootstrap_authority": "Bootstrap authority is invalid.",
    "spine.persistence.unavailable": "Persistence is temporarily unavailable.",
    "spine.persistence.retryable_failure": "The persistence operation may be retried.",
    "spine.persistence.deadlock": "The transaction encountered a temporary conflict.",
    "spine.persistence.serialization": "The transaction encountered a temporary conflict.",
    "spine.persistence.optimistic_conflict": "The operation conflicts with a newer version.",
    "spine.persistence.constraint_conflict": "The operation conflicts with a persistence constraint.",
    "spine.persistence.idempotency_conflict": "The idempotency key conflicts with an existing command.",
    "spine.persistence.outbox_conflict": "The outbox operation conflicts with an existing event.",
    "spine.persistence.audit_conflict": "The audit operation conflicts with an existing event.",
    "spine.persistence.incompatible_schema": "The persistence schema is incompatible.",
    "spine.persistence.unexpected": "The persistence operation failed unexpectedly.",
    "spine.persistence.unit_of_work_lifecycle": "The persistence operation has an invalid lifecycle.",
    "spine.persistence.unsupported_outbox_event": "The outbox event is not supported.",
    "spine.persistence.unsupported_audit_event": "The audit event is not supported.",
})


class Retryability(str, Enum):
    """Caller-facing retry advice; this enum does not execute retries."""

    NEVER = "never"
    SAME_COMMAND = "same_command"
    AFTER_DELAY = "after_delay"


class StructuredError(BaseModel):
    """The allowlisted public error envelope for schema version one.

    Safe messages are static code/message pairs owned by this module. Registry
    configuration must use one of those pairs; arbitrary caller text is not a
    valid public error value.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )

    schema_version: Literal[1] = 1
    code: str = Field(min_length=1, max_length=_MAX_CODE_LENGTH)
    safe_message: str = Field(min_length=1, max_length=256)
    retryability: Retryability
    trace_id: UUID

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not _CODE_PATTERN.fullmatch(value):
            raise ValueError("error code must be a bounded namespaced identifier")
        return value

    @field_validator("trace_id")
    @classmethod
    def reject_zero_trace_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("trace_id must be non-zero")
        return value

    @model_validator(mode="after")
    def validate_safe_message_pair(self) -> StructuredError:
        if _SAFE_MESSAGE_BY_CODE.get(self.code) != self.safe_message:
            raise ValueError("safe_message is not approved for code")
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> StructuredError:
        """Copy only through the same validation as ordinary construction."""

        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(copied.model_dump())

    def copy(
        self,
        *,
        include: Any = None,
        exclude: Any = None,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> StructuredError:
        """Keep Pydantic's deprecated copy API on the validated path too."""

        values = self.model_dump(include=include, exclude=exclude)
        if update:
            values.update(update)
        if deep:
            copied = super().model_copy(deep=True)
            values = copied.model_dump(include=include, exclude=exclude)
            if update:
                values.update(update)
        return type(self).model_validate(values)

    @classmethod
    def model_construct(
        cls,
        _fields_set: set[str] | None = None,
        **values: Any,
    ) -> StructuredError:
        """Prevent Pydantic's explicitly unvalidated construction shortcut."""

        raise TypeError("StructuredError.model_construct is not supported")


class ErrorRegistryConfigurationError(ValueError):
    """Base class for deterministic registry construction failures."""


class DuplicateExceptionMappingError(ErrorRegistryConfigurationError):
    """A failure type was registered more than once."""


class DuplicateErrorCodeError(ErrorRegistryConfigurationError):
    """A public error code was assigned to more than one failure type."""


@dataclass(frozen=True, slots=True)
class ErrorMapping:
    """A static mapping from one application failure category to safe output."""

    exception_type: type[BaseException]
    code: str
    safe_message: str
    retryability: Retryability


class StructuredErrorRegistry:
    """Explicit registry using exact categories to make ambiguity impossible."""

    def __init__(self) -> None:
        self._mappings: dict[type[BaseException], ErrorMapping] = {}
        self._codes: dict[str, type[BaseException]] = {}

    def register(
        self,
        exception_type: type[BaseException],
        code: str,
        safe_message: str,
        retryability: Retryability,
    ) -> None:
        """Register one category, rejecting duplicate or invalid definitions."""

        if exception_type in self._mappings:
            raise DuplicateExceptionMappingError(
                "an exception type already has a registered mapping"
            )
        if code in self._codes:
            raise DuplicateErrorCodeError("an error code already has a registered mapping")
        _validate_mapping_values(code, safe_message)
        mapping = ErrorMapping(exception_type, code, safe_message, retryability)
        self._mappings[exception_type] = mapping
        self._codes[code] = exception_type

    def map(self, failure: BaseException, context: DiagnosticContext) -> StructuredError:
        """Map a failure without consulting its string or representation."""

        mapping = self._mappings.get(type(failure))
        if mapping is None:
            return StructuredError(
                code="spine.internal.unexpected",
                safe_message="An unexpected internal error occurred.",
                retryability=Retryability.NEVER,
                trace_id=context.trace_id,
            )
        return StructuredError(
            code=mapping.code,
            safe_message=mapping.safe_message,
            retryability=mapping.retryability,
            trace_id=context.trace_id,
        )


def build_default_error_registry() -> StructuredErrorRegistry:
    """Build the explicit registry for the current persistence error taxonomy."""

    registry = StructuredErrorRegistry()
    mappings = (
        (PersistenceError, "spine.persistence.failure", "Persistence operation failed.", Retryability.NEVER),
        (InvalidPersistenceContextError, "spine.persistence.invalid_context", "Persistence context is invalid.", Retryability.NEVER),
        (InvalidBootstrapAuthorityError, "spine.persistence.invalid_bootstrap_authority", "Bootstrap authority is invalid.", Retryability.NEVER),
        (PersistenceUnavailableError, "spine.persistence.unavailable", "Persistence is temporarily unavailable.", Retryability.AFTER_DELAY),
        (RetryablePersistenceError, "spine.persistence.retryable_failure", "The persistence operation may be retried.", Retryability.AFTER_DELAY),
        (TransactionDeadlockError, "spine.persistence.deadlock", "The transaction encountered a temporary conflict.", Retryability.AFTER_DELAY),
        (TransactionSerializationError, "spine.persistence.serialization", "The transaction encountered a temporary conflict.", Retryability.AFTER_DELAY),
        (OptimisticConflictError, "spine.persistence.optimistic_conflict", "The operation conflicts with a newer version.", Retryability.NEVER),
        (ConstraintConflictError, "spine.persistence.constraint_conflict", "The operation conflicts with a persistence constraint.", Retryability.NEVER),
        (IdempotencyConflictError, "spine.persistence.idempotency_conflict", "The idempotency key conflicts with an existing command.", Retryability.NEVER),
        (OutboxConflictError, "spine.persistence.outbox_conflict", "The outbox operation conflicts with an existing event.", Retryability.NEVER),
        (AuditConflictError, "spine.persistence.audit_conflict", "The audit operation conflicts with an existing event.", Retryability.NEVER),
        (IncompatibleSchemaError, "spine.persistence.incompatible_schema", "The persistence schema is incompatible.", Retryability.NEVER),
        (UnexpectedPersistenceError, "spine.persistence.unexpected", "The persistence operation failed unexpectedly.", Retryability.NEVER),
        (UnitOfWorkLifecycleError, "spine.persistence.unit_of_work_lifecycle", "The persistence operation has an invalid lifecycle.", Retryability.NEVER),
        (UnsupportedOutboxEventError, "spine.persistence.unsupported_outbox_event", "The outbox event is not supported.", Retryability.NEVER),
        (UnsupportedAuditEventError, "spine.persistence.unsupported_audit_event", "The audit event is not supported.", Retryability.NEVER),
    )
    for exception_type, code, safe_message, retryability in mappings:
        registry.register(exception_type, code, safe_message, retryability)
    return registry


def _validate_mapping_values(code: str, safe_message: str) -> None:
    if len(code) > _MAX_CODE_LENGTH or not _CODE_PATTERN.fullmatch(code):
        raise ErrorRegistryConfigurationError("error code is not a bounded namespaced identifier")
    if not 1 <= len(safe_message) <= 256:
        raise ErrorRegistryConfigurationError("safe message must be bounded and non-empty")
    if _SAFE_MESSAGE_BY_CODE.get(code) != safe_message:
        raise ErrorRegistryConfigurationError("code/message pair is not allowlisted")
