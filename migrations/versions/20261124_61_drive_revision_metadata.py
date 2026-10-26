"""Allow versioned connector representation profiles in source revisions."""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261124_61"
down_revision = "20261123_60"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _metadata_shape_constraint(*, representation_profile: bool) -> str:
    allowed_fields = "'embedded_title' - 'document_language'"
    if representation_profile:
        allowed_fields += " - 'representation_profile'"
    profile_clause = ""
    if representation_profile:
        profile_clause = (
            " AND (revision_metadata->'representation_profile' IS NULL OR "
            "pg_catalog.jsonb_typeof(revision_metadata->'representation_profile') IN ('string', 'null'))"
            " AND (revision_metadata->>'representation_profile' IS NULL OR "
            "(revision_metadata->>'representation_profile') OPERATOR(pg_catalog.~) "
            "'^[a-z][a-z0-9_.:-]{0,63}$')"
        )
    return (
        "revision_metadata IS NULL OR (pg_catalog.jsonb_typeof(revision_metadata) = 'object' "
        f"AND (revision_metadata - {allowed_fields}) = '{{}}'::pg_catalog.jsonb "
        "AND (revision_metadata->'embedded_title' IS NULL OR "
        "pg_catalog.jsonb_typeof(revision_metadata->'embedded_title') IN ('string', 'null')) "
        "AND (revision_metadata->'document_language' IS NULL OR "
        "pg_catalog.jsonb_typeof(revision_metadata->'document_language') IN ('string', 'null'))"
        f"{profile_clause})"
    )


