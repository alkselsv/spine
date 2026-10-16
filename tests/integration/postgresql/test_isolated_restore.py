from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from testcontainers.core.container import ExecConfig
from testcontainers.community.postgres import PostgresContainer

from spine.infrastructure.db.settings import TestDatabaseSettings
from spine.infrastructure.db.test_harness import (
    TestDatabaseProvision,
    install_container_marker,
    provision_test_database,
)
from spine.infrastructure.isolated_restore import (
    IsolatedRestoreTarget,
    IsolatedRestoreVerifier,
)
from spine.infrastructure.postgres_backup import PostgreSQLBackupConnection, TenantInventoryScope
from tests.infrastructure.test_isolated_restore import OBJECT, OBJECT_B, make_manifest, write_archive


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def ssl_postgresql_provision() -> TestDatabaseProvision:
    """Own a PG17 container initialized with a local TLS certificate."""
    settings = TestDatabaseSettings()
    command = (
        "mkdir -p /docker-entrypoint-initdb.d && "
        "cat > /docker-entrypoint-initdb.d/001-local-tls.sh <<'SPINE_TLS_HOOK'\n"
        "#!/bin/sh\n"
        'openssl req -x509 -newkey rsa:2048 -nodes -keyout "$PGDATA/server.key" '
        '-out "$PGDATA/server.crt" -days 1 -subj "/CN=localhost"\n'
        'chmod 600 "$PGDATA/server.key"\n'
        "SPINE_TLS_HOOK\n"
        "chmod 755 /docker-entrypoint-initdb.d/001-local-tls.sh && "
        'exec /usr/local/bin/docker-entrypoint.sh postgres -c ssl=on '
        '-c ssl_cert_file="$PGDATA/server.crt" -c ssl_key_file="$PGDATA/server.key"'
    )

    def ssl_container_factory(**kwargs: object) -> PostgresContainer:
        return PostgresContainer(**kwargs).with_command(["bash", "-c", command])

    provision = provision_test_database(
        settings,
        run_id=f"isolated-restore-{uuid4().hex[:8]}",
        container_factory=ssl_container_factory,
    )
    if provision.container is None:
        pytest.skip("isolated restore integration requires an owned PostgreSQL testcontainer")
    await install_container_marker(provision)
    try:
        yield provision
    finally:
        provision.container.stop()


