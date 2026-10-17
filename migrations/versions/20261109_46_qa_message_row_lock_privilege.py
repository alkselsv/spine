"""Permit Q&A message versions to be locked for append-only transitions."""

from __future__ import annotations

from alembic import op

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261109_46"
down_revision = "20261108_45"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _runtime_role() -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(settings.runtime_role)


def upgrade() -> None:
    runtime = _runtime_role()
    # Row locking needs UPDATE privilege in PostgreSQL. The version tables are
    # append-only, so guard even that narrowly granted column privilege at the
    # database boundary.
    op.execute(
        f'''CREATE OR REPLACE FUNCTION {SCHEMA}.reject_qa_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'Q&A version history is append-only'
        USING ERRCODE = '55000';
END;
$$'''
    )
    for table in ("qa_session_versions", "qa_message_versions"):
        op.execute(
            f'DROP TRIGGER IF EXISTS "trg_{table}_append_only" ON {SCHEMA}."{table}"'
        )
        op.execute(
            f'''CREATE TRIGGER "trg_{table}_append_only"
BEFORE UPDATE OR DELETE ON {SCHEMA}."{table}"
FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_qa_version_mutation()'''
        )
    # PostgreSQL requires UPDATE privilege to acquire SELECT ... FOR UPDATE.
    # Restrict it to an unused-by-writes column; actual changes remain inserts.
    op.execute(
        f'GRANT UPDATE ("version") ON TABLE {SCHEMA}."qa_session_versions" TO {runtime}'
    )
    op.execute(
        f'GRANT UPDATE ("version") ON TABLE {SCHEMA}."qa_message_versions" TO {runtime}'
    )


def downgrade() -> None:
    runtime = _runtime_role()
    for table in ("qa_message_versions", "qa_session_versions"):
        op.execute(
            f'DROP TRIGGER "trg_{table}_append_only" ON {SCHEMA}."{table}"'
        )
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_qa_version_mutation()")
    op.execute(
        f'REVOKE UPDATE ("version") ON TABLE {SCHEMA}."qa_message_versions" FROM {runtime}'
    )
    op.execute(
        f'REVOKE UPDATE ("version") ON TABLE {SCHEMA}."qa_session_versions" FROM {runtime}'
    )
