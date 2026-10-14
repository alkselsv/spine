"""Persist immutable server-approved ContextProfile versions."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from spine.infrastructure.db.settings import MigrationDatabaseSettings


revision = "20261023_27"
down_revision = "20261022_26"
branch_labels = None
depends_on = None
SCHEMA = "spine"


def _role(name: str) -> str:
    settings = op.get_context().config.attributes.get("migration_settings")
    if not isinstance(settings, MigrationDatabaseSettings):
        raise RuntimeError("migration_settings must be MigrationDatabaseSettings")
    return op.get_bind().dialect.identifier_preparer.quote(getattr(settings, name))


def _tenant() -> None:
    runtime = _role("runtime_role")
    migration = _role("migration_role")
    qualified = f'{SCHEMA}."context_profiles"'
    scope = (
        "workspace_id = (CASE WHEN pg_catalog.current_setting('spine.workspace_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.workspace_id', true) AS pg_catalog.uuid) ELSE NULL END) "
        "AND environment_id = (CASE WHEN pg_catalog.current_setting('spine.environment_id', true) "
        "OPERATOR(pg_catalog.~) '^[0-9A-Fa-f-]{36}$' THEN "
        "CAST(pg_catalog.current_setting('spine.environment_id', true) AS pg_catalog.uuid) ELSE NULL END)"
    )
    op.execute(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY")
    op.execute(
        f'CREATE POLICY "pol_context_profiles_tenant_isolation" ON {qualified} '
        f"FOR ALL TO {runtime} USING ({scope}) WITH CHECK ({scope})"
    )
    op.execute(
        f'CREATE POLICY "pol_context_profiles_migration_maintenance" ON {qualified} '
        f"FOR ALL TO {migration} USING (true) WITH CHECK (true)"
    )
    op.execute(f"GRANT SELECT, INSERT ON TABLE {qualified} TO {runtime}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {qualified} FROM {runtime}"
    )


def _immutable() -> None:
    qualified = f'{SCHEMA}."context_profiles"'
    op.execute(
        f"""
        CREATE FUNCTION {SCHEMA}.reject_context_profiles_mutation() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, {SCHEMA}
        AS $function$ BEGIN
            RAISE EXCEPTION 'Context profile history is immutable.'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_context_profiles_immutable';
        END; $function$
        """
    )
    op.execute(
        f"CREATE TRIGGER trg_context_profiles_immutable BEFORE UPDATE OR DELETE ON "
        f"{qualified} FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_context_profiles_mutation()"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION {SCHEMA}.reject_context_profiles_mutation() FROM PUBLIC"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "context_profiles",
        sa.Column("profile_id", uuid, nullable=False),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("environment_id", uuid, nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("retrieval_strategy", sa.Text(), nullable=False),
        sa.Column("max_results", sa.Integer(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("max_relation_hops", sa.Integer(), nullable=False),
        sa.Column("freshness_max_age_seconds", sa.Integer(), nullable=False),
        sa.Column("completeness_requirement", sa.Text(), nullable=False),
        sa.Column("partial_answer_fallback", sa.Text(), nullable=False),
        sa.Column("server_approved", sa.Boolean(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("predecessor_profile_id", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "environment_id",
            "profile_id",
            "profile_version",
            name="pk_context_profiles",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "environment_id",
            "profile_id",
            "profile_digest",
            name="uq_context_profiles_identity_digest",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "environment_id"],
            [f"{SCHEMA}.environments.workspace_id", f"{SCHEMA}.environments.id"],
            name="fk_context_profiles_environment",
        ),
        sa.CheckConstraint(
            "profile_version > 0", name="ck_context_profiles_version_positive"
        ),
        sa.CheckConstraint(
            "schema_version = 'context-profile:v1'",
            name="ck_context_profiles_schema",
        ),
        sa.CheckConstraint(
            "profile_digest ~ '^[0-9a-f]{64}$'",
            name="ck_context_profiles_digest",
        ),
        sa.CheckConstraint(
            "retrieval_strategy IN ('chunks', 'hybrid', 'graph_completion')",
            name="ck_context_profiles_strategy",
        ),
        sa.CheckConstraint(
            "max_results BETWEEN 1 AND 1000 AND max_tokens BETWEEN 1 AND 65536 "
            "AND max_relation_hops BETWEEN 0 AND 8 "
            "AND freshness_max_age_seconds BETWEEN 0 AND 31536000",
            name="ck_context_profiles_bounds",
        ),
        sa.CheckConstraint(
            "completeness_requirement IN ('best_effort', 'complete', 'exhaustive')",
            name="ck_context_profiles_completeness",
        ),
        sa.CheckConstraint(
            "partial_answer_fallback IN ('allowed', 'disallowed')",
            name="ck_context_profiles_partial_fallback",
        ),
        sa.CheckConstraint(
            "server_approved", name="ck_context_profiles_server_approved"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_context_profiles_lookup",
        "context_profiles",
        ["workspace_id", "environment_id", "profile_id", "profile_version"],
        schema=SCHEMA,
    )
    _tenant()
    _immutable()


def downgrade() -> None:
    runtime = _role("runtime_role")
    qualified = f'{SCHEMA}."context_profiles"'
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM {runtime}")
    op.drop_index("ix_context_profiles_lookup", table_name="context_profiles", schema=SCHEMA)
    op.drop_table("context_profiles", schema=SCHEMA)
    op.execute(f"DROP FUNCTION {SCHEMA}.reject_context_profiles_mutation()")
