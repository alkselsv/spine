"""Persist immutable content-free trusted-content verifier evidence."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261219_87"
down_revision = "20261218_86"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def upgrade() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    uuid = postgresql.UUID(as_uuid=True)
    op.create_unique_constraint(
        "uq_source_receipt_associations_verifier_binding",
        "source_revision_receipt_associations",
        [
            "association_id",
            "workspace_id",
            "environment_id",
            "source_object_id",
            "revision_id",
            "original_receipt_id",
            "original_upload_id",
            "original_sha256",
            "byte_length",
        ],
        schema=SCHEMA,
    )
    op.create_table(
        "trusted_content_verification_evidence",
        sa.Column("evidence_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("receipt_association_id", uuid, nullable=False),
        sa.Column("original_receipt_id", uuid, nullable=False),
        sa.Column("original_upload_id", uuid, nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_trusted_content_evidence"),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "evidence_digest",
            name="uq_trusted_content_evidence_digest",
        ),
        sa.ForeignKeyConstraint(
            [
                "receipt_association_id",
                "workspace_id",
                "environment_id",
                "source_object_id",
                "source_revision_id",
                "original_receipt_id",
                "original_upload_id",
                "original_sha256",
                "byte_length",
            ],
            [
                f"{SCHEMA}.source_revision_receipt_associations.association_id",
                f"{SCHEMA}.source_revision_receipt_associations.workspace_id",
                f"{SCHEMA}.source_revision_receipt_associations.environment_id",
                f"{SCHEMA}.source_revision_receipt_associations.source_object_id",
                f"{SCHEMA}.source_revision_receipt_associations.revision_id",
                f"{SCHEMA}.source_revision_receipt_associations.original_receipt_id",
                f"{SCHEMA}.source_revision_receipt_associations.original_upload_id",
                f"{SCHEMA}.source_revision_receipt_associations.original_sha256",
                f"{SCHEMA}.source_revision_receipt_associations.byte_length",
            ],
            name="fk_trusted_content_evidence_exact_receipt",
        ),
        sa.CheckConstraint(
            "byte_length >= 0", name=op.f("ck_trusted_content_evidence_length")
        ),
        sa.CheckConstraint(
            "original_sha256 ~ '^[0-9a-f]{64}$' AND evidence_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_trusted_content_evidence_digests"),
        ),
        sa.CheckConstraint(
            "record->>'schema_version' = 'trusted-content-verification:v1' "
            "AND jsonb_typeof(record) = 'object' "
            "AND record - ARRAY['schema_version', 'evidence_id', 'workspace_id', "
            "'environment_id', 'source_object_id', 'source_revision_id', "
            "'receipt_association_id', 'original_receipt_id', 'original_upload_id', "
            "'original_sha256', 'byte_length', 'recognized_format', "
            "'format_profile_version', 'verifier_profile_version', "
            "'bounds_profile_version', 'outcome', 'reason_codes', "
            "'cleanup_outcome', 'verified_at', 'evidence_digest'] = '{}'::jsonb "
            "AND record->>'evidence_id' = evidence_id::text "
            "AND record->>'workspace_id' = workspace_id::text "
            "AND record->>'environment_id' = environment_id::text "
            "AND record->>'source_object_id' = source_object_id::text "
            "AND record->>'source_revision_id' = source_revision_id::text "
            "AND record->>'receipt_association_id' = receipt_association_id::text "
            "AND record->>'original_receipt_id' = original_receipt_id::text "
            "AND record->>'original_upload_id' = original_upload_id::text "
            "AND record->>'original_sha256' = original_sha256 "
            "AND record->>'byte_length' = byte_length::text "
            "AND record->>'evidence_digest' = evidence_digest",
            name=op.f("ck_trusted_content_evidence_record_binding"),
        ),
        schema=SCHEMA,
    )
    qualified = f'{SCHEMA}."trusted_content_verification_evidence"'
    ws = "nullif(current_setting('spine.workspace_id', true), '')::uuid"
    env = "nullif(current_setting('spine.environment_id', true), '')::uuid"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_trusted_content_verification_evidence_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING (workspace_id = {ws} AND environment_id = {env}) "
        f"WITH CHECK (workspace_id = {ws} AND environment_id = {env})"
    )
    op.execute(
        f'CREATE POLICY "pol_trusted_content_verification_evidence_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {qualified} FROM {runtime}")
    op.execute(
        f"CREATE FUNCTION {SCHEMA}.reject_trusted_content_evidence_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'trusted content verification evidence is immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        f"CREATE TRIGGER trg_trusted_content_evidence_immutable BEFORE UPDATE OR DELETE "
        f"ON {qualified} FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.reject_trusted_content_evidence_mutation()"
    )
    op.execute(
        f"COMMENT ON TABLE {qualified} IS "
        "'classification=content_free_verifier_evidence; immutable=true; raw_content_forbidden'"
    )


def downgrade() -> None:
    op.drop_table("trusted_content_verification_evidence", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_trusted_content_evidence_mutation()")
    op.drop_constraint(
        "uq_source_receipt_associations_verifier_binding",
        "source_revision_receipt_associations",
        type_="unique",
        schema=SCHEMA,
    )
