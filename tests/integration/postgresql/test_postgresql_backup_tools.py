from __future__ import annotations

from uuid import UUID, uuid4
from urllib.parse import unquote, urlsplit

import pytest
from psycopg import AsyncConnection
from testcontainers.core.container import ExecConfig

from spine.infrastructure.db.test_harness import TestDatabaseProvision


@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_actual_pg_dump_archive_contains_canonical_reference_data(
    postgresql_provision: TestDatabaseProvision,
) -> None:
    """Run matching tools in the owned PG17 container against a fresh disposable DB."""
    container = postgresql_provision.container
    if container is None:
        pytest.skip("real backup-tool qualification requires a fresh owned testcontainer")

    parsed = urlsplit(postgresql_provision.url.get_secret_value())
    if parsed.scheme != "postgresql+psycopg" or not parsed.hostname:
        pytest.fail("test harness returned an unsupported PostgreSQL URL")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    admin_database = unquote(parsed.path.removeprefix("/"))
    database_name = f"spine_test_backup_{uuid4().hex[:12]}"
    object_id = UUID("30000000-0000-4000-8000-000000000359")
    association_id = UUID("40000000-0000-4000-8000-000000000359")
    connection_kwargs: dict[str, object] = {
        "user": username,
        "password": password,
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "sslmode": "prefer",
    }
    async with await AsyncConnection.connect(
        dbname=admin_database, autocommit=True, **connection_kwargs
    ) as admin_connection:
        async with admin_connection.cursor() as cursor:
            await cursor.execute(f'CREATE DATABASE "{database_name}"')
    try:
        async with await AsyncConnection.connect(
            dbname=database_name, **connection_kwargs
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute("CREATE SCHEMA spine")
                await cursor.execute(
                    "CREATE TABLE spine.source_revision_receipt_associations ("
                    "association_id uuid PRIMARY KEY, original_reference jsonb NOT NULL, "
                    "verification_status text NOT NULL, storage_status text NOT NULL)"
                )
                await cursor.execute(
                    "INSERT INTO spine.source_revision_receipt_associations "
                    "(association_id, original_reference, verification_status, storage_status) "
                    "VALUES (%s, %s::jsonb, 'verified', 'finalized_verified')",
                    (
                        association_id,
                        '{"schema_version":1,"object_id":"'
                        f'{object_id}'
                        '"}',
                    ),
                )

        listing_content, data_content = _run_tools_in_postgres_container(
            container,
            database_name=database_name,
            username=username,
            password=password,
        )
        assert "source_revision_receipt_associations" in listing_content
        assert "TABLE DATA" in listing_content
        assert str(object_id) in data_content
    finally:
        async with await AsyncConnection.connect(
            dbname=admin_database, autocommit=True, **connection_kwargs
        ) as admin_connection:
            async with admin_connection.cursor() as cursor:
                await cursor.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


def _run_tools_in_postgres_container(
    container: object,
    *,
    database_name: str,
    username: str,
    password: str,
) -> tuple[str, str]:
    execute = getattr(container, "exec", None)
    if execute is None:
        pytest.skip("test container does not expose command execution")
    suffix = uuid4().hex[:16]
    archive = f"/tmp/spine-backup-{suffix}.dump"
    listing = f"/tmp/spine-backup-{suffix}.toc"
    data_dump = f"/tmp/spine-backup-{suffix}.sql"
    environment = {
        "PGDATABASE": database_name,
        "PGUSER": username,
        "PGPASSWORD": password,
        "PGSSLMODE": "prefer",
        "LANG": "C",
        "LC_ALL": "C",
    }
    dump_result = execute(
        ExecConfig(
            [
                "pg_dump",
                "--host=127.0.0.1",
                "--format=custom",
                "--no-password",
                f"--file={archive}",
            ],
            environment=environment,
        )
    )
    if dump_result.exit_code != 0:
        pytest.fail("container pg_dump failed against the disposable test database")
    restore_result = execute(
        ExecConfig(
            ["pg_restore", "--list", f"--file={listing}", archive],
            environment=environment,
        )
    )
    if restore_result.exit_code != 0:
        pytest.fail("container pg_restore could not verify the custom archive")
    data_result = execute(
        ExecConfig(
            [
                "pg_restore",
                "--data-only",
                "--schema=spine",
                "--table=source_revision_receipt_associations",
                f"--file={data_dump}",
                archive,
            ],
            environment=environment,
        )
    )
    if data_result.exit_code != 0:
        pytest.fail("container pg_restore could not inspect canonical association data")
    cat_result = execute(ExecConfig(["cat", listing], environment=environment))
    data_cat_result = execute(ExecConfig(["cat", data_dump], environment=environment))
    execute(ExecConfig(["rm", "--", archive, listing, data_dump], environment=environment))
    if cat_result.exit_code != 0 or data_cat_result.exit_code != 0:
        pytest.fail("container pg_restore output could not be read")
    listing_content = cat_result.output.decode("utf-8", errors="replace")
    data_content = data_cat_result.output.decode("utf-8", errors="replace")
    assert listing_content.startswith(";\n; Archive created")
    return listing_content, data_content
