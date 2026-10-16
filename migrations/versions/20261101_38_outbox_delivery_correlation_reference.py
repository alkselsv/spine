"""Retain safe correlation references on delivery state rows.

Revision ID: 20261101_38
Revises: 20261031_37
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261101_38"
down_revision = "20261031_37"
branch_labels = None
depends_on = None

SCHEMA = "spine"


def upgrade() -> None:
    op.add_column(
        "outbox_deliveries",
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.execute(
        "UPDATE spine.outbox_deliveries AS delivery "
        "SET correlation_id = intent.correlation_id "
        "FROM spine.outbox_intents AS intent "
        "WHERE intent.event_id = delivery.event_id"
    )


def downgrade() -> None:
    op.drop_column("outbox_deliveries", "correlation_id", schema=SCHEMA)
