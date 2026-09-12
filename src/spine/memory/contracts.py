"""Provider-neutral context and retrieval contracts."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pydantic import Field

from spine.domain.common import ActorRef, SpineModel


class RetrievalQuery(SpineModel):
    workspace_id: UUID
    query: str
    requested_by: ActorRef
    context_profile: str
    scopes: tuple[str, ...] = ()
    limit: int = Field(default=15, ge=1, le=100)


class ContextReference(SpineModel):
    source_id: str
    locator: dict[str, Any] = Field(default_factory=dict)
    excerpt: str | None = None


class RetrievalResult(SpineModel):
    context: tuple[str, ...] = ()
    entities: tuple[dict[str, Any], ...] = ()
    relations: tuple[dict[str, Any], ...] = ()
    references: tuple[ContextReference, ...] = ()
    retrieval_strategy: str
    index_version: str | None = None


class ContextBroker(Protocol):
    async def retrieve(self, query: RetrievalQuery) -> RetrievalResult: ...
