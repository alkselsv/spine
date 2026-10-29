"""Preserve provider revision evidence in canonical source metadata."""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261209_77"
down_revision = "20261208_76"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _metadata_shape_constraint(*, external_revision: bool) -> str:
    allowed_fields = "'embedded_title' - 'document_language' - 'representation_profile'"
    external_clause = ""
    if external_revision:
        allowed_fields += " - 'external_file_revision'"
        external_clause = (
            " AND (revision_metadata->'external_file_revision' IS NULL OR "
            "revision_metadata->'external_file_revision' = 'null'::pg_catalog.jsonb OR ("
            "pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision') = 'object' "
            "AND ((revision_metadata->'external_file_revision') - 'schema_version' - 'provider_kind' "
            "- 'provider_revision' - 'provider_resource_id' - 'resource_name' - 'resource_path' "
            "- 'content_md5' - 'content_sha256' - 'byte_length' - 'modified_at' "
            "- 'identity_assurance' - 'assurance_evidence_reference') = '{}'::pg_catalog.jsonb "
            "AND revision_metadata->'external_file_revision'->>'schema_version' = "
            "'external-file-revision-metadata:v1' "
            "AND pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'provider_kind') = 'string' "
            "AND pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'provider_revision') = 'string' "
            "AND pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'byte_length') = 'number' "
            "AND (revision_metadata->'external_file_revision'->>'byte_length') "
            "OPERATOR(pg_catalog.~) '^(0|[1-9][0-9]*)$' "
            "AND pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'modified_at') = 'string' "
            "AND pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'identity_assurance') = 'string' "
            "AND pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'assurance_evidence_reference') = 'string' "
            "AND (revision_metadata->'external_file_revision'->'provider_resource_id' IS NULL OR "
            "pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'provider_resource_id') IN ('string', 'null')) "
            "AND (revision_metadata->'external_file_revision'->'resource_name' IS NULL OR "
            "pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'resource_name') IN ('string', 'null')) "
            "AND (revision_metadata->'external_file_revision'->'resource_path' IS NULL OR "
            "pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'resource_path') IN ('string', 'null')) "
            "AND (revision_metadata->'external_file_revision'->'content_md5' IS NULL OR "
            "pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'content_md5') IN ('string', 'null')) "
            "AND (revision_metadata->'external_file_revision'->'content_sha256' IS NULL OR "
            "pg_catalog.jsonb_typeof(revision_metadata->'external_file_revision'->'content_sha256') IN ('string', 'null'))))"
        )
    return (
        "revision_metadata IS NULL OR (pg_catalog.jsonb_typeof(revision_metadata) = 'object' "
        f"AND (revision_metadata - {allowed_fields}) = '{{}}'::pg_catalog.jsonb "
        "AND (revision_metadata->'embedded_title' IS NULL OR "
        "pg_catalog.jsonb_typeof(revision_metadata->'embedded_title') IN ('string', 'null')) "
        "AND (revision_metadata->'document_language' IS NULL OR "
        "pg_catalog.jsonb_typeof(revision_metadata->'document_language') IN ('string', 'null')) "
        "AND (revision_metadata->'representation_profile' IS NULL OR "
        "pg_catalog.jsonb_typeof(revision_metadata->'representation_profile') IN ('string', 'null')) "
        "AND (revision_metadata->>'representation_profile' IS NULL OR "
        "(revision_metadata->>'representation_profile') OPERATOR(pg_catalog.~) "
        "'^[a-z][a-z0-9_.:-]{0,63}$')"
        f"{external_clause})"
    )


