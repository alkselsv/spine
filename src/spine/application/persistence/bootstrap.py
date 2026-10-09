"""Narrow, sealed contract for establishing the first Workspace."""

from __future__ import annotations

from typing import Protocol

from spine.domain.workspaces import Workspace


class InitialWorkspaceBootstrapAuthority(Protocol):
    """Opaque capability bound to one adapter by a trusted composition root."""

    @property
    def bootstrap_authority_marker(self) -> None: ...


class InitialWorkspaceBootstrap(Protocol):
    async def create_initial_workspace(
        self,
        authority: InitialWorkspaceBootstrapAuthority,
        workspace: Workspace,
    ) -> None: ...
