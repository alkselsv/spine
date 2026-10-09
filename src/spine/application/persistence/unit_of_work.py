"""Framework-independent asynchronous Unit of Work protocols."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol

from spine.application.persistence.context import TrustedPersistenceContext
from spine.application.persistence.repositories import (
    EnvironmentRepository,
    WorkspaceRepository,
)


class UnitOfWork(Protocol):
    @property
    def workspaces(self) -> WorkspaceRepository: ...

    @property
    def environments(self) -> EnvironmentRepository: ...

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self, context: TrustedPersistenceContext) -> UnitOfWork: ...