def _install_canonical_json_function(runtime: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.source_canonical_json(value pg_catalog.jsonb)
        RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            kind text;
            result text;
        BEGIN
            kind := pg_catalog.jsonb_typeof(value);
            IF kind = 'object' THEN
                SELECT '{{' || coalesce(pg_catalog.string_agg(
                    pg_catalog.to_json(entry.key)::text || ':' || {SCHEMA}.source_canonical_json(entry.value),
                    ',' ORDER BY entry.key COLLATE "C"), '') || '}}'
                  INTO result
                  FROM pg_catalog.jsonb_each(value) AS entry;
                RETURN result;
            ELSIF kind = 'array' THEN
                SELECT '[' || coalesce(pg_catalog.string_agg(
                    {SCHEMA}.source_canonical_json(entry.value), ',' ORDER BY entry.ordinality), '') || ']'
                  INTO result
                  FROM pg_catalog.jsonb_array_elements(value) WITH ORDINALITY AS entry(value, ordinality);
                RETURN result;
            ELSIF kind = 'string' THEN
                RETURN pg_catalog.to_json(value #>> '{{}}')::text;
            END IF;
            RETURN value::text;
        END; $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.source_canonical_json(pg_catalog.jsonb) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION {SCHEMA}.source_canonical_json(pg_catalog.jsonb) TO {runtime}"
    )


def _install_revision_trigger(runtime: str, *, external_revision: bool) -> None:
    external_assignment = ""
    if external_revision:
        external_assignment = (
            "                IF NEW.revision_metadata->'external_file_revision' IS NOT NULL "
            "AND NEW.revision_metadata->'external_file_revision' <> 'null'::pg_catalog.jsonb THEN\n"
            "                    metadata := metadata || pg_catalog.jsonb_build_object("
            "'external_file_revision', NEW.revision_metadata->'external_file_revision');\n"
            "                END IF;\n"
        )
    op.execute(f"DROP TRIGGER IF EXISTS trg_source_revision_canonical_validation ON {SCHEMA}.source_revisions")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.validate_source_revision_canonical() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            canonical text;
            metadata pg_catalog.jsonb;
            metadata_canonical text;
            metadata_digest text;
            title text;
            raw_title text;
            language_tag text;
            representation_profile text;
            unicode_digest text;
            bcp47_digest text;
        BEGIN
            SELECT table_digest INTO unicode_digest FROM {SCHEMA}.source_canonical_tables WHERE table_name = 'unicode';
            SELECT table_digest INTO bcp47_digest FROM {SCHEMA}.source_canonical_tables WHERE table_name = 'bcp47';
            IF NEW.canonicalization_profile <> 'r1-c14n-2026-10'
               OR NEW.unicode_table_digest <> unicode_digest
               OR NEW.bcp47_table_digest <> bcp47_digest THEN
                RAISE EXCEPTION 'Invalid source canonicalization profile.' USING ERRCODE = '23514';
            END IF;
            IF NEW.kind = 'content' THEN
                raw_title := NEW.revision_metadata->>'embedded_title';
                title := {SCHEMA}.source_canonical_trim(raw_title);
                language_tag := NEW.revision_metadata->>'document_language';
                representation_profile := NEW.revision_metadata->>'representation_profile';
                IF raw_title IS NOT NULL AND raw_title <> title THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                IF title IS NOT NULL AND title = '' THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                IF language_tag IS NOT NULL THEN
                    IF language_tag <> lower(language_tag) THEN
                        RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                    END IF;
                    language_tag := {SCHEMA}.source_canonical_language(language_tag);
                END IF;
                IF representation_profile IS NOT NULL
                   AND representation_profile !~ '^[a-z][a-z0-9_.:-]{{0,63}}$' THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                metadata := pg_catalog.jsonb_build_object(
                    'document_language', language_tag, 'embedded_title', title
                );
                IF representation_profile IS NOT NULL THEN
                    metadata := metadata || pg_catalog.jsonb_build_object(
                        'representation_profile', representation_profile
                    );
                END IF;
{external_assignment}                metadata_canonical := {SCHEMA}.source_canonical_json(metadata);
                metadata_digest := encode(pg_catalog.sha256(
                    pg_catalog.convert_to(metadata_canonical, 'UTF8')), 'hex');
                IF NEW.revision_metadata_digest <> metadata_digest THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                canonical := format('{{"bcp47_table":%s,"content":{{"byte_length":%s,'
                    '"media_type":%s,"metadata":%s,"metadata_digest":%s,"metadata_schema":%s,'
                    '"original_sha256":%s,"reappearance_after_tombstone_revision_id":%s}},'
                    '"kind":%s,"profile":%s,"schema":%s,"unicode_table":%s}}',
                    to_json(NEW.bcp47_table_digest)::text, to_json(NEW.byte_length::text)::text,
                    to_json(NEW.media_type)::text, metadata_canonical,
                    to_json(NEW.revision_metadata_digest)::text, to_json(NEW.revision_metadata_schema)::text,
                    to_json(NEW.original_sha256)::text,
                    coalesce(to_json(NEW.reappearance_after_tombstone_revision_id::text)::text, 'null'),
                    to_json(NEW.kind)::text, to_json(NEW.canonicalization_profile)::text,
                    to_json(NEW.revision_schema_version)::text, to_json(NEW.unicode_table_digest)::text);
            ELSE
                canonical := format('{{"bcp47_table":%s,"kind":%s,"profile":%s,"schema":%s,'
                    '"tombstone":{{"deletion_fact":"deletion-fact:r1-v1","reason":%s}},"unicode_table":%s}}',
                    to_json(NEW.bcp47_table_digest)::text, to_json(NEW.kind)::text,
                    to_json(NEW.canonicalization_profile)::text, to_json(NEW.revision_schema_version)::text,
                    to_json(NEW.deletion_reason)::text, to_json(NEW.unicode_table_digest)::text);
            END IF;
            IF encode(pg_catalog.sha256(convert_to(canonical, 'UTF8')), 'hex') <> NEW.revision_digest THEN
                RAISE EXCEPTION 'Invalid source revision digest.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_source_revision_canonical_validation "
        f"AFTER INSERT OR UPDATE ON {SCHEMA}.source_revisions DEFERRABLE INITIALLY DEFERRED "
        f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_source_revision_canonical()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_source_revision_canonical() FROM PUBLIC")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {SCHEMA}.source_revisions FROM {runtime}")


def _install(*, external_revision: bool) -> None:
    runtime = _role("runtime_role")
    op.drop_constraint(
        "ck_source_revisions_metadata_shape",
        table_name="source_revisions",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_revisions_metadata_shape",
        "source_revisions",
        _metadata_shape_constraint(external_revision=external_revision),
        schema=SCHEMA,
    )
    _install_canonical_json_function(runtime)
    _install_revision_trigger(runtime, external_revision=external_revision)


def upgrade() -> None:
    _install(external_revision=True)


def downgrade() -> None:
    op.execute(
        f"""
        DO $block$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM {SCHEMA}.source_revisions
                WHERE revision_metadata->'external_file_revision' IS NOT NULL
                  AND revision_metadata->'external_file_revision' <> 'null'::pg_catalog.jsonb
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while external file revision metadata exists.';
            END IF;
        END $block$
        """
    )
    _install(external_revision=False)
