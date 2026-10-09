"""Narrow, sealed contract for establishing the first Workspace."""

from __future__ import annotations

from typing import Protocol

from spine.application.persistence.errors import InvalidBootstrapAuthorityError
from spine.domain.workspaces import Workspace


_BOOTSTRAP_AUTHORITY_SEAL = object()


class InitialWorkspaceBootstrapAuthority:
    """Opaque one-purpose capability; it contains no tenant authority fields."""

    __slots__ = ("_seal",)

    def __init__(self, seal: object) -> None:
        if seal is not _BOOTSTRAP_AUTHORITY_SEAL:
            raise InvalidBootstrapAuthorityError("Initial bootstrap is not authorized.")
        self._seal = seal


def issue_initial_workspace_bootstrap_authority() -> InitialWorkspaceBootstrapAuthority:
    """Issue bootstrap authority from an explicitly authorized composition root."""

    return InitialWorkspaceBootstrapAuthority(_BOOTSTRAP_AUTHORITY_SEAL)


class InitialWorkspaceBootstrap(Protocol):
    async def create_initial_workspace(
        self,
        authority: InitialWorkspaceBootstrapAuthority,
        workspace: Workspace,
    ) -> None: ...
