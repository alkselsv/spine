"""Application persistence seam for immutable ContextBundles."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.context_bundles import ContextBundle


class ContextBundleRepository(Protocol):
    """Persist and safely replay one exact broker handoff."""

    async def add(self, bundle: ContextBundle) -> None: ...

    async def create(self, bundle: ContextBundle) -> ContextBundle: ...

    async def resolve(
        self,
        *,
        bundle_id: UUID,
        bundle_version: int,
        bundle_digest: str,
    ) -> ContextBundle: ...

    async def resolve_attempt(
        self,
        *,
        run_id: UUID,
        attempt_id: UUID,
        idempotency_key: str,
    ) -> ContextBundle: ...


__all__ = ["ContextBundleRepository"]