@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_isolated_restore_verifier_reconciles_real_postgres_and_filesystem_artifacts(
    ssl_postgresql_provision: TestDatabaseProvision,
    tmp_path: Path,
) -> None:
    """Exercise the verifier with a real container-local pg_restore and isolated database."""
    postgresql_provision = ssl_postgresql_provision
    container = postgresql_provision.container
    if container is None:
        pytest.skip("isolated restore qualification requires a fresh owned testcontainer")

    parsed = urlsplit(postgresql_provision.url.get_secret_value())
    if parsed.scheme != "postgresql+psycopg" or not parsed.hostname:
        pytest.fail("test harness returned an unsupported PostgreSQL URL")
    admin_user = unquote(parsed.username or "")
    admin_password = unquote(parsed.password or "")
    admin_database = unquote(parsed.path.removeprefix("/"))
    suffix = uuid4().hex[:12]
    source_database = f"spine_test_restore_{suffix}"
    restore_database = f"spine_restore_{suffix}"
    restore_user = f"spine_restore_user_{suffix}"
    verifier_user = f"spine_verify_user_{suffix}"
    verifier_role = f"spine_verify_role_{suffix}"
    serving_role = f"spine_runtime_{suffix}"
    restore_password = f"restore-{suffix}"
    verifier_password = f"verify-{suffix}"
    serving_password = f"serve-{suffix}"
    archive_in_container = f"/tmp/spine-restore-{suffix}.dump"
    workspace_a = UUID("10000000-0000-4000-8000-000000000001")
    environment_a = UUID("20000000-0000-4000-8000-000000000001")
    workspace_b = UUID("10000000-0000-4000-8000-000000000002")
    environment_b = UUID("20000000-0000-4000-8000-000000000002")
    admin_kwargs: dict[str, object] = {
        "user": admin_user,
        "password": admin_password,
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "sslmode": "prefer",
    }
    created_roles = (restore_user, verifier_user, verifier_role, serving_role)

    async with await AsyncConnection.connect(
        dbname=admin_database, autocommit=True, **admin_kwargs
    ) as admin_connection:
        async with admin_connection.cursor() as cursor:
            for role_name, options, password in (
                (restore_user, "LOGIN NOSUPERUSER NOBYPASSRLS", restore_password),
                (verifier_user, "LOGIN NOSUPERUSER NOBYPASSRLS", verifier_password),
                (verifier_role, "NOLOGIN NOSUPERUSER NOBYPASSRLS", None),
                (serving_role, "LOGIN NOSUPERUSER NOBYPASSRLS", serving_password),
            ):
                await cursor.execute(
                    sql.SQL("CREATE ROLE {} " + options).format(sql.Identifier(role_name))
                )
                if password is not None:
                    await cursor.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(role_name), sql.Literal(password)
                        )
                    )
            await cursor.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(verifier_role), sql.Identifier(verifier_user)
                )
            )
            await cursor.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(source_database)))
            await cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(restore_database), sql.Identifier(restore_user)
                )
            )
            await cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(restore_database), sql.Identifier(serving_role)
                )
            )

    try:
        async with await AsyncConnection.connect(
            dbname=source_database, autocommit=True, **admin_kwargs
        ) as source_connection:
            async with source_connection.cursor() as cursor:
                await cursor.execute("CREATE SCHEMA spine")
                await cursor.execute(
                    "CREATE TABLE spine.source_revision_receipt_associations ("
                    "association_id uuid PRIMARY KEY, workspace_id uuid NOT NULL, "
                    "environment_id uuid NOT NULL, original_reference jsonb NOT NULL, "
                    "verification_status text NOT NULL, storage_status text NOT NULL)"
                )
                await cursor.execute(
                    "ALTER TABLE spine.source_revision_receipt_associations "
                    "ENABLE ROW LEVEL SECURITY"
                )
                await cursor.execute(
                    "CREATE POLICY tenant_scope ON spine.source_revision_receipt_associations "
                    "USING (workspace_id = current_setting('spine.workspace_id')::uuid "
                    "AND environment_id = current_setting('spine.environment_id')::uuid)"
                )
                await cursor.execute(
                    "INSERT INTO spine.source_revision_receipt_associations "
                    "(association_id, workspace_id, environment_id, original_reference, "
                    "verification_status, storage_status) VALUES (%s, %s, %s, %s::jsonb, "
                    "'verified', 'finalized_verified')",
                    (
                        UUID("40000000-0000-4000-8000-000000000359"),
                        workspace_a,
                        environment_a,
                        OBJECT.model_dump_json(),
                    ),
                )
                await cursor.execute(
                    "INSERT INTO spine.source_revision_receipt_associations "
                    "(association_id, workspace_id, environment_id, original_reference, "
                    "verification_status, storage_status) VALUES (%s, %s, %s, %s::jsonb, "
                    "'verified', 'finalized_verified')",
                    (
                        UUID("40000000-0000-4000-8000-000000000360"),
                        workspace_b,
                        environment_b,
                        OBJECT_B.model_dump_json(),
                    ),
                )

        pg_archive = tmp_path / "postgres.dump"
        _exec_container(
            container,
            ["pg_dump", "--host=127.0.0.1", "--format=custom", f"--file={archive_in_container}"],
            environment={
                "PGDATABASE": source_database,
                "PGUSER": admin_user,
                "PGPASSWORD": admin_password,
                "PGSSLMODE": "prefer",
            },
        )
        container.copy_from_container(archive_in_container, pg_archive)
        filesystem_archive = tmp_path / "objects.tar"
        write_archive(
            filesystem_archive,
            tenant_objects=(
                (OBJECT, workspace_a, environment_a, b"synthetic source bytes"),
                (OBJECT_B, workspace_b, environment_b, b"synthetic source bytes B"),
            ),
        )
        manifest = make_manifest(
            hashlib.sha256(pg_archive.read_bytes()).hexdigest(),
            hashlib.sha256(filesystem_archive.read_bytes()).hexdigest(),
            references=(OBJECT, OBJECT_B),
        )

        scope_a = TenantInventoryScope(workspace_id=workspace_a, environment_id=environment_a)
        scope_b = TenantInventoryScope(workspace_id=workspace_b, environment_id=environment_b)
        passfile = tmp_path / "isolated-restore.pgpass"
        passfile.write_text(
            "\n".join(
                (
                    f"{parsed.hostname}:{parsed.port or 5432}:{restore_database}:{restore_user}:{restore_password}",
                    f"{parsed.hostname}:{parsed.port or 5432}:{restore_database}:{verifier_user}:{verifier_password}",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(passfile, 0o600)
        target = IsolatedRestoreTarget(
            database=restore_database,
            restore_connection=PostgreSQLBackupConnection(
                database=restore_database,
                username=restore_user,
                backup_role=restore_user,
                host=parsed.hostname,
                port=parsed.port or 5432,
                password_file=str(passfile),
                sslmode="require",
            ),
            verifier_connection=PostgreSQLBackupConnection(
                database=restore_database,
                username=verifier_user,
                backup_role=verifier_role,
                host=parsed.hostname,
                port=parsed.port or 5432,
                password_file=str(passfile),
                sslmode="require",
            ),
            filesystem_root=tmp_path / "restored-originals",
            verifier_scopes=(scope_a, scope_b),
            serving_identities=(serving_role,),
        )
        verifier = IsolatedRestoreVerifier(
            postgres_restore_command=ContainerPostgreSQLRestoreCommand(
                container=container,
                restore_user=restore_user,
                restore_password=restore_password,
                serving_role=serving_role,
                serving_password=serving_password,
                archive_in_container=archive_in_container,
            ),
        )

        evidence = await verifier.verify_and_restore(
            manifest=manifest,
            postgres_artifact=pg_archive,
            filesystem_artifact=filesystem_archive,
            target=target,
        )
        evidence_file = target.filesystem_root / "restore-evidence.json"
        original = target.filesystem_root / hashlib.sha256(
            f"{workspace_a}:{environment_a}".encode()
        ).hexdigest() / OBJECT.object_id.hex
        original_b = target.filesystem_root / hashlib.sha256(
            f"{workspace_b}:{environment_b}".encode()
        ).hexdigest() / OBJECT_B.object_id.hex
        assert evidence.outcome == "verified"
        assert original.read_bytes() == b"synthetic source bytes"
        assert original_b.read_bytes() == b"synthetic source bytes B"
        before = evidence_file.read_bytes()

        replay = await verifier.verify_and_restore(
            manifest=manifest,
            postgres_artifact=pg_archive,
            filesystem_artifact=filesystem_archive,
            target=target,
        )

        assert replay == evidence
        assert evidence_file.read_bytes() == before
        async with await AsyncConnection.connect(
            dbname=source_database, **admin_kwargs
        ) as source_connection:
            async with source_connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT count(*) FROM spine.source_revision_receipt_associations"
                )
                assert (await cursor.fetchone())[0] == 2
    finally:
        async with await AsyncConnection.connect(
            dbname=admin_database, autocommit=True, **admin_kwargs
        ) as admin_connection:
            async with admin_connection.cursor() as cursor:
                await cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(source_database)
                    )
                )
                await cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(restore_database)
                    )
                )
                for role_name in created_roles:
                    await cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name)))
        container.exec(ExecConfig(["rm", "-f", archive_in_container]))


