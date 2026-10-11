"""Persist verified original receipts alongside source observations."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261014_17"
down_revision = "20261013_16"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'

    def _uuid_setting(name: str) -> str:
        return (
            "CASE WHEN pg_catalog.current_setting('spine.%s', true) "
            "OPERATOR(pg_catalog.~) "
            "'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' "
            "THEN CAST(pg_catalog.current_setting('spine.%s', true) AS pg_catalog.uuid) ELSE NULL END"
            % (name, name)
        )
    predicate = (
        f"workspace_id = ({_uuid_setting('workspace_id')}) AND "
        f"environment_id = ({_uuid_setting('environment_id')})"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {qualified} FROM {runtime}")


def _immutable() -> None:
    runtime = _role("runtime_role")
    qualified = f'{SCHEMA}."source_revision_receipt_associations"'
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_source_revision_receipt_associations_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Source receipt associations are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_source_receipt_associations_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_source_revision_receipt_associations_immutable "
        f"BEFORE UPDATE OR DELETE ON {qualified} FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.reject_source_revision_receipt_associations_mutation()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_source_revision_receipt_associations_mutation() "
        f"FROM PUBLIC, {runtime}"
    )


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "source_revision_receipt_associations",
        sa.Column("association_id", uuid_type, nullable=False),
        sa.Column("source_object_id", uuid_type, nullable=False),
        sa.Column("revision_id", uuid_type, nullable=False),
        sa.Column("provenance_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("original_receipt_kind", sa.Text(), nullable=False),
        sa.Column("original_receipt_id", uuid_type, nullable=False),
        sa.Column("original_command_result_id", uuid_type, nullable=False),
        sa.Column("original_command_result_schema_version", sa.Integer(), nullable=False),
        sa.Column("original_upload_id", uuid_type, nullable=False),
        sa.Column("original_reference", postgresql.JSONB(), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("storage_status", sa.Text(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("associated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("association_id", name="pk_source_revision_receipt_associations"),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "original_receipt_id",
            name="uq_source_receipt_associations_receipt",
        ),
        sa.UniqueConstraint(
            "workspace_id", "environment_id", "provenance_id",
            name="uq_source_receipt_associations_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "revision_id"],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_source_receipt_associations_revision",
        ),
        sa.ForeignKeyConstraint(
            ["provenance_id"],
            [f"{SCHEMA}.source_revision_provenance.provenance_id"],
            name="fk_source_receipt_associations_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["original_receipt_id"],
            [f"{SCHEMA}.idempotency_receipts.receipt_id"],
            name="fk_source_receipt_associations_receipt",
        ),
        sa.CheckConstraint(
            "original_receipt_kind = 'issue6.write_receipt' AND verification_status = 'verified' "
            "AND storage_status = 'finalized_verified' AND original_command_result_schema_version > 0 "
            "AND byte_length >= 0 AND original_sha256 OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' "
            "AND associated_at >= finalized_at "
            "AND association_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid "
            "AND source_object_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid "
            "AND revision_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid "
            "AND provenance_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid "
            "AND original_receipt_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid "
            "AND original_command_result_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid "
            "AND original_upload_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid",
            name="ck_source_receipt_associations_verified",
        ),
        sa.CheckConstraint(
            "pg_catalog.jsonb_typeof(original_reference) = 'object' "
            "AND (original_reference - 'schema_version' - 'object_id' - 'storage_generation' "
            "- 'digest_algorithm' - 'digest_hex' - 'byte_length') = '{}'::pg_catalog.jsonb "
            "AND original_reference ?& ARRAY['schema_version', 'object_id', 'storage_generation', "
            "'digest_algorithm', 'digest_hex', 'byte_length'] "
            "AND (original_reference->>'schema_version') OPERATOR(pg_catalog.~) '^[1-9][0-9]*$' "
            "AND (original_reference->>'object_id') OPERATOR(pg_catalog.~) "
            "'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' "
            "AND (original_reference->>'object_id') <> '00000000-0000-0000-0000-000000000000' "
            "AND (original_reference->>'storage_generation') OPERATOR(pg_catalog.~) "
            "'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' "
            "AND (original_reference->>'storage_generation') <> '00000000-0000-0000-0000-000000000000' "
            "AND original_reference->>'digest_algorithm' = 'sha256' "
            "AND original_reference->>'digest_hex' = original_sha256 "
            "AND (original_reference->>'digest_hex') OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' "
            "AND (original_reference->>'byte_length') OPERATOR(pg_catalog.~) '^[0-9]+$' "
            "AND (original_reference->>'byte_length') = byte_length::text",
            name="ck_source_receipt_associations_reference",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_source_receipt_associations_revision",
        "source_revision_receipt_associations",
        ["workspace_id", "environment_id", "revision_id", "associated_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_source_receipt_associations_object_digest",
        "source_revision_receipt_associations",
        ["workspace_id", "environment_id", "original_sha256"],
        schema=SCHEMA,
    )

    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.validate_source_receipt_association()
        RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE receipt_workspace uuid;
        DECLARE receipt_environment uuid;
        DECLARE revision_sha256 text;
        DECLARE revision_length integer;
        DECLARE provenance_revision uuid;
        DECLARE provenance_source uuid;
        BEGIN
            SELECT workspace_id, environment_id INTO receipt_workspace, receipt_environment
              FROM {SCHEMA}.idempotency_receipts WHERE receipt_id = NEW.original_receipt_id FOR KEY SHARE;
            SELECT original_sha256, byte_length INTO revision_sha256, revision_length
              FROM {SCHEMA}.source_revisions
             WHERE workspace_id = NEW.workspace_id AND environment_id = NEW.environment_id
               AND source_object_id = NEW.source_object_id AND revision_id = NEW.revision_id
               AND kind = 'content' FOR KEY SHARE;
            SELECT revision_id, source_object_id INTO provenance_revision, provenance_source
              FROM {SCHEMA}.source_revision_provenance
             WHERE provenance_id = NEW.provenance_id AND workspace_id = NEW.workspace_id
               AND environment_id = NEW.environment_id FOR KEY SHARE;
            IF receipt_workspace IS DISTINCT FROM NEW.workspace_id
               OR receipt_environment IS DISTINCT FROM NEW.environment_id
               OR revision_sha256 IS NULL
               OR revision_sha256 IS DISTINCT FROM NEW.original_sha256
               OR revision_length IS DISTINCT FROM NEW.byte_length
               OR provenance_revision IS DISTINCT FROM NEW.revision_id
               OR provenance_source IS DISTINCT FROM NEW.source_object_id
            THEN
                RAISE EXCEPTION 'Source receipt association is inconsistent.'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_source_receipt_associations_lineage';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_source_receipt_associations_lineage "
        f"AFTER INSERT ON {SCHEMA}.\"source_revision_receipt_associations\" "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "
        f"{SCHEMA}.validate_source_receipt_association()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_source_receipt_association() FROM PUBLIC"
    )
    _tenant("source_revision_receipt_associations")
    _immutable()


def downgrade() -> None:
    runtime = _role("runtime_role")
    qualified = f'{SCHEMA}."source_revision_receipt_associations"'
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
    op.execute(f"DROP TRIGGER trg_source_receipt_associations_lineage ON {qualified}")
    op.execute(f"DROP FUNCTION {SCHEMA}.validate_source_receipt_association()")
    op.drop_table("source_revision_receipt_associations", schema=SCHEMA)
    op.execute(
        f"DROP FUNCTION {SCHEMA}.reject_source_revision_receipt_associations_mutation()"
    )
