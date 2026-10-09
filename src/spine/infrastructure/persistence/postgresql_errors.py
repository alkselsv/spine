"""Stable application error translation for the PostgreSQL adapter."""

from __future__ import annotations

from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    OperationalError,
    TimeoutError as SQLAlchemyTimeoutError,
)

from spine.application.persistence.errors import (
    ConstraintConflictError,
    IncompatibleSchemaError,
    OptimisticConflictError,
    PersistenceError,
    PersistenceUnavailableError,
    TransactionDeadlockError,
    TransactionSerializationError,
    UnexpectedPersistenceError,
)


_CONSTRAINT_MESSAGES = {
    "pk_workspaces": "Workspace identity already exists.",
    "uq_workspaces_slug": "Workspace identity already exists.",
    "ck_workspaces_slug_not_empty": "Workspace data violates persistence constraints.",
    "ck_workspaces_display_name_not_empty": (
        "Workspace data violates persistence constraints."
    ),
    "pk_environments": "Environment identity already exists.",
    "uq_environments_workspace_id_id": "Environment identity already exists.",
    "ck_environments_kind": "Environment data violates persistence constraints.",
    "ck_environments_display_name_not_empty": (
        "Environment data violates persistence constraints."
    ),
    "fk_environments_workspace_id_workspaces": "Owning Workspace does not exist.",
}


def translate_persistence_error(error: BaseException) -> PersistenceError:
    """Translate provider failures without retaining SQL or bound values."""

    if isinstance(error, PersistenceError):
        return error

    original = error.orig if isinstance(error, DBAPIError) else error
    sqlstate = getattr(original, "sqlstate", None)
    if sqlstate == "40P01":
        return TransactionDeadlockError("Persistence transaction deadlocked.")
    if sqlstate == "40001":
        return TransactionSerializationError(
            "Persistence transaction could not be serialized."
        )
    if sqlstate in {"3F000", "42P01", "42703"}:
        return IncompatibleSchemaError("Persistence schema is incompatible.")

    if isinstance(error, IntegrityError):
        diagnostic = getattr(original, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None)
        message = _CONSTRAINT_MESSAGES.get(
            constraint_name,
            "Persistence constraint rejected the operation.",
        )
        return ConstraintConflictError(message)

    if isinstance(error, (SQLAlchemyTimeoutError, OperationalError)) or (
        isinstance(error, DBAPIError) and error.connection_invalidated
    ):
        return PersistenceUnavailableError("Persistence service is unavailable.")

    return UnexpectedPersistenceError("Persistence operation failed unexpectedly.")


def require_compare_and_swap_match(rowcount: int) -> None:
    """Translate a public compare-and-swap outcome into a stable category."""

    if rowcount == 1:
        return
    if rowcount == 0:
        raise OptimisticConflictError("Persistence version no longer matches.")
    raise UnexpectedPersistenceError("Persistence operation failed unexpectedly.")


__all__ = ["require_compare_and_swap_match", "translate_persistence_error"]
