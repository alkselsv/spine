"""Persist exact requirements acceptance decisions and terminal outcomes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261224_92"
down_revision = "20261223_91"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts", schema=SCHEMA, type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts",
        "command_type IN ('add_context', 'edit_requirement', 'reclassify_requirement', 'save_review', "
        "'request_rework', 'accept_requirements', 'answer_clarification', 'cancel')",
        schema=SCHEMA,
    )
    op.drop_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events", schema=SCHEMA, type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events",
        "event_type IN ('workflow.run-started', 'workflow.context-added', "
        "'workflow.requirement-edited', 'workflow.requirements-reviewed', "
        "'workflow.requirement-reclassified', "
        "'workflow.requirements-rework-requested', 'workflow.requirements-accepted', "
        "'workflow.clarification-requested', 'workflow.clarification-answered', "
        "'workflow.run-cancelled')",
        schema=SCHEMA,
    )
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "requirements_acceptance_decisions",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("command_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=False),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "command_id",
            name="pk_requirements_acceptance_decisions",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "command_id"],
            [f"{SCHEMA}.workflow_command_receipts.workspace_id",
             f"{SCHEMA}.workflow_command_receipts.environment_id",
             f"{SCHEMA}.workflow_command_receipts.command_id"],
            name="fk_requirements_acceptance_command",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "artifact_id", "artifact_version"],
            [f"{SCHEMA}.requirements_artifact_versions.workspace_id",
             f"{SCHEMA}.requirements_artifact_versions.environment_id",
             f"{SCHEMA}.requirements_artifact_versions.artifact_id",
             f"{SCHEMA}.requirements_artifact_versions.artifact_version"],
            name="fk_requirements_acceptance_artifact",
        ),
        sa.CheckConstraint(
            "artifact_version >= 1 AND artifact_digest ~ '^[0-9a-f]{64}$'",
            name="ck_requirements_acceptance_artifact",
        ),
        schema=SCHEMA,
    )
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    table = f"{SCHEMA}.requirements_acceptance_decisions"
    scope = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY pol_requirements_acceptance_decisions_tenant_isolation ON {table} "
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f"CREATE POLICY pol_requirements_acceptance_decisions_migration_maintenance ON {table} "
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {table} FROM {runtime}"
    )


def downgrade() -> None:
    op.drop_table("requirements_acceptance_decisions", schema=SCHEMA)
    op.drop_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events", schema=SCHEMA, type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events",
        "event_type IN ('workflow.run-started', 'workflow.context-added', "
        "'workflow.requirement-edited', 'workflow.requirements-reviewed', "
        "'workflow.requirements-rework-requested', 'workflow.clarification-requested', "
        "'workflow.clarification-answered', 'workflow.run-cancelled')",
        schema=SCHEMA,
    )
    op.drop_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts", schema=SCHEMA, type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts",
        "command_type IN ('add_context', 'edit_requirement', 'save_review', "
        "'request_rework', 'answer_clarification', 'cancel')",
        schema=SCHEMA,
    )
