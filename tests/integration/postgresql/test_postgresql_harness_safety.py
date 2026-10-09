from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from spine.infrastructure.db.test_harness import (
    NamespaceOwnership,
    TestDatabaseProvision as DatabaseProvision,
    TestTarget as DatabaseTestTarget,
    UnsafeTestTargetError,
    HarnessLease,
    database_name_from_url,
)

pytestmark = pytest.mark.postgresql


def _ownership(label: str) -> NamespaceOwnership:
    return NamespaceOwnership.generate(
        run_id=uuid.uuid4().hex[:12],
        worker_id=os.getenv("PYTEST_XDIST_WORKER", "local"),
        nonce=label,
    )


def _url_with_database(url: SecretStr, database_name: str) -> SecretStr:
    parsed = urlsplit(url.get_secret_value())
    return SecretStr(urlunsplit(parsed._replace(path=f"/{database_name}")))


@pytest.mark.asyncio(loop_scope="session")
async def test_unsafe_explicit_url_is_rejected_before_connection(
    postgresql_provision: DatabaseProvision,
) -> None:
    unsafe_url = _url_with_database(postgresql_provision.url, "postgres")

    with pytest.raises(UnsafeTestTargetError):
        database_name_from_url(unsafe_url)


@pytest.mark.asyncio(loop_scope="session")
async def test_wrong_marker_rejects_real_database_before_namespace_ddl(
    postgresql_provision: DatabaseProvision,
) -> None:
    target = DatabaseTestTarget(
        database_name=postgresql_provision.target.database_name,
        disposable_marker=SecretStr("spine-test-harness:v1:wrong-marker"),
    )

    with pytest.raises(UnsafeTestTargetError, match="marker"):
        await HarnessLease.acquire(
            postgresql_provision.url,
            target,
            _ownership("wrongmarker"),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_absent_marker_rejects_real_database_before_namespace_ddl(
    postgresql_provision: DatabaseProvision,
) -> None:
    database = f'"{postgresql_provision.target.database_name}"'
    marker = postgresql_provision.target.disposable_marker.get_secret_value()
    engine = create_async_engine(postgresql_provision.url.get_secret_value())
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"COMMENT ON DATABASE {database} IS NULL"))
        with pytest.raises(UnsafeTestTargetError, match="marker"):
            await HarnessLease.acquire(
                postgresql_provision.url,
                postgresql_provision.target,
                _ownership("absentmarker"),
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"COMMENT ON DATABASE {database} IS '{marker}'")
            )
        await engine.dispose()


@pytest.mark.asyncio(loop_scope="session")
async def test_unexpected_real_database_identity_is_rejected(
    postgresql_provision: DatabaseProvision,
) -> None:
    target = DatabaseTestTarget(
        database_name="spine_test_unexpected",
        disposable_marker=postgresql_provision.target.disposable_marker,
    )

    with pytest.raises(UnsafeTestTargetError, match="identity"):
        await HarnessLease.acquire(
            postgresql_provision.url,
            target,
            _ownership("identity"),
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_advisory_lock_contention_rejects_second_harness(
    postgresql_provision: DatabaseProvision,
) -> None:
    first = await HarnessLease.acquire(
        postgresql_provision.url,
        postgresql_provision.target,
        _ownership("firstlock"),
    )
    try:
        with pytest.raises(UnsafeTestTargetError, match="advisory lock"):
            await HarnessLease.acquire(
                postgresql_provision.url,
                postgresql_provision.target,
                _ownership("secondlock"),
            )
    finally:
        await first.cleanup()


@pytest.mark.asyncio(loop_scope="session")
async def test_foreign_namespace_owner_blocks_cleanup_and_leaves_resources(
    postgresql_provision: DatabaseProvision,
) -> None:
    lease = await HarnessLease.acquire(
        postgresql_provision.url,
        postgresql_provision.target,
        _ownership("foreignowner"),
    )
    foreign = _ownership("foreignrole")
    foreign_role = f'"{foreign.role_name}"'
    schema = f'"{lease.ownership.schema_name}"'
    owned_role = f'"{lease.ownership.role_name}"'
    await lease.connection.execute(text(f"CREATE ROLE {foreign_role} NOLOGIN"))
    await lease.connection.execute(
        text(f"ALTER SCHEMA {schema} OWNER TO {foreign_role}")
    )
    await lease.connection.commit()

    with pytest.raises(UnsafeTestTargetError, match="left intact"):
        await lease.cleanup()

    still_exists = await lease.connection.scalar(
        text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
        {"name": lease.ownership.schema_name},
    )
    assert still_exists is True

    await lease.connection.execute(text(f"ALTER SCHEMA {schema} OWNER TO {owned_role}"))
    await lease.connection.execute(text(f"DROP ROLE {foreign_role}"))
    await lease.connection.commit()
    await lease.cleanup()


@pytest.mark.asyncio(loop_scope="session")
async def test_unverifiable_cleanup_leaves_namespace_and_never_drops_base_database(
    postgresql_provision: DatabaseProvision,
) -> None:
    lease = await HarnessLease.acquire(
        postgresql_provision.url,
        postgresql_provision.target,
        _ownership("nomarker"),
    )
    database = f'"{postgresql_provision.target.database_name}"'
    await lease.connection.execute(text(f"COMMENT ON DATABASE {database} IS NULL"))
    await lease.connection.commit()

    with pytest.raises(UnsafeTestTargetError, match="marker"):
        await lease.cleanup()

    still_exists = await lease.connection.scalar(
        text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"),
        {"name": lease.ownership.schema_name},
    )
    assert still_exists is True

    marker = postgresql_provision.target.disposable_marker.get_secret_value()
    await lease.connection.execute(
        text(f"COMMENT ON DATABASE {database} IS '{marker}'")
    )
    await lease.connection.commit()
    await lease.cleanup()

    assert database_name_from_url(postgresql_provision.url) == (
        postgresql_provision.target.database_name
    )
