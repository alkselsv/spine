"""Process-owned SQLAlchemy async engine lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from spine.infrastructure.db.settings import (
    REDACTED_DATABASE_URL,
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    SUPPORTED_POSTGRESQL_MAJOR,
    RuntimeDatabaseSettings,
)


class DisposableAsyncEngine(Protocol):
    async def dispose(self) -> None: ...


EngineFactory = Callable[..., DisposableAsyncEngine]
SessionFactory = Callable[..., AsyncSession]
SessionFactoryBuilder = Callable[..., SessionFactory]
ReadinessCheck = Callable[[DisposableAsyncEngine], Awaitable[None]]


async def _verify_supported_postgresql(engine: DisposableAsyncEngine) -> None:
    async_engine = cast(AsyncEngine, engine)
    async with async_engine.connect() as connection:
        major = await connection.scalar(
            text("SELECT current_setting('server_version_num')::integer / 10000")
        )
    if major != SUPPORTED_POSTGRESQL_MAJOR:
        raise RuntimeError("unsupported PostgreSQL server identity")


class DatabaseStartupError(RuntimeError):
    """A sanitized failure to initialize process database resources."""


class DatabaseShutdownError(RuntimeError):
    """A sanitized failure to dispose process database resources."""


@dataclass(frozen=True, slots=True)
class DatabaseResources:
    """The engine and operation-scoped async session factory for one process."""

    engine: DisposableAsyncEngine
    session_factory: SessionFactory


class DatabaseRuntime:
    """Own exactly one engine lifecycle for an application process."""

    def __init__(
        self,
        *,
        engine_factory: EngineFactory = create_async_engine,
        session_factory_builder: SessionFactoryBuilder = async_sessionmaker,
        readiness_check: ReadinessCheck = _verify_supported_postgresql,
    ) -> None:
        self._engine_factory = engine_factory
        self._session_factory_builder = session_factory_builder
        self._readiness_check = readiness_check
        self._resources: DatabaseResources | None = None
        self._settings: RuntimeDatabaseSettings | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def resources(self) -> DatabaseResources | None:
        return self._resources

    async def start(self, settings: RuntimeDatabaseSettings) -> DatabaseResources:
        """Create the bounded engine after typed settings have validated."""

        async with self._lifecycle_lock:
            if self._resources is not None:
                if settings != self._settings:
                    raise DatabaseStartupError(
                        "database runtime already started with different settings"
                    )
                return self._resources

            engine: DisposableAsyncEngine | None = None
            try:
                engine = self._engine_factory(
                    settings.url.get_secret_value(),
                    pool_size=settings.pool_size,
                    max_overflow=settings.max_overflow,
                    pool_timeout=settings.pool_timeout_seconds,
                    pool_pre_ping=settings.pool_pre_ping,
                    pool_reset_on_return="rollback",
                    isolation_level="READ COMMITTED",
                    connect_args={
                        "connect_timeout": settings.connect_timeout_seconds,
                        "options": POSTGRESQL_SEARCH_PATH_OPTIONS,
                    },
                    hide_parameters=True,
                )
                await self._readiness_check(engine)
                session_factory = self._session_factory_builder(
                    engine,
                    expire_on_commit=False,
                    autoflush=False,
                )
            except Exception:
                if engine is not None:
                    try:
                        await engine.dispose()
                    except Exception:
                        pass
                raise DatabaseStartupError(
                    f"database startup failed for {REDACTED_DATABASE_URL}"
                ) from None

            self._settings = settings
            self._resources = DatabaseResources(
                engine=engine,
                session_factory=session_factory,
            )
            return self._resources

    async def shutdown(self) -> None:
        """Await disposal of every pooled connection owned by this runtime."""

        async with self._lifecycle_lock:
            resources = self._resources
            if resources is None:
                return
            try:
                await resources.engine.dispose()
            except Exception:
                raise DatabaseShutdownError(
                    "database shutdown failed while disposing pooled connections"
                ) from None
            self._resources = None
            self._settings = None


_process_database_runtime: DatabaseRuntime | None = None


def get_process_database_runtime() -> DatabaseRuntime:
    """Return the single production lifecycle owner for this Python process."""

    global _process_database_runtime
    if _process_database_runtime is None:
        _process_database_runtime = DatabaseRuntime()
    return _process_database_runtime
