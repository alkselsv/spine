from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, TimeoutError as SQLAlchemyTimeoutError

from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    IncompatibleSchemaError,
    OptimisticConflictError,
    PersistenceUnavailableError,
    TransactionDeadlockError,
    TransactionSerializationError,
    UnexpectedPersistenceError,
)
from spine.infrastructure.persistence.postgresql import translate_persistence_error
from spine.infrastructure.persistence.postgresql_errors import (
    require_compare_and_swap_match,
)


class ProviderError(Exception):
    def __init__(
        self,
        *,
        sqlstate: str | None = None,
        constraint_name: str | None = None,
    ) -> None:
        super().__init__("provider detail containing protected values")
        self.sqlstate = sqlstate
        self.diag = type("Diagnostic", (), {"constraint_name": constraint_name})()


@pytest.mark.parametrize(
    ("source", "expected_type", "expected_message"),
    [
        (
            DBAPIError.instance(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(sqlstate="40P01"),
                Exception,
            ),
            TransactionDeadlockError,
            "Persistence transaction deadlocked.",
        ),
        (
            DBAPIError.instance(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(sqlstate="40001"),
                Exception,
            ),
            TransactionSerializationError,
            "Persistence transaction could not be serialized.",
        ),
        (
            DBAPIError.instance(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(sqlstate="42P01"),
                Exception,
            ),
            IncompatibleSchemaError,
            "Persistence schema is incompatible.",
        ),
        (
            IntegrityError(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(constraint_name="pk_workspaces"),
            ),
            ConstraintConflictError,
            "Workspace identity already exists.",
        ),
        (
            IntegrityError(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(
                    constraint_name="fk_environments_workspace_id_workspaces"
                ),
            ),
            ConstraintConflictError,
            "Owning Workspace does not exist.",
        ),
        (
            IntegrityError(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(constraint_name="uq_workspaces_slug"),
            ),
            ConstraintConflictError,
            "Workspace identity already exists.",
        ),
        (
            IntegrityError(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(constraint_name="ck_workspaces_slug_not_empty"),
            ),
            ConstraintConflictError,
            "Workspace data violates persistence constraints.",
        ),
        (
            IntegrityError(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(
                    constraint_name="uq_idempotency_receipts_environment_key"
                ),
            ),
            IdempotencyConflictError,
            "Idempotency key conflicts with existing command.",
        ),
        (
            IntegrityError(
                "protected SQL",
                {"secret": "protected"},
                ProviderError(
                    constraint_name="fk_idempotency_receipts_scope_environments"
                ),
            ),
            ConstraintConflictError,
            "Environment does not belong to Workspace.",
        ),
        (
            SQLAlchemyTimeoutError("pool detail"),
            PersistenceUnavailableError,
            "Persistence service is unavailable.",
        ),
        (
            RuntimeError("unknown protected detail"),
            UnexpectedPersistenceError,
            "Persistence operation failed unexpectedly.",
        ),
    ],
)
def test_provider_failure_maps_to_stable_redacted_category(
    source: BaseException,
    expected_type: type[Exception],
    expected_message: str,
) -> None:
    translated = translate_persistence_error(source)

    assert type(translated) is expected_type
    assert str(translated) == expected_message
    assert "protected" not in str(translated)
    assert translated.__cause__ is None


def test_compare_and_swap_miss_maps_to_optimistic_conflict() -> None:
    with pytest.raises(
        OptimisticConflictError,
        match="Persistence version no longer matches",
    ):
        require_compare_and_swap_match(0)


def test_compare_and_swap_rejects_ambiguous_row_count() -> None:
    with pytest.raises(
        UnexpectedPersistenceError,
        match="Persistence operation failed unexpectedly",
    ):
        require_compare_and_swap_match(2)

    require_compare_and_swap_match(1)
