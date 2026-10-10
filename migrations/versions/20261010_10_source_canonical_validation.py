"""Validate canonical source metadata and revision digests at the SQL seam."""

from __future__ import annotations

from alembic import op

from spine.domain.sources.profile import (
    BCP47_TABLE_DIGEST,
    EXTLANG_TAGS,
    GRANDFATHERED_TAGS,
    PRIMARY_LANGUAGE_TAGS,
    REGION_TAGS,
    SCRIPT_TAGS,
    UNICODE_TABLE_DIGEST,
    VARIANT_TAGS,
)
from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_10"
down_revision = "20261010_09"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _sql_text_array(values: frozenset[str]) -> str:
    """Render the pinned profile table as migration-owned SQL data."""

    escaped = ", ".join("'" + value.replace("'", "''") + "'" for value in sorted(values))
    return f"ARRAY[{escaped}]::pg_catalog.text[]"


def upgrade() -> None:
    runtime = _role("runtime_role")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.validate_source_revision_canonical() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}, public
        AS $function$
        DECLARE
            canonical text;
            metadata_digest text;
            title text;
            language_tag text;
            parts text[];
            token text;
            seen text[] := ARRAY[]::pg_catalog.text[];
            i integer;
            start_index integer;
        BEGIN
            IF NEW.canonicalization_profile <> 'r1-c14n-2026-10'
               OR NEW.unicode_table_digest <> '{UNICODE_TABLE_DIGEST}'
               OR NEW.bcp47_table_digest <> '{BCP47_TABLE_DIGEST}' THEN
                RAISE EXCEPTION 'Invalid source canonicalization profile.' USING ERRCODE = '23514';
            END IF;

            IF NEW.kind = 'content' THEN
                title := NEW.revision_metadata->>'embedded_title';
                language_tag := NEW.revision_metadata->>'document_language';
                IF title IS NOT NULL AND title <> pg_catalog.normalize(title, 'NFC') THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                IF title IS NOT NULL AND btrim(title, E'\\t\\n\\v\\f\\r \\u0085\\u00a0\\u1680\\u2000\\u2001\\u2002\\u2003\\u2004\\u2005\\u2006\\u2007\\u2008\\u2009\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000') = '' THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                IF language_tag IS NOT NULL THEN
                    IF language_tag <> lower(language_tag)
                       OR language_tag !~ '^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$' THEN
                        RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                    END IF;
                    parts := pg_catalog.string_to_array(language_tag, '-');
                    IF lower(language_tag) <> ALL({_sql_text_array(GRANDFATHERED_TAGS)}) THEN
                        IF parts[1] = ANY({_sql_text_array(PRIMARY_LANGUAGE_TAGS)}) IS FALSE AND parts[1] <> 'x' THEN
                            RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                        END IF;
                        i := 2;
                        IF parts[1] = 'x' THEN
                            IF i > pg_catalog.array_length(parts, 1) THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            END IF;
                            WHILE i <= pg_catalog.array_length(parts, 1) LOOP
                                IF parts[i] !~ '^[a-z0-9]{1,8}$' THEN
                                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                                END IF;
                                i := i + 1;
                            END LOOP;
                        END IF;
                        WHILE i <= pg_catalog.array_length(parts, 1)
                              AND parts[i] ~ '^[a-z]{{3}}$' LOOP
                            IF parts[i] <> ALL({_sql_text_array(EXTLANG_TAGS)}) THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            END IF;
                            i := i + 1;
                        END LOOP;
                        IF i <= pg_catalog.array_length(parts, 1) AND parts[i] ~ '^[a-z]{{4}}$' THEN
                            IF parts[i] <> ALL({_sql_text_array(frozenset(tag.lower() for tag in SCRIPT_TAGS))}) THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            END IF;
                            i := i + 1;
                        END IF;
                        IF i <= pg_catalog.array_length(parts, 1) AND parts[i] ~ '^(?:[a-z]{{2}}|[0-9]{{3}})$' THEN
                            IF parts[i] <> ALL({_sql_text_array(REGION_TAGS)}) OR parts[i] = 'zz' THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            END IF;
                            i := i + 1;
                        END IF;
                        WHILE i <= pg_catalog.array_length(parts, 1) AND parts[i] ~ '^(?:[0-9][a-z0-9]{{3}}|[a-z0-9]{{5,8}})$' LOOP
                            IF parts[i] <> ALL({_sql_text_array(VARIANT_TAGS)}) OR parts[i] = ANY(seen) THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            END IF;
                            seen := pg_catalog.array_append(seen, parts[i]);
                            i := i + 1;
                        END LOOP;
                        WHILE i <= pg_catalog.array_length(parts, 1) LOOP
                            IF parts[i] = 'x' THEN
                                i := i + 1;
                                IF i > pg_catalog.array_length(parts, 1) THEN
                                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                                END IF;
                                WHILE i <= pg_catalog.array_length(parts, 1) LOOP
                                    IF parts[i] !~ '^[a-z0-9]{{1,8}}$' THEN
                                        RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                                    END IF;
                                    i := i + 1;
                                END LOOP;
                            ELSE
                                IF parts[i] !~ '^[0-9a-wy-z]$' OR parts[i] = ANY(seen) THEN
                                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                                END IF;
                                seen := pg_catalog.array_append(seen, parts[i]);
                                i := i + 1;
                                start_index := i;
                                WHILE i <= pg_catalog.array_length(parts, 1) AND parts[i] ~ '^[a-z0-9]{{2,8}}$' LOOP
                                    i := i + 1;
                                END LOOP;
                                IF i = start_index THEN
                                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                                END IF;
                            END IF;
                        END LOOP;
                    END IF;
                END IF;
                metadata_digest := encode(public.digest(convert_to(
                    format('{"document_language":%s,"embedded_title":%s}',
                        coalesce(to_json(language_tag)::text, 'null'),
                        coalesce(to_json(title)::text, 'null')),
                    'UTF8'), 'sha256'), 'hex');
                IF NEW.revision_metadata_digest <> metadata_digest THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                canonical := format(
                    '{"bcp47_table":%s,"content":{"byte_length":%s,"media_type":%s,"metadata":{"document_language":%s,"embedded_title":%s},"metadata_digest":%s,"metadata_schema":%s,"original_sha256":%s,"reappearance_after_tombstone_revision_id":%s},"kind":%s,"profile":%s,"schema":%s,"unicode_table":%s}',
                    to_json(NEW.bcp47_table_digest)::text,
                    to_json(NEW.byte_length::text)::text,
                    to_json(NEW.media_type)::text,
                    coalesce(to_json(language_tag)::text, 'null'),
                    coalesce(to_json(title)::text, 'null'),
                    to_json(NEW.revision_metadata_digest)::text,
                    to_json(NEW.revision_metadata_schema)::text,
                    to_json(NEW.original_sha256)::text,
                    coalesce(to_json(NEW.reappearance_after_tombstone_revision_id::text)::text, 'null'),
                    to_json(NEW.kind)::text,
                    to_json(NEW.canonicalization_profile)::text,
                    to_json(NEW.revision_schema_version)::text,
                    to_json(NEW.unicode_table_digest)::text);
            ELSE
                canonical := format(
                    '{"bcp47_table":%s,"kind":%s,"profile":%s,"schema":%s,"tombstone":{"deletion_fact":"deletion-fact:r1-v1","reason":%s},"unicode_table":%s}',
                    to_json(NEW.bcp47_table_digest)::text,
                    to_json(NEW.kind)::text,
                    to_json(NEW.canonicalization_profile)::text,
                    to_json(NEW.revision_schema_version)::text,
                    to_json(NEW.deletion_reason)::text,
                    to_json(NEW.unicode_table_digest)::text);
            END IF;
            IF encode(public.digest(convert_to(canonical, 'UTF8'), 'sha256'), 'hex') <> NEW.revision_digest THEN
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


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_source_revision_canonical_validation ON {SCHEMA}.source_revisions")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.validate_source_revision_canonical()")
