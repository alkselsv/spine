"""Persist trusted trace and correlation references on metric source events."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261105_42"
down_revision = "20261104_41"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.add_column(
        "operational_metric_events",
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "operational_metric_events",
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_operational_metric_events_trace_correlation",
        "operational_metric_events",
        ["workspace_id", "environment_id", "trace_id", "correlation_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operational_metric_events_trace_correlation",
        table_name="operational_metric_events",
        schema=SCHEMA,
    )
    op.drop_column("operational_metric_events", "correlation_id", schema=SCHEMA)
    op.drop_column("operational_metric_events", "trace_id", schema=SCHEMA)
