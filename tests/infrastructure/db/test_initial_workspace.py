from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from spine.application.persistence.errors import (
    ConstraintConflictError,
    InvalidBootstrapAuthorityError,
)
from spine.domain.workspaces import Workspace
from spine.infrastructure.db.initial_workspace import (
    PostgreSQLInitialWorkspaceBootstrap,
)
from spine.infrastructure.db.settings import MigrationDatabaseSettings
from spine.infrastructure.persistence.contexts import (
    create_initial_workspace_bootstrap_authority,
)


MIGRATION_URL = "postgresql+psycopg://migration:secret@db/spine"


def workspace() -> Workspace:
    return Workspace(
        id=UUID("10000000-0000-0000-0000-000000000001"),
        slug="initial",
        display_name="Initial Workspace",
    )


class FakeConnection:
    def __init__(self, counts: Iterator[int]) -> None:
        self.counts = counts
        self.statements: list[tuple[str, object | None]] = []

    async def execute(self, statement: object, parameters: object | None = None) -> None:
        self.statements.append((str(statement), parameters))

    async def scalar(self, statement: object) -> int:
        self.statements.append((str(statement), None))
        return next(self.counts)


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield self.connection

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.asyncio
async def test_postgresql_bootstrap_rejects_foreign_authority_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = create_initial_workspace_bootstrap_authority()
    caller = create_initial_workspace_bootstrap_authority()
    adapter = PostgreSQLInitialWorkspaceBootstrap(
        settings=MigrationDatabaseSettings(url=MIGRATION_URL),
        authority=expected,
    )

    def unexpected_engine(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unauthorized bootstrap opened a database engine")

    monkeypatch.setattr(
        "spine.infrastructure.db.initial_workspace.create_async_engine",
        unexpected_engine,
    )

    with pytest.raises(InvalidBootstrapAuthorityError, match="not authorized"):
        await adapter.create_initial_workspace(caller, workspace())


@pytest.mark.asyncio
async def test_postgresql_bootstrap_creates_one_workspace_and_audit_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = create_initial_workspace_bootstrap_authority()
    connection = FakeConnection(iter([0]))
    engine = FakeEngine(connection)
    monkeypatch.setattr(
        "spine.infrastructure.db.initial_workspace.create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    adapter = PostgreSQLInitialWorkspaceBootstrap(
        settings=MigrationDatabaseSettings(url=MIGRATION_URL),
        authority=authority,
    )

    await adapter.create_initial_workspace(authority, workspace())

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "LOCK TABLE spine.initial_workspace_bootstrap" in sql
    assert "INSERT INTO spine.workspaces" in sql
    assert "INSERT INTO spine.initial_workspace_bootstrap" in sql
    assert "SELECT *" not in sql
    assert engine.disposed is True
    assert not hasattr(adapter, "list_workspaces")
    assert not hasattr(adapter, "mutate_workspace")


@pytest.mark.asyncio
async def test_postgresql_bootstrap_refuses_when_any_workspace_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = create_initial_workspace_bootstrap_authority()
    connection = FakeConnection(iter([1]))
    engine = FakeEngine(connection)
    monkeypatch.setattr(
        "spine.infrastructure.db.initial_workspace.create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    adapter = PostgreSQLInitialWorkspaceBootstrap(
        settings=MigrationDatabaseSettings(url=MIGRATION_URL),
        authority=authority,
    )

    with pytest.raises(ConstraintConflictError, match="sealed"):
        await adapter.create_initial_workspace(authority, workspace())

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "INSERT INTO spine.workspaces" not in sql
    assert engine.disposed is True
