"""Purpose-specific canonical Workspace and Environment repositories."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.workspaces import Environment, Workspace
from spine.domain.sources import SourceObject, SourceRevision, SourceRevisionProvenance
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceObservationResult:
    source: SourceObject
    revision: SourceRevision
    provenance: SourceRevisionProvenance
    replay: bool


class SourceObservationRepository(Protocol):
    async def resolve_or_create_source(self, source: SourceObject) -> SourceObject: ...

    async def record_observation(
        self,
        revision: SourceRevision,
        provenance: SourceRevisionProvenance,
    ) -> SourceObservationResult: ...

    async def resolve_revision(self, revision_id: UUID) -> SourceRevision | None: ...
from spine.application.persistence.idempotency import IdempotencyRepository


class WorkspaceRepository(Protocol):
    async def add(self, workspace: Workspace) -> None: ...

    async def resolve(self, workspace_id: UUID) -> Workspace | None: ...


class EnvironmentRepository(Protocol):
    async def add(self, environment: Environment) -> None: ...

    async def resolve(self, environment_id: UUID) -> Environment | None: ...


__all__ = [
    "EnvironmentRepository",
    "IdempotencyRepository",
    "SourceObservationRepository",
    "SourceObservationResult",
    "WorkspaceRepository",
]
