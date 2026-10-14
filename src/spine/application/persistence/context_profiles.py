"""Application persistence port for server-approved ContextProfiles."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from spine.domain.context_profiles import ContextProfileVersion


class ContextProfileRepository(Protocol):
    async def add(self, profile: ContextProfileVersion) -> None: ...

    async def create(self, profile: ContextProfileVersion) -> ContextProfileVersion: ...

    async def resolve(
        self,
        *,
        profile_id: UUID,
        profile_version: int,
        profile_digest: str,
    ) -> ContextProfileVersion: ...


__all__ = ["ContextProfileRepository"]
