from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from spine.infrastructure.db.engine import DatabaseRuntime, DatabaseStartupError
from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import bootstrap_database_roles
from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
    RuntimeDatabaseSettings,
)
from spine.infrastructure.db.test_harness import TestDatabaseProvision

pytestmark = pytest.mark.postgresql

MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
MIGRATION_PASSWORD = "migration-readiness-secret"
RUNTIME_PASSWORD = "runtime-readiness-secret"
HEAD_REVISION = "20261010_06"


@dataclass(frozen=True, slots=True)
class ReadinessDatabase:
    migration_url: SecretStr
    runtime_url: SecretStr


def _role_url(provision: TestDatabaseProvision, role: str, password: str) -> SecretStr:
    value = make_url(provision.url.get_secret_value()).set(
        username=role,
        password=password,
    )
    return SecretStr(value.render_as_string(hide_password=False))


@asynccontextmanager
async def _connection(url: SecretStr) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def readiness_database(
    postgresql_provision: TestDatabaseProvision,
) -> ReadinessDatabase:
    operator = OperatorDatabaseSettings(
        url=postgresql_provision.url,
        database_name=postgresql_provision.target.database_name,
        migration_role=MIGRATION_ROLE,
        migration_password=MIGRATION_PASSWORD,
        runtime_role=RUNTIME_ROLE,
        runtime_password=RUNTIME_PASSWORD,
    )
    await bootstrap_database_roles(operator)
    migration_url = _role_url(
        postgresql_provision,
        MIGRATION_ROLE,
        MIGRATION_PASSWORD,
    )
    await asyncio.to_thread(
        upgrade_database,
        MigrationDatabaseSettings(
            url=migration_url,
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
        ),
    )
    yield ReadinessDatabase(
        migration_url=migration_url,
        runtime_url=_role_url(
            postgresql_provision,
            RUNTIME_ROLE,
            RUNTIME_PASSWORD,
        ),
    )
    await _replace_database_heads(migration_url, (HEAD_REVISION,))


def _runtime_settings(url: SecretStr, *, role: str = RUNTIME_ROLE) -> RuntimeDatabaseSettings:
    return RuntimeDatabaseSettings(
        url=url,
        runtime_role=role,
        pool_size=1,
        max_overflow=0,
        connect_timeout_seconds=5,
    )


async def _replace_database_heads(
    migration_url: SecretStr,
    heads: tuple[str, ...],
) -> None:
    async with _connection(migration_url) as connection:
        await connection.execute(text("DELETE FROM spine.alembic_version"))
        for head in heads:
            await connection.execute(
                text(
                    "INSERT INTO spine.alembic_version (version_num) VALUES (:head)"
                ),
                {"head": head},
            )
        await connection.commit()


async def _database_heads(migration_url: SecretStr) -> tuple[str, ...]:
    async with _connection(migration_url) as connection:
        return tuple(
            (
                await connection.execute(
                    text(
                        "SELECT version_num FROM spine.alembic_version "
                        "ORDER BY version_num"
                    )
                )
            ).scalars()
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_is_ready_at_exact_supported_head(
    readiness_database: ReadinessDatabase,
) -> None:
    runtime = DatabaseRuntime()

    resources = await runtime.start(_runtime_settings(readiness_database.runtime_url))

    assert resources is runtime.resources
    await runtime.shutdown()


@pytest.mark.parametrize(
    "database_heads",
    (
        (),
        ("20261010_04",),
        ("20261011_01",),
        ("unrecognized",),
        ("20261010_04", HEAD_REVISION),
    ),
    ids=("empty", "older", "newer", "unknown", "multiple-heads"),
)
@pytest.mark.asyncio(loop_scope="session")
async def test_incompatible_schema_fails_without_mutating_revision(
    readiness_database: ReadinessDatabase,
    database_heads: tuple[str, ...],
) -> None:
    await _replace_database_heads(readiness_database.migration_url, database_heads)
    runtime = DatabaseRuntime()
    try:
        with pytest.raises(DatabaseStartupError, match="database startup failed"):
            await runtime.start(_runtime_settings(readiness_database.runtime_url))

        assert runtime.resources is None
        assert await _database_heads(readiness_database.migration_url) == tuple(
            sorted(database_heads)
        )
    finally:
        await _replace_database_heads(
            readiness_database.migration_url,
            (HEAD_REVISION,),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_migration_authority_cannot_start_as_runtime(
    readiness_database: ReadinessDatabase,
) -> None:
    runtime = DatabaseRuntime()

    with pytest.raises(DatabaseStartupError, match="database startup failed"):
        await runtime.start(
            _runtime_settings(
                readiness_database.migration_url,
                role=RUNTIME_ROLE,
            )
        )

    assert runtime.resources is None


@pytest.mark.asyncio(loop_scope="session")
async def test_missing_required_runtime_privilege_fails_readiness(
    readiness_database: ReadinessDatabase,
) -> None:
    async with _connection(readiness_database.migration_url) as connection:
        await connection.execute(
            text("REVOKE INSERT ON spine.outbox_intents FROM spine_runtime")
        )
        await connection.commit()
    runtime = DatabaseRuntime()
    try:
        with pytest.raises(DatabaseStartupError, match="database startup failed"):
            await runtime.start(_runtime_settings(readiness_database.runtime_url))
    finally:
        async with _connection(readiness_database.migration_url) as connection:
            await connection.execute(
                text("GRANT INSERT ON spine.outbox_intents TO spine_runtime")
            )
            await connection.commit()

    assert runtime.resources is None
