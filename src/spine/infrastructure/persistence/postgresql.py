"""PostgreSQL implementation of the application persistence seam."""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from types import TracebackType
from typing import NoReturn
from uuid import UUID

from sqlalchemy import insert, select, text

from spine.application.persistence.context import (
    EnvironmentScope,
    PersistenceScope,
    TrustedContextVerifier,
    TrustedPersistenceContext,
    WorkspaceScope,
)
from spine.application.persistence.errors import (
    InvalidPersistenceContextError,
    PersistenceError,
    UnitOfWorkLifecycleError,
)
from spine.domain.common import EnvironmentKind
from spine.domain.workspaces import Environment, Workspace
from spine.infrastructure.db.engine import SessionFactory
from spine.infrastructure.persistence.postgresql_errors import (
    translate_persistence_error,
)
from spine.infrastructure.persistence.postgresql_mappings import (
    environments as _ENVIRONMENTS,
    workspaces as _WORKSPACES,
)

_BIND_CONTEXT = text(
    "SELECT "
    "set_config('spine.workspace_id', :workspace_id, true), "
    "set_config('spine.environment_id', :environment_id, true), "
    "set_config('spine.acting_subject_id', :acting_subject_id, true), "
    "set_config('spine.service_principal_id', :service_principal_id, true), "
    "set_config('spine.purpose', :purpose, true), "
    "set_config('spine.operation', :operation, true), "
    "set_config('spine.trace_id', :trace_id, true)"
)
_BIND_ENVIRONMENT = text(
    "SELECT set_config('spine.environment_id', :environment_id, true)"
)


class _Lifecycle(Enum):
    NEW = auto()
    ENTERING = auto()
    ACTIVE = auto()
    COMMITTED = auto()
    ROLLED_BACK = auto()
    CLOSED = auto()


