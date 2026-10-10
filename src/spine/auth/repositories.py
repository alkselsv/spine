"""Purpose-specific repository contract for current authorization snapshots."""

from __future__ import annotations

from typing import Protocol

from spine.auth.contracts import AuthenticationAlias, RequestedAuthorizationScope
from spine.auth.records import CurrentAuthorizationSnapshot


class AuthorizationSnapshotRepository(Protocol):
    """Resolve one exact alias and scope without enumeration or generic CRUD."""

    async def resolve_current_snapshot(
        self,
        alias: AuthenticationAlias,
        requested_scope: RequestedAuthorizationScope,
    ) -> CurrentAuthorizationSnapshot | None: ...


__all__ = ["AuthorizationSnapshotRepository"]
