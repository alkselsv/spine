from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from spine.infrastructure.db.operator import (
    OperatorBootstrapError,
    bootstrap_database_roles,
)
from spine.infrastructure.db.settings import OperatorDatabaseSettings


class FakeCursor:
    def __init__(
        self,
        result: tuple[object, ...] | list[tuple[object, ...]] | None,
    ) -> None:
        self.result = result

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.result if isinstance(self.result, tuple) else None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self.result if isinstance(self.result, list) else []


class FakeConnection:
    def __init__(
        self,
        rows: list[tuple[object, ...] | list[tuple[object, ...]] | None],
    ) -> None:
        self.rows = iter(rows)
        self.statements: list[tuple[object, tuple[object, ...] | None]] = []

    async def execute(
        self,
        statement: object,
        parameters: tuple[object, ...] | None = None,
    ) -> FakeCursor:
        self.statements.append((statement, parameters))
        rendered = str(statement)
        row = next(self.rows) if "SELECT" in rendered else (1,)
        return FakeCursor(row)


def settings() -> OperatorDatabaseSettings:
    return OperatorDatabaseSettings(
        url="postgresql+psycopg://operator:operator-secret@db/postgres",
        database_name="spine",
        migration_role="spine_migration",
        migration_password="migration-secret",
        runtime_role="spine_runtime",
        runtime_password="runtime-secret",
    )


@pytest.mark.asyncio
async def test_operator_bootstrap_creates_missing_roles_and_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            ("operator",),
            None,
            None,
            [],
            None,
        ]
    )

    @asynccontextmanager
    async def connect(_url: str):  # type: ignore[no-untyped-def]
        yield connection

    monkeypatch.setattr("spine.infrastructure.db.operator._connect", connect)

    await bootstrap_database_roles(settings())

    rendered = "\n".join(
        statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        for statement, _ in connection.statements
        if "ALTER ROLE" not in str(statement)
    )
    assert "CREATE ROLE \"spine_migration\" LOGIN" in rendered
    assert "CREATE ROLE \"spine_runtime\" LOGIN" in rendered
    assert "CREATE DATABASE \"spine\" OWNER \"spine_migration\"" in rendered
    assert "migration-secret" not in rendered
    assert "runtime-secret" not in rendered
    assert (
        sum("ALTER ROLE" in str(statement) for statement, _ in connection.statements)
        == 2
    )


@pytest.mark.asyncio
async def test_operator_bootstrap_revokes_existing_runtime_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        [
            ("operator",),
            (1,),
            (1,),
            [("spine_migration",), ("legacy_admin",)],
            ("operator",),
        ]
    )

    @asynccontextmanager
    async def connect(_url: str):  # type: ignore[no-untyped-def]
        yield connection

    monkeypatch.setattr("spine.infrastructure.db.operator._connect", connect)

    await bootstrap_database_roles(settings())

    rendered = "\n".join(
        statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        for statement, _ in connection.statements
        if "ALTER ROLE" not in str(statement)
    )
    assert 'REVOKE "spine_migration" FROM "spine_runtime"' in rendered
    assert 'REVOKE "legacy_admin" FROM "spine_runtime"' in rendered


@pytest.mark.asyncio
async def test_operator_bootstrap_failure_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def failing_connect(_url: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("operator-secret")
        yield

    monkeypatch.setattr("spine.infrastructure.db.operator._connect", failing_connect)

    with pytest.raises(OperatorBootstrapError) as error:
        await bootstrap_database_roles(settings())

    assert str(error.value) == "database operator bootstrap failed"
    assert error.value.__cause__ is None
