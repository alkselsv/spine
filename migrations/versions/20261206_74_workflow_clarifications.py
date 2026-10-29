"""Allow typed clarification commands and disclosure-safe timeline events."""

from alembic import op


revision = "20261206_74"
down_revision = "20261205_73"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts",
        "command_type IN ("
        "'add_context', 'edit_requirement', 'save_review', 'request_rework', "
        "'answer_clarification', 'cancel'"
        ")",
        schema=SCHEMA,
    )
    op.drop_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events",
        "event_type IN ("
        "'workflow.run-started', "
        "'workflow.context-added', "
        "'workflow.requirement-edited', "
        "'workflow.requirements-reviewed', "
        "'workflow.requirements-rework-requested', "
        "'workflow.clarification-requested', "
        "'workflow.clarification-answered', "
        "'workflow.run-cancelled'"
        ")",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_run_events_type"),
        "workflow_run_events",
        "event_type IN ('workflow.run-started', 'workflow.context-added', "
        "'workflow.requirement-edited', 'workflow.requirements-reviewed', "
        "'workflow.requirements-rework-requested')",
        schema=SCHEMA,
    )
    op.drop_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts",
        schema=SCHEMA,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_workflow_command_receipts_type"),
        "workflow_command_receipts",
        "command_type IN ('add_context', 'edit_requirement', 'save_review', 'request_rework')",
        schema=SCHEMA,
    )
