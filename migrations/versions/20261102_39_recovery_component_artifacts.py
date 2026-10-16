"""Persist component artifact verification in coordinated recovery manifests."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261102_39"
down_revision = "20261101_38"
branch_labels = None
depends_on = None

SCHEMA = "spine"


def upgrade() -> None:
    op.add_column(
        "storage_recovery_manifests",
        sa.Column("backup_profile_version", sa.Text()),
        schema=SCHEMA,
    )
    op.add_column(
        "storage_recovery_manifests",
        sa.Column("staged_state_policy", sa.Text()),
        schema=SCHEMA,
    )
    op.add_column(
        "storage_recovery_manifests",
        sa.Column("backup_protection_profile", postgresql.JSONB()),
        schema=SCHEMA,
    )
    op.add_column(
        "storage_recovery_manifests",
        sa.Column("failure_category", sa.String(length=64)),
        schema=SCHEMA,
    )
    op.add_column(
        "storage_recovery_manifests",
        sa.Column("postgres_component_evidence", postgresql.JSONB()),
        schema=SCHEMA,
    )
    op.add_column(
        "storage_recovery_manifests",
        sa.Column("object_component_evidence", postgresql.JSONB()),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column(
        "storage_recovery_manifests",
        "object_component_evidence",
        schema=SCHEMA,
    )
    op.drop_column(
        "storage_recovery_manifests",
        "postgres_component_evidence",
        schema=SCHEMA,
    )
    op.drop_column(
        "storage_recovery_manifests",
        "failure_category",
        schema=SCHEMA,
    )
    op.drop_column(
        "storage_recovery_manifests",
        "backup_protection_profile",
        schema=SCHEMA,
    )
    op.drop_column(
        "storage_recovery_manifests",
        "staged_state_policy",
        schema=SCHEMA,
    )
    op.drop_column(
        "storage_recovery_manifests",
        "backup_profile_version",
        schema=SCHEMA,
    )
