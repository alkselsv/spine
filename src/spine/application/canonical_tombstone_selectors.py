"""Trusted tombstone writer selector contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from spine.domain.audit import AuditEvent
from spine.domain.sources.canonical_tombstone_selector import (
    CanonicalTombstoneSelectorHead,
    TrustedCanonicalTombstoneVersionSelector,
)


class CanonicalTombstoneSelectorError(RuntimeError):
    """A selector cannot be resolved or published safely."""


class CanonicalTombstoneSelectorRepository(Protocol):
    async def list_current(
        self, *, workspace_id: UUID, environment_id: UUID
    ) -> tuple[
        tuple[TrustedCanonicalTombstoneVersionSelector, CanonicalTombstoneSelectorHead],
        ...,
    ]: ...

    async def publish(
        self,
        selector: TrustedCanonicalTombstoneVersionSelector,
        *,
        expected_current_selector_id: UUID | None,
        expected_cas_version: int | None,
        recorded_at: datetime,
        audit_event: AuditEvent,
    ) -> object: ...


__all__ = [
    "CanonicalTombstoneSelectorError",
    "CanonicalTombstoneSelectorRepository",
]
