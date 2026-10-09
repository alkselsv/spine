from __future__ import annotations

from pathlib import Path

import pytest

from spine.infrastructure.db import migrations as migration_module
from spine.infrastructure.db.migrations import (
    ALEMBIC_VERSION_TABLE,
    MigrationPreflightError,
    MigrationPreflightState,
    MIGRATION_METADATA,
    NAMING_CONVENTION,
    SPINE_SCHEMA,
    locate_migration_assets,
    validate_migration_preflight,
)


def test_migration_identifiers_and_naming_convention_are_stable() -> None:
    assert SPINE_SCHEMA == "spine"
    assert ALEMBIC_VERSION_TABLE == "alembic_version"
    assert NAMING_CONVENTION == {
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
    assert MIGRATION_METADATA.naming_convention == NAMING_CONVENTION


def test_migration_assets_resolve_in_source_checkout() -> None:
    config, scripts = locate_migration_assets()

    assert config.name == "alembic.ini"
    assert scripts.name == "migrations"
    assert (scripts / "env.py").is_file()


def test_migration_assets_resolve_from_installed_wheel_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_directory = tmp_path / "site-packages" / "spine" / "infrastructure" / "db"
    scripts = module_directory / "alembic_migrations"
    scripts.mkdir(parents=True)
    config = module_directory / "alembic.ini"
    config.write_text("[alembic]\n", encoding="utf-8")
    (scripts / "env.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        migration_module,
        "__file__",
        str(module_directory / "migrations.py"),
    )

    resolved_config, resolved_scripts = migration_module.locate_migration_assets()

    assert resolved_config == config
    assert resolved_scripts == scripts
    project = (Path(__file__).parents[3] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '"alembic.ini" = "spine/infrastructure/db/alembic.ini"' in project
    assert (
        '"migrations" = "spine/infrastructure/db/alembic_migrations"' in project
    )


def valid_preflight_state(**overrides: object) -> MigrationPreflightState:
    values: dict[str, object] = {
        "current_user": "spine_migration",
        "database_owner": "spine_migration",
        "migration_role_exists": True,
        "migration_role_is_superuser": False,
        "migration_role_can_create_roles": False,
        "migration_role_can_create_databases": False,
        "migration_role_bypasses_rls": False,
        "runtime_role_exists": True,
        "runtime_role_is_superuser": False,
        "runtime_role_bypasses_rls": False,
        "runtime_role_can_create_roles": False,
        "runtime_role_can_create_databases": False,
        "runtime_role_inherits_privileges": False,
        "runtime_role_has_memberships": False,
        "runtime_role_can_assume_migration": False,
        "runtime_role_can_create_in_database": False,
        "runtime_role_has_spine_schema_access": False,
        "runtime_role_has_tenant_table_privileges": False,
        "schema_owner": None,
    }
    values.update(overrides)
    return MigrationPreflightState(**values)  # type: ignore[arg-type]


def test_migration_preflight_accepts_empty_database_with_separated_roles() -> None:
    validate_migration_preflight(
        valid_preflight_state(),
        migration_role="spine_migration",
        runtime_role="spine_runtime",
    )


def test_migration_preflight_rejects_missing_or_privileged_runtime_role() -> None:
    for state in (
        valid_preflight_state(runtime_role_exists=False),
        valid_preflight_state(runtime_role_is_superuser=True),
        valid_preflight_state(runtime_role_bypasses_rls=True),
        valid_preflight_state(runtime_role_can_create_roles=True),
        valid_preflight_state(runtime_role_can_create_databases=True),
        valid_preflight_state(runtime_role_inherits_privileges=True),
        valid_preflight_state(runtime_role_has_memberships=True),
        valid_preflight_state(runtime_role_can_assume_migration=True),
        valid_preflight_state(runtime_role_can_create_in_database=True),
        valid_preflight_state(runtime_role_has_spine_schema_access=True),
        valid_preflight_state(runtime_role_has_tenant_table_privileges=True),
    ):
        try:
            validate_migration_preflight(
                state,
                migration_role="spine_migration",
                runtime_role="spine_runtime",
            )
        except MigrationPreflightError as error:
            assert str(error) == "database migration preflight failed"
        else:
            raise AssertionError("unsafe runtime role passed migration preflight")


def test_migration_preflight_rejects_wrong_database_or_schema_owner() -> None:
    for state in (
        valid_preflight_state(database_owner="operator"),
        valid_preflight_state(schema_owner="operator"),
        valid_preflight_state(current_user="operator"),
    ):
        try:
            validate_migration_preflight(
                state,
                migration_role="spine_migration",
                runtime_role="spine_runtime",
            )
        except MigrationPreflightError as error:
            assert str(error) == "database migration preflight failed"
        else:
            raise AssertionError("invalid ownership passed migration preflight")
