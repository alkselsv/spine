"""Persist mutable upload command state beside immutable original bytes."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261114_51"
down_revision = "20261113_50"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "original_upload_records",
        sa.Column("upload_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("upload_id", name="pk_original_upload_records"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "upload_id",
            name="uq_original_upload_records_scope_id",
        ),
        sa.CheckConstraint("record_version >= 1", name="ck_original_upload_record_version"),
        sa.CheckConstraint("jsonb_typeof(record) = 'object'", name="ck_original_upload_record_json"),
        schema=SCHEMA,
    )
    qualified = f'{SCHEMA}."original_upload_records"'
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    uuid_setting = lambda name: (
        "CASE WHEN pg_catalog.current_setting('spine.%s', true) "
        "OPERATOR(pg_catalog.~) "
        "'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' "
        "THEN CAST(pg_catalog.current_setting('spine.%s', true) AS pg_catalog.uuid) ELSE NULL END"
        % (name, name)
    )
    scope = (
        f"workspace_id = ({uuid_setting('workspace_id')}) AND "
        f"environment_id = ({uuid_setting('environment_id')})"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_original_upload_records_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_original_upload_records_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE {SCHEMA}.original_upload_records "
        f"FROM {_role('runtime_role')}"
    )
    op.drop_table("original_upload_records", schema=SCHEMA)
