"""Complete database-side invariants for source observations."""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_08"
down_revision = "20261010_07"
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
    source_objects = f'{SCHEMA}."source_objects"'
    revisions = f'{SCHEMA}."source_revisions"'
    provenance = f'{SCHEMA}."source_revision_provenance"'

    op.create_check_constraint(
        "ck_source_objects_kind_format", "source_objects",
        "source_kind OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$'", schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_objects_identity_format", "source_objects",
        "(identity_mode = 'connector' AND connection_id <> '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid AND external_namespace OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$' AND external_generation OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$' AND external_object_id OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$') OR (identity_mode = 'upload' AND upload_identity OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_schema_profile", "source_revisions",
        "revision_schema_version = 'source-revision:v1' AND revision_metadata_schema = 'revision-metadata:r1-document-v1'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_original_digest", "source_revisions",
        "original_sha256 IS NULL OR original_sha256 OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_original_reference", "source_revisions",
        "original_reference IS NULL OR (pg_catalog.jsonb_typeof(original_reference) = 'object' AND (original_reference - 'schema_version' - 'object_id' - 'storage_generation' - 'digest_algorithm' - 'digest_hex' - 'byte_length') = '{}'::pg_catalog.jsonb AND original_reference ?& ARRAY['schema_version', 'object_id', 'storage_generation', 'digest_algorithm', 'digest_hex', 'byte_length'] AND (original_reference->>'schema_version') OPERATOR(pg_catalog.~) '^[1-9][0-9]*$' AND (original_reference->>'object_id') OPERATOR(pg_catalog.~) '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' AND (original_reference->>'object_id') <> '00000000-0000-0000-0000-000000000000' AND (original_reference->>'storage_generation') OPERATOR(pg_catalog.~) '^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$' AND (original_reference->>'storage_generation') <> '00000000-0000-0000-0000-000000000000' AND original_reference->>'digest_algorithm' = 'sha256' AND (original_reference->>'digest_hex') OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' AND original_reference->>'digest_hex' = original_sha256 AND (original_reference->>'byte_length') OPERATOR(pg_catalog.~) '^[0-9]+$' AND (original_reference->>'byte_length') = byte_length::text)",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_media_type", "source_revisions",
        "media_type IS NULL OR media_type OPERATOR(pg_catalog.~) '^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_metadata_shape", "source_revisions",
        "revision_metadata IS NULL OR (pg_catalog.jsonb_typeof(revision_metadata) = 'object' AND (revision_metadata - 'embedded_title' - 'document_language') = '{}'::pg_catalog.jsonb AND (revision_metadata->'embedded_title' IS NULL OR pg_catalog.jsonb_typeof(revision_metadata->'embedded_title') IN ('string', 'null')) AND (revision_metadata->'document_language' IS NULL OR pg_catalog.jsonb_typeof(revision_metadata->'document_language') IN ('string', 'null')))",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_deletion_reason", "source_revisions",
        "deletion_reason IS NULL OR deletion_reason IN ('source_deleted', 'provider_deleted', 'legal_erasure', 'retention_expired')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_deletion_provenance", "source_revisions",
        "deletion_provenance IS NULL OR deletion_provenance OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revision_provenance_digest", "source_revision_provenance",
        "event_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$' AND (origin_locator_digest IS NULL OR origin_locator_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$')",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revision_provenance_fields", "source_revision_provenance",
        "producer_kind OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$' AND producer_reference OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$' AND event_identity OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$' AND observer_service OPERATOR(pg_catalog.~) '^[a-z][a-z0-9_.:-]{0,63}$'",
        schema=SCHEMA,
    )

    op.execute(f"""
        CREATE FUNCTION {SCHEMA}.check_source_reappearance() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE predecessor_kind text;
        BEGIN
            IF NEW.reappearance_after_tombstone_revision_id IS NOT NULL THEN
                IF NEW.kind <> 'content' THEN
                    RAISE EXCEPTION 'Invalid source reappearance.' USING ERRCODE = '23514';
                END IF;
                SELECT kind INTO predecessor_kind
                FROM {SCHEMA}.source_revisions
                WHERE workspace_id = NEW.workspace_id
                  AND environment_id = NEW.environment_id
                  AND source_object_id = NEW.source_object_id
                  AND revision_id = NEW.reappearance_after_tombstone_revision_id;
                IF predecessor_kind IS DISTINCT FROM 'tombstone' THEN
                    RAISE EXCEPTION 'Invalid source reappearance.' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END; $function$
    """)
    op.execute(f"CREATE CONSTRAINT TRIGGER trg_source_reappearance AFTER INSERT ON {revisions} DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.check_source_reappearance()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.check_source_reappearance() FROM PUBLIC")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {source_objects}, {revisions}, {provenance} FROM {runtime}")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_source_reappearance ON {SCHEMA}.source_revisions")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.check_source_reappearance()")
    for table, constraint in (
        ("source_revision_provenance", "ck_source_revision_provenance_fields"),
        ("source_revision_provenance", "ck_source_revision_provenance_digest"),
        ("source_revisions", "ck_source_revisions_deletion_reason"),
        ("source_revisions", "ck_source_revisions_deletion_provenance"),
        ("source_revisions", "ck_source_revisions_metadata_shape"),
        ("source_revisions", "ck_source_revisions_original_reference"),
        ("source_revisions", "ck_source_revisions_media_type"),
        ("source_revisions", "ck_source_revisions_original_digest"),
        ("source_revisions", "ck_source_revisions_schema_profile"),
        ("source_objects", "ck_source_objects_identity_format"),
        ("source_objects", "ck_source_objects_kind_format"),
    ):
        op.drop_constraint(constraint, table_name=table, schema=SCHEMA, type_="check")
