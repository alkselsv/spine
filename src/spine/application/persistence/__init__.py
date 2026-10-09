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
from spine.application.persistence.errors import (
    ConstraintConflictError,
    IncompatibleSchemaError,
    InvalidBootstrapAuthorityError,
    InvalidPersistenceContextError,
    OptimisticConflictError,
    PersistenceError,
    PersistenceUnavailableError,
    RetryablePersistenceError,
    UnexpectedPersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.application.persistence.repositories import (
    EnvironmentRepository,
    WorkspaceRepository,
)
from spine.application.persistence.unit_of_work import UnitOfWork, UnitOfWorkFactory

__all__ = [
    "ContextOrigin",
    "EnvironmentScope",
    "EnvironmentRepository",
    "ConstraintConflictError",
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
    "TrustedContextProvenance",
    "TrustedContextVerifier",
    "TrustedPersistenceContext",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UnitOfWorkLifecycleError",
    "UnexpectedPersistenceError",
    "WorkspaceScope",
    "WorkspaceRepository",
]
