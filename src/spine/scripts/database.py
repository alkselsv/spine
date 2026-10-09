"""Explicit operator commands; application startup never imports this module."""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from spine.domain.workspaces import Workspace
from spine.infrastructure.db.initial_workspace import (
    PostgreSQLInitialWorkspaceBootstrap,
)
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
)
from spine.infrastructure.persistence.contexts import (
    create_initial_workspace_bootstrap_authority,
)


def operator_bootstrap_main() -> None:
    asyncio.run(bootstrap_database_roles(OperatorDatabaseSettings()))


def migrate_main() -> None:
    upgrade_database(MigrationDatabaseSettings())


def initial_workspace_main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and permanently seal Spine's initial Workspace."
    )
    parser.add_argument("--id", type=UUID, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--display-name", required=True)
    arguments = parser.parse_args()
    authority = create_initial_workspace_bootstrap_authority()
    adapter = PostgreSQLInitialWorkspaceBootstrap(
        settings=MigrationDatabaseSettings(),
        authority=authority,
    )
    asyncio.run(
        adapter.create_initial_workspace(
            authority,
            Workspace(
                id=arguments.id,
                slug=arguments.slug,
                display_name=arguments.display_name,
            ),
        )
    )
