from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from alembic import command as alembic_command
from sqlalchemy import MetaData

from spine.infrastructure.db.engine import (
    DatabaseRuntime,
    DatabaseShutdownError,
    DatabaseStartupError,
    get_process_database_runtime,
)
from spine.infrastructure.db.settings import RuntimeDatabaseSettings

DATABASE_URL = "postgresql+psycopg://runtime_user:runtime_secret@db/runtime"


class RecordingEngine:
    def __init__(self) -> None:
        self.dispose_awaited = False

    async def dispose(self) -> None:
        self.dispose_awaited = True


def runtime_settings(**overrides: Any) -> RuntimeDatabaseSettings:
    values: dict[str, Any] = {
        "url": DATABASE_URL,
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout_seconds": 7.0,
        "connect_timeout_seconds": 4,
        "pool_pre_ping": True,
    }
    values.update(overrides)
    return RuntimeDatabaseSettings(**values)


@pytest.mark.asyncio
async def test_start_creates_one_bounded_engine_and_async_session_factory() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    engine = RecordingEngine()
    built_sessions: list[tuple[object, dict[str, Any]]] = []
    readiness_checks: list[object] = []

    def engine_factory(url: str, **kwargs: Any) -> RecordingEngine:
        calls.append((url, kwargs))
        return engine

    def session_factory_builder(
        created_engine: object, **kwargs: Any
    ) -> Callable[[], object]:
        built_sessions.append((created_engine, kwargs))
        return object

    async def readiness_check(created_engine: object) -> None:
        readiness_checks.append(created_engine)

    runtime = DatabaseRuntime(
        engine_factory=engine_factory,
        session_factory_builder=session_factory_builder,
        readiness_check=readiness_check,
    )

    first = await runtime.start(runtime_settings())
    second = await runtime.start(runtime_settings())

    assert first is second
    assert len(calls) == 1
    assert calls[0] == (
        DATABASE_URL,
        {
            "pool_size": 3,
            "max_overflow": 2,
            "pool_timeout": 7.0,
            "pool_pre_ping": True,
            "pool_reset_on_return": "rollback",
            "isolation_level": "READ COMMITTED",
            "connect_args": {
                "connect_timeout": 4,
                "options": "-csearch_path=pg_catalog,spine",
            },
            "hide_parameters": True,
        },
    )
    assert readiness_checks == [engine]
    assert built_sessions == [(engine, {"expire_on_commit": False, "autoflush": False})]


@pytest.mark.asyncio
async def test_default_startup_readiness_receives_validated_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = RecordingEngine()
    checked: list[tuple[object, RuntimeDatabaseSettings]] = []

    async def readiness_check(
        created_engine: object,
        settings: RuntimeDatabaseSettings,
    ) -> None:
        checked.append((created_engine, settings))

    monkeypatch.setattr(
        "spine.infrastructure.db.engine.verify_database_readiness",
        readiness_check,
    )
    settings = runtime_settings(runtime_role="spine_runtime")
    runtime = DatabaseRuntime(
        engine_factory=lambda _url, **_kwargs: engine,
        session_factory_builder=lambda _engine, **_kwargs: object,
    )

    await runtime.start(settings)

    assert checked == [(engine, settings)]


@pytest.mark.asyncio
async def test_start_never_invokes_alembic_or_creates_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = RecordingEngine()

    def unexpected_migration(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("application startup invoked Alembic")

    monkeypatch.setattr(alembic_command, "upgrade", unexpected_migration)
    monkeypatch.setattr(MetaData, "create_all", unexpected_migration)
    runtime = DatabaseRuntime(
        engine_factory=lambda _url, **_kwargs: engine,
        session_factory_builder=lambda _engine, **_kwargs: object,
        readiness_check=lambda _engine: _completed_check(),
    )

    await runtime.start(runtime_settings())

    assert runtime.resources is not None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_awaits_engine_disposal_and_is_idempotent() -> None:
    engine = RecordingEngine()
    runtime = DatabaseRuntime(
        engine_factory=lambda _url, **_kwargs: engine,
        session_factory_builder=lambda _engine, **_kwargs: object,
        readiness_check=lambda _engine: _completed_check(),
    )
    await runtime.start(runtime_settings())

    await runtime.shutdown()
    await runtime.shutdown()

    assert engine.dispose_awaited is True
    assert runtime.resources is None


@pytest.mark.asyncio
async def test_engine_factory_failure_redacts_database_credentials() -> None:
    def failing_factory(url: str, **_kwargs: Any) -> object:
        raise RuntimeError(f"could not connect to {url}")

    runtime = DatabaseRuntime(
        engine_factory=failing_factory,
        session_factory_builder=lambda _engine, **_kwargs: object,
    )

    with pytest.raises(DatabaseStartupError) as error:
        await runtime.start(runtime_settings())

    rendered = f"{error.value!r} {error.value}"
    assert "runtime_secret" not in rendered
    assert DATABASE_URL not in rendered
    assert "<redacted-database-url>" in rendered


def test_process_database_runtime_has_one_lifecycle_owner() -> None:
    assert get_process_database_runtime() is get_process_database_runtime()


async def _completed_check() -> None:
    return None


@pytest.mark.asyncio
async def test_readiness_failure_disposes_engine_and_fails_startup() -> None:
    engine = RecordingEngine()

    async def failing_readiness(_engine: object) -> None:
        raise RuntimeError(f"unsupported server for {DATABASE_URL}")

    runtime = DatabaseRuntime(
        engine_factory=lambda _url, **_kwargs: engine,
        session_factory_builder=lambda _engine, **_kwargs: object,
        readiness_check=failing_readiness,
    )

    with pytest.raises(DatabaseStartupError) as error:
        await runtime.start(runtime_settings())

    assert engine.dispose_awaited is True
    assert runtime.resources is None
    assert DATABASE_URL not in str(error.value)


@pytest.mark.asyncio
async def test_disposal_failure_is_sanitized_and_remains_retryable() -> None:
    class FailingEngine(RecordingEngine):
        async def dispose(self) -> None:
            raise RuntimeError(f"failure for {DATABASE_URL}")

    engine = FailingEngine()
    runtime = DatabaseRuntime(
        engine_factory=lambda _url, **_kwargs: engine,
        session_factory_builder=lambda _engine, **_kwargs: object,
        readiness_check=lambda _engine: _completed_check(),
    )
    await runtime.start(runtime_settings())

    with pytest.raises(DatabaseShutdownError) as error:
        await runtime.shutdown()

    assert DATABASE_URL not in str(error.value)
    assert "runtime_secret" not in str(error.value)
    assert runtime.resources is not None
