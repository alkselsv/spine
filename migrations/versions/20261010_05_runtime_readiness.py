"""Expose the schema revision to the restricted runtime readiness probe.

Revision ID: 20261010_05
Revises: 20261010_04
"""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_05"
down_revision = "20261010_04"
branch_labels = None
depends_on = None

SCHEMA = "spine"
TABLE = "alembic_version"


def _runtime_role() -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(settings.runtime_role)


def upgrade() -> None:
    runtime_role = _runtime_role()
    op.execute(
        f'GRANT SELECT ON TABLE {SCHEMA}."{TABLE}" TO {runtime_role}'
    )


def downgrade() -> None:
    runtime_role = _runtime_role()
    op.execute(
        f'REVOKE SELECT ON TABLE {SCHEMA}."{TABLE}" FROM {runtime_role}'
    )
