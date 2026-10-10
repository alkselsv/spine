"""Persist the canonicalization profile used for each source revision."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa



revision = "20261010_09"
down_revision = "20261010_08"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.add_column(
        "source_revisions", sa.Column(
            "canonicalization_profile", sa.Text(), nullable=False,
            server_default=sa.text("'r1-c14n-2026-10'"),
        ), schema=SCHEMA,
    )
    op.add_column(
        "source_revisions", sa.Column(
            "unicode_table_digest", sa.Text(), nullable=False,
            server_default=sa.text("'12f429d27cedef784dcda284ec37555ac092a05f4665b9fcd335ec36d05ebb8d'"),
        ), schema=SCHEMA,
    )
    op.add_column(
        "source_revisions", sa.Column(
            "bcp47_table_digest", sa.Text(), nullable=False,
            server_default=sa.text("'d03ad7c70a60b0d9dcbf80d805ae1308e690f378c93206e3a9af303261a531a6'"),
        ), schema=SCHEMA,
    )
    op.alter_column("source_revisions", "canonicalization_profile", server_default=None, schema=SCHEMA)
    op.alter_column("source_revisions", "unicode_table_digest", server_default=None, schema=SCHEMA)
    op.alter_column("source_revisions", "bcp47_table_digest", server_default=None, schema=SCHEMA)
    op.create_check_constraint(
        "ck_source_revisions_canonical_profile", "source_revisions",
         "canonicalization_profile = 'r1-c14n-2026-10' AND unicode_table_digest = '12f429d27cedef784dcda284ec37555ac092a05f4665b9fcd335ec36d05ebb8d' AND bcp47_table_digest = 'd03ad7c70a60b0d9dcbf80d805ae1308e690f378c93206e3a9af303261a531a6'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_source_revisions_metadata_digest", "source_revisions",
        "revision_metadata_digest IS NULL OR revision_metadata_digest OPERATOR(pg_catalog.~) '^[0-9a-f]{64}$'",
        schema=SCHEMA,
    )


def downgrade() -> None:
    for constraint in ("ck_source_revisions_metadata_digest", "ck_source_revisions_canonical_profile"):
        op.drop_constraint(constraint, table_name="source_revisions", schema=SCHEMA, type_="check")
    for column in ("bcp47_table_digest", "unicode_table_digest", "canonicalization_profile"):
        op.drop_column("source_revisions", column, schema=SCHEMA)
