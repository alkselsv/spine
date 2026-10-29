"""Persist governed transcript publications and derived text projections."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261212_80"
down_revision = "20261211_79"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "transcript_publications",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("publication_id", uuid, nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("publication_digest", sa.String(64), nullable=False),
        sa.Column("predecessor_publication_id", uuid, nullable=True),
        sa.Column("source_object_id", uuid, nullable=False),
        sa.Column("source_revision_id", uuid, nullable=False),
        sa.Column("source_revision_digest", sa.String(64), nullable=False),
        sa.Column("transcript_artifact_id", uuid, nullable=False),
        sa.Column("transcript_artifact_digest", sa.String(64), nullable=False),
        sa.Column("transcript_artifact_schema_version", sa.Text(), nullable=False),
        sa.Column("policy_reference", sa.Text(), nullable=False),
        sa.Column("approval_id", uuid, nullable=True),
        sa.Column("partial_allowed", sa.Boolean(), nullable=False),
        sa.Column("projection_version", sa.Text(), nullable=False),
        sa.Column("projection_state", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("segments", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "environment_id", "publication_id",
            name="pk_transcript_publications",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "source_object_id", "source_revision_id"],
            [
                f"{SCHEMA}.source_revisions.workspace_id",
                f"{SCHEMA}.source_revisions.environment_id",
                f"{SCHEMA}.source_revisions.source_object_id",
                f"{SCHEMA}.source_revisions.revision_id",
            ],
            name="fk_transcript_publications_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id", "transcript_artifact_id"],
            [
                f"{SCHEMA}.transcription_artifacts.workspace_id",
                f"{SCHEMA}.transcription_artifacts.environment_id",
                f"{SCHEMA}.transcription_artifacts.artifact_id",
            ],
            name="fk_transcript_publications_artifact",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'revoked', 'superseded', 'stale')",
            name=op.f("ck_transcript_publications_state"),
        ),
        sa.CheckConstraint(
            "projection_state IN ('current', 'stale')",
            name=op.f("ck_transcript_publications_projection_state"),
        ),
        schema=SCHEMA,
        if_not_exists=True,
    )
    op.create_index(
        "ix_transcript_publications_source_revision",
        "transcript_publications",
        ["workspace_id", "environment_id", "source_revision_id"],
        schema=SCHEMA,
        if_not_exists=True,
    )
    op.create_index(
        "uq_transcript_publications_artifact",
        "transcript_publications",
        ["workspace_id", "environment_id", "transcript_artifact_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        schema=SCHEMA,
        if_not_exists=True,
    )
    _tenant_scope("transcript_publications", mutable=True)


def _tenant_scope(table: str, *, mutable: bool = False) -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."{table}"'
    ws_setting = "pg_catalog.current_setting('spine.workspace_id', true)"
    env_setting = "pg_catalog.current_setting('spine.environment_id', true)"
    pattern = "^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    ws = f"CASE WHEN {ws_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({ws_setting} AS pg_catalog.uuid) ELSE NULL END"
    env = f"CASE WHEN {env_setting} OPERATOR(pg_catalog.~) '{pattern}' THEN CAST({env_setting} AS pg_catalog.uuid) ELSE NULL END"
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(f'DROP POLICY IF EXISTS "pol_{table}_tenant_isolation" ON {qualified}')
    op.execute(f'DROP POLICY IF EXISTS "pol_{table}_migration_maintenance" ON {qualified}')
    op.execute(
        f'CREATE POLICY "pol_{table}_tenant_isolation" ON {qualified} FOR ALL TO {runtime} '
        f"USING (workspace_id = ({ws}) AND environment_id = ({env})) "
        f"WITH CHECK (workspace_id = ({ws}) AND environment_id = ({env}))"
    )
    op.execute(
        f'CREATE POLICY "pol_{table}_migration_maintenance" ON {qualified} FOR ALL TO {migration} '
        "USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    if mutable:
        op.execute(
            f"REVOKE UPDATE ON TABLE {qualified} FROM {runtime}"
        )
        op.execute(
            f"GRANT UPDATE (is_current) "
            f"ON TABLE {qualified} TO {runtime}"
        )
    op.execute(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}")


def downgrade() -> None:
    qualified = f'{SCHEMA}."transcript_publications"'
    op.execute(f'DROP POLICY IF EXISTS "pol_transcript_publications_tenant_isolation" ON {qualified}')
    op.execute(f'DROP POLICY IF EXISTS "pol_transcript_publications_migration_maintenance" ON {qualified}')
    op.drop_index(
        "uq_transcript_publications_artifact",
        table_name="transcript_publications",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_transcript_publications_source_revision",
        table_name="transcript_publications",
        schema=SCHEMA,
    )
    op.drop_table("transcript_publications", schema=SCHEMA)
