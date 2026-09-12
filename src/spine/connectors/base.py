"""Framework-independent connector contracts."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import Field

from spine.domain.common import SpineModel


class SourceRecord(SpineModel):
    external_id: str
    external_version: str | None = None
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = None
    deleted: bool = False


class SyncBatch(SpineModel):
    records: tuple[SourceRecord, ...] = ()
    next_cursor: str | None = None
    has_more: bool = False


class Connector(Protocol):
    """A source adapter with explicit cursor and no hidden global state."""

    async def sync(self, cursor: str | None, *, limit: int = 100) -> SyncBatch: ...

    async def get(self, external_id: str) -> SourceRecord | None: ...
