"""Migration-authority adapter for the one-time initial Workspace action."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from spine.application.persistence.bootstrap import InitialWorkspaceBootstrapAuthority
from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidBootstrapAuthorityError,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
)


class PostgreSQLInitialWorkspaceBootstrap:
    """Expose exactly one sealed create action under migration credentials."""

    __slots__ = ("_authority", "_settings")

    def __init__(
        self,
        *,
        settings: MigrationDatabaseSettings,
        authority: InitialWorkspaceBootstrapAuthority,
    ) -> None:
        self._settings = settings
        self._authority = authority

    async def create_initial_workspace(
        self,
        authority: InitialWorkspaceBootstrapAuthority,
        workspace: Workspace,
    ) -> None:
        if authority is not self._authority or type(workspace) is not Workspace:
            raise InvalidBootstrapAuthorityError(
                "Initial Workspace bootstrap is not authorized."
            )

        engine = create_async_engine(
            self._settings.url.get_secret_value(),
            hide_parameters=True,
            pool_pre_ping=True,
            connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
        )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "LOCK TABLE spine.initial_workspace_bootstrap "
                        "IN ACCESS EXCLUSIVE MODE"
                    )
                )
                count = await connection.scalar(
                    text(
                        "SELECT (SELECT count(*) FROM spine.workspaces) + "
                        "(SELECT count(*) FROM spine.initial_workspace_bootstrap)"
                    )
                )
                if count != 0:
                    raise ConstraintConflictError(
                        "Initial Workspace bootstrap is sealed."
                    )
                await connection.execute(
                    text(
                        "INSERT INTO spine.workspaces (id, slug, display_name) "
                        "VALUES (:id, :slug, :display_name)"
                    ),
                    {
                        "id": workspace.id,
                        "slug": workspace.slug,
                        "display_name": workspace.display_name,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO spine.initial_workspace_bootstrap "
                        "(singleton, action_id, workspace_id, executed_by) "
                        "VALUES (true, :action_id, :workspace_id, current_user)"
                    ),
                    {"action_id": uuid4(), "workspace_id": workspace.id},
                )
        finally:
            await engine.dispose()
