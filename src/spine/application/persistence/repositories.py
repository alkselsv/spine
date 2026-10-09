"""Purpose-specific canonical Workspace and Environment repositories."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.workspaces import Environment, Workspace
from spine.application.persistence.idempotency import IdempotencyRepository


class WorkspaceRepository(Protocol):
    async def add(self, workspace: Workspace) -> None: ...

    async def resolve(self, workspace_id: UUID) -> Workspace | None: ...


class EnvironmentRepository(Protocol):
    async def add(self, environment: Environment) -> None: ...

    async def resolve(self, environment_id: UUID) -> Environment | None: ...


__all__ = ["EnvironmentRepository", "IdempotencyRepository", "WorkspaceRepository"]
