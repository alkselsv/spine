from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from sqlalchemy import text

import pytest
import pytest_asyncio

from spine.infrastructure.db.settings import TestDatabaseSettings
from spine.infrastructure.db.test_harness import (
    PostgreSQLGateError,
    TestDatabaseProvision,
    UnsafeTestTargetError,
    install_container_marker,
    provision_test_database,
)


async def wait_for_postgresql_lock_wait(
    adapter: Any,
    context: Any,
    backend_pid: int,
    operation: asyncio.Task[Any],
) -> bool:
    """Wait until PostgreSQL reports that a transaction is lock-blocked."""

    async def observe() -> bool:
        while not operation.done():
            async with adapter.uow_factory(context) as uow:
                result = await uow.execute_statement(
                    text(
                        "SELECT cardinality(pg_blocking_pids(:pid)) > 0 "
                        "FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": backend_pid},
                )
                blocked = result.scalar_one_or_none()
                await uow.rollback()
            if blocked:
                return True
            await asyncio.sleep(0)
        return False

    return await asyncio.wait_for(observe(), timeout=10)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgresql_provision() -> TestDatabaseProvision:
    settings = TestDatabaseSettings()
    run_id = f"{os.getenv('PYTEST_XDIST_WORKER', 'local')}-{uuid.uuid4().hex[:12]}"
    failure: str | None = None
    try:
        provision = provision_test_database(settings, run_id=run_id)
        if not provision.is_explicit:
            await install_container_marker(provision)
    except (PostgreSQLGateError, UnsafeTestTargetError) as error:
        failure = str(error)
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    try:
        yield provision
    finally:
        if provision.container is not None:
            provision.container.stop()
