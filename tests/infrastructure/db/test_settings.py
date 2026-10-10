from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from spine.infrastructure.db.settings import (
    MigrationDatabaseSettings,
    OperatorDatabaseSettings,
    RuntimeDatabaseSettings,
    TestDatabaseSettings as DatabaseTestSettings,
)

RUNTIME_URL = "postgresql+psycopg://runtime_user:runtime_secret@db/runtime"
MIGRATION_URL = "postgresql+psycopg://migration_user:migration_secret@db/migration"
TEST_URL = "postgresql+psycopg://test_user:test_secret@db/spine_test_explicit"
OPERATOR_URL = "postgresql+psycopg://operator:operator_secret@db/postgres"


def test_runtime_settings_are_frozen_and_reject_unknown_fields() -> None:
    settings = RuntimeDatabaseSettings(url=RUNTIME_URL)

    with pytest.raises(ValidationError):
        settings.pool_size = 10  # type: ignore[misc]

    with pytest.raises(ValidationError, match="extra_forbidden") as error:
        RuntimeDatabaseSettings(url=RUNTIME_URL, migration_url=MIGRATION_URL)

    rendered = f"{error.value.errors()!r} {error.value.json()}"
    assert "migration_secret" not in rendered
    assert "migration_user" not in rendered


def test_runtime_settings_require_safe_expected_runtime_role() -> None:
    settings = RuntimeDatabaseSettings(
        url=RUNTIME_URL,
        runtime_role="spine_runtime",
    )

    assert settings.runtime_role == "spine_runtime"

    with pytest.raises(ValidationError):
        RuntimeDatabaseSettings(
            url=RUNTIME_URL,
            runtime_role='unsafe"role',
        )


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

    rendered = f"{error.value!r} {error.value.errors()!r} {error.value.json()}"
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


def test_operator_settings_are_explicit_and_separate_from_runtime_credentials() -> None:
    settings = OperatorDatabaseSettings(
        url=OPERATOR_URL,
        database_name="spine",
        migration_role="spine_migration",
        migration_password="migration-secret",
        runtime_role="spine_runtime",
        runtime_password="runtime-secret",
    )

    assert settings.database_name == "spine"
    assert settings.migration_role == "spine_migration"
    assert settings.runtime_role == "spine_runtime"
    rendered = repr(settings)
    assert "operator_secret" not in rendered
    assert "migration-secret" not in rendered
    assert "runtime-secret" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_name", "spine; DROP DATABASE postgres"),
        ("migration_role", "spine-migration"),
        ("runtime_role", "SpineRuntime"),
    ],
)
def test_operator_settings_reject_unsafe_identifiers(field: str, value: str) -> None:
    values = {
        "url": OPERATOR_URL,
        "database_name": "spine",
        "migration_role": "spine_migration",
        "migration_password": "migration-secret",
        "runtime_role": "spine_runtime",
        "runtime_password": "runtime-secret",
    }
    values[field] = value

    with pytest.raises(ValidationError, match="identifier"):
        OperatorDatabaseSettings(**values)


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
    with pytest.raises(ValidationError, match="expected_name.*disposable_marker") as error:
        DatabaseTestSettings(
            url=SecretStr(TEST_URL),
            expected_name=None,
            disposable_marker=None,
        )

    rendered = f"{error.value.errors()!r} {error.value.json()}"
    assert "test_secret" not in rendered
    assert "test_user" not in rendered


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

    rendered = f"{error.value!r} {error.value.errors()!r} {error.value.json()}"
    assert "secret" not in rendered
    assert url not in rendered
