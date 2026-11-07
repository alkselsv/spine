"""Persist bounded source disposition state and its transactional wake-up intent."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270113_112"
down_revision = "20270112_111"
branch_labels = None
depends_on = None

SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_table(name: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    predicate = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name} FOR ALL TO {runtime} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name} FOR ALL TO {migration} "
        "USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {SCHEMA}.{name} TO {runtime}"
    )
    op.execute(
        f"REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE {SCHEMA}.{name} FROM {runtime}"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "source_disposition_operations",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("operation_id", uuid, nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("fence_active", sa.Boolean(), nullable=False),
        sa.Column("operation_data", postgresql.JSONB(), nullable=False),
        sa.Column("completion_ack_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "operation_id",
            name="pk_source_disposition_operations",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_source_disposition_operations_environment",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_source_disposition_operations_digest",
        ),
        sa.CheckConstraint(
            "state_version > 0",
            name="ck_source_disposition_operations_state_version",
        ),
        sa.CheckConstraint(
            "status IN ('blocked', 'deletion_pending', 'incomplete', 'unknown', 'verified', 'cancelled')",
            name="ck_source_disposition_operations_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(operation_data) = 'object'",
            name="ck_source_disposition_operations_data_object",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_source_disposition_operations_expiry",
        "source_disposition_operations",
        ["completion_ack_expires_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'verified'"),
    )
    op.create_table(
        "source_disposition_outbox",
        sa.Column("event_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("operation_id", uuid, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_source_disposition_outbox"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "operation_id"],
            [
                f"{SCHEMA}.source_disposition_operations.workspace_id",
                f"{SCHEMA}.source_disposition_operations.environment_id",
                f"{SCHEMA}.source_disposition_operations.operation_id",
            ],
            name="fk_source_disposition_outbox_operation",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "operation_id", "event_type",
            name="uq_source_disposition_outbox_operation_event",
        ),
        sa.CheckConstraint(
            "event_type = 'source.disposition.requested.v1'",
            name="ck_source_disposition_outbox_event_type",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_source_disposition_outbox_created",
        "source_disposition_outbox",
        ["created_at", "event_id"],
        schema=SCHEMA,
    )
    _tenant_table("source_disposition_operations")
    _tenant_table("source_disposition_outbox")


def downgrade() -> None:
    op.drop_index(
        "ix_source_disposition_outbox_created",
        table_name="source_disposition_outbox",
        schema=SCHEMA,
    )
    op.drop_table("source_disposition_outbox", schema=SCHEMA)
    op.drop_index(
        "ix_source_disposition_operations_expiry",
        table_name="source_disposition_operations",
        schema=SCHEMA,
    )
    op.drop_table("source_disposition_operations", schema=SCHEMA)
