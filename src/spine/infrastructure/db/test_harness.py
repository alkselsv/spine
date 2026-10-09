"""Fail-closed safety primitives for Spine's mandatory PostgreSQL test harness."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from spine.infrastructure.db.settings import (
    SUPPORTED_POSTGRESQL_MAJOR,
    TestDatabaseSettings,
)

__test__ = False


DISPOSABLE_MARKER_PREFIX = "spine-test-harness:v1:"
HARNESS_ADVISORY_LOCK_ID = 0x5350494E455F5432
_DEFAULT_DATABASES = frozenset({"postgres", "template0", "template1"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TARGET_DATABASE = re.compile(r"^spine_test_[a-z0-9][a-z0-9_]{0,51}$")
_MARKER = re.compile(r"^spine-test-harness:v1:[A-Za-z0-9_-]+$")


class UnsafeTestTargetError(RuntimeError):
    """A test target or cleanup action could not be proven safe."""


class PostgreSQLGateError(RuntimeError):
    """The mandatory real-PostgreSQL prerequisite is unavailable."""


@dataclass(frozen=True, slots=True)
class TestTarget:
    __test__ = False

    database_name: str
    disposable_marker: SecretStr = field(repr=False)
    postgresql_major: int = SUPPORTED_POSTGRESQL_MAJOR

    def __post_init__(self) -> None:
        validate_target_database_name(self.database_name)
        if self.postgresql_major != SUPPORTED_POSTGRESQL_MAJOR:
            raise UnsafeTestTargetError(
                "test target must use the supported PostgreSQL major"
            )
        marker = self.disposable_marker.get_secret_value()
        if not _MARKER.fullmatch(marker):
            raise UnsafeTestTargetError("disposable test marker has an invalid format")


@dataclass(frozen=True, slots=True)
class DatabaseIdentity:
    database_name: str
    disposable_marker: SecretStr | str | None = field(repr=False)
    postgresql_major: int


def _identifier_component(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized[:12] or "local"


@dataclass(frozen=True, slots=True)
class NamespaceOwnership:
    run_id: str
    worker_id: str
    nonce: str
    schema_name: str
    role_name: str

    def __post_init__(self) -> None:
        components = (self.run_id, self.worker_id, self.nonce)
        if any(_identifier_component(value) != value for value in components):
            raise UnsafeTestTargetError("namespace ownership token is not canonical")
        ownership_token = "_".join(components)
        expected_schema = f"spine_test_schema_{ownership_token}"[:63]
        expected_role = f"spine_test_role_{ownership_token}"[:63]
        if self.schema_name != expected_schema or self.role_name != expected_role:
            raise UnsafeTestTargetError("namespace ownership proof is inconsistent")

    @classmethod
    def generate(
        cls,
        *,
        run_id: str,
        worker_id: str,
        nonce: str | None = None,
    ) -> NamespaceOwnership:
        safe_run = _identifier_component(run_id)
        safe_worker = _identifier_component(worker_id)
        safe_nonce = _identifier_component(nonce or secrets.token_hex(6))
        ownership_token = f"{safe_run}_{safe_worker}_{safe_nonce}"
        schema_name = f"spine_test_schema_{ownership_token}"[:63]
        role_name = f"spine_test_role_{ownership_token}"[:63]
        if not _IDENTIFIER.fullmatch(schema_name) or not _IDENTIFIER.fullmatch(
            role_name
        ):
            raise UnsafeTestTargetError("generated namespace is not a safe identifier")
        return cls(
            run_id=safe_run,
            worker_id=safe_worker,
            nonce=safe_nonce,
            schema_name=schema_name,
            role_name=role_name,
        )

    def owns(self, identifier: str) -> bool:
        return identifier in {self.schema_name, self.role_name}


@dataclass(frozen=True, slots=True)
class OwnedResources:
    schemas: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    schemas: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


def validate_target_database_name(database_name: str) -> str:
    if database_name in _DEFAULT_DATABASES or not _TARGET_DATABASE.fullmatch(
        database_name
    ):
        raise UnsafeTestTargetError(
            "test database must be a non-default exact name beginning with "
            "spine_test_"
        )
    return database_name


def database_name_from_url(url: SecretStr) -> str:
    parsed = urlsplit(url.get_secret_value())
    database_name = unquote(parsed.path.removeprefix("/"))
    return validate_target_database_name(database_name)


def validate_disposable_marker(
    expected: SecretStr, actual: SecretStr | str | None
) -> None:
    expected_value = expected.get_secret_value()
    actual_value = (
        actual.get_secret_value() if isinstance(actual, SecretStr) else actual
    )
    if (
        not _MARKER.fullmatch(expected_value)
        or actual_value is None
        or not secrets.compare_digest(expected_value, actual_value)
    ):
        raise UnsafeTestTargetError("disposable test marker could not be verified")


def validate_database_identity(
    expected: TestTarget,
    actual: DatabaseIdentity,
) -> None:
    validate_target_database_name(actual.database_name)
    if not secrets.compare_digest(expected.database_name, actual.database_name):
        raise UnsafeTestTargetError("test database identity did not match expectation")
    if expected.postgresql_major != actual.postgresql_major:
        raise UnsafeTestTargetError(
            "PostgreSQL server identity did not match expectation"
        )
    validate_disposable_marker(
        expected.disposable_marker,
        actual.disposable_marker,
    )


def validate_cleanup_plan(
    plan: CleanupPlan,
    ownership: NamespaceOwnership,
    resources: OwnedResources,
) -> CleanupPlan:
    for identifier in (*plan.schemas, *plan.roles):
        if not _IDENTIFIER.fullmatch(identifier):
            raise UnsafeTestTargetError("cleanup identifier is not exact and safe")
        if not ownership.owns(identifier):
            raise UnsafeTestTargetError("cleanup identifier has foreign ownership")
    if not set(plan.schemas).issubset(resources.schemas) or not set(
        plan.roles
    ).issubset(resources.roles):
        raise UnsafeTestTargetError("cleanup identifier was not recorded as created")
    return plan


class DisposableContainer(Protocol):
    def start(self) -> Any: ...

    def stop(self) -> Any: ...

    def get_connection_url(self) -> str: ...


ContainerFactory = Any


@dataclass(frozen=True, slots=True)
class TestDatabaseProvision:
    __test__ = False

    url: SecretStr = field(repr=False)
    target: TestTarget
    container: DisposableContainer | None = field(default=None, repr=False)

    @property
    def is_explicit(self) -> bool:
        return self.container is None


def provision_test_database(
    settings: TestDatabaseSettings,
    *,
    run_id: str,
    container_factory: ContainerFactory | None = None,
) -> TestDatabaseProvision:
    """Resolve only the dedicated explicit target or a new unique container."""

    if settings.url is not None:
        database_name = database_name_from_url(settings.url)
        if database_name != settings.expected_name:
            raise UnsafeTestTargetError(
                "explicit test URL database does not match expected identity"
            )
        assert settings.disposable_marker is not None
        return TestDatabaseProvision(
            url=settings.url,
            target=TestTarget(
                database_name=database_name,
                disposable_marker=settings.disposable_marker,
            ),
        )

    if not settings.use_testcontainers:
        raise PostgreSQLGateError(
            "mandatory PostgreSQL gate requires SPINE_TEST_DATABASE_URL or "
            "a working container runtime"
        )

    safe_run = _identifier_component(run_id)
    suffix = secrets.token_hex(6)
    database_name = validate_target_database_name(
        f"spine_test_{safe_run}_{suffix}"[:63]
    )
    username = f"spine_test_user_{safe_run}_{suffix}"[:63]
    password = secrets.token_urlsafe(24)
    marker = SecretStr(f"{DISPOSABLE_MARKER_PREFIX}{safe_run}-{suffix}")

    if container_factory is None:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except Exception:
            raise PostgreSQLGateError(
                "mandatory PostgreSQL gate cannot import Testcontainers"
            ) from None
        container_factory = PostgresContainer

    container: DisposableContainer | None = None
    try:
        container = container_factory(
            image=settings.image,
            username=username,
            password=password,
            dbname=database_name,
            driver="psycopg",
        )
        container.start()
        url = SecretStr(container.get_connection_url())
        if database_name_from_url(url) != database_name:
            container.stop()
            raise PostgreSQLGateError(
                "container returned an unexpected database identity"
            )
    except PostgreSQLGateError:
        raise
    except Exception:
        if container is not None:
            try:
                container.stop()
            except Exception:
                pass
        raise PostgreSQLGateError(
            "mandatory PostgreSQL gate could not start container runtime image "
            f"{settings.image}"
        ) from None

    return TestDatabaseProvision(
        url=url,
        target=TestTarget(
            database_name=database_name,
            disposable_marker=marker,
        ),
        container=container,
    )


async def install_container_marker(provision: TestDatabaseProvision) -> None:
    """Mark only a freshly created, uniquely named disposable container database."""

    if provision.container is None:
        raise UnsafeTestTargetError(
            "explicit databases cannot be marked by the harness"
        )
    engine = create_async_engine(provision.url.get_secret_value())
    try:
        async with engine.begin() as connection:
            actual_name = await connection.scalar(text("SELECT current_database()"))
            if actual_name != provision.target.database_name:
                raise UnsafeTestTargetError(
                    "container database identity did not match expectation"
                )
            database_identifier = _quoted_identifier(provision.target.database_name)
            marker = _marker_sql_literal(
                provision.target.disposable_marker.get_secret_value()
            )
            await connection.execute(
                text(f"COMMENT ON DATABASE {database_identifier} IS {marker}")
            )
    except UnsafeTestTargetError:
        raise
    except Exception:
        raise PostgreSQLGateError(
            "mandatory PostgreSQL gate could not initialize the disposable marker"
        ) from None
    finally:
        await engine.dispose()


def _quoted_identifier(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise UnsafeTestTargetError("database identifier is not exact and safe")
    return f'"{identifier}"'


def _marker_sql_literal(marker: str) -> str:
    if not _MARKER.fullmatch(marker):
        raise UnsafeTestTargetError("disposable test marker has an invalid format")
    return f"'{marker}'"


async def _read_database_identity(connection: AsyncConnection) -> DatabaseIdentity:
    row = (
        await connection.execute(
            text(
                "SELECT current_database(), "
                "shobj_description(oid, 'pg_database'), "
                "current_setting('server_version_num')::integer / 10000 "
                "FROM pg_database WHERE datname = current_database()"
            )
        )
    ).one()
    return DatabaseIdentity(
        database_name=row[0],
        disposable_marker=SecretStr(row[1]) if row[1] is not None else None,
        postgresql_major=row[2],
    )


class HarnessLease:
    """An identity-checked connection holding the harness advisory lock."""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        connection: AsyncConnection,
        target: TestTarget,
        ownership: NamespaceOwnership,
    ) -> None:
        self._engine = engine
        self._connection = connection
        self.target = target
        self.ownership = ownership
        self.resources = OwnedResources()
        self._lock_acquired = True
        self._closed = False

    @property
    def connection(self) -> AsyncConnection:
        """Expose the locked test connection for exact negative test setup."""

        return self._connection

    @classmethod
    async def acquire(
        cls,
        url: SecretStr,
        target: TestTarget,
        ownership: NamespaceOwnership,
    ) -> HarnessLease:
        engine = create_async_engine(
            url.get_secret_value(),
            pool_size=1,
            max_overflow=0,
            pool_timeout=5.0,
            pool_pre_ping=True,
            pool_reset_on_return="rollback",
        )
        connection: AsyncConnection | None = None
        lock_acquired = False
        try:
            connection = await engine.connect()
            identity = await _read_database_identity(connection)
            validate_database_identity(target, identity)
            lock_acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": HARNESS_ADVISORY_LOCK_ID},
                )
            )
            if not lock_acquired:
                raise UnsafeTestTargetError(
                    "test harness advisory lock is already held"
                )
            locked_identity = await _read_database_identity(connection)
            validate_database_identity(target, locked_identity)
            lease = cls(
                engine=engine,
                connection=connection,
                target=target,
                ownership=ownership,
            )
            await lease._provision_owned_namespaces()
            return lease
        except UnsafeTestTargetError as error:
            try:
                if connection is not None:
                    await connection.rollback()
                    if lock_acquired:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_id)"),
                            {"lock_id": HARNESS_ADVISORY_LOCK_ID},
                        )
                    await connection.close()
                await engine.dispose()
            except Exception:
                pass
            raise error from None
        except Exception:
            try:
                if connection is not None:
                    await connection.rollback()
                    if lock_acquired:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_id)"),
                            {"lock_id": HARNESS_ADVISORY_LOCK_ID},
                        )
                    await connection.close()
                await engine.dispose()
            except Exception:
                pass
            raise UnsafeTestTargetError(
                "test database setup could not be safely verified"
            ) from None

    async def _provision_owned_namespaces(self) -> None:
        role = _quoted_identifier(self.ownership.role_name)
        schema = _quoted_identifier(self.ownership.schema_name)
        try:
            await self._connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
            await self._connection.execute(
                text(f"CREATE SCHEMA {schema} AUTHORIZATION {role}")
            )
            await self._connection.commit()
        except Exception:
            await self._connection.rollback()
            raise UnsafeTestTargetError(
                "test namespace provisioning failed closed"
            ) from None
        self.resources = OwnedResources(
            schemas=frozenset({self.ownership.schema_name}),
            roles=frozenset({self.ownership.role_name}),
        )

    async def _verify_cleanup_ownership(self) -> None:
        identity = await _read_database_identity(self._connection)
        validate_database_identity(self.target, identity)

        lock_owned = bool(
            await self._connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                    "AND granted AND classid::bigint = :class_id "
                    "AND objid::bigint = :object_id "
                    "AND objsubid = 1)"
                ),
                {
                    "class_id": HARNESS_ADVISORY_LOCK_ID >> 32,
                    "object_id": HARNESS_ADVISORY_LOCK_ID & 0xFFFFFFFF,
                },
            )
        )
        if not lock_owned:
            raise UnsafeTestTargetError("harness advisory lock ownership was lost")

        actual_owner = await self._connection.scalar(
            text(
                "SELECT owner.rolname FROM pg_namespace AS namespace "
                "JOIN pg_roles AS owner ON owner.oid = namespace.nspowner "
                "WHERE namespace.nspname = :schema_name"
            ),
            {"schema_name": self.ownership.schema_name},
        )
        role_exists = await self._connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": self.ownership.role_name},
        )
        if actual_owner != self.ownership.role_name or not role_exists:
            raise UnsafeTestTargetError(
                "cleanup ownership could not be verified; resources were left intact"
            )

    async def cleanup(self) -> None:
        """Drop only exact, recorded resources after re-verifying every safeguard."""

        if self._closed:
            return
        plan = CleanupPlan(
            schemas=(self.ownership.schema_name,),
            roles=(self.ownership.role_name,),
        )
        try:
            validate_cleanup_plan(plan, self.ownership, self.resources)
            await self._verify_cleanup_ownership()
            schema = _quoted_identifier(self.ownership.schema_name)
            role = _quoted_identifier(self.ownership.role_name)
            await self._connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            await self._connection.execute(text(f"DROP ROLE {role}"))
            await self._connection.commit()
        except UnsafeTestTargetError as error:
            await self._connection.rollback()
            raise UnsafeTestTargetError(
                f"{error}; resources left intact: "
                f"schema={self.ownership.schema_name}, "
                f"role={self.ownership.role_name}"
            ) from None
        except Exception:
            await self._connection.rollback()
            raise UnsafeTestTargetError(
                "cleanup could not be verified; resources left intact: "
                f"schema={self.ownership.schema_name}, "
                f"role={self.ownership.role_name}"
            ) from None
        await self.close()

    async def close(self) -> None:
        """Release the advisory lock and engine without deleting resources."""

        if self._closed:
            return
        await self._connection.rollback()
        if self._lock_acquired:
            await self._connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": HARNESS_ADVISORY_LOCK_ID},
            )
            self._lock_acquired = False
        await self._connection.close()
        await self._engine.dispose()
        self._closed = True
