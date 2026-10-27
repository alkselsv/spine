"""Record authorization revocation as a suspended Google sync state."""

from alembic import op


revision = "20261130_67"
down_revision = "20261129_66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION spine.enforce_google_connection_successor()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, spine
        AS $function$
        DECLARE
            predecessor spine.google_connection_versions%ROWTYPE;
            approval spine.google_connection_approvals%ROWTYPE;
            audit_scope RECORD;
        BEGIN
            IF cardinality(NEW.approved_resource_scope_references) <> (
                SELECT count(DISTINCT scope_reference)
                  FROM unnest(NEW.approved_resource_scope_references)
                       AS scope_reference
            ) THEN
                RAISE EXCEPTION 'google connection resource scopes are duplicated'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_google_connection_resource_scopes';
            END IF;
            IF NEW.version > 1 THEN
                SELECT * INTO predecessor
                  FROM spine.google_connection_versions
                 WHERE connection_version_id = NEW.predecessor_version_id;
                IF NOT FOUND
                   OR predecessor.connection_id <> NEW.connection_id
                   OR predecessor.workspace_id <> NEW.workspace_id
                   OR predecessor.environment_id <> NEW.environment_id
                   OR predecessor.version + 1 <> NEW.version
                   OR predecessor.authorization_generation > NEW.authorization_generation
                   OR predecessor.configuration_generation > NEW.configuration_generation
                   OR (
                       predecessor.authorization_generation = NEW.authorization_generation
                       AND predecessor.configuration_generation = NEW.configuration_generation
                       AND NOT (
                           predecessor.status = 'active'
                           AND NEW.status = 'suspended'
                       )
                   )
                THEN
                    RAISE EXCEPTION 'google connection successor is invalid'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_google_connection_successor';
                END IF;
            END IF;
            SELECT * INTO approval
              FROM spine.google_connection_approvals
             WHERE approval_id = NEW.approval_id;
            IF NOT FOUND
               OR approval.workspace_id <> NEW.workspace_id
               OR approval.environment_id <> NEW.environment_id
               OR approval.service_principal_id <> NEW.service_principal_id
               OR approval.credential_reference <> NEW.credential_reference
               OR approval.grant_reference <> NEW.grant_reference
               OR approval.approved_resource_scope_references
                  <> NEW.approved_resource_scope_references
               OR approval.authorization_generation <> NEW.authorization_generation
               OR approval.configuration_generation <> NEW.configuration_generation
               OR (
                   approval.status <> NEW.status
                   AND NOT (approval.status = 'active' AND NEW.status = 'suspended')
               )
            THEN
                RAISE EXCEPTION 'google connection approval does not match'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_google_connection_approval_match';
            END IF;
            SELECT workspace_id, environment_id INTO audit_scope
              FROM spine.audit_events
             WHERE audit_event_id = NEW.audit_event_id;
            IF NOT FOUND
               OR audit_scope.workspace_id <> NEW.workspace_id
               OR audit_scope.environment_id IS DISTINCT FROM NEW.environment_id
            THEN
                RAISE EXCEPTION 'google connection audit scope does not match'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_google_connection_audit_scope';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        "ALTER TABLE spine.drive_snapshot_runs "
        "ALTER COLUMN proposed_start_page_token DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE spine.drive_snapshot_runs "
        "DROP CONSTRAINT IF EXISTS ck_drive_snapshot_runs_start_token"
    )
    op.execute(
        "ALTER TABLE spine.drive_snapshot_runs "
        "DROP CONSTRAINT IF EXISTS ck_drive_snapshot_runs_ck_drive_snapshot_runs_start_token"
    )
    op.execute(
        "ALTER TABLE spine.drive_snapshot_runs ADD CONSTRAINT "
        "ck_drive_snapshot_runs_start_token CHECK "
        "(proposed_start_page_token IS NULL OR "
        "length(proposed_start_page_token) BETWEEN 1 AND 4096)"
    )
    states = "'running', 'succeeded', 'resumable', 'reconciliation_required', 'suspended', 'policy_blocked'"
    for table, constraint in (
        ("drive_snapshot_runs", "ck_drive_snapshot_runs_state"),
        ("drive_change_runs", "ck_drive_change_runs_state"),
        ("google_sheets_sync_runs", "ck_google_sheets_sync_runs_state"),
    ):
        op.execute(
            f"ALTER TABLE spine.{table} DROP CONSTRAINT IF EXISTS {constraint}"
        )
        op.execute(
            f"ALTER TABLE spine.{table} DROP CONSTRAINT IF EXISTS ck_{table}_{constraint}"
        )
        op.execute(
            f"ALTER TABLE spine.{table} ADD CONSTRAINT {constraint} "
            f"CHECK (state IN ({states}))"
        )


