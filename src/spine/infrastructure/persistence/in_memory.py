"""Deterministic transaction-aware in-memory persistence adapter."""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from types import TracebackType
from typing import NoReturn
from uuid import UUID

from spine.application.persistence.bootstrap import (
    InitialWorkspaceBootstrapAuthority,
)
from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceScope,
    TrustedPersistenceContext,
    TrustedContextVerifier,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidBootstrapAuthorityError,
    InvalidPersistenceContextError,
    UnitOfWorkLifecycleError,
)
from spine.domain.workspaces import Environment, Workspace


class _Store:
    def __init__(self, *, transaction_lock: asyncio.Lock | None = None) -> None:
        self.workspaces: dict[UUID, Workspace] = {}
        self.environments: dict[UUID, Environment] = {}
        self.lock = transaction_lock or asyncio.Lock()
        self.initialized = False
        self.bootstrap_sealed = False


def _workspace_copy(value: Workspace) -> Workspace:
    return value.model_copy(deep=True)


def _environment_copy(value: Environment) -> Environment:
    return value.model_copy(deep=True)


class _Lifecycle(Enum):
    NEW = auto()
    ENTERING = auto()
    ACTIVE = auto()
    COMMITTED = auto()
    ROLLED_BACK = auto()
    CLOSED = auto()


class _WorkspaceRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def add(self, workspace: Workspace) -> None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, WorkspaceScope) or workspace.id != scope.workspace_id:
            self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        if not self._uow._initialized:
            self._uow._fail(
                InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
            )
        if self._uow._resolve_workspace(workspace.id) is not None:
            self._uow._fail(ConstraintConflictError("Workspace identity already exists."))
        self._uow._pending_workspaces[workspace.id] = _workspace_copy(workspace)

    async def resolve(self, workspace_id: UUID) -> Workspace | None:
        self._uow._guard_active()
        if workspace_id != self._uow.scope.workspace_id:
            return None
        workspace = self._uow._resolve_workspace(workspace_id)
        return _workspace_copy(workspace) if workspace is not None else None


class _EnvironmentRepository:
    def __init__(self, uow: "InMemoryUnitOfWork") -> None:
        self._uow = uow

    async def add(self, environment: Environment) -> None:
        self._uow._guard_active()
        scope = self._uow.scope
        if not isinstance(scope, WorkspaceScope) or environment.workspace_id != scope.workspace_id:
            self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        if self._uow._resolve_workspace(scope.workspace_id) is None:
            self._uow._fail(ConstraintConflictError("Owning Workspace does not exist."))
        if self._uow._resolve_environment(environment.id) is not None:
            self._uow._fail(ConstraintConflictError("Environment identity already exists."))
        self._uow._pending_environments[environment.id] = _environment_copy(environment)

    async def resolve(self, environment_id: UUID) -> Environment | None:
        self._uow._guard_active()
        environment = self._uow._resolve_environment(environment_id)
        if environment is None:
            return None
        scope = self._uow.scope
        if environment.workspace_id != scope.workspace_id:
            return None
        if isinstance(scope, EnvironmentScope) and environment.id != scope.environment_id:
            return None
        return _environment_copy(environment)


