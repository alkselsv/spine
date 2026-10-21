"""Preserve optional source metadata through canonicalization functions."""

from __future__ import annotations

from alembic import op


revision = "20261115_52"
down_revision = "20261114_51"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def upgrade() -> None:
    op.execute(
        f"ALTER FUNCTION {SCHEMA}.source_canonical_nfc(text) IMMUTABLE STRICT"
    )
    op.execute(
        f"ALTER FUNCTION {SCHEMA}.source_canonical_trim(text) IMMUTABLE STRICT"
    )


def downgrade() -> None:
    op.execute(
        f"ALTER FUNCTION {SCHEMA}.source_canonical_nfc(text) IMMUTABLE CALLED ON NULL INPUT"
    )
    op.execute(
        f"ALTER FUNCTION {SCHEMA}.source_canonical_trim(text) IMMUTABLE CALLED ON NULL INPUT"
    )