def _install_revision_trigger(runtime: str, *, representation_profile: bool) -> None:
    if representation_profile:
        metadata_digest = (
            "IF representation_profile IS NULL THEN\n"
            "                    metadata_digest := encode(pg_catalog.sha256(convert_to(format("
            "'{\"document_language\":%s,\"embedded_title\":%s}', "
            "coalesce(to_json(language_tag)::text, 'null'), coalesce(to_json(title)::text, 'null')), 'UTF8')), 'hex');\n"
            "                ELSE\n"
            "                    metadata_digest := encode(pg_catalog.sha256(convert_to(format("
            "'{\"document_language\":%s,\"embedded_title\":%s,\"representation_profile\":%s}', "
            "coalesce(to_json(language_tag)::text, 'null'), coalesce(to_json(title)::text, 'null'), "
            "coalesce(to_json(representation_profile)::text, 'null')), 'UTF8')), 'hex');\n"
            "                END IF;"
        )
        canonical = (
            "IF representation_profile IS NULL THEN\n"
            "                    canonical := format('{\"bcp47_table\":%s,\"content\":{\"byte_length\":%s,"
            "\"media_type\":%s,\"metadata\":{\"document_language\":%s,\"embedded_title\":%s},"
            "\"metadata_digest\":%s,\"metadata_schema\":%s,\"original_sha256\":%s,"
            "\"reappearance_after_tombstone_revision_id\":%s},\"kind\":%s,"
            "\"profile\":%s,\"schema\":%s,\"unicode_table\":%s}', "
            "to_json(NEW.bcp47_table_digest)::text, to_json(NEW.byte_length::text)::text, "
            "to_json(NEW.media_type)::text, coalesce(to_json(language_tag)::text, 'null'), "
            "coalesce(to_json(title)::text, 'null'), to_json(NEW.revision_metadata_digest)::text, "
            "to_json(NEW.revision_metadata_schema)::text, to_json(NEW.original_sha256)::text, "
            "coalesce(to_json(NEW.reappearance_after_tombstone_revision_id::text)::text, 'null'), "
            "to_json(NEW.kind)::text, to_json(NEW.canonicalization_profile)::text, "
            "to_json(NEW.revision_schema_version)::text, to_json(NEW.unicode_table_digest)::text);\n"
            "                ELSE\n"
            "                    canonical := format('{\"bcp47_table\":%s,\"content\":{\"byte_length\":%s,"
            "\"media_type\":%s,\"metadata\":{\"document_language\":%s,"
            "\"embedded_title\":%s,\"representation_profile\":%s},"
            "\"metadata_digest\":%s,\"metadata_schema\":%s,\"original_sha256\":%s,"
            "\"reappearance_after_tombstone_revision_id\":%s},\"kind\":%s,"
            "\"profile\":%s,\"schema\":%s,\"unicode_table\":%s}', "
            "to_json(NEW.bcp47_table_digest)::text, to_json(NEW.byte_length::text)::text, "
            "to_json(NEW.media_type)::text, coalesce(to_json(language_tag)::text, 'null'), "
            "coalesce(to_json(title)::text, 'null'), coalesce(to_json(representation_profile)::text, 'null'), "
            "to_json(NEW.revision_metadata_digest)::text, to_json(NEW.revision_metadata_schema)::text, "
            "to_json(NEW.original_sha256)::text, coalesce(to_json(NEW.reappearance_after_tombstone_revision_id::text)::text, 'null'), "
            "to_json(NEW.kind)::text, to_json(NEW.canonicalization_profile)::text, "
            "to_json(NEW.revision_schema_version)::text, to_json(NEW.unicode_table_digest)::text);\n"
            "                END IF;"
        )
        profile_declaration = "            representation_profile text;\n"
        profile_assignment = "                representation_profile := NEW.revision_metadata->>'representation_profile';\n"
        profile_validation = (
            "                IF representation_profile IS NOT NULL AND representation_profile !~ '^[a-z][a-z0-9_.:-]{0,63}$' THEN\n"
            "                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';\n"
            "                END IF;\n"
        )
    else:
        metadata_digest = (
            "metadata_digest := encode(pg_catalog.sha256(convert_to(format("
            "'{\"document_language\":%s,\"embedded_title\":%s}', "
            "coalesce(to_json(language_tag)::text, 'null'), coalesce(to_json(title)::text, 'null')), 'UTF8')), 'hex');"
        )
        canonical = (
            "canonical := format('{\"bcp47_table\":%s,\"content\":{\"byte_length\":%s,"
            "\"media_type\":%s,\"metadata\":{\"document_language\":%s,\"embedded_title\":%s},"
            "\"metadata_digest\":%s,\"metadata_schema\":%s,\"original_sha256\":%s,"
            "\"reappearance_after_tombstone_revision_id\":%s},\"kind\":%s,"
            "\"profile\":%s,\"schema\":%s,\"unicode_table\":%s}', "
            "to_json(NEW.bcp47_table_digest)::text, to_json(NEW.byte_length::text)::text, "
            "to_json(NEW.media_type)::text, coalesce(to_json(language_tag)::text, 'null'), "
            "coalesce(to_json(title)::text, 'null'), to_json(NEW.revision_metadata_digest)::text, "
            "to_json(NEW.revision_metadata_schema)::text, to_json(NEW.original_sha256)::text, "
            "coalesce(to_json(NEW.reappearance_after_tombstone_revision_id::text)::text, 'null'), "
            "to_json(NEW.kind)::text, to_json(NEW.canonicalization_profile)::text, "
            "to_json(NEW.revision_schema_version)::text, to_json(NEW.unicode_table_digest)::text);"
        )
        profile_declaration = ""
        profile_assignment = ""
        profile_validation = ""

    op.execute(f"DROP TRIGGER IF EXISTS trg_source_revision_canonical_validation ON {SCHEMA}.source_revisions")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.validate_source_revision_canonical() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            canonical text;
            metadata_digest text;
            title text;
            raw_title text;
            language_tag text;
            unicode_digest text;
            bcp47_digest text;
{profile_declaration}        BEGIN
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
{profile_assignment}{profile_validation}                IF raw_title IS NOT NULL AND raw_title <> title THEN
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
                {metadata_digest}
                IF NEW.revision_metadata_digest <> metadata_digest THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                {canonical}
            ELSE
                canonical := format('{{\"bcp47_table\":%s,\"kind\":%s,\"profile\":%s,\"schema\":%s,\"tombstone\":{{\"deletion_fact\":\"deletion-fact:r1-v1\",\"reason\":%s}},\"unicode_table\":%s}}', to_json(NEW.bcp47_table_digest)::text, to_json(NEW.kind)::text, to_json(NEW.canonicalization_profile)::text, to_json(NEW.revision_schema_version)::text, to_json(NEW.deletion_reason)::text, to_json(NEW.unicode_table_digest)::text);
            END IF;
            IF encode(pg_catalog.sha256(convert_to(canonical, 'UTF8')), 'hex') <> NEW.revision_digest THEN
                RAISE EXCEPTION 'Invalid source revision digest.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(
        f"CREATE CONSTRAINT TRIGGER trg_source_revision_canonical_validation AFTER INSERT OR UPDATE ON {SCHEMA}.source_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_source_revision_canonical()"
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_source_revision_canonical() FROM PUBLIC")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {SCHEMA}.source_revisions FROM {runtime}")


def upgrade() -> None:
    runtime = _role("runtime_role")
    op.drop_constraint("ck_source_revisions_metadata_shape", table_name="source_revisions", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_source_revisions_metadata_shape",
        "source_revisions",
        _metadata_shape_constraint(representation_profile=True),
        schema=SCHEMA,
    )
    _install_revision_trigger(runtime, representation_profile=True)


def downgrade() -> None:
    runtime = _role("runtime_role")
    op.drop_constraint("ck_source_revisions_metadata_shape", table_name="source_revisions", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_source_revisions_metadata_shape",
        "source_revisions",
        _metadata_shape_constraint(representation_profile=False),
        schema=SCHEMA,
    )
    _install_revision_trigger(runtime, representation_profile=False)
