"""Fresh PostgreSQL application target for post-restore qualification.

Unlike :mod:`spine.infrastructure.isolated_restore`, this adapter restores the
archive's owners and ACLs into a second, empty qualification database.  It is
therefore suitable only after immutable isolated-restore evidence has already
been verified and only behind the one-shot recovered-application qualifier.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from psycopg import AsyncConnection, sql

from spine.application.recovered_application_qualification import (
    RecoveredApplicationQualificationRequest,
)
from spine.infrastructure.postgres_backup import PostgreSQLBackupConnection


class RecoveredPostgreSQLTargetError(RuntimeError):
    """The qualification target could not be prepared or sealed safely."""


class RecoveredPostgreSQLApplicationTarget:
    """Restore one verified archive with its runtime ownership and ACL metadata."""

    def __init__(
        self,
        *,
        target_reference: str,
        connection: PostgreSQLBackupConnection,
        archive: Path,
        archive_digest: str,
        runtime_role: str,
        pg_restore_executable: Path,
        command_timeout_seconds: float = 900,
    ) -> None:
        if not target_reference.startswith("target:"):
            raise ValueError("target_reference must be opaque")
        if not archive.is_absolute() or archive.is_symlink():
            raise ValueError("archive must be an absolute non-symlink path")
        if not pg_restore_executable.is_absolute() or not os.access(
            pg_restore_executable, os.X_OK
        ):
            raise ValueError("pg_restore executable is unavailable")
        if len(archive_digest) != 64 or any(
            character not in "0123456789abcdef" for character in archive_digest
        ):
            raise ValueError("archive_digest must be lowercase sha256")
        if not runtime_role or not runtime_role.replace("_", "a").isalnum():
            raise ValueError("runtime_role is invalid")
        if command_timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")
        self._target_reference = target_reference
        self._connection = connection
        self._archive = archive
        self._archive_digest = archive_digest
        self._runtime_role = runtime_role
        self._pg_restore = pg_restore_executable
        self._timeout = command_timeout_seconds

    async def prepare(
        self, qualification: RecoveredApplicationQualificationRequest
    ) -> None:
        self._assert_scope(qualification)
        try:
            digest = await asyncio.to_thread(_sha256_file, self._archive)
            if digest != self._archive_digest:
                raise RecoveredPostgreSQLTargetError(
                    "recovered application archive is unavailable"
                )
            await self._assert_empty()
            await self._restore()
            await self._grant_runtime_connect()
            await self._assert_runtime_metadata()
        except RecoveredPostgreSQLTargetError:
            raise
        except Exception:
            raise RecoveredPostgreSQLTargetError(
                "recovered application target preparation failed"
            ) from None

    async def seal(
        self, qualification: RecoveredApplicationQualificationRequest
    ) -> None:
        self._assert_scope(qualification)
        try:
            async with await AsyncConnection.connect(
                **self._connection.connection_kwargs(), autocommit=True
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute(
                        "SELECT pg_terminate_backend(activity.pid) "
                        "FROM pg_catalog.pg_stat_activity AS activity "
                        "JOIN pg_catalog.pg_roles AS role ON role.oid = activity.usesysid "
                        "WHERE activity.datname = current_database() "
                        "AND role.rolname = %s AND activity.pid <> pg_backend_pid()",
                        (self._runtime_role,),
                    )
                    await cursor.execute(
                        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                            sql.Identifier(self._connection.database),
                            sql.Identifier(self._runtime_role),
                        )
                    )
        except Exception:
            raise RecoveredPostgreSQLTargetError(
                "recovered application target sealing failed"
            ) from None

    def _assert_scope(
        self, qualification: RecoveredApplicationQualificationRequest
    ) -> None:
        if (
            type(qualification) is not RecoveredApplicationQualificationRequest
            or qualification.target_reference != self._target_reference
            or not qualification.isolated_activation
            or qualification.external_serving_allowed
            or qualification.cutover_allowed
        ):
            raise RecoveredPostgreSQLTargetError(
                "recovered application target scope is invalid"
            )

    async def _assert_empty(self) -> None:
        async with await AsyncConnection.connect(
            **self._connection.connection_kwargs()
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT count(*) FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE relation.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') "
                    "AND namespace.nspname NOT IN ('pg_catalog', 'information_schema') "
                    "AND namespace.nspname NOT LIKE 'pg_toast%' "
                    "AND namespace.nspname NOT LIKE 'pg_temp_%'"
                )
                row = await cursor.fetchone()
                if row is None or row[0] != 0:
                    raise RecoveredPostgreSQLTargetError(
                        "recovered application target is not empty"
                    )

    async def _restore(self) -> None:
        process = await asyncio.create_subprocess_exec(
            os.fspath(self._pg_restore),
            "--exit-on-error",
            "--single-transaction",
            "--dbname",
            self._connection.database,
            os.fspath(self._archive),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._connection.process_environment(),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=self._timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RecoveredPostgreSQLTargetError(
                "recovered application restore timed out"
            ) from None
        if process.returncode != 0:
            raise RecoveredPostgreSQLTargetError(
                "recovered application restore failed"
            )

    async def _assert_runtime_metadata(self) -> None:
        async with await AsyncConnection.connect(
            **self._connection.connection_kwargs()
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT current_user, owner.rolname, "
                    "has_schema_privilege(%s, namespace.oid, 'USAGE') "
                    "FROM pg_catalog.pg_namespace AS namespace "
                    "JOIN pg_catalog.pg_roles AS owner ON owner.oid = namespace.nspowner "
                    "WHERE namespace.nspname = 'spine'",
                    (self._runtime_role,),
                )
                row = await cursor.fetchone()
                if (
                    row is None
                    or row[1] in {row[0], self._runtime_role}
                    or row[2] is not True
                ):
                    raise RecoveredPostgreSQLTargetError(
                        "recovered runtime ownership is invalid"
                    )
                await cursor.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'spine' "
                    "AND relation.relkind IN ('r', 'p') "
                    "AND relation.relrowsecurity AND relation.relforcerowsecurity "
                    "AND has_table_privilege(%s, relation.oid, 'SELECT'))",
                    (self._runtime_role,),
                )
                rls_row = await cursor.fetchone()
                if rls_row is None or rls_row[0] is not True:
                    raise RecoveredPostgreSQLTargetError(
                        "recovered runtime ACL is unavailable"
                    )
                await cursor.execute(
                    "SELECT count(*) FILTER (WHERE owner.rolname = current_user) "
                    "FROM pg_catalog.pg_proc AS function "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = function.pronamespace "
                    "JOIN pg_catalog.pg_roles AS owner ON owner.oid = function.proowner "
                    "WHERE namespace.nspname = 'spine' AND function.prosecdef"
                )
                function_row = await cursor.fetchone()
                if function_row is None or function_row[0] != 0:
                    raise RecoveredPostgreSQLTargetError(
                        "recovered function ownership is invalid"
                    )

    async def _grant_runtime_connect(self) -> None:
        async with await AsyncConnection.connect(
            **self._connection.connection_kwargs(), autocommit=True
        ) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(self._connection.database),
                        sql.Identifier(self._runtime_role),
                    )
                )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "RecoveredPostgreSQLApplicationTarget",
    "RecoveredPostgreSQLTargetError",
]
