from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from spine.infrastructure.db.settings import (
    TESTCONTAINERS_POSTGRES_IMAGE,
    TestDatabaseSettings as DatabaseTestSettings,
)
from spine.infrastructure.db.test_harness import (
    PostgreSQLGateError,
    TestDatabaseProvision,
    TestTarget,
    UnsafeTestTargetError,
    install_container_marker,
    provision_test_database,
)


class FakeContainer:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    def start(self) -> FakeContainer:
        self.started = True
        return self

    def stop(self) -> None:
        self.stopped = True

    def get_connection_url(self) -> str:
        return (
            "postgresql+psycopg://"
            f"{self.kwargs['username']}:{self.kwargs['password']}@container/"
            f"{self.kwargs['dbname']}"
        )


def container_settings() -> DatabaseTestSettings:
    return DatabaseTestSettings(
        url=None,
        expected_name=None,
        disposable_marker=None,
    )


def test_container_provisioning_uses_unique_database_credentials_and_pinned_image() -> (
    None
):
    created: list[FakeContainer] = []

    def factory(**kwargs: Any) -> FakeContainer:
        container = FakeContainer(**kwargs)
        created.append(container)
        return container

    first = provision_test_database(
        container_settings(), run_id="session-a", container_factory=factory
    )
    second = provision_test_database(
        container_settings(), run_id="session-b", container_factory=factory
    )

    assert first.target.database_name != second.target.database_name
    assert created[0].kwargs["username"] != created[1].kwargs["username"]
    assert created[0].kwargs["password"] != created[1].kwargs["password"]
    assert created[0].kwargs["image"] == TESTCONTAINERS_POSTGRES_IMAGE
    assert created[0].kwargs["driver"] == "psycopg"


def test_explicit_url_requires_exact_expected_database_identity() -> None:
    settings = DatabaseTestSettings(
        url=SecretStr("postgresql+psycopg://user:secret@db/spine_test_explicit"),
        expected_name="spine_test_other",
        disposable_marker=SecretStr("spine-test-harness:v1:marker"),
    )

    with pytest.raises(UnsafeTestTargetError, match="identity"):
        provision_test_database(settings, run_id="explicit")


def test_missing_explicit_url_and_container_runtime_fails_mandatory_gate() -> None:
    settings = DatabaseTestSettings(
        url=None,
        expected_name=None,
        disposable_marker=None,
        use_testcontainers=False,
    )

    with pytest.raises(PostgreSQLGateError, match="mandatory PostgreSQL gate"):
        provision_test_database(settings, run_id="gate")


def test_container_start_failure_fails_gate_without_credentials() -> None:
    def failing_factory(**kwargs: Any) -> FakeContainer:
        raise RuntimeError(f"docker failure for {kwargs!r}")

    with pytest.raises(PostgreSQLGateError) as error:
        provision_test_database(
            container_settings(),
            run_id="gate",
            container_factory=failing_factory,
        )

    rendered = str(error.value)
    assert "password" not in rendered
    assert "postgresql" not in rendered


@pytest.mark.asyncio
async def test_marker_installation_keeps_marker_out_of_logged_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "spine-test-harness:v1:secret-marker"
    statements: list[str] = []
    parameters: list[dict[str, str] | None] = []
    engine_options: dict[str, object] = {}

    class FakeConnection:
        async def scalar(self, _statement: object) -> str:
            return "spine_test_container"

        async def execute(
            self,
            statement: object,
            values: dict[str, str] | None = None,
        ) -> None:
            statements.append(str(statement))
            parameters.append(values)

    class BeginContext:
        async def __aenter__(self) -> FakeConnection:
            return FakeConnection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeEngine:
        disposed = False

        def begin(self) -> BeginContext:
            return BeginContext()

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()

    def fake_engine_factory(_url: str, **kwargs: object) -> FakeEngine:
        engine_options.update(kwargs)
        return engine

    monkeypatch.setattr(
        "spine.infrastructure.db.test_harness.create_async_engine",
        fake_engine_factory,
    )
    provision = TestDatabaseProvision(
        url=SecretStr(
            "postgresql+psycopg://test_user:test_secret@db/spine_test_container"
        ),
        target=TestTarget(
            database_name="spine_test_container",
            disposable_marker=SecretStr(marker),
        ),
        container=FakeContainer(),
    )

    await install_container_marker(provision)

    assert engine_options["hide_parameters"] is True
    assert marker not in " ".join(statements)
    assert parameters == [{"marker": marker}, None]
    assert engine.disposed is True
