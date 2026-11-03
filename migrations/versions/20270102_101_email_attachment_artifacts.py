"""Persist immutable email attachment originals with exact MIME part lineage."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20270102_101"
down_revision = "20270101_100"
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
    op.create_table(
        "email_attachment_artifacts",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("artifact_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("canonical_acceptance_id", uuid, nullable=False),
        sa.Column("mime_part_path", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("extraction_profile", sa.Text(), nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "artifact_id", name="pk_email_attachment_artifacts"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "source_revision_id", "mime_part_path",
            "extraction_profile", name="uq_email_attachment_artifacts_part_profile",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "canonical_acceptance_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.canonical_acceptances.workspace_id", f"{SCHEMA}.canonical_acceptances.environment_id", f"{SCHEMA}.canonical_acceptances.canonical_acceptance_id", f"{SCHEMA}.canonical_acceptances.source_object_id", f"{SCHEMA}.canonical_acceptances.source_revision_id"],
            name="fk_email_attachment_artifacts_acceptance",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_email_attachment_artifacts_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_email_attachment_artifacts_environment",
        ),
        sa.CheckConstraint("mime_part_path ~ '^1(\\.[1-9][0-9]*)*$'", name="ck_email_attachment_artifacts_part_path"),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$' AND artifact_digest ~ '^[0-9a-f]{64}$'", name="ck_email_attachment_artifacts_digests"),
        sa.CheckConstraint("jsonb_typeof(record) = 'object'", name="ck_email_attachment_artifacts_record"),
        sa.CheckConstraint("record->>'source_revision_id' = source_revision_id::text AND record->>'mime_part_path' = mime_part_path AND record->>'content_sha256' = content_sha256", name="ck_email_attachment_artifacts_record_binding"),
        schema=SCHEMA,
    )
    table = f"{SCHEMA}.email_attachment_artifacts"
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    scope = (
        "workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid "
        "AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY pol_email_attachment_artifacts_tenant_isolation ON {table} FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})")
    op.execute(f"CREATE POLICY pol_email_attachment_artifacts_migration_maintenance ON {table} FOR ALL TO {migration} USING (true) WITH CHECK (true)")
    op.execute(f"GRANT SELECT, INSERT ON TABLE {table} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {table} FROM {runtime}")
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_email_attachment_artifact_mutation() RETURNS trigger "
        "LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, spine AS $fn$ "
        "BEGIN RAISE EXCEPTION 'Email attachment history is immutable.' USING ERRCODE = '23514'; "
        "END; $fn$"
    )
    op.execute(f"CREATE TRIGGER trg_email_attachment_artifact_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_email_attachment_artifact_mutation()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_email_attachment_artifact_mutation() FROM PUBLIC, {runtime}")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER trg_email_attachment_artifact_immutable ON {SCHEMA}.email_attachment_artifacts")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_email_attachment_artifact_mutation()")
    op.drop_table("email_attachment_artifacts", schema=SCHEMA)
