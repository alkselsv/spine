"""Framework-independent persistence contracts for application use cases."""

from spine.application.persistence.bootstrap import (
    InitialWorkspaceBootstrap,
    InitialWorkspaceBootstrapAuthority,
    issue_initial_workspace_bootstrap_authority,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    TrustedContextAuthority,
    TrustedPersistenceContext,
    WorkspaceScope,
    issue_trusted_context_authority,
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
    "TrustedContextAuthority",
    "TrustedPersistenceContext",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "UnitOfWorkLifecycleError",
    "UnexpectedPersistenceError",
    "WorkspaceScope",
    "WorkspaceRepository",
    "issue_initial_workspace_bootstrap_authority",
    "issue_trusted_context_authority",
]