class _PostgreSQLWorkspaceRepository:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    async def add(self, workspace: Workspace) -> None:
        self._uow._guard_active()
        scope = self._uow.scope
        if (
            type(workspace) is not Workspace
            or not isinstance(scope, WorkspaceScope)
            or workspace.id != scope.workspace_id
        ):
            await self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        await self._uow._execute(
            insert(_WORKSPACES).values(
                id=workspace.id,
                slug=workspace.slug,
                display_name=workspace.display_name,
            )
        )

    async def resolve(self, workspace_id: UUID) -> Workspace | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if workspace_id != scope.workspace_id:
            return None
        result = await self._uow._execute(
            select(
                _WORKSPACES.c.id,
                _WORKSPACES.c.slug,
                _WORKSPACES.c.display_name,
            ).where(
                _WORKSPACES.c.id == workspace_id,
                _WORKSPACES.c.id == scope.workspace_id,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return Workspace(id=row.id, slug=row.slug, display_name=row.display_name)


class _PostgreSQLEnvironmentRepository:
    def __init__(self, uow: "PostgreSQLTenantUnitOfWork") -> None:
        self._uow = uow

    async def add(self, environment: Environment) -> None:
        self._uow._guard_active()
        scope = self._uow.scope
        if (
            type(environment) is not Environment
            or not isinstance(scope, WorkspaceScope)
            or environment.workspace_id != scope.workspace_id
        ):
            await self._uow._fail(
                InvalidPersistenceContextError("Persistence context is invalid.")
            )
        await self._uow._bind_environment(environment.id)
        await self._uow._execute(
            insert(_ENVIRONMENTS).values(
                id=environment.id,
                workspace_id=environment.workspace_id,
                kind=environment.kind.value,
                display_name=environment.display_name,
            )
        )

    async def resolve(self, environment_id: UUID) -> Environment | None:
        self._uow._guard_active()
        scope = self._uow.scope
        if isinstance(scope, EnvironmentScope) and environment_id != scope.environment_id:
            return None
        if isinstance(scope, WorkspaceScope):
            await self._uow._bind_environment(environment_id)
        result = await self._uow._execute(
            select(
                _ENVIRONMENTS.c.id,
                _ENVIRONMENTS.c.workspace_id,
                _ENVIRONMENTS.c.kind,
                _ENVIRONMENTS.c.display_name,
            ).where(
                _ENVIRONMENTS.c.id == environment_id,
                _ENVIRONMENTS.c.workspace_id == scope.workspace_id,
            )
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return Environment(
            id=row.id,
            workspace_id=row.workspace_id,
            kind=EnvironmentKind(row.kind),
            display_name=row.display_name,
        )


class PostgreSQLTenantUnitOfWork:
    """Own one PostgreSQL session and transaction for one trusted operation."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        source_context: TrustedPersistenceContext,
        context_snapshot: TrustedPersistenceContext,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._source_context = source_context
        self._context_snapshot = context_snapshot
        self._context_verifier = context_verifier
        self._scope: PersistenceScope | None = None
        self._session = None
        self._lifecycle = _Lifecycle.NEW
        self._owner: asyncio.Task[object] | None = None
        self._workspaces = _PostgreSQLWorkspaceRepository(self)
        self._environments = _PostgreSQLEnvironmentRepository(self)

    @property
    def scope(self) -> PersistenceScope:
        self._guard_active()
        if self._scope is None:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        return self._scope

    @property
    def workspaces(self) -> _PostgreSQLWorkspaceRepository:
        self._guard_active()
        return self._workspaces

    @property
    def environments(self) -> _PostgreSQLEnvironmentRepository:
        self._guard_active()
        return self._environments

    async def __aenter__(self) -> "PostgreSQLTenantUnitOfWork":
        if self._lifecycle is not _Lifecycle.NEW:
            raise UnitOfWorkLifecycleError(
                "Unit of Work cannot be entered more than once."
            )
        self._lifecycle = _Lifecycle.ENTERING
        self._owner = asyncio.current_task()
        try:
            snapshot = self._context_verifier.verify(self._source_context)
            if snapshot != self._context_snapshot:
                raise InvalidPersistenceContextError(
                    "Persistence context is invalid."
                )
            self._session = self._session_factory()
            await self._session.begin()
            await self._bind_context(snapshot)
            self._scope = snapshot.scope
            if isinstance(snapshot.scope, EnvironmentScope):
                await self._validate_environment_scope(snapshot.scope)
        except BaseException as error:
            await self._terminate(error)
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
        await self._close()

    async def commit(self) -> None:
        self._guard_active()
        assert self._session is not None
        try:
            await self._session.commit()
        except BaseException as error:
            await self._terminate(error)
        self._lifecycle = _Lifecycle.COMMITTED

    async def rollback(self) -> None:
        self._guard_owner()
        if self._lifecycle is _Lifecycle.ROLLED_BACK:
            return
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")
        assert self._session is not None
        try:
            await self._session.rollback()
        except BaseException as error:
            await self._terminate(error)
        self._lifecycle = _Lifecycle.ROLLED_BACK

    def _guard_owner(self) -> None:
        if self._owner is None or asyncio.current_task() is not self._owner:
            raise UnitOfWorkLifecycleError(
                "Unit of Work may only be used by its owning task."
            )

    def _guard_active(self) -> None:
        self._guard_owner()
        if self._lifecycle is not _Lifecycle.ACTIVE:
            raise UnitOfWorkLifecycleError("Unit of Work is not active.")

    async def _bind_context(self, context: TrustedPersistenceContext) -> None:
        scope = context.scope
        environment_id = (
            str(scope.environment_id) if isinstance(scope, EnvironmentScope) else ""
        )
        await self._execute_during_entry(
            _BIND_CONTEXT,
            {
                "workspace_id": str(scope.workspace_id),
                "environment_id": environment_id,
                "acting_subject_id": str(context.acting_subject_id or ""),
                "service_principal_id": str(context.service_principal_id or ""),
                "purpose": context.purpose.value,
                "operation": context.operation.value,
                "trace_id": str(context.trace_id),
            },
        )

    async def _validate_environment_scope(self, scope: EnvironmentScope) -> None:
        result = await self._execute_during_entry(
            select(_ENVIRONMENTS.c.id).where(
                _ENVIRONMENTS.c.id == scope.environment_id,
                _ENVIRONMENTS.c.workspace_id == scope.workspace_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise InvalidPersistenceContextError("Persistence context is invalid.")

    async def _bind_environment(self, environment_id: UUID) -> None:
        await self._execute(
            _BIND_ENVIRONMENT,
            {"environment_id": str(environment_id)},
        )

    async def _execute(self, statement: object, parameters: object | None = None):
        self._guard_active()
        assert self._session is not None
        try:
            return await self._session.execute(statement, parameters)
        except BaseException as error:
            await self._terminate(error)

    async def _execute_during_entry(
        self, statement: object, parameters: object | None = None
    ):
        assert self._session is not None
        return await self._session.execute(statement, parameters)

    async def _fail(self, error: BaseException) -> NoReturn:
        await self._terminate(error)

    async def _terminate(self, error: BaseException) -> NoReturn:
        self._lifecycle = _Lifecycle.CLOSED
        session = self._session
        self._scope = None
        if session is not None:
            try:
                await session.rollback()
            except BaseException:
                pass
            try:
                await session.close()
            except BaseException:
                pass
            self._session = None
        if isinstance(error, asyncio.CancelledError):
            raise error
        if isinstance(error, PersistenceError):
            raise error
        if isinstance(error, Exception):
            raise translate_persistence_error(error) from None
        raise error

    async def _close(self) -> None:
        session = self._session
        self._scope = None
        self._session = None
        self._lifecycle = _Lifecycle.CLOSED
        if session is not None:
            try:
                await session.close()
            except Exception as error:
                raise translate_persistence_error(error) from None


class PostgreSQLTenantUnitOfWorkFactory:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self._session_factory = session_factory
        self._context_verifier = context_verifier

    def __call__(
        self, context: TrustedPersistenceContext
    ) -> PostgreSQLTenantUnitOfWork:
        if not isinstance(context, TrustedPersistenceContext):
            raise InvalidPersistenceContextError("Persistence context is invalid.")
        snapshot = self._context_verifier.verify(context)
        return PostgreSQLTenantUnitOfWork(
            session_factory=self._session_factory,
            source_context=context,
            context_snapshot=snapshot,
            context_verifier=self._context_verifier,
        )


class PostgreSQLPersistence:
    """Production persistence adapter assembled from process database resources."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        context_verifier: TrustedContextVerifier,
    ) -> None:
        self.tenant_uow_factory = PostgreSQLTenantUnitOfWorkFactory(
            session_factory=session_factory,
            context_verifier=context_verifier,
        )


__all__ = [
    "PostgreSQLPersistence",
    "PostgreSQLTenantUnitOfWork",
    "PostgreSQLTenantUnitOfWorkFactory",
    "translate_persistence_error",
]
