"""Explicit cluster-level bootstrap for Spine database roles and ownership."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Protocol

from psycopg import AsyncConnection, sql

from spine.infrastructure.db.settings import OperatorDatabaseSettings


class OperatorBootstrapError(RuntimeError):
    """The operator bootstrap could not establish the required safe role model."""


class _Cursor(Protocol):
    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def fetchall(self) -> list[tuple[object, ...]]: ...


class _Connection(Protocol):
    async def execute(
        self,
        query: object,
        params: tuple[object, ...] | None = None,
    ) -> _Cursor: ...


@asynccontextmanager
async def _connect(url: str) -> AsyncIterator[AsyncConnection[tuple[object, ...]]]:
    psycopg_url = url.replace("postgresql+psycopg://", "postgresql://", 1)
    connection = await AsyncConnection.connect(psycopg_url, autocommit=True)
    try:
        yield connection
    finally:
        await connection.close()


async def bootstrap_database_roles(settings: OperatorDatabaseSettings) -> None:
    """Create or reconcile the database and least-privilege login roles."""

    try:
        async with _connect(settings.url.get_secret_value()) as connection:
            operator = await (await connection.execute("SELECT current_user")).fetchone()
            if operator is None:
                raise OperatorBootstrapError(
                    "database operator identity is unavailable"
                )

            await _ensure_role(
                connection,
                role=settings.migration_role,
                password=settings.migration_password.get_secret_value(),
            )
            await _ensure_role(
                connection,
                role=settings.runtime_role,
                password=settings.runtime_password.get_secret_value(),
            )
            await _ensure_role(
                connection,
                role=settings.dispatcher_role,
                password=settings.dispatcher_password.get_secret_value(),
            )
            await _ensure_erasure_role(
                connection,
                role=settings.erasure_role,
            )
            await _revoke_erasure_role_members(
                connection,
                role=settings.erasure_role,
            )
            await _revoke_role_memberships(connection, role=settings.runtime_role)
            await _revoke_role_memberships(
                connection,
                role=settings.dispatcher_role,
            )
            await _ensure_database(connection, settings)
            await connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(settings.database_name)
                )
            )
            await connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                    sql.Identifier(settings.database_name),
                    sql.Identifier(settings.runtime_role),
                )
            )
            await connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}, {}").format(
                    sql.Identifier(settings.database_name),
                    sql.Identifier(settings.migration_role),
                    sql.Identifier(settings.runtime_role),
                    sql.Identifier(settings.dispatcher_role),
                )
            )
    except OperatorBootstrapError:
        raise
    except Exception:
        raise OperatorBootstrapError("database operator bootstrap failed") from None


async def activate_postgresql_source_erasure_executor(
    settings: OperatorDatabaseSettings,
) -> bool:
    """Transfer the fixed erasure entrypoint after migrations under operator authority.

    The migration role is never a member of the executor role. The entrypoint is
    created without PUBLIC/runtime execution, then this operator-only step gives
    it the dedicated owner and grants runtime execution of that fixed signature.
    """

    function_signature = (
        "spine.execute_source_disposition_erasure(uuid,uuid,uuid,integer)"
    )
    email_sync_trigger_signature = "spine.erase_email_sync_source_lineage()"
    try:
        async with _connect(settings.url.get_secret_value()) as connection:
            role = await (
                await connection.execute(
                    "SELECT rolcanlogin, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = %s",
                    (settings.erasure_role,),
                )
            ).fetchone()
            if role != (False, False, False):
                raise OperatorBootstrapError("erasure executor role is unsafe")
            exists = await (
                await connection.execute(
                    "SELECT to_regprocedure(%s) IS NOT NULL",
                    (function_signature,),
                )
            ).fetchone()
            if exists is None or exists[0] is not True:
                return False
            email_sync_trigger = await (
                await connection.execute(
                    "SELECT to_regprocedure(%s) IS NOT NULL",
                    (email_sync_trigger_signature,),
                )
            ).fetchone()
            if email_sync_trigger is None or email_sync_trigger[0] is not True:
                raise OperatorBootstrapError(
                    "email sync erasure trigger function is unavailable"
                )
            await connection.execute(
                sql.SQL("REVOKE ALL ON FUNCTION {} FROM PUBLIC").format(
                    sql.SQL(function_signature)
                )
            )
            await connection.execute(
                sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
                    sql.SQL(function_signature),
                    sql.Identifier(settings.erasure_role),
                )
            )
            await connection.execute(
                sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
                    sql.SQL(email_sync_trigger_signature),
                    sql.Identifier(settings.erasure_role),
                )
            )
            await connection.execute(
                sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(
                    sql.SQL(function_signature),
                    sql.Identifier(settings.runtime_role),
                )
            )
            return True
    except OperatorBootstrapError:
        raise
    except Exception:
        raise OperatorBootstrapError("erasure executor activation failed") from None


async def _ensure_role(
    connection: _Connection,
    *,
    role: str,
    password: str,
) -> None:
    existing = await (
        await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    ).fetchone()
    if existing is None:
        await connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
        )
    await connection.execute(
        sql.SQL(
            "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOBYPASSRLS PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password))
    )


async def _ensure_erasure_role(
    connection: _Connection,
    *,
    role: str,
) -> None:
    existing = await (
        await connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    ).fetchone()
    if existing is None:
        await connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role))
        )
    await connection.execute(
        sql.SQL(
            "ALTER ROLE {} WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOBYPASSRLS"
        ).format(sql.Identifier(role))
    )


async def _revoke_erasure_role_members(
    connection: _Connection,
    *,
    role: str,
) -> None:
    members = await (
        await connection.execute(
            "SELECT member.rolname FROM pg_auth_members AS membership "
            "JOIN pg_roles AS parent ON parent.oid = membership.roleid "
            "JOIN pg_roles AS member ON member.oid = membership.member "
            "WHERE parent.rolname = %s",
            (role,),
        )
    ).fetchall()
    for (member,) in members:
        if not isinstance(member, str):
            raise OperatorBootstrapError("erasure role membership is invalid")
        await connection.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(role),
                sql.Identifier(member),
            )
        )


async def _revoke_role_memberships(connection: _Connection, *, role: str) -> None:
    memberships = await (
        await connection.execute(
            "SELECT parent.rolname FROM pg_auth_members AS membership "
            "JOIN pg_roles AS parent ON parent.oid = membership.roleid "
            "JOIN pg_roles AS member ON member.oid = membership.member "
            "WHERE member.rolname = %s",
            (role,),
        )
    ).fetchall()
    for (parent_role,) in memberships:
        if not isinstance(parent_role, str):
            raise OperatorBootstrapError("database role membership is invalid")
        await connection.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(parent_role),
                sql.Identifier(role),
            )
        )


async def _ensure_database(
    connection: _Connection,
    settings: OperatorDatabaseSettings,
) -> None:
    existing = await (
        await connection.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s",
            (settings.database_name,),
        )
    ).fetchone()
    if existing is None:
        await connection.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(settings.database_name),
                sql.Identifier(settings.migration_role),
            )
        )
        return
    await connection.execute(
        sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
            sql.Identifier(settings.database_name),
            sql.Identifier(settings.migration_role),
        )
    )
