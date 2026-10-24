"""Allow bounded qualification failure codes in the ingestion ledger."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261121_58"
down_revision = "20261120_57"
branch_labels = None
depends_on = None
SCHEMA = "spine"
CONSTRAINT = "ck_ingestion_failures_code"

BASE_CODES = (
    "invalid_input",
    "storage_unavailable",
    "storage_integrity_conflict",
    "observation_conflict",
    "parser_failed",
    "locator_failed",
    "persistence_failed",
    "cancelled",
)
QUALIFICATION_CODES = (
    "parser_retryable",
    "original_storage_unavailable",
    "pre_acceptance_retryable",
)


def _check(codes: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{code}'" for code in codes)
    return f"code IN ({quoted})"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "ingestion_failures", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        "ingestion_failures",
        _check(BASE_CODES + QUALIFICATION_CODES),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "ingestion_failures", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        "ingestion_failures",
        _check(BASE_CODES),
        schema=SCHEMA,
    )
