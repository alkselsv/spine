"""Record authorization revocation as a suspended Google sync state."""

from alembic import op


revision = "20261129_66"
down_revision = "20261128_65"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_drive_snapshot_runs_state", "drive_snapshot_runs", schema="spine"
    )
    op.create_check_constraint(
        "ck_drive_snapshot_runs_state",
        "drive_snapshot_runs",
        "state IN ('running', 'succeeded', 'resumable', 'reconciliation_required', 'suspended', 'policy_blocked')",
        schema="spine",
    )
    op.drop_constraint(
        "ck_drive_change_runs_state", "drive_change_runs", schema="spine"
    )
    op.create_check_constraint(
        "ck_drive_change_runs_state",
        "drive_change_runs",
        "state IN ('running', 'succeeded', 'resumable', 'reconciliation_required', 'suspended', 'policy_blocked')",
        schema="spine",
    )
    op.drop_constraint(
        "ck_google_sheets_sync_runs_state", "google_sheets_sync_runs", schema="spine"
    )
    op.create_check_constraint(
        "ck_google_sheets_sync_runs_state",
        "google_sheets_sync_runs",
        "state IN ('running', 'succeeded', 'resumable', 'reconciliation_required', 'suspended', 'policy_blocked')",
        schema="spine",
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
    for name, table in (
        ("ck_google_sheets_sync_runs_state", "google_sheets_sync_runs"),
        ("ck_drive_change_runs_state", "drive_change_runs"),
        ("ck_drive_snapshot_runs_state", "drive_snapshot_runs"),
    ):
        op.drop_constraint(name, table, schema="spine")
    for name, table in (
        ("ck_google_sheets_sync_runs_state", "google_sheets_sync_runs"),
        ("ck_drive_change_runs_state", "drive_change_runs"),
        ("ck_drive_snapshot_runs_state", "drive_snapshot_runs"),
    ):
        op.create_check_constraint(
            name,
            table,
            "state IN ('running', 'succeeded', 'resumable', 'reconciliation_required')",
            schema="spine",
        )
