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
    """Create or reconcile only the database and two least-privilege login roles."""

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
            await _revoke_role_memberships(connection, role=settings.runtime_role)
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
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    sql.Identifier(settings.database_name),
                    sql.Identifier(settings.migration_role),
                    sql.Identifier(settings.runtime_role),
                )
            )
    except OperatorBootstrapError:
        raise
    except Exception:
        raise OperatorBootstrapError("database operator bootstrap failed") from None


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
