"""Create canonical Workspace and Environment tenancy tables.

Revision ID: 20261009_01
Revises: None
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261009_01"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "spine"


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("slug", name="uq_workspaces_slug"),
        sa.CheckConstraint(
            "slug <> ''", name=op.f("ck_workspaces_slug_not_empty")
        ),
        sa.CheckConstraint(
            "display_name <> ''",
            name=op.f("ck_workspaces_display_name_not_empty"),
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "environments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_environments"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            [f"{SCHEMA}.workspaces.id"],
            name="fk_environments_workspace_id_workspaces",
        ),
        sa.UniqueConstraint(
            "workspace_id", "id", name="uq_environments_workspace_id_id"
        ),
        sa.CheckConstraint(
            "kind IN ('development', 'staging', 'production')",
            name=op.f("ck_environments_kind"),
        ),
        sa.CheckConstraint(
            "display_name <> ''",
            name=op.f("ck_environments_display_name_not_empty"),
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_environments_workspace_id",
        "environments",
        ["workspace_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "initial_workspace_bootstrap",
        sa.Column("singleton", sa.Boolean(), nullable=False),
        sa.Column("action_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("executed_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("singleton", name="pk_initial_workspace_bootstrap"),
        sa.UniqueConstraint("action_id", name="uq_initial_workspace_bootstrap_action_id"),
        sa.UniqueConstraint(
            "workspace_id", name="uq_initial_workspace_bootstrap_workspace_id"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            [f"{SCHEMA}.workspaces.id"],
            name="fk_initial_workspace_bootstrap_workspace_id_workspaces",
        ),
        sa.CheckConstraint(
            "singleton",
            name=op.f("ck_initial_workspace_bootstrap_singleton"),
        ),
        schema=SCHEMA,
    )
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM PUBLIC")


def downgrade() -> None:
    op.drop_table("initial_workspace_bootstrap", schema=SCHEMA)
    op.drop_index("ix_environments_workspace_id", table_name="environments", schema=SCHEMA)
    op.drop_table("environments", schema=SCHEMA)
    op.drop_table("workspaces", schema=SCHEMA)
