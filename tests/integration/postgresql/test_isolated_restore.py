from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection, sql
from testcontainers.core.container import ExecConfig

from spine.infrastructure.db.test_harness import TestDatabaseProvision
from spine.infrastructure.isolated_restore import (
    IsolatedRestoreTarget,
    IsolatedRestoreVerifier,
)
from spine.infrastructure.postgres_backup import PostgreSQLBackupConnection, TenantInventoryScope
from tests.infrastructure.test_isolated_restore import OBJECT, make_manifest, write_archive


@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_isolated_restore_verifier_reconciles_real_postgres_and_filesystem_artifacts(
    postgresql_provision: TestDatabaseProvision,
    tmp_path: Path,
) -> None:
    """Exercise the verifier with a real container-local pg_restore and isolated database."""
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
            for role_name, options in (
                (restore_user, "LOGIN NOSUPERUSER NOBYPASSRLS"),
                (verifier_user, "LOGIN NOSUPERUSER NOBYPASSRLS"),
                (verifier_role, "NOLOGIN NOSUPERUSER NOBYPASSRLS"),
                (serving_role, "NOLOGIN NOSUPERUSER NOBYPASSRLS"),
            ):
                await cursor.execute(
                    sql.SQL("CREATE ROLE {} " + options).format(sql.Identifier(role_name))
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

        pg_archive = tmp_path / "postgres.dump"
        _exec_container(
            container,
            ["pg_dump", "--format=custom", f"--file={archive_in_container}"],
            environment={"PGDATABASE": source_database, "PGUSER": admin_user},
        )
        container.copy_from_container(archive_in_container, pg_archive)
        filesystem_archive = tmp_path / "objects.tar"
        write_archive(filesystem_archive)
        manifest = make_manifest(
            hashlib.sha256(pg_archive.read_bytes()).hexdigest(),
            hashlib.sha256(filesystem_archive.read_bytes()).hexdigest(),
        )

        scope_a = TenantInventoryScope(workspace_id=workspace_a, environment_id=environment_a)
        scope_b = TenantInventoryScope(workspace_id=workspace_b, environment_id=environment_b)
        target = IsolatedRestoreTarget(
            database=restore_database,
            restore_connection=PostgreSQLBackupConnection(
                database=restore_database,
                username=restore_user,
                backup_role=restore_user,
                host=parsed.hostname,
                port=parsed.port or 5432,
                sslmode="require",
            ),
            verifier_connection=PostgreSQLBackupConnection(
                database=restore_database,
                username=verifier_user,
                backup_role=verifier_role,
                host=parsed.hostname,
                port=parsed.port or 5432,
                sslmode="require",
            ),
            filesystem_root=tmp_path / "restored-originals",
            verifier_scopes=(scope_a, scope_b),
            serving_identities=(serving_role,),
        )
        verifier = IsolatedRestoreVerifier(
            pg_restore_executable=Path("/usr/bin/pg_restore"),
            postgres_restore_port=ContainerPostgreSQLRestorePort(
                container=container,
                admin_user=admin_user,
                restore_user=restore_user,
                verifier_user=verifier_user,
                verifier_role=verifier_role,
                serving_role=serving_role,
                scope_a=scope_a,
                scope_b=scope_b,
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
        assert evidence.outcome == "verified"
        assert original.read_bytes() == b"synthetic source bytes"
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
                assert (await cursor.fetchone())[0] == 1
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


class ContainerPostgreSQLRestorePort:
    """Container-backed integration adapter that exercises matching real PG tools."""

    def __init__(
        self,
        *,
        container: object,
        admin_user: str,
        restore_user: str,
        verifier_user: str,
        verifier_role: str,
        serving_role: str,
        scope_a: TenantInventoryScope,
        scope_b: TenantInventoryScope,
        archive_in_container: str,
    ) -> None:
        self._container = container
        self._admin_user = admin_user
        self._restore_user = restore_user
        self._verifier_user = verifier_user
        self._verifier_role = verifier_role
        self._serving_role = serving_role
        self._scopes = (scope_a, scope_b)
        self._archive_in_container = archive_in_container

    async def restore_and_read(
        self,
        archive: Path,
        target: IsolatedRestoreTarget,
        *,
        expected_component_version: str,
    ) -> tuple[object, ...]:
        self._container.copy_into_container(archive, self._archive_in_container)
        version = _exec_container(self._container, ["pg_restore", "--version"]).strip()
        version_major = version.rsplit(" ", maxsplit=1)[-1].split(".", maxsplit=1)[0]
        if version_major != expected_component_version.split(".", maxsplit=1)[0]:
            raise RuntimeError("container restore tool is incompatible with manifest")

        self._psql(
            self._restore_user,
            target.database,
            "SELECT count(*) FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'",
            expected="0",
        )
        self._psql(
            self._restore_user,
            target.database,
            f"REVOKE CONNECT ON DATABASE {target.database} FROM PUBLIC; "
            f"GRANT CONNECT ON DATABASE {target.database} TO {self._verifier_user}",
        )
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
            environment={"PGUSER": self._restore_user},
        )
        self._psql(
            self._restore_user,
            target.database,
            f"REVOKE ALL PRIVILEGES ON SCHEMA spine FROM PUBLIC; "
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA spine FROM PUBLIC; "
            f"GRANT USAGE ON SCHEMA spine TO {self._verifier_role}; "
            f"GRANT SELECT ON TABLE spine.source_revision_receipt_associations "
            f"TO {self._verifier_role}",
        )
        serving_result = self._container.exec(
            ExecConfig(
                [
                    "psql",
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "SELECT count(*) FROM spine.source_revision_receipt_associations",
                ],
                environment={
                    "PGDATABASE": target.database,
                    "PGUSER": self._serving_role,
                },
            )
        )
        assert serving_result.exit_code != 0

        references: list[object] = []
        scoped_counts: list[str] = []
        for scope in self._scopes:
            output = self._psql(
                self._verifier_user,
                target.database,
                f"BEGIN; SET ROLE {self._verifier_role}; "
                f"SET LOCAL spine.workspace_id = '{scope.workspace_id}'; "
                f"SET LOCAL spine.environment_id = '{scope.environment_id}'; "
                "SELECT original_reference::text FROM "
                "spine.source_revision_receipt_associations; COMMIT",
            )
            rows = [line for line in output.splitlines() if line.startswith("{")]
            scoped_counts.append(str(len(rows)))
            references.extend(json.loads(line) for line in rows)
        assert scoped_counts == ["1", "0"]
        from spine.domain.sources.original_reference import ObjectReference

        return (ObjectReference.model_validate(references[0], strict=False),)

    def _psql(self, user: str, database: str, statement: str, *, expected: str | None = None) -> str:
        output = _exec_container(
            self._container,
            ["psql", "--no-psqlrc", "--tuples-only", "--no-align", "--command", statement],
            environment={"PGDATABASE": database, "PGUSER": user},
        ).strip()
        if expected is not None:
            assert output == expected
        return output


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
