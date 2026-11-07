"""Capture a current PostgreSQL catalog snapshot for Issue #1042.

This utility always provisions a fresh, pinned Testcontainers PostgreSQL target,
marks it with the test harness, applies the repository migration head, and then
captures schema metadata only. It never connects to a configured application or
developer database.

Example:
    .venv/bin/python scripts/qualification/issue1042_postgresql_catalog.py \
        --output /tmp/issue1042-postgresql-catalog.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from spine.infrastructure.db.migrations import upgrade_database
from spine.infrastructure.db.operator import (
    activate_postgresql_source_erasure_executor,
    bootstrap_database_roles,
)
from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
    TestDatabaseSettings,
)
from spine.infrastructure.db.test_harness import (
    install_container_marker,
    provision_test_database,
)


MIGRATION_ROLE = "spine_migration"
RUNTIME_ROLE = "spine_runtime"
DISPATCHER_ROLE = "spine_dispatcher"
MIGRATION_PASSWORD = "issue1042-catalog-migration-synthetic"
RUNTIME_PASSWORD = "issue1042-catalog-runtime-synthetic"
DISPATCHER_PASSWORD = "issue1042-catalog-dispatcher-synthetic"


def _role_url(url: SecretStr, role: str, password: str) -> SecretStr:
    return SecretStr(
        make_url(url.get_secret_value())
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


async def _capture(database_url: SecretStr) -> dict[str, Any]:
    engine = create_async_engine(database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.connect() as connection:
            metadata: dict[str, Any] = {
                "postgresql_version": await connection.scalar(text("SELECT version()")),
                "database": await connection.scalar(text("SELECT current_database()")),
                "alembic_head": await connection.scalar(
                    text("SELECT version_num FROM spine.alembic_version")
                ),
            }
            metadata["tables"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT c.relname AS table_name, c.relkind, "
                            "pg_get_userbyid(c.relowner) AS owner, c.relrowsecurity AS rls, "
                            "c.relforcerowsecurity AS force_rls "
                            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'spine' AND c.relkind IN ('r', 'p') "
                            "ORDER BY c.relname"
                        )
                    )
                ).mappings()
            ]
            metadata["columns"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT table_name, column_name, ordinal_position, "
                            "data_type, udt_name, is_nullable, column_default "
                            "FROM information_schema.columns "
                            "WHERE table_schema = 'spine' "
                            "ORDER BY table_name, ordinal_position"
                        )
                    )
                ).mappings()
            ]
            metadata["constraints"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT child.relname AS table_name, constraint_row.conname AS name, "
                            "constraint_row.contype AS kind, "
                            "parent.relname AS parent_table, "
                            "ARRAY(SELECT attribute.attname FROM unnest(constraint_row.conkey) "
                            "WITH ORDINALITY AS key_column(attnum, position) "
                            "JOIN pg_attribute attribute ON attribute.attrelid = child.oid "
                            "AND attribute.attnum = key_column.attnum ORDER BY key_column.position) "
                            "AS columns, "
                            "ARRAY(SELECT attribute.attname FROM unnest(constraint_row.confkey) "
                            "WITH ORDINALITY AS key_column(attnum, position) "
                            "JOIN pg_attribute attribute ON attribute.attrelid = parent.oid "
                            "AND attribute.attnum = key_column.attnum ORDER BY key_column.position) "
                            "AS parent_columns, "
                            "CASE constraint_row.confdeltype WHEN 'a' THEN 'NO ACTION' "
                            "WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' "
                            "WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete, "
                            "pg_get_constraintdef(constraint_row.oid, true) AS definition, "
                            "constraint_row.convalidated AS validated "
                            "FROM pg_constraint constraint_row "
                            "JOIN pg_class child ON child.oid = constraint_row.conrelid "
                            "JOIN pg_namespace child_namespace ON child_namespace.oid = child.relnamespace "
                            "LEFT JOIN pg_class parent ON parent.oid = constraint_row.confrelid "
                            "WHERE child_namespace.nspname = 'spine' "
                            "ORDER BY child.relname, constraint_row.conname"
                        )
                    )
                ).mappings()
            ]
            metadata["triggers"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT relation.relname AS table_name, trigger.tgname AS name, "
                            "trigger.tgenabled AS enabled, pg_get_triggerdef(trigger.oid, true) AS definition, "
                            "pg_get_functiondef(trigger.tgfoid) AS function_definition "
                            "FROM pg_trigger trigger JOIN pg_class relation "
                            "ON relation.oid = trigger.tgrelid "
                            "JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace "
                            "WHERE namespace.nspname = 'spine' AND NOT trigger.tgisinternal "
                            "ORDER BY relation.relname, trigger.tgname"
                        )
                    )
                ).mappings()
            ]
            metadata["policies"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT relation.relname AS table_name, policy.polname AS name, "
                            "ARRAY(SELECT role.rolname FROM pg_roles role "
                            "WHERE role.oid = ANY(policy.polroles)) AS roles, "
                            "policy.polcmd AS command, pg_get_expr(policy.polqual, policy.polrelid) AS using, "
                            "pg_get_expr(policy.polwithcheck, policy.polrelid) AS with_check "
                            "FROM pg_policy policy JOIN pg_class relation "
                            "ON relation.oid = policy.polrelid "
                            "JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace "
                            "WHERE namespace.nspname = 'spine' "
                            "ORDER BY relation.relname, policy.polname"
                        )
                    )
                ).mappings()
            ]
            metadata["privileged_functions"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT procedure.proname AS name, "
                            "pg_get_userbyid(procedure.proowner) AS owner, "
                            "procedure.prosecdef AS security_definer, "
                            "procedure.proconfig AS configuration, "
                            "procedure.proacl AS privileges, "
                            "pg_get_functiondef(procedure.oid) AS definition "
                            "FROM pg_proc procedure "
                            "JOIN pg_namespace namespace ON namespace.oid = procedure.pronamespace "
                            "WHERE namespace.nspname = 'spine' "
                            "AND procedure.proname ILIKE '%erasure%' "
                            "OR namespace.nspname = 'spine' "
                            "AND procedure.proname = 'allow_governed_source_delete' "
                            "ORDER BY procedure.proname"
                        )
                    )
                ).mappings()
            ]
            metadata["role_memberships"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT role.rolname AS granted_role, member.rolname AS member_role "
                            "FROM pg_auth_members membership "
                            "JOIN pg_roles role ON role.oid = membership.roleid "
                            "JOIN pg_roles member ON member.oid = membership.member "
                            "WHERE role.rolname IN ('spine_erasure_executor', 'spine_runtime', "
                            "'spine_migration', 'spine_dispatcher') "
                            "ORDER BY role.rolname, member.rolname"
                        )
                    )
                ).mappings()
            ]
            metadata["source_erasure_privileges"] = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT table_name, privilege_type, grantee "
                            "FROM information_schema.role_table_grants "
                            "WHERE table_schema = 'spine' AND grantee IN "
                            "('spine_erasure_executor', 'spine_runtime', 'spine_migration') "
                            "AND table_name IN ('source_disposition_operations', "
                            "'source_erasure_execution_contexts', 'source_objects', "
                            "'source_revisions', 'email_message_artifacts', "
                            "'email_attachment_artifacts', 'email_attachment_processing_receipts', "
                            "'parsed_documents', 'parsed_document_segments', 'processing_receipts', "
                            "'source_processing_admissions', 'process_only_operation_lineages') "
                            "ORDER BY table_name, grantee, privilege_type"
                        )
                    )
                ).mappings()
            ]
            return metadata
    finally:
        await engine.dispose()


async def _run(output: Path) -> None:
    settings = TestDatabaseSettings()
    provision = provision_test_database(settings, run_id="issue1042-catalog")
    try:
        await install_container_marker(provision)
        operator = OperatorDatabaseSettings(
            url=provision.url,
            database_name=provision.target.database_name,
            migration_role=MIGRATION_ROLE,
            migration_password=MIGRATION_PASSWORD,
            runtime_role=RUNTIME_ROLE,
            runtime_password=RUNTIME_PASSWORD,
            dispatcher_role=DISPATCHER_ROLE,
            dispatcher_password=DISPATCHER_PASSWORD,
        )
        await bootstrap_database_roles(operator)
        migration = MigrationDatabaseSettings(
            url=_role_url(provision.url, MIGRATION_ROLE, MIGRATION_PASSWORD),
            migration_role=MIGRATION_ROLE,
            runtime_role=RUNTIME_ROLE,
            dispatcher_role=DISPATCHER_ROLE,
        )
        await asyncio.to_thread(upgrade_database, migration)
        await activate_postgresql_source_erasure_executor(operator)
        metadata = await _capture(migration.url)
        parsed = urlsplit(provision.url.get_secret_value())
        metadata["test_target"] = {
            "mode": "fresh-pinned-testcontainer",
            "database_name": provision.target.database_name,
            "postgresql_major": provision.target.postgresql_major,
            "container_host": parsed.hostname,
            "synthetic_data_only": True,
        }
        output.write_text(
            json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(
            f"Captured {len(metadata['tables'])} PostgreSQL tables, "
            f"{len(metadata['columns'])} columns and "
            f"{len(metadata['constraints'])} constraints at Alembic "
            f"{metadata['alembic_head']} -> {output}"
        )
    finally:
        if provision.container is not None:
            provision.container.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    asyncio.run(_run(arguments.output))


if __name__ == "__main__":
    main()
