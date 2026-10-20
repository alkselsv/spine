"""Validate ingestion bindings against canonical source receipt associations."""

from alembic import op


revision = "20261111_48"
down_revision = "20261110_47"
branch_labels = None
depends_on = None

SCHEMA = "spine"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.validate_issue86_binding_original_receipt()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE revision_kind text;
        DECLARE association_exists boolean;
        BEGIN
            SELECT revision.kind
              INTO revision_kind
              FROM {SCHEMA}.source_revisions AS revision
             WHERE revision.revision_id = NEW.source_revision_id
               AND revision.source_object_id = NEW.source_object_id
               AND revision.workspace_id = NEW.workspace_id
               AND revision.environment_id = NEW.environment_id
             ;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'Binding revision is unavailable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_binding_original_receipt_scope';
            END IF;

            IF revision_kind = 'content' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM {SCHEMA}.source_revision_receipt_associations AS association
                     WHERE association.workspace_id = NEW.workspace_id
                       AND association.environment_id = NEW.environment_id
                       AND association.source_object_id = NEW.source_object_id
                       AND association.revision_id = NEW.source_revision_id
                       AND association.original_receipt_id = NEW.original_receipt_id
                       AND association.original_receipt_kind = NEW.original_receipt_kind
                       AND association.original_sha256 = NEW.original_receipt_digest
                       AND association.original_reference = NEW.original_reference
                       AND association.verification_status = 'verified'
                       AND association.storage_status = 'finalized_verified'
                ) INTO association_exists;
                IF NOT association_exists THEN
                    RAISE EXCEPTION 'Verified source receipt association is unavailable.'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_ingestion_binding_original_receipt_scope';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.original_receipt_id IS NOT NULL
               OR NEW.original_receipt_kind IS NOT NULL
               OR NEW.original_receipt_digest IS NOT NULL
               OR NEW.original_reference IS NOT NULL
            THEN
                RAISE EXCEPTION 'Tombstone binding cannot contain original evidence.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_binding_original_receipt_scope';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )


def downgrade() -> None:
    """Restore the previous content-binding rejection policy."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.validate_issue86_binding_original_receipt()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY INVOKER
        SET search_path = pg_catalog, {SCHEMA}
        AS $function$
        DECLARE revision_kind text;
        BEGIN
            SELECT revision.kind
              INTO revision_kind
              FROM {SCHEMA}.source_revisions AS revision
             WHERE revision.revision_id = NEW.source_revision_id
               AND revision.workspace_id = NEW.workspace_id
               AND revision.environment_id = NEW.environment_id;
            IF NOT FOUND THEN
                RETURN NEW;
            END IF;
            IF revision_kind = 'content' THEN
                RAISE EXCEPTION 'Original receipt verification unavailable.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_binding_original_receipt_scope';
            END IF;
            IF NEW.original_receipt_id IS NOT NULL THEN
                RAISE EXCEPTION 'Contentless ingestion binding cannot include an original receipt.'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_ingestion_binding_original_receipt_scope';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
