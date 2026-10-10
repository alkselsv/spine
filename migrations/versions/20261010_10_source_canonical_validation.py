"""Validate canonical source metadata and revision digests at the SQL seam."""

from __future__ import annotations

from alembic import op

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
        BEGIN
            IF NEW.canonicalization_profile <> 'r1-c14n-2026-10'
               OR NEW.unicode_table_digest <> '12f429d27cedef784dcda284ec37555ac092a05f4665b9fcd335ec36d05ebb8d'
               OR NEW.bcp47_table_digest <> 'd03ad7c70a60b0d9dcbf80d805ae1308e690f378c93206e3a9af303261a531a6' THEN
                RAISE EXCEPTION 'Invalid source canonicalization profile.' USING ERRCODE = '23514';
            END IF;

            IF NEW.kind = 'content' THEN
                title := NEW.revision_metadata->>'embedded_title';
                language_tag := NEW.revision_metadata->>'document_language';
                IF title IS NOT NULL AND btrim(title, E'\\t\\n\\v\\f\\r \\u0085\\u00a0\\u1680\\u2000\\u2001\\u2002\\u2003\\u2004\\u2005\\u2006\\u2007\\u2008\\u2009\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000') = '' THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                IF language_tag IS NOT NULL AND (language_tag <> lower(language_tag) OR language_tag !~ '^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$') THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
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
