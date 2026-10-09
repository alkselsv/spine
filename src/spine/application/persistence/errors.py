"""Stable application-facing persistence failures.

Messages in this module deliberately describe categories rather than values. In
particular, tenant identifiers and provider error details must not cross this
boundary.
"""


class PersistenceError(Exception):
    """Base class for failures exposed by the persistence seam."""


class InvalidPersistenceContextError(PersistenceError):
    """The supplied trusted context is malformed or internally inconsistent."""


class InvalidBootstrapAuthorityError(PersistenceError):
    """The sealed bootstrap capability is absent or invalid."""


class PersistenceUnavailableError(PersistenceError):
    """The persistence service or capacity is unavailable."""


class RetryablePersistenceError(PersistenceError):
    """The whole transaction may be retried by an eligible caller."""


class OptimisticConflictError(PersistenceError):
    """An expected predecessor or version no longer matches."""


class ConstraintConflictError(PersistenceError):
    """A known persistence invariant rejected the requested change."""


class IncompatibleSchemaError(PersistenceError):
    """The canonical store schema is incompatible with this application."""


class UnexpectedPersistenceError(PersistenceError):
    """An unclassified persistence failure occurred."""


class UnitOfWorkLifecycleError(PersistenceError):
    """A Unit of Work was used outside its single-owner lifecycle."""
