from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from spine.infrastructure.db.engine import DatabaseRuntime
from spine.infrastructure.db.settings import RuntimeDatabaseSettings
from spine.infrastructure.db.test_harness import (
    TestDatabaseProvision as DatabaseProvision,
)

pytestmark = pytest.mark.postgresql


def _runtime_settings(
    provision: DatabaseProvision,
    **overrides: object,
) -> RuntimeDatabaseSettings:
    values: dict[str, object] = {
        "url": provision.url,
        "pool_size": 1,
        "max_overflow": 0,
        "pool_timeout_seconds": 0.1,
        "connect_timeout_seconds": 5,
    }
    values.update(overrides)
    return RuntimeDatabaseSettings(**values)


@pytest.mark.asyncio(loop_scope="session")
async def test_real_postgresql_connectivity_uses_supported_major(
    postgresql_provision: DatabaseProvision,
) -> None:
    runtime = DatabaseRuntime()
    resources = await runtime.start(_runtime_settings(postgresql_provision))

    async with resources.engine.connect() as connection:  # type: ignore[attr-defined]
        value = await connection.scalar(
            text("SELECT current_setting('server_version_num')")
        )

    await runtime.shutdown()
    assert str(value).startswith("17")


@pytest.mark.asyncio(loop_scope="session")
async def test_runtime_transactions_use_read_committed_isolation(
    postgresql_provision: DatabaseProvision,
) -> None:
    runtime = DatabaseRuntime()
    resources = await runtime.start(_runtime_settings(postgresql_provision))

    async with resources.engine.connect() as connection:  # type: ignore[attr-defined]
        isolation = await connection.scalar(text("SHOW transaction_isolation"))

    await runtime.shutdown()
    assert isolation == "read committed"


@pytest.mark.asyncio(loop_scope="session")
async def test_real_postgresql_pool_is_bounded_and_disposal_is_deterministic(
    postgresql_provision: DatabaseProvision,
) -> None:
    runtime = DatabaseRuntime()
    resources = await runtime.start(_runtime_settings(postgresql_provision))
    engine = resources.engine
    first = await engine.connect()  # type: ignore[attr-defined]

    with pytest.raises(SQLAlchemyTimeoutError):
        await engine.connect()  # type: ignore[attr-defined]

    await first.close()
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]

    await runtime.shutdown()

    assert runtime.resources is None
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio(loop_scope="session")
async def test_async_session_factory_returns_operation_owned_sessions(
    postgresql_provision: DatabaseProvision,
) -> None:
    runtime = DatabaseRuntime()
    resources = await runtime.start(_runtime_settings(postgresql_provision))
    session_factory = resources.session_factory

    first = session_factory()  # type: ignore[operator]
    second = session_factory()  # type: ignore[operator]

    assert first is not second
    await first.close()
    await second.close()
    await runtime.shutdown()
