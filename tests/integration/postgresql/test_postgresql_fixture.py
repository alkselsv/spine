from __future__ import annotations

import pytest
from pydantic import SecretStr

from spine.infrastructure.db.test_harness import TestDatabaseProvision, TestTarget
from tests.integration.postgresql import conftest


class FakeContainer:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_postgresql_session_fixture_stops_owned_container_on_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeContainer()
    provision = TestDatabaseProvision(
        url=SecretStr("postgresql+psycopg://test:test@db/spine_test_fixture"),
        target=TestTarget(
            database_name="spine_test_fixture",
            disposable_marker=SecretStr("spine-test-harness:v1:fixture"),
        ),
        container=container,
    )

    monkeypatch.setattr(
        conftest,
        "provision_test_database",
        lambda *_args, **_kwargs: provision,
    )

    async def install_marker(_provision: TestDatabaseProvision) -> None:
        return None

    monkeypatch.setattr(conftest, "install_container_marker", install_marker)
    fixture = conftest.postgresql_provision.__wrapped__()

    yielded = await anext(fixture)
    await fixture.aclose()

    assert yielded is provision
    assert container.stopped is True
