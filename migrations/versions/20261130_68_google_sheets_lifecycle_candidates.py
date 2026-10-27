"""Persist Sheets row absence candidates and independent removal decisions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261130_68"
down_revision = "20261130_67"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def _tenant_table(name: str) -> None:
    runtime_role = _role("runtime_role")
    migration_role = _role("migration_role")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {SCHEMA}.{name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name} "
        f"FOR ALL TO {runtime_role} "
        "USING (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid) "
        "WITH CHECK (workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid)"
    )
    op.execute(
        f"CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name} "
        f"FOR ALL TO {_role('migration_role')} USING (true) WITH CHECK (true)"
    )
    op.execute(f"REVOKE ALL ON {SCHEMA}.{name} FROM {runtime_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {SCHEMA}.{name} TO {runtime_role}")
    op.execute(
        f"COMMENT ON TABLE {SCHEMA}.{name} IS "
        "'classification=tenant_operational_state; provider_content=forbidden; "
        "replayable lifecycle evidence'"
    )


def upgrade() -> None:
    op.create_table(
        "google_sheets_absence_candidates",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_identity", sa.Text(), nullable=False),
        sa.Column("external_object_id", sa.String(length=64), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_current_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_google_sheets_absence_candidates"),
        sa.UniqueConstraint(
            "sync_run_id", "scope_identity", "source_object_id",
            name="uq_google_sheets_absence_candidate_run_scope_source",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_google_sheets_absence_candidates_environment",
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"], [f"{SCHEMA}.google_sheets_sync_runs.sync_run_id"],
            name="fk_google_sheets_absence_candidates_run",
        ),
        sa.CheckConstraint(
            "length(scope_identity) BETWEEN 1 AND 1024",
            name=op.f("ck_google_sheets_absence_candidates_scope"),
        ),
        sa.CheckConstraint(
            "external_object_id ~ '^gsk1:[a-z2-7]{52}$'",
            name=op.f("ck_google_sheets_absence_candidates_identity"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "google_sheets_removal_decisions",
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_authorized", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", name="pk_google_sheets_removal_decisions"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_google_sheets_removal_decisions_environment",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            [f"{SCHEMA}.google_sheets_absence_candidates.candidate_id"],
            name="fk_google_sheets_removal_decisions_candidate",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'tombstoned', 'retained')",
            name=op.f("ck_google_sheets_removal_decisions_state"),
        ),
        schema=SCHEMA,
    )
    for name in ("google_sheets_absence_candidates", "google_sheets_removal_decisions"):
        _tenant_table(name)


def downgrade() -> None:
    op.drop_table("google_sheets_removal_decisions", schema=SCHEMA)
    op.drop_table("google_sheets_absence_candidates", schema=SCHEMA)
