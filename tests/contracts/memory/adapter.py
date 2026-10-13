from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from spine.memory.contracts import (
    ContextProjection,
    ProjectionChunk,
    ProjectionEntity,
    ProjectionRelation,
)


class RetrievalSeed(Protocol):
    """Provider-neutral fixture setup for structured retrieval tests."""

    def __call__(
        self,
        *,
        workspace_id: UUID,
        environment_id: UUID,
        chunks: tuple[ProjectionChunk, ...] = (),
        entities: tuple[ProjectionEntity, ...] = (),
        relations: tuple[ProjectionRelation, ...] = (),
        physical_namespace: str | None = None,
    ) -> None: ...


class ProjectionFactory(Protocol):
    """Build one implementation with deterministic physical failures."""

    def __call__(
        self,
        *,
        fail_once_for: tuple[str, ...] = (),
        fail_for: tuple[str, ...] = (),
    ) -> ContextProjection: ...


@dataclass(frozen=True, slots=True)
class ContextProjectionContractAdapter:
    """Implementation plus deterministic setup for the shared contract suite.

    A physical adapter can provide the same wrapper without changing the suite.
    The seed hook is test preparation, not an additional production port.
    """

    projection: ContextProjection
    seed_retrieval: RetrievalSeed
    create_projection: ProjectionFactory


ContextProjectionFactory = Callable[[], ContextProjectionContractAdapter]
