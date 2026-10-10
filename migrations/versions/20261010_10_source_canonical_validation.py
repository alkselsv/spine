"""Install the database adapter for the pinned source canonicalization profile."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.domain.sources.profile import BCP47_TABLE_DIGEST, UNICODE_TABLE_DIGEST
from spine.domain.sources.profile_data import BCP47_DATA, UNICODE_DATA
from spine.domain.sources.profile_extensions import EXTENSION_DATA, EXTENSION_TABLE_DIGEST
from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261010_10"
down_revision = "20261010_09"
branch_labels = None
depends_on = None
SCHEMA = "spine"

_SBASE, _LBASE, _VBASE, _TBASE = 0xAC00, 0x1100, 0x1161, 0x11A7
_LCOUNT, _VCOUNT, _TCOUNT = 19, 21, 28
_NCOUNT, _SCOUNT = _VCOUNT * _TCOUNT, _LCOUNT * _VCOUNT * _TCOUNT


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _table(name: str, *columns: sa.Column[object]) -> sa.TableClause:
    return sa.table(name, *columns, schema=SCHEMA)


def _install_profile_tables(runtime: str, migration: str) -> None:
    op.create_table(
        "source_canonical_tables",
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("table_version", sa.Text(), nullable=False),
        sa.Column("table_digest", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("table_name", name="pk_source_canonical_tables"),
        sa.CheckConstraint("table_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'", name="ck_source_canonical_tables_digest"),
        schema=SCHEMA,
    )
    op.create_table(
        "source_canonical_unicode",
        sa.Column("codepoint", sa.Integer(), nullable=False),
        sa.Column("decomposition", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("combining_class", sa.Integer(), nullable=False),
        sa.Column("is_whitespace", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("codepoint", name="pk_source_canonical_unicode"),
        sa.CheckConstraint("codepoint BETWEEN 0 AND 1114111", name="ck_source_canonical_unicode_codepoint"),
        sa.CheckConstraint("combining_class BETWEEN 0 AND 255", name="ck_source_canonical_unicode_ccc"),
        schema=SCHEMA,
    )
    op.create_table(
        "source_canonical_compositions",
        sa.Column("left_codepoint", sa.Integer(), nullable=False),
        sa.Column("right_codepoint", sa.Integer(), nullable=False),
        sa.Column("composed_codepoint", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("left_codepoint", "right_codepoint", name="pk_source_canonical_compositions"),
        schema=SCHEMA,
    )
    op.create_table(
        "source_canonical_bcp47",
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("subtag", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("category", "subtag", "prefix", name="pk_source_canonical_bcp47"),
        sa.CheckConstraint("category IN ('language', 'extlang', 'script', 'region', 'variant', 'special', 'extension', 'extension_key', 'extension_value')", name="ck_source_canonical_bcp47_category"),
        schema=SCHEMA,
    )

    tables = _table(
        "source_canonical_tables",
        sa.column("table_name", sa.Text()),
        sa.column("table_version", sa.Text()),
        sa.column("table_digest", sa.Text()),
    )
    op.bulk_insert(
        tables,
        [
            {"table_name": "unicode", "table_version": "15.1.0", "table_digest": UNICODE_TABLE_DIGEST},
            {"table_name": "bcp47", "table_version": "2025-10-14+2026-09-17", "table_digest": BCP47_TABLE_DIGEST},
            {"table_name": "bcp47_extensions", "table_version": EXTENSION_DATA["registry_version"], "table_digest": EXTENSION_TABLE_DIGEST},
        ],
    )

    unicode_table = _table(
        "source_canonical_unicode",
        sa.column("codepoint", sa.Integer()),
        sa.column("decomposition", postgresql.ARRAY(sa.Integer())),
        sa.column("combining_class", sa.Integer()),
        sa.column("is_whitespace", sa.Boolean()),
    )
    decomposition = {int(key): value for key, value in UNICODE_DATA["decomp"].items()}
    combining = {int(key): value for key, value in UNICODE_DATA["ccc"].items()}
    whitespace = set(UNICODE_DATA["whitespace"])
    codepoints = sorted(set(decomposition) | set(combining) | whitespace)
    op.bulk_insert(
        unicode_table,
        [
            {
                "codepoint": codepoint,
                "decomposition": decomposition.get(codepoint),
                "combining_class": combining.get(codepoint, 0),
                "is_whitespace": codepoint in whitespace,
            }
            for codepoint in codepoints
        ],
    )

    compositions = _table(
        "source_canonical_compositions",
        sa.column("left_codepoint", sa.Integer()),
        sa.column("right_codepoint", sa.Integer()),
        sa.column("composed_codepoint", sa.Integer()),
    )
    op.bulk_insert(
        compositions,
        [
            {
                "left_codepoint": int(key.split(",")[0]),
                "right_codepoint": int(key.split(",")[1]),
                "composed_codepoint": value,
            }
            for key, value in UNICODE_DATA["compose"].items()
        ],
    )

    bcp47 = _table(
        "source_canonical_bcp47",
        sa.column("category", sa.Text()),
        sa.column("subtag", sa.Text()),
        sa.column("prefix", sa.Text()),
    )
    rows = []
    for record in BCP47_DATA["records"]:
        record_type = record.get("Type", [None])[0]
        if record_type in {"grandfathered", "redundant"}:
            category = "special"
            values = record.get("Tag", [])
        elif record_type in {"language", "extlang", "script", "region", "variant"}:
            category = record_type
            values = record.get("Subtag", [])
        else:
            continue
        for value in values:
            prefixes = [prefix.lower() for prefix in record.get("Prefix", [])] or ["*"]
            rows.extend(
                {"category": category, "subtag": value.lower(), "prefix": prefix}
                for prefix in prefixes
            )
    rows.extend(
        {"category": "extension", "subtag": value, "prefix": "*"}
        for value in EXTENSION_DATA["extensions"]
    )
    for key, values in EXTENSION_DATA["unicode"].items():
        rows.append({"category": "extension_key", "subtag": key, "prefix": "u"})
        rows.extend(
            {"category": "extension_value", "subtag": f"u:{key}:{value}", "prefix": "u"}
            for value in values
        )
    for key, values in EXTENSION_DATA["transformed"].items():
        rows.append({"category": "extension_key", "subtag": key, "prefix": "t"})
        rows.extend(
            {"category": "extension_value", "subtag": f"t:{key}:{value}", "prefix": "t"}
            for value in values
        )
    op.bulk_insert(bcp47, rows)

    for table in ("source_canonical_tables", "source_canonical_unicode", "source_canonical_compositions", "source_canonical_bcp47"):
        qualified = f'{SCHEMA}."{table}"'
        op.execute(f"GRANT SELECT ON TABLE {qualified} TO {runtime}")
        op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def _install_canonical_functions(runtime: str) -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.source_canonical_decompose(input_codepoint integer)
        RETURNS integer[] LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            offset_value integer;
            decomposition integer[];
            part integer;
            output integer[] := ARRAY[]::integer[];
        BEGIN
            offset_value := input_codepoint - {_SBASE};
            IF offset_value BETWEEN 0 AND {_SCOUNT - 1} THEN
                output := ARRAY[{_LBASE} + offset_value / {_NCOUNT}, {_VBASE} + (offset_value % {_NCOUNT}) / {_TCOUNT}];
                IF offset_value % {_TCOUNT} <> 0 THEN
                    output := output || ARRAY[{_TBASE} + offset_value % {_TCOUNT}];
                END IF;
                RETURN output;
            END IF;
            SELECT unicode_row.decomposition INTO decomposition
            FROM {SCHEMA}.source_canonical_unicode AS unicode_row
            WHERE unicode_row.codepoint = input_codepoint;
            IF decomposition IS NULL THEN
                RETURN ARRAY[input_codepoint];
            END IF;
            FOREACH part IN ARRAY decomposition LOOP
                output := output || {SCHEMA}.source_canonical_decompose(part);
            END LOOP;
            RETURN output;
        END; $function$;

        CREATE FUNCTION {SCHEMA}.source_canonical_combining_class(input_codepoint integer)
        RETURNS integer LANGUAGE sql IMMUTABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
            SELECT COALESCE((SELECT combining_class FROM {SCHEMA}.source_canonical_unicode WHERE codepoint = input_codepoint), 0);
        $function$;

        CREATE FUNCTION {SCHEMA}.source_canonical_compose(input_left integer, input_right integer)
        RETURNS integer LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            left_offset integer;
            result integer;
        BEGIN
            IF input_left BETWEEN {_LBASE} AND {_LBASE + _LCOUNT - 1}
               AND input_right BETWEEN {_VBASE} AND {_VBASE + _VCOUNT - 1} THEN
                RETURN {_SBASE} + ((input_left - {_LBASE}) * {_VCOUNT} + input_right - {_VBASE}) * {_TCOUNT};
            END IF;
            left_offset := input_left - {_SBASE};
            IF left_offset BETWEEN 0 AND {_SCOUNT - 1} AND left_offset % {_TCOUNT} = 0
               AND input_right BETWEEN {_TBASE + 1} AND {_TBASE + _TCOUNT - 1} THEN
                RETURN input_left + input_right - {_TBASE};
            END IF;
            SELECT composition.composed_codepoint INTO result
            FROM {SCHEMA}.source_canonical_compositions AS composition
            WHERE composition.left_codepoint = input_left
              AND composition.right_codepoint = input_right;
            RETURN result;
        END; $function$;

        CREATE FUNCTION {SCHEMA}.source_canonical_nfc(input_text text)
        RETURNS text LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            normalized text := replace(replace(input_text, E'\\r\\n', E'\\n'), E'\\r', E'\\n');
            decomposed integer[] := ARRAY[]::integer[];
            ordered integer[] := ARRAY[]::integer[];
            composed integer[] := ARRAY[]::integer[];
            codepoint integer;
            current_class integer;
            position integer;
            starter integer := 1;
            last_class integer := 0;
            candidate integer;
            output text;
        BEGIN
            FOR position IN 1..char_length(normalized) LOOP
                decomposed := decomposed || {SCHEMA}.source_canonical_decompose(pg_catalog.ascii(substr(normalized, position, 1)));
            END LOOP;
            FOREACH codepoint IN ARRAY decomposed LOOP
                current_class := {SCHEMA}.source_canonical_combining_class(codepoint);
                IF current_class = 0 THEN
                    ordered := array_append(ordered, codepoint);
                ELSE
                    position := cardinality(ordered) + 1;
                    WHILE position > 1 AND {SCHEMA}.source_canonical_combining_class(ordered[position - 1]) > current_class LOOP
                        position := position - 1;
                    END LOOP;
                    IF position = cardinality(ordered) + 1 THEN
                        ordered := array_append(ordered, codepoint);
                    ELSIF position = 1 THEN
                        ordered := ARRAY[codepoint] || ordered;
                    ELSE
                        ordered := ordered[1:position - 1] || ARRAY[codepoint] || ordered[position:cardinality(ordered)];
                    END IF;
                END IF;
            END LOOP;
            FOREACH codepoint IN ARRAY ordered LOOP
                current_class := {SCHEMA}.source_canonical_combining_class(codepoint);
                candidate := NULL;
                IF cardinality(composed) > 0 AND (current_class = 0 OR last_class < current_class) THEN
                    candidate := {SCHEMA}.source_canonical_compose(composed[starter], codepoint);
                END IF;
                IF candidate IS NOT NULL THEN
                    composed[starter] := candidate;
                ELSE
                    IF current_class = 0 THEN
                        starter := cardinality(composed) + 1;
                    END IF;
                    composed := array_append(composed, codepoint);
                    last_class := current_class;
                END IF;
            END LOOP;
            SELECT string_agg(chr(value), '') INTO output FROM unnest(composed) AS values(value);
            RETURN COALESCE(output, '');
        END; $function$;

        CREATE FUNCTION {SCHEMA}.source_canonical_trim(input_text text)
        RETURNS text LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            normalized text := {SCHEMA}.source_canonical_nfc(input_text);
            first_position integer := 1;
            last_position integer := char_length(normalized);
            current_codepoint integer;
        BEGIN
            WHILE first_position <= last_position LOOP
                current_codepoint := pg_catalog.ascii(substr(normalized, first_position, 1));
                EXIT WHEN NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_unicode WHERE codepoint = current_codepoint AND is_whitespace);
                first_position := first_position + 1;
            END LOOP;
            WHILE last_position >= first_position LOOP
                current_codepoint := pg_catalog.ascii(substr(normalized, last_position, 1));
                EXIT WHEN NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_unicode WHERE codepoint = current_codepoint AND is_whitespace);
                last_position := last_position - 1;
            END LOOP;
            RETURN substring(normalized FROM first_position FOR last_position - first_position + 1);
        END; $function$;

        CREATE FUNCTION {SCHEMA}.source_canonical_language(input_tag text)
        RETURNS text LANGUAGE plpgsql IMMUTABLE SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE
            parts text[] := string_to_array(lower(input_tag), '-');
            index_value integer := 2;
            token text;
            seen text[] := ARRAY[]::text[];
            extension_singleton text;
            current_extension_key text;
            previous_extension_key text;
            extension_value_count integer;
            extlang_count integer := 0;
        BEGIN
            IF input_tag !~ '^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*$' THEN
                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'special' AND subtag = lower(input_tag)) THEN
                RETURN lower(input_tag);
            END IF;
            IF parts[1] = 'x' THEN
                IF cardinality(parts) < 2 THEN RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514'; END IF;
                WHILE index_value <= cardinality(parts) LOOP
                    token := parts[index_value];
                    IF token !~ '^[a-z0-9]{{1,8}}$' THEN RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514'; END IF;
                    index_value := index_value + 1;
                END LOOP;
                RETURN lower(input_tag);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'language' AND subtag = parts[1]) THEN
                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
            END IF;
            WHILE index_value <= cardinality(parts) AND parts[index_value] ~ '^[a-z]{{3}}$' LOOP
                extlang_count := extlang_count + 1;
                IF extlang_count > 3 THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 AS registry WHERE registry.category = 'extlang' AND registry.subtag = parts[index_value] AND (registry.prefix = '*' OR registry.prefix = array_to_string(parts[1:index_value - 1], '-'))) THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                index_value := index_value + 1;
            END LOOP;
            IF index_value <= cardinality(parts) AND parts[index_value] ~ '^[a-z]{{4}}$' THEN
                IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'script' AND subtag = parts[index_value]) THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                index_value := index_value + 1;
            END IF;
            IF index_value <= cardinality(parts) AND parts[index_value] ~ '^(?:[a-z]{{2}}|[0-9]{{3}})$' THEN
                IF parts[index_value] = 'zz' OR NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'region' AND subtag = parts[index_value]) THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                index_value := index_value + 1;
            END IF;
            WHILE index_value <= cardinality(parts) AND parts[index_value] ~ '^(?:[0-9][a-z0-9]{{3}}|[a-z0-9]{{5,8}})$' LOOP
                IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 AS registry WHERE registry.category = 'variant' AND registry.subtag = parts[index_value] AND (registry.prefix = '*' OR registry.prefix = array_to_string(parts[1:index_value - 1], '-'))) OR parts[index_value] = ANY(seen) THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                seen := array_append(seen, parts[index_value]);
                index_value := index_value + 1;
            END LOOP;
            WHILE index_value <= cardinality(parts) LOOP
                extension_singleton := parts[index_value];
                IF extension_singleton = 'x' THEN
                    index_value := index_value + 1;
                    IF index_value > cardinality(parts) THEN RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514'; END IF;
                    WHILE index_value <= cardinality(parts) LOOP
                        IF parts[index_value] !~ '^[a-z0-9]{{1,8}}$' THEN RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514'; END IF;
                        index_value := index_value + 1;
                    END LOOP;
                ELSE
                    IF extension_singleton !~ '^[0-9a-wy-z]$' OR extension_singleton = ANY(seen) OR NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'extension' AND subtag = extension_singleton) THEN RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514'; END IF;
                    seen := array_append(seen, extension_singleton);
                    index_value := index_value + 1;
                    IF index_value > cardinality(parts) OR parts[index_value] !~ '^[a-z0-9]{{2,8}}$' THEN RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514'; END IF;
                    current_extension_key := NULL;
                    previous_extension_key := NULL;
                    extension_value_count := 0;
                    IF extension_singleton = 'u' THEN
                        WHILE index_value <= cardinality(parts) AND parts[index_value] <> ALL(seen) AND parts[index_value] <> 'x' AND parts[index_value] !~ '^[0-9a-wy-z]$' LOOP
                            IF parts[index_value] ~ '^[a-z0-9]{{2}}$' THEN
                                IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'extension_key' AND subtag = parts[index_value] AND prefix = 'u') OR (previous_extension_key IS NOT NULL AND parts[index_value] <= previous_extension_key) THEN
                                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                                END IF;
                                current_extension_key := parts[index_value];
                                previous_extension_key := current_extension_key;
                                extension_value_count := 0;
                            ELSIF current_extension_key IS NULL OR NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'extension_value' AND subtag = 'u:' || current_extension_key || ':' || parts[index_value] AND prefix = 'u') THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            ELSE
                                extension_value_count := extension_value_count + 1;
                            END IF;
                            index_value := index_value + 1;
                        END LOOP;
                        IF current_extension_key IS NULL OR extension_value_count = 0 THEN RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514'; END IF;
                    ELSIF extension_singleton = 't' THEN
                        IF index_value <= cardinality(parts) AND EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'language' AND subtag = parts[index_value]) THEN
                            index_value := index_value + 1;
                            IF index_value <= cardinality(parts) AND EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'script' AND subtag = parts[index_value]) THEN index_value := index_value + 1; END IF;
                            IF index_value <= cardinality(parts) AND EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'region' AND subtag = parts[index_value]) THEN index_value := index_value + 1; END IF;
                        END IF;
                        WHILE index_value <= cardinality(parts) AND parts[index_value] <> ALL(seen) AND parts[index_value] <> 'x' AND parts[index_value] !~ '^[0-9a-wy-z]$' LOOP
                            IF NOT (parts[index_value] ~ '^[a-z0-9]{{2}}$' AND parts[index_value] <> ALL(seen) AND EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'extension_key' AND subtag = parts[index_value] AND prefix = 't')) THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            END IF;
                            current_extension_key := parts[index_value];
                            seen := array_append(seen, current_extension_key);
                            index_value := index_value + 1;
                            IF index_value > cardinality(parts) OR NOT EXISTS (SELECT 1 FROM {SCHEMA}.source_canonical_bcp47 WHERE category = 'extension_value' AND subtag = 't:' || current_extension_key || ':' || parts[index_value] AND prefix = 't') THEN
                                RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                            END IF;
                            index_value := index_value + 1;
                        END LOOP;
                    ELSE
                        RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                    END IF;
                END IF;
            END LOOP;
            RETURN lower(input_tag);
        END; $function$;
        """
    )
    for name, signature in (
        ("source_canonical_decompose", "integer"),
        ("source_canonical_combining_class", "integer"),
        ("source_canonical_compose", "integer, integer"),
        ("source_canonical_nfc", "text"),
        ("source_canonical_trim", "text"),
        ("source_canonical_language", "text"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.{name}({signature}) FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {SCHEMA}.{name}({signature}) TO {runtime}")


def _install_revision_trigger(runtime: str) -> None:
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.validate_source_revision_canonical() RETURNS trigger
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
                metadata_digest := encode(pg_catalog.sha256(convert_to(format('{{"document_language":%s,"embedded_title":%s}}', coalesce(to_json(language_tag)::text, 'null'), coalesce(to_json(title)::text, 'null')), 'UTF8')), 'hex');
                IF NEW.revision_metadata_digest <> metadata_digest THEN
                    RAISE EXCEPTION 'Invalid source revision metadata.' USING ERRCODE = '23514';
                END IF;
                canonical := format('{{"bcp47_table":%s,"content":{{"byte_length":%s,"media_type":%s,"metadata":{{"document_language":%s,"embedded_title":%s}},"metadata_digest":%s,"metadata_schema":%s,"original_sha256":%s,"reappearance_after_tombstone_revision_id":%s}},"kind":%s,"profile":%s,"schema":%s,"unicode_table":%s}}', to_json(NEW.bcp47_table_digest)::text, to_json(NEW.byte_length::text)::text, to_json(NEW.media_type)::text, coalesce(to_json(language_tag)::text, 'null'), coalesce(to_json(title)::text, 'null'), to_json(NEW.revision_metadata_digest)::text, to_json(NEW.revision_metadata_schema)::text, to_json(NEW.original_sha256)::text, coalesce(to_json(NEW.reappearance_after_tombstone_revision_id::text)::text, 'null'), to_json(NEW.kind)::text, to_json(NEW.canonicalization_profile)::text, to_json(NEW.revision_schema_version)::text, to_json(NEW.unicode_table_digest)::text);
            ELSE
                canonical := format('{{"bcp47_table":%s,"kind":%s,"profile":%s,"schema":%s,"tombstone":{{"deletion_fact":"deletion-fact:r1-v1","reason":%s}},"unicode_table":%s}}', to_json(NEW.bcp47_table_digest)::text, to_json(NEW.kind)::text, to_json(NEW.canonicalization_profile)::text, to_json(NEW.revision_schema_version)::text, to_json(NEW.deletion_reason)::text, to_json(NEW.unicode_table_digest)::text);
            END IF;
            IF encode(pg_catalog.sha256(convert_to(canonical, 'UTF8')), 'hex') <> NEW.revision_digest THEN
                RAISE EXCEPTION 'Invalid source revision digest.' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $function$
        """
    )
    op.execute(f"CREATE CONSTRAINT TRIGGER trg_source_revision_canonical_validation AFTER INSERT OR UPDATE ON {SCHEMA}.source_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_source_revision_canonical()")
    op.execute(f"REVOKE ALL ON FUNCTION {SCHEMA}.validate_source_revision_canonical() FROM PUBLIC")
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {SCHEMA}.source_revisions FROM {runtime}")


def upgrade() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    _install_profile_tables(runtime, migration)
    _install_canonical_functions(runtime)
    _install_revision_trigger(runtime)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_source_revision_canonical_validation ON {SCHEMA}.source_revisions")
    for function in ("validate_source_revision_canonical", "source_canonical_language", "source_canonical_trim", "source_canonical_nfc", "source_canonical_compose", "source_canonical_combining_class", "source_canonical_decompose"):
        op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{function}")
    for table in ("source_canonical_bcp47", "source_canonical_compositions", "source_canonical_unicode", "source_canonical_tables"):
        op.drop_table(table, schema=SCHEMA)
