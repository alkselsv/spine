from __future__ import annotations

import os
import uuid
import warnings

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


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def postgresql_provision(
    request: pytest.FixtureRequest,
) -> TestDatabaseProvision:
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
            if request.session.testsfailed == 0:
                provision.container.stop()
            else:
                warnings.warn(
                    "PostgreSQL test failures left the owned disposable container "
                    "intact for verification",
                    RuntimeWarning,
                    stacklevel=1,
                )
