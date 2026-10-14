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


class TransactionDeadlockError(RetryablePersistenceError):
    """A translated transaction deadlock failure."""


class TransactionSerializationError(RetryablePersistenceError):
    """A translated transaction serialization failure."""


class OptimisticConflictError(PersistenceError):
    """An expected predecessor or version no longer matches."""


class ConstraintConflictError(PersistenceError):
    """A known persistence invariant rejected the requested change."""


class CanonicalAuthorityConflictError(ConstraintConflictError):
    """Caller-owned canonical snapshots differ from persisted authority."""


class CanonicalEligibilityError(ConstraintConflictError):
    """Current admission, receipt, policy, or disclosure evidence is ineligible."""


class CanonicalAcceptanceAuditUnavailableError(PersistenceUnavailableError):
    """A denied canonical attempt could not persist its required failed audit."""

    reason_code = "canonical_acceptance_audit_unavailable"

    def __init__(self) -> None:
        super().__init__(
            "Canonical acceptance remained denied; failed-attempt audit is unavailable."
        )


class ObservationIntegrityConflictError(ConstraintConflictError):
    """An observation identity was reused for different content."""


class RevisionDigestMismatchError(ConstraintConflictError):
    """Revision-bearing fields do not match the supplied canonical digest."""


class ObservationCommandDigestMismatchError(ConstraintConflictError):
    """Observation command fields do not match the supplied command digest."""


class IdempotencyConflictError(ConstraintConflictError):
    """An idempotency key was reused for different command content or scope."""


class OutboxConflictError(ConstraintConflictError):
    """An outbox event or producer deduplication identity already exists."""


class AuditConflictError(ConstraintConflictError):
    """An audit identity conflicts with an existing immutable event."""


class IncompatibleSchemaError(PersistenceError):
    """The canonical store schema is incompatible with this application."""


class UnexpectedPersistenceError(PersistenceError):
    """An unclassified persistence failure occurred."""


class UnitOfWorkLifecycleError(PersistenceError):
    """A Unit of Work was used outside its single-owner lifecycle."""


class ParsedArtifactConflictError(ConstraintConflictError):
    """A parsed artifact command or immutable identity conflicts with history."""


class ContextProfileError(PersistenceError):
    """Base class for safe, typed ContextProfile resolution failures."""


class ContextProfileNotFoundError(ContextProfileError):
    """No profile with the requested identity exists in the trusted scope."""


class ContextProfileDigestMismatchError(ContextProfileError):
    """The requested profile identity is present with another digest."""


class ContextProfileRevokedError(ContextProfileError):
    """The requested immutable profile version is revoked."""


class ContextProfileApprovalError(ContextProfileError):
    """The profile is not approved by the server for broker use."""


class ContextProfileMalformedError(ContextProfileError):
    """Persisted profile data is malformed or uses an unsupported schema."""
