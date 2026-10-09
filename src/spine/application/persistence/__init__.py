"""Framework-independent persistence contracts for application use cases."""

from spine.application.persistence.bootstrap import (
    InitialWorkspaceBootstrap,
    InitialWorkspaceBootstrapAuthority,
)
from spine.application.persistence.context import (
    ContextOrigin,
    EnvironmentScope,
    PersistenceOperation,
    PersistencePurpose,
    TrustedContextProvenance,
    TrustedContextVerifier,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.command_digest import (
    COMMAND_DIGEST_ALGORITHM_VERSION,
    CommandDigest,
    UnsupportedCommandValueError,
    canonical_command_bytes,
    digest_command,
)
from spine.application.persistence.errors import (
    ConstraintConflictError,
    IdempotencyConflictError,
    IncompatibleSchemaError,
    InvalidBootstrapAuthorityError,
    InvalidPersistenceContextError,
    OptimisticConflictError,
    PersistenceError,
    PersistenceUnavailableError,
    RetryablePersistenceError,
    TransactionDeadlockError,
    TransactionSerializationError,
    UnexpectedPersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.application.persistence.idempotency import (
    IdempotencyClaimResult,
    IdempotencyKey,
    IdempotencyOperation,
    IdempotencyReplay,
    OpaqueResultReference,
    OwnedIdempotencyClaim,
)
from spine.application.persistence.repositories import (
    EnvironmentRepository,
    IdempotencyRepository,
    WorkspaceRepository,
)
from spine.application.persistence.retry import (
    RetryEligibility,
    TransactionRetryPolicy,
    run_with_transaction_retry,
)
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory

__all__ = [
    "ContextOrigin",
    "COMMAND_DIGEST_ALGORITHM_VERSION",
    "CommandDigest",
    "EnvironmentScope",
    "EnvironmentRepository",
    "ConstraintConflictError",
    "IdempotencyClaimResult",
    "IdempotencyConflictError",
    "IdempotencyKey",
    "IdempotencyOperation",
    "IdempotencyReplay",
    "IdempotencyRepository",
    "IncompatibleSchemaError",
    "InitialWorkspaceBootstrap",
    "InitialWorkspaceBootstrapAuthority",
    "InvalidBootstrapAuthorityError",
    "InvalidPersistenceContextError",
    "OptimisticConflictError",
    "PersistenceError",
    "PersistenceUnavailableError",
    "RetryablePersistenceError",
    "PersistenceOperation",
    "PersistencePurpose",
    "OpaqueResultReference",
    "OwnedIdempotencyClaim",
    "RetryEligibility",
    "TransactionRetryPolicy",
    "TransactionDeadlockError",
    "TransactionSerializationError",
    "TrustedContextProvenance",
    "TrustedContextVerifier",
    "TrustedPersistenceContext",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UnitOfWorkLifecycleError",
    "UnsupportedCommandValueError",
    "UnexpectedPersistenceError",
    "WorkspaceScope",
    "WorkspaceRepository",
    "canonical_command_bytes",
    "digest_command",
    "run_with_transaction_retry",
]
