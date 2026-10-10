"""Persist tenant-scoped source identities, revisions and provenance."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_07"
down_revision = "20261010_06"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _uuid_setting(name: str) -> str:
    setting = f"pg_catalog.current_setting('spine.{name}', true)"
    pattern = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    return f"CASE WHEN {setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({setting} AS pg_catalog.uuid) ELSE NULL END"


def _tenant(table: str) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    workspace = _uuid_setting("workspace_id")
    environment = _uuid_setting("environment_id")
    predicate = f"workspace_id = ({workspace}) AND environment_id = ({environment})"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} USING ({predicate}) WITH CHECK ({predicate})')
    op.execute(f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} USING (true) WITH CHECK (true)')
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {qualified} FROM {runtime}")


def _immutable(table: str) -> None:
    function = f"reject_{table}_mutation"
    qualified = f'{SCHEMA}."{table}"'
    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.{function}() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Source observation rows are immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_{table}_immutable';
        END; $function$
    """)
    op.execute(f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{function}()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{function}() FROM PUBLIC")


def upgrade() -> None:
    op.create_table(
        "source_objects",
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("identity_mode", sa.Text(), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True)),
        sa.Column("external_namespace", sa.Text()), sa.Column("external_generation", sa.Text()),
        sa.Column("external_object_id", sa.Text()), sa.Column("upload_identity", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source_object_id", name="pk_source_objects"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", name="uq_source_objects_scope_id"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id"], [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"], name="fk_source_objects_scope"),
        sa.CheckConstraint("identity_mode IN ('connector', 'upload')", name="ck_source_objects_identity_mode"),
        sa.CheckConstraint("(identity_mode = 'connector' AND connection_id IS NOT NULL AND external_namespace IS NOT NULL AND external_generation IS NOT NULL AND external_object_id IS NOT NULL AND upload_identity IS NULL) OR (identity_mode = 'upload' AND upload_identity IS NOT NULL AND connection_id IS NULL AND external_namespace IS NULL AND external_generation IS NULL AND external_object_id IS NULL)", name="ck_source_objects_identity_complete"),
        schema=SCHEMA,
    )
    op.create_index("uq_source_objects_connector_identity", "source_objects", ["workspace_id", "environment_id", "connection_id", "external_namespace", "external_generation", "external_object_id"], unique=True, schema=SCHEMA, postgresql_where=sa.text("identity_mode = 'connector'"))
    op.create_index("uq_source_objects_upload_identity", "source_objects", ["workspace_id", "environment_id", "upload_identity"], unique=True, schema=SCHEMA, postgresql_where=sa.text("identity_mode = 'upload'"))
    op.create_index("ix_source_objects_connection_lookup", "source_objects", ["workspace_id", "environment_id", "connection_id", "external_namespace", "external_object_id"], schema=SCHEMA)

    op.create_table(
        "source_revisions",
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False), sa.Column("revision_digest", sa.Text(), nullable=False), sa.Column("revision_schema_version", sa.Text(), nullable=False), sa.Column("revision_metadata_schema", sa.Text(), nullable=False), sa.Column("revision_metadata", postgresql.JSONB()), sa.Column("revision_metadata_digest", sa.Text()), sa.Column("original_reference", postgresql.JSONB()), sa.Column("original_sha256", sa.Text()), sa.Column("byte_length", sa.Integer()), sa.Column("media_type", sa.Text()), sa.Column("reappearance_after_tombstone_revision_id", postgresql.UUID(as_uuid=True)), sa.Column("deletion_reason", sa.Text()), sa.Column("deletion_provenance", sa.Text()), sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("revision_id", name="pk_source_revisions"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id"], [f"{SCHEMA}.source_objects.workspace_id", f"{SCHEMA}.source_objects.environment_id", f"{SCHEMA}.source_objects.source_object_id"], name="fk_source_revisions_source"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "reappearance_after_tombstone_revision_id"], [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"], name="fk_source_revisions_reappearance"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "revision_digest", name="uq_source_revisions_digest"),
        sa.CheckConstraint("kind IN ('content', 'tombstone')", name="ck_source_revisions_kind"),
        sa.UniqueConstraint("workspace_id", "environment_id", "source_object_id", "revision_id", name="uq_source_revisions_scope_id"),
        sa.CheckConstraint("revision_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'", name="ck_source_revisions_digest"),
        sa.CheckConstraint("(kind = 'content' AND original_reference IS NOT NULL AND original_sha256 IS NOT NULL AND byte_length IS NOT NULL AND byte_length >= 0 AND media_type IS NOT NULL AND revision_metadata IS NOT NULL AND revision_metadata_digest IS NOT NULL AND deletion_reason IS NULL AND deletion_provenance IS NULL) OR (kind = 'tombstone' AND original_reference IS NULL AND original_sha256 IS NULL AND byte_length IS NULL AND media_type IS NULL AND revision_metadata IS NULL AND revision_metadata_digest IS NULL AND reappearance_after_tombstone_revision_id IS NULL AND deletion_reason IS NOT NULL AND deletion_provenance IS NOT NULL)", name="ck_source_revisions_content_tombstone"),
        schema=SCHEMA,
    )
    op.create_index("ix_source_revisions_source_observed", "source_revisions", ["workspace_id", "environment_id", "source_object_id", "observed_at"], schema=SCHEMA)
    op.create_index("ix_source_revisions_original_digest", "source_revisions", ["workspace_id", "environment_id", "original_sha256"], schema=SCHEMA)

    op.create_table(
        "source_revision_provenance",
        sa.Column("provenance_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("producer_kind", sa.Text(), nullable=False), sa.Column("producer_reference", sa.Text(), nullable=False), sa.Column("event_identity", sa.Text(), nullable=False), sa.Column("event_digest", sa.Text(), nullable=False), sa.Column("connection_id", postgresql.UUID(as_uuid=True)), sa.Column("upload_command_reference", sa.Text()), sa.Column("origin_locator_kind", sa.Text()), sa.Column("origin_locator_value", sa.Text()), sa.Column("origin_locator_schema", sa.Text()), sa.Column("origin_locator_digest", sa.Text()), sa.Column("order_scheme", sa.Text()), sa.Column("order_token", sa.Text()), sa.Column("observer_service", sa.Text(), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False), sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provenance_id", name="pk_source_revision_provenance"),
        sa.ForeignKeyConstraint(["workspace_id", "environment_id", "source_object_id", "revision_id"], [f"{SCHEMA}.source_revisions.workspace_id", f"{SCHEMA}.source_revisions.environment_id", f"{SCHEMA}.source_revisions.source_object_id", f"{SCHEMA}.source_revisions.revision_id"], name="fk_source_revision_provenance_revision"),
        sa.UniqueConstraint("workspace_id", "environment_id", "producer_kind", "producer_reference", "event_identity", name="uq_source_revision_provenance_event"),
        sa.CheckConstraint("(order_scheme IS NULL) = (order_token IS NULL)", name="ck_source_revision_provenance_order_pair"),
        schema=SCHEMA,
    )
    op.create_index("ix_source_revision_provenance_revision_order", "source_revision_provenance", ["workspace_id", "environment_id", "revision_id", "order_scheme", "order_token"], schema=SCHEMA)
    for table in ("source_objects", "source_revisions", "source_revision_provenance"):
        _tenant(table)
        _immutable(table)


def downgrade() -> None:
    runtime = _role("runtime_role")
    for table in ("source_revision_provenance", "source_revisions", "source_objects"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
        op.drop_table(table, schema=SCHEMA)
        op.execute(f"DROP FUNCTION {SCHEMA}.reject_{table}_mutation()")