class InMemoryUnitOfWork:
    def __init__(
        self,
        store: _Store,
        source_context: TrustedPersistenceContext,
        context_snapshot: TrustedPersistenceContext,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        if not isinstance(source_context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        self._store = store
        self._source_context = source_context
        self._context_snapshot = context_snapshot
        self._context_verifier = context_verifier
        self._scope: PersistenceScope | None = None
        self._initialized = False
        self._lifecycle = _Lifecycle.NEW
        self._owner: asyncio.Task[object] | None = None
        self._base_workspaces: dict[UUID, Workspace] = {}
        self._base_environments: dict[UUID, Environment] = {}
        self._pending_workspaces: dict[UUID, Workspace] = {}
        self._pending_environments: dict[UUID, Environment] = {}
        self._workspaces = _WorkspaceRepository(self)
        self._environments = _EnvironmentRepository(self)

    @property
    def workspaces(self) -> _WorkspaceRepository:
        self._guard_active()
        return self._workspaces

    @property
    def environments(self) -> _EnvironmentRepository:
        self._guard_active()
        return self._environments

    @property
    def scope(self) -> PersistenceScope:
        self._guard_active()
        if self._scope is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._scope

    async def __aenter__(self) -> "InMemoryUnitOfWork":
        if self._lifecycle is not _Lifecycle.NEW:
            raise UnitOfWorkLifecycleError("Unit of Work cannot be entered more than once.")
        self._lifecycle = _Lifecycle.ENTERING
        self._owner = asyncio.current_task()
        try:
            async with self._store.lock:
                context_snapshot = self._context_verifier.verify(self._source_context)
                self._base_workspaces = {
                    key: _workspace_copy(value) for key, value in self._store.workspaces.items()
                }
                self._base_environments = {
                    key: _environment_copy(value)
                    for key, value in self._store.environments.items()
                }
                self._initialized = self._store.initialized
                if not self._initialized:
                    raise InvalidBootstrapAuthorityError(
                        "Initial bootstrap is not authorized."
                    )
                self._validate_context(context_snapshot)
                self._context_snapshot = context_snapshot
        except BaseException:
            self._lifecycle = _Lifecycle.CLOSED
            self._clear_transaction()
            raise
        self._lifecycle = _Lifecycle.ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._lifecycle is _Lifecycle.CLOSED:
            return
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ACTIVE:
            await self.rollback()
        self._lifecycle = _Lifecycle.CLOSED
        self._clear_transaction()

    async def commit(self) -> None:
        self._guard_active()
        try:
            async with self._store.lock:
                self._validate_commit()
                self._store.workspaces.update(
                    {key: _workspace_copy(value) for key, value in self._pending_workspaces.items()}
                )
                self._store.environments.update(
                    {key: _environment_copy(value) for key, value in self._pending_environments.items()}
                )
        except BaseException:
            self._lifecycle = _Lifecycle.CLOSED
            self._clear_transaction()
            raise
        self._lifecycle = _Lifecycle.COMMITTED
        self._clear_transaction()

    async def rollback(self) -> None:
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ROLLED_BACK:
            return
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        self._pending_workspaces.clear()
        self._pending_environments.clear()
        self._lifecycle = _Lifecycle.ROLLED_BACK

    def _guard_owner(self) -> None:
        if self._owner is None or asyncio.current_task() is not self._owner:
            raise UnitOfWorkLifecycleError("Unit of Work may only be used by its owning task.")

    def _guard_active(self) -> None:
        self._guard_owner()
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")

    def _validate_context(self, context_snapshot: TrustedPersistenceContext) -> None:
        scope = context_snapshot.scope
        if isinstance(scope, WorkspaceScope):
            self._scope = WorkspaceScope(workspace_id=scope.workspace_id)
            return
        if not isinstance(scope, EnvironmentScope):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        environment = self._base_environments.get(scope.environment_id)
        if environment is None or environment.workspace_id != scope.workspace_id:
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        self._scope = EnvironmentScope(
            workspace_id=scope.workspace_id,
            environment_id=scope.environment_id,
        )

    def _validate_commit(self) -> None:
        if self._pending_workspaces and not self._store.initialized:
            raise InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
        if self._pending_workspaces.keys() & self._store.workspaces.keys():
            raise ConstraintConflictError("Workspace identity already exists.")
        if self._pending_environments.keys() & self._store.environments.keys():
            raise ConstraintConflictError("Environment identity already exists.")
        available_workspaces = self._store.workspaces.keys() | self._pending_workspaces.keys()
        if any(
            environment.workspace_id not in available_workspaces
            for environment in self._pending_environments.values()
        ):
            raise ConstraintConflictError("Owning Workspace does not exist.")

    def _resolve_workspace(self, workspace_id: UUID) -> Workspace | None:
        return self._pending_workspaces.get(workspace_id) or self._base_workspaces.get(workspace_id)

    def _resolve_environment(self, environment_id: UUID) -> Environment | None:
        return self._pending_environments.get(environment_id) or self._base_environments.get(
            environment_id
        )

    def _clear_transaction(self) -> None:
        self._base_workspaces.clear()
        self._base_environments.clear()
        self._pending_workspaces.clear()
        self._pending_environments.clear()
        self._scope = None

    def _fail(self, error: Exception) -> NoReturn:
        self._lifecycle = _Lifecycle.CLOSED
        self._clear_transaction()
        raise error


class _InMemoryUnitOfWorkFactory:
    def __init__(self, store: _Store, context_verifier: TrustedContextVerifier) -> None:
        self._store = store
        self._context_verifier = context_verifier

    def __call__(self, context: TrustedPersistenceContext) -> InMemoryUnitOfWork:
        if not isinstance(context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        context_snapshot = self._context_verifier.verify(context)
        return InMemoryUnitOfWork(
            self._store,
            context,
            context_snapshot,
            self._context_verifier,
        )


class _InMemoryInitialWorkspaceBootstrap:
    def __init__(
        self,
        store: _Store,
        expected_authority: InitialWorkspaceBootstrapAuthority | None,
    ) -> None:
        self._store = store
        self._expected_authority = expected_authority

    async def create_initial_workspace(
        self,
        authority: InitialWorkspaceBootstrapAuthority,
        workspace: Workspace,
    ) -> None:
        if self._expected_authority is None or authority is not self._expected_authority:
            raise InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
        async with self._store.lock:
            if self._store.bootstrap_sealed or self._store.workspaces:
                raise ConstraintConflictError("Initial Workspace bootstrap is sealed.")
            self._store.workspaces[workspace.id] = _workspace_copy(workspace)
            self._store.initialized = True
            self._store.bootstrap_sealed = True


class InMemoryPersistence:
    """Process-local deterministic adapter and its explicitly scoped entry points."""

    def __init__(
        self,
        *,
        context_verifier: TrustedContextVerifier,
        bootstrap_authority: InitialWorkspaceBootstrapAuthority | None = None,
        transaction_lock: asyncio.Lock | None = None,
    ) -> None:
        store = _Store(transaction_lock=transaction_lock)
        self.uow_factory = _InMemoryUnitOfWorkFactory(store, context_verifier)
        self.initial_workspace_bootstrap = _InMemoryInitialWorkspaceBootstrap(
            store, bootstrap_authority
        )