class ContainerPostgreSQLRestoreCommand:
    """Run the real, version-matched pg_restore inside the owned PG container."""

    def __init__(
        self,
        *,
        container: object,
        restore_user: str,
        restore_password: str,
        serving_role: str,
        serving_password: str,
        archive_in_container: str,
    ) -> None:
        self._container = container
        self._restore_user = restore_user
        self._restore_password = restore_password
        self._serving_role = serving_role
        self._serving_password = serving_password
        self._archive_in_container = archive_in_container

    async def restore(
        self,
        archive: Path,
        target: IsolatedRestoreTarget,
        *,
        expected_component_version: str,
    ) -> None:
        self._container.copy_into_container(archive, self._archive_in_container)
        version = _exec_container(self._container, ["pg_restore", "--version"]).strip()
        version_major = version.rsplit(" ", maxsplit=1)[-1].split(".", maxsplit=1)[0]
        if version_major != expected_component_version.split(".", maxsplit=1)[0]:
            raise RuntimeError("container restore tool is incompatible with manifest")
        _exec_container(
            self._container,
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--single-transaction",
                f"--dbname={target.database}",
                self._archive_in_container,
            ],
            environment={
                "PGHOST": "127.0.0.1",
                "PGUSER": self._restore_user,
                "PGPASSWORD": self._restore_password,
                "PGSSLMODE": "require",
            },
        )
        self._assert_serving_identity_is_sealed(target.database)

    def _assert_serving_identity_is_sealed(self, database: str) -> None:
        serving_result = self._container.exec(
            ExecConfig(
                [
                    "psql",
                    "--host=127.0.0.1",
                    "--no-password",
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SELECT count(*) FROM spine.source_revision_receipt_associations",
                ],
                environment={
                    "PGDATABASE": database,
                    "PGUSER": self._serving_role,
                    "PGPASSWORD": self._serving_password,
                    "PGSSLMODE": "require",
                },
            )
        )
        output = serving_result.output.decode("utf-8", errors="replace").lower()
        assert serving_result.exit_code != 0
        assert "permission denied for database" in output


def _exec_container(
    container: object,
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> str:
    result = container.exec(ExecConfig(command, environment=environment or {}))
    if result.exit_code != 0:
        raise RuntimeError("isolated restore container command failed")
    return result.output.decode("utf-8", errors="replace")
