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
    UnsafeTestTargetError,
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
