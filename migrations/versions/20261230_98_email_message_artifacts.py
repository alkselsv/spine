"""Persist immutable email body artifacts with tenant isolation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20261230_98"
down_revision = "20261229_97"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(attribute: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, attribute))


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_unique_constraint(
        "uq_canonical_acceptances_scope_source_revision",
        "canonical_acceptances",
        [
            "workspace_id",
            "environment_id",
            "canonical_acceptance_id",
            "source_object_id",
            "source_revision_id",
        ],
        schema=SCHEMA,
    )
    op.create_table(
        "email_message_artifacts",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("canonical_acceptance_id", uuid, nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "artifact_id", name="pk_email_message_artifacts"),
        sa.UniqueConstraint("workspace_id", "environment_id", "command_identity", name="uq_email_message_artifacts_command"),
        sa.ForeignKeyConstraint(
            [
                "workspace_id",
                "environment_id",
                "canonical_acceptance_id",
                "source_object_id",
                "source_revision_id",
            ],
            [
                f"{SCHEMA}.canonical_acceptances.workspace_id",
                f"{SCHEMA}.canonical_acceptances.environment_id",
                f"{SCHEMA}.canonical_acceptances.canonical_acceptance_id",
                f"{SCHEMA}.canonical_acceptances.source_object_id",
                f"{SCHEMA}.canonical_acceptances.source_revision_id",
            ],
            name="fk_email_message_artifacts_acceptance",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_email_message_artifacts_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_email_message_artifacts_environment",
        ),
        sa.CheckConstraint("artifact_digest ~ '^[0-9a-f]{64}$' AND command_digest ~ '^[0-9a-f]{64}$'", name="ck_email_message_artifacts_digests"),
        sa.CheckConstraint("jsonb_typeof(record) = 'object'", name="ck_email_message_artifacts_record"),
        schema=SCHEMA,
    )
    table = f"{SCHEMA}.email_message_artifacts"
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    scope = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY pol_email_message_artifacts_tenant_isolation ON {table} FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})")
    op.execute(f"CREATE POLICY pol_email_message_artifacts_migration_maintenance ON {table} FOR ALL TO {migration} USING (true) WITH CHECK (true)")
    op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {table} FROM {runtime}")
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_email_message_artifact_mutation() RETURNS trigger "
        "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine AS $fn$ "
        "BEGIN RAISE EXCEPTION 'Email artifact history is immutable.' USING ERRCODE = '23514'; "
        "END; $fn$"
    )
    op.execute(f"CREATE TRIGGER trg_email_message_artifact_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_email_message_artifact_mutation()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_email_message_artifact_mutation() FROM PUBLIC, {runtime}")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER trg_email_message_artifact_immutable ON {SCHEMA}.email_message_artifacts")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_email_message_artifact_mutation()")
    op.drop_table("email_message_artifacts", schema=SCHEMA)
    op.drop_constraint(
        "uq_canonical_acceptances_scope_source_revision",
        "canonical_acceptances",
        schema=SCHEMA,
        type_="unique",
    )
