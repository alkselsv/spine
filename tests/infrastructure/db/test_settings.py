from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    RuntimeDatabaseSettings,
    TestDatabaseSettings as DatabaseTestSettings,
)

RUNTIME_URL = "postgresql+psycopg://runtime_user:runtime_secret@db/runtime"
MIGRATION_URL = "postgresql+psycopg://migration_user:migration_secret@db/migration"
TEST_URL = "postgresql+psycopg://test_user:test_secret@db/spine_test_explicit"


def test_runtime_settings_are_frozen_and_reject_unknown_fields() -> None:
    settings = RuntimeDatabaseSettings(url=RUNTIME_URL)

    with pytest.raises(ValidationError):
        settings.pool_size = 10  # type: ignore[misc]

    with pytest.raises(ValidationError, match="extra_forbidden"):
        RuntimeDatabaseSettings(url=RUNTIME_URL, migration_url=MIGRATION_URL)


def test_database_settings_representations_redact_credentials() -> None:
    settings = RuntimeDatabaseSettings(url=RUNTIME_URL)

    rendered = f"{settings!r} {settings.model_dump()!r}"

    assert "runtime_secret" not in rendered
    assert "runtime_user" not in rendered
    assert "**********" in rendered


def test_database_validation_error_redacts_invalid_url_credentials() -> None:
    with pytest.raises(ValidationError) as error:
        RuntimeDatabaseSettings(
            url="postgresql+asyncpg://runtime_user:runtime_secret@db/runtime"
        )

    rendered = str(error.value)
    assert "runtime_secret" not in rendered
    assert "runtime_user" not in rendered


def test_runtime_migration_and_test_environment_surfaces_are_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPINE_DATABASE_URL", RUNTIME_URL)
    monkeypatch.setenv("SPINE_MIGRATION_DATABASE_URL", MIGRATION_URL)
    monkeypatch.setenv("SPINE_TEST_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("SPINE_TEST_DATABASE_EXPECTED_NAME", "spine_test_explicit")
    monkeypatch.setenv(
        "SPINE_TEST_DATABASE_DISPOSABLE_MARKER",
        "spine-test-harness:v1:explicit-marker",
    )

    runtime = RuntimeDatabaseSettings()
    migration = MigrationDatabaseSettings()
    test = DatabaseTestSettings()

    assert runtime.url.get_secret_value() == RUNTIME_URL
    assert migration.url.get_secret_value() == MIGRATION_URL
    assert test.url is not None
    assert test.url.get_secret_value() == TEST_URL


def test_test_settings_never_fall_back_to_runtime_or_migration_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPINE_DATABASE_URL", RUNTIME_URL)
    monkeypatch.setenv("SPINE_MIGRATION_DATABASE_URL", MIGRATION_URL)
    monkeypatch.delenv("SPINE_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("SPINE_TEST_DATABASE_EXPECTED_NAME", raising=False)
    monkeypatch.delenv("SPINE_TEST_DATABASE_DISPOSABLE_MARKER", raising=False)

    settings = DatabaseTestSettings(
        url=None,
        expected_name=None,
        disposable_marker=None,
        use_testcontainers=False,
    )

    assert settings.url is None


def test_explicit_test_url_requires_identity_and_marker() -> None:
    with pytest.raises(ValidationError, match="expected_name.*disposable_marker"):
        DatabaseTestSettings(
            url=SecretStr(TEST_URL),
            expected_name=None,
            disposable_marker=None,
        )


@pytest.mark.parametrize(
    "settings_type,url",
    [
        (RuntimeDatabaseSettings, "sqlite+aiosqlite:///runtime.db"),
        (MigrationDatabaseSettings, "postgresql+asyncpg://user:secret@db/migration"),
    ],
)
def test_database_settings_reject_unsupported_drivers_without_leaking_url(
    settings_type: type[RuntimeDatabaseSettings] | type[MigrationDatabaseSettings],
    url: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        settings_type(url=SecretStr(url))

    rendered = str(error.value)
    assert "secret" not in rendered
    assert url not in rendered
