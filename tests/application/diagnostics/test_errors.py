from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from spine.application.diagnostics import (
    DuplicateErrorCodeError,
    DuplicateExceptionMappingError,
    ErrorRegistryConfigurationError,
    Retryability,
    StructuredError,
    StructuredErrorRegistry,
    build_default_error_registry,
)
from spine.application.diagnostics.audit import UnsupportedAuditEventError
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
from spine.auth import (
    AuthenticationUnavailableError,
    AuthorizationDeniedError,
    AuthorizationUnavailableError,
    InvalidAuthenticationError,
)

from ...contracts.diagnostics.fixtures import ERROR_LEAK_CORPUS, assert_safe_structured_error, diagnostic_context, synthetic_uuid


def test_structured_error_v1_is_frozen_and_has_only_public_fields() -> None:
    error = StructuredError(
        code="spine.persistence.unavailable",
        safe_message="Persistence is temporarily unavailable.",
        retryability=Retryability.AFTER_DELAY,
        trace_id=synthetic_uuid(1),
    )

    assert error.schema_version == 1
    assert_safe_structured_error(error)
    with pytest.raises(ValidationError):
        error.safe_message = "changed"
    with pytest.raises(ValidationError):
        StructuredError(
            code="spine.persistence.unavailable",
            safe_message="Persistence is temporarily unavailable.",
            retryability="never",
            trace_id=synthetic_uuid(1),
            secret="forbidden",
        )


@pytest.mark.parametrize("retryability", list(Retryability))
def test_structured_error_accepts_only_declared_retryability_values(retryability: Retryability) -> None:
    error = StructuredError(
        code="spine.internal.unexpected",
        safe_message="An unexpected internal error occurred.",
        retryability=retryability,
        trace_id=synthetic_uuid(1),
    )

    assert error.retryability is retryability


def test_structured_error_rejects_invalid_schema_and_codes() -> None:
    with pytest.raises(ValidationError):
        StructuredError(
            schema_version=2,
            code="spine.internal.unexpected",
            safe_message="An unexpected internal error occurred.",
            retryability=Retryability.NEVER,
            trace_id=synthetic_uuid(1),
        )

    with pytest.raises(ValidationError):
        StructuredError(
            code="unsafe code",
            safe_message="An unexpected internal error occurred.",
            retryability=Retryability.NEVER,
            trace_id=synthetic_uuid(1),
        )


@pytest.mark.parametrize("unsafe_message", ERROR_LEAK_CORPUS)
def test_structured_error_rejects_untrusted_safe_message(unsafe_message: str) -> None:
    with pytest.raises(ValidationError):
        StructuredError(
            code="spine.persistence.unavailable",
            safe_message=unsafe_message,
            retryability=Retryability.AFTER_DELAY,
            trace_id=synthetic_uuid(1),
        )


def test_structured_error_model_validate_rejects_injected_message() -> None:
    with pytest.raises(ValidationError):
        StructuredError.model_validate(
            {
                "schema_version": 1,
                "code": "spine.persistence.unavailable",
                "safe_message": "SELECT password FROM credentials",
                "retryability": "after_delay",
                "trace_id": synthetic_uuid(1),
            }
        )


def test_structured_error_rejects_mismatched_registered_code_and_message() -> None:
    with pytest.raises(ValidationError):
        StructuredError(
            code="spine.persistence.unavailable",
            safe_message="An unexpected internal error occurred.",
            retryability=Retryability.AFTER_DELAY,
            trace_id=synthetic_uuid(1),
        )


def test_structured_error_copy_revalidates_safe_message() -> None:
    error = StructuredError(
        code="spine.persistence.unavailable",
        safe_message="Persistence is temporarily unavailable.",
        retryability=Retryability.AFTER_DELAY,
        trace_id=synthetic_uuid(1),
    )

    with pytest.raises(ValidationError):
        error.model_copy(update={"safe_message": "Bearer production-token"})

    with pytest.raises(ValidationError):
        error.copy(update={"safe_message": "Bearer production-token"})


def test_structured_error_disables_unvalidated_model_construct() -> None:
    with pytest.raises(TypeError):
        StructuredError.model_construct(
            code="spine.persistence.unavailable",
            safe_message="protected document text",
            retryability=Retryability.NEVER,
            trace_id=synthetic_uuid(1),
        )


