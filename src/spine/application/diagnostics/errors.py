"""Versioned safe errors and their explicit application failure mappings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from spine.application.diagnostics.context import DiagnosticContext
from spine.application.persistence.errors import (
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


class Retryability(str, Enum):
    """Caller-facing retry advice; this enum does not execute retries."""

    NEVER = "never"
    SAME_COMMAND = "same_command"
    AFTER_DELAY = "after_delay"


class StructuredError(BaseModel):
    """The allowlisted public error envelope for schema version one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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
        (IncompatibleSchemaError, "spine.persistence.incompatible_schema", "The persistence schema is incompatible.", Retryability.NEVER),
        (UnexpectedPersistenceError, "spine.persistence.unexpected", "The persistence operation failed unexpectedly.", Retryability.NEVER),
        (UnitOfWorkLifecycleError, "spine.persistence.unit_of_work_lifecycle", "The persistence operation has an invalid lifecycle.", Retryability.NEVER),
        (UnsupportedOutboxEventError, "spine.persistence.unsupported_outbox_event", "The outbox event is not supported.", Retryability.NEVER),
    )
    for exception_type, code, safe_message, retryability in mappings:
        registry.register(exception_type, code, safe_message, retryability)
    return registry


def _validate_mapping_values(code: str, safe_message: str) -> None:
    if len(code) > _MAX_CODE_LENGTH or not _CODE_PATTERN.fullmatch(code):
        raise ErrorRegistryConfigurationError("error code is not a bounded namespaced identifier")
    if not 1 <= len(safe_message) <= 256:
        raise ErrorRegistryConfigurationError("safe message must be bounded and non-empty")
