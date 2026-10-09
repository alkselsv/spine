from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from spine.infrastructure.db.migrations import (
    ALEMBIC_VERSION_TABLE,
    MIGRATION_METADATA,
    SPINE_SCHEMA,
    collect_migration_preflight,
    validate_migration_preflight,
)
from spine.infrastructure.db.settings import (
    POSTGRESQL_SEARCH_PATH_OPTIONS,
    MigrationDatabaseSettings,
)


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _settings() -> MigrationDatabaseSettings:
    configured = config.attributes.get("migration_settings")
    if configured is not None:
        if not isinstance(configured, MigrationDatabaseSettings):
            raise TypeError("migration_settings must be MigrationDatabaseSettings")
        return configured
    return MigrationDatabaseSettings()


def run_migrations_offline() -> None:
    raise RuntimeError("offline migrations are not supported")


def _run_migrations(connection: Connection, settings: MigrationDatabaseSettings) -> None:
    with connection.begin():
        state = collect_migration_preflight(
            connection,
            migration_role=settings.migration_role,
            runtime_role=settings.runtime_role,
        )
        validate_migration_preflight(
            state,
            migration_role=settings.migration_role,
            runtime_role=settings.runtime_role,
        )
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS {SPINE_SCHEMA} AUTHORIZATION CURRENT_USER")
        )
        context.configure(
            connection=connection,
            target_metadata=MIGRATION_METADATA,
            include_schemas=True,
            version_table=ALEMBIC_VERSION_TABLE,
            version_table_schema=SPINE_SCHEMA,
            transactional_ddl=True,
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


async def _run_async_migrations(settings: MigrationDatabaseSettings) -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = settings.url.get_secret_value()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
        connect_args={"options": POSTGRESQL_SEARCH_PATH_OPTIONS},
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations, settings)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations(_settings()))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
