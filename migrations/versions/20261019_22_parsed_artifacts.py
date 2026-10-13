"""Persist immutable parsed-document artifacts and their segments."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261019_22"
down_revision = "20261018_21"
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
    uuid_setting = lambda name: (
        "CASE WHEN pg_catalog.current_setting('spine.%s', true) "
        "OPERATOR(pg_catalog.~) "
        "'^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' "
        "THEN CAST(pg_catalog.current_setting('spine.%s', true) AS pg_catalog.uuid) ELSE NULL END"
        % (name, name)
    )
    scope = f"workspace_id = ({uuid_setting('workspace_id')}) AND environment_id = ({uuid_setting('environment_id')})"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def _immutable(table: str) -> None:
    runtime = _role("runtime_role")
    qualified = f'{SCHEMA}."{table}"'
    function = f"reject_{table}_mutation"
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        BEGIN
            RAISE EXCEPTION 'Parsed artifact history is immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{table}_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {qualified} "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC, {runtime}")


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    op.create_table(
        "parsed_documents",
        sa.Column("artifact_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("source_object_id", uuid_type, nullable=False),
        sa.Column("source_revision_id", uuid_type, nullable=False),
        sa.Column("source_revision_digest", sa.String(64), nullable=False),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_digest", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("parser_name", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("parser_config_reference", sa.Text(), nullable=False),
        sa.Column("locator_contract_version", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("coverage", sa.Text(), nullable=False),
        sa.Column("exclusions", postgresql.JSONB(), nullable=False),
        sa.Column("source_byte_length", sa.Integer(), nullable=False),
        sa.Column("source_codepoint_length", sa.Integer(), nullable=False),
        sa.Column("command_identity", sa.Text(), nullable=False),
        sa.Column("command_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("artifact_id", name="pk_parsed_documents"),
        sa.UniqueConstraint("workspace_id", "environment_id", "artifact_id", name="uq_parsed_documents_scope_id"),
        sa.UniqueConstraint("workspace_id", "environment_id", "artifact_digest", name="uq_parsed_documents_digest"),
        sa.UniqueConstraint("workspace_id", "environment_id", "command_identity", name="uq_parsed_documents_command"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id",
             f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"],
            name="fk_parsed_documents_revision",
        ),
        sa.CheckConstraint("schema_version = 'parsed-document:v1'", name="ck_parsed_documents_schema"),
        sa.CheckConstraint("coverage IN ('complete', 'partial', 'failed')", name="ck_parsed_documents_coverage"),
        sa.CheckConstraint("source_revision_digest ~ '^[0-9a-f]{64}$' AND original_sha256 ~ '^[0-9a-f]{64}$' AND artifact_digest ~ '^[0-9a-f]{64}$' AND command_digest ~ '^[0-9a-f]{64}$'", name="ck_parsed_documents_digests"),
        sa.CheckConstraint("jsonb_typeof(exclusions) = 'array'", name="ck_parsed_documents_exclusions"),
        schema=SCHEMA,
    )
    op.create_table(
        "parsed_document_segments",
        sa.Column("segment_id", uuid_type, nullable=False),
        sa.Column("artifact_id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("environment_id", uuid_type, nullable=False),
        sa.Column("segment_ordinal", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("segment_digest", sa.String(64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_digest", sa.String(64), nullable=False),
        sa.Column("locator", postgresql.JSONB(), nullable=False),
        sa.Column("heading_path", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("segment_id", name="pk_parsed_document_segments"),
        sa.UniqueConstraint("workspace_id", "environment_id", "artifact_id", "segment_digest", name="uq_parsed_segments_digest"),
        sa.UniqueConstraint("workspace_id", "environment_id", "artifact_id", "segment_ordinal", name="uq_parsed_segments_ordinal"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "artifact_id"],
            [f"{SCHEMA}.parsed_documents.workspace_id", f"{SCHEMA}.parsed_documents.environment_id", f"{SCHEMA}.parsed_documents.artifact_id"],
            name="fk_parsed_segments_document",
        ),
        sa.CheckConstraint("schema_version = 'parsed-segment:v1'", name="ck_parsed_segments_schema"),
        sa.CheckConstraint("segment_ordinal >= 0 AND segment_digest ~ '^[0-9a-f]{64}$' AND text_digest ~ '^[0-9a-f]{64}$'", name="ck_parsed_segments_identity"),
        sa.CheckConstraint("jsonb_typeof(locator) = 'object' AND jsonb_typeof(heading_path) = 'array'", name="ck_parsed_segments_metadata"),
        schema=SCHEMA,
    )
    op.create_index("ix_parsed_documents_revision", "parsed_documents", ["workspace_id", "environment_id", "source_revision_id"], schema=SCHEMA)
    op.create_index("ix_parsed_segments_artifact", "parsed_document_segments", ["workspace_id", "environment_id", "artifact_id", "segment_ordinal"], schema=SCHEMA)
    for table in ("parsed_documents", "parsed_document_segments"):
        _tenant(table)
        _immutable(table)


def downgrade() -> None:
    runtime = _role("runtime_role")
    for table in ("parsed_document_segments", "parsed_documents"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
        op.drop_table(table, schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
