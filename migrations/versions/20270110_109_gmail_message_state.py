"""Persist Gmail label state and relationship provenance."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20270110_109"
down_revision = "20270109_108"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant_table(table: str, runtime: str, migration: str, grants: str) -> None:
    qualified = f"{SCHEMA}.{table}"
    scope = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{table}_tenant_isolation ON {qualified} "
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f"CREATE POLICY pol_{table}_migration_maintenance ON {qualified} "
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT {grants} ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def upgrade() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    op.create_table(
        "gmail_message_label_observations",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("history_id", sa.Numeric(32, 0), nullable=False),
        sa.Column("event_identity", sa.Text(), nullable=False),
        sa.Column("label_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("observation_id", name="pk_gmail_message_label_observations"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_gmail_label_observation_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_gmail_label_observation_environment",
        ),
        sa.CheckConstraint("history_id >= 0", name="ck_gmail_label_observation_history"),
        sa.CheckConstraint(
            "state IN ('present', 'absent')",
            name="ck_gmail_label_observation_state",
        ),
        sa.CheckConstraint(
            "length(event_identity) BETWEEN 1 AND 4096",
            name="ck_gmail_label_observation_event",
        ),
        sa.CheckConstraint(
            "length(label_id) BETWEEN 1 AND 1024",
            name="ck_gmail_label_observation_label",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(record) = 'object'",
            name="ck_gmail_label_observation_record",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "gmail_message_label_heads",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label_id", sa.Text(), nullable=False),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("history_id", sa.Numeric(32, 0), nullable=False),
        sa.Column("event_identity", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "source_object_id", "label_id",
            name="pk_gmail_message_label_heads",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            [f"{SCHEMA}.gmail_message_label_observations.observation_id"],
            name="fk_gmail_message_label_head_observation",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_gmail_message_label_head_environment",
        ),
        sa.CheckConstraint("history_id >= 0", name="ck_gmail_message_label_head_history"),
        sa.CheckConstraint(
            "state IN ('present', 'absent')",
            name="ck_gmail_message_label_head_state",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "gmail_message_relationship_evidence",
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("authority", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_gmail_message_relationship_evidence"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_gmail_relationship_evidence_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_gmail_relationship_evidence_environment",
        ),
        sa.CheckConstraint(
            "(kind = 'provider_thread' AND authority = 'provider_reported_thread_id') OR "
            "(kind IN ('references', 'in_reply_to') AND authority = 'header_inferred')",
            name="ck_gmail_relationship_evidence_authority",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(record) = 'object'",
            name="ck_gmail_relationship_evidence_record",
        ),
        schema=SCHEMA,
    )
    _tenant_table("gmail_message_label_observations", runtime, migration, "SELECT, INSERT")
    _tenant_table("gmail_message_label_heads", runtime, migration, "SELECT, INSERT, UPDATE")
    _tenant_table("gmail_message_relationship_evidence", runtime, migration, "SELECT, INSERT")
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_gmail_message_evidence_mutation() RETURNS trigger "
        "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine AS $fn$ "
        "BEGIN RAISE EXCEPTION 'Gmail message evidence is immutable.' USING ERRCODE = '23514'; "
        "END; $fn$"
    )
    for table in (
        "gmail_message_label_observations",
        "gmail_message_relationship_evidence",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {SCHEMA}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_gmail_message_evidence_mutation()"
        )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_gmail_message_evidence_mutation() "
        f"FROM PUBLIC, {runtime}"
    )


def downgrade() -> None:
    for table in (
        "gmail_message_relationship_evidence",
        "gmail_message_label_observations",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {SCHEMA}.{table}")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_gmail_message_evidence_mutation()")
    op.drop_table("gmail_message_relationship_evidence", schema=SCHEMA)
    op.drop_table("gmail_message_label_heads", schema=SCHEMA)
    op.drop_table("gmail_message_label_observations", schema=SCHEMA)
