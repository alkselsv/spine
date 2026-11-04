"""Persist immutable private-ASR manifests and a current release pointer."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings

revision = "20270108_107"
down_revision = "20270107_106"
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
    op.execute(f"""CREATE POLICY pol_{name}_tenant_isolation ON {SCHEMA}.{name}
        FOR SELECT TO {runtime_role} USING (
          workspace_id = nullif(current_setting('spine.workspace_id', true), '')::uuid
          AND environment_id = nullif(current_setting('spine.environment_id', true), '')::uuid
          AND purpose = nullif(current_setting('spine.purpose', true), ''))""")
    op.execute(f"""CREATE POLICY pol_{name}_migration_maintenance ON {SCHEMA}.{name}
        FOR ALL TO {migration_role} USING (true) WITH CHECK (true)""")
    op.execute(f"GRANT SELECT ON {SCHEMA}.{name} TO {runtime_role}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON {SCHEMA}.{name} FROM {runtime_role}")


def upgrade() -> None:
    op.create_table(
        "private_asr_corpus_manifests",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_version", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "purpose", "dataset_id", "manifest_id", name="pk_private_asr_corpus_manifests"),
        sa.UniqueConstraint("workspace_id", "environment_id", "purpose", "dataset_id", "manifest_version", name="uq_private_asr_corpus_manifest_version"),
        sa.CheckConstraint("purpose = 'asr_model_evaluation'", name="ck_private_asr_corpus_manifest_purpose"),
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_private_asr_corpus_manifest_digest"),
        schema=SCHEMA,
    )
    op.create_table(
        "private_asr_corpus_releases",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("manifest_id", postgresql.UUID(as_uuid=True)),
        sa.Column("manifest_version", sa.Text()),
        sa.Column("manifest_sha256", sa.String(64)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "environment_id", "purpose", "dataset_id", name="pk_private_asr_corpus_releases"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "purpose", "dataset_id", "manifest_id"], [f"{SCHEMA}.private_asr_corpus_manifests.workspace_id", f"{SCHEMA}.private_asr_corpus_manifests.environment_id", f"{SCHEMA}.private_asr_corpus_manifests.purpose", f"{SCHEMA}.private_asr_corpus_manifests.dataset_id", f"{SCHEMA}.private_asr_corpus_manifests.manifest_id"], name="fk_private_asr_corpus_release_manifest"),
        sa.CheckConstraint("purpose = 'asr_model_evaluation'", name="ck_private_asr_corpus_release_purpose"),
        sa.CheckConstraint("generation > 0", name="ck_private_asr_corpus_release_generation"),
        sa.CheckConstraint("status IN ('released','withdrawn')", name="ck_private_asr_corpus_release_status"),
        sa.CheckConstraint("(status = 'withdrawn' AND manifest_id IS NULL AND manifest_version IS NULL AND manifest_sha256 IS NULL) OR (status = 'released' AND manifest_id IS NOT NULL AND manifest_version IS NOT NULL AND manifest_sha256 ~ '^[0-9a-f]{64}$')", name="ck_private_asr_corpus_release_binding"),
        schema=SCHEMA,
    )
    _tenant_table("private_asr_corpus_manifests")
    _tenant_table("private_asr_corpus_releases")
    op.execute(f"""CREATE FUNCTION {SCHEMA}.reject_private_asr_manifest_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable private ASR manifest'; END $$""")
    op.execute(f"""CREATE TRIGGER trg_private_asr_manifest_immutable
        BEFORE UPDATE OR DELETE ON {SCHEMA}.private_asr_corpus_manifests
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_private_asr_manifest_mutation()""")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER trg_private_asr_manifest_immutable ON {SCHEMA}.private_asr_corpus_manifests")
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_private_asr_manifest_mutation()")
    op.drop_table("private_asr_corpus_releases", schema=SCHEMA)
    op.drop_table("private_asr_corpus_manifests", schema=SCHEMA)