def downgrade() -> None:
    op.execute(
        "UPDATE spine.google_sheets_sync_runs SET state = 'resumable' WHERE state = 'suspended'"
    )
    op.execute(
        "UPDATE spine.drive_change_runs SET state = 'resumable' WHERE state = 'suspended'"
    )
    op.execute(
        "UPDATE spine.drive_snapshot_runs SET state = 'resumable' WHERE state = 'suspended'"
    )
    op.execute(
        "UPDATE spine.google_sheets_sync_runs SET state = 'resumable' WHERE state = 'policy_blocked'"
    )
    op.execute(
        "UPDATE spine.drive_change_runs SET state = 'resumable' WHERE state = 'policy_blocked'"
    )
    op.execute(
        "UPDATE spine.drive_snapshot_runs SET state = 'resumable' WHERE state = 'policy_blocked'"
    )
    op.execute(
        """
        DO $block$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM spine.drive_snapshot_runs
                 WHERE proposed_start_page_token IS NULL
            ) THEN
                RAISE EXCEPTION 'cannot downgrade with Drive snapshots awaiting a start token';
            END IF;
        END;
        $block$
        """
    )
    op.execute(
        "ALTER TABLE spine.drive_snapshot_runs "
        "DROP CONSTRAINT IF EXISTS ck_drive_snapshot_runs_start_token"
    )
    op.execute(
        "ALTER TABLE spine.drive_snapshot_runs "
        "ADD CONSTRAINT ck_drive_snapshot_runs_start_token "
        "CHECK (length(proposed_start_page_token) BETWEEN 1 AND 4096)"
    )
    op.execute(
        "ALTER TABLE spine.drive_snapshot_runs "
        "ALTER COLUMN proposed_start_page_token SET NOT NULL"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION spine.enforce_google_connection_successor()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, spine
        AS $function$
        DECLARE
            predecessor spine.google_connection_versions%ROWTYPE;
            approval spine.google_connection_approvals%ROWTYPE;
            audit_scope RECORD;
        BEGIN
            IF cardinality(NEW.approved_resource_scope_references) <> (
                SELECT count(DISTINCT scope_reference)
                  FROM unnest(NEW.approved_resource_scope_references)
                       AS scope_reference
            ) THEN
                RAISE EXCEPTION 'google connection resource scopes are duplicated'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_google_connection_resource_scopes';
            END IF;
            IF NEW.version > 1 THEN
                SELECT * INTO predecessor
                  FROM spine.google_connection_versions
                 WHERE connection_version_id = NEW.predecessor_version_id;
                IF NOT FOUND
                   OR predecessor.connection_id <> NEW.connection_id
                   OR predecessor.workspace_id <> NEW.workspace_id
                   OR predecessor.environment_id <> NEW.environment_id
                   OR predecessor.version + 1 <> NEW.version
                   OR predecessor.authorization_generation > NEW.authorization_generation
                   OR predecessor.configuration_generation > NEW.configuration_generation
                   OR (
                       predecessor.authorization_generation = NEW.authorization_generation
                       AND predecessor.configuration_generation = NEW.configuration_generation
                   )
                THEN
                    RAISE EXCEPTION 'google connection successor is invalid'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_google_connection_successor';
                END IF;
            END IF;
            SELECT * INTO approval
              FROM spine.google_connection_approvals
             WHERE approval_id = NEW.approval_id;
            IF NOT FOUND
               OR approval.workspace_id <> NEW.workspace_id
               OR approval.environment_id <> NEW.environment_id
               OR approval.service_principal_id <> NEW.service_principal_id
               OR approval.credential_reference <> NEW.credential_reference
               OR approval.grant_reference <> NEW.grant_reference
               OR approval.approved_resource_scope_references
                  <> NEW.approved_resource_scope_references
               OR approval.authorization_generation <> NEW.authorization_generation
               OR approval.configuration_generation <> NEW.configuration_generation
               OR approval.status <> NEW.status
            THEN
                RAISE EXCEPTION 'google connection approval does not match'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_google_connection_approval_match';
            END IF;
            SELECT workspace_id, environment_id INTO audit_scope
              FROM spine.audit_events
             WHERE audit_event_id = NEW.audit_event_id;
            IF NOT FOUND
               OR audit_scope.workspace_id <> NEW.workspace_id
               OR audit_scope.environment_id IS DISTINCT FROM NEW.environment_id
            THEN
                RAISE EXCEPTION 'google connection audit scope does not match'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_google_connection_audit_scope';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    for table, constraint in (
        ("google_sheets_sync_runs", "ck_google_sheets_sync_runs_state"),
        ("drive_change_runs", "ck_drive_change_runs_state"),
        ("drive_snapshot_runs", "ck_drive_snapshot_runs_state"),
    ):
        op.execute(
            f"ALTER TABLE spine.{table} DROP CONSTRAINT IF EXISTS {constraint}"
        )
        op.execute(
            f"ALTER TABLE spine.{table} DROP CONSTRAINT IF EXISTS ck_{table}_{constraint}"
        )
        op.execute(
            f"ALTER TABLE spine.{table} ADD CONSTRAINT {constraint} CHECK "
            "(state IN ('running', 'succeeded', 'resumable', 'reconciliation_required'))"
        )