@pytest.mark.parametrize(
    ("failure_type", "expected_code", "expected_retryability"),
    [
        (PersistenceError, "spine.persistence.failure", Retryability.NEVER),
        (InvalidPersistenceContextError, "spine.persistence.invalid_context", Retryability.NEVER),
        (InvalidBootstrapAuthorityError, "spine.persistence.invalid_bootstrap_authority", Retryability.NEVER),
        (PersistenceUnavailableError, "spine.persistence.unavailable", Retryability.AFTER_DELAY),
        (RetryablePersistenceError, "spine.persistence.retryable_failure", Retryability.AFTER_DELAY),
        (TransactionDeadlockError, "spine.persistence.deadlock", Retryability.AFTER_DELAY),
        (TransactionSerializationError, "spine.persistence.serialization", Retryability.AFTER_DELAY),
        (OptimisticConflictError, "spine.persistence.optimistic_conflict", Retryability.NEVER),
        (ConstraintConflictError, "spine.persistence.constraint_conflict", Retryability.NEVER),
        (IdempotencyConflictError, "spine.persistence.idempotency_conflict", Retryability.NEVER),
        (OutboxConflictError, "spine.persistence.outbox_conflict", Retryability.NEVER),
        (AuditConflictError, "spine.persistence.audit_conflict", Retryability.NEVER),
        (IncompatibleSchemaError, "spine.persistence.incompatible_schema", Retryability.NEVER),
        (UnexpectedPersistenceError, "spine.persistence.unexpected", Retryability.NEVER),
        (UnitOfWorkLifecycleError, "spine.persistence.unit_of_work_lifecycle", Retryability.NEVER),
        (UnsupportedOutboxEventError, "spine.persistence.unsupported_outbox_event", Retryability.NEVER),
        (UnsupportedAuditEventError, "spine.persistence.unsupported_audit_event", Retryability.NEVER),
    ],
)
def test_default_registry_maps_each_persistence_category(
    failure_type: type[PersistenceError],
    expected_code: str,
    expected_retryability: Retryability,
) -> None:
    result = build_default_error_registry().map(failure_type("sensitive details"), diagnostic_context())

    assert result.code == expected_code
    assert result.retryability is expected_retryability
    assert result.trace_id == synthetic_uuid(1)
    assert_safe_structured_error(result)


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_retryability"),
    [
        (
            InvalidAuthenticationError(),
            "spine.authentication.invalid",
            Retryability.NEVER,
        ),
        (
            AuthenticationUnavailableError(),
            "spine.authentication.unavailable",
            Retryability.AFTER_DELAY,
        ),
        (
            AuthorizationDeniedError(),
            "spine.authorization.denied",
            Retryability.NEVER,
        ),
        (
            AuthorizationUnavailableError(),
            "spine.authorization.unavailable",
            Retryability.AFTER_DELAY,
        ),
    ],
)
def test_default_registry_maps_each_auth_category(
    failure: Exception,
    expected_code: str,
    expected_retryability: Retryability,
) -> None:
    result = build_default_error_registry().map(failure, diagnostic_context())

    assert result.code == expected_code
    assert result.retryability is expected_retryability
    assert result.trace_id == synthetic_uuid(1)
    assert_safe_structured_error(result)


class SensitiveError(Exception):
    def __str__(self) -> str:
        raise AssertionError("the mapper must not call str()")

    def __repr__(self) -> str:
        raise AssertionError("the mapper must not call repr()")


def test_unknown_error_is_generic_without_inspecting_or_leaking_exception() -> None:
    result = build_default_error_registry().map(SensitiveError(), diagnostic_context())

    assert result.code == "spine.internal.unexpected"
    assert result.retryability is Retryability.NEVER
    assert_safe_structured_error(result)
    assert all(value not in result.safe_message for value in ERROR_LEAK_CORPUS)


def test_registry_rejects_ambiguous_duplicate_exception_mappings() -> None:
    registry = StructuredErrorRegistry()
    registry.register(
        PersistenceError,
        "spine.persistence.failure",
        "Persistence operation failed.",
        Retryability.NEVER,
    )

    with pytest.raises(DuplicateExceptionMappingError):
        registry.register(
            PersistenceError,
            "spine.persistence.unavailable",
            "Persistence is temporarily unavailable.",
            Retryability.AFTER_DELAY,
        )


def test_registry_rejects_duplicate_codes() -> None:
    registry = StructuredErrorRegistry()
    registry.register(
        PersistenceError,
        "spine.persistence.failure",
        "Persistence operation failed.",
        Retryability.NEVER,
    )

    with pytest.raises(DuplicateErrorCodeError):
        registry.register(
            ValueError,
            "spine.persistence.failure",
            "Persistence operation failed.",
            Retryability.NEVER,
        )


def test_registry_rejects_unallowlisted_safe_message() -> None:
    with pytest.raises(ErrorRegistryConfigurationError):
        StructuredErrorRegistry().register(
            ValueError,
            "spine.persistence.failure",
            "SELECT password FROM credentials",
            Retryability.NEVER,
        )
